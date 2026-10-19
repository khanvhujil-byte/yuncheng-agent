"""Regression tests for deterministic completion repair (dedupe / trim / meals).

These cover the failure mode observed in the 50-scenario evaluation: the unified
planner kept producing days above the 660-minute comfort limit and repeating one
attraction on two days, while unified planning had disabled every local repair.
The result was a partial result whose own data contradicted the audit.
"""
import copy
from collections import Counter

import pytest

from app.models.requirements import FoodNeeds
from app.models.schemas import Attraction, DayPlan, Location, Restaurant, TripRequest
from app.services import itinerary_experience as experience
from app.services.schedule_repair import (
    can_trim,
    dedupe_attractions,
    preflight_plan,
)


def _request() -> TripRequest:
    return TripRequest(city="杭州", start_date="2026-10-20", travel_days=3,
                       transportation="公共交通", accommodation="经济型酒店")


def _spot(name: str, poi_id: str, longitude: float, duration: int = 120, required: bool = False) -> Attraction:
    return Attraction(name=name, poi_id=poi_id, location=Location(longitude=longitude, latitude=30),
                      visit_duration=duration, required=required)


def _restaurant(name: str, poi_id: str, longitude: float) -> Restaurant:
    return Restaurant(name=name, poi_id=poi_id, location=Location(longitude=longitude, latitude=30))


# ---------------------------------------------------------------- dedupe
def test_dedupe_keeps_first_occurrence_and_writes_a_note():
    attraction = _spot("西湖", "a1", 120.15)
    plan_days = [
        DayPlan(date="2026-10-20", day_index=0, attractions=[attraction, _spot("灵隐寺", "a2", 120.10)]),
        DayPlan(date="2026-10-21", day_index=1, attractions=[attraction.model_copy(deep=True),
                                                             _spot("雷峰塔", "a3", 120.14)]),
    ]
    notes = dedupe_attractions(plan_days)
    assert [spot.poi_id for spot in plan_days[0].attractions] == ["a1", "a2"]
    assert [spot.poi_id for spot in plan_days[1].attractions] == ["a3"]
    assert notes and "西湖" in notes[0] and "去重" in notes[0]


def test_dedupe_never_empties_a_day_that_only_repeats():
    attraction = _spot("西湖", "a1", 120.15)
    days = [
        {"date": "2026-10-20", "day_index": 0, "attractions": [attraction.model_dump()]},
        {"date": "2026-10-21", "day_index": 1, "attractions": [attraction.model_dump()]},
    ]
    dedupe_attractions(days)
    assert len(days[1]["attractions"]) == 1


def test_dedupe_replaces_a_repeat_only_day_with_an_unused_candidate():
    attraction = _spot("西湖", "a1", 120.15)
    replacement = _spot("灵隐寺", "a2", 120.10)
    days = [
        DayPlan(date="2026-10-20", day_index=0, attractions=[attraction]),
        DayPlan(date="2026-10-21", day_index=1, attractions=[attraction.model_copy(deep=True)]),
    ]
    notes = dedupe_attractions(days, [attraction, replacement])
    assert [spot.poi_id for spot in days[1].attractions] == ["a2"]
    assert notes and "替换为候选" in notes[-1]


# ---------------------------------------------------------------- trimming
def test_unified_planning_does_not_disable_overfull_heuristic_trim():
    request = _request()
    spots = [_spot(f"景点{i}", f"a{i}", 120 + i * 0.002, required=i == 3) for i in range(4)]
    plan = {"city": request.city, "start_date": request.start_date, "end_date": "2026-10-22",
            "days": [{"date": request.start_date, "day_index": 0,
                      "attractions": [spot.model_dump() for spot in spots], "meals": []}]}
    state = {"request": request, "experience_enabled": True, "unified_planning": True,
             "restaurants": [], "must_visit_resolved": [spots[-1]], "weather": [], "route_cache": {}}
    assert can_trim(state) is True
    repaired, notes = preflight_plan(plan, state)
    chosen = repaired["days"][0]["attractions"]
    assert len(chosen) == 3
    assert any(spot["required"] for spot in chosen)
    assert notes


def test_partial_edit_and_food_first_keep_their_attractions():
    request = _request()
    spots = [_spot(f"景点{i}", f"a{i}", 120 + i * 0.002) for i in range(4)]
    plan = {"days": [{"date": request.start_date, "day_index": 0,
                      "attractions": [spot.model_dump() for spot in spots], "meals": []}]}
    for override in ({"preserve_meals": True}, None):
        state = {"request": request, "experience_enabled": True, "unified_planning": True,
                 "restaurants": [], "must_visit_resolved": [], "weather": [], "route_cache": {}}
        if override:
            state.update(override)
        else:
            request.requirements.food.priority = "food_first"
        assert can_trim(state) is False
        repaired, notes = preflight_plan(plan, state)
        assert repaired == plan and notes == []


