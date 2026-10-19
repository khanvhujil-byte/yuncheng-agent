"""Reused itineraries recheck exact current food evidence without renewing it."""
import copy
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.budget import patch_budget
from app.models.schemas import Location, Restaurant, WeatherInfo
from app.services import catalog, planning, price_catalog
from app.services.itinerary_experience import meal_from_restaurant
from tests.factories import make_attraction, make_day, make_request


def restaurant(cost=30):
    return Restaurant(name="原餐厅", poi_id="R0", address="原地址", avg_cost=cost,
                      avg_cost_status="estimated", cost_source="amap",
                      location=Location(longitude=120.1, latitude=30.2))


@pytest.fixture
def trip(monkeypatch):
    req = make_request(start_date="2026-10-20", travel_days=1, nights=0, must_visit=[])
    day = make_day(0, req.start_date, [make_attraction("景点", ticket_price=0,
                   ticket_price_status="known", ticket_price_source="amap")], meals=0)
    day["meals"] = [meal_from_restaurant(restaurant(), "lunch")]
    day["route_legs"] = [{"from_name": "景点", "to_name": "原餐厅", "mode": "walking",
                           "duration_minutes": 15, "status": "known", "source": "amap"}]
    plan = {"city": req.city, "start_date": req.start_date, "end_date": req.start_date,
            "hotel_nights": 0, "days": [day], "sources": {"food": "amap", "weather": "amap"}}
    plan = patch_budget({"plan": plan, "request": req, "travel_days": 1, "nights": 0})["plan"]
    monkeypatch.setattr(planning.amap, "geocode", lambda *a, **kw: (Location(longitude=120, latitude=30), "330100"))

    def weather(role, name, args, context):
        context.weather = [WeatherInfo(date=req.start_date, day_weather="晴")]

    monkeypatch.setattr(planning, "execute", weather)
    return req, plan


def timestamps(db_path):
    with sqlite3.connect(db_path) as db:
        return db.execute("SELECT poi_id,observed_at,expires_at FROM place_catalog ORDER BY poi_id").fetchall()


def test_just_expired_food_evidence_becomes_unknown_without_mutating_cache(trip, temp_db):
    req, original = trip
    before_plan = copy.deepcopy(original)
    now = datetime.now(timezone.utc)
    catalog.upsert_places(req.city, "restaurant", [restaurant()], observed_at=now - timedelta(hours=1),
                          expires_at=now - timedelta(microseconds=1))
    before_db = timestamps(temp_db)
    updated = planning._reuse_with_new_dates(original, req)
    meal = updated["days"][0]["meals"][0]
    assert meal["estimated_cost"] is None and meal["cost_status"] == "unknown"
    assert meal["cost_source"] == "unknown" and meal["evidence_url"] is None
    assert updated["budget"]["total_meals"] == 0
    assert meal["name"] in updated["budget"]["unknown_items"]
    assert "未知" in "".join(updated["warnings"])
    assert original == before_plan and timestamps(temp_db) == before_db


def test_current_imported_food_price_survives_missing_poi_price(trip):
    req, original = trip
    now = datetime.now(timezone.utc)
    price_catalog.import_prices([{"city": req.city, "kind": "restaurant", "poi_id": "R0",
        "amount": 75, "unit": "person_meal", "status": "estimated", "source_url": "https://example.com/menu",
        "observed_at": (now - timedelta(minutes=1)).isoformat(), "expires_at": (now + timedelta(hours=1)).isoformat()}])
    updated = planning._reuse_with_new_dates(original, req)
    meal = updated["days"][0]["meals"][0]
    assert meal["estimated_cost"] == 75 and meal["cost_source"] == "imported"
    assert meal["evidence_url"] == "https://example.com/menu"
    assert updated["budget"]["total_meals"] == 75
    assert original["days"][0]["meals"][0]["estimated_cost"] == 30


