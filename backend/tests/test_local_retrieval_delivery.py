"""Real collector output crosses delivery schemas and online monitoring.

The established valid final-state fixture isolates delivery from remote model
and route generation. Collection, serialization, finalization and storage are
real, with only the remote restaurant lookup replaced.
"""
import pytest

from app.models.schemas import Location, Restaurant, TripPlan, TripPlanResponse
from app.services import catalog, food, map_gateway, monitoring, planning, store
from tests.factories import make_request
from tests.test_api import _final_state


def restaurant(index):
    return Restaurant(name=f"已核实餐厅{index}", poi_id=f"R{index}", cuisine="杭帮菜",
                      location=Location(longitude=120.1, latitude=30.2),
                      avg_cost=60, avg_cost_status="estimated", cost_source="amap")


def final_with_food(collected):
    final = _final_state()
    final["restaurants"] = collected["restaurants"]
    final["sources"].update(collected["sources"])
    final["warnings"].extend(collected["warnings"])
    final["errors"].extend(collected["errors"])
    final["tool_calls"] = collected["tool_calls"]
    # This fixture predates route enrichment; route/time audit has separate
    # coverage. Keep that mode while exercising the actual delivery boundary.
    final["experience_enabled"] = False
    return final


@pytest.mark.parametrize(("cached_count", "source", "upstream_calls", "local_hits"), [
    (6, "catalog", 0, 1),
    (2, "catalog+amap", 1, 0),
])
def test_food_sources_round_trip_delivery_and_report_real_upstream_work(
        monkeypatch, cached_count, source, upstream_calls, local_hits):
    req = make_request()
    catalog.upsert_places(req.city, "restaurant", [restaurant(i) for i in range(cached_count)])
    live_calls = []

    def live(*args, **kwargs):
        live_calls.append(1)
        assert upstream_calls, "warm local retrieval must not call the remote provider"
        return [restaurant(i) for i in range(6)]

    monkeypatch.setattr(food, "search_restaurants", live)
    collected = food.collect_food({"request": req, "travel_days": 3, "experience_enabled": True})
    assert collected["sources"]["food"] == source
    assert len(live_calls) == upstream_calls
    if source == "catalog":
        assert collected["tool_calls"][0]["attempts"] == []

    response = planning._finalize(final_with_food(collected), req, f"food-{source}",
                                  store.fingerprint_of(req), 10)
    restored = TripPlanResponse.model_validate_json(response.model_dump_json())
    assert restored.success, restored.message
    plan = TripPlan.model_validate(restored.data.model_dump())
    assert plan.sources["food"] == source
    assert store.find_itinerary(store.fingerprint_of(req)) is not None
    metrics = monitoring.summary()
    assert metrics["logical_tool_calls"] == 1
    assert metrics["sample_tool_calls"] == metrics["tool_attempts"] == upstream_calls
    assert metrics["local_retrieval_hits"] == local_hits
    assert metrics["local_retrieval_calls"] == 1
    assert metrics["error_codes"] == {}


def test_partial_local_food_after_provider_failure_cannot_be_success_or_cached(monkeypatch):
    req = make_request()
    catalog.upsert_places(req.city, "restaurant", [restaurant(0)])

    def unavailable(*args, **kwargs):
        raise map_gateway.AmapError("AMAP_KEY_INVALID", "invalid key")

    monkeypatch.setattr(food, "search_restaurants", unavailable)
    collected = food.collect_food({"request": req, "travel_days": 3, "experience_enabled": True})
    assert collected["sources"]["food"] == "catalog_partial"
    assert collected["restaurants"]
    assert collected["tool_calls"][0]["outcome"]["stats"]["retrieval_decision"] == "local_partial_upstream_error"
    final = final_with_food(collected)
    assert planning._degraded_sources(planning._to_trip_plan(final, req)) == ["food"]

    response = planning._finalize(final, req, "food-partial", store.fingerprint_of(req), 10)
    restored = TripPlanResponse.model_validate_json(response.model_dump_json())
    assert restored.success is False
    assert restored.data is not None and restored.data.sources["food"] == "catalog_partial"
    assert "food" in restored.message and "未取到完整数据" in restored.message
    assert store.find_itinerary(store.fingerprint_of(req)) is None
    metrics = monitoring.summary()
    assert metrics["logical_tool_calls"] == metrics["sample_tool_calls"] == metrics["tool_attempts"] == 1
    assert metrics["local_retrieval_hits"] == 0
    assert metrics["error_codes"]["AMAP_KEY_INVALID"] == 1
    assert metrics["plan_success_rate"] == 0
