"""预算节点（`agents/budget.py`）的测试。

预算是最典型的"不能让模型算"的部分：加法错了没人会去复核，
所以这里盯两个最容易错的口径 —— **住宿费按晚数（不是天数）**、**每日小计之和必须精确等于总计**。
"""

from app.agents.budget import patch_budget, _transport_cost
from tests.factories import make_attraction, make_day, make_hotel, make_plan, make_request


def _state(days, hotel=None, nights=2, transportation="公共交通", travel_days=3):
    return {
        "request": make_request(transportation=transportation, travel_days=travel_days),
        "travel_days": travel_days,
        "nights": nights,
        "hotel": hotel,
        "plan": make_plan(days=days),
    }


class TestHotelCost:
    def test_hotel_cost_is_price_times_nights_not_days(self):
        """★ 3 天行程住 2 晚：住宿费必须是 260×2，按天数算会多算一晚。"""
        days = [make_day(i, f"2026-10-1{i}", [make_attraction(f"景点{i}")]) for i in range(3)]
        result = patch_budget(_state(days, hotel=make_hotel(price=260, nights=2)))
        assert result["plan"]["budget"]["total_hotels"] == 520

    def test_zero_nights_means_no_hotel_cost(self):
        days = [make_day(0, "2026-10-10", [make_attraction("一日游")])]
        result = patch_budget(_state(days, hotel=make_hotel(price=300, nights=0),
                                     nights=0, travel_days=1))
        assert result["plan"]["budget"]["total_hotels"] == 0

    def test_missing_hotel_is_zero_and_warned(self):
        days = [make_day(0, "2026-10-10", [make_attraction("景点")])]
        result = patch_budget(_state(days, hotel=None))
        assert result["plan"]["budget"]["total_hotels"] == 0
        assert any("未确定酒店" in w for w in result["warnings"])


class TestTotals:
    def test_per_day_costs_sum_exactly_to_total(self):
        """★ 每日小计之和必须精确等于总计。

        曾经用 round() 摊分住宿与交通，结果差 1 元 —— 而"每日小计加起来不等于总计"
        在预算卡上是很扎眼的问题。现在是整除 + 余数补给最后一天。
        """
        days = [
            make_day(0, "2026-10-10", [make_attraction("A", ticket_price=30)]),
            make_day(1, "2026-10-11", [make_attraction("B", ticket_price=0)]),
            make_day(2, "2026-10-12", [make_attraction("C", ticket_price=60)]),
        ]
        result = patch_budget(_state(days, hotel=make_hotel(price=260, nights=2)))
        budget = result["plan"]["budget"]
        assert sum(d["day_total_cost"] for d in result["plan"]["days"]) == budget["total"]

    def test_total_is_sum_of_four_parts(self):
        days = [make_day(0, "2026-10-10", [make_attraction("A", ticket_price=100)])]
        result = patch_budget(_state(days, hotel=make_hotel(price=200, nights=1),
                                     nights=1, travel_days=1))
        budget = result["plan"]["budget"]
        assert budget["total"] == (
            budget["total_attractions"] + budget["total_hotels"]
            + budget["total_meals"] + budget["total_transportation"]
        )
        assert budget["total_attractions"] == 100
        assert budget["total_meals"] == 150  # 三餐，每餐 50

    def test_zero_ticket_prices_are_warned(self):
        days = [make_day(0, "2026-10-10", [make_attraction("免费公园", ticket_price=0)])]
        result = patch_budget(_state(days, hotel=make_hotel()))
        assert any("门票价未获取" in w for w in result["warnings"])


class TestEdgeCases:
    def test_empty_days_returns_none_budget(self):
        result = patch_budget(_state([], hotel=None))
        assert result["plan"]["budget"] is None
        assert any("行程为空" in w for w in result["warnings"])

    def test_transport_cost_by_mode(self):
        assert _transport_cost("公共交通", 3) == 120
        assert _transport_cost("自驾", 2) == 240
        assert _transport_cost("未知方式", 1) == 60  # 兜底默认值

    def test_input_plan_is_not_mutated_in_place(self):
        """patch_budget 返回新 dict，不该改调用方传进来的那份（避免隐蔽的共享状态）。"""
        days = [make_day(0, "2026-10-10", [make_attraction("A")])]
        state = _state(days, hotel=make_hotel())
        original_day_cost = state["plan"]["days"][0]["day_total_cost"]
        patch_budget(state)
        assert state["plan"]["days"][0]["day_total_cost"] == original_day_cost
