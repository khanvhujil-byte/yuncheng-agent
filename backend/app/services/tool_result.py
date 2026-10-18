"""Uniform result contract for every tool outcome; domain data stays in ToolContext."""
import json
from dataclasses import asdict, dataclass, field
from typing import Literal


@dataclass(frozen=True)
class ToolResult:
    status: Literal["ok", "error"]
    message: str
    error_code: str | None = None
    retryable: bool = False
    stats: dict[str, int] = field(default_factory=dict)
    retry_after_seconds: float | None = None

    @classmethod
    def success(cls, message: str, **stats: int) -> "ToolResult":
        return cls(status="ok", message=message, stats=stats)

    @classmethod
    def failure(cls, message: str, error_code: str, *, retryable: bool = False,
                retry_after_seconds: float | None = None) -> "ToolResult":
        return cls(status="error", message=message, error_code=error_code, retryable=retryable,
                   retry_after_seconds=retry_after_seconds)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_model_content(self) -> str:
        """Tool-message content remains a string, with only compact result metadata."""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


def next_action(result: ToolResult) -> str:
    """A routing hint; retry remains subject to the runtime's attempt/time limits."""
    if result.status == "error":
        if result.retryable:
            return "retry_if_budget_allows"
        if result.error_code == "TOOL_DEPENDENCY":
            return "wait_next_turn"
        return "report_or_correct"
    if result.stats.get("returned_count") == 0:
        return "review_query_if_needed"
    if result.stats.get("added_count") == 0:
        return "check_candidate_coverage"
    return "continue"
