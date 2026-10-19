"""Verify the shared result contract through tool handlers, scheduling and model messages."""
import json
import threading

import httpx
import pytest

from app.agents import react_loop
from app.services import amap, tools
from app.services.tool_result import ToolResult, next_action
from app.services.tool_scheduler import execute_batch
from tests.factories import make_attraction, make_hotel, make_weather


@pytest.fixture(autouse=True)
def single_attempt_contract_tests(monkeypatch, settings):
    """These cases isolate classification; retries have separate runtime tests."""
    monkeypatch.setattr(settings, "tool_max_attempts", 1)


def context():
    ctx = tools.ToolContext(city="杭州", adcode="330100", start_date="2026-10-10", travel_days=1, nights=1)
    ctx.attractions["lake"] = make_attraction("西湖", poi_id="lake", required=True)
    ctx.hotels["hotel"] = make_hotel(nights=1)
    return ctx


def batch(name, args=None, identifier="call"):
    return [({"id": identifier}, name, args or {})]


def check_contract(result):
    assert isinstance(result, ToolResult)
    payload = json.loads(result.to_model_content())
    assert set(payload) == {"status", "message", "error_code", "retryable", "stats", "retry_after_seconds"}
    assert payload == result.to_dict()
    assert isinstance(payload["message"], str) and isinstance(payload["retryable"], bool)
    if result.status == "ok":
        assert result.error_code is None and result.retryable is False
    else:
        assert isinstance(result.error_code, str) and result.error_code


@pytest.mark.parametrize(("role", "name", "args"), [
    ("attractions", "search_attractions", {"keyword": "博物馆"}),
    ("weather", "get_weather", {}),
    ("hotels", "search_hotels", {}),
    ("hotels", "rank_hotels", {}),
    ("hotels", "measure_distance", {"hotel": "测试酒店", "attraction": "西湖"}),
    ("hotels", "choose_hotel", {"hotel": "测试酒店", "price_per_night": 200}),
])
def test_every_tool_returns_same_contract_on_success_and_precondition_failure(monkeypatch, role, name, args):
    monkeypatch.setattr(amap, "search_poi", lambda *_a, **_kw: [
        make_attraction("博物馆"), make_attraction("新酒店", typecode="100105"),
    ])
    monkeypatch.setattr(amap, "get_weather", lambda *_a: [make_weather("2026-10-10")])
    monkeypatch.setattr(amap, "measure_distance", lambda *_a: 2000)
    successful = tools.execute(role, name, args, context())
    check_contract(successful)
    assert successful.status == "ok"
    ctx = tools.ToolContext(city="杭州", adcode=None, start_date="2026-10-10", travel_days=1)
    if name in ("search_hotels", "search_attractions"):
        monkeypatch.setattr(amap, "search_poi", lambda *_a, **_kw: (_ for _ in ()).throw(
            amap.AmapError("AMAP_KEY_INVALID", "private detail")
        ))
    failed = tools.execute(role, name, args, ctx)
    check_contract(failed)
    assert failed.status == "error" and failed.retryable is False


@pytest.mark.parametrize(("role", "name", "args", "upstream"), [
    ("attractions", "search_attractions", {"keyword": "景点"}, "search_poi"),
    ("hotels", "search_hotels", {}, "search_poi"),
    ("weather", "get_weather", {}, "get_weather"),
    ("hotels", "measure_distance", {"hotel": "测试酒店", "attraction": "西湖"}, "measure_distance"),
])
@pytest.mark.parametrize("failure", ["timeout", "key", "quota"])
def test_typed_upstream_error_metadata_survives_handler_catch(monkeypatch, role, name, args, upstream, failure):
    calls = []
    ctx = context()

    def fail(*_a, **_kw):
        calls.append(True)
        if failure == "timeout":
            raise amap.AmapUnreachable("private upstream detail")
        code = "AMAP_KEY_INVALID" if failure == "key" else "AMAP_QUOTA_EXCEEDED"
        raise amap.AmapError(code, "private upstream detail")

    monkeypatch.setattr(amap, upstream, fail)
    results = execute_batch(role, batch(name, args), ctx)
    result = results[0]
    check_contract(result)
    assert result.status == "error" and result.retryable is (failure == "timeout")
    assert result.error_code == {"timeout": "UPSTREAM_TIMEOUT", "key": "AMAP_KEY_INVALID",
                                 "quota": "AMAP_QUOTA_EXCEEDED"}[failure]
    assert "private upstream detail" not in result.message
    assert "private upstream detail" in ctx.errors[0]
    assert ctx.calls[0]["outcome"] == result.to_dict()
    expected = "report_or_change_query" if failure == "timeout" else "report_or_correct"
    assert ctx.calls[0]["next_action"] == expected
    assert calls == [True]


