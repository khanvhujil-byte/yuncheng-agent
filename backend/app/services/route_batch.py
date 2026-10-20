"""Deduplicated route prefetch with bounded concurrency, attempts and cache age.

Only observed AMap routes enter the shared SQLite cache. Geometric fallbacks
remain labelled estimates in the current plan, together with the actual reason
and structured retry report. Workers still use map_gateway and its request gate.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import sqlite3
from threading import Lock
from time import monotonic, time

from ..config import get_settings
from ..models.schemas import Location
from . import map_gateway as amap, store
from .tool_result import ToolResult
from .tool_retry import ExecutionReport, RetryPolicy, run
from .tools import haversine_km

logger = logging.getLogger(__name__)

_MODE_ALIASES = {"自驾": "driving", "驾车": "driving", "步行": "walking", "骑行": "bicycling",
                 "公共交通": "transit", "公交": "transit", "混合": "transit"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS route_observation (
    route_key TEXT PRIMARY KEY,
    route_json TEXT NOT NULL,
    observed_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_route_observation_expiry ON route_observation(expires_at);
"""


@dataclass
class RouteBatchResult:
    cache: dict
    calls: list[dict]
    query_count: int
    cache_hits: int = 0
    elapsed_ms: float = 0.0
    blocked_code: str | None = None


def _location(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, Location):
        return value
    if isinstance(value, dict) and "location" in value:
        value = value["location"]
    return Location.model_validate(value)


def _namespace():
    settings = get_settings()
    if settings.map_backend == "local":
        return "local"
    # MCP endpoints and tenant configuration can describe different providers.
    # Hash the configuration so credentials never appear in a key or trace.
    identity = {key: getattr(settings, key) for key in
                ("mcp_transport", "mcp_url", "mcp_command", "mcp_args", "mcp_headers", "mcp_env")}
    return "mcp:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]


def route_key(origin, destination, mode, city):
    """Exact directional coordinates + transport + city + provider identity."""
    a, b = _location(origin), _location(destination)
    selected = _MODE_ALIASES.get(mode, mode)
    return (f"{_namespace()}|{a.longitude},{a.latitude}>{b.longitude},{b.latitude}"
            f"|{selected}|{str(city or '').strip()}")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _known(raw):
    # Missing source is retained for legacy test/provider adapters, but cannot
    # enter the persistent cache (which requires explicit observed provenance).
    return (isinstance(raw, dict) and raw.get("status") == "known"
            and raw.get("source", "amap") == "amap" and _number(raw.get("duration_minutes"))
            and (raw.get("distance_meters") is None or _number(raw["distance_meters"])))


def _anomalous(raw, a, b, mode):
    """Conservative integrity gate, never a claim about expected road times."""
    if not _known(raw):
        return False
    straight = haversine_km(a, b)
    minutes = raw['duration_minutes']
    meters = raw.get('distance_meters')
    selected = _MODE_ALIASES.get(mode, mode)
    if straight > 1 and (minutes <= 0 or (meters is not None and meters < straight*800)):
        return True
    if meters is not None and meters > max(100000, straight*30000):
        return True
    if selected in ('driving', 'transit') and minutes > max(300, straight*20):
        return True
    return False


def _memory_hit(value, now, ttl):
    if not isinstance(value, dict) or not _number(value.get("_cached_at")):
        return False
    age = now - value["_cached_at"]
    # A transient failure can save duplicate work inside a repair round, never
    # become a long-lived negative cache across subsequent user requests.
    limit = ttl if _known(value) else min(ttl, 15.0)
    estimated = (value.get("status") == "estimated" and value.get("source") == "static_estimate"
                 and _number(value.get("duration_minutes")))
    return 0 <= age < limit and (estimated or _known(value))


def cached_route(value, now=None, ttl=None):
    """Return a fresh copy of usable memory evidence, retaining its observation time.

    The same check applies to heuristic decisions and final route reuse. A cache
    hit updates only the display age, never the expiry or original observation.
    """
    now = time() if now is None else now
    ttl = getattr(get_settings(), "route_cache_ttl_seconds", 900.0) if ttl is None else ttl
    if not _number(now) or not _number(ttl) or not _memory_hit(value, now, ttl):
        return None
    result = copy.deepcopy(value)
    if _known(result):
        result["note"] = (f"高德路线接口缓存（{math.ceil((now-result['_cached_at'])/60)} 分钟内）；"
                          "出发时路况仍可能变化。")
    return result


