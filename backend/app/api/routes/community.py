"""社区联动接口（Python 这一侧对外的出口）。

前端点"发布到社区"走这里 → 本服务把行程转交给 **Java 社区服务**入库。
Python 依然**不碰 MySQL**：它只负责把契约传过去（"数据库连接在 Java"这条边界的落地）。

同时暴露一个读接口，让前端能直接展示"社区现在热什么"。
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ...config import get_settings
from ...services import java_bridge
from ..security import rate_limit, require_api_key

logger = logging.getLogger(__name__)

# 和 trip 路由同样的访问控制：鉴权与限流默认关，生产显式打开
router = APIRouter(
    prefix="/community",
    tags=["社区（与 Java 服务联动）"],
    dependencies=[Depends(require_api_key), Depends(rate_limit)],
)


class PublishPayload(BaseModel):
    """要发布的行程。`plan` 就是生成接口返回的那份 TripPlan，原样转给 Java。"""

    plan: dict[str, Any] = Field(..., description="完整行程（TripPlan 结构）")
    title: Optional[str] = Field(None, description="标题；不填用「城市 + 天数」拼")


@router.post("/publish", summary="把行程发布到 Java 社区")
def publish(payload: PublishPayload) -> dict:
    """发布行程。

    返回 `{success, itinerary_id, message}`。
    **失败不是 500**：Java 不可用是一个可预期的运维状况，前端应该能显示"稍后重试"，
    而不是弹一个「服务器错误」（这与项目既有的"部分结果也返回 200"口径一致）。
    """
    plan = payload.plan or {}
    city = str(plan.get("city") or "").strip()
    days = plan.get("days") or []
    start_date = str(plan.get("start_date") or "")

    if not city or not days:
        return {"success": False, "itinerary_id": None,
                "message": "行程缺少 city 或 days，无法发布。"}

    title = payload.title or f"{city}{len(days)}天行程"
    itinerary_id = java_bridge.publish_itinerary(
        title=title,
        city=city,
        travel_days=len(days),
        start_date=start_date,
        plan=plan,
    )
    if itinerary_id is None:
        return {"success": False, "itinerary_id": None,
                "message": "发布失败：社区服务暂时不可用，请稍后重试。"}
    return {"success": True, "itinerary_id": itinerary_id,
            "message": f"已发布到社区（#{itinerary_id}）。"}


@router.get("/hot", summary="社区热度（来自 Java 的真实统计）")
def hot(limit: int = 5) -> dict:
    """给前端展示"社区在热什么"。读不到就如实说明，不编数据。"""
    settings = get_settings()
    stats = java_bridge.fetch_hot(limit)
    return {
        "enabled": settings.community_enabled,
        "available": stats is not None,
        "stats": stats or {},
        "message": "" if stats else "社区服务暂时不可用（热度数据来自 Java 侧统计）。",
    }
