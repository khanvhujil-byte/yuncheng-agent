"""独立评估者（`agents/verify_plan.py`）的测试。

它审的是"行程自己站不站得住"（时间排不下 / 重复排期 / 跨度过大），
与 compose_plan 审的"用户要求满足了没"是两个视角。测试要守住这条分界：
**它只做确定性判断，不打分、不看用户偏好。**
"""

from app.agents.verify_plan import (
    MAX_DAY_MINUTES,
    MAX_DAY_SPAN_KM,
    MINUTES_PER_HOP,
    SINGLE_DAY_BUDGET_SHARE,
    verify_plan,
)
from tests.factories import make_attraction, make_day, make_plan


def _state(days, **overrides):
    state = {"plan": make_plan(days=days)}
    state.update(overrides)
    return state


class TestPassThrough:
    def test_skips_when_previous_layer_already_failed(self):
        """上一层（两层校验）已经判不合格时不该重复审 —— 否则会覆盖掉更具体的错误信息。"""
        days = [make_day(0, "2026-10-10", [make_attraction("A", visit_duration=600)])]
        result = verify_plan(_state(days, validate_error="缺少必去景点：西湖"))
        assert result == {}

    def test_empty_plan_returns_nothing(self):
        assert verify_plan({"plan": None}) == {}
        assert verify_plan({"plan": {"days": []}}) == {}

    def test_clean_plan_produces_no_warnings(self):
        days = [
            make_day(0, "2026-10-10", [make_attraction("A", lng=120.12, lat=30.22),
                                       make_attraction("B", lng=120.13, lat=30.23)]),
            make_day(1, "2026-10-11", [make_attraction("C", lng=120.14, lat=30.24)]),
        ]
        assert verify_plan(_state(days)) == {}


class TestSevereFindings:
    def test_infeasible_day_triggers_retry(self):
        """3 个各 4 小时的景点 → 远超一天 10 小时，必须回喂重试。"""
        spots = [make_attraction(name, visit_duration=240) for name in ("A", "B", "C")]
        result = verify_plan(_state([make_day(0, "2026-10-10", spots)]))

        assert "validate_error" in result
        assert "排不下" in result["validate_error"]
        assert result["errors"] and result["errors"][0].startswith("verify_plan:")
        # 不在这里动 retries —— 计数由 compose_plan 每次尝试自增，条件边据此封顶
        assert "retries" not in result

    def test_duplicate_attraction_across_days_triggers_retry(self):
        same = make_attraction("西湖", poi_id="POI-X")
        days = [
            make_day(0, "2026-10-10", [same]),
            make_day(1, "2026-10-11", [make_attraction("西湖别名", poi_id="POI-X")]),
        ]
        result = verify_plan(_state(days))
        assert "validate_error" in result
        assert "重复" in result["validate_error"]

    def test_infeasible_threshold_boundary(self):
        """刚好卡在阈值上下：n 个景点 → 总时长 = n×d + (n-1)×30。"""
        # 3 个 180 分钟的景点：540 + 60 = 600，等于上限（不超过）→ 不触发
        exact = [make_attraction(str(i), visit_duration=180) for i in range(3)]
        assert MAX_DAY_MINUTES == 600 and sum([180, 180, 180]) + 2 * MINUTES_PER_HOP == 600
        assert verify_plan(_state([make_day(0, "2026-10-10", exact)])) == {}

        # 再加 1 分钟就越界
        over = [make_attraction(str(i), visit_duration=181) for i in range(3)]
        assert "validate_error" in verify_plan(_state([make_day(0, "2026-10-10", over)]))


class TestHints:
    def test_far_apart_same_day_is_only_a_hint(self):
        """跨度过大可以接受（用户就想跑两处），所以只提示、不触发重试。"""
        spots = [
            make_attraction("市区", lng=120.12, lat=30.22, visit_duration=60),
            # 经度差 0.5 度 ≈ 48 km（北纬 30 度附近 1 度经度约 96 km），稳稳超过 25 km
            make_attraction("远郊", lng=120.62, lat=30.22, visit_duration=60),
        ]
        result = verify_plan(_state([make_day(0, "2026-10-10", spots)]))
        assert "validate_error" not in result
        assert any("跨度" in w for w in result["warnings"])

    def test_single_day_expensive_is_hinted(self):
        days = [
            make_day(0, "2026-10-10", [make_attraction("A")]),
            make_day(1, "2026-10-11", [make_attraction("B")]),
        ]
        # 一天 800 / 另一天 100，总 900 → 榜首占 89%，远超 60% 的门槛
        days[0]["day_total_cost"] = 800
        days[1]["day_total_cost"] = 100
        plan = make_plan(days=days, budget={"total": 900})
        result = verify_plan({"plan": plan})
        assert any("总预算" in w for w in result.get("warnings", []))

    def test_dirty_attraction_does_not_crash(self):
        """审不了一条脏数据就跳过它 —— 不能因为一条脏数据让整个节点崩。"""
        day = make_day(0, "2026-10-10", [])
        day["attractions"] = [{"name": "缺少坐标和时长"}]
        assert verify_plan(_state([day])) == {}
