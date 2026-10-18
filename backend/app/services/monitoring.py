"""Deterministic online monitor: outcomes, first-attempt health and evidence-backed alerts."""
from collections import Counter
import json
import time

from . import store


def record(run_id, final, success, elapsed_ms):
    tools = final.get("tool_calls") or []
    failures = Counter()
    logical = []
    for call in tools:
        attempts = call.get("attempts") or []
        for item in attempts:
            outcome = item.get("outcome") or {}
            if outcome.get("status") == "error":
                failures[outcome.get("error_code") or "UNKNOWN"] += 1
        logical.append({"role": call.get("role"), "tool": call.get("tool"),
                        "tool_call_id": call.get("tool_call_id"), "attempt_count": len(attempts),
                        "first_success": bool(attempts and attempts[0]["outcome"]["status"] == "ok"),
                        "success": (call.get("outcome") or {}).get("status") == "ok",
                        "stop_reason": call.get("stop_reason"),
                        "duration_ms": sum(a.get("duration_ms", 0) for a in attempts),
                        "stats": (call.get("outcome") or {}).get("stats", {})})
    report = {"run_id": run_id, "success": success, "elapsed_ms": elapsed_ms,
              "usage": final.get("usage") or {}, "tools": logical, "errors": dict(failures),
              "unresolved": bool(final.get("validate_error") or final.get("delivery_errors")),
              "delivery_errors": final.get("delivery_errors") or [], "cached": bool(final.get("cached")),
              "budget": final.get("budget_verdict"),
              "versions": {"contract": 4, "prompt": "tourism-requirements-v4", "tools": "tools-v4"}}
    from ..config import get_settings
    report["versions"]["model"] = get_settings().llm_model_id
    with store._connect() as db:
        db.execute("INSERT INTO quality_run VALUES(?,?,?) ON CONFLICT(run_id) DO UPDATE SET report_json=excluded.report_json",
                   (run_id, json.dumps(report, ensure_ascii=False), store._now()))
        db.execute("INSERT INTO run_snapshot VALUES(?,?,?) ON CONFLICT(run_id) DO NOTHING",
                   (run_id, json.dumps(final, ensure_ascii=False, default=store._json_default), time.time()))


def summary(limit=200):
    with store._connect() as db:
        rows = db.execute("SELECT report_json FROM quality_run ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
    reports = [json.loads(r[0]) for r in rows]
    calls = [call for report in reports for call in report["tools"] if call["attempt_count"] > 0]
    failures = Counter()
    for report in reports:
        failures.update(report["errors"])
    denominator = len(calls)
    retry_rate = sum(c["attempt_count"] > 1 for c in calls) / denominator if denominator else 0
    first_success = sum(c["first_success"] for c in calls) / denominator if denominator else 0
    final_success = sum(c["success"] for c in calls) / denominator if denominator else 0
    latencies = sorted(r["elapsed_ms"] for r in reports)
    p95 = latencies[min(len(latencies)-1, int(len(latencies)*0.95))] if latencies else None
    alerts = []
    plan_success = sum(r["success"] for r in reports) / len(reports) if reports else None
    searches = [c for c in calls if "returned_count" in c.get("stats", {})]
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
    if len(reports) >= 5 and plan_success < .8:
        alerts.append({"code": "PLAN_FAILURE_RATE", "observed": 1-plan_success, "threshold": .2,
                       "action": "检查失败运行的交付校验、数据降级和预算结论，并加入离线回归。"})
    return {"sample_runs": len(reports), "sample_tool_calls": denominator, "window_limit": limit,
            "first_success_rate": first_success, "retry_rate": retry_rate, "final_success_rate": final_success,
            "tool_attempts": sum(c["attempt_count"] for c in calls), "error_codes": dict(failures),
            "latency_p95_ms": p95, "alerts": alerts,
            "search_calls": len(searches), "empty_search_calls": empty,
            "plan_success_rate": plan_success,
            "status": "no_data" if not reports else ("degraded" if alerts else "healthy")}