@pytest.mark.parametrize(("failure", "expected_code", "retryable"), [
    ("timeout", "UPSTREAM_TIMEOUT", True),
    ("connection", "UPSTREAM_UNAVAILABLE", True),
    ("bad-json", "UPSTREAM_INVALID_RESPONSE", False),
    (400, "UPSTREAM_HTTP_ERROR", False),
    (403, "UPSTREAM_HTTP_ERROR", False),
    (429, "UPSTREAM_RATE_LIMITED", True),
    (503, "UPSTREAM_HTTP_ERROR", True),
])
def test_http_error_classification_does_not_treat_all_failures_as_retryable(monkeypatch, failure, expected_code, retryable):
    def response(*_a, **_kw):
        request = httpx.Request("GET", "https://example.invalid/mock")
        if failure == "timeout":
            raise httpx.ReadTimeout("mock timeout", request=request)
        if failure == "connection":
            raise httpx.ConnectError("mock disconnected", request=request)
        if failure == "bad-json":
            return httpx.Response(200, text="not-json", request=request)
        return httpx.Response(failure, json={}, request=request)

    monkeypatch.setattr(httpx, "get", response)
    result = tools.execute("hotels", "search_hotels", {}, context())
    check_contract(result)
    assert result.error_code == expected_code and result.retryable is retryable


@pytest.mark.parametrize("role", ["attractions", "hotels"])
@pytest.mark.parametrize("kind", ["empty", "filtered", "duplicate"])
def test_search_counts_distinguish_zero_results_filtering_and_duplicates(monkeypatch, role, kind):
    ctx = context()
    if role == "hotels":
        existing = make_attraction("测试酒店", typecode="100105", poi_id="hotel")
        rejected = make_attraction("咖啡店", typecode="050000")
    else:
        existing = ctx.attractions["lake"]
        rejected = make_attraction("旅馆", typecode="100105")
    hits = {"empty": [], "filtered": [rejected], "duplicate": [existing]}[kind]
    monkeypatch.setattr(amap, "search_poi", lambda *_a, **_kw: hits)
    name = "search_hotels" if role == "hotels" else "search_attractions"
    result = tools.execute(role, name, {"keyword": "测试"}, ctx)
    check_contract(result)
    assert result.status == "ok" and result.retryable is False
    expected = {"returned_count": len(hits), "filtered_count": int(kind == "filtered"),
                "duplicate_count": int(kind == "duplicate"), "added_count": 0,
                "candidate_count": 1}
    assert {key: result.stats[key] for key in expected} == expected
    assert result.stats["local_count"] == 0 and result.stats["upstream_calls"] == 1
    action = "review_query_if_needed" if kind == "empty" else "check_candidate_coverage"
    assert next_action(result) == action


def test_parallel_merge_adjusts_duplicate_counts_without_losing_failure_metadata(monkeypatch):
    ctx = context()
    ctx.hotels.clear()
    barrier = threading.Barrier(2)

    def search(keyword, *_a, **_kw):
        barrier.wait(timeout=5)
        return [make_attraction("共同酒店", typecode="100105", poi_id="common"),
                make_attraction(keyword, typecode="100105", poi_id=keyword)]

    monkeypatch.setattr(amap, "search_poi", search)
    pending = batch("search_hotels", {"keyword": "first"}, "first") + batch(
        "search_hotels", {"keyword": "second"}, "second"
    )
    results = execute_batch("hotels", pending, ctx)
    assert [r.stats["added_count"] for r in results] == [2, 1]
    assert [r.stats["duplicate_count"] for r in results] == [0, 1]
    assert [r.stats["candidate_count"] for r in results] == [3, 3]
    for result, trace in zip(results, ctx.calls):
        check_contract(result)
        stats = result.stats
        assert stats["returned_count"] == stats["added_count"] + stats["filtered_count"] + stats["duplicate_count"]
        assert trace["outcome"] == result.to_dict()


