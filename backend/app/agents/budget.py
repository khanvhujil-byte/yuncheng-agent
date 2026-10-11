"""预算汇总 —— 纯代码加法，不过 LLM。

为什么加法必须由代码做：模型做加法会错，而且这种错很难被发现 ——
预算卡上写「总计 2060」，没有人会真去逐项加一遍。

★ v0.7 起这里多了一件同样重要的事：**逐项记录费用三态**。
    每条费用项都带 金额 / status(known|estimated|unknown) / 来源。
    金额未知的项 `amount=None`，**不进 total**，而是列进 `unknown_items`。
    以前的做法是"未知按 0 计入"，于是预算卡上那个总数看起来像真的，
    实际上把"不知道"当成了"不要钱"。

它只负责**汇总**；够不够由 `agents/verify_budget.py` 在之后独立复核（两个节点分开，
因为"算钱"和"判断钱够不够"是两件事，混在一起就会重演"检查写在算钱之前"的老问题）。
"""

import copy
import logging

from ..models.state import TripState

logger = logging.getLogger(__name__)

# 每人每天交通费（元）的粗估。这不是"真实票价"，所以标记为 static_estimate（我们自己的固定表）。
TRANSPORT_PER_DAY = {
    "公共交通": 40,
    "自驾": 120,
    "步行": 10,
    "混合": 80,
}
DEFAULT_TRANSPORT_PER_DAY = 60


def _transport_cost(transportation: str, travel_days: int) -> int:
    return TRANSPORT_PER_DAY.get(transportation, DEFAULT_TRANSPORT_PER_DAY) * travel_days


def _cost_item(category: str, name: str, amount, status: str, source: str,
               day_index=None, note: str = "") -> dict:
    """造一条费用项。`unknown` 时**强制**把金额置 None —— 这是本模块最重要的一条不变式。"""
    if status == "unknown":
        amount = None
    assert not (status == "unknown" and amount is not None), "未知费用项不能带金额（那是把未知当成了 0）"
    return {
        "category": category,
        "name": name,
        "day_index": day_index,
        "amount": amount,
        "status": status,
        "source": source,
        "note": note,
    }


