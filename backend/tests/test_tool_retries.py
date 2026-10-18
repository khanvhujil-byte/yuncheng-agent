"""Retry actual tool boundaries, parallel merging, model usage and persistence."""
import json
import threading

import httpx
import pytest
from pydantic import ValidationError

from app.agents import react_loop, weather_agent
from app.config import Settings
from app.services import amap, store, tool_retry, tools
from app.services.tool_result import ToolResult
from app.services.tool_scheduler import execute_batch
from tests.factories import make_attraction, make_hotel, make_request, make_weather


@pytest.fixture(autouse=True)
def retry_defaults(monkeypatch, settings):
    monkeypatch.setattr(settings, "tool_max_attempts", 2)
    monkeypatch.setattr(settings, "tool_retry_budget_seconds", 20.0)
    monkeypatch.setattr(settings, "tool_retry_base_delay", 0.25)
    monkeypatch.setattr(tool_retry, "sleep", lambda _: None)


def context():
    return tools.ToolContext(city="杭州", adcode="330100", start_date="2026-10-10", travel_days=1, nights=1)


def pending(keyword, identifier=None):
    return ({"id": identifier or keyword}, "search_attractions", {"keyword": keyword})


def test_transient_failure_then_success_keeps_history_and_no_extra_model_call(monkeypatch):
    ctx = context()
    calls = []

    def search(*_a, **_kw):
        calls.append(True)
        if len(calls) == 1:
            raise amap.AmapUnreachable("first failure detail")
        return [make_attraction("西湖", poi_id="lake")]

    turns = []

    def model(messages, _schemas):
        turns.append(True)
        if len(turns) == 1:
            return {"tool_calls": [{"id": "search", "function": {
                "name": "search_attractions", "arguments": '{"keyword":"自然风光"}'}}]}, {"llm_calls": 1}
        assert json.loads(messages[-1]["content"])["status"] == "ok"
        return {"content": "完成"}, {"llm_calls": 1}

    monkeypatch.setattr(amap, "search_poi", search)
    monkeypatch.setattr(react_loop, "chat_with_tools", model)
    result = react_loop.run_agent("attractions", "system", "user", ctx)
    assert result.ok and result.usage["llm_calls"] == 2 and len(calls) == 2
    assert ctx.attractions["lake"].name == "西湖" and len(ctx.errors) == 1
    assert len(ctx.calls) == 1
    trace = ctx.calls[0]
    assert trace["attempt_count"] == 2 and trace["stop_reason"] == "success"
    assert [a["outcome"]["status"] for a in trace["attempts"]] == ["error", "ok"]
    assert trace["outcome"]["status"] == "ok" and trace["next_action"] == "continue"


def test_persistent_failure_cannot_reset_budget_by_repeating_query(monkeypatch):
    ctx = context()
    calls = []

    def fail(*_a, **_kw):
        calls.append(True)
        raise amap.AmapUnreachable(f"failure {len(calls)}")

    monkeypatch.setattr(amap, "search_poi", fail)
    result = execute_batch("attractions", [pending("风光")], ctx)[0]
    assert len(calls) == 2 and result.retryable and result.error_code == "UPSTREAM_TIMEOUT"
    assert "failure 2" in ctx.errors[-1]
    assert ctx.calls[-1]["stop_reason"] == "attempt_limit"
    assert ctx.calls[-1]["next_action"] == "report_or_change_query"
    again = tools.execute("attractions", "search_attractions", {"keyword": "风光"}, ctx)
    assert again == result and len(calls) == 2
    assert ctx.calls[-1]["stop_reason"] == "query_exhausted" and ctx.calls[-1]["attempt_count"] == 0
    tools.execute("attractions", "search_attractions", {"keyword": "博物馆"}, ctx)
    assert len(calls) == 4  # A changed query gets its own bounded allowance.
    tools.execute("attractions", "search_attractions", {"keyword": "风光"}, context())
    assert len(calls) == 6  # Exhaustion is local, not a process-wide outage cache.


