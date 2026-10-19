"""Regression tests for the 2026-10-19 hardening fixes.

Covers:
    - stale background jobs recovered after a process restart
    - cache entries that fail re-audit are deleted instead of served forever
    - request size limits (city / free text / preference count)
    - monitoring no longer counts preflight/input rejections as planning failures
    - bounded replan entry for comfort repair
"""
import copy
import time

import pytest
from pydantic import ValidationError

from app.agents.graph import route_after_budget
from app.models.schemas import TripRequest
from app.services import monitoring, planning, store


def _request(**kw) -> TripRequest:
    base = {"city": "杭州", "start_date": "2026-10-20", "travel_days": 3,
            "transportation": "公共交通", "accommodation": "经济型酒店"}
    base.update(kw)
    return TripRequest(**base)


# ---------------------------------------------------------------- stale jobs
def test_stale_running_job_is_marked_interrupted_on_startup():
    store.save_job("stale-thread", status="running", events=[], started_at=time.time() - 60)
    assert store.load_job("stale-thread")["status"] == "running"
    recovered = store.mark_stale_jobs()
    assert recovered == 1
    job = store.load_job("stale-thread")
    assert job["status"] == "error"
    assert "中断" in job["error"]
    # 已经结束的任务不会被改写
    store.save_job("done-thread", status="done", events=[], result={"success": True}, started_at=time.time())
    assert store.mark_stale_jobs() == 0
    assert store.load_job("done-thread")["status"] == "done"


# ---------------------------------------------------------------- cache purge
def test_cached_entry_is_deleted_when_reaudit_fails(monkeypatch):
    req = _request()
    spot = {"name": "西湖", "poi_id": "a1",
            "location": {"longitude": 120.15, "latitude": 30.25}, "visit_duration": 120}
    plan = {"city": req.city, "start_date": req.start_date, "end_date": "2026-10-22",
            "days": [{"date": req.start_date, "day_index": 0, "attractions": [spot], "meals": []},
                     {"date": "2026-10-21", "day_index": 1,
                      "attractions": [dict(spot)], "meals": []}],
            "warnings": [], "sources": {}}
    fingerprint = store.fingerprint_of(req)
    store.save_itinerary(fingerprint, req, plan)
    hit = store.find_itinerary(fingerprint)
    assert hit is not None

    # Keep this test offline and deterministic: the cache-refresh path normally
    # re-queries the map/weather (blocked in tests), so inject the shifted plan.
    monkeypatch.setattr(planning, "_reuse_with_new_dates", lambda saved, request: copy.deepcopy(saved))
    response = planning._cached_response(hit, req, "thread-cache", fingerprint, 0)

    assert response.success is False
    assert store.find_itinerary(fingerprint) is None, "复核不通过的缓存必须被删除，不能继续命中"


# ---------------------------------------------------------------- input limits
def test_oversized_request_fields_are_rejected_with_422_style_validation():
    with pytest.raises(ValidationError):
        _request(city="杭" * 200)
    with pytest.raises(ValidationError):
        _request(free_text_input="x" * 30000)
    with pytest.raises(ValidationError):
        _request(preferences=[f"偏好{i}" for i in range(21)])
    # 边界内的输入仍然可用
    ok = _request(preferences=[f"偏好{i}" for i in range(20)], free_text_input="x" * 20000)
    assert len(ok.preferences) == 20


# ---------------------------------------------------------------- monitoring
def test_preflight_rejections_do_not_lower_plan_success_rate():
    monitoring.record("preflight-1", {"error_code": "CITY_NOT_FOUND"}, False, 5)
    monitoring.record("preflight-2", {"error_code": "DATE_RANGE_MISMATCH"}, False, 5)
    monitoring.record("generated-1", {"tool_calls": []}, True, 500)
    metrics = monitoring.summary()
    assert metrics["plan_success_rate"] == 1.0
    assert metrics["preflight_rejections"] == 2
    assert metrics["plan_runs"] == 1


# ---------------------------------------------------------------- replan bounds
def test_comfort_replan_has_its_own_bounded_counter():
    state = {"unified_planning": True, "experience_enabled": True,
             "composition_valid": True, "validate_error": "舒适度超限",
             "repair_rounds": 0, "budget_retries": 0,
             "planning_calls": 2, "planning_started_at": time.time(),
             "plan": {"days": [{}]}}
    assert route_after_budget(state) == "repair"
    # 用尽体验修复名额后不再重排；预算重排名额仍然是独立的。
    state["repair_rounds"] = 2
    assert route_after_budget(state) == "next"
    state.update(validate_error="预算复核不通过：known_exceeded", budget_verdict={"status": "known_exceeded"})
    assert route_after_budget(state) == "retry"
