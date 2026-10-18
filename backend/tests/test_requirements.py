"""Cross-module intent, durable explicit choices, filtering and independent audits."""
import copy
import json
import time

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from app.api.main import app
from app.models.dialog import IntentDecision, SlotPatch, TurnInput
from app.models.requirements import Requirements
from app.models.schemas import Location, Restaurant, TripPlanResponse
from app.services import conversation, dialog_store, food, identity, planning, store, tools
from app.services.itinerary_experience import _select_meals, enrich_experience
from app.services.requirements import audit_requirements
from app.services.request_validation import validate_request
from tests.factories import make_attraction, make_hotel, make_request
from tests.test_itinerary_experience import setup_trip


def command(revision, action="message", **kwargs):
    return TurnInput(request_id=f"requirements-turn-{revision}", expected_revision=revision, action=action, **kwargs)


@pytest.fixture
def chat():
    user = identity.register("requirements_user", "valid-password")["user_id"]
    current = dialog_store.create_conversation(user, make_request(must_visit=[]).model_dump())
    return user, current["conversation_id"]


def ambiguous_food():
    return IntentDecision.model_validate({"intent": "clarify", "clarifications": [{
        "module": "food", "question": "美食优先时，你愿意专程去较远的餐厅吗？", "options": [
            {"label": "全城找店，围绕餐厅安排", "slot_updates": {"requirements": {
                "food": {"priority": "food_first", "travel_scope": "citywide"}}}},
            {"label": "仍然优先附近", "slot_updates": {"requirements": {
                "food": {"priority": "nearby", "travel_scope": "nearby"}}}}]}]})


def ask(chat, monkeypatch):
    monkeypatch.setattr(conversation, "extract", lambda *a: (ambiguous_food(), {"llm_calls": 1}))
    return conversation.turn(chat[1], chat[0], command(0, message="美食优先，但路上也别太折腾"))


def test_choices_are_durable_not_preselected_and_block_expensive_generation(chat, monkeypatch):
    response = ask(chat, monkeypatch)
    assert response["intent"] == "clarify" and not response["ready"]
    assert response["slots"]["requirements"]["food"]["priority"] == "nearby"
    question = response["clarifications"][0]
    assert all("slot_updates" not in option for option in question["options"])
    restored = dialog_store.load_conversation(chat[1], chat[0])
    assert restored["clarifications"] == response["clarifications"]
    monkeypatch.setattr(planning, "plan", lambda *a, **kw: pytest.fail("must not generate before answering"))
    blocked = conversation.turn(chat[1], chat[0], command(1, "confirm"))
    assert blocked["run_id"] is None and blocked["candidate_result"] is None
    assert blocked["clarifications"] == response["clarifications"]
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM conversation_operation").fetchone()[0] == 0
    selected = command(2, "answer", answer={"question_id": question["question_id"], "option_index": 0})
    answered = conversation.turn(chat[1], chat[0], selected)
    assert answered["ready"] and answered["clarifications"] == []
    assert answered["slots"]["requirements"]["food"]["travel_scope"] == "citywide"
    assert conversation.turn(chat[1], chat[0], selected) == answered


def test_stale_choice_and_cross_user_choice_preserve_state(chat, monkeypatch):
    original = ask(chat, monkeypatch)
    q = original["clarifications"][0]
    for answer in ({"question_id": "stale-id", "option_index": 0},
                   {"question_id": q["question_id"], "option_index": 3}):
        with pytest.raises(HTTPException) as exc:
            conversation.turn(chat[1], chat[0], command(1, "answer", answer=answer))
        assert exc.value.status_code == 409
        restored = dialog_store.load_conversation(chat[1], chat[0])
        assert restored["revision"] == 1 and not restored["busy"]
        assert restored["clarifications"] == original["clarifications"]
    other = identity.register("another_requirements_user", "valid-password")["user_id"]
    with pytest.raises(HTTPException) as exc:
        conversation.turn(chat[1], other, command(1, "answer", answer={"question_id": q["question_id"], "option_index": 0}))
    assert exc.value.status_code == 404


def test_answering_one_question_keeps_other_module_question_pending(chat, monkeypatch):
    decision = ambiguous_food()
    other = decision.clarifications[0].model_copy(update={"module": "transport", "question": "还要确认交通安排吗？"})
    decision.clarifications.append(other)
    monkeypatch.setattr(conversation, "extract", lambda *a: (decision, {}))
    asked = conversation.turn(chat[1], chat[0], command(0, message="确认餐饮和交通"))
    first, second = asked["clarifications"]
    response = conversation.turn(chat[1], chat[0], command(1, "answer", answer={
        "question_id": first["question_id"], "option_index": 0}))
    assert not response["ready"] and response["clarifications"] == [second]


