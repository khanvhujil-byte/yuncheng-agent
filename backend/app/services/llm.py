"""DeepSeek 客户端（**直接 HTTP**，两种调用形态）。全项目唯一和模型说话的地方。

为什么不走第三方封装（实测记录，2026-10-10）：
    langchain-openai 1.6.2 与 openai SDK 3.16.2 组合下，`ChatOpenAI(...)` **构造即挂死**
    （假 Key、纯本地构造也一样，不是网络问题）。而直连 `POST /chat/completions`
    用 httpx 一次就通。我们只需要两种能力，自己写更可控、更好排查。

两种调用形态：
    chat_json()        —— 不带工具，要 JSON 对象（结构化输出）。给 compose_plan 用。
    chat_with_tools()  —— 带工具，返回助手消息（可能含 tool_calls）。给 ReAct 循环用。

错误分两类（和 services/amap.py 同一套理念）：
    LlmError        鉴权/余额/限流/参数 —— 确定性错误，该明确告知
    LlmUnreachable  超时/连不上/响应非 JSON —— 网络类，该降级
"""

import logging
from typing import Any

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)

CHAT_PATH = "/chat/completions"

STATUS_TO_ERROR = {
    400: "LLM_ERROR",  # 参数/请求体问题（比如不支持 response_format 或 tools）
    401: "LLM_AUTH_ERROR",  # Key 无效
    402: "LLM_QUOTA_EXCEEDED",  # 余额不足
    429: "LLM_RATE_LIMITED",  # 触发限流
    500: "LLM_ERROR",
    503: "LLM_ERROR",
}


class LlmError(RuntimeError):
    """模型服务明确回了错误 —— 该拦住或明确告知用户。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"[{code}] {detail}")
        self.code = code
        self.detail = detail


class LlmUnreachable(RuntimeError):
    """网络问题 / 超时 / 响应不是 JSON —— 该降级。"""


def _post(body: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
    """唯一发请求的地方：拼鉴权、超时、状态码分类、JSON 解析。"""
    settings = get_settings()
    try:
        response = httpx.post(
            settings.llm_base_url.rstrip("/") + CHAT_PATH,
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=timeout if timeout is not None else settings.llm_timeout,
        )
    except httpx.HTTPError as exc:
        raise LlmUnreachable(f"{type(exc).__name__}: {exc}") from exc

    if response.status_code != 200:
        raise LlmError(
            STATUS_TO_ERROR.get(response.status_code, "LLM_ERROR"),
            f"HTTP {response.status_code}: {response.text[:200]}",
        )

    try:
        return response.json()
    except ValueError as exc:
        raise LlmUnreachable(f"响应不是 JSON：{exc}") from exc


def _usage_of(payload: dict[str, Any]) -> dict[str, int]:
    raw = payload.get("usage") or {}
    return {
        "prompt_tokens": int(raw.get("prompt_tokens") or 0),
        "completion_tokens": int(raw.get("completion_tokens") or 0),
        "llm_calls": 1,
    }


def chat_json(system_prompt: str, user_prompt: str, *, extra_user: str = "", timeout: float | None = None, max_tokens: int | None = None) -> tuple[str, dict[str, int]]:
    """要一个 JSON 对象。返回 (文本, usage)。不做解析 —— 校验是调用方的事。"""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    if extra_user:
        messages.append({"role": "user", "content": extra_user})

    body = {
            "model": get_settings().llm_model_id,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
    if max_tokens is not None:
        body['max_tokens'] = max_tokens
    payload = _post(body, timeout=timeout) if timeout is not None else _post(body)
    choices = payload.get("choices") or [{}]
    text = (choices[0].get("message") or {}).get("content") or ""
    usage = _usage_of(payload)
    logger.info("chat_json 完成 | 输出 %d 字符 | tokens=%s", len(text), usage)
    return text, usage


def chat_text(system_prompt: str, user_prompt: str, *, max_tokens: int | None = None) -> tuple[str, dict[str, int]]:
    """要一段**自由文本**。返回 (文本, usage)。

    和 `chat_json` 的区别只在一个字段：不传 `response_format`。
    ★ 这个函数是被一个真 bug 逼出来的：我们曾经用 `chat_json` 去要"一句话简介"，
      结果 DeepSeek 直接返回 HTTP 400 ——
      `Prompt must contain the word 'json' in some form to use 'response_format' of type 'json_object'`。
      也就是说：**强制 JSON 模式的接口不能拿来做自由文本生成**，
      哪怕输出很短也一样。（当时因为上层有兜底，发布流程没挂，但摘要一直是兜底文案。）
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    body: dict[str, Any] = {
        "model": get_settings().llm_model_id,
        "messages": messages,
        "temperature": 0,
    }
    if max_tokens:
        body["max_tokens"] = max_tokens

    payload = _post(body)
    choices = payload.get("choices") or [{}]
    text = (choices[0].get("message") or {}).get("content") or ""
    usage = _usage_of(payload)
    logger.info("chat_text 完成 | 输出 %d 字符 | tokens=%s", len(text), usage)
    return text, usage


def chat_with_tools(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, int]]:
    """ReAct 循环用：发 messages（system/user/assistant/tool 混合）+ 工具清单。

    返回 (助手消息 dict, usage)。助手消息里**可能带 tool_calls**：
        带 → 循环要执行工具并把结果作为 tool 消息回填
        不带 → 本轮是最终回答，循环结束
    注意 messages 必须是纯 dict（我们走原始 HTTP，不经过任何消息类）。
    """
    body: dict[str, Any] = {
        "model": get_settings().llm_model_id,
        "messages": messages,
        "temperature": 0,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    payload = _post(body)
    choices = payload.get("choices") or [{}]
    message = choices[0].get("message") or {}
    usage = _usage_of(payload)
    logger.info(
        "chat_with_tools 完成 | tool_calls=%d | tokens=%s",
        len(message.get("tool_calls") or []),
        usage,
    )
    return message, usage