@pytest.mark.parametrize("kind", ["unknown", "forbidden", "invalid-return", "exception"])
def test_boundary_failures_share_contract_and_never_slice_non_string_results(monkeypatch, kind):
    ctx = context()
    name = "test_tool"
    expected = {"unknown": "TOOL_NOT_FOUND", "forbidden": "TOOL_FORBIDDEN",
                "invalid-return": "TOOL_RESULT_INVALID", "exception": "TOOL_ERROR"}[kind]
    if kind != "unknown":
        def handler(_args, _ctx):
            if kind == "exception":
                raise RuntimeError("private debug detail")
            return "legacy string"
        monkeypatch.setitem(tools.TOOLS, name, {"roles": ("weather",) if kind == "forbidden" else ("hotels",),
                                               "handler": handler, "parameters": {"type": "object"}})
    result = execute_batch("hotels", batch(name), ctx)[0]
    check_contract(result)
    assert result.error_code == expected and result.retryable is False
    assert "private debug detail" not in result.message


def test_dependency_and_conflict_are_typed_errors_instead_of_bare_strings(monkeypatch):
    ctx = context()
    monkeypatch.setattr(amap, "search_poi", lambda *_a, **_kw: [])
    result = execute_batch("hotels", batch("search_hotels") + batch(
        "choose_hotel", {"hotel": "测试酒店", "price_per_night": 200}, "choose"
    ), ctx)[1]
    check_contract(result)
    assert result.error_code == "TOOL_DEPENDENCY" and next_action(result) == "wait_next_turn"
    choices = batch("choose_hotel", {"hotel": "测试酒店", "price_per_night": 200}, "one") + batch(
        "choose_hotel", {"hotel": "测试酒店", "price_per_night": 200}, "two"
    )
    assert all(r.error_code == "TOOL_CONFLICT" for r in execute_batch("hotels", choices, ctx))


def test_runtime_routing_uses_fields_instead_of_message_keywords():
    assert next_action(ToolResult.success("文字里有失败，不代表执行失败")) == "continue"
    assert next_action(ToolResult.failure("文字里有成功，但实际超时", "UPSTREAM_TIMEOUT",
                                         retryable=True)) == "retry_if_budget_allows"


def test_real_react_loop_serializes_both_success_and_failure_into_string_messages(monkeypatch):
    ctx = context()
    monkeypatch.setattr(amap, "search_poi", lambda *_a, **_kw: [])
    turn = 0

    def model(messages, _schemas):
        nonlocal turn
        turn += 1
        if turn == 1:
            return {"tool_calls": [
                {"id": "good", "function": {"name": "search_attractions", "arguments": '{"keyword":"景点"}'}},
                {"id": "bad", "function": {"name": "unknown_tool", "arguments": "{}"}},
            ]}, {"llm_calls": 1}
        tool_messages = [message for message in messages if message["role"] == "tool"]
        assert [m["tool_call_id"] for m in tool_messages] == ["good", "bad"]
        assert all(isinstance(m["content"], str) for m in tool_messages)
        contents = [json.loads(m["content"]) for m in tool_messages]
        assert [p["status"] for p in contents] == ["ok", "error"]
        assert set(contents[0]) == set(contents[1])
        assert "attractions" not in contents[0]
        return {"content": "结束"}, {"llm_calls": 1}

    monkeypatch.setattr(react_loop, "chat_with_tools", model)
    result = react_loop.run_agent("attractions", "system", "user", ctx)
    assert result.ok and result.usage["llm_calls"] == 2
