"""编排节点（`agents/planner_agent.py`）里几段确定性逻辑的测试。

这里不测 LLM 本身（那要靠实验），测的是**围在 LLM 外面的三道工序**：
    1. 上下文压缩（送多少候选进去）
    2. 第 1 层结构校验（天数 / 日期 / 每天景点数与三餐）
    3. 字段回填（模型不该复述的字段补回来）
这三段的共同点是：**都不需要联网、也不该出错**，出错就是静默的数据损失。
"""

from app.agents.planner_agent import (
    MAX_SPOTS_PER_DAY,
    _enrich,
    _format_attractions,
    _format_weather,
    _select_candidates,
    _validate_structure,
)
from app.models.schemas import DayPlan
from tests.factories import make_attraction, make_day, make_weather


def _days(*, count=2, spots_per_day=2, meals=3, start_day=10):
    return [
        DayPlan.model_validate(
            make_day(
                index,
                f"2026-10-{start_day + index:02d}",
                [make_attraction(f"景点{index}-{j}") for j in range(spots_per_day)],
                meals=meals,
            )
        )
        for index in range(count)
    ]


class TestContextCompression:
    def test_anchors_are_always_kept_even_with_worst_rating(self):
        """★ 压缩不能把必去景点裁掉 —— 否则硬约束会静默消失。"""
        anchor = make_attraction("必去景点", required=True, rating=0.1)
        others = [make_attraction(f"普通{i}", rating=4.9) for i in range(50)]
        selected, total = _select_candidates([anchor] + others, travel_days=3)

        assert total == 51
        assert anchor in selected
        assert all(spot.required for spot in selected if spot.required)

    def test_limit_is_at_least_days_times_four(self):
        """天数多时必须给够候选，否则编排无米下锅。

        注意：能给的永远不超过池子本身 —— 池子 60 条、上限 120 条时，就是全给。
        """
        candidates = [make_attraction(f"景点{i}", rating=4.0) for i in range(60)]
        selected, total = _select_candidates(candidates, travel_days=30)
        assert total == 60
        assert len(selected) == 60  # 池子比上限小 → 全给

    def test_large_pool_is_capped_by_days_times_four(self):
        candidates = [make_attraction(f"景点{i}", rating=4.0) for i in range(200)]
        selected, total = _select_candidates(candidates, travel_days=30)
        assert total == 200
        assert len(selected) == 30 * 4  # 上限生效

    def test_selection_prefers_higher_rating(self):
        candidates = [make_attraction(f"低分{i}", rating=2.0) for i in range(40)]
        candidates.append(make_attraction("高分景点", rating=5.0))
        selected, _ = _select_candidates(candidates, travel_days=3)
        assert any(spot.name == "高分景点" for spot in selected)

    def test_compaction_can_be_switched_off(self, monkeypatch, settings):
        """关掉压缩就回到 v0.3 行为（全部候选都进 prompt），用于 A/B 实验。"""
        monkeypatch.setattr(settings, "prompt_compact", False)
        candidates = [make_attraction(f"景点{i}") for i in range(60)]
        selected, total = _select_candidates(candidates, travel_days=3)
        assert len(selected) == total == 60


class TestStructureValidation:
    def test_ok_returns_none(self):
        assert _validate_structure(_days(count=2), "2026-10-10", 2) is None

    def test_wrong_day_count(self):
        message = _validate_structure(_days(count=2), "2026-10-10", 3)
        assert message is not None and "正好 3 天" in message

    def test_wrong_date(self):
        days = _days(count=2)
        days[1] = days[1].model_copy(update={"date": "2026-10-15"})
        message = _validate_structure(days, "2026-10-10", 2)
        assert message is not None and "应是 2026-10-11" in message

    def test_wrong_day_index(self):
        days = _days(count=2)
        days[1] = days[1].model_copy(update={"day_index": 7})
        message = _validate_structure(days, "2026-10-10", 2)
        assert message is not None and "day_index" in message

    def test_too_many_spots_in_one_day(self):
        """实测模型会把一天排到 5 个 —— prompt 写了 2~3 个也拦不住，必须靠这层。"""
        days = _days(count=1, spots_per_day=MAX_SPOTS_PER_DAY + 1)
        message = _validate_structure(days, "2026-10-10", 1)
        assert message is not None and "超过每天上限" in message

    def test_day_without_attractions(self):
        days = _days(count=1, spots_per_day=0)
        message = _validate_structure(days, "2026-10-10", 1)
        assert message is not None and "没有安排任何景点" in message

    def test_day_without_meals(self):
        days = _days(count=1, meals=0)
        message = _validate_structure(days, "2026-10-10", 1)
        assert message is not None and "三餐" in message

    def test_unparsable_start_date_is_not_this_layer_s_job(self):
        """日期本身不合法由预检负责，这里不该重复报错。"""
        assert _validate_structure(_days(count=1), "不是日期", 1) is None


class TestEnrichment:
    def test_backfills_fields_the_model_should_not_repeat(self):
        """★ 前端验证时发现的真问题：图片 / 评分 / poi_id / 必去标记全丢。"""
        candidate = make_attraction(
            "杭州西湖风景名胜区", required=True, rating=4.9, poi_id="B023B13L9M",
            typecode="110202", image_url="https://example.com/x.jpg",
        )
        days = _days(count=1, spots_per_day=1)
        # 模型输出里只有名字与坐标，其余字段是空的
        days[0].attractions[0].name = "杭州西湖风景名胜区"
        days[0].attractions[0].poi_id = None
        days[0].attractions[0].image_url = None
        days[0].attractions[0].rating = None
        days[0].attractions[0].required = False

        filled = _enrich(days, [candidate])
        spot = days[0].attractions[0]
        assert filled == 1
        assert spot.poi_id == "B023B13L9M"
        assert spot.image_url == "https://example.com/x.jpg"
        assert spot.rating == 4.9
        assert spot.required is True

    def test_matches_by_poi_id_even_when_name_differs(self):
        candidate = make_attraction("正式名", poi_id="POI-SAME", image_url="https://a/b.jpg")
        days = _days(count=1, spots_per_day=1)
        days[0].attractions[0].poi_id = "POI-SAME"
        days[0].attractions[0].name = "模型写的不太一样的名字"

        assert _enrich(days, [candidate]) == 1
        assert days[0].attractions[0].image_url == "https://a/b.jpg"

    def test_unknown_attraction_is_left_alone(self):
        days = _days(count=1, spots_per_day=1)
        assert _enrich(days, [make_attraction("完全不同的景点")]) == 0


class TestPromptFormatting:
    def test_beyond_window_is_explicitly_forbidden(self):
        text = _format_weather([make_weather("2026-10-14", beyond=True)])
        assert "超出预报窗口" in text and "不要编造" in text

    def test_normal_weather_shows_numbers(self):
        text = _format_weather([make_weather("2026-10-10")])
        assert "2026-10-10" in text and "晴" in text and "26" in text

    def test_anchors_are_marked_in_candidate_list(self):
        text = _format_attractions([make_attraction("西湖", required=True)])
        assert "【用户必去】" in text
        assert "【用户必去】" not in _format_attractions([make_attraction("普通景点")])

    def test_empty_candidate_list_is_explicit(self):
        assert "无候选景点" in _format_attractions([])
        assert "无天气数据" in _format_weather([])