@pytest.mark.parametrize("failure", ["key", "quota", "empty", "duplicate", "invalid-json", 400, 403])
def test_non_transient_or_successful_queries_do_not_retry(monkeypatch, failure):
    ctx = context()
    ctx.attractions["lake"] = make_attraction("西湖", poi_id="lake")
    calls = []

    def response(*_a, **_kw):
        calls.append(True)
        request = httpx.Request("GET", "https://example.invalid")
        if isinstance(failure, int):
            return httpx.Response(failure, json={}, request=request)
        if failure == "invalid-json":
            return httpx.Response(200, text="invalid", request=request)
        if failure in ("key", "quota"):
            return httpx.Response(200, json={"infocode": "10001" if failure == "key" else "10003"}, request=request)
        return httpx.Response(200, json={"infocode": "10000", "pois": []}, request=request)

    if failure == "duplicate":
        def search(*_a, **_kw):
            calls.append(True)
            return [ctx.attractions["lake"]]
        monkeypatch.setattr(amap, "search_poi", search)
    else:
        monkeypatch.setattr(httpx, "get", response)
    result = tools.execute("attractions", "search_attractions", {"keyword": "自然风光"}, ctx)
    assert len(calls) == 1 and ctx.calls[0]["attempt_count"] == 1
    assert result.status == ("ok" if failure in ("empty", "duplicate") else "error")
    assert not result.retryable and not ctx.exhausted_queries


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_http_status_retries_and_recovers(monkeypatch, status):
    ctx = context()
    calls = []

    def response(*_a, **_kw):
        calls.append(True)
        request = httpx.Request("GET", "https://example.invalid")
        return httpx.Response(status if len(calls) == 1 else 200,
                              json={"infocode": "10000", "pois": []}, request=request)

    monkeypatch.setattr(httpx, "get", response)
    result = tools.execute("attractions", "search_attractions", {"keyword": "风光"}, ctx)
    assert result.status == "ok" and len(calls) == 2
    assert ctx.calls[0]["attempts"][0]["outcome"]["error_code"] == (
        "UPSTREAM_RATE_LIMITED" if status == 429 else "UPSTREAM_HTTP_ERROR")


@pytest.mark.parametrize("name", ["choose_hotel", "rank_hotels", "new_mutating_tool"])
def test_tools_without_explicit_read_only_permission_never_retry(monkeypatch, name):
    ctx = context()
    calls = []

    def mutate(_args, _ctx):
        calls.append(True)
        return ToolResult.failure("uncertain mutation", "UPSTREAM_TIMEOUT", retryable=True)

    monkeypatch.setitem(tools.TOOLS, name, {"roles": ("hotels",), "handler": mutate, "parameters": {"type": "object"}})
    tools.execute("hotels", name, {}, ctx)
    assert len(calls) == 1 and ctx.calls[-1]["stop_reason"] == "unsafe_tool"


@pytest.mark.parametrize("elapsed", [19.9, 21.0])
def test_time_budget_stops_before_wait_or_new_attempt(monkeypatch, elapsed):
    now = [100.0]
    waits = []
    calls = []
    monkeypatch.setattr(tool_retry, "monotonic", lambda: now[0])
    monkeypatch.setattr(tool_retry, "sleep", waits.append)

    def attempt():
        calls.append(True)
        now[0] += elapsed
        return ToolResult.failure("timeout", "UPSTREAM_TIMEOUT", retryable=True)

    report = tool_retry.run(attempt, safe=True, policy=tool_retry.RetryPolicy())
    assert report.stop_reason == "time_budget" and len(calls) == 1 and not waits
    assert tool_retry.deadline.get() is None


def test_delay_is_exponential_and_never_after_last_attempt(monkeypatch):
    waits = []
    monkeypatch.setattr(tool_retry, "sleep", waits.append)
    report = tool_retry.run(lambda: ToolResult.failure("timeout", "UPSTREAM_TIMEOUT", retryable=True),
                            safe=True, policy=tool_retry.RetryPolicy(max_attempts=3))
    assert waits == [0.25, 0.5] and len(report.attempts) == 3


