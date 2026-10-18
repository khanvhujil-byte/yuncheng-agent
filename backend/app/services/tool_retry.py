"""Bounded retries for explicitly safe tools; no model calls inside this layer."""
from contextvars import ContextVar
from dataclasses import dataclass, replace
from time import monotonic, sleep
from typing import Callable

from .tool_result import ToolResult, next_action

# Each parallel worker owns its deadline. None keeps direct adapter calls unchanged.
deadline: ContextVar[float | None] = ContextVar("tool_deadline", default=None)


def remaining_seconds() -> float | None:
    end = deadline.get()
    return None if end is None else max(0.0, end - monotonic())


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2  # Includes the initial attempt.
    budget_seconds: float = 20.0
    base_delay: float = 0.25


@dataclass
class ExecutionReport:
    result: ToolResult
    attempts: list[dict]
    stop_reason: str

    @property
    def next_action(self) -> str:
        if self.result.status == "error" and self.result.retryable:
            return "report_or_change_query"
        return next_action(self.result)


def run(attempt: Callable[[], ToolResult], *, safe: bool, policy: RetryPolicy) -> ExecutionReport:
    started = monotonic()
    end = started + policy.budget_seconds
    attempts = []
    token = deadline.set(end)
    try:
        for number in range(1, policy.max_attempts + 1):
            before = monotonic()
            result = attempt()
            attempts.append({"attempt": number, "duration_ms": round((monotonic() - before) * 1000, 3),
                             "outcome": result.to_dict()})
            if result.status == "ok":
                reason = "success"
                break
            if not result.retryable:
                reason = "non_retryable"
                break
            if not safe:
                reason = "unsafe_tool"
                break
            if number == policy.max_attempts:
                reason = "attempt_limit"
                break
            delay = max(policy.base_delay * (2 ** (number - 1)), result.retry_after_seconds or 0.0)
            if monotonic() + delay >= end:
                reason = "time_budget"
                break
            sleep(delay)
            if monotonic() >= end:
                reason = "time_budget"
                break
        if result.status == "error" and result.retryable:
            # Keep the actual upstream code/retryability. Exhaustion is a runtime fact.
            explanation = {"attempt_limit": "已达尝试次数上限", "time_budget": "已用尽查询时间预算",
                           "unsafe_tool": "该操作不能自动重复执行"}[reason]
            result = replace(result, message=result.message + f" 已尝试 {len(attempts)} 次，{explanation}。")
        return ExecutionReport(result, attempts, reason)
    finally:
        deadline.reset(token)
