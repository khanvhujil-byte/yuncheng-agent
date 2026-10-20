"""Warm retrieval avoids model round trips without changing evidence rules."""
import httpx

from app.agents import attraction_agent
from app.api.routes.trip import _initial_state
from app.services import java_bridge, tools
from app.services.node_timing import measured
from tests.factories import make_attraction, make_request


def test_structured_collection_needs_no_model_and_keeps_required_anchors(monkeypatch, settings):
    monkeypatch.setattr(settings, "attraction_use_agent", False)
    monkeypatch.setattr(attraction_agent, "run_agent", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model called")))
    seen = []
    anchor = make_attraction("必去公园", required=True, poi_id="A1")

    def search(keyword, city, **kwargs):
        seen.append(keyword)
        return [make_attraction("普通公园", poi_id="A2"), anchor.model_copy(update={"required": False})]

    monkeypatch.setattr(tools.amap, "search_poi", search)
    request = make_request(preferences=["自然风光", "自然风光", "美食"], must_visit=[anchor.name])
    state = {**_initial_state(request), "adcode": "330100", "must_visit_resolved": [anchor]}
    result = attraction_agent.collect_attractions(state)
    assert sorted(seen) == ["景点", "自然风光"]
    assert result["usage"]["llm_calls"] == 0
    assert next(a for a in result["attractions"] if a.poi_id == "A1").required
    assert len(result["attractions"]) == 2
    assert result["tool_calls"][0]["outcome"]["status"] == "ok"


def test_collection_preserves_typed_nonretryable_failure(monkeypatch, settings):
    monkeypatch.setattr(settings, "attraction_use_agent", False)

    def failed(*args, **kwargs):
        raise tools.amap.AmapError("AMAP_KEY_INVALID", "无效Key")

    monkeypatch.setattr(tools.amap, "search_poi", failed)
    result = attraction_agent.collect_attractions(_initial_state(make_request(preferences=["自然风光"], must_visit=[])))
    assert result["sources"]["attractions"] == "degraded"
    call = result["tool_calls"][0]
    assert call["attempt_count"] == 1
    assert call["outcome"]["error_code"] == "AMAP_KEY_INVALID"
    assert call["outcome"]["retryable"] is False
    assert result["warnings"]


def test_optional_community_failure_cools_down_and_retries_after_expiry(monkeypatch):
    now = [10.0]
    calls = []
    monkeypatch.setattr(java_bridge, "monotonic", lambda: now[0])

    def failed(*args, **kwargs):
        calls.append(kwargs["timeout"])
        raise httpx.ReadTimeout("unavailable")

    monkeypatch.setattr(java_bridge.httpx, "get", failed)
    assert java_bridge.fetch_hot() is None
    assert java_bridge.fetch_hot() is None
    assert calls == [0.35]
    now[0] += 31
    assert java_bridge.fetch_hot() is None
    assert len(calls) == 2


def test_optional_community_cache_does_not_alias_mutable_results(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"stats": {"city": [{"key": "杭州", "score": 1}]}}

    monkeypatch.setattr(java_bridge.httpx, "get", lambda *a, **k: Response())
    first = java_bridge.fetch_hot()
    first["city"][0]["score"] = 999
    assert java_bridge.fetch_hot()["city"][0]["score"] == 1


def test_node_timings_record_separate_repeated_executions(monkeypatch):
    from app.services import node_timing
    times = iter([1, 1.5, 10, 10.1])
    monkeypatch.setattr(node_timing, "perf_counter", lambda: next(times))
    call = measured("compose_plan", lambda s: {"plan": {"days": []}})
    first = call({"trace": []})
    second = call({"trace": first["trace"]})
    assert first["trace"][0]["elapsed_ms"] == 500
    assert second["trace"][0]["elapsed_ms"] == 100
    assert first["trace"][0]["step"] == 1
    assert second["trace"][0]["step"] == 2
