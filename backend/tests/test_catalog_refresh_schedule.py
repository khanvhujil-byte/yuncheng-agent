"""Scheduler boundaries that prevent stale observations and city starvation."""
import time
from datetime import datetime, timedelta, timezone
from threading import Event

from app.services import catalog, catalog_refresh, map_gateway, weather_cache
from tests.factories import make_attraction


def test_configured_city_never_ages_out_and_missing_kinds_are_due(monkeypatch, settings):
    monkeypatch.setattr(settings, "catalog_prewarm_cities", ["杭州"])
    monkeypatch.setattr(settings, "catalog_refresh_cities_per_tick", 1)
    catalog_refresh.touch_city("杭州", "330100")
    catalog_refresh.touch_city("北京", "110100")
    with catalog._connection() as db:
        db.execute("UPDATE catalog_city SET last_seen=? WHERE city='杭州'", (time.time() - 60 * 86400,))
        db.execute("INSERT INTO catalog_refresh_job(city,kind,next_due,status) VALUES(?,?,?,'complete')",
                   ("杭州", "weather", time.time() + 3600))
        for kind in catalog_refresh.KINDS:
            db.execute("INSERT INTO catalog_refresh_job(city,kind,next_due,status) VALUES(?,?,?,'complete')",
                       ("北京", kind, time.time() + 600))
    seen = []
    monkeypatch.setattr(catalog_refresh, "refresh_city", lambda city: seen.append(city) or {"city": city})
    catalog_refresh.tick()
    assert seen == ["杭州"]


def test_weather_job_uses_actual_cache_expiry_and_force_is_forwarded(monkeypatch):
    now = datetime.now(timezone.utc)
    metadata = {"cache_status": "hit", "fetched_at": (now - timedelta(minutes=55)).isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat()}
    flags = []
    def cached(code, **kwargs):
        flags.append(kwargs["force_refresh"])
        return [object()], metadata
    monkeypatch.setattr(weather_cache, "get_forecast", cached)
    catalog_refresh.refresh_city("杭州", adcode="330100", kinds=["weather"])
    row = catalog_refresh.status()[0]
    assert abs(row["next_due"] - datetime.fromisoformat(metadata["expires_at"]).timestamp()) < 1
    assert abs(row["refreshed_at"] - datetime.fromisoformat(metadata["fetched_at"]).timestamp()) < 1
    catalog_refresh.refresh_city("杭州", kinds=["weather"], force=True)
    assert flags == [False, True]


def test_shutdown_does_not_start_more_network_queries(monkeypatch):
    stop = Event()
    seen = []
    def search(query, *args, **kwargs):
        seen.append(query)
        stop.set()
        return [make_attraction("公园")]
    monkeypatch.setattr(map_gateway, "search_poi", search)
    result = catalog_refresh.refresh_city("杭州", stop=stop)
    assert len(seen) == len(result["jobs"]) == 1
    assert result["jobs"][0]["error_code"] == "REFRESH_STOPPED"
