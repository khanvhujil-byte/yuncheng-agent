"""行程库（`services/store.py`）的测试。

这一层最值得测的是**指纹的语义**：它决定了"什么算同一批需求"。
判宽了会给出错误的行程（比如 6 天需求命中 3 天的行程），判窄了缓存就永远不命中。
"""

import json

import pytest

from app.models.schemas import Hotel, WeatherInfo
from app.services import store
from tests.factories import make_attraction, make_hotel, make_plan, make_request


class TestFingerprint:
    def test_same_request_same_fingerprint(self):
        assert store.fingerprint_of(make_request()) == store.fingerprint_of(make_request())

    def test_preference_order_does_not_matter(self):
        a = make_request(preferences=["自然风光", "博物馆"])
        b = make_request(preferences=["博物馆", "自然风光"])
        assert store.fingerprint_of(a) == store.fingerprint_of(b)

    def test_must_visit_order_does_not_matter(self):
        a = make_request(must_visit=["西湖", "灵隐寺"])
        b = make_request(must_visit=["灵隐寺", "西湖"])
        assert store.fingerprint_of(a) == store.fingerprint_of(b)

    def test_whitespace_does_not_matter(self):
        """多打一个空格不该被当成另一批需求。"""
        a = make_request(city="杭州")
        b = make_request(city=" 杭州 ")
        assert store.fingerprint_of(a) == store.fingerprint_of(b)

    def test_start_date_is_excluded(self):
        """★ 核心设计决定：指纹**不含出发日期**，同形状需求换个日期应当命中同一份行程。

        代价是命中后必须"平移日期 + 重取天气"（在 api/routes/trip.py 里做），
        这条测试就是那个决定的守卫：谁哪天顺手把 start_date 加进指纹，它会立刻红。
        """
        a = make_request(start_date="2026-10-10")
        b = make_request(start_date="2026-11-20")
        assert store.fingerprint_of(a) == store.fingerprint_of(b)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("travel_days", 5),
            ("city", "成都"),
            ("transportation", "自驾"),
            ("accommodation", "豪华酒店"),
            ("preferences", ["自然风光"]),
            ("must_visit", []),
            ("free_text_input", "想有一天去周边"),
            ("fixed_hotel", "某家酒店"),
        ],
    )
    def test_shape_changes_change_fingerprint(self, field, value):
        """任何"需求形状"变了，都必须算另一批需求。"""
        assert store.fingerprint_of(make_request()) != store.fingerprint_of(
            make_request(**{field: value})
        )

    def test_nights_default_matches_explicit(self):
        """不填 nights（默认 天数-1）与显式填 2 晚，是同一批需求。"""
        assert store.fingerprint_of(make_request()) == store.fingerprint_of(make_request(nights=2))


class TestSaveAndFind:
    def test_roundtrip_with_pydantic_objects(self):
        """★ 回归测试：TripPlan 是混合体（dict + Pydantic 对象）。

        曾经直接 json.dumps 会在 WeatherInfo 上抛 TypeError，
        而且崩点在"行程已经生成完、钱已经花掉"之后的入库那一刻 —— 最亏的失败方式。
        """
        plan = make_plan(
            days=[],
            weather_info=[WeatherInfo(date="2026-10-10", day_weather="晴", day_temp=26)],
            hotel=make_hotel(),
        )
        assert isinstance(plan["weather_info"][0], WeatherInfo)
        assert isinstance(plan["hotel"], Hotel)

        fingerprint = store.fingerprint_of(make_request())
        store.save_itinerary(fingerprint, make_request(), plan)

        found = store.find_itinerary(fingerprint)
        assert found is not None
        assert found["plan"]["weather_info"][0]["day_weather"] == "晴"
        assert found["plan"]["hotel"]["name"] == "测试酒店"

    def test_find_missing_returns_none(self):
        assert store.find_itinerary("not-a-real-fingerprint") is None

    def test_hit_count_increments_and_is_preserved_on_save(self):
        fingerprint = store.fingerprint_of(make_request())
        store.save_itinerary(fingerprint, make_request(), make_plan(days=[]))

        assert store.find_itinerary(fingerprint)["hit_count"] == 1
        assert store.find_itinerary(fingerprint)["hit_count"] == 2

        # 再存一次（覆盖内容）不该把复用次数清零 —— 它是历史证据
        store.save_itinerary(fingerprint, make_request(), make_plan(days=[], overall_suggestions="新"))
        found = store.find_itinerary(fingerprint)
        assert found["hit_count"] == 3
        assert found["plan"]["overall_suggestions"] == "新"


class TestRunLogAndStats:
    def test_log_run_and_stats(self):
        fingerprint = store.fingerprint_of(make_request())
        store.save_itinerary(fingerprint, make_request(), make_plan(days=[]))

        store.log_run(thread_id="t1", fingerprint=fingerprint, cached=False, success=True,
                      elapsed_ms=40000, llm_calls=11, prompt_tokens=15000, completion_tokens=3000)
        store.log_run(thread_id="t2", fingerprint=fingerprint, cached=True, success=True,
                      elapsed_ms=2200)

        stats = store.stats()
        assert stats["total_runs"] == 2
        assert stats["cached_runs"] == 1
        assert stats["cache_hit_rate"] == 0.5
        assert stats["avg_ms_generated"] == 40000
        assert stats["avg_ms_cached"] == 2200
        assert stats["tokens_generated"] == 18000
        assert stats["itineraries_stored"] == 1

    def test_stats_on_empty_db(self):
        stats = store.stats()
        assert stats["total_runs"] == 0
        assert stats["cache_hit_rate"] == 0.0
        assert stats["avg_ms_cached"] is None


class TestJournal:
    def test_journal_roundtrip_keeps_order(self):
        store.save_run("t-journal", make_request())
        store.journal_node("t-journal", "validate_city", 1, {"adcode": "330100"})
        store.journal_node("t-journal", "compose_plan", 3, {"plan": make_plan(days=[])})

        records = store.load_journal("t-journal")
        assert [r["node"] for r in records] == ["validate_city", "compose_plan"]

    def test_same_node_preserves_every_execution(self):
        """重试历史必须保留，否则无法重建计量与执行位置。"""
        store.save_run("t-retry", make_request())
        store.journal_node("t-retry", "compose_plan", 1, {"retries": 1})
        store.journal_node("t-retry", "compose_plan", 2, {"retries": 2})

        records = store.load_journal("t-retry")
        assert len(records) == 2
        assert [r["update"]["retries"] for r in records] == [1, 2]

    def test_load_run_returns_original_request(self):
        request = make_request(city="成都")
        store.save_run("t-run", request)
        loaded = store.load_run("t-run")
        assert loaded is not None
        assert loaded["request"]["city"] == "成都"
        assert loaded["request"]["preferences"] == ["自然风光", "博物馆"]

    def test_load_run_missing(self):
        assert store.load_run("nope") is None


def test_json_default_handles_pydantic_and_fallback():
    """`_json_default` 是入库的最后一道兜底：模型对象转 dict，其余转字符串。"""
    assert store._json_default(make_attraction("西湖"))["name"] == "西湖"
    assert store._json_default(object()) .startswith("<object object at")
    assert json.dumps({"x": WeatherInfo(date="2026-10-10")}, default=store._json_default)
