"""Deterministic online monitor: outcomes, first-attempt health and evidence-backed alerts."""
from collections import Counter
import json
import time

from . import store

# 预检/输入拦截不是"生成失败"：把它们算进 plan_success_rate，会让一个健康服务
# 因为几个非法输入就常亮 PLAN_FAILURE_RATE（实测真实库里 plan_success_rate=0.0645）。
PREFLIGHT_ERROR_CODES = frozenset({
    "CITY_NOT_FOUND", "DATE_RANGE_MISMATCH", "INVALID_REQUEST",
    "CONSTRAINT_INFEASIBLE", "ATTRACTION_NOT_FOUND", "ATTRACTION_AMBIGUOUS",
    "HOTEL_NOT_FOUND", "HOTEL_AMBIGUOUS", "CITY_MISMATCH", "CLARIFICATION_REQUIRED",
})


def _normalize(call):
    """Read historical URL-only ticket traces without inventing HTTP outcomes."""
    call = dict(call)
    warnings = []
    attempts = call.get('attempts') or []
    if call.get('tool') == 'search_ticket_web' and 'http_attempts' not in call:
        evidence = call.get('outcome') or {}
        http = attempts
        status = evidence.get('status')
        outcome = {'status': 'ok' if status == 'ok' else 'error',
                   'error_code': None if status == 'ok' else status or 'UNKNOWN',
                   'stats': {'upstream_calls': len(http), 'lookup_status': status}}
        call.update(outcome=outcome, attempts=[{'outcome': outcome}], http_attempts=http)
        warnings.append('LEGACY_TICKET_TRACE: HTTP outcomes and duration may be unavailable')
    return call, warnings


def _outcome(attempt):
    return attempt.get('outcome') or {}


def record(run_id, final, success, elapsed_ms):
    tools = final.get("tool_calls") or []
    failures = Counter()
    logical = []
    trace_warnings = []
    for call in tools:
        call, notices = _normalize(call)
        trace_warnings.extend(notices)
        attempts = call.get("attempts") or []
        unknown = sum(_outcome(a).get('status') not in ('ok', 'error') for a in attempts)
        if unknown:
            trace_warnings.append(f"{call.get('tool')}: {unknown} attempt outcomes unavailable")
        upstream_attempts = [a for a in attempts if _outcome(a).get('status') in ('ok', 'error')
                             and (_outcome(a).get('stats', {}).get('upstream_calls', 1) or 0) > 0]
        http = call.get('http_attempts') or []
        # Ticket lookup failure is semantic; HTTP health is a separate metric.
        for item in (http if 'http_attempts' in call else upstream_attempts):
            outcome = item.get("outcome") or {}
            if outcome.get("status") == "error":
                failures[outcome.get("error_code") or "UNKNOWN"] += 1
        logical.append({"role": call.get("role"), "tool": call.get("tool"),
                        "tool_call_id": call.get("tool_call_id"), "attempt_count": len(attempts),
                        "first_success": bool(attempts and _outcome(attempts[0]).get('status') == 'ok'),
                        "success": (call.get("outcome") or {}).get("status") == "ok",
                        "stop_reason": call.get("stop_reason"),
                        "upstream_attempt_count": len(upstream_attempts),
                        "upstream_first_success": bool(upstream_attempts and _outcome(upstream_attempts[0]).get('status') == 'ok'),
                        "upstream_success": bool(upstream_attempts and _outcome(upstream_attempts[-1]).get('status') == 'ok'),
                        "unknown_attempt_count": unknown,
                        "http_request_count": len(http),
                        "http_unknown_count": sum(_outcome(a).get('status') not in ('ok', 'error') for a in http),
                        "lookup_error_code": (call.get('outcome') or {}).get('error_code'),
                        "duration_ms": sum(a.get("duration_ms", 0) for a in attempts),
                        "stats": (call.get("outcome") or {}).get("stats", {})})
    report = {"run_id": run_id, "success": success, "elapsed_ms": elapsed_ms,
              "preflight": bool(final.get("error_code") in PREFLIGHT_ERROR_CODES),
              "usage": final.get("usage") or {}, "tools": logical, "errors": dict(failures),
              "trace_warnings": trace_warnings,
              "node_timings": list(final.get("trace") or []),
              "unresolved": bool(final.get("validate_error") or final.get("delivery_errors")),
              "delivery_errors": final.get("delivery_errors") or [], "cached": bool(final.get("cached")),
              "budget": final.get("budget_verdict"),
              "versions": {"contract": store.FINGERPRINT_CONTRACT_VERSION,
                           "prompt": "tourism-requirements-v4", "tools": "tools-v5"}}
    from ..config import get_settings
    report["versions"]["model"] = get_settings().llm_model_id
    with store._connect() as db:
        from .execution_lease import fence
        fence(db)
        db.execute("INSERT INTO quality_run VALUES(?,?,?) ON CONFLICT(run_id) DO UPDATE SET report_json=excluded.report_json",
                   (run_id, json.dumps(report, ensure_ascii=False), store._now()))
        db.execute("INSERT INTO run_snapshot VALUES(?,?,?) ON CONFLICT(run_id) DO NOTHING",
                   (run_id, json.dumps(final, ensure_ascii=False, default=store._json_default), time.time()))


