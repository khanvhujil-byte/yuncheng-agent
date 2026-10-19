import copy

import pytest

from app.models.schemas import Attraction, Hotel, Location, Restaurant, TripRequest
from app.services import itinerary_experience as experience
from app.services.schedule_repair import build_sequence, preflight_plan, simulate


def fixture():
    request = TripRequest(city="杭州", start_date="2026-10-20", travel_days=3,
                          transportation="公共交通", accommodation="经济型酒店")
    spots = [Attraction(name=f"景点{i}", poi_id=f"a{i}",
                        location=Location(longitude=120 + i * .002, latitude=30),
                        visit_duration=120, required=i == 3) for i in range(4)]
    restaurants = [Restaurant(name=f"近餐厅{i}", poi_id=f"r{i}",
                              location=Location(longitude=120 + i * .001, latitude=30)) for i in range(3)]
    day = {"date": request.start_date, "day_index": 0, "attractions": [s.model_dump() for s in spots],
           "meals": []}
    plan = {"city": request.city, "start_date": request.start_date, "end_date": "2026-10-22",
            "days": [day]}
    state = {"request": request, "experience_enabled": True, "restaurants": restaurants,
             "must_visit_resolved": [spots[-1]], "weather": [], "route_cache": {}}
    return plan, state


def test_overfull_optional_selection_is_repaired_before_hotel_and_route_work():
    plan, state = fixture()
    original = copy.deepcopy(plan)
    repaired, notes = preflight_plan(plan, state)
    chosen = repaired["days"][0]["attractions"]
    assert len(chosen) == 3 and any(s["poi_id"] == "a3" for s in chosen)
    assert all(s["visit_duration"] == 120 for s in chosen)
    assert notes and "可选景点" in notes[0] and "120分钟" in notes[0]
    assert plan == original
    again, repeated_notes = preflight_plan(repaired, state)
    assert again == repaired and repeated_notes == []


@pytest.mark.parametrize("locked", ["required", "food_first", "partial"])
def test_hard_anchors_and_local_edits_are_not_trimmed(locked):
    plan, state = fixture()
    if locked == "required":
        for spot in plan["days"][0]["attractions"]:
            spot["required"] = True
    elif locked == "food_first":
        state["request"].requirements.food.priority = "food_first"
    else:
        state["preserve_meals"] = True
    repaired, notes = preflight_plan(plan, state)
    assert repaired == plan and notes == []


def test_long_visit_goes_after_lunch_without_shortening_its_duration():
    plan, state = fixture()
    day = plan["days"][0]
    day["attractions"] = day["attractions"][:1]
    day["attractions"][0]["visit_duration"] = 310
    day["meals"] = [experience.meal_from_restaurant(state["restaurants"][0], kind)
                    for kind in ("breakfast", "lunch", "dinner")]
    sequence = build_sequence(day, minutes=lambda *_: 5)
    assert next(index for index, (_, item, _) in enumerate(sequence) if item.get("type") == "lunch") < next(
        index for index, (kind, _, _) in enumerate(sequence) if kind == "attraction")
    assert [duration for kind, _, duration in sequence if kind == "attraction"] == [310]
    assert simulate(sequence, None, lambda *_: 5)["late_meal_minutes"] == 0


def test_nearby_food_does_not_take_long_detour_only_to_avoid_repeating_a_restaurant():
    plan, state = fixture()
    near = state["restaurants"][0]
    far = Restaurant(name="为换店绕远路", poi_id="far", location=Location(longitude=120.03, latitude=30))
    meals = experience._select_meals(plan["days"][0], [near, far], None, {near.poi_id})
    assert all(meal["restaurant_id"] == near.poi_id for meal in meals)


def test_dinner_can_move_half_an_hour_to_leave_time_for_returning_to_hotel():
    plan, state = fixture()
    day = plan["days"][0]
    day["attractions"] = day["attractions"][:1]
    day["meals"] = [experience.meal_from_restaurant(state["restaurants"][0], kind)
                    for kind in ("breakfast", "lunch", "dinner")]
    hotel = Hotel(name="酒店", location=Location(longitude=120, latitude=30)).model_dump()
    sequence = build_sequence(day, hotel, minutes=lambda *_: 35)
    timing = simulate(sequence, hotel, lambda *_: 35)
    assert timing["total_minutes"] <= 660 and timing["early_dinner_minutes"] == 30


def test_model_cannot_mark_every_optional_attraction_as_required_to_block_repair():
    from app.agents.planner_agent import _enrich
    from app.models.schemas import DayPlan
    plan, _ = fixture()
    candidate = Attraction.model_validate(plan["days"][0]["attractions"][0])
    hallucinated = candidate.model_copy(update={"required": True})
    days = [DayPlan(date="2026-10-20", day_index=0, attractions=[hallucinated])]
    _enrich(days, [candidate])
    assert not days[0].attractions[0].required


def test_three_day_failure_case_locally_reduces_load_and_keeps_auditable_routes(monkeypatch):
    plan, state = fixture()
    plan["days"] = [copy.deepcopy(plan["days"][0]) for _ in range(3)]
    for index, day in enumerate(plan["days"]):
        day.update(day_index=index, date=f"2026-10-{20 + index}")
        for spot in day["attractions"]:
            spot["poi_id"] += str(index)
            spot["name"] += str(index)
    state["hotel"] = Hotel(name="酒店", poi_id="hotel", location=Location(longitude=120, latitude=30))
    # Actual traffic is materially slower than the geometric first estimate.
    observed = []
    def route(*args, **kwargs):
        observed.append(1)
        return {"status": "known", "duration_minutes": 25, "distance_meters": 1000}
    monkeypatch.setattr(experience.amap, "travel_route", route)
    original = copy.deepcopy(plan)
    first, notes = preflight_plan(plan, state)
    result, errors, warnings = experience.enrich_experience(first, state)
    assert errors == []
    assert notes and warnings
    assert len(observed) <= 48
    for day in result["days"]:
        assert 1 <= len(day["attractions"]) <= 3
        assert any(spot["required"] for spot in day["attractions"])
        assert day["total_minutes"] <= 660
        assert all(spot["visit_duration"] == 120 for spot in day["attractions"])
        assert all(leg["duration_minutes"] == 25 and leg["status"] == "known" for leg in day["route_legs"])
        lunch = next(s for s in day["schedule"] if s.get("meal_type") == "lunch")
        assert lunch["start_time"] <= "14:00"
    assert plan == original


def test_impossible_required_day_stays_failed_and_does_not_hide_weather(monkeypatch):
    plan, state = fixture()
    for spot in plan["days"][0]["attractions"]:
        spot.update(required=True, category="湖泊")
    state["weather"] = [{"date": state["request"].start_date, "day_weather": "暴雨", "day_temp": 20}]
    monkeypatch.setattr(experience.amap, "travel_route", lambda *_args, **_kwargs: {
        "status": "known", "duration_minutes": 35})
    result, errors, _ = experience.enrich_experience(plan, state)
    assert len(result["days"][0]["attractions"]) == 4
    assert any("舒适上限" in reason for reason in errors)
    assert any("暴雨" in reason for reason in errors)
    node = experience.enrich_node({**state, "plan": plan})
    assert "舒适上限" in node["validate_error"] and "暴雨" in node["validate_error"]
