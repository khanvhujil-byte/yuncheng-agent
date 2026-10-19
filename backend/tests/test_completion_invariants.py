"""Phase A「完成条件」不变量测试。

为什么单独一个文件、而不是散在各自的测试里：
    这些不是"某个函数的边界"，而是**系统对用户的承诺**。它们必须一眼可见、一票否决，
    而且每一条都对应一个真实发生过的失败（注释里写了现场）。

四条承诺：
    1. 被拦下的请求不产生任何下游动作（0 次 LLM、0 次工具、没有任何产出）
    2. success 必须包含"校验结论"，不合格的计划不能被标成成功
    3. 不合格的计划不能进行程库（否则把一次失败永久固化给之后所有人）
    4. 验证依据必须来自数据快照（坐标），不能信模型复述的地理事实
    5. 续跑不能因为"节点出现过"就跳过独立复核或丢掉那次重试
"""

import pytest
from fastapi.testclient import TestClient

from app.agents import graph as graph_module
from app.agents.city import MAX_MUST_VISIT, MAX_SPOTS_PER_DAY, validate_city
from app.agents.planner_agent import _enrich
from app.api.main import app
from app.api.routes import trip as trip_module
from app.api.routes.trip import _initial_state
from app.models.schemas import DayPlan, Location
from app.services import runner, store
from tests.factories import make_attraction, make_day, make_request
from tests.test_api import StubGraph, _final_state


# ================================================================ 1. 被拦下的请求

class TestBlockedRequestsStayBlocked:
    """承诺 1：预检失败 = 0 次 LLM、0 次工具、没有下游产出。

    真 bug 现场（eval_gate 日志）：预检已经拦下，日志里却出现
        `Agent(hotels) 到达步数上限 5，强制结束`
    —— 根因是 `validate_city → collect_weather` 是**普通边**（普通边一定触发）。"""

    @pytest.fixture
    def unresolvable_city(self, monkeypatch):
        monkeypatch.setattr("app.agents.city.geocode", lambda *a, **k: (None, None))
        monkeypatch.setattr("app.agents.city.search_poi", lambda *a, **k: [])

    def test_no_llm_and_no_tools_after_preflight_failure(self, unresolvable_city):
        final = graph_module.app.invoke(_initial_state(make_request()))

        assert final["error_code"] == "CITY_NOT_FOUND"
        assert final.get("usage", {}).get("llm_calls", 0) == 0, "预检失败却花了 LLM 调用"
        assert final.get("tool_calls") in (None, []), "预检失败却执行了工具"
        assert final.get("plan") is None
        assert not final.get("weather")
        assert not final.get("hotels")
        assert final.get("hotel") is None


# ================================================================ 2、3. 成功与入库

class TestSuccessRequiresValidation:
    """承诺 2、3：success 要含校验结论；不合格的**绝不入库**。"""

    @pytest.fixture
    def client(self, monkeypatch):
        def _install(final):
            monkeypatch.setattr(trip_module, "trip_graph", StubGraph(final))
            monkeypatch.setattr(graph_module, "app", StubGraph(final))
            return TestClient(app)

        return _install

    def _post(self, http):
        return http.post("/api/trip/plan", json=make_request().model_dump()).json()

    def test_unresolved_validation_error_is_partial_not_success(self, client):
        """独立复核/两层校验留下了未解决的错误 → 必须是"部分结果"，不能是成功。"""
        final = _final_state()
        final["validate_error"] = "2026-10-10 这一天排不下（独立复核）"

        body = self._post(client(final))

        assert body["success"] is False
        assert "未通过校验" in body["message"]
        assert body["data"] is not None  # 仍然把行程给用户看，但明说它没通过

    def test_unsatisfied_hard_constraint_is_partial_not_success(self, client):
        final = _final_state()
        final["constraint_report"] = {"satisfied": [], "unsatisfied": ["缺少必去景点：西湖"]}

        body = self._post(client(final))

        assert body["success"] is False
        assert "缺少必去景点" in body["message"]

    def test_unqualified_plan_is_not_cached(self, client):
        """★ 最关键的一条：不合格的计划不能进缓存 —— 否则之后每个命中的人拿到的都是它。"""
        final = _final_state()
        final["validate_error"] = "时间排不下"
        http = client(final)
        self._post(http)

        assert store.find_itinerary(store.fingerprint_of(make_request())) is None

    def test_qualified_plan_is_still_cached(self, client):
        """正面控制：判据收紧之后，合格行程**仍然**要入库（别把缓存一起修没了）。"""
        http = client(_final_state())
        body = self._post(http)

        assert body["success"] is True
        assert store.find_itinerary(store.fingerprint_of(make_request())) is not None