def test_deadline_rechecked_after_wait_and_context_restored_on_exception(monkeypatch):
    now = [0.0]
    calls = []
    monkeypatch.setattr(tool_retry, "monotonic", lambda: now[0])
    monkeypatch.setattr(tool_retry, "sleep", lambda _: now.__setitem__(0, 21.0))

    def attempt():
        calls.append(True)
        return ToolResult.failure("timeout", "UPSTREAM_TIMEOUT", retryable=True)

    report = tool_retry.run(attempt, safe=True, policy=tool_retry.RetryPolicy())
    assert report.stop_reason == "time_budget" and len(calls) == 1
    with pytest.raises(RuntimeError):
        tool_retry.run(lambda: (_ for _ in ()).throw(RuntimeError("unexpected")),
                       safe=True, policy=tool_retry.RetryPolicy())
    assert tool_retry.deadline.get() is None


def test_adapter_caps_timeout_and_refuses_expired_deadline(monkeypatch, settings):
    now = [100.0]
    timeouts = []
    monkeypatch.setattr(tool_retry, "monotonic", lambda: now[0])

    def response(_url, *, params, timeout):
        timeouts.append(timeout)
        return httpx.Response(200, json={"infocode": "10000"}, request=httpx.Request("GET", "https://example.invalid"))

    monkeypatch.setattr(httpx, "get", response)
    token = tool_retry.deadline.set(103.0)
    try:
        amap._get("https://example.invalid", {})
        now[0] = 104.0
        with pytest.raises(amap.AmapUnreachable):
            amap._get("https://example.invalid", {})
    finally:
        tool_retry.deadline.reset(token)
    amap._get("https://example.invalid", {})
    assert timeouts == [min(settings.amap_timeout, 3.0), settings.amap_timeout]


@pytest.mark.parametrize("role", ["attractions", "hotels"])
def test_parallel_retries_preserve_success_errors_and_exhaustion_cache(monkeypatch, role):
    ctx = context()
    barrier = threading.Barrier(2)
    counts = {"good": 0, "bad": 0}

    def search(keyword, *_a, **_kw):
        counts[keyword] += 1
        assert tool_retry.remaining_seconds() is not None
        if counts[keyword] == 1:
            barrier.wait(timeout=5)
        if keyword == "bad" or counts[keyword] == 1:
            raise amap.AmapUnreachable(keyword + " temporary")
        return [make_attraction("西湖", poi_id="lake", typecode="100105" if role == "hotels" else "110202")]

    monkeypatch.setattr(amap, "search_poi", search)
    name = "search_hotels" if role == "hotels" else "search_attractions"
    batch = [({"id": keyword}, name, {"keyword": keyword}) for keyword in ("good", "bad")]
    results = execute_batch(role, batch, ctx)
    assert counts == {"good": 2, "bad": 2}
    assert [r.status for r in results] == ["ok", "error"]
    pool = ctx.hotels if role == "hotels" else ctx.attractions
    assert pool["lake"].name == "西湖" and len(ctx.errors) == 3
    assert [c["tool_call_id"] for c in ctx.calls] == ["good", "bad"]
    assert [c["attempt_count"] for c in ctx.calls] == [2, 2]
    execute_batch(role, [({"id": "again"}, name, {"keyword": "bad"})], ctx)
    assert counts["bad"] == 2 and ctx.calls[-1]["attempt_count"] == 0
    assert tool_retry.deadline.get() is None


def test_identical_calls_in_one_batch_share_exhaustion_limit(monkeypatch):
    ctx = context()
    calls = []

    def fail(*_a, **_kw):
        calls.append(True)
        raise amap.AmapUnreachable("offline")

    monkeypatch.setattr(amap, "search_poi", fail)
    execute_batch("attractions", [pending("风光", "first"), pending("风光", "second")], ctx)
    assert len(calls) == 2
    assert [c["attempt_count"] for c in ctx.calls] == [2, 0]


