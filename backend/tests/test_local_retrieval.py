"""Offline local-first regressions verify avoided calls and evidence boundaries."""
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.models.requirements import Requirements
from app.models.schemas import Attraction, Hotel, Location, Restaurant
from app.services import catalog, food, map_gateway, price_catalog, tools
from tests.factories import make_request


def spot(identifier, name=None, code="110000"):
    return Attraction(name=name or f"景点{identifier}", poi_id=identifier, typecode=code,
                      location=Location(longitude=120.1, latitude=30.2))


def meal(identifier, *, cost=60, cuisine="杭帮菜"):
    return Restaurant(name=f"餐馆{identifier}", poi_id=identifier, cuisine=cuisine,
                      location=Location(longitude=120.1, latitude=30.2), avg_cost=cost,
                      avg_cost_status="estimated" if cost is not None else "unknown", cost_source="amap")


def ctx(**kw):
    return tools.ToolContext(city="杭州", adcode="330100", start_date="2026-10-20", travel_days=3,
                             nights=2, **kw)


def no_live(*a, **kw):
    raise AssertionError("sufficient fresh local candidates must avoid upstream")


def snapshots(db_path):
    with sqlite3.connect(db_path) as db:
        return db.execute("SELECT poi_id,observed_at,expires_at FROM place_catalog ORDER BY poi_id").fetchall()


def test_warm_attractions_avoid_network_and_preserve_observation(monkeypatch, temp_db):
    catalog.upsert_places("杭州", "attraction", [spot(str(i)) for i in range(6)])
    before = snapshots(temp_db)
    monkeypatch.setattr(map_gateway, "search_poi", no_live)
    context = ctx()
    result = tools.execute("attractions", "search_attractions", {"keyword": "景点"}, context)
    assert result.status == "ok" and len(context.attractions) == 6
    assert result.stats["cache_hit"] == 1 and result.stats["upstream_calls"] == 0
    assert snapshots(temp_db) == before
    assert context.calls[0]["attempts"][0]["outcome"]["stats"]["upstream_calls"] == 0


def test_cold_hotels_fill_once_then_local_reuse_clears_model_price(monkeypatch):
    calls = []
    def live(*a, **kw):
        calls.append(1)
        return [spot(str(i), f"酒店{i}", "100100") for i in range(3)]
    monkeypatch.setattr(map_gateway, "search_poi", live)
    result = tools.execute("hotels", "search_hotels", {}, ctx())
    assert result.stats["upstream_calls"] == 1 and len(calls) == 1
    cached = [Hotel.model_validate(raw).model_copy(update={"estimated_cost": 500})
              for raw in catalog.list_places("杭州", "hotel")]
    catalog.upsert_places("杭州", "hotel", cached)
    monkeypatch.setattr(map_gateway, "search_poi", no_live)
    context = ctx()
    result = tools.execute("hotels", "search_hotels", {}, context)
    assert result.stats["local_count"] == 3 and result.stats["cache_hit"] == 1
    assert all(p.estimated_cost == 0 and p.cost_status == "unknown" for p in context.hotels.values())


def test_unrelated_local_count_does_not_satisfy_keyword(monkeypatch):
    catalog.upsert_places("杭州", "attraction", [spot(str(i), f"博物馆{i}") for i in range(30)])
    called = []
    monkeypatch.setattr(map_gateway, "search_poi", lambda keyword, *a, **kw: called.append(keyword) or [spot("T", "古镇")])
    context = ctx()
    result = tools.execute("attractions", "search_attractions", {"keyword": "古镇"}, context)
    assert called == ["古镇"] and result.stats["local_count"] == 0
    assert set(context.attractions) == {"T"}


def test_expired_local_places_trigger_upstream(monkeypatch):
    observed = datetime.now(timezone.utc) - timedelta(days=3)
    catalog.upsert_places("杭州", "attraction", [spot(str(i)) for i in range(10)],
                          observed_at=observed, expires_at=observed + timedelta(days=1))
    called = []
    monkeypatch.setattr(map_gateway, "search_poi", lambda *a, **kw: called.append(1) or [])
    result = tools.execute("attractions", "search_attractions", {"keyword": "景点"}, ctx())
    assert called == [1] and result.stats["local_count"] == 0
    assert result.stats["local_search"]["stale_count"] == 10


def test_local_exclusions_are_applied_before_sufficiency(monkeypatch):
    catalog.upsert_places("杭州", "attraction", [spot(str(i), f"排除{i}") for i in range(10)])
    called = []
    monkeypatch.setattr(map_gateway, "search_poi", lambda *a, **kw: called.append(1) or [])
    context = ctx(requirements=Requirements(attractions={"excluded_names": ["排除"]}))
    result = tools.execute("attractions", "search_attractions", {"keyword": "景点"}, context)
    assert called == [1] and not context.attractions
    assert "hard_constraints_filtered" in result.stats["retrieval_reasons"]


def test_food_warm_keyword_and_price_cap_skip_live_without_self_renewal(monkeypatch, temp_db):
    catalog.upsert_places("杭州", "restaurant", [meal(str(i)) for i in range(6)])
    before = snapshots(temp_db)
    monkeypatch.setattr(food, "search_restaurants", no_live)
    req = make_request(requirements={"food": {"keywords": ["杭帮菜"], "max_cost_per_meal": 80}})
    result = food.collect_food({"request": req, "travel_days": 3, "experience_enabled": True})
    assert len(result["restaurants"]) == 6 and result["sources"]["food"] == "catalog"
    trace = result["tool_calls"][0]
    assert trace["attempts"] == [] and trace["outcome"]["stats"]["upstream_calls"] == 0
    assert snapshots(temp_db) == before


