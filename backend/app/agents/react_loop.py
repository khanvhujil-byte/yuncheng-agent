"""共用的 ReAct 循环 —— 四个 Agent 都跑它，不各写一遍。

一轮 Agent 的完整过程：

    system（角色规则） + user（任务与数据）
      → 调模型（带上该角色可用的工具清单）
      → 模型要调工具？ → 执行工具 → 结果作为 tool 消息回填 → 再调模型
      → 模型不再要工具 = 本轮结束

三个护栏：
    MAX_STEPS    步数上限，防死循环、控成本；到顶就用收集箱里已有的数据（不崩）
    摘要化       工具只把摘要回填给模型，原始数据进 ctx，不进对话历史
    不冒泡       工具的异常、模型的异常都转成结果并如实记录，绝不崩掉整条请求

执行调度：同轮搜索可并发，但工作线程使用独立 ctx，主线程统一合并。
    酒店搜索先于排序。测距或选择需要模型读取新结果时，回填依赖提示，下一轮再提交。
    实际执行顺序与模型消息顺序分别记录，不能用 future.result() 的取回顺序保证依赖。
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..services.llm import LlmError, LlmUnreachable, chat_with_tools
from ..services.tool_scheduler import execute_batch
from ..services.tools import ToolContext, schemas_for

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 6


@dataclass
class ReactResult:
    text: str = ""
    steps: int = 0
    stop_reason: str = "no_tool_calls"  # no_tool_calls / max_steps / llm_error
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.stop_reason != "llm_error"


def _merge_usage(total: dict[str, int], part: dict[str, int]) -> None:
    for key in ("prompt_tokens", "completion_tokens", "llm_calls"):
        total[key] = total.get(key, 0) + int(part.get(key) or 0)


def run_agent(
    role: str,
    system_prompt: str,
    user_prompt: str,
    ctx: ToolContext,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> ReactResult:
    """跑一轮 Agent。role 决定它手上有什么工具（见 services/tools.py 的 ROLE）。"""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    tools = schemas_for(role)
    usage_total: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0}

    for step in range(1, max_steps + 1):
        try:
            message, usage = chat_with_tools(messages, tools)
        except LlmError as exc:
            ctx.errors.append(f"{exc.code}: {exc.detail}")
            logger.error("Agent(%s) 模型报错 | %s", role, exc)
            return ReactResult(steps=step, stop_reason="llm_error", usage=usage_total)
        except LlmUnreachable as exc:
            ctx.errors.append(f"UPSTREAM_TIMEOUT: {exc}")
            logger.error("Agent(%s) 模型不可达 | %s", role, exc)
            return ReactResult(steps=step, stop_reason="llm_error", usage=usage_total)

        _merge_usage(usage_total, usage)

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            text = message.get("content") or ""
            logger.info("Agent(%s) 结束 | 步数 %d | 无工具调用 | 输出 %d 字符", role, step, len(text))
            return ReactResult(text=text, steps=step, stop_reason="no_tool_calls", usage=usage_total)

        # assistant 消息（含 tool_calls）必须原样放回历史：
        # 少了它，下一轮模型看到自己"没调用过工具"，会重复调用同一个工具。
        messages.append(
            {"role": "assistant", "content": message.get("content") or "", "tool_calls": tool_calls}
        )

        # 先解析，交给调度器区分可并发搜索、确定性依赖和需要下一轮决策的调用。
        pending: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
        for call in tool_calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            try:
                args = json.loads(function.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {"__invalid_arguments__": True}
            except (json.JSONDecodeError, TypeError):
                args = {"__invalid_arguments__": True}
            pending.append((call, name, args))

        results = execute_batch(role, pending, ctx)

        for (call, name, args), result in zip(pending, results):
            messages.append(
                {"role": "tool", "tool_call_id": call.get("id"), "content": result.to_model_content()}
            )

    logger.warning("Agent(%s) 到达步数上限 %d，强制结束（用收集箱里已有的数据）", role, max_steps)
    return ReactResult(steps=max_steps, stop_reason="max_steps", usage=usage_total)
