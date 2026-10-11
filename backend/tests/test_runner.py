"""后台任务与 journal（`services/runner.py`）的测试。

两个最容易出错的地方：
    1. `merge_update` —— 它是**在图外面**复刻 LangGraph 的 reducers。
       复刻错了，断点续跑拼出来的状态就是错的，而且不会有任何报错。
    2. `can_resume_from` —— 它是"能不能续跑"的唯一判据，判宽了会拿错误的起点去跑图。
"""

from app.services import runner, store
from tests.factories import make_plan, make_request


class TestMergeUpdate:
    """reducer 语义必须与 `models/state.py` 保持一致：改一边就要改另一边。"""

    def test_append_lists(self):
        state = {"errors": ["a"], "warnings": ["b"], "tool_calls": [{"t": 1}]}
        merged = runner.merge_update(state, {"errors": ["c"], "warnings": ["d"],
                                            "tool_calls": [{"t": 2}]})
        assert merged["errors"] == ["a", "c"]
        assert merged["warnings"] == ["b", "d"]
        assert len(merged["tool_calls"]) == 2

    def test_merge_sources_dict(self):
        merged = runner.merge_update({"sources": {"attractions": "amap"}},
                                     {"sources": {"weather": "amap"}})
        assert merged["sources"] == {"attractions": "amap", "weather": "amap"}

    def test_sum_usage(self):
        merged = runner.merge_update({"usage": {"prompt_tokens": 100, "llm_calls": 1}},
                                     {"usage": {"prompt_tokens": 200, "llm_calls": 2}})
        assert merged["usage"] == {"prompt_tokens": 300, "llm_calls": 3}

    def test_plain_values_are_replaced(self):
        merged = runner.merge_update({"retries": 1, "adcode": "old"}, {"retries": 2})
        assert merged["retries"] == 2
        assert merged["adcode"] == "old"  # 没提到的键原样保留

    def test_missing_keys_in_state_are_handled(self):
        merged = runner.merge_update({}, {"errors": ["x"], "sources": {"a": "amap"}})
        assert merged == {"errors": ["x"], "sources": {"a": "amap"}}

    def test_none_values_do_not_crash(self):
        merged = runner.merge_update({"errors": None, "sources": None, "usage": None},
                                     {"errors": ["x"], "sources": {"a": "amap"},
                                      "usage": {"llm_calls": 1}})
        assert merged["errors"] == ["x"]
        assert merged["sources"] == {"a": "amap"}
        assert merged["usage"] == {"llm_calls": 1}


class TestCanResumeFrom:
    """V0 只在"两路采集都完成之后"的线性段支持续跑。"""

    def test_nothing_done(self):
        assert runner.can_resume_from([]) is None

    def test_only_city_done_cannot_resume(self):
        assert runner.can_resume_from(["validate_city"]) is None

    def test_only_one_collector_done_cannot_resume(self):
        assert runner.can_resume_from(["validate_city", "collect_attractions"]) is None

    def test_both_collectors_done_resumes_at_compose(self):
        completed = ["validate_city", "collect_attractions", "collect_weather"]
        assert runner.can_resume_from(completed) == "compose_plan"

    def test_after_compose_resumes_at_hotel(self):
        completed = ["validate_city", "collect_attractions", "collect_weather",
                     "compose_plan", "verify_plan"]
        assert runner.can_resume_from(completed) == "resolve_hotel"

    def test_after_hotel_resumes_at_budget(self):
        completed = ["compose_plan", "resolve_hotel"]
        assert runner.can_resume_from(completed) == "patch_budget"

    def test_fully_done_has_nothing_to_resume(self):
        assert runner.can_resume_from(["resolve_hotel", "patch_budget"]) is None


class TestRebuildState:
    def test_rebuild_merges_journal_in_order(self):
        thread_id = "t-rebuild"
        store.save_run(thread_id, make_request())
        store.journal_node(thread_id, "validate_city", 1, {"adcode": "330100"})
        store.journal_node(thread_id, "collect_attractions", 2,
                           {"attractions": [], "warnings": ["候选偏少"]})
        store.journal_node(thread_id, "collect_weather", 3,
                           {"sources": {"weather": "amap"}})

        state, completed = runner.rebuild_state({"adcode": None, "warnings": []}, thread_id)

        assert completed == ["validate_city", "collect_attractions", "collect_weather"]
        assert state["adcode"] == "330100"
        assert state["warnings"] == ["候选偏少"]
        assert state["sources"] == {"weather": "amap"}

    def test_rebuild_keeps_plan_payload(self):
        thread_id = "t-rebuild-plan"
        store.save_run(thread_id, make_request())
        store.journal_node(thread_id, "compose_plan", 4, {"plan": make_plan(days=[]), "retries": 1})

        state, completed = runner.rebuild_state({}, thread_id)
        assert completed == ["compose_plan"]
        assert state["plan"]["city"] == "杭州"
        assert state["retries"] == 1

    def test_rebuild_with_empty_journal(self):
        state, completed = runner.rebuild_state({"adcode": None}, "no-such-thread")
        assert state == {"adcode": None}
        assert completed == []


class TestJobRegistry:
    def test_register_done_job_is_immediately_done(self):
        """缓存命中时走这条路：SSE 不用分叉，照样能立刻收到 done。"""
        job = runner.register_done_job("t-cached", make_request(), {"success": True})
        assert job.status == "done"
        assert runner.get_job("t-cached").result == {"success": True}

    def test_get_unknown_job(self):
        assert runner.get_job("nope") is None

    def test_node_labels_are_chinese(self):
        assert runner.NODE_LABELS["verify_plan"] == "独立复核"
        assert runner.NODE_LABELS["verify_budget"] == "预算复核"
        assert set(runner.NODE_LABELS) == {
            "validate_city", "collect_attractions", "collect_weather",
            "compose_plan", "verify_plan", "resolve_hotel", "patch_budget",
            "verify_budget",
        }