def _read_observations(keys, ttl):
    if not keys or ttl <= 0:
        return {}
    try:
        store.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(store.DB_PATH, timeout=0.05) as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            placeholders = ",".join("?" for _ in keys)
            rows = conn.execute(f"SELECT * FROM route_observation WHERE route_key IN ({placeholders})", list(keys)).fetchall()
        now = time()
        result = {}
        for row in rows:
            if not 0 <= now - row["observed_at"] < ttl or row["expires_at"] <= now:
                continue
            try:
                route = json.loads(row["route_json"])
            except (TypeError, ValueError):
                continue
            if not _known(route) or route.get("source") != "amap" or not _number(route.get("distance_meters")):
                continue
            route["_cached_at"] = row["observed_at"]
            route["note"] = (f"高德路线接口缓存（{math.ceil((now-row['observed_at'])/60)} 分钟内）；"
                             "出发时路况仍可能变化。")
            result[row["route_key"]] = route
        return result
    except (OSError, sqlite3.Error):
        logger.warning("Route observation cache unavailable; querying provider without persistent cache")
        return {}


def _write_observations(routes, ttl):
    rows = []
    for key, value in routes.items():
        if _known(value) and value.get("source") == "amap" and _number(value.get("distance_meters")):
            observed = value["_cached_at"]
            rows.append((key, json.dumps(value, ensure_ascii=False), observed, observed + ttl))
    if not rows or ttl <= 0:
        return
    try:
        with sqlite3.connect(store.DB_PATH, timeout=0.05) as conn:
            conn.executescript(_SCHEMA)
            conn.executemany("INSERT OR REPLACE INTO route_observation VALUES (?, ?, ?, ?)", rows)
            conn.execute("DELETE FROM route_observation WHERE expires_at <= ?", (time(),))
    except (OSError, sqlite3.Error):
        logger.warning("Observed routes could not be persisted; current plan retains provider results")


class _AttemptBudget:
    def __init__(self, used, limit, end):
        self.used = max(0, int(used))
        self.limit = max(0, int(limit))
        self.end = end
        self.blocked = None
        self.lock = Lock()

    def take(self):
        with self.lock:
            if self.blocked:
                return ToolResult.failure("地图服务配置或配额错误，停止后续路线请求", self.blocked)
            if monotonic() >= self.end:
                return ToolResult.failure("已达到本轮路线查询时间预算", "ROUTE_TIME_BUDGET")
            if self.used >= self.limit:
                return ToolResult.failure("已达到本次路线查询上限", "ROUTE_QUERY_LIMIT")
            self.used += 1
            return None

    def block(self, code):
        with self.lock:
            self.blocked = code


def _estimate(a, b, mode, reason):
    distance = haversine_km(a, b)
    selected = _MODE_ALIASES.get(mode, mode)
    walking = selected == "walking"
    speed = 4 if walking else (12 if selected == "transit" else 22)
    return {"duration_minutes": max(5, math.ceil(distance*1.5/speed*60) + (0 if walking else 10)),
            "distance_meters": None, "status": "estimated", "source": "static_estimate",
            "note": reason + "按直线距离×1.5与交通速度估算；未获取实际道路路线。", "_cached_at": time()}


def _trace(a, b, mode, city, report):
    return {"role": "experience", "tool": "travel_route",
            "args": {"origin": a.model_dump(), "destination": b.model_dump(), "mode": mode, "city": city},
            "result": report.result.message, "outcome": report.result.to_dict(), "attempts": report.attempts,
            "attempt_count": len(report.attempts), "stop_reason": report.stop_reason, "next_action": report.next_action}


