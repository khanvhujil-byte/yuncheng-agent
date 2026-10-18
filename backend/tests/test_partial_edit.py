"""Local edits preserve committed facts, isolate users and never run the graph."""
import copy
from datetime import date, datetime, timedelta, timezone
import json
import time

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app.api.main import app
from app.models.dialog import ModuleEdit, TurnInput
from app.models.schemas import Restaurant, TripPlanResponse
from app.services import conversation, dialog_store, identity, partial_edit, planning, runner, store
from app.services.itinerary_experience import enrich_experience
from app.agents.budget import patch_budget
from app.agents.verify_budget import verify_budget
from tests.factories import make_attraction, make_day, make_hotel, make_request, make_weather


@pytest.fixture
def saved(monkeypatch):
    from app.services import itinerary_experience
    monkeypatch.setattr(itinerary_experience.amap, "travel_route", lambda *a, **kw: {
        "duration_minutes": 4, "distance_meters": 350, "status": "known"})
    user = identity.register("local_editor", "valid-password")["user_id"]
    req = make_request(travel_days=2, must_visit=[])
    spots = [make_attraction(name, ticket_price=20, ticket_price_status="known") for name in ("博物馆甲", "博物馆乙", "博物馆丙", "博物馆丁", "博物馆戊")]
    hotels = [make_hotel(price=price, nights=1).model_copy(update={"name": name, "poi_id": identifier})
              for price, name, identifier in ((200, "酒店甲", "hotel-a"), (300, "酒店乙", "hotel-b"))]
    restaurants = [Restaurant(name=f"餐馆{i}", poi_id=f"food-{i}", location=spots[0].location,
                              avg_cost=30+i, avg_cost_status="estimated", cost_source="amap") for i in range(8)]
    days = [make_day(index, (date.fromisoformat(req.start_date) + timedelta(days=index)).isoformat(),
                     spots[index*2:index*2+2]) for index in range(2)]
    state = {"request": req, "attractions": spots, "hotels": hotels, "hotel": hotels[0],
             "restaurants": restaurants, "must_visit_resolved": [], "nights": 1, "travel_days": 2,
             "weather": [make_weather(d["date"]) for d in days], "experience_enabled": True,
             "sources": {"attractions": "amap", "weather": "amap", "hotel": "amap"}, "route_cache": {}}
    plan = {"city": req.city, "start_date": req.start_date, "end_date": days[-1]["date"], "days": days,
            "hotel": hotels[0].model_dump(), "hotel_nights": 1, "hotel_reason": "原先的原因",
            "weather_info": [w.model_dump() for w in state["weather"]], "warnings": [], "sources": state["sources"]}
    plan, errors, _ = enrich_experience(plan, state)
    assert not errors
    state["plan"] = plan
    state.update(patch_budget(state))
    state.update(verify_budget(state))
    original = TripPlanResponse(success=True, data=state["plan"], thread_id="original-run").model_dump(mode="json")
    current = dialog_store.create_conversation(user, req.model_dump())
    identifier = current["conversation_id"]
    with store._connect() as db:
        db.execute("UPDATE conversation SET committed_json=?,result_json=? WHERE conversation_id=?",
                   (dialog_store.encode(req.model_dump()), dialog_store.encode(original), identifier))
        db.execute("INSERT INTO owned_run VALUES(?,?,?)", ("original-run", user, identifier))
        db.execute("INSERT INTO run_snapshot VALUES(?,?,?)", ("original-run", dialog_store.encode(state), time.time()))
    monkeypatch.setattr(planning, "plan", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("local edit must not run planning graph")))
    return user, identifier, original


def turn(saved, revision, action, **kwargs):
    user, identifier, _ = saved
    return conversation.turn(identifier, user, TurnInput(request_id=f"edit-request-{revision}", expected_revision=revision,
                                                        action=action, **kwargs))


def current(saved):
    return dialog_store.load_conversation(saved[1], saved[0])


