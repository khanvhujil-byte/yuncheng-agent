"""Budget/cache boundaries across scheduling and route batches."""
from time import time

from app.services import itinerary_experience as experience, route_batch
from tests.test_schedule_repair import fixture


def test_repair_bridge_queries_share_the_first_route_deadline(monkeypatch):
    plan, state = fixture()
    clock = [100.0]
    budgets = []
    monkeypatch.setattr(experience, "monotonic", lambda: clock[0])
    monkeypatch.setattr(experience.amap, "travel_route", lambda *a, **k: {
        "duration_minutes": 60, "distance_meters": 10000, "source": "amap", "status": "known"})
    original = route_batch.prefetch_routes

    def capture(*args, **kwargs):
        budgets.append(kwargs["budget_seconds"])
        result = original(*args, **kwargs)
        clock[0] += 9
        return result

    monkeypatch.setattr(route_batch, "prefetch_routes", capture)
    experience.enrich_experience(plan, state)
    assert len(budgets) == 2
    assert budgets == [12, 3]


def test_expired_route_cannot_remove_optional_stop_before_refresh(monkeypatch, settings):
    plan, state = fixture()
    day = plan["days"][0]
    day["attractions"] = day["attractions"][:2]
    # No hotel: the first actual leg is breakfast -> attraction. Seed every
    # directional pair with an expired absurd route which must not prune.
    places = [*day["attractions"], *(r.model_dump() for r in state["restaurants"])]
    for a in places:
        for b in places:
            key = route_batch.route_key(a, b, "公共交通", "杭州")
            state["route_cache"][key] = {"duration_minutes": 9999, "distance_meters": 1,
                                          "status": "known", "source": "amap",
                                          "_cached_at": time() - settings.route_cache_ttl_seconds - 1}
    monkeypatch.setattr(experience.amap, "travel_route", lambda *a, **k: {
        "duration_minutes": 5, "distance_meters": 500, "source": "amap", "status": "known"})
    result, errors, notes = experience.enrich_experience(plan, state)
    assert len(result["days"][0]["attractions"]) == 2
    assert not any("未安排可选景点" in note for note in notes)
    assert not errors
