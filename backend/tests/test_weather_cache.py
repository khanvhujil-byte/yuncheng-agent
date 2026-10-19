"""Forecast dates, durable cache, concurrent refresh and failure semantics."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.schemas import WeatherInfo
from app.services import map_gateway, store, weather_cache


@pytest.fixture
def clock(monkeypatch):
    state = {"now": datetime(2026, 10, 18, 2, tzinfo=timezone.utc)}
    monkeypatch.setattr(weather_cache, "_utcnow", lambda: state["now"])
    monkeypatch.setattr(weather_cache, "get_settings", lambda: SimpleNamespace(
        map_backend="local", weather_cache_ttl_seconds=3600,
        weather_cache_failure_cooldown_seconds=30))
    yield state
    weather_cache.invalidate(clear_memory_only=True)


def forecast(start="2026-10-18", days=4):
    start = datetime.fromisoformat(start)
    return [WeatherInfo(date=(start + timedelta(days=n)).date().isoformat(),
                        day_weather="晴", night_weather="多云", day_temp=22, night_temp=15)
            for n in range(days)]


def test_persisted_hit_survives_process_state_reset(clock):
    calls = []

    def loader(adcode):
        calls.append(adcode)
        return forecast()

    original, first = weather_cache.get_forecast("330100", loader=loader)
    weather_cache.invalidate(clear_memory_only=True)
    restored, second = weather_cache.get_forecast("330100", loader=loader)
    assert calls == ["330100"]
    assert restored == original
    assert first["cache_status"] == "miss"
    assert second["cache_status"] == "hit"
    assert second["local_hit"] is True and second["upstream_calls"] == 0
    assert second["quality"] == "complete"
    assert second["provider"] == "amap"
    assert second["fetched_at"].endswith("+00:00")


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("database_mode", ["available", "open_failure", "write_failure"])
def test_overlapping_requests_share_one_upstream_refresh(clock, force, database_mode, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = []
    entered = threading.Barrier(21)
    all_queued = threading.Event()
    queue_guard = threading.Lock()
    queued = 0

    class ObservedLock:
        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            nonlocal queued
            with queue_guard:
                queued += 1
                if queued == 20:
                    all_queued.set()
            self.lock.acquire()
            return self

        def __exit__(self, *args):
            self.lock.release()

    monkeypatch.setattr(weather_cache, "_Flight", lambda: SimpleNamespace(
        lock=ObservedLock(), generation=0))
    if database_mode == "open_failure":
        def unavailable(_):
            raise sqlite3.OperationalError("database unavailable")
        monkeypatch.setattr(weather_cache, "_connect", unavailable)
    elif database_mode == "write_failure":
        connect = weather_cache._connect

        class ReadOnlyConnection:
            def __init__(self, path):
                self.connection = connect(path)

            def execute(self, sql, *args):
                if sql.lstrip().startswith("INSERT"):
                    raise sqlite3.OperationalError("cache read only")
                return self.connection.execute(sql, *args)

            def close(self):
                self.connection.close()

        monkeypatch.setattr(weather_cache, "_connect", ReadOnlyConnection)

    def loader(adcode):
        calls.append(adcode)
        started.set()
        assert release.wait(5)
        return forecast()

    def request():
        entered.wait(timeout=5)
        return weather_cache.get_forecast("330100", loader=loader, force_refresh=force)

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(request) for _ in range(20)]
        entered.wait(timeout=5)
        assert started.wait(5)
        # Every caller captured the generation before the provider can finish.
        # No timing-only sleep is needed to prove forced refresh coalescing.
        assert all_queued.wait(5)
        key = next(key for key in weather_cache._flights if key[0] == str(store.DB_PATH.resolve()))
        with weather_cache._guard:
            flight = weather_cache._flights[key]
        assert flight.generation == 0
        release.set()
        results = [future.result(timeout=5) for future in futures]
    assert calls == ["330100"]
    assert sum(metadata["upstream_calls"] for _, metadata in results) == 1
    assert all(items == forecast() for items, _ in results)
    if database_mode != "available":
        assert all(metadata["cache_status"] == "unavailable" and not metadata["local_hit"]
                   for _, metadata in results)


def test_different_regions_refresh_in_parallel(clock):
    both_running = threading.Barrier(2)

    def loader(_):
        both_running.wait(timeout=5)
        return forecast()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(weather_cache.get_forecast, code, loader=loader)
                   for code in ("330100", "110100")]
        assert all(len(future.result(timeout=5)[0]) == 4 for future in futures)


def test_expiry_refresh_and_real_dates_are_not_shifted(clock):
    weather_cache.get_forecast("330100", loader=lambda _: forecast())
    clock["now"] += timedelta(hours=2)
    result, metadata = weather_cache.get_forecast("330100", loader=lambda _: forecast("2026-10-19", 2))
    assert metadata["cache_status"] == "miss"
    assert metadata["quality"] == "partial"
    assert [item.date for item in result] == ["2026-10-19", "2026-10-20"]
    assert "2026-10-25" not in metadata["forecast_dates"]


def test_midnight_invalidates_even_with_manually_extended_expiry(clock):
    weather_cache.get_forecast("330100", loader=lambda _: forecast())
    with sqlite3.connect(store.DB_PATH) as conn:
        conn.execute("UPDATE weather_forecast_cache SET expires_at='2026-10-20T12:00:00+00:00'")
    clock["now"] = datetime(2026, 10, 18, 16, 1, tzinfo=timezone.utc)
    result, metadata = weather_cache.get_forecast("330100", loader=lambda _: forecast("2026-10-19"))
    assert metadata["upstream_calls"] == 1
    assert result[0].date == "2026-10-19"


def test_empty_response_failure_cooldown_is_durable_and_bounded(clock):
    calls = []

    def empty(adcode):
        calls.append(adcode)
        return []

    with pytest.raises(map_gateway.AmapUnreachable) as first:
        weather_cache.get_forecast("330100", loader=empty)
    assert first.value.code == "WEATHER_EMPTY_RESULT"
    assert first.value.retryable is True
    weather_cache.invalidate(clear_memory_only=True)
    with pytest.raises(map_gateway.AmapUnreachable) as repeated:
        weather_cache.get_forecast("330100", loader=empty)
    assert repeated.value.weather_cache["upstream_calls"] == 0
    assert calls == ["330100"]
    clock["now"] += timedelta(seconds=31)
    result, _ = weather_cache.get_forecast("330100", loader=lambda _: forecast())
    assert len(result) == 4


@pytest.mark.parametrize("raw", [None, {"forecast": []}, [{}], ["bad"],
                                  [WeatherInfo(date="bad", day_weather="晴")],
                                  [WeatherInfo(date="2026-10-01", day_weather="晴")],
                                  [WeatherInfo(date="2026-10-18")]])
def test_malformed_or_outdated_upstream_is_never_successfully_cached(clock, raw):
    with pytest.raises(map_gateway.AmapUnreachable) as error:
        weather_cache.get_forecast("330100", loader=lambda _: raw)
    assert error.value.code == "WEATHER_INVALID_RESULT"
    with sqlite3.connect(store.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM weather_forecast_cache").fetchone()[0] == 0


def test_partial_payload_preserves_only_actual_valid_forecasts(clock):
    rows = forecast(days=2) + [WeatherInfo(date="invalid", day_weather="晴")]
    result, metadata = weather_cache.get_forecast("330100", loader=lambda _: rows)
    assert len(result) == 2
    assert metadata["quality"] == "partial"
    assert metadata["discarded_records"] == 1
    _, cached = weather_cache.get_forecast("330100", loader=lambda _: pytest.fail("cache miss"))
    assert cached["quality"] == "partial" and cached["discarded_records"] == 1


def test_corrupt_local_payload_is_replaced_by_valid_upstream(clock):
    weather_cache.get_forecast("330100", loader=lambda _: forecast())
    with sqlite3.connect(store.DB_PATH) as conn:
        conn.execute("UPDATE weather_forecast_cache SET payload_json='not JSON'")
    result, metadata = weather_cache.get_forecast("330100", loader=lambda _: forecast(days=2))
    assert len(result) == 2 and metadata["upstream_calls"] == 1


def test_failed_refresh_preserves_typed_error_and_does_not_return_expired_data(clock):
    weather_cache.get_forecast("330100", loader=lambda _: forecast())
    clock["now"] += timedelta(hours=2)
    calls = []

    def failed(adcode):
        calls.append(adcode)
        raise map_gateway.AmapUnreachable("limited", code="AMAP_RATE_LIMIT",
                                         retryable=True, retry_after_seconds=45)

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(weather_cache.get_forecast, "330100", loader=failed) for _ in range(100)]
        for future in futures:
            with pytest.raises(map_gateway.AmapUnreachable) as error:
                future.result(timeout=5)
            assert error.value.code == "AMAP_RATE_LIMIT"
            assert error.value.retryable is True
            assert error.value.retry_after_seconds == 45
    assert calls == ["330100"]
    clock["now"] += timedelta(seconds=46)
    result, _ = weather_cache.get_forecast("330100", loader=lambda _: forecast())
    assert len(result) == 4


def test_invalid_key_is_preserved_across_cooldown(clock):
    def invalid(_):
        raise map_gateway.AmapError("AMAP_KEY_INVALID", "invalid key")

    for _ in range(2):
        with pytest.raises(map_gateway.AmapError) as error:
            weather_cache.get_forecast("330100", loader=invalid)
        assert error.value.code == "AMAP_KEY_INVALID"


@pytest.mark.parametrize("configuration_error", [False, True])
def test_durable_failure_never_persists_raw_urls_or_credentials(clock, configuration_error):
    secret = "never-save-this-test-key"
    url = f"https://restapi.amap.com/v3/weather/weatherInfo?key={secret}&city=330100"

    def failed(_):
        if configuration_error:
            raise map_gateway.AmapError("AMAP_KEY_INVALID", f"provider echoed secret {url}")
        raise map_gateway.AmapUnreachable(
            f"HTTPStatusError: 429 for url '{url}'", retryable=True,
            code="UPSTREAM_RATE_LIMITED", retry_after_seconds=45)

    expected = map_gateway.AmapError if configuration_error else map_gateway.AmapUnreachable
    with pytest.raises(expected):
        weather_cache.get_forecast("330100", loader=failed)
    with sqlite3.connect(store.DB_PATH) as conn:
        stored = conn.execute("SELECT error_json FROM weather_refresh_failure").fetchone()[0]
    assert secret not in stored and "https://" not in stored and "weatherInfo" not in stored
    weather_cache.invalidate(clear_memory_only=True)
    with pytest.raises(expected) as restored:
        weather_cache.get_forecast("330100", loader=lambda _: pytest.fail("cooldown was lost"))
    assert secret not in str(restored.value) and "https://" not in str(restored.value)
    if configuration_error:
        assert restored.value.code == "AMAP_KEY_INVALID"
    else:
        assert restored.value.code == "UPSTREAM_RATE_LIMITED"
        assert restored.value.retryable is True
        assert restored.value.retry_after_seconds == 45


def test_cache_and_invalidation_are_scoped_to_database(clock, tmp_path, monkeypatch):
    first_db = store.DB_PATH
    weather_cache.get_forecast("330100", loader=lambda _: forecast())
    second_db = tmp_path / "other.sqlite"
    monkeypatch.setattr(store, "DB_PATH", second_db)
    _, metadata = weather_cache.get_forecast("330100", loader=lambda _: forecast(days=2))
    assert metadata["cache_status"] == "miss"
    weather_cache.invalidate()
    monkeypatch.setattr(store, "DB_PATH", first_db)
    result, _ = weather_cache.get_forecast("330100", loader=lambda _: pytest.fail("wrong database invalidated"))
    assert len(result) == 4


def test_local_and_mcp_provenance_never_share_cached_result(clock, monkeypatch):
    weather_cache.get_forecast("330100", loader=lambda _: forecast())
    config = SimpleNamespace(map_backend="mcp", mcp_url="https://example.invalid/mcp")
    monkeypatch.setattr(weather_cache, "get_settings", lambda: config)
    _, metadata = weather_cache.get_forecast("330100", loader=lambda _: forecast(days=2))
    assert metadata["source"] == "mcp" and metadata["cache_status"] == "miss"
    config.mcp_url = "https://another.invalid/mcp"
    _, changed = weather_cache.get_forecast("330100", loader=lambda _: forecast(days=3))
    assert changed["cache_status"] == "miss"


def test_force_refresh_bypasses_hit_but_not_failure_cooldown(clock):
    weather_cache.get_forecast("330100", loader=lambda _: forecast())
    _, metadata = weather_cache.get_forecast("330100", loader=lambda _: forecast(days=2), force_refresh=True)
    assert metadata["cache_status"] == "refreshed" and len(metadata["forecast_dates"]) == 2

    def fail(_):
        raise map_gateway.AmapUnreachable("timeout")

    with pytest.raises(map_gateway.AmapUnreachable):
        weather_cache.get_forecast("330100", loader=fail, force_refresh=True)
    with pytest.raises(map_gateway.AmapUnreachable) as error:
        weather_cache.get_forecast("330100", loader=lambda _: pytest.fail("cooldown bypassed"), force_refresh=True)
    assert error.value.weather_cache["cache_status"] == "failure_cooldown"


@pytest.mark.parametrize("error_type", [sqlite3.OperationalError, PermissionError])
def test_database_open_failure_uses_validated_provider_once(clock, monkeypatch, error_type):
    calls = []

    def unavailable(_):
        raise error_type("private local database path")

    def loader(code):
        calls.append(code)
        return forecast(days=2)

    monkeypatch.setattr(weather_cache, "_connect", unavailable)
    result, metadata = weather_cache.get_forecast("330100", loader=loader)
    assert calls == ["330100"] and len(result) == 2
    assert metadata["cache_status"] == "unavailable" and metadata["local_hit"] is False
    assert metadata["upstream_calls"] == 1 and metadata["quality"] == "partial"
    assert metadata["forecast_dates"] == ["2026-10-18", "2026-10-19"]
    assert "private" not in metadata["cache_warning"]


@pytest.mark.parametrize("failure_stage", ["read", "write", "commit"])
def test_database_failure_never_repeats_successful_upstream(clock, monkeypatch, failure_stage):
    connect = weather_cache._connect
    calls = []

    class FailingConnection:
        def __init__(self, path):
            self.connection = connect(path)

        def execute(self, sql, *args):
            if ((failure_stage == "read" and sql.lstrip().startswith("SELECT"))
                    or (failure_stage == "write" and sql.lstrip().startswith("INSERT"))):
                raise sqlite3.OperationalError("cache not available")
            return self.connection.execute(sql, *args)

        def commit(self):
            if failure_stage == "commit":
                raise sqlite3.OperationalError("cache commit failed")
            return self.connection.commit()

        def close(self):
            self.connection.close()

    def loader(code):
        calls.append(code)
        return forecast()

    monkeypatch.setattr(weather_cache, "_connect", FailingConnection)
    result, metadata = weather_cache.get_forecast("330100", loader=loader)
    assert len(result) == 4 and calls == ["330100"]
    assert metadata["cache_status"] == "unavailable" and metadata["upstream_calls"] == 1


def test_database_failure_retains_original_provider_error_and_memory_cooldown(clock, monkeypatch):
    calls = []

    def unavailable(_):
        raise sqlite3.OperationalError("cache down")

    def failed(code):
        calls.append(code)
        raise map_gateway.AmapUnreachable(
            "rate limited", code="UPSTREAM_RATE_LIMITED", retryable=True, retry_after_seconds=45)

    monkeypatch.setattr(weather_cache, "_connect", unavailable)
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(weather_cache.get_forecast, "330100", loader=failed) for _ in range(100)]
        for future in futures:
            with pytest.raises(map_gateway.AmapUnreachable) as error:
                future.result(timeout=5)
            assert error.value.code == "UPSTREAM_RATE_LIMITED"
            assert error.value.retryable is True and error.value.retry_after_seconds == 45
            assert error.value.weather_cache["cache_status"] == "unavailable"
    assert calls == ["330100"]
    clock["now"] += timedelta(seconds=46)
    result, _ = weather_cache.get_forecast("330100", loader=lambda _: forecast())
    assert len(result) == 4


def test_failed_cooldown_write_does_not_mask_provider_error_or_repeat_it(clock, monkeypatch):
    connect = weather_cache._connect
    calls = []

    class ReadOnlyConnection:
        def __init__(self, path):
            self.connection = connect(path)

        def execute(self, sql, *args):
            if sql.lstrip().startswith("INSERT"):
                raise sqlite3.OperationalError("cache read only")
            return self.connection.execute(sql, *args)

        def close(self):
            self.connection.close()

    def failed(code):
        calls.append(code)
        raise map_gateway.AmapError("AMAP_KEY_INVALID", "invalid key")

    monkeypatch.setattr(weather_cache, "_connect", ReadOnlyConnection)
    with pytest.raises(map_gateway.AmapError) as error:
        weather_cache.get_forecast("330100", loader=failed)
    assert error.value.code == "AMAP_KEY_INVALID"
    assert error.value.weather_cache["cache_status"] == "unavailable"
    assert error.value.weather_cache["upstream_calls"] == 1
    assert calls == ["330100"]


def test_database_unavailable_still_rejects_invalid_forecast(clock, monkeypatch):
    def unavailable(_):
        raise sqlite3.OperationalError("cache down")

    monkeypatch.setattr(weather_cache, "_connect", unavailable)
    with pytest.raises(map_gateway.AmapUnreachable) as error:
        weather_cache.get_forecast("330100", loader=lambda _: [WeatherInfo(date="2026-10-01", day_weather="晴")])
    assert error.value.code == "WEATHER_INVALID_RESULT"
    assert error.value.weather_cache["cache_status"] == "unavailable"