def test_partial_requirement_patch_preserves_other_modules_and_other_food_fields():
    original = make_request(requirements={"food": {"priority": "food_first", "travel_scope": "citywide"},
                                         "hotel": {"instructions": "偏好安静", "max_price_per_night": 500}}).model_dump()
    patch = SlotPatch.model_validate({"requirements": {"food": {"max_cost_per_meal": 100}}})
    merged = conversation.merge(original, patch.model_dump(exclude_unset=True))
    assert merged["requirements"]["food"]["priority"] == "food_first"
    assert merged["requirements"]["food"]["travel_scope"] == "citywide"
    assert merged["requirements"]["food"]["max_cost_per_meal"] == 100
    assert merged["requirements"]["hotel"] == original["requirements"]["hotel"]
    assert original["requirements"]["food"]["max_cost_per_meal"] is None


def test_unrelated_structured_update_does_not_answer_pending_question(chat, monkeypatch):
    first = ask(chat, monkeypatch)
    response = conversation.turn(chat[1], chat[0], command(1, slot_updates={"budget_limit": 3000}))
    assert response["clarifications"] == first["clarifications"]
    assert not response["ready"]


def test_free_text_answer_carries_pending_questions_and_nothing_is_auto_saved(chat, monkeypatch):
    ask(chat, monkeypatch)
    seen = []
    def model(system, user):
        seen.append((system, json.loads(user)))
        return json.dumps({"intent": "modify_plan", "resolved_question_ids": [json.loads(user)["pending_questions"][0]["question_id"]], "slot_updates": {"requirements": {
            "food": {"priority": "food_first", "travel_scope": "citywide"}}}}), {}
    monkeypatch.setattr(conversation, "extract", conversation_extract)
    monkeypatch.setattr(conversation.llm, "chat_json", model)
    response = conversation.turn(chat[1], chat[0], command(1, message="我愿意专程去吃，全城都可以"))
    assert response["ready"] and not response["clarifications"]
    assert seen[0][1]["pending_questions"]
    assert "不自驾" in seen[0][0] and "不能把‘轻松点’" in seen[0][0]
    assert conversation.defaults(chat[0]) == {}


def test_unrelated_natural_language_update_does_not_silently_drop_questions(chat, monkeypatch):
    first = ask(chat, monkeypatch)
    monkeypatch.setattr(conversation, "extract", lambda *a: (IntentDecision.model_validate({
        "intent": "modify_plan", "slot_updates": {"requirements": {"hotel": {"max_price_per_night": 500}}}}), {}))
    response = conversation.turn(chat[1], chat[0], command(1, message="对了，酒店每晚不超过500"))
    assert response["clarifications"] == first["clarifications"] and not response["ready"]
    assert response["slots"]["requirements"]["hotel"]["max_price_per_night"] == 500


conversation_extract = conversation.extract


def test_uncertain_with_no_generated_options_still_cannot_confirm(chat, monkeypatch):
    monkeypatch.setattr(conversation, "extract", lambda *a: (IntentDecision(intent="clarify"), {}))
    response = conversation.turn(chat[1], chat[0], command(0, message="就那样改一下"))
    assert response["clarifications"][0]["options"] == [] and not response["ready"]


def test_question_payloads_and_confirm_answer_cannot_carry_hidden_updates(chat, monkeypatch):
    q = ask(chat, monkeypatch)["clarifications"][0]
    with pytest.raises(HTTPException) as exc:
        conversation.turn(chat[1], chat[0], command(1, "answer", answer={"question_id": q["question_id"], "option_index": 0},
                                                     slot_updates={"city": "上海"}))
    assert exc.value.status_code == 422
    with pytest.raises(ValidationError):
        IntentDecision.model_validate({"intent": "clarify", "clarifications": [{"module": "food", "question": "选哪个？",
            "options": [{"label": "我来替你选"}]}]})
    with pytest.raises(ValidationError):
        SlotPatch.model_validate({"requirements": {"transport": {"max_leg_minutes": "30"}}})
    with pytest.raises(ValidationError):
        IntentDecision.model_validate({"intent": "clarify", "clarifications": [{"module": "food", "question": "选哪个？",
            "options": [{"label": "没有实际作用", "slot_updates": {"requirements": {"food": {}}}}]}]})


def test_reject_clears_questions_without_overwriting_committed_result(chat, monkeypatch):
    ask(chat, monkeypatch)
    response = conversation.turn(chat[1], chat[0], command(1, "reject"))
    assert response["clarifications"] == []
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM conversation_clarification").fetchone()[0] == 0


