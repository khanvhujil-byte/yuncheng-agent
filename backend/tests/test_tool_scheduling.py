"""Real tool handlers and ReAct messages, with deterministic scheduling failures."""
import json
import threading

import pytest

from app.agents import react_loop
from app.services import amap, tools
from app.services.tool_scheduler import execute_batch
from app.services.tools import ToolContext, execute
from app.services.tool_result import ToolResult
from tests.factories import make_attraction, make_hotel


@pytest.fixture(autouse=True)
def single_attempt_scheduling_tests(monkeypatch, settings):
    """Keep barrier tests about scheduling, independent of retry policy."""
    monkeypatch.setattr(settings, "tool_max_attempts", 1)


def context():
    ctx = ToolContext(city="杭州", adcode="330100", start_date="2026-10-10", travel_days=1, nights=1)
    ctx.attractions["lake"] = make_attraction("西湖", required=True, poi_id="lake")
    return ctx


def call(identifier, name, **args):
    return {"id": identifier, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def batch(*calls):
    return [(item, item["function"]["name"], json.loads(item["function"]["arguments"])) for item in calls]


def model_script(monkeypatch, turns):
    iterator = iter(turns)

    def model(messages, _tools):
        turn = next(iterator)
        if callable(turn):
            turn = turn(messages)
        return turn, {"llm_calls": 1, "prompt_tokens": 11}

    monkeypatch.setattr(react_loop, "chat_with_tools", model)


def test_reverse_order_search_and_rank_is_fixed_but_message_ids_stay_aligned(monkeypatch):
    ctx = context()
    searched = threading.Event()

    def search(*_args, **_kwargs):
        searched.set()
        return [make_attraction("近湖酒店", typecode="100105", poi_id="hotel")]

    actual_rank = tools.TOOLS["rank_hotels"]["handler"]

    def rank(args, local):
        assert searched.is_set() and len(local.hotels) == 1
        return actual_rank(args, local)

    monkeypatch.setattr(amap, "search_poi", search)
    monkeypatch.setitem(tools.TOOLS["rank_hotels"], "handler", rank)
    rank_call = call("rank", "rank_hotels")
    search_call = call("search", "search_hotels", keyword="酒店")

    def inspect(messages):
        assert messages[-3]["tool_calls"] == [rank_call, search_call]
        assert [(m["tool_call_id"], m["role"]) for m in messages[-2:]] == [
            ("rank", "tool"), ("search", "tool"),
        ]
        assert "近湖酒店" in messages[-2]["content"]
        assert "累计候选 1" in messages[-1]["content"]
        return {"content": "已获得结果"}

    model_script(monkeypatch, [{"tool_calls": [rank_call, search_call]}, inspect])
    result = react_loop.run_agent("hotels", "system", "user", ctx)
    assert result.steps == 2 and result.usage["llm_calls"] == 2
    assert ctx.ranked == ["近湖酒店"]
    assert [c["tool_call_id"] for c in ctx.calls] == ["search", "rank"]


@pytest.mark.parametrize("failure", ["empty", "network", "quota"])
def test_failed_search_cannot_produce_a_successful_ranking(monkeypatch, failure):
    ctx = context()

    def search(*_args, **_kwargs):
        if failure == "network":
            raise amap.AmapUnreachable("offline")
        if failure == "quota":
            raise amap.AmapError("QUOTA_EXCEEDED", "quota reached")
        return []

    monkeypatch.setattr(amap, "search_poi", search)
    results = execute_batch("hotels", batch(
        call("rank", "rank_hotels"), call("search", "search_hotels"),
    ), ctx)
    assert "还没有酒店候选" in results[0].message
    assert ctx.ranked == [] and ctx.chosen_hotel is None and ctx.hotels == {}
    assert "搜索酒店失败" in results[1].message if failure != "empty" else "累计候选 0" in results[1].message
    if failure != "empty":
        assert len(ctx.errors) == 1


def test_fresh_results_require_another_model_turn_before_dependent_decisions(monkeypatch):
    ctx = context()
    distance_calls = []
    monkeypatch.setattr(amap, "search_poi", lambda *_a, **_kw: [
        make_attraction("近湖酒店", typecode="100105", poi_id="hotel"),
    ])

    def measure(*_args):
        distance_calls.append(True)
        return 2300

    monkeypatch.setattr(amap, "measure_distance", measure)
    early_choice = call("early-choice", "choose_hotel", hotel="近湖酒店", price_per_night=200,
                        reason="尚未看到结果")
    early_distance = call("early-distance", "measure_distance", hotel="近湖酒店", attraction="西湖")

    def after_search(messages):
        assert ctx.chosen_hotel is None and distance_calls == []
        assert ctx.ranked == ["近湖酒店"]
        responses = {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}
        assert "下一轮" in responses["early-choice"]
        assert "下一轮" in responses["early-distance"]
        return {"tool_calls": [
            call("measure", "measure_distance", hotel="近湖酒店", attraction="西湖"),
            call("still-early", "choose_hotel", hotel="近湖酒店", price_per_night=200),
        ]}

    def after_distance(messages):
        assert ctx.chosen_hotel is None and distance_calls == [True]
        assert "2.3 公里" in messages[-2]["content"]
        assert "下一轮" in messages[-1]["content"]
        return {"tool_calls": [call("final-choice", "choose_hotel", hotel="近湖酒店",
                                    price_per_night=200, reason="已实测到西湖驾车距离 2.3 公里") ]}

    model_script(monkeypatch, [
        {"tool_calls": [early_choice, early_distance, call("rank", "rank_hotels"),
                        call("search", "search_hotels")]},
        after_search, after_distance, {"content": "已选定近湖酒店"},
    ])
    result = react_loop.run_agent("hotels", "system", "user", ctx, max_steps=5)
    assert result.stop_reason == "no_tool_calls" and result.steps == 4
    assert ctx.chosen_hotel.name == "近湖酒店" and ctx.chosen_hotel.estimated_cost == 200
    assert ctx.chosen_reason == "已实测到西湖驾车距离 2.3 公里"
    assert result.usage == {"llm_calls": 4, "prompt_tokens": 44, "completion_tokens": 0}
    assert len(ctx.calls) == 7


@pytest.mark.parametrize("role", ["attractions", "hotels"])
def test_independent_searches_run_in_parallel_with_isolated_deterministic_merge(monkeypatch, role):
    ctx = context()
    ctx.errors.append("existing")
    if role == "hotels":
        ctx.hotels["existing"] = make_hotel()
        ctx.ranked = ["obsolete"]
        ctx.distance_cache["__rank_best__"] = 99.0
    barrier = threading.Barrier(2)
    finished_second = threading.Event()
    typecode = "110202" if role == "attractions" else "100105"

    def search(keyword, *_a, **_kw):
        barrier.wait(timeout=5)  # This would fail if the searches were serialized.
        if keyword == "first":
            assert finished_second.wait(timeout=5)
        else:
            finished_second.set()
        return [
            make_attraction("duplicate-" + keyword, poi_id="duplicate", typecode=typecode),
            make_attraction(keyword, poi_id=keyword, typecode=typecode),
            make_attraction("西湖改写", poi_id="lake", typecode=typecode),
        ]

    monkeypatch.setattr(amap, "search_poi", search)
    name = "search_attractions" if role == "attractions" else "search_hotels"
    results = execute_batch(role, batch(call("one", name, keyword="first"),
                                       call("two", name, keyword="second")), ctx)
    pool = ctx.attractions if role == "attractions" else ctx.hotels
    assert pool["duplicate"].name == "duplicate-first"  # Completion order cannot overwrite this.
    assert "first" in pool and "second" in pool
    assert ctx.attractions["lake"].required and ctx.attractions["lake"].name == "西湖"
    assert ctx.errors == ["existing"]
    assert [c["tool_call_id"] for c in ctx.calls] == ["one", "two"]
    assert "first" in results[0].message and "second" in results[1].message
    if role == "hotels":
        assert ctx.ranked == [] and "__rank_best__" not in ctx.distance_cache


def test_parallel_failure_does_not_erase_successful_search_or_existing_errors(monkeypatch):
    ctx = context()
    ctx.errors.append("existing")
    barrier = threading.Barrier(2)

    def search(keyword, *_a, **_kw):
        barrier.wait(timeout=5)
        if keyword == "bad":
            raise amap.AmapUnreachable("offline")
        return [make_attraction("博物馆", poi_id="museum")]

    monkeypatch.setattr(amap, "search_poi", search)
    results = execute_batch("attractions", batch(call("bad", "search_attractions", keyword="bad"),
                                                call("good", "search_attractions", keyword="good")), ctx)
    assert "网络不可达" in results[0].message and "采纳 1 条" in results[1].message
    assert ctx.attractions["museum"].name == "博物馆"
    assert len(ctx.errors) == 2 and ctx.errors[0] == "existing"


def test_multiple_final_hotel_choices_are_rejected_instead_of_last_write_winning():
    ctx = context()
    ctx.hotels["a"] = make_hotel()
    ctx.hotels["b"] = make_hotel()
    ctx.hotels["b"].name = "另一家酒店"
    results = execute_batch("hotels", batch(
        call("a", "choose_hotel", hotel="测试酒店", price_per_night=200),
        call("b", "choose_hotel", hotel="另一家酒店", price_per_night=300),
    ), ctx)
    assert all("多次 choose_hotel" in result.message for result in results)
    assert ctx.chosen_hotel is None


def test_unregistered_tool_and_wrong_role_remain_rejected(monkeypatch):
    ctx = context()

    def forbidden(*_a, **_kw):
        raise AssertionError("Not authorized to call AMap")

    monkeypatch.setattr(amap, "search_poi", forbidden)
    results = execute_batch("weather", batch(call("unknown", "not_a_tool"),
                                             call("hotel", "search_hotels")), ctx)
    assert "没有名为" in results[0].message and "无权" in results[1].message
    assert len(ctx.calls) == 2 and ctx.hotels == {}


def test_new_hotel_candidates_invalidate_old_ranking_before_fallback(monkeypatch):
    ctx = context()
    ctx.hotels["old"] = make_hotel()
    ctx.ranked = ["测试酒店"]
    ctx.distance_cache = {"__rank_best__": 10, "测试酒店->西湖": 2000}
    monkeypatch.setattr(amap, "search_poi", lambda *_a, **_kw: [
        make_attraction("新酒店", typecode="100105", poi_id="new"),
    ])
    execute("hotels", "search_hotels", {}, ctx)
    assert ctx.ranked == [] and "__rank_best__" not in ctx.distance_cache
    assert ctx.distance_cache["测试酒店->西湖"] == 2000


def test_unknown_new_tools_default_to_sequential_execution(monkeypatch):
    ctx = context()
    seen = []

    def handler(args, _ctx):
        seen.append(args["value"])
        return ToolResult.success(str(args["value"]))

    monkeypatch.setitem(tools.TOOLS, "new_tool", {"roles": ("hotels",), "handler": handler,
                        "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}}})
    results = execute_batch("hotels", batch(call("one", "new_tool", value=1),
                                            call("two", "new_tool", value=2)), ctx)
    assert [r.message for r in results] == ["1", "2"] and seen == [1, 2]


def test_dependency_feedback_does_not_remove_agent_step_limit(monkeypatch):
    ctx = context()
    monkeypatch.setattr(amap, "search_poi", lambda *_a, **_kw: [])
    turn = {"tool_calls": [call("search", "search_hotels"),
                           call("choose", "choose_hotel", hotel="不存在", price_per_night=200)]}
    model_script(monkeypatch, [turn] * 3)
    result = react_loop.run_agent("hotels", "system", "user", ctx, max_steps=3)
    assert result.stop_reason == "max_steps" and result.usage["llm_calls"] == 3
    assert ctx.chosen_hotel is None and len(ctx.calls) == 6