# ================================================================ 4. 验证依据的来源

class TestAuditUsesSnapshotFacts:
    """承诺 4：地理事实来自数据快照，模型只负责"点哪个景点"。"""

    def test_model_coordinates_are_overwritten(self):
        """verify_plan 的"一天跨度 >25km"用的就是这些坐标，不能让模型决定。"""
        candidate = make_attraction("杭州西湖风景名胜区", lng=120.121358, lat=30.222692)
        days = [DayPlan.model_validate(make_day(0, "2026-10-10", [make_attraction("占位")]))]
        spot = days[0].attractions[0]
        spot.name = "杭州西湖风景名胜区"
        spot.poi_id = None
        spot.location = Location(longitude=0.0, latitude=0.0)  # 模型瞎写的坐标

        assert _enrich(days, [candidate]) == 1
        assert spot.location.longitude == pytest.approx(120.121358)
        assert spot.location.latitude == pytest.approx(30.222692)


# ================================================================ 5. 续跑语义

class TestResumeDoesNotSkipAudit:
    """承诺 5：重试会让节点重复出现 —— "出现过"不等于"这一阶段完成了"。"""

    def test_pending_retry_resumes_at_compose_not_hotel(self):
        """复核判了不合格、重排还没做：必须回 compose_plan，不能跳到 resolve_hotel。"""
        completed = ["validate_city", "collect_attractions", "collect_weather",
                     "compose_plan", "verify_plan"]
        assert runner.can_resume_from(completed, {"validate_error": "第二天排不下"}) == "compose_plan"

    def test_compose_without_audit_resumes_at_audit(self):
        """编排完成但独立复核没跑过：续跑会**绕过复核**，宁可不续。"""
        completed = ["validate_city", "collect_attractions", "collect_weather", "compose_plan"]
        assert runner.can_resume_from(completed, {}) == "verify_plan"

    def test_compose_with_clean_audit_resumes_at_hotel(self):
        completed = ["validate_city", "collect_attractions", "collect_weather",
                     "compose_plan", "verify_plan"]
        assert runner.can_resume_from(completed, {}) == "resolve_hotel"


# ================================================================ 6. 图接线（图级测试）

class TestGraphWiring:
    """★ 图级测试：把每个节点换成记录器，只测**路由接线本身**。

    为什么需要这一层：上面那个真 bug 的根因在接线（普通边 vs 条件边），
    而不是任何节点的内部实现。用真节点去测会被 LLM 调用、配额和耗时搅在一起，
    所以这里把节点全部桩掉 —— 接错了立刻暴露，且零成本、零网络。
    """

    @pytest.fixture
    def wiring(self, monkeypatch):
        import app.agents.graph as graph_module

        calls: list[str] = []
        outcome = {"adcode": None}

        def stub_validate(_state):
            calls.append("validate_city")
            return {"adcode": outcome["adcode"]}

        def recorder(name: str):
            def node(_state):
                calls.append(name)
                return {}

            return node

        monkeypatch.setattr(graph_module, "validate_city", stub_validate)
        for name in ("collect_attractions", "collect_weather", "collect_food", "collect_hotels", "prepare_planning_context", "compose_plan",
                     "verify_plan", "resolve_hotel", "patch_budget"):
            monkeypatch.setattr(graph_module, name, recorder(name))

        return calls, outcome, graph_module.build_graph()

    def test_ok_path_fans_out_to_both_collectors(self, wiring):
        calls, outcome, graph = wiring
        outcome["adcode"] = "330100"

        graph.invoke({"adcode": None})

        # 两路采集是并行的，先后顺序不保证；要点是**两路都跑到**
        assert "collect_attractions" in calls, f"景点那一路没跑：{calls}"
        assert "collect_weather" in calls, f"天气那一路没跑：{calls}"
        # 汇聚之后整条链都要走完
        for node in ("compose_plan", "verify_plan", "collect_hotels", "prepare_planning_context", "patch_budget"):
            assert node in calls, f"{node} 没跑：{calls}"

    def test_fail_path_runs_nothing_but_preflight(self, wiring):
        """★ 这就是那个真 bug 的图级守卫：预检失败时**只有 validate_city 允许跑**。

        修之前，天气那条普通边会把 collect_weather → compose_plan → … → resolve_hotel
        整条链带跑（实测日志里出现过 `Agent(hotels) 到达步数上限 5`）。
        """
        calls, outcome, graph = wiring
        outcome["adcode"] = None

        graph.invoke({"adcode": None})

        assert calls == ["validate_city"], f"预检失败却跑了下游节点：{calls}"