def test_fresh_food_price_updates_budget_but_preserves_identity_location_and_routes(trip, temp_db):
    req, original = trip
    before_plan = copy.deepcopy(original)
    fresh = restaurant(85).model_copy(update={"name": "新名字", "address": "新地址",
        "location": Location(longitude=121, latitude=31)})
    catalog.upsert_places(req.city, "restaurant", [fresh])
    before_db = timestamps(temp_db)
    updated = planning._reuse_with_new_dates(original, req)
    old_meal, new_meal = original["days"][0]["meals"][0], updated["days"][0]["meals"][0]
    assert new_meal["estimated_cost"] == 85 and updated["budget"]["total_meals"] == 85
    for key in ("name", "address", "location", "description", "restaurant_id"):
        assert new_meal[key] == old_meal[key]
    assert updated["days"][0]["route_legs"] == original["days"][0]["route_legs"]
    assert original == before_plan and timestamps(temp_db) == before_db


def test_expired_ranking_removed_even_when_embedded_in_fresh_poi(trip):
    req, original = trip
    stale = {"ranking_name": "历史榜单", "ranking_rank": 1, "ranking_url": "https://example.com/old"}
    original["days"][0]["meals"][0].update(stale)
    catalog.upsert_places(req.city, "restaurant", [restaurant().model_copy(update=stale)])
    catalog.save_rankings(req.city, [{"poi_id": "R0", "name": "原餐厅", "rank": 1}],
                          source_url=stale["ranking_url"], ranking_name=stale["ranking_name"],
                          observed_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(), ttl_days=1)
    updated = planning._reuse_with_new_dates(original, req)
    meal = updated["days"][0]["meals"][0]
    assert all(meal[key] is None for key in stale)
    assert "榜单" in "".join(updated["warnings"])
    assert original["days"][0]["meals"][0]["ranking_rank"] == 1


def test_only_current_exact_id_ranking_can_replace_old_label(trip):
    req, original = trip
    original["days"][0]["meals"][0].update(ranking_name="旧榜", ranking_rank=1, ranking_url="https://example.com/old")
    catalog.upsert_places(req.city, "restaurant", [restaurant()])
    catalog.save_rankings(req.city, [{"poi_id": "R0", "name": "原餐厅", "rank": 3},
                                   {"poi_id": "OTHER", "name": "原餐厅", "rank": 1}],
                          source_url="https://example.com/current", ranking_name="当前榜")
    meal = planning._reuse_with_new_dates(original, req)["days"][0]["meals"][0]
    assert (meal["ranking_name"], meal["ranking_rank"], meal["ranking_url"]) == (
        "当前榜", 3, "https://example.com/current")


def test_database_read_failure_clears_old_cost_and_degrades_without_crashing(trip, monkeypatch):
    req, original = trip
    def unavailable(*a, **kw):
        raise sqlite3.OperationalError("database unavailable")
    monkeypatch.setattr(catalog, "get_places_by_ids", unavailable)
    monkeypatch.setattr(catalog, "rankings", unavailable)
    updated = planning._reuse_with_new_dates(original, req)
    assert updated["days"][0]["meals"][0]["cost_status"] == "unknown"
    assert updated["sources"]["food"] == "catalog_partial"
    assert "暂不可用" in "".join(updated["warnings"])
    assert planning._degraded_sources(updated) == ["food"]


def test_new_food_price_cannot_bypass_cached_budget_limit(trip):
    req, original = trip
    req = req.model_copy(update={"budget_limit": 100})
    catalog.upsert_places(req.city, "restaurant", [restaurant(150)])
    response = planning._cached_response({"plan": original, "hit_count": 1, "created_at": "test"},
                                         req, "food-over-budget", "food-fingerprint", 1)
    assert response.success is False
    assert response.data.budget.total_meals == 150
    assert response.data.budget.limit_status == "estimated_exceeded"
    assert "超预算" in response.message


def test_exact_id_lookup_does_not_truncate_at_city_search_limit():
    places = [restaurant().model_copy(update={"poi_id": f"R{i}"}) for i in range(450)]
    catalog.upsert_places("杭州", "restaurant", places)
    requested = [p.poi_id for p in places] + ["R0", "missing"]
    found = catalog.get_places_by_ids("杭州市", "restaurant", requested)
    assert set(found) == {p.poi_id for p in places}
    assert catalog.get_places_by_ids("北京", "restaurant", requested) == {}
    assert catalog.get_places_by_ids("杭州", "hotel", requested) == {}