def summary(limit=200):
    with store._connect() as db:
        rows = db.execute("SELECT report_json FROM quality_run ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
    reports = [json.loads(r[0]) for r in rows]
    all_calls = [call for report in reports for call in report["tools"]
                 if call["attempt_count"] > 0 or "retrieval_decision" in call.get("stats", {})
                 or "local_hit" in call.get("stats", {})]
    calls = [c for c in all_calls if c.get("upstream_attempt_count", c["attempt_count"]) > 0]
    failures = Counter()
    for report in reports:
        failures.update(report["errors"])
    denominator = len(calls)
    retry_rate = sum(c.get("upstream_attempt_count", c["attempt_count"]) > 1 for c in calls) / denominator if denominator else 0
    first_success = sum(c.get("upstream_first_success", c["first_success"]) for c in calls) / denominator if denominator else 0
    final_success = sum(c.get("upstream_success", c["success"]) for c in calls) / denominator if denominator else 0
    latencies = sorted(r["elapsed_ms"] for r in reports)
    p95 = latencies[min(len(latencies)-1, int(len(latencies)*0.95))] if latencies else None
    alerts = []
    incomplete_traces = sum(bool(r.get('trace_warnings')) for r in reports)
    if incomplete_traces:
        alerts.append({'code': 'INCOMPLETE_TOOL_TRACE', 'observed': incomplete_traces,
                       'action': '历史工具记录缺少部分调用结果；未将未知的 HTTP 状态统计为成功。'})
    # 生成质量只看真正进入生成流程的请求；预检/输入拒绝单独计数。
    plan_reports = [r for r in reports if not r.get("preflight", False)]
    plan_success = sum(r["success"] for r in plan_reports) / len(plan_reports) if plan_reports else None
    searches = [c for c in all_calls if "returned_count" in c.get("stats", {})]
    retrievals = [c for c in all_calls if "retrieval_decision" in c.get("stats", {})]
    local_hits = sum(bool(c["stats"].get("cache_hit")) for c in retrievals)
    weather_hits = sum(c["tool"] == "get_weather" and bool(c["stats"].get("local_hit")) for c in all_calls)
    empty = sum(c["stats"]["returned_count"] == 0 for c in searches)
    if denominator >= 20 and retry_rate > 0.25:
        alerts.append({"code": "HIGH_RETRY_RATE", "observed": retry_rate, "threshold": 0.25,
                       "action": "按错误码检查限流、网络或服务异常；不要只看最终成功率。"})
    if failures["UPSTREAM_RATE_LIMITED"]:
        alerts.append({"code": "UPSTREAM_RATE_LIMITED", "observed": failures["UPSTREAM_RATE_LIMITED"],
                       "action": "检查请求间隔、账户配额和共享 Key 调用量。"})
    if len(searches) >= 20 and empty / len(searches) > .3:
        alerts.append({"code": "EMPTY_SEARCH_RATE", "observed": empty / len(searches), "threshold": .3,
                       "action": "检查关键词、城市范围、分页和类目过滤；正常返回空结果不等于接口超时。"})
    if len(plan_reports) >= 5 and plan_success is not None and plan_success < .8:
        alerts.append({"code": "PLAN_FAILURE_RATE", "observed": 1-plan_success, "threshold": .2,
                       "action": "检查失败运行的交付校验、数据降级和预算结论，并加入离线回归。"})
    return {"sample_runs": len(reports), "sample_tool_calls": denominator, "window_limit": limit,
            "first_success_rate": first_success, "retry_rate": retry_rate, "final_success_rate": final_success,
            "tool_attempts": sum(c.get("upstream_attempt_count", c["attempt_count"]) for c in calls), "error_codes": dict(failures),
            "logical_tool_calls": len(all_calls), "local_retrieval_calls": len(retrievals),
            "http_requests": sum(c.get('http_request_count', 0) for c in all_calls),
            "http_unknown_outcomes": sum(c.get('http_unknown_count', 0) for c in all_calls),
            "local_retrieval_hits": local_hits, "weather_cache_hits": weather_hits,
            "local_retrieval_hit_rate": local_hits / len(retrievals) if retrievals else None,
            "latency_p95_ms": p95, "alerts": alerts,
            "search_calls": len(searches), "empty_search_calls": empty,
            "plan_success_rate": plan_success,
            "plan_runs": len(plan_reports),
            "preflight_rejections": len(reports) - len(plan_reports),
            "status": "no_data" if not reports else ("degraded" if alerts else "healthy")}
