"""Bounded POI/weather prewarming for the current single-worker deployment.

Background refresh uses the same paced map boundary as user requests. Only actual
provider observations renew POI freshness; no model-generated prices or tags are
introduced. Durable job leases keep CLI and background work from duplicating a
refresh, and failed jobs retain existing observations until their own expiry.
"""
import json
import logging
import re
import threading
import time
from datetime import datetime
from uuid import uuid4

from ..config import get_settings
from ..models.schemas import Hotel
from . import catalog, map_gateway

logger = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_city (
 city TEXT PRIMARY KEY, adcode TEXT, accesses INTEGER NOT NULL DEFAULT 0,
 last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_refresh_job (
 city TEXT NOT NULL, kind TEXT NOT NULL, next_due REAL NOT NULL DEFAULT 0,
 lease_until REAL NOT NULL DEFAULT 0, lease_id TEXT, status TEXT NOT NULL,
 refreshed_at REAL, error_code TEXT, summary_json TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(city,kind)
);
"""
KINDS = ("attraction", "hotel", "restaurant", "weather")


def _connect():
    # Use catalog's short-lived connections: lazy tables also work for an empty DB.
    return catalog._connection()


def touch_city(city, adcode=None, *, access=True):
    name = city.strip().removesuffix("市")
    if not name or len(name) > 80:
        raise ValueError("城市名不合法")
    if adcode is not None and not re.fullmatch(r"\d{6}", str(adcode)):
        raise ValueError("城市编码必须为6位数字")
    with _connect() as db:
        db.executescript(SCHEMA)
        db.execute("""INSERT INTO catalog_city VALUES(?,?,?,?)
            ON CONFLICT(city) DO UPDATE SET adcode=COALESCE(excluded.adcode,catalog_city.adcode),
            accesses=catalog_city.accesses+excluded.accesses,
            last_seen=CASE WHEN excluded.accesses>0 THEN excluded.last_seen ELSE catalog_city.last_seen END""",
                   (name, adcode, int(access), time.time()))
    return name


def _claim(city, kind, *, force=False):
    now, lease = time.time(), uuid4().hex
    with _connect() as db:
        db.executescript(SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT OR IGNORE INTO catalog_refresh_job(city,kind,status) VALUES(?,?,'new')", (city, kind))
        row = db.execute("SELECT * FROM catalog_refresh_job WHERE city=? AND kind=?", (city, kind)).fetchone()
        if row["lease_until"] > now or (not force and row["next_due"] > now):
            return None
        db.execute("UPDATE catalog_refresh_job SET lease_until=?,lease_id=?,status='running' WHERE city=? AND kind=?",
                   (now + 600, lease, city, kind))
    return lease


def _finish(city, kind, lease, result, success):
    settings, now = get_settings(), time.time()
    interval = settings.weather_cache_ttl_seconds if kind == "weather" else settings.catalog_poi_refresh_seconds
    wait = max(interval, 60) if success else settings.catalog_refresh_failure_seconds
    observed = now
    due = now + wait
    if success and kind == "weather":
        metadata = result.get("cache", {})
        if metadata.get("expires_at"):
            due = max(now + 60, datetime.fromisoformat(metadata["expires_at"]).timestamp())
        if metadata.get("fetched_at"):
            observed = datetime.fromisoformat(metadata["fetched_at"]).timestamp()
    with _connect() as db:
        db.execute("""UPDATE catalog_refresh_job SET lease_until=0,lease_id=NULL,status=?,next_due=?,
            refreshed_at=CASE WHEN ? THEN ? ELSE refreshed_at END,error_code=?,summary_json=?
            WHERE city=? AND kind=? AND lease_id=?""",
                   ("complete" if success else "error", due, success, observed, result.get("error_code"),
                    json.dumps(result, ensure_ascii=False), city, kind, lease))


def _fetch_places(city, kind, *, stop=None):
    if kind == "restaurant":
        return map_gateway.search_restaurants("当地特色美食", city, limit=20)
    queries = ("风景名胜", "博物馆") if kind == "attraction" else ("酒店", "快捷酒店")
    by_id = {}
    for query in queries:
        if stop and stop.is_set():
            raise map_gateway.AmapUnreachable("资料更新已停止", retryable=False, code="REFRESH_STOPPED")
        for place in map_gateway.search_poi(query, city, offset=20, page=1):
            codes = (place.typecode or "").split("|")
            hotel = any(code.startswith("10") for code in codes)
            if kind == "hotel":
                if not hotel:
                    continue
                # POI identity does not establish a dated room quote.
                value = Hotel(name=place.name, poi_id=place.poi_id, address=place.address, location=place.location,
                              rating=str(place.rating) if place.rating is not None else "", type=place.category or "",
                              cost_status="unknown", cost_source="unknown", estimated_cost=0)
            else:
                if any(code.startswith(("05", "10")) for code in codes):
                    continue
                value = place
            if place.poi_id:
                by_id[place.poi_id] = value
    return list(by_id.values())


def refresh_city(city, *, kinds=KINDS, adcode=None, force=False, stop=None):
    """Refresh at most five POI calls + one forecast; no planning/LLM calls."""
    if not kinds or any(kind not in KINDS for kind in kinds):
        raise ValueError("未知采集类型")
    city = touch_city(city, adcode, access=False)
    outcomes = []
    for kind in dict.fromkeys(kinds):
        if stop and stop.is_set():
            break
        lease = _claim(city, kind, force=force)
        if lease is None:
            outcomes.append({"kind": kind, "status": "skipped"})
            continue
        try:
            if kind == "weather":
                from .weather_cache import get_forecast
                with _connect() as db:
                    row = db.execute("SELECT adcode FROM catalog_city WHERE city=?", (city,)).fetchone()
                code = row[0] if row else None
                if not code:
                    location, code = map_gateway.geocode(city)
                    if not location or not code:
                        raise map_gateway.AmapUnreachable("城市编码未解析", retryable=False, code="CITY_NOT_FOUND")
                    touch_city(city, code, access=False)
                places, metadata = get_forecast(code, force_refresh=force)
                result = {"kind": kind, "status": "complete", "count": len(places), "cache": metadata}
            else:
                places = _fetch_places(city, kind, stop=stop)
                if not places:
                    raise map_gateway.AmapUnreachable("正常返回但没有可用候选", retryable=False, code="EMPTY_SEARCH_RESULT")
                stored = catalog.upsert_places(city, kind, places, source="amap" if get_settings().map_backend == "local" else "mcp")
                if stored == 0:
                    raise map_gateway.AmapUnreachable("没有可入库的有效候选", retryable=False, code="EMPTY_SEARCH_RESULT")
                result = {"kind": kind, "status": "complete", "count": stored}
            _finish(city, kind, lease, result, True)
        except Exception as exc:
            # Keep raw provider messages/URLs out of this operational report.
            result = {"kind": kind, "status": "error", "error_code": getattr(exc, "code", "REFRESH_FAILED")}
            logger.warning("资料更新失败 city=%s kind=%s code=%s", city, kind, result["error_code"])
            _finish(city, kind, lease, result, False)
        outcomes.append(result)
        if result.get("error_code") in {"AMAP_KEY_INVALID", "AMAP_QUOTA_EXCEEDED"}:
            break
    return {"city": city, "jobs": outcomes}


def tick(*, stop=None):
    """Bound the work per tick; include only configured or recently accessed cities."""
    settings = get_settings()
    for city in settings.catalog_prewarm_cities:
        touch_city(city, access=False)
    cutoff = time.time() - 30 * 86400
    with _connect() as db:
        db.executescript(SCHEMA)
        configured = list(dict.fromkeys(c.strip().removesuffix("市") for c in settings.catalog_prewarm_cities))
        placeholders = ",".join("?" for _ in configured) or "NULL"
        # Materialize all four kinds, so missing jobs are due even after a
        # weather-only/manual import. Configured cities never age out.
        rows = db.execute(f"""SELECT c.city FROM catalog_city c
            CROSS JOIN (SELECT 'attraction' kind UNION ALL SELECT 'hotel'
                UNION ALL SELECT 'restaurant' UNION ALL SELECT 'weather') k
            LEFT JOIN catalog_refresh_job j ON j.city=c.city AND j.kind=k.kind
            WHERE c.last_seen>=? OR c.city IN ({placeholders}) GROUP BY c.city
            HAVING SUM(CASE WHEN COALESCE(j.next_due,0)<=? AND COALESCE(j.lease_until,0)<=? THEN 1 ELSE 0 END)>0
            ORDER BY MIN(COALESCE(j.next_due,0)),c.accesses DESC LIMIT ?""",
                          (cutoff, *configured, time.time(), time.time(), settings.catalog_refresh_cities_per_tick)).fetchall()
    results = []
    for row in rows:
        if stop and stop.is_set():
            break
        results.append(refresh_city(row["city"], **({"stop": stop} if stop else {})))
    return results


def status():
    with _connect() as db:
        db.executescript(SCHEMA)
        rows = db.execute("SELECT city,kind,status,next_due,refreshed_at,error_code FROM catalog_refresh_job ORDER BY city,kind").fetchall()
    return [dict(row) for row in rows]


class RefreshWorker:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        settings = get_settings()
        # No hidden network work in unconfigured/test deployments.
        if not settings.catalog_refresh_enabled or (settings.map_backend == "local" and not settings.amap_api_key):
            return
        self.thread = threading.Thread(target=self._run, name="catalog-refresh", daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                tick(stop=self.stop_event)
                if not self.stop_event.is_set():
                    from .ticket_sources import refresh
                    refresh(limit=3)
            except Exception:
                logger.exception("资料更新任务未完成")
            self.stop_event.wait(get_settings().catalog_refresh_tick_seconds)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
