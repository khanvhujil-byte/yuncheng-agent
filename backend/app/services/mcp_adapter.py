"""Official MCP SDK lifecycle; fixed read-only tool allowlist, validated arguments/results."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import math

import anyio
from jsonschema import Draft202012Validator, ValidationError

from ..config import get_settings
from .amap import AmapError, AmapUnreachable
from . import amap
from .request_gate import AdmissionTimeout
from .tool_retry import remaining_seconds

ALLOWED = {"geocode", "search_poi", "get_weather", "measure_distance", "search_restaurants", "travel_route"}


@asynccontextmanager
async def _transport(settings):
    from mcp import StdioServerParameters
    if settings.mcp_transport == "stdio":
        from mcp.client.stdio import stdio_client
        if not settings.mcp_command:
            raise AmapUnreachable("未配置 MCP 启动命令", retryable=False, code="MCP_CONFIG_ERROR")
        async with stdio_client(StdioServerParameters(command=settings.mcp_command,
                                                      args=settings.mcp_args, env=settings.mcp_env or None)) as streams:
            yield streams[0], streams[1]
    else:
        import httpx
        from urllib.parse import urlparse
        from mcp.client.streamable_http import streamable_http_client
        if not settings.mcp_url.startswith(("http://", "https://")):
            raise AmapUnreachable("未配置 MCP HTTP 地址", retryable=False, code="MCP_CONFIG_ERROR")
        local = urlparse(settings.mcp_url).hostname in {"localhost", "127.0.0.1", "::1"}
        async with httpx.AsyncClient(headers=settings.mcp_headers, timeout=settings.mcp_timeout,
                                     trust_env=not local) as client:
            async with streamable_http_client(settings.mcp_url, http_client=client) as streams:
                yield streams[0], streams[1]


async def _call(name: str, args: dict, timeout: float):
    from mcp import ClientSession
    settings = get_settings()
    with anyio.fail_after(timeout):
        async with _transport(settings) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                await session.initialize()
                discovered = {}
                cursor = None
                for _ in range(10):
                    page = await session.list_tools(cursor=cursor)
                    discovered.update({tool.name: tool for tool in page.tools})
                    cursor = page.nextCursor
                    if not cursor:
                        break
                if name not in discovered:
                    raise AmapUnreachable("MCP 服务没有所需工具", retryable=False, code="MCP_TOOL_NOT_FOUND")
                try:
                    Draft202012Validator({**discovered[name].inputSchema, "additionalProperties": False}).validate(args)
                except ValidationError as exc:
                    raise AmapUnreachable("MCP 工具参数与服务契约不符", retryable=False, code="INVALID_ARGUMENT") from exc
                result = await session.call_tool(name, args)
                if result.isError:
                    raise AmapUnreachable("MCP 工具返回执行错误", retryable=False, code="MCP_TOOL_ERROR")
                payload = result.structuredContent
                if payload is None:
                    blocks = [part.text for part in result.content if getattr(part, "type", None) == "text"]
                    if len(blocks) != 1:
                        raise ValueError("expected one JSON text result")
                    payload = json.loads(blocks[0])
                if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 1_000_000:
                    raise ValueError("MCP result too large")
                if not isinstance(payload, dict) or payload.get("status") not in ("ok", "error"):
                    raise ValueError("MCP result envelope invalid")
                if payload["status"] == "error":
                    code = payload.get("error_code")
                    if code in {"AMAP_KEY_INVALID", "AMAP_QUOTA_EXCEEDED", "INVALID_REQUEST"}:
                        raise AmapError(code, "MCP 地图服务返回配置或请求错误")
                    delay = payload.get("retry_after_seconds")
                    if delay is not None and (isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay < 0 or not math.isfinite(delay)):
                        raise ValueError("invalid retry delay")
                    raise AmapUnreachable("MCP 地图服务查询失败", code=code if code in {
                        "UPSTREAM_TIMEOUT", "UPSTREAM_UNAVAILABLE", "UPSTREAM_RATE_LIMITED"} else "MCP_TOOL_ERROR",
                        retryable=payload.get("retryable") is True, retry_after_seconds=delay)
                return payload["data"]


def _find_exception(exc, classes):
    if isinstance(exc, classes):
        return exc
    for child in getattr(exc, "exceptions", ()):
        found = _find_exception(child, classes)
        if found:
            return found
    return None


def call(name: str, args: dict):
    if name not in ALLOWED:
        raise AmapUnreachable("未授权的 MCP 工具", retryable=False, code="TOOL_FORBIDDEN")
    settings = get_settings()
    remaining = remaining_seconds()
    timeout = min(settings.mcp_timeout, remaining if remaining is not None else settings.mcp_timeout)
    try:
        # The parent gate survives individual stdio server sessions and is shared by workers.
        from time import monotonic
        started = monotonic()
        amap.request_gate.acquire(interval=settings.amap_request_interval_seconds, budget=timeout)
        timeout -= monotonic() - started
        if timeout <= 0:
            raise TimeoutError("MCP time budget exhausted")
        return asyncio.run(_call(name, args, timeout))
    except AdmissionTimeout as exc:
        raise AmapUnreachable("MCP 限流等待超出时间预算", code="UPSTREAM_RATE_LIMITED",
                              retry_after_seconds=exc.wait_seconds) from exc
    except Exception as exc:
        typed = _find_exception(exc, (AmapError, AmapUnreachable))
        if typed:
            if isinstance(typed, AmapUnreachable) and typed.code == "UPSTREAM_RATE_LIMITED":
                amap.request_gate.defer(typed.retry_after_seconds or settings.amap_rate_limit_cooldown_seconds)
            if typed is exc:
                raise
            raise typed from exc
        if _find_exception(exc, (TimeoutError,)):
            raise AmapUnreachable("MCP 调用超时", code="UPSTREAM_TIMEOUT") from exc
        raise AmapUnreachable("MCP 连接或响应不合法", retryable=False, code="MCP_PROTOCOL_ERROR") from exc