def patch_budget(state: TripState) -> dict:
    # deepcopy 不能省（新写的测试抓出来的真 bug）：`dict(...)` 只是**浅拷贝**，
    # days 还是同一批 dict 对象，后面改 day["day_total_cost"] 会连带改掉调用方那一份。
    # 在 LangGraph 里这就是"节点就地改了入参"，后果很隐蔽（journal / 缓存可能共享引用），
    # 排查起来极难。宁可多花一次深拷贝。
    plan = copy.deepcopy(state.get("plan") or {})
    days = plan.get("days") or []

    request = state.get("request")
    limit = getattr(request, "budget_limit", None)

    if not days:
        logger.warning("预算跳过 | 行程为空")
        return {
            "plan": {**plan, "budget": None},
            "warnings": ["行程为空，未计算预算。"],
            "budget_verdict": None,
        }

    warnings: list[str] = []
    items: list[dict] = []

    for day in days:
        index = day.get("day_index")
        day_known = 0
        day_estimated = 0

        for spot in (day.get("attractions") or []):
            price = spot.get("ticket_price")
            # 三态推导规则（顺序很重要）：
            #   价格是 None          → unknown（未知就是未知）
            #   有价格 + 有明确 status → 用它（正常链路里 amap 给 unknown、估算给 estimated）
            #   有价格 + status 是 unknown（默认值，说明调用方没标来源）
            #                        → 按 estimated 处理，并如实标注
            #     ★ 为什么不能反过来一律当 unknown：那会让"确实带价格的调用方"也被判未知，
            #       把有信息的输入当成没信息 —— 过度保守和过度乐观一样是失真。
            if price is None:
                status = "unknown"
            else:
                status = spot.get("ticket_price_status") or "estimated"
                if status == "unknown":
                    status = "estimated"
            item = _cost_item(
                "attraction", str(spot.get("name") or "未命名景点"), price, status,
                "amap" if status == "known" else ("llm_estimated" if status == "estimated" else "unknown"),
                day_index=index,
                note="" if status != "unknown" else "门票价未获取（高德 biz_ext.cost 实测恒为空）",
            )
            items.append(item)
            if status == "known":
                day_known += int(price)
            elif status == "estimated":
                day_estimated += int(price)

        for meal in (day.get("meals") or []):
            raw = meal.get("estimated_cost")
            if raw is None:
                item = _cost_item("meal", str(meal.get("name") or meal.get("type") or "餐饮"),
                                  None, "unknown", "unknown", day_index=index, note="未给出估算金额")
            else:
                item = _cost_item("meal", str(meal.get("name") or meal.get("type") or "餐饮"),
                                  int(raw), "estimated", "llm_estimated", day_index=index,
                                  note="模型估算")
                day_estimated += int(raw)
            items.append(item)

        day["day_total_cost"] = day_known + day_estimated

    # ---- 住宿：行程级一家，按晚数计（v0.2 的单一真相源是 state["nights"]）----
    hotel = state.get("hotel")
    nights = int(state.get("nights") or 0)
    hotel_estimated = 0
    if hotel is None:
        items.append(_cost_item("hotel", "住宿（未确定）", None, "unknown", "unknown",
                                note="没有选出酒店，住宿费无从估算"))
    else:
        price = hotel.estimated_cost
        name = f"住宿：{hotel.name} × {nights} 晚"
        if not price:
            items.append(_cost_item("hotel", name, None, "unknown", "unknown",
                                    note="酒店价格未能估算"))
        else:
            # 用户自己指定的酒店也不代表我们知道它的真实价格 —— 高德基本不提供住宿价格，
            # 所以这里统一按**估算**处理（诚实优先于好看）。
            amount = int(price) * nights
            items.append(_cost_item("hotel", name, amount, "estimated", "llm_estimated",
                                    note=f"每晚约 {price} 元 × {nights} 晚（估算）"))
            hotel_estimated += amount

    # ---- 交通：固定表估算 ----
    transport_amount = _transport_cost(getattr(request, "transportation", "") or "", state["travel_days"])
    items.append(_cost_item("transportation", f"市内交通（{getattr(request, 'transportation', '')}）",
                            transport_amount, "estimated", "static_estimate",
                            note=f"按每人每天 {TRANSPORT_PER_DAY.get(getattr(request, 'transportation', ''), DEFAULT_TRANSPORT_PER_DAY)} 元的固定表估算"))

    known_total = sum(int(i["amount"]) for i in items if i["status"] == "known")
    estimated_total = sum(int(i["amount"]) for i in items if i["status"] == "estimated")
    unknown_items = [i["name"] for i in items if i["status"] == "unknown"]
    total = known_total + estimated_total

    # 分类小计：保留旧字段（前端仍在用），但**只统计已知与估算**，未知不在其中。
    total_attractions = sum(int(i["amount"]) for i in items
                            if i["category"] == "attraction" and i["amount"] is not None)
    total_hotels = hotel_estimated
    total_meals = sum(int(i["amount"]) for i in items
                      if i["category"] == "meal" and i["amount"] is not None)
    total_transportation = transport_amount

    unknown_attractions = [i["name"] for i in items
                           if i["category"] == "attraction" and i["status"] == "unknown"]
    if unknown_attractions:
        # 单独给景点门票一条更具体的说明：它是**最常见**的未知项（高德不提供），
        # 混在"共 N 项未知"里用户看不出该怎么补信息。
        warnings.append(
            f"有 {len(unknown_attractions)} 个景点的门票价未获取（高德不提供该字段），"
            "未计入总价 —— 实际开销可能更高。"
        )
    if hotel is None:
        warnings.append("未确定酒店，住宿费未知（不是 0）—— 实际开销会更高。")
    if unknown_items:
        shown = "、".join(unknown_items[:3])
        more = f" 等 {len(unknown_items)} 项" if len(unknown_items) > 3 else ""
        warnings.append(
            f"共 {len(unknown_items)} 项费用金额未知（{shown}{more}），未计入总价 —— "
            "所以下面的总计只是已知与估算部分之和，不代表实际开销。"
        )

    # 住宿与交通是行程级开销，摊到每一天，让每天的 day_total_cost 加起来**正好**等于 total。
    # 用整除 + 把余数补给最后一天：直接用 round() 会几元几元地对不上（实测差 1 元），
    # 而「每日小计之和 ≠ 总计」在预算卡上是很扎眼的问题。
    extras = hotel_estimated + transport_amount
    per_day, remainder = divmod(extras, len(days))
    for index, day in enumerate(days):
        day["day_total_cost"] = int(day["day_total_cost"]) + per_day + (remainder if index == len(days) - 1 else 0)

    budget = {
        "total_attractions": total_attractions,
        "total_hotels": total_hotels,
        "total_meals": total_meals,
        "total_transportation": total_transportation,
        "total": total,
        "items": items,
        "known_total": known_total,
        "estimated_total": estimated_total,
        "unknown_items": unknown_items,
        # 上限由请求来；判定（limit_status）留给 verify_budget —— 汇总与判定分开
        "limit": limit,
        "limit_status": "none",
        "limit_basis": "",
    }
    logger.info(
        "预算汇总 | 已知 %d + 估算 %d = %d | 未知 %d 项 | 上限 %s | 景点 %d + 住宿 %d(%d晚) + 餐饮 %d + 交通 %d",
        known_total, estimated_total, total, len(unknown_items), limit,
        total_attractions, total_hotels, nights, total_meals, total_transportation,
    )
    return {"plan": {**plan, "days": days, "budget": budget}, "warnings": warnings}