# ---------------------------------------------------------------- comfort repair
def test_unified_overfull_day_is_trimmed_by_enrich(monkeypatch):
    request = _request()
    spots = [_spot("近景点A", "a1", 120.001, duration=180),
             _spot("近景点B", "a2", 120.003, duration=180),
             _spot("近景点C", "a3", 120.005, duration=180)]
    restaurants = [_restaurant("近餐厅1", "r1", 120.001), _restaurant("近餐厅2", "r2", 120.003)]
    plan = {"city": request.city, "start_date": request.start_date, "end_date": "2026-10-22",
            "days": [{"date": request.start_date, "day_index": 0,
                      "attractions": [spot.model_dump() for spot in spots], "meals": []}]}
    state = {"request": request, "experience_enabled": True, "unified_planning": True, "nights": 0,
             "attractions": spots, "restaurants": restaurants, "must_visit_resolved": [],
             "weather": [], "route_cache": {}, "plan": copy.deepcopy(plan)}
    monkeypatch.setattr(experience.amap, "travel_route",
                        lambda *_a, **_k: {"status": "known", "duration_minutes": 10,
                                           "distance_meters": 800, "source": "amap"})
    result, errors, warnings = experience.enrich_experience(copy.deepcopy(plan), state)
    day = result["days"][0]
    assert len(day["attractions"]) < 3
    assert day["total_minutes"] <= 660
    assert any("未安排可选景点" in note for note in warnings)
    assert not any("舒适上限" in reason for reason in errors)


def test_required_only_overfull_day_stays_failed(monkeypatch):
    request = _request()
    spots = [_spot(f"必去{i}", f"a{i}", 120 + i * 0.002, duration=240, required=True)
             for i in range(3)]
    plan = {"days": [{"date": request.start_date, "day_index": 0,
                      "attractions": [spot.model_dump() for spot in spots], "meals": []}]}
    state = {"request": request, "experience_enabled": True, "unified_planning": True, "nights": 0,
             "attractions": spots, "restaurants": [], "must_visit_resolved": spots,
             "weather": [], "route_cache": {}}
    monkeypatch.setattr(experience.amap, "travel_route",
                        lambda *_a, **_k: {"status": "known", "duration_minutes": 20,
                                           "distance_meters": 1200, "source": "amap"})
    result, errors, _ = experience.enrich_experience(copy.deepcopy(plan), state)
    assert len(result["days"][0]["attractions"]) == 3
    assert any("舒适上限" in reason for reason in errors)


# ---------------------------------------------------------------- meals
def test_invalid_preserved_meal_is_replaced_only_when_replacement_is_requested():
    near = _restaurant("近餐厅", "near", 120.001)
    far = _restaurant("远餐厅", "far", 120.2)
    spot = _spot("景点", "a1", 120.0)
    day = {"attractions": [spot.model_dump()], "meals": [
        experience.meal_from_restaurant(far, kind) for kind in ("breakfast", "lunch", "dinner")
    ]}
    replaced = experience._select_meals(day, [near, far], None, Counter(), preserve=True,
                                        needs=FoodNeeds(), replace_invalid=True)
    assert all(meal["restaurant_id"] == "near" for meal in replaced)
    kept = experience._select_meals(day, [near, far], None, Counter(), preserve=True,
                                    needs=FoodNeeds(), replace_invalid=False)
    assert all(meal["restaurant_id"] == "far" for meal in kept)


# ---------------------------------------------------------------- stale errors
def test_stale_format_error_is_cleared_once_the_repaired_plan_audits_clean(monkeypatch):
    request = _request()
    spots = [_spot("景点A", "a1", 120.001), _spot("景点B", "a2", 120.004)]
    restaurants = [_restaurant("近餐厅1", "r1", 120.001), _restaurant("近餐厅2", "r2", 120.004)]
    plan = {"city": request.city, "start_date": request.start_date, "end_date": "2026-10-22",
            "days": [{"date": request.start_date, "day_index": 0,
                      "attractions": [spot.model_dump() for spot in spots], "meals": []}]}
    state = {"request": request, "experience_enabled": True, "unified_planning": True, "nights": 0,
             "attractions": spots, "restaurants": restaurants, "must_visit_resolved": [],
             "weather": [], "route_cache": {}, "plan": copy.deepcopy(plan),
             "validate_error": "输出不是合法 JSON：Expecting ',' delimiter",
             "retries": 2, "hotel": None}
    monkeypatch.setattr(experience.amap, "travel_route",
                        lambda *_a, **_k: {"status": "known", "duration_minutes": 10,
                                           "distance_meters": 800, "source": "amap"})
    update = experience.enrich_node(state)
    assert update.get("validate_error") is None