def test_meal_preview_confirm_and_idempotency_preserve_other_modules(saved):
    original = saved[2]
    draft = turn(saved, 0, "edit", edit={"module": "meals", "day_index": 0, "item_index": 1, "candidate_id": "food-7"})
    assert draft["result"] == original
    assert draft["pending_edit"]["module"] == "meals"
    assert draft["edit_preview"]["success"] is False and draft["edit_preview"]["error_code"] == "EDIT_PENDING"
    assert draft["candidate_result"] is None
    assert draft["edit_preview"]["data"]["days"][0]["meals"][1]["restaurant_id"] == "food-7"
    assert current(saved)["pending_edit"] == draft["pending_edit"]
    confirmed = turn(saved, 1, "confirm")
    assert confirmed["candidate_result"]["success"]
    assert confirmed["pending_edit"] is None
    new, old = confirmed["result"]["data"], original["data"]
    assert new["hotel"] == old["hotel"]
    assert new["weather_info"] == old["weather_info"]
    assert new["days"][0]["attractions"] == old["days"][0]["attractions"]
    assert new["days"][1]["meals"] == old["days"][1]["meals"]
    assert new["days"][1]["schedule"] == old["days"][1]["schedule"]
    assert new["days"][0]["meals"][0] == old["days"][0]["meals"][0]
    assert new["days"][0]["meals"][2] == old["days"][0]["meals"][2]
    assert new["budget"]["total_meals"] != old["budget"]["total_meals"]
    replay = turn(saved, 1, "confirm")
    assert replay == confirmed
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM conversation_version").fetchone()[0] == 1


def test_reject_edit_restores_previous_and_does_not_generate(saved):
    turn(saved, 0, "edit", edit={"module": "hotel", "candidate_id": "hotel-b"})
    response = turn(saved, 1, "reject")
    assert response["result"] == saved[2]
    assert response["pending_edit"] is None
    assert current(saved)["_pending_edit"] is None


@pytest.mark.parametrize("edit", [
    {"module": "meals", "day_index": 2, "item_index": 0, "candidate_id": "food-7"},
    {"module": "meals", "day_index": 0, "item_index": 7, "candidate_id": "food-7"},
    {"module": "meals", "day_index": 0, "candidate_id": "food-7"},
    {"module": "attractions", "day_index": 0, "item_index": 0, "candidate_id": "invented-poi"},
    {"module": "attractions", "day_index": 0, "item_index": 0, "candidate_id": "POI-博物馆丙"},
    {"module": "hotel", "candidate_id": "hotel-a"},
    {"module": "hotel", "day_index": 0, "candidate_id": "hotel-b"},
    {"module": "day", "day_index": 0, "candidate_ids": ["POI-博物馆戊", "POI-博物馆戊"]},
])
def test_invalid_edits_retain_state_and_release_lock(saved, edit):
    with pytest.raises(HTTPException) as error:
        turn(saved, 0, "edit", edit=edit)
    assert error.value.status_code == 422
    restored = current(saved)
    assert restored["result"] == saved[2]
    assert restored["revision"] == 0 and not restored["busy"]


def test_attraction_and_day_edits_preserve_meals_and_other_day(saved):
    turn(saved, 0, "edit", edit={"module": "attractions", "day_index": 0, "item_index": 0, "candidate_id": "POI-博物馆戊"})
    response = turn(saved, 1, "confirm")
    assert response["candidate_result"]["success"]
    old, edited = saved[2]["data"], response["result"]["data"]
    assert edited["days"][0]["meals"] == old["days"][0]["meals"]
    assert edited["days"][1]["attractions"] == old["days"][1]["attractions"]
    turn(saved, 2, "edit", edit={"module": "day", "day_index": 0, "candidate_ids": ["POI-博物馆乙", "POI-博物馆戊"]})
    response = turn(saved, 3, "confirm")
    assert response["candidate_result"]["success"]
    assert [a["poi_id"] for a in response["result"]["data"]["days"][0]["attractions"]] == ["POI-博物馆乙", "POI-博物馆戊"]


def test_hotel_edit_recomputes_derived_days_but_preserves_restaurants(saved):
    turn(saved, 0, "edit", edit={"module": "hotel", "candidate_id": "hotel-b"})
    confirmed = turn(saved, 1, "confirm")
    assert confirmed["candidate_result"]["success"]
    plan = confirmed["result"]["data"]
    assert plan["hotel"]["poi_id"] == "hotel-b"
    assert plan["budget"]["total_hotels"] == 300
    from app.services import catalog
    assert {h["poi_id"] for h in catalog.list_places("杭州", "hotel")} == {"hotel-b"}
    for old, new in zip(saved[2]["data"]["days"], plan["days"]):
        assert new["attractions"] == old["attractions"]
        assert new["meals"] == old["meals"]
        assert new["route_legs"][0]["from_name"] == "酒店乙"


