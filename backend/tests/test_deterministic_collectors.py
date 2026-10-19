"""Fast collectors retain identity, price evidence and changed-plan constraints."""
from datetime import datetime, timedelta, timezone

import pytest

from app.agents import hotel_agent, weather_agent
from app.agents.budget import patch_budget
from app.models.requirements import Requirements
from app.models.schemas import Hotel, Location, WeatherInfo
from app.services import map_gateway, price_catalog, tools
from app.services.tools import ToolContext
from tests.factories import make_attraction, make_request


@pytest.fixture(autouse=True)
def deterministic(monkeypatch, settings):
    monkeypatch.setattr(settings, "hotel_use_agent", False)
    monkeypatch.setattr(settings, "weather_use_agent", False)
    monkeypatch.setattr(hotel_agent, "run_agent", lambda *a, **kw: pytest.fail("no hotel LLM needed"))
    monkeypatch.setattr(weather_agent, "run_agent", lambda *a, **kw: pytest.fail("no weather LLM needed"))


def _poi(name, identity, longitude):
    return make_attraction(name, poi_id=identity, typecode="100100", lng=longitude, lat=30.0)


def _state(req=None, longitude=120.0):
    req = req or make_request(start_date="2026-10-20", travel_days=2, nights=1, must_visit=[])
    return {"request": req, "adcode": "330100", "travel_days": req.travel_days,
            "nights": req.nights, "plan": {"days": [{"day_index": 0, "date": req.start_date,
            "attractions": [make_attraction("景点", lng=longitude, lat=30.0).model_dump()], "meals": []}]}}


def _quote(identity, amount):
    now = datetime.now(timezone.utc)
    return {"city": "杭州", "kind": "hotel", "poi_id": identity, "amount": amount,
            "unit": "night", "status": "known", "source_url": "https://example.com/dated-room",
            "observed_at": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "service_from": "2026-10-20", "service_to": "2026-10-21"}


def test_fast_hotel_uses_all_candidates_and_preserves_dated_price(monkeypatch):
    price_catalog.import_prices([_quote("NEAR", 389)])
    hotels = [_poi(f"较远{i}", f"FAR{i}", 120.1 + i / 100) for i in range(5)]
    hotels.append(_poi("最近酒店", "NEAR", 120.0))
    calls = []
    monkeypatch.setattr(map_gateway, "search_poi", lambda *a, **kw: calls.append(a) or hotels)
    result = hotel_agent.resolve_hotel(_state())
    assert result["hotel"].poi_id == "NEAR"
    assert result["hotel"].estimated_cost == 389 and result["hotel"].cost_status == "known"
    assert result["hotel"].cost_source == "imported"
    assert result["usage"]["llm_calls"] == 0 and len(calls) == 1
    assert [c["tool"] for c in result["tool_calls"]] == ["search_hotels", "rank_hotels", "choose_hotel"]


def test_hotel_reserve_is_labeled_and_counted_without_inventing_a_quote(monkeypatch):
    monkeypatch.setattr(map_gateway, "search_poi", lambda *a, **kw: [_poi("真实酒店", "H1", 120.0)])
    state = _state()
    selected = hotel_agent.resolve_hotel(state)
    assert selected["hotel"].estimated_cost == 260
    assert selected["hotel"].cost_source == "static_estimate"
    assert selected["hotel"].price_evidence_url is None
    assert "不是酒店报价" in selected["warnings"][0]
    state.update(selected)
    priced = patch_budget(state)["plan"]["budget"]
    assert priced["total_hotels"] == 260
    assert next(i for i in priced["items"] if i["category"] == "hotel")["source"] == "static_estimate"


def test_experience_replan_reuses_candidates_but_reranks_for_changed_attractions(monkeypatch):
    calls = []
    hotels = [_poi("西酒店", "WEST", 120.0), _poi("东酒店", "EAST", 120.2)]
    monkeypatch.setattr(map_gateway, "search_poi", lambda *a, **kw: calls.append(a) or hotels)
    state = _state(longitude=120.0)
    first = hotel_agent.resolve_hotel(state)
    state.update(first)
    state["plan"]["days"][0]["attractions"] = [make_attraction("新景点", lng=120.2, lat=30.0).model_dump()]
    second = hotel_agent.resolve_hotel(state)
    assert first["hotel"].poi_id == "WEST" and second["hotel"].poi_id == "EAST"
    assert len(calls) == 1
    assert [c["tool"] for c in second["tool_calls"]] == ["rank_hotels", "choose_hotel"]
    assert first["hotel"].poi_id == "WEST"  # no in-place mutation of old selection


