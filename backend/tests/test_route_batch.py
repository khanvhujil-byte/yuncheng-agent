"""Route latency fixes must preserve quota, source provenance and failure evidence."""
from threading import Barrier, Lock
from time import sleep, monotonic

import pytest

from app.services import route_batch as batch, store
from app.services.tool_retry import remaining_seconds


def point(index):
    return {"name": f"point-{index}", "location": {"longitude": 120 + index/1000, "latitude": 30.0}}


def pairs(count):
    return [(point(index), point(index + 1)) for index in range(count)]


def known(*args, **kwargs):
    return {"duration_minutes": 7.2, "distance_meters": 1234, "status": "known", "source": "amap"}


def observations():
    with store._connect() as conn:
        return conn.execute("SELECT * FROM route_observation").fetchall()


def test_exact_duplicates_share_one_request_and_workers_really_overlap(monkeypatch):
    lock = Lock()
    barrier = Barrier(3)
    calls = 0
    active = 0
    peak = 0

    def route(*args, **kwargs):
        nonlocal calls, active, peak
        with lock:
            calls += 1
            current = calls
            active += 1
            peak = max(peak, active)
        if current <= 3:
            barrier.wait(timeout=3)
        sleep(0.015)
        with lock:
            active -= 1
        return known()

    monkeypatch.setattr(batch.amap, "travel_route", route)
    inputs = pairs(6)
    result = batch.prefetch_routes(inputs + inputs + inputs, mode="walking", city="杭州", cache={}, max_workers=3)
    assert calls == result.query_count == len(result.cache) == 6
    assert peak == 3
    assert len(result.calls) == 6
    assert all(v["duration_minutes"] == 8 and v["status"] == "known" for v in result.cache.values())


def test_shared_attempt_limit_includes_retries_and_prior_round(monkeypatch, settings):
    monkeypatch.setattr(settings, "tool_retry_base_delay", 0)
    lock = Lock()
    calls = 0

    def route(*args, **kwargs):
        nonlocal calls
        with lock:
            calls += 1
        sleep(0.002)
        raise batch.amap.AmapUnreachable("transient", code="UPSTREAM_RATE_LIMITED", retry_after_seconds=0)

    monkeypatch.setattr(batch.amap, "travel_route", route)
    result = batch.prefetch_routes(pairs(12), mode="driving", city="杭州", cache={},
                                   query_count=44, max_queries=48, max_workers=4)
    assert calls == 4
    assert result.query_count == 48
    assert all(v["status"] == "estimated" for v in result.cache.values())
    codes = [a["outcome"]["error_code"] for call in result.calls for a in call["attempts"]]
    assert "UPSTREAM_RATE_LIMITED" in codes and "ROUTE_QUERY_LIMIT" in codes
    assert not observations()
    again = batch.prefetch_routes(pairs(2), mode="driving", city="杭州", cache={}, query_count=result.query_count)
    assert calls == 4 and again.query_count == 48