# ================================================================ 7. 预算复核与有界重排

def _budget_state(*, limit=None, known=0, estimated=0, unknown=None,
                  retries=0, validate_error=None) -> dict:
    """造一个"刚算完钱"的最小状态，用来单测 verify_budget 的判定。"""
    items = []
    if known:
        items.append({"category": "hotel", "name": "住宿", "amount": known,
                      "status": "known", "source": "amap"})
    if estimated:
        items.append({"category": "meal", "name": "餐饮", "amount": estimated,
                      "status": "estimated", "source": "llm_estimated"})
    for name in (unknown or []):
        items.append({"category": "attraction", "name": name, "amount": None,
                      "status": "unknown", "source": "unknown"})
    plan = {
        "city": "杭州",
        "days": [{"date": "2026-10-10", "day_index": 0, "attractions": [], "meals": [],
                  "day_total_cost": known + estimated}],
        "budget": {"items": items, "known_total": known, "estimated_total": estimated,
                   "unknown_items": list(unknown or []), "total": known + estimated, "limit": limit},
    }
    return {"plan": plan, "request": make_request(budget_limit=limit),
            "budget_retries": retries, "validate_error": validate_error}


class TestBudgetVerdict:
    """承诺：预算结论能区分"超了""很可能超""没验证过"，且未知绝不按零元通过。"""

    def test_no_limit_means_no_constraint(self):
        from app.agents.verify_budget import verify_budget

        verdict = verify_budget(_budget_state(limit=None, known=500))["budget_verdict"]
        assert verdict["status"] == "none"

    def test_known_exceeded_is_hard(self):
        from app.agents.verify_budget import verify_budget

        verdict = verify_budget(_budget_state(limit=100, known=500))["budget_verdict"]
        assert verdict["status"] == "known_exceeded"
        assert "硬违反" in verdict["basis"]

    def test_estimated_exceeded_says_not_infeasible(self):
        """估算超限**不能**被说成"无解"—— 必须写明它只是"按当前口径很可能超"。"""
        from app.agents.verify_budget import verify_budget

        verdict = verify_budget(_budget_state(limit=100, known=50, estimated=80))["budget_verdict"]
        assert verdict["status"] == "estimated_exceeded"
        assert "不等于实际旅行不可行" in verdict["basis"]

    def test_unknown_items_block_confirmation(self):
        """有未知项时不能宣称满足预算 —— 这是"未知不按零元通过"的落点。"""
        from app.agents.verify_budget import verify_budget

        state = _budget_state(limit=1000, known=50, estimated=80, unknown=["西湖", "灵隐寺"])
        verdict = verify_budget(state)["budget_verdict"]

        assert verdict["status"] == "unverified"
        assert "无法确认" in verdict["basis"]
        assert verdict["unknown_items"] == ["西湖", "灵隐寺"]

    def test_within_states_its_basis(self):
        from app.agents.verify_budget import verify_budget

        verdict = verify_budget(_budget_state(limit=1000, known=50, estimated=80))["budget_verdict"]
        assert verdict["status"] == "within"
        assert "上限" in verdict["basis"] and "范围" in verdict["basis"]


