"""Agent 3 / 4 · 住宿顾问（resolve_hotel 节点）—— v0.3，带 4 个工具。

为什么它在编排之后（v0.2 的方案 D）：只有编排完才知道每天去哪几个景点，
"选哪家酒店"这个问题才有确定的自变量。上一版它和景点并行，只能拿市中心近似。

四个工具的分工（这是本节点最重要的架构点）：
    search_hotels    取候选（内部映射关键词、只留住宿类目）
    rank_hotels      **加权 1-median 数学在工具里**（按各景点建议游览时长加权）
    measure_distance 真实驾车距离（高德接口，带缓存）—— 给 Agent 用来核实冠军
    choose_hotel     把决定**落进收集箱**，而不是让模型写在自然语言里再被解析

最后一件事很关键：Agent 的"决定"也走工具。否则节点得去解析模型的自由文本，
一旦它说"我选 A 吧，不过 B 也不错"，解析就崩了。工具是结构化的出口。
"""

import logging
from copy import deepcopy

from pydantic import ValidationError

from ..models.schemas import Attraction
from ..models.state import TripState
from ..services.tools import ToolContext, execute
from .prompts import HOTEL_AGENT_PROMPT
from .react_loop import run_agent

logger = logging.getLogger(__name__)

MAX_STEPS = 5  # 搜候选 1 + 排序 1 + 实测 1 + 决定 1，留 1 步余量。
# ↑ 从 6 收到 5 的原因（实测）：住宿 Agent 一旦开始"多试几家/反复实测"，
#   这一步会从十几秒涨到近百秒，成为整条链路的瓶颈（SSE 逐节点进度让它第一次可见）。
#   步数上限是兜底：真跑歪了也不会把端到端拖到两分钟。


def _user_prompt(state: TripState, nights: int) -> str:
    req = state["request"]
    plan = state.get("plan") or {}
    names = [
        str(spot.get("name") or "")
        for day in (plan.get("days") or [])
        for spot in (day.get("attractions") or [])
    ]
    spots = "、".join(name for name in names if name) or "（无景点信息）"
    lines = [
        f"城市：{req.city}",
        f"住宿偏好：{req.accommodation}",
        f"住 {nights} 晚",
        f"用户已订酒店：{req.fixed_hotel or '（无，请按偏好自选一家）'}",
    ]
    if req.fixed_hotel_address:
        lines.append(f"已订酒店地址（消歧用）：{req.fixed_hotel_address}")
    lines.append(f"行程景点（选址权衡用）：{spots}")
    from ..services.requirements import context, policy
    lines.append("优先检索关键词：" + "、".join(policy(req).hotel.keywords))
    return "\n".join(lines) + context(req, "hotel", "transport", "food", "budget")


def _degraded(reason: str, errors: list[str], calls: list[dict]) -> dict:
    return {
        "hotel": None,
        "hotel_nights": 0,
        "hotel_source": "degraded",
        "warnings": [reason],
        "errors": errors,
        "sources": {"hotel": "degraded"},
        "tool_calls": calls,
    }


def resolve_hotel(state: TripState) -> dict:
    req = state["request"]
    nights = state["nights"]
    plan = state.get("plan") or {}

    if nights <= 0:
        return {"hotel": None, "hotel_source": "agent", "hotel_reason": "本次不住宿。", "sources": {}}

    anchor = state.get("hotel_anchor")
    if req.fixed_hotel:
        if anchor is None:
            return _degraded("指定酒店尚未解析，不能用其他酒店替代。", ["HOTEL_ANCHOR_MISSING"], [])
        hotel = deepcopy(anchor)
        from ..services.price_catalog import apply_prices
        hotel = apply_prices(req.city, "hotel", [hotel], service_date=req.start_date, nights=nights)[0]
        from ..models.schemas import ConstraintReport
        report = state.get("constraint_report") or ConstraintReport()
        report = ConstraintReport.model_validate(report).model_copy(update={"hotel_source": "user", "hotel_anchor": hotel.name})
        return {"hotel": hotel, "hotels": [hotel], "hotel_source": "user",
                "constraint_report": report,
                "hotel_reason": "使用用户指定且已按名称与地址解析的酒店。",
                "sources": {"hotel": "user"},
                "warnings": ["指定酒店的实际房价未知，预算中保留未知项。"] if not hotel.estimated_cost else []}

    ctx = ToolContext(
        city=req.city,
        adcode=state.get("adcode"),
        start_date=req.start_date,
        travel_days=state["travel_days"],
        nights=nights,
        requirements=req.requirements,
    )
    # 把行程里的景点放进收集箱 —— rank_hotels / measure_distance 都要用它们
    for day in plan.get("days") or []:
        for raw in day.get("attractions") or []:
            try:
                spot = Attraction.model_validate(raw)
            except ValidationError:
                continue
            ctx.attractions[spot.poi_id or spot.name] = spot

    result = run_agent("hotels", HOTEL_AGENT_PROMPT, _user_prompt(state, nights), ctx, max_steps=MAX_STEPS)

    warnings: list[str] = []
    hotel = ctx.chosen_hotel
    from ..services.requirements import hotel_allowed
    if hotel is not None and not hotel_allowed(hotel, req.requirements.hotel):
        hotel = None
    ctx.hotels = {k: h for k, h in ctx.hotels.items() if hotel_allowed(h, req.requirements.hotel)}
    ctx.ranked = [name for name in ctx.ranked if any(h.name == name for h in ctx.hotels.values())]
    if hotel is None:
        # 兜底：Agent 没给出最终选择（或步数用尽）→ 用排序第一名顶上
        if not ctx.ranked:
            execute("hotels", "rank_hotels", {}, ctx)
        hotel = next((h for h in ctx.hotels.values() if h.name == (ctx.ranked[0] if ctx.ranked else "")), None)
        if hotel is None:
            return _degraded("住宿数据获取失败，未能确定酒店。", list(ctx.errors), ctx.calls)
        warnings.append("住宿助手没有给出最终选择，已按加权距离排序取第一名。")

    # 来源标记：用户指定过的标 user（前端显示「你指定的」），其余是模型估算价
    source = "amap" if hotel.cost_source == "imported" else "llm_estimated"
    if ctx.attractions and hotel.location:
        from ..services.tools import haversine_km
        located = [s for s in ctx.attractions.values() if s.location]
        weight = sum(max(s.visit_duration, 1) for s in located)
        if weight:
            cost = sum(haversine_km(hotel.location, s.location) * max(s.visit_duration, 1) for s in located) / weight
            hotel.distance = f"距行程景点加权平均 {cost:.1f} km（直线估计）"
    if not hotel.estimated_cost:
        warnings.append("酒店价格未能估算，住宿预算保留为未知项。")

    logger.info(
        "住宿完成 | 步数 %d | 工具调用 %d 次 | 选定 %s | 来源 %s | %d 元/晚 × %d 晚",
        result.steps,
        len(ctx.calls),
        hotel.name,
        source,
        hotel.estimated_cost,
        nights,
    )
    return {
        "hotel": hotel,
        "hotels": list(ctx.hotels.values()),
        "hotel_source": source,
        "hotel_reason": ctx.chosen_reason or "按景点加权距离排序选择；房价为估算或未知。",
        "warnings": warnings,
        "errors": list(ctx.errors),
        "sources": {"hotel": source},
        "tool_calls": ctx.calls,
        "usage": result.usage,
    }
