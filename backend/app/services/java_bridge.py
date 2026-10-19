"""与 Java 社区服务的桥接层。

这是**双向联动**里 Python 这一侧的出口：
    - 读热度（`fetch_hot`）：生成行程前问 Java "社区现在热门什么"，用来反哺 prompt
    - 发布行程（`publish_itinerary`）：用户点"发布"时把行程交给 Java 入库

★ 三个硬性约定（跨服务调用最容易做错的地方）：

1. **绝不抛异常**。Java 挂了不该让"生成行程"失败 —— 这里所有函数都返回兜底值，
   并把失败如实反映到调用方能看到的地方（`sources` 里标 degraded），而不是静默吞掉。
2. **必须有超时**。跨进程调用的默认超时往往是无限，一个卡死的下游会把上游线程全占满。
   这里默认 2 秒 —— 热度只是"锦上添花"，不值得为它等。
3. **只传契约字段**。跨语言边界上字段名就是接口，所以这里显式列字段，不做 `**kwargs` 透传
   （透传会让 Python 改个字段名就把 Java 弄崩，而且报错在 Java 侧，排查要跨服务）。
"""

import logging
from copy import deepcopy
from threading import Lock
from time import monotonic
from typing import Any, Optional

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)
_hot_cache: dict[tuple[str, int], tuple[float, object]] = {}
_hot_lock = Lock()


def _base_url() -> str:
    return get_settings().community_base_url.rstrip("/")


def _enabled() -> bool:
    return bool(get_settings().community_enabled)


def fetch_hot(limit: int = 5) -> Optional[dict[str, list[dict[str, Any]]]]:
    """读 Java 的热度统计，用于反哺生成。

    @return `{"city": [{"key": "杭州", "score": 120}], "attraction": [...]}`；
            Java 不可用/未开启时返回 None（**不是抛异常**）。
    """
    if not _enabled():
        return None
    key = (_base_url(), limit)
    with _hot_lock:
        cached = _hot_cache.get(key)
        if cached and cached[0] > monotonic():
            return deepcopy(cached[1])
        result = _fetch_hot(limit)
        _hot_cache[key] = (monotonic() + (300 if result else 30), deepcopy(result))
        return result


def _fetch_hot(limit):
    try:
        response = httpx.get(
            f"{_base_url()}/hot",
            params={"limit": limit},
            timeout=min(get_settings().community_timeout, get_settings().community_hot_timeout),
        )
        response.raise_for_status()
        stats = (response.json() or {}).get("stats") or {}
        total = sum(len(items) for items in stats.values())
        logger.info("已从 Java 读到热度数据 | 共 %d 条 | %s", total,
                    {key: [item.get("key") for item in items[:3]] for key, items in stats.items()})
        return stats or None
    except Exception as exc:  # noqa: BLE001 —— 反哺是加分项，不该拖垮主流程
        logger.warning("读取 Java 热度失败，按无热度数据继续 | %s: %s", type(exc).__name__, exc)
        return None


def publish_itinerary(
    *,
    title: str,
    city: str,
    travel_days: int,
    start_date: str,
    plan: dict[str, Any],
    preferences: Optional[list[str]] = None,
    user_id: int = 1,
    session_token: str = "",
) -> Optional[int]:
    """把行程发布到 Java 社区。

    @param preferences 用户勾选的偏好标签。**必须显式传**：行程 JSON（plan）里没有它，
            而 Java 的热度统计要靠它累积"常见偏好"，那正是反哺生成时最有用的那一路数据。
    @return 新行程 id；失败时返回 None（调用方据此给用户"发布失败，可重试"的提示）。
    """
    if not _enabled():
        return None
    try:
        response = httpx.post(
            f"{_base_url()}/itineraries",
            json={
                "title": title,
                "city": city,
                "travelDays": travel_days,
                "startDate": start_date,
                "preferences": preferences or [],
                "plan": plan,  # 原样透传 TripPlan，Java 不解析内部结构
            },
            headers={"X-Session-Token": session_token},
            # 发布要等 Java 回调 Python 生成摘要，给足时间
            timeout=max(get_settings().community_timeout, 30.0),
        )
        response.raise_for_status()
        itinerary_id = (response.json() or {}).get("itineraryId")
        logger.info("行程已发布到 Java 社区 | id=%s", itinerary_id)
        return int(itinerary_id) if itinerary_id is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("发布到 Java 社区失败 | %s: %s", type(exc).__name__, exc)
        return None


def format_hot_hint(hot: Optional[dict[str, list[dict[str, Any]]]]) -> str:
    """把热度数据渲染成 prompt 里的一段提示。

    刻意写成"参考"而不是"必须"：热度是**引导**，不能盖过用户明确表达的偏好
    （否则就成了"热门绑架用户需求"，那是产品的倒退）。
    """
    if not hot:
        return ""
    parts: list[str] = []
    for label, key in (("热门城市", "city"), ("大家常去的景点", "attraction"),
                       ("常见偏好", "preference")):
        items = [str(item.get("key")) for item in (hot.get(key) or []) if item.get("key")]
        if items:
            parts.append(f"{label}：{'、'.join(items)}")
    if not parts:
        return ""
    return (
        "【社区热度参考（来自已发布行程的真实统计）】\n"
        + "\n".join(f"- {line}" for line in parts)
        + "\n用法：如果用户没有明确偏好，可以优先考虑这些方向；"
        "但**用户明确说了的偏好和必去景点永远优先**，不要为了蹭热度改变用户的要求。"
    )
