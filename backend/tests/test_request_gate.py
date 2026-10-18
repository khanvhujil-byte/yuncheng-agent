"""Retry-After parsing, shared pacing, concurrent admission and actual tools."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from threading import Barrier, Event, Lock
from time import monotonic

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services import amap, tool_retry, tools
from app.services.request_gate import AdmissionTimeout, RequestGate, parse_retry_after
from app.services.tool_result import ToolResult
from app.services.tool_scheduler import execute_batch


NOW = datetime(2026, 10, 17, 10, 0, 0, tzinfo=timezone.utc)


class FakeTime:
    def __init__(self):
        self.now = 100.0
        self.waits = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.waits.append(seconds)
        self.now += seconds


@pytest.mark.parametrize(("header", "expected"), [
    ("12", 12.0), (" 0 ", 0.0), ("003", 3.0),
    (format_datetime(NOW + timedelta(seconds=30), usegmt=True), 30.0),
    (format_datetime(NOW - timedelta(seconds=30), usegmt=True), 0.0),
    (None, None), ("", None), ("-1", None), ("1.5", None), ("NaN", None),
    ("inf", None), ("1e10", None), ("３", None), ("9" * 400, None),
    ("junk", None), ("Sat, 17 Oct 2026 10:00:30", None),
])
def test_retry_after_formats(header, expected):
    assert parse_retry_after(header, now=NOW) == expected


def test_shared_interval_and_cooldown_never_shortens():
    timer = FakeTime()
    gate = RequestGate(clock=timer.clock, sleeper=timer.sleep)
    gate.acquire(interval=0.2, budget=20)
    gate.acquire(interval=0.2, budget=20)
    assert timer.now == pytest.approx(100.2)
    gate.defer(3)
    gate.defer(1)
    gate.acquire(interval=0.2, budget=20)
    assert timer.now == pytest.approx(103.2)


def test_cooldown_extension_during_wait_is_rechecked():
    timer = FakeTime()
    extended = []

    def wait(seconds):
        timer.sleep(seconds)
        if not extended:
            extended.append(True)
            gate.defer(2)

    gate = RequestGate(clock=timer.clock, sleeper=wait)
    gate.defer(1)
    gate.acquire(interval=0.2, budget=10)
    assert timer.now == pytest.approx(102.25)


def test_over_budget_cooldown_does_not_sleep_or_reserve_a_request():
    timer = FakeTime()
    gate = RequestGate(clock=timer.clock, sleeper=timer.sleep)
    gate.defer(30)
    with pytest.raises(AdmissionTimeout):
        gate.acquire(interval=0.2, budget=20)
    assert timer.waits == []
    timer.now += 30
    gate.acquire(interval=0.2, budget=20)


def test_concurrent_requests_are_spaced_and_share_429_cooldown():
    gate = RequestGate()
    barrier = Barrier(4)
    starts = []
    lock = Lock()

    def request():
        barrier.wait(timeout=5)
        gate.acquire(interval=0.02, budget=3)
        with lock:
            starts.append(monotonic())

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: request(), range(4)))
    # Verify admissions directly rather than sleeping in each network mock.
    starts.sort()
    assert all(b - a >= 0.015 for a, b in zip(starts, starts[1:]))
    gate.defer(1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(gate.acquire, interval=0.02, budget=0.1) for _ in range(2)]
        for future in futures:
            with pytest.raises(AdmissionTimeout):
                future.result()


@pytest.fixture
def timed_runtime(monkeypatch, settings):
    timer = FakeTime()
    monkeypatch.setattr(tool_retry, "monotonic", timer.clock)
    monkeypatch.setattr(tool_retry, "sleep", timer.sleep)
    monkeypatch.setattr(amap, "request_gate", RequestGate(clock=timer.clock, sleeper=timer.sleep))
    monkeypatch.setattr(settings, "amap_request_interval_seconds", 0.2)
    monkeypatch.setattr(settings, "amap_rate_limit_cooldown_seconds", 1.0)
    monkeypatch.setattr(settings, "tool_max_attempts", 2)
    monkeypatch.setattr(settings, "tool_retry_budget_seconds", 20.0)
    monkeypatch.setattr(settings, "tool_retry_base_delay", 0.25)
    return timer


def response(status=200, *, retry_after=None, payload=None):
    return httpx.Response(status, json=payload or {"infocode": "10000", "pois": []},
                          headers={} if retry_after is None else {"Retry-After": retry_after},
                          request=httpx.Request("GET", "https://example.invalid"))


def context():
    return tools.ToolContext(city="杭州", adcode="330100", start_date="2026-10-10", travel_days=1)


@pytest.mark.parametrize(("header", "delay"), [("3", 3.0), ("0", 1.0), (None, 1.0), ("bad", 1.0)])
def test_429_retries_after_header_or_fallback_and_preserves_metadata(monkeypatch, timed_runtime, header, delay):
    timer = timed_runtime
    sends = []

    def get(*_a, **_kw):
        sends.append(timer.now)
        return response(429, retry_after=header) if len(sends) == 1 else response()

    monkeypatch.setattr(httpx, "get", get)
    ctx = context()
    result = tools.execute("attractions", "search_attractions", {"keyword": "博物馆"}, ctx)
    assert result.status == "ok" and sends == [100.0, 100.0 + delay]
    first = ctx.calls[0]["attempts"][0]["outcome"]
    assert first["error_code"] == "UPSTREAM_RATE_LIMITED" and first["retry_after_seconds"] == delay
    assert json.loads(ToolResult(**first).to_model_content()) == first


def test_very_long_retry_after_stops_without_wait_or_second_http(monkeypatch, timed_runtime):
    timer = timed_runtime
    sends = []

    def get(*_a, **_kw):
        sends.append(timer.now)
        return response(429, retry_after="120")

    monkeypatch.setattr(httpx, "get", get)
    ctx = context()
    result = tools.execute("attractions", "search_attractions", {"keyword": "景点"}, ctx)
    assert result.retry_after_seconds == 120 and result.retryable
    assert ctx.calls[0]["stop_reason"] == "time_budget"
    assert sends == [100.0] and timer.waits == []
    # A different keyword or request cannot bypass the shared cooldown.
    other = tools.execute("weather", "get_weather", {}, context())
    assert other.error_code == "UPSTREAM_RATE_LIMITED" and len(sends) == 1
    assert timer.waits == [] and other.retry_after_seconds == 120


def test_header_http_date_reaches_adapter_and_tool(monkeypatch, timed_runtime):
    # Use a frozen parser wall clock; admission and retry use monotonic time.
    from app.services.request_gate import parse_retry_after as actual_parse
    monkeypatch.setattr(amap, "parse_retry_after", lambda value: actual_parse(value, now=NOW))
    sends = []

    def get(*_a, **_kw):
        sends.append(timed_runtime.now)
        return response(429, retry_after=format_datetime(NOW + timedelta(seconds=5), usegmt=True)) if len(sends) == 1 else response()

    monkeypatch.setattr(httpx, "get", get)
    result = tools.execute("hotels", "search_hotels", {}, context())
    assert result.status == "ok" and sends == [100.0, 105.0]


def test_direct_weather_waits_for_other_requests_429(monkeypatch, timed_runtime):
    sends = []

    def get(url, **_kw):
        sends.append((url, timed_runtime.now))
        return response(429, retry_after="2") if len(sends) == 1 else response()

    monkeypatch.setattr(httpx, "get", get)
    with pytest.raises(amap.AmapUnreachable):
        amap.search_poi("博物馆", "杭州")
    amap.get_weather("330100")
    assert [item[1] for item in sends] == [100.0, 102.0]


def test_parallel_search_branches_share_real_429_cooldown(monkeypatch, settings):
    gate = RequestGate()
    cooled = Event()
    sends = []
    lock = Lock()
    monkeypatch.setattr(amap, "request_gate", gate)
    monkeypatch.setattr(settings, "amap_request_interval_seconds", 0.01)
    monkeypatch.setattr(settings, "amap_rate_limit_cooldown_seconds", 0.05)
    monkeypatch.setattr(settings, "tool_retry_base_delay", 0.0)
    actual_defer = gate.defer

    def defer(seconds):
        actual_defer(seconds)
        cooled.set()

    monkeypatch.setattr(gate, "defer", defer)
    actual_search = amap.search_poi

    def search(keyword, *args, **kwargs):
        if keyword == "second":
            assert cooled.wait(timeout=5)
        return actual_search(keyword, *args, **kwargs)

    def get(*_a, **kwargs):
        with lock:
            sends.append((kwargs["params"]["keywords"], monotonic()))
            number = len(sends)
        return response(429) if number == 1 else response()

    monkeypatch.setattr(amap, "search_poi", search)
    monkeypatch.setattr(httpx, "get", get)
    ctx = context()
    results = execute_batch("attractions", [({"id": word}, "search_attractions", {"keyword": word})
                                             for word in ("first", "second")], ctx)
    assert all(item.status == "ok" for item in results)
    assert len(sends) == 3 and all(t - sends[0][1] >= 0.045 for _, t in sends[1:])
    assert [call["attempt_count"] for call in ctx.calls] == [2, 1]


def test_503_retry_after_is_also_honored(monkeypatch, timed_runtime):
    sends = []

    def get(*_a, **_kw):
        sends.append(timed_runtime.now)
        return response(503, retry_after="2") if len(sends) == 1 else response()

    monkeypatch.setattr(httpx, "get", get)
    assert tools.execute("hotels", "search_hotels", {}, context()).status == "ok"
    assert sends == [100.0, 102.0]


@pytest.mark.parametrize("values", [
    {"amap_request_interval_seconds": 0}, {"amap_request_interval_seconds": 11},
    {"amap_rate_limit_cooldown_seconds": 0}, {"amap_rate_limit_cooldown_seconds": 61},
    {"amap_request_interval_seconds": float("nan")},
    {"amap_rate_limit_cooldown_seconds": float("inf")},
])
def test_rate_settings_are_bounded(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)
