"""图接线的不变量测试 —— 守卫"被拦下的请求不该产生任何下游动作"。

为什么单独一个文件：这类问题**不会报错**，只会让成本悄悄变高、语义悄悄变脏。
真 bug 现场（eval_gate 日志）：
    预检不通过 | ATTRACTION_NOT_FOUND | ...
    编排跳过 | 候选景点为空
    预算跳过 | 行程为空
    Agent(hotels) 到达步数上限 5，强制结束（用收集箱里已有的数据）
—— 请求已经被拦下了，酒店 Agent 却真的跑了一轮 LLM（花钱 + 花高德配额）。
根因：`validate_city → collect_weather` 是**普通边**，普通边一定会触发，
条件边把流程导向 END 的同时，weather 那一路照样把整条下游链带跑了。
"""

import pytest

from app.agents.graph import app as trip_graph

from tests.factories import make_request
from app.api.routes.trip import _initial_state


@pytest.fixture
def unresolvable_city(monkeypatch):
    """让地理编码查不到城市 —— 预检必然失败。"""
    monkeypatch.setattr("app.agents.city.geocode", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr("app.agents.city.search_poi", lambda *args, **kwargs: [])


def test_preflight_failure_costs_zero_llm_calls(unresolvable_city):
    """★ 核心不变量：预检失败 = 0 次 LLM 调用、0 次工具调用、没有任何下游产出。

    这条测试就是那个真 bug 的守卫：修之前它会失败（酒店 Agent 会真的跑一轮）。
    """
    final = trip_graph.invoke(_initial_state(make_request()))

    assert final["error_code"] == "CITY_NOT_FOUND"
    assert final.get("usage", {}).get("llm_calls", 0) == 0, "预检失败却花了 LLM 调用"
    assert final.get("tool_calls") in (None, []), "预检失败却执行了工具"
    assert final.get("plan") is None
    # 天气与酒店这两路都不该有任何产出
    assert not final.get("weather")
    assert not final.get("hotels")
    assert final.get("hotel") is None


def test_preflight_failure_does_not_run_budget(unresolvable_city):
    """预算节点也不该跑：它现在会在"行程为空"时写一条 warning，
    但更该守的是**响应里不该出现一份空预算**（那会让前端以为算过钱）。"""
    final = trip_graph.invoke(_initial_state(make_request()))
    plan = final.get("plan")
    assert plan is None or not (plan.get("budget") if isinstance(plan, dict) else None)
