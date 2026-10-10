"""预算汇总节点 patch_budget（纯代码，不进 LLM）。

输入：state["plan"]（编排产出，days 已通过两层校验）、state["hotel"]、state["hotel_nights"]
输出：{"plan": <填好 budget 与 day_total_cost 的 plan>}

计算口径（v0.2 / v0.3 一致）：
    total_attractions    = 各天 Attraction.ticket_price 求和
    total_meals          = 各天 Meal.estimated_cost 求和
    total_hotels         = 酒店每晚预估单价 × **晚数**（不是 × 天数；3 天行程住 2 晚）
    total_transportation = 按交通方式与天数估算（这一项是估的，不是加的）
    total                = 四项之和
    day_total_cost       = 该天「景点 + 餐饮」+ 住宿与交通按天均摊

为什么单独一个节点，不让模型一起算（需求文档 FR-07）：
    加法是确定性的，模型做加法会出错，而且这类错误很难被发现 ——
    预算卡上写着「总计 2060」，没人真会逐项加一遍。
    凡是能确定性完成的计算，都不交给模型。
    （注意 v0.3 把"搜景点/找酒店"交给了 Agent，但**加法仍然留在代码里** ——
      这不是偷懒，是分工：Agent 做决策，代码做算术。）

边界处理：任一项缺失按 0 计，并把缺失写进 warnings，不静默。
"""

import logging

from ..models.state import TripState

logger = logging.getLogger(__name__)

# 每人每天交通费（元）的粗估。这不是"真实票价"，所以最终会标成估算值。
TRANSPORT_PER_DAY = {
    "公共交通": 40,
    "自驾": 120,
    "步行": 10,
    "混合": 80,
}
DEFAULT_TRANSPORT_PER_DAY = 60


def _transport_cost(transportation: str, travel_days: int) -> int:
    return TRANSPORT_PER_DAY.get(transportation, DEFAULT_TRANSPORT_PER_DAY) * travel_days


def patch_budget(state: TripState) -> dict:
    plan = dict(state.get("plan") or {})
    days = plan.get("days") or []

    if not days:
        logger.warning("预算跳过 | 行程为空")
        return {"plan": {**plan, "budget": None}, "warnings": ["行程为空，未计算预算。"]}

    warnings: list[str] = []
    total_attractions = 0
    total_meals = 0

    for day in days:
        cost_attractions = sum(int(spot.get("ticket_price") or 0) for spot in (day.get("attractions") or []))
        cost_meals = sum(int(meal.get("estimated_cost") or 0) for meal in (day.get("meals") or []))
        total_attractions += cost_attractions
        total_meals += cost_meals
        day["day_total_cost"] = cost_attractions + cost_meals  # 先记「景点 + 餐饮」

    hotel = state.get("hotel")
    nights = int(state.get("nights") or 0)  # 晚数的唯一真相源是 state["nights"]（不是天数）
    if hotel is None:
        total_hotels = 0
        warnings.append("未确定酒店，住宿预算按 0 计 —— 实际开销会更高。")
    else:
        total_hotels = int(hotel.estimated_cost or 0) * nights
        if not hotel.estimated_cost:
            warnings.append("酒店价格未能估算，住宿预算按 0 计 —— 实际开销会更高。")

    total_transportation = _transport_cost(state["request"].transportation, state["travel_days"])
    total = total_attractions + total_hotels + total_meals + total_transportation

    if total_attractions == 0:
        warnings.append("景点门票价未获取（高德不提供该字段），按 0 计入 —— 实际开销可能更高。")

    # 住宿与交通是行程级开销，摊到每一天，让每天的 day_total_cost 加起来**正好**等于 total。
    # 用整除 + 把余数补给最后一天：直接用 round() 会几元几元地对不上（实测差 1 元），
    # 而「每日小计之和 ≠ 总计」在预算卡上是很扎眼的问题。
    extras = total_hotels + total_transportation
    per_day, remainder = divmod(extras, len(days))
    for index, day in enumerate(days):
        day["day_total_cost"] = int(day["day_total_cost"]) + per_day + (remainder if index == len(days) - 1 else 0)

    budget = {
        "total_attractions": total_attractions,
        "total_hotels": total_hotels,
        "total_meals": total_meals,
        "total_transportation": total_transportation,
        "total": total,
    }
    logger.info(
        "预算完成 | 景点 %d + 住宿 %d(%d晚) + 餐饮 %d + 交通 %d = %d",
        total_attractions,
        total_hotels,
        nights,
        total_meals,
        total_transportation,
        total,
    )
    return {"plan": {**plan, "days": days, "budget": budget}, "warnings": warnings}
