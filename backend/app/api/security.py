"""访问控制与限流。

为什么这两件事必须有（也是"玩具"和"能上线"最直观的分界线）：
    鉴权 —— 没有它，任何人都能调你的接口，烧的是**你的**模型余额与高德配额。
    限流 —— 没有它，一个脚本狂点就能把配额打光；限流既保护上游，也保护钱包。

两个刻意的设计决定：

    1. **默认关闭**：`API_KEY` 为空时不校验、`RATE_LIMIT_PER_MINUTE<=0` 时不限流。
       理由：本地开发与 CI 不该被迫配密钥。**但生产必须显式打开** ——
       所以启动时会打印一条明确的警告（见 main.py），而不是默默放行。

    2. **限流用进程内滑动窗口**：单副本够用，零依赖。
       多副本部署时要换成 Redis 计数器（注释写在这里，不假装它是分布式的）。
"""

import logging
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import Header, HTTPException, Request, status

from ..config import get_settings

logger = logging.getLogger(__name__)

# 每个客户端的请求时间戳队列（滑动窗口）
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()

_warned_no_key = False


def client_key(request: Request) -> str:
    """按客户端标识限流。

    取 X-Forwarded-For 的第一段（经 nginx 反代时才有），否则用直连 IP。
    注意：X-Forwarded-For 可以被伪造 —— 在 nginx 后面部署时要确保它被覆盖写
    （deploy/nginx.conf 里用的是 $remote_addr，不是 $proxy_add_x_forwarded_for）。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and get_settings().trust_proxy_headers:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def require_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
) -> None:
    """校验 API Key。支持 `X-API-Key: <key>` 或 `Authorization: Bearer <key>`。"""
    global _warned_no_key

    settings = get_settings()
    expected = (settings.api_key or "").strip()
    if settings.deployment_mode == 'production':
        if request.url.path in ('/api/auth/login', '/api/auth/register'):
            return
        from ..services.identity import current_user
        current_user(request.headers.get('x-session-token'))
        return
    if request.url.path == '/api/auth/community':
        from ..services.identity import current_user
        current_user(request.headers.get('x-session-token'))
        return
    if not expected:
        if not _warned_no_key:
            logger.warning(
                "鉴权未开启（API_KEY 为空）：任何人都能调用本服务，只适合本地开发。"
                "生产请在 .env 里设置 API_KEY。"
            )
            _warned_no_key = True
        return

    provided = (x_api_key or "").strip()
    if not provided and authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()

    # compare_digest：避免用 == 比较密钥时泄露长度/前缀信息（时序攻击）
    if not provided or not secrets.compare_digest(provided, expected):
        logger.warning("鉴权失败 | client=%s path=%s", client_key(request), request.url.path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"success": False, "message": "缺少或错误的 API Key。", "error_code": "UNAUTHORIZED"},
        )


async def rate_limit(request: Request) -> None:
    """滑动窗口限流：默认每分钟 N 次（N<=0 表示关闭）。"""
    settings = get_settings()
    limit = int(settings.rate_limit_per_minute or 0)
    if limit <= 0:
        return

    now = time.time()
    key = client_key(request)
    if settings.deployment_mode == 'production' and request.url.path not in ('/api/auth/login', '/api/auth/register'):
        from ..services.identity import current_user
        user = current_user(request.headers.get('x-session-token'))
        read_only = request.method in ('GET', 'HEAD')
        key = 'account:' + user + (':read' if read_only else ':write')
        if read_only:
            limit = settings.read_rate_limit_per_minute
    with _lock:
        window = _hits[key]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= limit:
            retry_after = max(1, int(60 - (now - window[0])))
            logger.warning("触发限流 | client=%s 窗口内 %d 次（上限 %d）", key, len(window), limit)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "success": False,
                    "message": f"请求过于频繁，请 {retry_after} 秒后重试。",
                    "error_code": "RATE_LIMITED",
                },
                headers={"Retry-After": str(retry_after)},
            )
        window.append(now)


def reset_rate_limit_state() -> None:
    """清空限流窗口（测试用：避免用例之间互相影响）。"""
    with _lock:
        _hits.clear()