def test_public_http_get_does_not_expose_option_patches(chat, monkeypatch):
    ask(chat, monkeypatch)
    token = identity.login("requirements_user", "valid-password")["token"]
    response = TestClient(app).get("/api/conversations/" + chat[1], headers={"X-Session-Token": token})
    assert response.status_code == 200
    data = response.json()
    assert "_clarifications" not in data
    assert data["clarifications"][0]["options"] == [{"label": "全城找店，围绕餐厅安排"}, {"label": "仍然优先附近"}]


def test_new_requirements_change_fingerprint_and_prevent_stale_candidate_reuse():
    req = make_request()
    changed = req.model_copy(update={"requirements": Requirements(food={"travel_scope": "citywide"})})
    assert store.fingerprint_of(req) != store.fingerprint_of(changed)
    state = {"request": req.model_dump(), "adcode": "330100", "attractions": [make_attraction("西湖").model_dump()],
             "weather": [{"date": req.start_date}], "sources": {"attractions": "amap", "weather": "amap"}}
    with store._connect() as db:
        db.execute("INSERT INTO run_snapshot VALUES(?,?,?)", ("req-source", json.dumps(state), time.time()))
    assert conversation.reusable_seed({"thread_id": "req-source"}, changed) is None


def test_unverified_hard_requirement_and_conflicting_names_block_preflight(chat, monkeypatch):
    req = make_request(requirements={"food": {"unverified": ["必须有可核实的花生过敏安全保障"]}})
    assert validate_request(req)[0] == "CLARIFICATION_REQUIRED"
    monkeypatch.setattr(planning, "plan", lambda *a, **kw: pytest.fail("unverified hard requirement must block"))
    first = conversation.turn(chat[1], chat[0], command(0, slot_updates={"requirements": req.requirements.model_dump()}))
    assert not first["ready"] and "核验依据" in first["reply"]
    result = conversation.turn(chat[1], chat[0], command(1, "confirm"))
    assert result["run_id"] is None
    conflict = make_request(must_visit=["西湖"], requirements={"attractions": {"excluded_names": ["西湖"]}})
    assert validate_request(conflict)[0] == "CLARIFICATION_REQUIRED"


def test_food_candidates_honor_keywords_exclusions_and_price_cap(monkeypatch):
    requests = []
    def restaurant(name, identifier, price):
        return Restaurant(name=name, poi_id=identifier, location=Location(longitude=120, latitude=30),
                          avg_cost=price, avg_cost_status="estimated" if price is not None else "unknown")
    hits = [restaurant("杭帮菜甲", "r1", 50), restaurant("排除餐厅", "r2", 40),
            restaurant("昂贵餐厅", "r3", 200), restaurant("未知价格", "r4", None)]
    def search(city, keyword, **kw):
        requests.append(keyword)
        return hits
    monkeypatch.setattr(food, "search_restaurants", search)
    req = make_request(requirements={"food": {"keywords": ["杭帮菜"], "excluded_names": ["排除餐厅"], "max_cost_per_meal": 80}})
    result = food.collect_food({"request": req, "experience_enabled": True, "travel_days": 3})
    assert requests[0] == "杭帮菜" and len(requests) <= 5
    assert [r.poi_id for r in result["restaurants"]] == ["r1"]


def test_citywide_food_selection_drops_nearby_bias_but_driving_alone_does_not():
    near = Restaurant(name="普通近店", poi_id="near", location=Location(longitude=120, latitude=30))
    far = Restaurant(name="榜单远店", poi_id="far", location=Location(longitude=120.2, latitude=30), ranking_name="真实榜单", ranking_rank=1)
    day = {"attractions": [make_attraction("景点", lng=120, lat=30).model_dump()], "meals": []}
    nearby = _select_meals(day, [near, far], None, set())
    citywide = _select_meals(day, [near, far], None, set(), needs=Requirements(food={"travel_scope": "citywide"}).food)
    assert nearby[0]["restaurant_id"] == "near"
    assert citywide[0]["restaurant_id"] == "far"


def test_primary_food_keeps_fact_bound_model_anchor_instead_of_overriding_it():
    near = Restaurant(name="近店", poi_id="near", location=Location(longitude=120, latitude=30))
    far = Restaurant(name="指定远店", poi_id="far", location=Location(longitude=120.2, latitude=30))
    day = {"attractions": [], "meals": [{"type": "lunch", "restaurant_id": "far", "name": "模型改名"}]}
    meals = _select_meals(day, [near, far], None, set(), needs=Requirements(food={"priority": "food_first"}).food)
    lunch = next(m for m in meals if m["type"] == "lunch")
    assert lunch["restaurant_id"] == "far" and lunch["name"] == "指定远店"