def test_retry_evidence_survives_success_and_only_true_observation_is_persisted(monkeypatch, settings):
    monkeypatch.setattr(settings, "tool_retry_base_delay", 0)
    calls = []

    def route(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise batch.amap.AmapUnreachable("transient", code="UPSTREAM_TIMEOUT")
        return known()

    monkeypatch.setattr(batch.amap, "travel_route", route)
    result = batch.prefetch_routes(pairs(1), mode="walking", city="杭州", cache={})
    trace = result.calls[0]
    assert result.query_count == trace["attempt_count"] == 2
    assert trace["attempts"][0]["outcome"]["error_code"] == "UPSTREAM_TIMEOUT"
    assert trace["outcome"]["status"] == "ok"
    assert len(observations()) == 1


def test_persistent_cache_reuses_evidence_without_spending_attempts(monkeypatch):
    monkeypatch.setattr(batch.amap, "travel_route", known)
    initial = batch.prefetch_routes(pairs(2), mode="公共交通", city="杭州", cache={})
    monkeypatch.setattr(batch.amap, "travel_route", lambda *a, **kw: pytest.fail("cache must avoid provider"))
    result = batch.prefetch_routes(pairs(2), mode="公共交通", city="杭州", cache={}, query_count=9)
    assert initial.query_count == 2
    assert result.query_count == 9 and result.cache_hits == 2
    assert all(call["outcome"]["stats"]["cache_hit"] and call["attempt_count"] == 0 for call in result.calls)
    assert all(value["status"] == "known" and "缓存" in value["note"] for value in result.cache.values())


def test_cache_keys_preserve_direction_city_mode_provider_and_exact_coordinates(monkeypatch, settings):
    a, b = point(0), point(1)
    key = batch.route_key(a, b, "公共交通", "杭州")
    assert key == batch.route_key(a, b, "transit", "杭州")
    alternatives = [batch.route_key(b, a, "transit", "杭州"), batch.route_key(a, b, "walking", "杭州"),
                    batch.route_key(a, b, "transit", "绍兴"), batch.route_key(point(0.0001), b, "transit", "杭州")]
    monkeypatch.setattr(settings, "map_backend", "mcp")
    alternatives.append(batch.route_key(a, b, "transit", "杭州"))
    monkeypatch.setattr(settings, "mcp_url", "https://other.invalid/mcp")
    alternatives.append(batch.route_key(a, b, "transit", "杭州"))
    assert key not in alternatives and len(set(alternatives)) == len(alternatives)


def test_cache_expiry_does_not_slide_on_read_and_expires_memory_too(monkeypatch):
    clock = [1000.0]
    calls = []
    monkeypatch.setattr(batch, "time", lambda: clock[0])
    monkeypatch.setattr(batch.amap, "travel_route", lambda *a, **kw: calls.append(1) or known())
    result = batch.prefetch_routes(pairs(1), mode="walking", city="杭州", cache={}, cache_ttl_seconds=10)
    clock[0] = 1009
    cached = batch.prefetch_routes(pairs(1), mode="walking", city="杭州", cache={}, cache_ttl_seconds=10)
    assert len(calls) == 1 and cached.cache_hits == 1
    assert observations()[0]["observed_at"] == 1000
    clock[0] = 1011
    refreshed = batch.prefetch_routes(pairs(1), mode="walking", city="杭州", cache=result.cache, cache_ttl_seconds=10)
    assert len(calls) == 2 and refreshed.cache_hits == 0
    assert observations()[0]["observed_at"] == 1011


@pytest.mark.parametrize("raw", [
    {"status": "unknown", "source": "unknown", "duration_minutes": None},
    {"status": "estimated", "source": "estimate", "duration_minutes": 12},
    {"status": "known", "source": "estimate", "duration_minutes": 12},
    {"status": "known", "source": "amap", "duration_minutes": float("nan")},
    {"status": "known", "source": "amap", "duration_minutes": True},
])
def test_empty_estimated_or_invalid_routes_never_pollute_evidence_cache(monkeypatch, raw):
    calls = []
    monkeypatch.setattr(batch.amap, "travel_route", lambda *a, **kw: calls.append(1) or raw)
    result = batch.prefetch_routes(pairs(1), mode="walking", city="杭州", cache={})
    value = next(iter(result.cache.values()))
    assert value["status"] == "estimated" and value["source"] == "static_estimate"
    assert "未获取实际道路路线" in value["note"] and not observations()
    batch.prefetch_routes(pairs(1), mode="walking", city="杭州", cache={})
    assert len(calls) == 2  # No cross-request negative cache.


def test_key_error_stops_pending_work_and_preserves_specific_failure(monkeypatch):
    calls = []

    def invalid(*args, **kwargs):
        calls.append(1)
        raise batch.amap.AmapError("AMAP_KEY_INVALID", "bad key")

    monkeypatch.setattr(batch.amap, "travel_route", invalid)
    result = batch.prefetch_routes(pairs(8), mode="driving", city="杭州", cache={}, max_workers=1)
    assert len(calls) == result.query_count == 1
    assert all(call["outcome"]["error_code"] == "AMAP_KEY_INVALID" and not call["outcome"]["retryable"] for call in result.calls)
    assert all("AMAP_KEY_INVALID" in value["note"] for value in result.cache.values())
    assert not observations()


def test_batch_deadline_reaches_transport_and_queued_routes_do_not_start(monkeypatch, settings):
    monkeypatch.setattr(settings, "tool_retry_base_delay", 0)
    calls = []

    def timeout(*args, **kwargs):
        remaining = remaining_seconds()
        calls.append(remaining)
        assert 0 < remaining <= 0.06
        sleep(remaining)
        raise batch.amap.AmapUnreachable("timeout", code="UPSTREAM_TIMEOUT")

    monkeypatch.setattr(batch.amap, "travel_route", timeout)
    started = monotonic()
    result = batch.prefetch_routes(pairs(20), mode="driving", city="杭州", cache={}, max_workers=2, budget_seconds=0.06)
    assert monotonic()-started < 0.5
    assert len(calls) <= 2 and result.query_count == len(calls)
    assert any(call["outcome"]["error_code"] == "ROUTE_TIME_BUDGET" for call in result.calls)
    assert all(value["status"] == "estimated" for value in result.cache.values())


def test_parallel_prefetch_still_obeys_existing_provider_gate(monkeypatch, settings):
    from app.services import amap
    monkeypatch.setattr(settings, "amap_request_interval_seconds", 0.025)
    started = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "1", "infocode": "10000", "route": {"paths": [{"duration": "600", "distance": "1000"}]}}

    def get(*args, **kwargs):
        started.append(monotonic())
        return Response()

    monkeypatch.setattr(amap.httpx, "get", get)
    result = batch.prefetch_routes(pairs(4), mode="walking", city="杭州", cache={}, max_workers=4)
    assert len(started) == result.query_count == 4
    assert all(b-a >= 0.02 for a, b in zip(started, started[1:]))
    assert all(value["status"] == "known" for value in result.cache.values())