def test_budget_failure_keeps_original_and_specific_candidate_reason(saved):
    with store._connect() as db:
        request = current(saved)["committed"]
        request["budget_limit"] = 1
        db.execute("UPDATE conversation SET slots_json=?,committed_json=? WHERE conversation_id=?",
                   (json.dumps(request), json.dumps(request), saved[1]))
    turn(saved, 0, "edit", edit={"module": "hotel", "candidate_id": "hotel-b"})
    failed = turn(saved, 1, "confirm")
    assert failed["candidate_result"]["success"] is False
    assert "预算超限" in failed["candidate_result"]["message"]
    assert failed["result"] == saved[2]
    assert failed["pending_edit"] is not None


def test_local_edit_filters_restaurants_by_committed_requirements(saved):
    request = current(saved)["committed"]
    request["requirements"]["food"]["max_cost_per_meal"] = 35
    with store._connect() as db:
        db.execute("UPDATE conversation SET slots_json=?,committed_json=? WHERE conversation_id=?",
                   (json.dumps(request), json.dumps(request), saved[1]))
    with pytest.raises(HTTPException) as error:
        turn(saved, 0, "edit", edit={"module": "meals", "day_index": 0, "item_index": 1, "candidate_id": "food-7"})
    assert error.value.status_code == 422
    assert current(saved)["result"] == saved[2]


def test_local_edit_delivery_rechecks_transport_requirement(saved):
    request = current(saved)["committed"]
    request["requirements"]["transport"]["max_leg_minutes"] = 3
    with store._connect() as db:
        db.execute("UPDATE conversation SET slots_json=?,committed_json=? WHERE conversation_id=?",
                   (json.dumps(request), json.dumps(request), saved[1]))
    turn(saved, 0, "edit", edit={"module": "hotel", "candidate_id": "hotel-b"})
    failed = turn(saved, 1, "confirm")
    assert not failed["candidate_result"]["success"]
    assert "单段3分钟" in failed["candidate_result"]["message"]
    assert failed["result"] == saved[2]


def test_fixed_hotel_and_pending_whole_request_are_protected(saved):
    slots = current(saved)["committed"]
    slots["fixed_hotel"] = "酒店甲"
    with store._connect() as db:
        db.execute("UPDATE conversation SET slots_json=?,committed_json=? WHERE conversation_id=?", (json.dumps(slots), json.dumps(slots), saved[1]))
    assert partial_edit.candidates(current(saved), "hotel")["locked"]
    with pytest.raises(HTTPException) as exc:
        turn(saved, 0, "edit", edit={"module": "hotel", "candidate_id": "hotel-b"})
    assert "用户指定" in str(exc.value.detail)
    turn(saved, 0, "message", slot_updates={"budget_limit": 3000})
    with pytest.raises(HTTPException) as exc:
        turn(saved, 1, "edit", edit={"module": "meals", "day_index": 0, "item_index": 0, "candidate_id": "food-7"})
    assert exc.value.status_code == 409


def test_natural_language_selects_only_verified_target_and_cannot_trigger_graph(saved, monkeypatch):
    replies = iter([
        ('{"intent":"edit_module","edit":{"module":"meals","day_index":0,"item_index":1,"instruction":"换便宜点的午餐"}}', {"llm_calls": 1}),
        ('{"candidate_ids":["food-7"]}', {"llm_calls": 1}),
    ])
    monkeypatch.setattr(conversation.llm, "chat_json", lambda *a: next(replies))
    draft = turn(saved, 0, "message", message="只换第一天的午餐")
    assert draft["intent"] == "edit_module" and draft["pending_edit"]
    assert draft["usage"]["llm_calls"] == 2
    assert turn(saved, 1, "confirm")["candidate_result"]["success"]


def test_natural_language_ambiguous_day_asks_without_silent_full_replan(saved, monkeypatch):
    monkeypatch.setattr(conversation.llm, "chat_json", lambda *a: (
        '{"intent":"edit_module","edit":{"module":"meals","instruction":"换一家"}}', {}))
    response = turn(saved, 0, "message", message="换一家餐馆")
    assert "第几天" in response["reply"]
    assert response["result"] == saved[2]
    assert response["pending_edit"] is None


