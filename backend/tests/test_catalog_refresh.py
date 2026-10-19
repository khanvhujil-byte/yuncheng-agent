"""Real SQLite job scheduling; only external map/weather calls are replaced."""
import threading
from concurrent.futures import ThreadPoolExecutor

from app.models.schemas import Restaurant
from app.services import catalog, catalog_refresh, map_gateway, weather_cache
from tests.factories import make_attraction


def test_prewarm_reuses_job_results_and_does_not_invent_quotes(monkeypatch):
    calls = []
    def poi(query, city, **kwargs):
        calls.append((query, city))
        return [make_attraction(query, poi_id=query, typecode="100100" if "酒店" in query else "110100")]
    monkeypatch.setattr(map_gateway, "search_poi", poi)
    monkeypatch.setattr(map_gateway, "search_restaurants", lambda *a, **k: [Restaurant(
        name="杭帮菜馆", poi_id="FOOD", cuisine="杭帮菜", location={"longitude": 120, "latitude": 30})])
    monkeypatch.setattr(map_gateway, "geocode", lambda city: (make_attraction("city").location, "330100"))
    monkeypatch.setattr(weather_cache, "get_forecast", lambda code, **kwargs: ([], {"cache_status": "hit"}))
    first = catalog_refresh.refresh_city("杭州市")
    assert all(j["status"] == "complete" for j in first["jobs"])
    assert len(calls) == 4
    hotels = catalog.list_places("杭州", "hotel")
    assert len(hotels) == 2 and all(h["cost_status"] == "unknown" for h in hotels)
    assert all(p["ticket_price_status"] == "unknown" for p in catalog.list_places("杭州", "attraction"))
    assert all(j["status"] == "skipped" for j in catalog_refresh.refresh_city("杭州")["jobs"])
    assert len(calls) == 4


def test_failed_refresh_preserves_old_observation_without_renewing(monkeypatch):
    catalog.upsert_places("杭州", "attraction", [make_attraction("西湖")])
    with catalog._connection() as db:
        old = tuple(db.execute("SELECT observed_at,expires_at FROM place_catalog").fetchone())
    monkeypatch.setattr(map_gateway, "search_poi", lambda *a, **k: [])
    result = catalog_refresh.refresh_city("杭州", kinds=["attraction"])
    assert result["jobs"][0]["error_code"] == "EMPTY_SEARCH_RESULT"
    with catalog._connection() as db:
        assert tuple(db.execute("SELECT observed_at,expires_at FROM place_catalog").fetchone()) == old
    assert catalog_refresh.refresh_city("杭州", kinds=["attraction"])["jobs"][0]["status"] == "skipped"


def test_same_city_refresh_job_has_one_owner(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def search(*a, **k):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return [Restaurant(name="真实餐厅", poi_id="R1", location={"longitude": 120, "latitude": 30})]
    monkeypatch.setattr(map_gateway, "search_restaurants", search)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(catalog_refresh.refresh_city, "杭州", kinds=["restaurant"])
        assert entered.wait(5)
        second = pool.submit(catalog_refresh.refresh_city, "杭州", kinds=["restaurant"], force=True)
        try:
            assert second.result(timeout=5)["jobs"][0]["status"] == "skipped"
        finally:
            release.set()
        assert first.result(timeout=5)["jobs"][0]["status"] == "complete"
    assert len(calls) == 1


def test_refresh_skips_wrong_categories_and_stops_invalid_key(monkeypatch):
    calls = []
    def wrong(query, city, **kwargs):
        return [make_attraction("饭店", typecode="050100"), make_attraction("博物馆", typecode="140100")]
    monkeypatch.setattr(map_gateway, "search_poi", wrong)
    catalog_refresh.refresh_city("杭州", kinds=["attraction"])
    assert [p["name"] for p in catalog.list_places("杭州", "attraction")] == ["博物馆"]
    def invalid(*a, **k):
        calls.append(1)
        raise map_gateway.AmapError("AMAP_KEY_INVALID", "secret request url must not leak")
    monkeypatch.setattr(map_gateway, "search_poi", invalid)
    result = catalog_refresh.refresh_city("北京", kinds=["hotel", "restaurant", "weather"])
    assert len(calls) == len(result["jobs"]) == 1
    assert result["jobs"][0]["error_code"] == "AMAP_KEY_INVALID"
    assert "secret" not in str(catalog_refresh.status())


def test_tick_bounds_work_and_tracks_recent_cold_city(monkeypatch, settings):
    monkeypatch.setattr(settings, "catalog_prewarm_cities", ["杭州", "北京", "上海"])
    monkeypatch.setattr(settings, "catalog_refresh_cities_per_tick", 2)
    catalog_refresh.touch_city("苏州", "320500")
    visited = []
    def refresh(city):
        visited.append(city)
        return {"city": city}
    monkeypatch.setattr(catalog_refresh, "refresh_city", refresh)
    assert len(catalog_refresh.tick()) == 2
    assert "苏州" in visited


def test_worker_disabled_does_not_start_thread(settings):
    worker = catalog_refresh.RefreshWorker()
    worker.start()
    assert worker.thread is None


def test_local_hits_do_not_inflate_upstream_success():
    from app.services import monitoring
    hit = {"status": "ok", "stats": {"upstream_calls": 0, "cache_hit": 1, "local_count": 12,
                                    "retrieval_decision": "local_sufficient", "returned_count": 12}}
    failed = {"status": "error", "error_code": "UPSTREAM_TIMEOUT", "stats": {"upstream_calls": 1}}
    monitoring.record("local-and-failure", {"tool_calls": [
        {"tool": "search_attractions", "outcome": hit, "attempts": [{"outcome": hit}]},
        {"tool": "search_attractions", "outcome": failed, "attempts": [{"outcome": failed}]}]}, False, 3)
    result = monitoring.summary()
    assert result["sample_tool_calls"] == result["tool_attempts"] == 1
    assert result["final_success_rate"] == 0
    assert result["local_retrieval_hits"] == 1 and result["logical_tool_calls"] == 2