def test_unavailable_database_does_not_discard_successful_provider_result(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DB_PATH", tmp_path)  # A directory cannot be an SQLite file.
    monkeypatch.setattr(batch.amap, "travel_route", known)
    result = batch.prefetch_routes(pairs(1), mode="walking", city="杭州", cache={})
    assert result.query_count == 1 and next(iter(result.cache.values()))["status"] == "known"


def test_permanent_failure_blocks_new_edges_in_later_batch_without_spending_quota(monkeypatch):
    calls = []

    def invalid(*args, **kwargs):
        calls.append(1)
        raise batch.amap.AmapError("AMAP_KEY_INVALID", "bad key")

    monkeypatch.setattr(batch.amap, "travel_route", invalid)
    initial = batch.prefetch_routes(pairs(2), mode="driving", city="杭州", cache={}, max_workers=1)
    assert initial.blocked_code == "AMAP_KEY_INVALID"
    bridges = [(point(20), point(21)), (point(21), point(22))]
    later = batch.prefetch_routes(bridges, mode="driving", city="杭州", cache={},
                                  query_count=initial.query_count, blocked_code=initial.blocked_code, budget_seconds=0)
    assert len(calls) == initial.query_count == later.query_count == 1
    assert later.blocked_code == "AMAP_KEY_INVALID"
    assert all(call["outcome"]["error_code"] == "AMAP_KEY_INVALID" and call["attempt_count"] == 0 for call in later.calls)
    assert all("AMAP_KEY_INVALID" in value["note"] for value in later.cache.values())


def test_public_cache_validation_keeps_ttl_and_refreshes_age_without_mutating_input():
    value = {**known(), "_cached_at": 1000.0, "note": "original provider note"}
    reused = batch.cached_route(value, now=1121, ttl=900)
    assert "缓存（3 分钟内）" in reused["note"]
    assert reused["_cached_at"] == value["_cached_at"] == 1000
    assert value["note"] == "original provider note"
    assert batch.cached_route(value, now=1900, ttl=900) is None
    assert batch.cached_route(value, now=999, ttl=900) is None
    assert batch.cached_route({**value, "_cached_at": None}, now=1121, ttl=900) is None
    estimate = {"status": "estimated", "source": "static_estimate", "duration_minutes": 12,
                "_cached_at": 1000, "note": "UPSTREAM_TIMEOUT; geometric estimate"}
    assert batch.cached_route(estimate, now=1014, ttl=900)["note"] == estimate["note"]
    assert batch.cached_route(estimate, now=1015, ttl=900) is None
    assert batch.cached_route({**estimate, "duration_minutes": float("nan")}, now=1001, ttl=900) is None


def test_memory_hit_refreshes_display_age_but_not_observation_or_request_count(monkeypatch):
    clock = [1000.0]
    calls = []
    monkeypatch.setattr(batch, "time", lambda: clock[0])
    monkeypatch.setattr(batch.amap, "travel_route", lambda *a, **kw: calls.append(1) or known())
    initial = batch.prefetch_routes(pairs(1), mode="walking", city="杭州", cache={})
    clock[0] = 1121
    later = batch.prefetch_routes(pairs(1), mode="walking", city="杭州", cache=initial.cache, query_count=initial.query_count)
    assert later.query_count == len(calls) == 1 and later.cache_hits == 1
    assert next(iter(later.cache.values()))["_cached_at"] == 1000
    assert "缓存（3 分钟内）" in next(iter(later.cache.values()))["note"]
