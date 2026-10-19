"""Durable, date-aware forecast cache; refresh coalescing is single-process only.

Forecast dates are upstream dates, never shifted to a requested travel date. An
expired forecast is not returned on refresh failure. Typed gateway errors retain
their code/retry policy, including while a bounded failure cooldown is active.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from ..config import get_settings
from ..models.schemas import WeatherInfo
from . import map_gateway, store

_CHINA = timezone(timedelta(hours=8))
_guard = threading.Lock()


@dataclass
class _Flight:
    lock: threading.Lock = field(default_factory=threading.Lock)
    generation: int = 0
    last_result: tuple[list[WeatherInfo], dict] | None = None
    last_failure: dict | None = None
    retry_at: datetime | None = None


@dataclass
class _Attempt:
    upstream_calls: int = 0
    result: tuple[list[WeatherInfo], dict] | None = None
    error: Exception | None = None


_flights: dict[tuple[str, str, str], _Flight] = {}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_forecast_cache (
    adcode TEXT NOT NULL,
    source_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    forecast_day TEXT NOT NULL,
    PRIMARY KEY (adcode, source_key)
);
CREATE TABLE IF NOT EXISTS weather_refresh_failure (
    adcode TEXT NOT NULL,
    source_key TEXT NOT NULL,
    error_json TEXT NOT NULL,
    retry_at TEXT NOT NULL,
    PRIMARY KEY (adcode, source_key)
);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _bounded_setting(name: str, default: float, maximum: float) -> float:
    try:
        value = float(getattr(get_settings(), name, default))
        return min(maximum, max(0.0, value)) if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _identity() -> tuple[str, str, str]:
    settings = get_settings()
    backend = getattr(settings, "map_backend", "local")
    if backend == "local":
        return "local", "amap", "amap"
    # Distinct configured MCP servers must not share a cache. Credentials are
    # hashed as part of the identity and never persisted or exposed in metadata.
    names = ("mcp_transport", "mcp_command", "mcp_args", "mcp_url", "mcp_env", "mcp_headers")
    config = {name: getattr(settings, name, None) for name in names}
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    return f"mcp:{digest}", "configured_mcp", "mcp"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
    except Exception:
        conn.close()
        raise
    return conn


def _normalise(raw: object, today: date) -> tuple[list[WeatherInfo], dict]:
    if not isinstance(raw, list):
        raise map_gateway.AmapUnreachable(
            "天气接口返回的预报结构不合法", retryable=False, code="WEATHER_INVALID_RESULT")
    if not raw:
        raise map_gateway.AmapUnreachable(
            "天气接口未返回预报", retryable=True, code="WEATHER_EMPTY_RESULT")
    valid: dict[str, WeatherInfo] = {}
    discarded = 0
    for item in raw:
        try:
            forecast = WeatherInfo.model_validate(item)
            target = date.fromisoformat(forecast.date)
            if (target.isoformat() != forecast.date or target < today
                    or not forecast.is_forecast or forecast.beyond_window
                    or not (forecast.day_weather.strip() or forecast.night_weather.strip())
                    or forecast.date in valid):
                discarded += 1
                continue
            valid[forecast.date] = forecast
        except (ValidationError, ValueError, TypeError):
            discarded += 1
    if not valid:
        raise map_gateway.AmapUnreachable(
            "天气接口未返回日期有效的真实预报", retryable=True, code="WEATHER_INVALID_RESULT")
    forecasts = [valid[key] for key in sorted(valid)]
    # AMap's documented forecast window is four days. Shorter/gapped real
    # responses remain usable for their own dates, explicitly marked partial.
    expected = {(today + timedelta(days=n)).isoformat() for n in range(4)}
    return forecasts, {
        "quality": "complete" if not discarded and expected.issubset(valid) else "partial",
        "forecast_dates": [forecast.date for forecast in forecasts],
        "discarded_records": discarded,
    }


def _failure_payload(exc: Exception) -> dict:
    # HTTP exception text can contain a complete request URL, including API keys
    # in query parameters. Persist actionable typed fields, never raw messages.
    if isinstance(exc, map_gateway.AmapError):
        return {"kind": "amap", "code": exc.code, "detail": "天气服务配置或请求错误"}
    return {"kind": "unreachable", "code": exc.code, "detail": "天气服务查询失败，请根据错误码处理",
            "retryable": exc.retryable, "retry_after_seconds": exc.retry_after_seconds}


def _restore_failure(payload: dict) -> Exception:
    if payload["kind"] == "amap":
        return map_gateway.AmapError(payload["code"], payload["detail"])
    return map_gateway.AmapUnreachable(
        payload["detail"], code=payload["code"], retryable=payload["retryable"],
        retry_after_seconds=payload.get("retry_after_seconds"))


def _cached(conn: sqlite3.Connection, adcode: str, source_key: str,
            now: datetime) -> tuple[list[WeatherInfo], dict] | None:
    row = conn.execute(
        "SELECT * FROM weather_forecast_cache WHERE adcode=? AND source_key=?",
        (adcode, source_key)).fetchone()
    if row is None:
        return None
    today = now.astimezone(_CHINA).date()
    try:
        if datetime.fromisoformat(row["expires_at"]) <= now or row["forecast_day"] != today.isoformat():
            return None
        forecasts, _ = _normalise(json.loads(row["payload_json"]), today)
        metadata = json.loads(row["metadata_json"])
        if not isinstance(metadata, dict):
            return None
        metadata.update(fetched_at=row["fetched_at"], expires_at=row["expires_at"])
        return forecasts, metadata
    except (ValueError, TypeError, KeyError, map_gateway.AmapUnreachable):
        # A corrupt local entry is never served as trusted data. Online refresh
        # can replace it; a failed refresh still follows the typed error path.
        return None


def _retry_at(exc: Exception) -> datetime:
    cooldown = _bounded_setting("weather_cache_failure_cooldown_seconds", 30, 300)
    retry_after = getattr(exc, "retry_after_seconds", None)
    if isinstance(retry_after, (int, float)) and math.isfinite(retry_after):
        cooldown = max(cooldown, min(300, max(0, retry_after)))
    return _utcnow() + timedelta(seconds=cooldown)


def _load_observation(adcode, loader, provider, source, attempt):
    """Keep an upstream result before attempting any SQLite writes."""
    attempt.upstream_calls = 1
    try:
        now = _utcnow()
        forecasts, metadata = _normalise(
            (loader or map_gateway.get_weather)(adcode), now.astimezone(_CHINA).date())
        fetched_at = _utcnow()
        today = fetched_at.astimezone(_CHINA).date()
        # A request can cross midnight; revalidate before assigning a new day.
        forecasts, coverage = _normalise(forecasts, today)
        if coverage["quality"] == "partial":
            metadata["quality"] = "partial"
        metadata["forecast_dates"] = coverage["forecast_dates"]
        metadata["discarded_records"] += coverage["discarded_records"]
        ttl = _bounded_setting("weather_cache_ttl_seconds", 3600, 21600)
        midnight = datetime.combine(today + timedelta(days=1), datetime.min.time(), _CHINA)
        expires_at = min(fetched_at + timedelta(seconds=ttl), midnight.astimezone(timezone.utc))
        metadata.update(provider=provider, source=source,
                        fetched_at=fetched_at.isoformat(), expires_at=expires_at.isoformat())
        attempt.result = forecasts, metadata
        return forecasts, metadata
    except Exception as exc:
        if not isinstance(exc, (map_gateway.AmapError, map_gateway.AmapUnreachable)):
            exc = map_gateway.AmapUnreachable(
                "天气上游调用失败", retryable=True, code="WEATHER_UPSTREAM_ERROR")
        attempt.error = exc
        raise exc


def _unavailable_metadata(provider, source, upstream_calls=0):
    return {"cache_status": "unavailable", "cache_warning": "本地天气缓存暂不可用",
            "local_hit": False, "upstream_calls": upstream_calls,
            "provider": provider, "source": source}


def _memory_outcome(flight, coalesced, metadata):
    now = _utcnow()
    previous = getattr(flight, "last_result", None)
    if (coalesced and previous is not None
            and datetime.fromisoformat(previous[1]["expires_at"]) > now):
        forecasts, provenance = previous
        return [item.model_copy(deep=True) for item in forecasts], {**provenance, **metadata}
    retry_at = getattr(flight, "retry_at", None)
    failure = getattr(flight, "last_failure", None)
    if failure is not None and retry_at is not None and retry_at > now:
        exc = _restore_failure(failure)
        exc.weather_cache = {**metadata, "retry_at": retry_at.isoformat()}
        raise exc
    return None


def _lookup(db_path, adcode, source_key, provider, source, loader,
            force_refresh, coalesced, attempt, flight):
    with closing(_connect(db_path)) as conn:
        now = _utcnow()
        if not force_refresh or coalesced:
            cached = _cached(conn, adcode, source_key, now)
            if cached is not None:
                forecasts, metadata = cached
                metadata.update(cache_status="coalesced" if coalesced else "hit",
                                local_hit=True, upstream_calls=0)
                return forecasts, metadata
        failure = conn.execute(
            "SELECT error_json, retry_at FROM weather_refresh_failure WHERE adcode=? AND source_key=?",
            (adcode, source_key)).fetchone()
        if failure is not None and datetime.fromisoformat(failure["retry_at"]) > now:
            exc = _restore_failure(json.loads(failure["error_json"]))
            exc.weather_cache = {"cache_status": "failure_cooldown", "local_hit": False,
                                 "upstream_calls": 0, "provider": provider, "source": source,
                                 "retry_at": failure["retry_at"]}
            raise exc
        # A preceding request may have received a valid provider result but
        # failed to persist it. Check its in-flight result before another fetch,
        # even when this connection can still read an empty/read-only database.
        memory = _memory_outcome(flight, coalesced, _unavailable_metadata(provider, source))
        if memory is not None:
            return memory
        try:
            forecasts, metadata = _load_observation(adcode, loader, provider, source, attempt)
        except (map_gateway.AmapError, map_gateway.AmapUnreachable) as exc:
            retry_at = _retry_at(exc).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO weather_refresh_failure VALUES (?, ?, ?, ?)",
                (adcode, source_key, json.dumps(_failure_payload(exc), ensure_ascii=False), retry_at))
            conn.commit()
            exc.weather_cache = {"cache_status": "refresh_failed", "local_hit": False,
                                 "upstream_calls": 1, "provider": provider, "source": source,
                                 "retry_at": retry_at}
            raise
        conn.execute(
            "INSERT OR REPLACE INTO weather_forecast_cache VALUES (?, ?, ?, ?, ?, ?, ?)",
            (adcode, source_key,
             json.dumps([forecast.model_dump() for forecast in forecasts], ensure_ascii=False),
             json.dumps(metadata, ensure_ascii=False), metadata["fetched_at"], metadata["expires_at"],
             datetime.fromisoformat(metadata["fetched_at"]).astimezone(_CHINA).date().isoformat()))
        conn.execute("DELETE FROM weather_refresh_failure WHERE adcode=? AND source_key=?",
                     (adcode, source_key))
        conn.commit()
        metadata.update(cache_status="refreshed" if force_refresh else "miss",
                        local_hit=False, upstream_calls=1)
        return forecasts, metadata


def _without_database(adcode, loader, provider, source, attempt, flight, coalesced):
    metadata = _unavailable_metadata(provider, source, attempt.upstream_calls)
    if attempt.result is None and attempt.error is None:
        memory = _memory_outcome(flight, coalesced, metadata)
        if memory is not None:
            return memory
        try:
            _load_observation(adcode, loader, provider, source, attempt)
        except (map_gateway.AmapError, map_gateway.AmapUnreachable):
            pass  # Preserve the exact typed provider failure below.
        metadata["upstream_calls"] = attempt.upstream_calls
    if attempt.error is not None:
        attempt.error.weather_cache = {**metadata, "retry_at": _retry_at(attempt.error).isoformat()}
        raise attempt.error
    forecasts, provenance = attempt.result
    return forecasts, {**provenance, **metadata}


def get_forecast(adcode: str, *, loader: Callable | None = None,
                 force_refresh: bool = False) -> tuple[list[WeatherInfo], dict]:
    """Return actual forecast dates and provenance, or raise a typed map error.

    Metadata: cache_status=hit/miss/refreshed/coalesced/unavailable, local_hit,
    upstream_calls, provider/source, fetched_at/expires_at (UTC), quality and
    forecast_dates. SQLite failures fall back to validated provider data, with a
    sanitized cache_warning; persistence failure never repeats a completed fetch.
    Errors additionally expose ``weather_cache`` metadata. ``force_refresh``
    coalesces overlapping refreshes and respects bounded failure cooldown.
    """
    adcode = str(adcode).strip()
    if not re.fullmatch(r"\d{6}", adcode):
        raise map_gateway.AmapError("WEATHER_INVALID_ADCODE", "天气查询需要六位行政区代码")
    db_path = Path(store.DB_PATH).resolve()
    source_key, provider, source = _identity()
    key = (str(db_path), adcode, source_key)
    with _guard:
        flight = _flights.setdefault(key, _Flight())
        observed_generation = flight.generation
    with flight.lock:
        attempt = _Attempt()
        coalesced = observed_generation != flight.generation
        try:
            try:
                return _lookup(db_path, adcode, source_key, provider, source, loader,
                               force_refresh, coalesced, attempt, flight)
            except (sqlite3.Error, OSError):
                return _without_database(adcode, loader, provider, source, attempt, flight, coalesced)
        finally:
            if attempt.upstream_calls:
                flight.generation += 1
                if attempt.result is not None:
                    forecasts, metadata = attempt.result
                    flight.last_result = ([item.model_copy(deep=True) for item in forecasts], dict(metadata))
                    flight.last_failure = None
                    flight.retry_at = None
                elif attempt.error is not None:
                    flight.last_result = None
                    flight.last_failure = _failure_payload(attempt.error)
                    flight.retry_at = _retry_at(attempt.error)


def invalidate(adcode: str | None = None, *, db_path: str | Path | None = None,
               clear_memory_only: bool = False) -> None:
    """Clear this database's cache, or only process-local state for restart tests.

    Call while this database is idle. Other databases' cache/locks are untouched.
    """
    target = Path(db_path if db_path is not None else store.DB_PATH).resolve()
    with _guard:
        keys = [key for key in _flights if key[0] == str(target) and (adcode is None or key[1] == adcode)]
        for key in keys:
            del _flights[key]
    if clear_memory_only:
        return
    with closing(_connect(target)) as conn:
        for table in ("weather_forecast_cache", "weather_refresh_failure"):
            if adcode is None:
                conn.execute(f"DELETE FROM {table}")
            else:
                conn.execute(f"DELETE FROM {table} WHERE adcode=?", (adcode,))
        conn.commit()