def test_food_unknown_or_over_cap_prices_cannot_make_warm_cache_sufficient(monkeypatch):
    catalog.upsert_places("杭州", "restaurant", [meal(str(i), cost=200 if i % 2 else None) for i in range(20)])
    calls = []
    monkeypatch.setattr(food, "search_restaurants", lambda *a, **kw: calls.append(1) or [meal("OK", cost=50)])
    req = make_request(requirements={"food": {"max_cost_per_meal": 80}})
    result = food.collect_food({"request": req, "travel_days": 3, "experience_enabled": True})
    assert len(calls) == 1 and [r.poi_id for r in result["restaurants"]] == ["OK"]
    assert result["tool_calls"][0]["outcome"]["stats"]["local_count"] == 0


def test_food_cold_fill_avoids_unnecessary_broad_query(monkeypatch):
    calls = []
    monkeypatch.setattr(food, "search_restaurants", lambda city, keyword, **kw: calls.append(keyword) or [meal(str(i)) for i in range(6)])
    req = make_request(requirements={"food": {"keywords": ["杭帮菜"]}})
    result = food.collect_food({"request": req, "travel_days": 3, "experience_enabled": True})
    assert calls == ["杭帮菜"] and len(result["restaurants"]) == 6
    assert len(catalog.list_places("杭州", "restaurant")) == 6


def test_local_partial_survives_nonretryable_online_outage(monkeypatch):
    catalog.upsert_places("杭州", "restaurant", [meal("LOCAL")])
    def fail(*a, **kw):
        raise map_gateway.AmapError("AMAP_KEY_INVALID", "invalid")
    monkeypatch.setattr(food, "search_restaurants", fail)
    result = food.collect_food({"request": SimpleNamespace(city="杭州"), "travel_days": 3, "experience_enabled": True})
    assert [p.poi_id for p in result["restaurants"]] == ["LOCAL"]
    assert result["sources"]["food"] == "catalog_partial"
    assert "AMAP_KEY_INVALID" in result["errors"][0]
    trace = result["tool_calls"][0]
    assert len(trace["attempts"]) == 1 and trace["outcome"]["stats"]["upstream_calls"] == 1
    assert trace["retrieval"]["retrieval_decision"] == "local_partial_upstream_error"


def test_catalog_failure_falls_back_instead_of_crashing(monkeypatch):
    def broken(*a, **kw):
        raise sqlite3.OperationalError("unavailable")
    monkeypatch.setattr(catalog, "search_places", broken)
    monkeypatch.setattr(map_gateway, "search_poi", lambda *a, **kw: [spot("LIVE")])
    context = ctx()
    result = tools.execute("attractions", "search_attractions", {"keyword": "景点"}, context)
    assert result.status == "ok" and "LIVE" in context.attractions
    assert "catalog_read_failed" in result.stats["retrieval_reasons"]


def test_tool_outage_keeps_local_pool_and_structured_retry_metadata(monkeypatch, settings):
    catalog.upsert_places("杭州", "attraction", [spot("LOCAL")])
    monkeypatch.setattr(settings, "tool_retry_base_delay", 0.0)
    calls = []
    def fail(*a, **kw):
        calls.append(1)
        raise map_gateway.AmapUnreachable("timeout", code="UPSTREAM_TIMEOUT", retryable=True)
    monkeypatch.setattr(map_gateway, "search_poi", fail)
    context = ctx()
    result = tools.execute("attractions", "search_attractions", {"keyword": "景点"}, context)
    assert result.status == "error" and result.error_code == "UPSTREAM_TIMEOUT" and result.retryable
    assert set(context.attractions) == {"LOCAL"} and len(calls) == settings.tool_max_attempts
    assert all(a["outcome"]["stats"]["upstream_calls"] == 1 for a in context.calls[0]["attempts"])
    assert result.stats["retrieval_decision"] == "local_partial_upstream_error"


def test_warm_local_search_still_honors_requested_limit(monkeypatch):
    catalog.upsert_places("杭州", "attraction", [spot(str(i)) for i in range(30)])
    monkeypatch.setattr(map_gateway, "search_poi", no_live)
    context = ctx()
    result = tools.execute("attractions", "search_attractions", {"keyword": "景点", "limit": 2}, context)
    assert result.stats["cache_hit"] == 1 and len(context.attractions) == 2


def test_hotel_caps_require_current_dated_evidence(monkeypatch):
    hotels = [Hotel(name=f"酒店{i}", poi_id=str(i), location=Location(longitude=120, latitude=30),
                    estimated_cost=100, cost_status="estimated", cost_source="llm_estimated") for i in range(3)]
    catalog.upsert_places("杭州", "hotel", hotels)
    calls = []
    monkeypatch.setattr(map_gateway, "search_poi", lambda *a, **kw: calls.append(1) or [])
    context = ctx(requirements=Requirements(hotel={"max_price_per_night": 300}))
    result = tools.execute("hotels", "search_hotels", {}, context)
    assert calls == [1] and result.stats["local_count"] == 0 and not context.hotels
    now = datetime.now(timezone.utc)
    price_catalog.import_prices([{"city": "杭州", "kind": "hotel", "poi_id": str(i), "amount": 250,
        "unit": "night", "status": "estimated", "source_url": "https://example.com/quote",
        "observed_at": (now - timedelta(minutes=1)).isoformat(), "expires_at": (now + timedelta(hours=1)).isoformat(),
        "service_from": "2026-10-20", "service_to": "2026-10-21"} for i in range(3)])
    monkeypatch.setattr(map_gateway, "search_poi", no_live)
    context = ctx(requirements=Requirements(hotel={"max_price_per_night": 300}))
    result = tools.execute("hotels", "search_hotels", {}, context)
    assert result.stats["cache_hit"] == 1
    assert all(p.estimated_cost == 250 and p.cost_source == "imported" for p in context.hotels.values())