def test_model_invented_candidate_is_rejected(saved, monkeypatch):
    monkeypatch.setattr(conversation.llm, "chat_json", lambda *a: ('{"candidate_ids":["invented"]}', {}))
    with pytest.raises(HTTPException) as exc:
        turn(saved, 0, "edit", edit={"module": "hotel", "instruction": "换个酒店"})
    assert exc.value.status_code == 422
    assert current(saved)["result"] == saved[2]


def test_candidate_api_is_owned_and_never_exposes_internal_snapshot(saved):
    client = TestClient(app)
    token = identity.login("local_editor", "valid-password")["token"]
    headers = {"X-Session-Token": token}
    url = "/api/conversations/" + saved[1]
    response = client.get(url + "/edit-candidates", params={"module": "meals", "day_index": 0}, headers=headers)
    assert response.status_code == 200
    assert {item["candidate_id"] for item in response.json()["candidates"]} == {f"food-{i}" for i in range(8)}
    assert "_pending_edit" not in client.get(url, headers=headers).json()
    identity.register("another_user", "valid-password")
    other = identity.login("another_user", "valid-password")["token"]
    assert client.get(url + "/edit-candidates?module=hotel", headers={"X-Session-Token": other}).status_code == 404


def test_owned_form_run_import_is_idempotent_and_rejects_foreign_runs(saved, monkeypatch):
    user, _, original = saved
    req = make_request(travel_days=2, must_visit=[])
    response = copy.deepcopy(original)
    response["thread_id"] = "form-run"
    store.save_run("form-run", req)
    with store._connect() as db:
        db.execute("INSERT INTO owned_run VALUES(?,?,?)", ("form-run", user, ""))
    monkeypatch.setattr(runner, "get_job", lambda _: runner.Job(thread_id="form-run", status="done", result=response))
    client = TestClient(app)
    headers = {"X-Session-Token": identity.login("local_editor", "valid-password")["token"]}
    first = client.post("/api/conversations/from-run", json={"thread_id": "form-run"}, headers=headers)
    assert first.status_code == 200
    repeated = client.post("/api/conversations/from-run", json={"thread_id": "form-run"}, headers=headers)
    assert repeated.json()["conversation_id"] == first.json()["conversation_id"]
    assert first.json()["result"] == response
    assert "_pending_edit" not in first.json()
    denied = client.post("/api/conversations/from-run", json={"thread_id": "foreign-run"}, headers=headers)
    assert denied.status_code == 404


def test_confirmed_local_candidate_recovers_without_duplicate_edit(saved, monkeypatch):
    turn(saved, 0, "edit", edit={"module": "hotel", "candidate_id": "hotel-b"})
    actual_finish = dialog_store.finish
    monkeypatch.setattr(dialog_store, "finish", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("lost commit")))
    with pytest.raises(RuntimeError):
        turn(saved, 1, "confirm")
    dialog_store.recover_interrupted()
    monkeypatch.setattr(dialog_store, "finish", actual_finish)
    monkeypatch.setattr(partial_edit, "apply", lambda *a: (_ for _ in ()).throw(AssertionError("completed candidate must be reused")))
    recovered = turn(saved, 1, "confirm")
    assert recovered["result"]["data"]["hotel"]["poi_id"] == "hotel-b"
    assert recovered["pending_edit"] is None


def test_new_local_edit_resets_route_query_budget_and_records_actual_attempts(saved, monkeypatch):
    from app.services import itinerary_experience
    calls = []
    monkeypatch.setattr(itinerary_experience.amap, "travel_route", lambda *a, **kw: calls.append(kw) or {
        "status": "known", "duration_minutes": 5, "distance_meters": 400})
    with store._connect() as db:
        snapshot = json.loads(db.execute("SELECT state_json FROM run_snapshot WHERE run_id='original-run'").fetchone()[0])
        snapshot["route_query_count"] = 48
        snapshot["hotels"][1]["location"]["longitude"] += .002
        db.execute("UPDATE run_snapshot SET state_json=? WHERE run_id='original-run'", (json.dumps(snapshot),))
    turn(saved, 0, "edit", edit={"module": "hotel", "candidate_id": "hotel-b"})
    result = turn(saved, 1, "confirm")
    assert result["candidate_result"]["success"]
    assert calls
    with store._connect() as db:
        report = json.loads(db.execute("SELECT report_json FROM quality_run WHERE run_id=?", (result["run_id"],)).fetchone()[0])
    assert any(item["tool"] == "travel_route" and item["attempt_count"] == 1 for item in report["tools"])


