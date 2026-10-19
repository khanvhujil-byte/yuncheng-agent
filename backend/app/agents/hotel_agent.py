"""住宿选址：默认确定性工具链，可显式开启旧 ReAct 对照实验。

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

import json
import logging
from copy import deepcopy

from pydantic import ValidationError

from ..config import get_settings
from ..models.schemas import Attraction, Hotel
from ..models.state import TripState
from ..services.tools import ToolContext, execute
from .prompts import HOTEL_AGENT_PROMPT
from .react_loop import run_agent

logger = logging.getLogger(__name__)

MAX_STEPS = 5  # 搜候选 1 + 排序 1 + 实测 1 + 决定 1，留 1 步余量。
# ↑ 从 6 收到 5 的原因（实测）：住宿 Agent 一旦开始"多试几家/反复实测"，
#   这一步会从十几秒涨到近百秒，成为整条链路的瓶颈（SSE 逐节点进度让它第一次可见）。
#   步数上限是兜底：真跑歪了也不会把端到端拖到两分钟。

# These are budgeting reserves by accommodation preference, never room quotes.
# A strict nightly price cap still requires dated evidence during candidate filtering.
HOTEL_BUDGET_RESERVES = {"经济型酒店": 260, "舒适型酒店": 450, "豪华酒店": 900, "民宿": 300}
DEFAULT_HOTEL_BUDGET_RESERVE = 350
MAX_SEARCH_KEYWORDS = 2


def _candidate_key(req, nights: int) -> str:
    """Reuse only within the same city, dates and hotel requirements."""
    return json.dumps([req.city, req.start_date, nights, req.accommodation,
                       req.requirements.hotel.model_dump()], ensure_ascii=False, sort_keys=True)


def _direct_selection(state: TripState, ctx: ToolContext, key: str) -> tuple[int, dict, bool]:
    """Search -> local rank -> structured choose, without model round trips."""
    from ..services.candidate_search import minimum_candidates, priced_allowed

    req = state["request"]
    reused = False
    if state.get("hotel_candidate_key") == key:
        candidates = []
        for raw in state.get("hotels") or []:
            try:
                candidates.append(Hotel.model_validate(raw).model_copy(deep=True))
            except (ValidationError, TypeError):
                continue
        # Model estimates/reserves from the previous selection are not current
        # price evidence. Refresh imported quotes, then reapply hard constraints.
        candidates = priced_allowed(req.city, "hotel", candidates, req.requirements.hotel,
                                    start_date=req.start_date, nights=ctx.nights)
        ctx.hotels = {h.poi_id or h.name: h for h in candidates}
        reused = bool(ctx.hotels)

    if not ctx.hotels:
        keywords = list(dict.fromkeys([*req.requirements.hotel.keywords, req.accommodation or "酒店"]))
        for keyword in keywords[:MAX_SEARCH_KEYWORDS]:
            outcome = execute("hotels", "search_hotels", {"keyword": keyword}, ctx)
            if len(ctx.hotels) >= minimum_candidates("hotel"):
                break
            # Authentication/argument failures cannot be fixed with another query.
            if outcome.status == "error" and not outcome.retryable:
                break

    if ctx.hotels:
        execute("hotels", "rank_hotels", {}, ctx)
    if ctx.ranked_ids:
        reserve = HOTEL_BUDGET_RESERVES.get(req.accommodation, DEFAULT_HOTEL_BUDGET_RESERVE)
        ranked = [ctx.hotels[identity] for identity in ctx.ranked_ids]
        hotel = ranked[0]
        if req.budget_limit is not None:
            from .budget import patch_budget
            # Use the same deterministic expense rules as final verification.
            # Unknown expenses stay unknown; this is selection guidance, never
            # proof of meeting the total budget.
            preview = patch_budget({**state, "hotel": None}).get("plan", {}).get("budget") or {}
            non_hotel = sum(preview.get(field) or 0 for field in
                            ("total_attractions", "total_meals", "total_transportation"))
            remaining_per_night = max(0, req.budget_limit - non_hotel) / ctx.nights

            def projected_price(candidate):
                return candidate.estimated_cost if candidate.cost_source == "imported" else reserve

            affordable = [candidate for candidate in ranked if projected_price(candidate) <= remaining_per_night]
            hotel = affordable[0] if affordable else min(ranked, key=projected_price)
        sourced = hotel.cost_source == "imported" and hotel.cost_status in ("known", "estimated")
        ctx.hotel_price_source = "static_estimate"
        reason = "根据本轮实际景点与游览时长，按加权直线距离排序选择；实际通勤由后续路线工具复核。"
        if req.budget_limit is not None:
            reason += "同时考虑当前非住宿费用与剩余预算；最终是否满足预算以逐项复核为准。"
        if not sourced:
            reason += f"每晚 {reserve} 元仅为住宿预算预留，尚未取得该酒店报价。"
        execute("hotels", "choose_hotel", {"hotel": hotel.poi_id or hotel.name,
                                           "price_per_night": hotel.estimated_cost if sourced else reserve,
                                           "reason": reason}, ctx)
    return 0, {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0}, reused


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
        hotel.nights = nights
        from ..models.schemas import ConstraintReport
        report = state.get("constraint_report") or ConstraintReport()
        report = ConstraintReport.model_validate(report).model_copy(update={"hotel_source": "user", "hotel_anchor": hotel.name})
        return {"hotel": hotel, "hotels": [hotel], "hotel_source": "user",
                "constraint_report": report,
                "hotel_reason": "使用用户指定且已按名称与地址解析的酒店。",
                "sources": {"hotel": "user"},
                "warnings": ["指定酒店的实际房价未知，预算中保留未知项。"]
                if hotel.cost_status == "unknown" or (not hotel.estimated_cost and hotel.cost_status != "known") else []}

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

    use_agent = get_settings().hotel_use_agent
    key = _candidate_key(req, nights)
    if use_agent:
        result = run_agent("hotels", HOTEL_AGENT_PROMPT, _user_prompt(state, nights), ctx, max_steps=MAX_STEPS)
        steps, usage, reused = result.steps, result.usage, False
    else:
        steps, usage, reused = _direct_selection(state, ctx, key)

    warnings: list[str] = []
    hotel = ctx.chosen_hotel
    from ..services.requirements import hotel_allowed
    if hotel is not None and not hotel_allowed(hotel, req.requirements.hotel):
        hotel = None
    ctx.hotels = {k: h for k, h in ctx.hotels.items() if hotel_allowed(h, req.requirements.hotel)}
    ctx.ranked = [name for name in ctx.ranked if any(h.name == name for h in ctx.hotels.values())]
    ctx.ranked_ids = [key for key in ctx.ranked_ids if key in ctx.hotels]
    if hotel is None and use_agent:
        # 兜底：Agent 没给出最终选择（或步数用尽）→ 用排序第一名顶上
        if not ctx.ranked:
            execute("hotels", "rank_hotels", {}, ctx)
        hotel = ctx.hotels.get(ctx.ranked_ids[0]) if ctx.ranked_ids else next(
            (h for h in ctx.hotels.values() if h.name == (ctx.ranked[0] if ctx.ranked else "")), None)
        if hotel is None:
            failed = _degraded("住宿数据获取失败，未能确定酒店。", list(ctx.errors), ctx.calls)
            failed.update(usage=usage)
            return failed
        warnings.append("住宿助手没有给出最终选择，已按加权距离排序取第一名。")

    if hotel is None:
        result = _degraded("住宿数据获取失败，未能确定符合要求的酒店。", list(ctx.errors), ctx.calls)
        result.update(usage=usage, hotels=list(ctx.hotels.values()), hotel_candidate_key=key)
        if req.requirements.hotel.max_price_per_night is not None:
            result["warnings"].append("每晚价格上限需要本次入住日期的价格证据，不能用预算预留金额冒充已满足。")
        return result

    # 来源标记：用户指定过的标 user（前端显示「你指定的」），其余是模型估算价
    local_hit = reused or any(call.get("outcome", {}).get("stats", {}).get("cache_hit") for call in ctx.calls)
    source = ("amap" if hotel.cost_source == "imported" else "llm_estimated") if use_agent else ("catalog" if local_hit else "amap")
    if ctx.attractions and hotel.location:
        from ..services.tools import haversine_km
        located = [s for s in ctx.attractions.values() if s.location]
        weight = sum(max(s.visit_duration, 1) for s in located)
        if weight:
            cost = sum(haversine_km(hotel.location, s.location) * max(s.visit_duration, 1) for s in located) / weight
            hotel.distance = f"距行程景点加权平均 {cost:.1f} km（直线估计）"
    if hotel.cost_source == "static_estimate":
        warnings.append(f"酒店实际房价尚未核实，暂按每晚 {hotel.estimated_cost} 元预留并计入参考预算；这是固定预算预留，不是酒店报价，以实际预订为准。")
    elif not hotel.estimated_cost and hotel.cost_status != "known":
        warnings.append("酒店价格未能估算，住宿预算保留为未知项。")

    logger.info(
        "住宿完成 | 步数 %d | 工具调用 %d 次 | 选定 %s | 来源 %s | %d 元/晚 × %d 晚",
        steps,
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
        "usage": usage,
        "hotel_candidate_key": key,
    }