class TestBudgetRetryIsBounded:
    """★ 防死循环：预算超限最多重排 BUDGET_RETRY_LIMIT 次。

    budget_retries 统计进入预算重排的轮数，retries 限制每轮编排的尝试次数。
    复核只提供结论和反馈，不提前消耗重排次数。
    """

    def test_router_retries_then_stops(self):
        from app.agents.graph import route_after_budget
        from app.agents.verify_budget import BUDGET_RETRY_LIMIT

        overloaded = {"budget_verdict": {"status": "estimated_exceeded"}}
        assert route_after_budget({**overloaded, "budget_retries": 1}) == "retry"
        assert route_after_budget(
            {**overloaded, "budget_retries": BUDGET_RETRY_LIMIT}) == "next"

    def test_router_does_not_retry_when_budget_is_fine(self):
        from app.agents.graph import route_after_budget

        assert route_after_budget({"budget_retries": 1, "budget_verdict": {"status": "within"}}) == "next"

    def test_retry_signals_without_consuming_round(self):
        from app.agents.verify_budget import verify_budget

        result = verify_budget(_budget_state(limit=100, known=500))
        assert "budget_retries" not in result
        assert str(result["validate_error"]).startswith("预算复核不通过")

    def test_exhausted_retries_stop_signalling(self):
        from app.agents.verify_budget import BUDGET_RETRY_LIMIT, verify_budget

        result = verify_budget(_budget_state(limit=100, known=500, retries=BUDGET_RETRY_LIMIT))
        assert "budget_retries" not in result
        assert "validate_error" not in result  # 不再触发重排，交给 budget_verdict 参与最终判定

    def test_solved_budget_clears_its_own_stale_error(self):
        """重排后预算通过了，就得把上一轮那条错误清掉 —— 否则已经解决的问题会把结果判成失败。"""
        from app.agents.verify_budget import verify_budget

        result = verify_budget(_budget_state(
            limit=1000, known=50, retries=1,
            validate_error="预算复核不通过（estimated_exceeded）：上一轮的说明"))
        assert result["validate_error"] is None


# ================================================================ 8. 两个上限的职责

class TestTwoLimitsHaveDifferentJobs:
    """`capacity`（物理上限）与 `MAX_MUST_VISIT`（我们的配额）不能混为一谈。"""

    @pytest.fixture
    def geo(self, monkeypatch):
        monkeypatch.setattr("app.agents.city.geocode",
                            lambda *a, **k: (Location(longitude=120.2, latitude=30.25), "330100"))
        monkeypatch.setattr("app.agents.city.search_poi",
                            lambda keyword, *a, **k: [make_attraction(f"{keyword}景区")])

    def _state(self, **overrides):
        return _initial_state(make_request(**overrides))

    def test_physically_infeasible_is_rejected(self, geo):
        """15 个必去塞进 3 天（容量 = 3 × 每天上限）→ 拒绝，
        而不是"截到 10 个然后装作成功"。"""
        names = [f"景点{i}" for i in range(15)]
        result = validate_city(self._state(travel_days=3, must_visit=names))

        capacity = 3 * MAX_SPOTS_PER_DAY
        assert result["error_code"] == "CONSTRAINT_INFEASIBLE"
        assert f"3 天最多安排 {capacity} 个" in result["errors"][0]
        # 一个必去都没解析（拒绝要发生在花钱之前）
        assert result.get("must_visit_resolved") in (None, [])

    def test_quota_limit_rejects_without_dropping_hard_constraints(self, geo):
        """配额不够时拒绝请求，不能静默删掉用户必须去的景点。"""
        names = [f"景点{i}" for i in range(MAX_MUST_VISIT + 2)]
        result = validate_city(self._state(travel_days=30, must_visit=names))

        assert result["error_code"] == "CONSTRAINT_INFEASIBLE"
        assert not result.get("must_visit_resolved")
