"""AI 能力接口 —— 对外开放给 Java 社区服务调用。

这是**双向联动**里 Java → Python 的那一半：Java 发布行程时回调这里生成一句简介。

为什么这个接口必须有兜底：
    "发布行程"是 Java 那边的主流程，不能因为"摘要没生成出来"就让发布失败。
    所以无论大模型是否可用，这里都返回一句话，并用 `source` 如实标出它是怎么来的
    （`llm` 还是 `fallback`）—— 降级可见，是贯穿整个项目的约定。
"""

import json
import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ...services.llm import chat_text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["AI 能力（供 Java 社区服务调用）"])

SYSTEM_PROMPT = """你在为一份旅行行程写"社区简介"，供别人在列表里快速判断要不要看。
要求：
1. 只写一句话，30~50 个字，不要标题、不要换行、不要引号。
2. 突出这条行程**与众不同的地方**（主题、节奏、适合谁），不要罗列所有景点。
3. 只用给定数据里出现的事实，不要添加没提到的景点、价格或天气。
4. 直接输出那句话本身，不要"这条行程……"这样的开头。"""


class SummaryRequest(BaseModel):
    """Java 侧传来的行程概要。字段名与 Java 的 PublishRequest 对齐（snake_case）。"""

    city: str = Field(..., description="城市")
    travel_days: int = Field(..., ge=1, le=30, description="天数")
    plan_json: str = Field("", description="完整行程 JSON（Java 原样透传）")


def _extract_facts(plan_json: str) -> tuple[list[str], int]:
    """从行程 JSON 里抽出景点名与天数 —— 兜底摘要用，也用来给模型当输入。"""
    try:
        plan = json.loads(plan_json) if plan_json else {}
    except (TypeError, ValueError):
        return [], 0
    names: list[str] = []
    days = plan.get("days") or []
    for day in days:
        for spot in (day.get("attractions") or []):
            name = (spot or {}).get("name")
            if name:
                names.append(str(name))
    return names, len(days)


def _fallback_summary(request: SummaryRequest, names: list[str], days: int) -> str:
    """不花 token 的兜底：只用行程里已有的事实拼一句话。"""
    day_count = days or request.travel_days
    if not names:
        return f"{request.city}{day_count}天行程，{day_count}天节奏安排。"
    head = "、".join(names[:3])
    tail = f"等 {len(names)} 个点位" if len(names) > 3 else ""
    return f"{request.city}{day_count}天，走{head}{tail}，节奏适中。"


@router.post("/summary", summary="生成行程社区简介（供 Java 回调）")
def summary(request: SummaryRequest) -> dict:
    """把一份行程压成一句简介。

    返回：`{summary, source, usage}`；`source` 为 `llm` 或 `fallback`。
    """
    names, days = _extract_facts(request.plan_json)
    fallback = _fallback_summary(request, names, days)

    user_prompt = (
        f"城市：{request.city}\n"
        f"天数：{request.travel_days}\n"
        f"行程里的点位：{'、'.join(names) if names else '（未提供）'}\n"
        f"请写一句社区简介。"
    )
    try:
        # 注意用 chat_text 而不是 chat_json：摘要要的是自由文本，
        # 强制 JSON 模式会被模型端直接拒（HTTP 400，"Prompt must contain the word 'json'"）。
        text, usage = chat_text(SYSTEM_PROMPT, user_prompt, max_tokens=120)
        summary = (text or "").strip().strip('"').replace("\n", " ").strip()
        if not summary:
            raise ValueError("模型返回空摘要")
        return {"summary": summary[:120], "source": "llm", "usage": usage}
    except Exception as exc:  # noqa: BLE001 —— 主流程不能被摘要失败拖垮
        logger.warning("摘要生成失败，使用兜底文案 | %s: %s", type(exc).__name__, exc)
        return {
            "summary": fallback,
            "source": "fallback",
            "reason": f"{type(exc).__name__}: {exc}"[:200],
        }


@router.get("/ping", summary="供 Java 侧探测连通性")
def ping() -> dict:
    return {"pong": True, "service": "trip-planner-agent"}