@pytest.mark.parametrize("module,pool_key,unit,status,identifier", [
    ("hotel", "hotels", "night", "known", "HOTELB"),
    ("meals", "restaurants", "person_meal", "estimated", "FOOD7"),
    ("attractions", "attractions", "admission", "known", "SPOT5"),
])
def test_sourced_prices_expiring_between_preview_and_confirmation_are_not_reused(saved, module, pool_key, unit, status, identifier):
    from app.services import price_catalog
    offset = {"hotel": 1, "meals": 7, "attractions": 4}[module]
    with store._connect() as db:
        snapshot = json.loads(db.execute("SELECT state_json FROM run_snapshot WHERE run_id='original-run'").fetchone()[0])
        snapshot[pool_key][offset]["poi_id"] = identifier
        db.execute("UPDATE run_snapshot SET state_json=? WHERE run_id='original-run'", (json.dumps(snapshot),))
    now = datetime.now(timezone.utc)
    price_catalog.import_prices([{
        "city": "杭州", "kind": {"hotel": "hotel", "meals": "restaurant", "attractions": "attraction"}[module],
        "poi_id": identifier, "amount": 950, "unit": unit, "status": status,
        "source_url": "https://official.example/quote", "observed_at": (now-timedelta(minutes=1)).isoformat(),
        "expires_at": (now+timedelta(hours=1)).isoformat(),
        "service_from": "2026-10-10", "service_to": "2026-10-11"}])
    edit = {"module": module, "candidate_id": identifier}
    if module != "hotel":
        edit.update(day_index=0, item_index=1 if module == "meals" else 0)
    preview = turn(saved, 0, "edit", edit=edit)["edit_preview"]["data"]
    if module == "hotel":
        assert preview["hotel"]["estimated_cost"] == 950
    elif module == "meals":
        assert preview["days"][0]["meals"][1]["estimated_cost"] == 950
    else:
        assert preview["days"][0]["attractions"][0]["ticket_price"] == 950
    with store._connect() as db:
        db.execute("UPDATE price_observation SET expires_at=?", ((now-timedelta(seconds=1)).isoformat(),))
    confirmed = turn(saved, 1, "confirm")
    assert confirmed["candidate_result"]["success"]
    plan = confirmed["result"]["data"]
    if module == "hotel":
        assert plan["hotel"]["estimated_cost"] == 0
        assert plan["hotel"]["cost_status"] == "unknown"
        assert plan["budget"]["total_hotels"] == 0
        assert any("住宿" in label for label in plan["budget"]["unknown_items"])
    elif module == "meals":
        meal = plan["days"][0]["meals"][1]
        assert meal["estimated_cost"] == 60 and meal["cost_source"] == "static_estimate"
        assert meal["cost_status"] == "estimated"
    else:
        spot = plan["days"][0]["attractions"][0]
        assert spot["ticket_price"] is None and spot["ticket_price_status"] == "unknown"
        assert spot["name"] in plan["budget"]["unknown_items"]


def test_cached_run_without_collection_snapshot_recovers_verified_meals_and_catalog_alternatives(saved):
    from app.services import catalog
    current_plan = saved[2]["data"]
    alternative = Restaurant(name="资料库新增餐馆", poi_id="CATALOG1", location=current_plan["days"][0]["attractions"][0]["location"],
                             avg_cost=45, avg_cost_status="estimated", cost_source="amap")
    catalog.upsert_places("杭州", "restaurant", [alternative])
    with store._connect() as db:
        db.execute("UPDATE run_snapshot SET state_json=? WHERE run_id='original-run'",
                   (json.dumps({"request": current(saved)["committed"], "cached": True}),))
    options = partial_edit.candidates(current(saved), "meals", 0)
    identifiers = {c["candidate_id"] for c in options["candidates"]}
    assert "CATALOG1" in identifiers
    assert {m["restaurant_id"] for day in current_plan["days"] for m in day["meals"]} <= identifiers
    turn(saved, 0, "edit", edit={"module": "meals", "day_index": 0, "item_index": 1, "candidate_id": "CATALOG1"})
    confirmed = turn(saved, 1, "confirm")
    assert confirmed["candidate_result"]["success"]
    assert confirmed["result"]["data"]["days"][0]["meals"][1]["restaurant_id"] == "CATALOG1"
    assert confirmed["result"]["data"]["days"][1]["meals"] == current_plan["days"][1]["meals"]