def _query(a, b, mode, city, quota, settings):
    raw = {}
    def attempt():
        nonlocal raw
        rejected = quota.take()
        if rejected:
            return rejected
        try:
            raw = amap.travel_route(a, b, mode=mode, city=city)
            if _anomalous(raw, a, b, mode):
                return ToolResult(status='error', message='供应商路线与坐标显著不一致，需核验；不作为已验证路线',
                                  error_code='ANOMALOUS_ROUTE_RESULT', retryable=True,
                                  stats={'observed_route':raw, 'straight_km':round(haversine_km(a,b),3)})
            if _known(raw):
                return ToolResult.success("已取得真实路线时间", returned_count=1)
            if isinstance(raw, dict) and raw.get("status") == "known":
                return ToolResult.failure("路线接口返回的耗时、距离或来源不合法", "INVALID_ROUTE_RESULT")
            return ToolResult.success("未找到可用路线", returned_count=0)
        except (amap.AmapError, amap.AmapUnreachable, ValueError, TypeError) as exc:
            code = getattr(exc, "code", "INVALID_ROUTE_RESULT")
            if code in ("AMAP_KEY_INVALID", "AMAP_QUOTA_EXCEEDED"):
                quota.block(code)
            return ToolResult.failure("路线接口查询失败", code, retryable=bool(getattr(exc, "retryable", False)),
                                      retry_after_seconds=getattr(exc, "retry_after_seconds", None))
    remaining = quota.end - monotonic()
    if quota.blocked:
        report = ExecutionReport(ToolResult.failure("地图服务配置或配额错误，停止后续路线请求", quota.blocked), [], "upstream_blocked")
    elif remaining <= 0:
        report = ExecutionReport(ToolResult.failure("已达到本轮路线查询时间预算", "ROUTE_TIME_BUDGET"), [], "time_budget")
    else:
        report = run(attempt, safe=True, policy=RetryPolicy(settings.tool_max_attempts,
                     min(settings.tool_retry_budget_seconds, remaining), settings.tool_retry_base_delay))
    if report.result.status == "ok" and _known(raw):
        value = {"duration_minutes": math.ceil(raw["duration_minutes"]), "distance_meters": raw.get("distance_meters"),
                 "status": "known", "source": "amap", "_cached_at": time(),
                 "note": "高德路线接口结果，出发时路况仍可能变化。"}
        # A compatibility adapter without explicit source must not create shared
        # evidence simply because normalization supplies a display source.
        persistable = raw.get("source") == "amap" and _number(raw.get("distance_meters"))
    else:
        reason = (f"路线接口未取得结果（{report.result.error_code}）。" if report.result.status == "error"
                  else "路线接口未找到适用交通方式的可用路线（ROUTE_NOT_FOUND）。")
        value = _estimate(a, b, mode, reason)
        persistable = False
    return value, _trace(a, b, mode, city, report), persistable


def prefetch_routes(pairs, *, mode, city, cache, query_count=0, max_queries=48,
                    max_workers=None, budget_seconds=None, cache_ttl_seconds=None, blocked_code=None):
    """Resolve each exact OD pair once, then merge results on the calling thread.

    The count covers every physical gateway invocation, including retries, and
    is shared by all workers and retained by the caller across repair rounds.
    Gateway transports enforce the remaining deadline, including gate waits.
    Existing in-flight synchronous HTTP operations finish before this returns;
    no detached worker can mutate plan state after a timeout.
    """
    started = monotonic()
    settings = get_settings()
    workers = int(max_workers if max_workers is not None else getattr(settings, "route_max_workers", 4))
    seconds = float(budget_seconds if budget_seconds is not None else getattr(settings, "route_batch_budget_seconds", 12.0))
    ttl = float(cache_ttl_seconds if cache_ttl_seconds is not None else getattr(settings, "route_cache_ttl_seconds", 900.0))
    if not 1 <= workers <= 8 or not math.isfinite(seconds) or seconds < 0 or not math.isfinite(ttl) or ttl < 0:
        raise ValueError("Invalid route worker/time/cache limits")
    unique = {}
    for origin, destination in pairs:
        a, b = _location(origin), _location(destination)
        unique.setdefault(route_key(a, b, mode, city), (a, b))
    quota = _AttemptBudget(query_count, max_queries, started + seconds)
    if blocked_code:
        quota.block(blocked_code)
    now = time()
    pending = {}
    for key, pair in unique.items():
        reused = cached_route(cache.get(key), now=now, ttl=ttl)
        if reused is not None and _anomalous(reused, *pair, mode):
            reused = None
        if reused is None:
            pending[key] = pair
        else:
            cache[key] = reused
    observations = _read_observations(pending, ttl) if monotonic() < quota.end else {}
    observations = {key: value for key, value in observations.items() if not _anomalous(value, *pending[key], mode)}
    cache.update(observations)
    calls = []
    for key in observations:
        a, b = pending[key]
        report = ExecutionReport(ToolResult.success("复用有效的高德路线观测缓存", returned_count=1, cache_hit=True), [], "cache_hit")
        calls.append(_trace(a, b, mode, city, report))
    pending = {key: pair for key, pair in pending.items() if key not in observations}
    observed = {}
    if pending:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="route-prefetch") as pool:
            futures = [(key, pool.submit(_query, a, b, mode, city, quota, settings))
                       for key, (a, b) in pending.items()]
            # Read in input order so traces and fixtures are deterministic even
            # when the upstream completes requests in a different order.
            for key, future in futures:
                value, trace, persistable = future.result()
                cache[key] = value
                calls.append(trace)
                if persistable:
                    observed[key] = value
    _write_observations(observed, ttl)
    return RouteBatchResult(cache=cache, calls=calls, query_count=quota.used,
                            cache_hits=len(unique)-len(pending), elapsed_ms=round((monotonic()-started)*1000, 3),
                            blocked_code=quota.blocked)