@pytest.mark.parametrize("module,text", [("attractions", "只考虑博物馆"), ("hotel", "想住湖边"),
                                         ("weather", "下雨安排室内"), ("pace", "午后多休息"),
                                         ("transport", "不想连续坐车"), ("budget", "餐饮多分配预算")])
def test_each_node_receives_relevant_user_instructions(module, text):
    from app.agents import attraction_agent, hotel_agent, planner_agent, weather_agent
    req = make_request(requirements={module: {"instructions": text}})
    state = {"request": req, "travel_days": 3, "nights": 2, "end_date": "2026-10-12", "attractions": []}
    assert text in planner_agent._build_user_prompt(state)
    if module in ("attractions", "weather", "pace", "transport", "budget"):
        assert text in attraction_agent._user_prompt(state)
    if module in ("hotel", "transport", "budget"):
        assert text in hotel_agent._user_prompt(state, 2)
    if module == "weather":
        assert text in weather_agent._user_prompt(state)


def test_tools_enforce_exclusions_and_hotel_reference_cap(monkeypatch):
    req = make_request(requirements={"attractions": {"excluded_names": ["购物"]},
                                    "hotel": {"max_price_per_night": 300}})
    ctx = tools.ToolContext(city=req.city, adcode="330100", start_date=req.start_date,
                            travel_days=3, nights=2, requirements=req.requirements)
    monkeypatch.setattr(tools.amap, "search_poi", lambda *a, **kw: [make_attraction("购物街"), make_attraction("博物馆")])
    tools.execute("attractions", "search_attractions", {"keyword": "景点"}, ctx)
    assert [a.name for a in ctx.attractions.values()] == ["博物馆"]
    ctx.hotels["h"] = make_hotel().model_copy(update={"name": "候选酒店"})
    result = tools.execute("hotels", "choose_hotel", {"hotel": "h", "price_per_night": 500}, ctx)
    assert result.error_code == "REQUIREMENT_VIOLATION" and ctx.chosen_hotel is None
    assert tools.execute("hotels", "choose_hotel", {"hotel": "h", "price_per_night": 250}, ctx).status == "ok"


def test_route_time_and_activity_caps_are_checked_independently(monkeypatch):
    monkeypatch.setattr(tools.amap, "travel_route", lambda *a, **kw: {"duration_minutes": 35, "status": "known"})
    plan, state = setup_trip()
    state["request"] = state["request"].model_copy(update={"requirements": Requirements(
        transport={"max_leg_minutes": 20}, pace={"max_activity_minutes": 90})})
    enriched, errors, _ = enrich_experience(plan, state)
    assert any("单段20分钟" in e for e in errors)
    assert any("最多90分钟" in e for e in errors)
    tampered = copy.deepcopy(enriched)
    tampered["days"][0]["route_legs"][0]["duration_minutes"] = 1
    tampered["days"][0]["route_legs"][0]["status"] = "estimated"
    assert any("单段20分钟" in e for e in audit_requirements(tampered, state["request"]))


def test_unknown_route_and_weather_are_not_claimed_to_meet_user_requirements(monkeypatch):
    monkeypatch.setattr(tools.amap, "travel_route", lambda *a, **kw: {})
    plan, state = setup_trip()
    state["request"] = state["request"].model_copy(update={"requirements": Requirements(
        transport={"allow_estimated_routes": False}, weather={"avoid_rain_outdoors": True})})
    plan["days"][0]["attractions"][0].update(name="湖边公园", category="公园")
    enriched, errors, _ = enrich_experience(plan, state)
    assert any("真实路线" in e for e in errors) and any("天气未知" in e for e in errors)
    enriched["weather_info"] = [{"date": state["request"].start_date, "day_weather": "小雨"}]
    assert any("预报有雨" in e for e in audit_requirements(enriched, state["request"]))


def test_cached_response_cannot_bypass_user_excluded_attraction(monkeypatch):
    from app.services import amap
    from tests.factories import make_day, make_plan, make_weather
    req = make_request(travel_days=1, nights=0, must_visit=[], requirements={"attractions": {"excluded_names": ["西湖"]}})
    historical = make_plan([make_day(0, req.start_date, [make_attraction("西湖")])],
                           start_date=req.start_date, end_date=req.start_date, hotel_nights=0)
    monkeypatch.setattr(amap, "geocode", lambda *a: (None, "330100"))
    monkeypatch.setattr(amap, "get_weather", lambda *a: [make_weather(req.start_date)])
    response = planning._cached_response({"plan": historical, "hit_count": 1, "created_at": "2026-10-10"}, req, "cache-excluded", "fingerprint", 1)
    assert not response.success and "排除" in response.message