def test_weather_tool_only_recovers_and_trace_survives_journal(monkeypatch, settings):
    calls = []
    monkeypatch.setattr(settings, "weather_use_agent", False)

    def weather(_adcode):
        calls.append(True)
        if len(calls) == 1:
            raise amap.AmapUnreachable("first weather failure")
        return [make_weather("2026-10-10")]

    monkeypatch.setattr(amap, "get_weather", weather)
    request = make_request(travel_days=1, nights=0, start_date="2026-10-10")
    update = weather_agent.collect_weather({"request": request, "travel_days": 1, "nights": 0, "adcode": "330100"})
    assert update["sources"]["weather"] == "amap" and not update["warnings"]
    assert len(update["errors"]) == 1 and update["usage"]["llm_calls"] == 0
    assert update["tool_calls"][0]["attempt_count"] == 2
    store.save_run("retry-journal", request)
    store.journal_node("retry-journal", "collect_weather", 1, update)
    loaded = store.load_journal("retry-journal")
    assert loaded[0]["update"]["tool_calls"] == update["tool_calls"]


def test_distance_query_retries_but_missing_candidate_can_be_corrected(monkeypatch):
    ctx = context()
    args = {"hotel": "测试酒店", "attraction": "西湖"}
    assert tools.execute("hotels", "measure_distance", args, ctx).error_code == "CANDIDATE_NOT_FOUND"
    ctx.hotels["hotel"] = make_hotel()
    ctx.attractions["lake"] = make_attraction("西湖")
    calls = []

    def distance(*_a):
        calls.append(True)
        if len(calls) == 1:
            raise amap.AmapUnreachable("first distance failure")
        return 2000

    monkeypatch.setattr(amap, "measure_distance", distance)
    assert tools.execute("hotels", "measure_distance", args, ctx).status == "ok"
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["timeout", "key"])
def test_weather_final_failure_reports_actual_reason(monkeypatch, settings, failure):
    monkeypatch.setattr(settings, "weather_use_agent", False)

    def fail(_adcode):
        if failure == "key":
            raise amap.AmapError("AMAP_KEY_INVALID", "invalid key detail")
        raise amap.AmapUnreachable("last timeout detail")

    monkeypatch.setattr(amap, "get_weather", fail)
    update = weather_agent.collect_weather({"request": make_request(), "travel_days": 1,
                                            "nights": 0, "adcode": "330100"})
    assert update["sources"]["weather"] == "degraded" and not update["weather"]
    assert "缺少城市编码" not in update["warnings"][0]
    assert update["tool_calls"][0]["attempt_count"] == (2 if failure == "timeout" else 1)
    if failure == "key":
        assert "AMAP_KEY_INVALID" in update["warnings"][0]
    else:
        assert "已达尝试次数上限" in update["warnings"][0]


def test_deferred_call_has_zero_attempts(monkeypatch):
    ctx = context()
    monkeypatch.setattr(amap, "search_poi", lambda *_a, **_kw: [])
    results = execute_batch("hotels", [({"id": "search"}, "search_hotels", {}),
                                      ({"id": "choose"}, "choose_hotel", {})], ctx)
    assert results[1].error_code == "TOOL_DEPENDENCY"
    assert ctx.calls[-1]["attempt_count"] == 0 and ctx.calls[-1]["stop_reason"] == "not_executed"


@pytest.mark.parametrize("values", [
    {"tool_max_attempts": 0}, {"tool_max_attempts": 4}, {"tool_retry_budget_seconds": 0},
    {"tool_retry_budget_seconds": 61}, {"tool_retry_base_delay": -1}, {"tool_retry_base_delay": 3},
    {"tool_retry_budget_seconds": float("nan")}, {"tool_retry_base_delay": float("inf")},
])
def test_retry_configuration_is_bounded(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)