@pytest.mark.parametrize("changed", ["city", "date", "requirements", "accommodation"])
def test_changed_hotel_request_invalidates_candidate_reuse(monkeypatch, changed):
    calls = []
    monkeypatch.setattr(map_gateway, "search_poi", lambda *a, **kw: calls.append(a) or [_poi("酒店", "H1", 120.0)])
    state = _state()
    state.update(hotel_agent.resolve_hotel(state))
    updates = {"city": {"city": "上海"}, "date": {"start_date": "2026-10-22"},
               "requirements": {"requirements": Requirements(hotel={"excluded_names": ["别家酒店"]})},
               "accommodation": {"accommodation": "民宿"}}
    state["request"] = state["request"].model_copy(update=updates[changed])
    result = hotel_agent.resolve_hotel(state)
    assert result["tool_calls"][0]["tool"] == "search_hotels"
    if changed == "city":
        assert len(calls) == 2


def test_hard_nightly_cap_never_uses_a_reserve_as_price_evidence(monkeypatch):
    monkeypatch.setattr(map_gateway, "search_poi", lambda *a, **kw: [_poi("未知房价酒店", "H1", 120.0)])
    req = make_request(start_date="2026-10-20", travel_days=2, nights=1, must_visit=[],
                       requirements=Requirements(hotel={"max_price_per_night": 300}))
    result = hotel_agent.resolve_hotel(_state(req))
    assert result["hotel"] is None
    assert "不能用预算预留" in "".join(result["warnings"])


def test_reused_candidates_refresh_expired_quotes_before_hard_cap(monkeypatch):
    price_catalog.import_prices([_quote("H1", 200)])
    monkeypatch.setattr(map_gateway, "search_poi", lambda *a, **kw: [_poi("酒店", "H1", 120.0)])
    req = make_request(start_date="2026-10-20", travel_days=2, nights=1, must_visit=[],
                       requirements=Requirements(hotel={"max_price_per_night": 300}))
    state = _state(req)
    first = hotel_agent.resolve_hotel(state)
    assert first["hotel"].cost_source == "imported"
    state.update(first)
    price_catalog.import_prices([_quote("H1", 500)])
    second = hotel_agent.resolve_hotel(state)
    assert second["hotel"] is None


def test_same_named_branches_select_by_id(monkeypatch):
    monkeypatch.setattr(map_gateway, "search_poi", lambda *a, **kw: [
        _poi("连锁酒店", "FAR", 120.2), _poi("连锁酒店", "NEAR", 120.0)])
    result = hotel_agent.resolve_hotel(_state())
    assert result["hotel"].poi_id == "NEAR"


def test_total_budget_prefers_affordable_dated_quote_over_nearest_expensive_hotel(monkeypatch):
    price_catalog.import_prices([_quote("NEAR", 400), _quote("FAR", 250)])
    monkeypatch.setattr(map_gateway, "search_poi", lambda *a, **kw: [
        _poi("贵酒店", "NEAR", 120.0), _poi("便宜酒店", "FAR", 120.01)])
    req = make_request(start_date="2026-10-20", travel_days=2, nights=1, must_visit=[], budget_limit=400)
    result = hotel_agent.resolve_hotel(_state(req))
    assert result["hotel"].poi_id == "FAR" and result["hotel"].estimated_cost == 250
    assert "剩余预算" in result["hotel_reason"]


def test_low_budget_does_not_shrink_an_unknown_room_reserve_to_make_budget_pass(monkeypatch):
    monkeypatch.setattr(map_gateway, "search_poi", lambda *a, **kw: [_poi("真实酒店", "H1", 120.0)])
    req = make_request(start_date="2026-10-20", travel_days=2, nights=1, must_visit=[], budget_limit=1)
    state = _state(req)
    state.update(hotel_agent.resolve_hotel(state))
    assert state["hotel"].estimated_cost == 260 and state["hotel"].cost_source == "static_estimate"
    assert patch_budget(state)["plan"]["budget"]["total"] > req.budget_limit


def test_rank_uses_later_days_attractions_too():
    ctx = ToolContext(city="杭州", adcode="330100", start_date="2026-10-20", travel_days=10, nights=9)
    ctx.hotels = {"W": Hotel(name="西边", poi_id="W", location=Location(longitude=120, latitude=30)),
                  "E": Hotel(name="东边", poi_id="E", location=Location(longitude=120.2, latitude=30))}
    ctx.attractions = {str(i): make_attraction(str(i), lng=120 if i < 8 else 120.2,
                                             lat=30, visit_duration=30 if i < 8 else 480) for i in range(9)}
    result = tools.execute("hotels", "rank_hotels", {}, ctx)
    assert result.status == "ok" and ctx.ranked_ids[0] == "E"


def test_weather_calls_tool_without_a_model(monkeypatch):
    def execute(role, name, args, ctx):
        assert role == "weather" and name == "get_weather"
        ctx.weather = [WeatherInfo(date="2026-10-20", day_weather="晴")]
        return tools.ToolResult.success("ok")
    monkeypatch.setattr(weather_agent, "execute", execute)
    result = weather_agent.collect_weather(_state())
    assert result["weather"][0].day_weather == "晴"
    assert result["usage"]["llm_calls"] == 0
