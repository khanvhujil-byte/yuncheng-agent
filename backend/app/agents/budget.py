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
from ..config import get_settings

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
    settings = get_settings()
    reserved_tickets = []
    from ..services.admission_policy import apply_admission_policy

    for day in days:
        index = day.get("day_index")
        day_known = 0
        day_estimated = 0

        for spot in (day.get("attractions") or []):
            spot.update(apply_admission_policy(spot))
            price = spot.get("ticket_price")
            if (settings.ticket_budget_reserve_enabled and spot.get('admission_policy') != 'consumption'
                    and (price is None or spot.get("ticket_price_status") == "unknown" or spot.get("ticket_price_source") == "static_estimate")):
                price = settings.ticket_budget_reserve_amount
                reason = spot.get('ticket_price_note') or '暂无适用于该地点和日期的可靠门票来源。'
                if '门票预算预留，' in reason:
                    reason = reason.split('门票预算预留，', 1)[0].strip()
                spot.update(ticket_price=price, ticket_price_status="estimated", ticket_price_source="static_estimate",
                            ticket_price_evidence_url=None, ticket_price_note=reason + " 门票预算预留，尚未核实实际票价；不是景点报价。")
                reserved_tickets.append(spot.get("name") or "未命名景点")
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
                spot.get("ticket_price_source") if spot.get("ticket_price_source") not in (None,"unknown") else ("amap" if status == "known" else ("llm_estimated" if status == "estimated" else "unknown")),
                day_index=index,
                note=spot.get("ticket_price_note") or ("" if status != "unknown" else "门票价尚未核实"),
            )
            items.append(item)
            if status == "known":
                day_known += int(price)
            elif status == "estimated":
                day_estimated += int(price)

        for meal in (day.get("meals") or []):
            raw = meal.get("estimated_cost")
            status = meal.get("cost_status", "estimated")
            source = meal.get("cost_source", "llm_estimated")
            if raw is None or status == "unknown":
                item = _cost_item("meal", str(meal.get("name") or meal.get("type") or "餐饮"),
                                  None, "unknown", "unknown", day_index=index, note="未给出估算金额")
            else:
                item = _cost_item("meal", str(meal.get("name") or meal.get("type") or "餐饮"),
                                  int(raw), status, source, day_index=index,
                                  note=meal.get("cost_note") or "模型估算")
                if status == "known":
                    day_known += int(raw)
                else:
                    day_estimated += int(raw)
            items.append(item)

        day["day_total_cost"] = day_known + day_estimated

    # ---- 住宿：行程级一家，按晚数计（v0.2 的单一真相源是 state["nights"]）----
    hotel = state.get("hotel")
    if hotel is not None and (hotel.platform_quotes or hotel.price_note.startswith('飞猪')):
        from ..services.platform_hotels import quoted
        hotel = quoted(hotel, request.start_date)
        if plan.get('hotel'):
            plan['hotel'] = hotel.model_dump(mode='json')
    nights = int(state.get("nights") or 0)
    hotel_estimated = 0
    dated = plan.get('accommodation_version', 0) >= 11
    if dated and nights:
        from ..services.stays import plan_stays
        stays = plan_stays(plan)
        for stay in stays:
            if stay['hotel'].get('platform_quotes') or stay['hotel'].get('price_note', '').startswith('飞猪'):
                from ..services.platform_hotels import quoted
                stay['hotel'] = quoted(stay['hotel'], stay['check_in']).model_dump(mode='json')
            h, d = stay['hotel'], stay['check_in']
            price, status = h.get('estimated_cost'), h.get('cost_status', 'unknown')
            if price is None or status == 'unknown' or (not price and status != 'known'):
                amount, status = None, 'unknown'
            else:
                amount = int(price)
                hotel_estimated += amount
            items.append(_cost_item('hotel', f"{d} 住宿：{h['name']}", amount, status,
                                    h.get('cost_source', 'unknown'),
                                    day_index=next((x.get('day_index') for x in days if x.get('date') == d), None),
                                    note='一间房一晚；预算预留非报价' if h.get('cost_source') == 'static_estimate' else '一间房一晚；以实际预订为准'))
            if amount is not None:
                target = next((x for x in days if x.get('date') == d), None)
                if target is not None:
                    target['day_total_cost'] += amount
        if len(stays) != nights:
            items.append(_cost_item('hotel', '住宿日期缺失', None, 'unknown', 'unknown'))
        plan['stays'] = stays
    elif nights == 0:
        items.append(_cost_item("hotel", "本次不住宿", 0, "known", "static_estimate"))
    elif hotel is None:
        items.append(_cost_item("hotel", "住宿（未确定）", None, "unknown", "unknown",
                                note="没有选出酒店，住宿费无从估算"))
    else:
        price = hotel.estimated_cost
        name = f"住宿：{hotel.name} × {nights} 晚"
        if not price and not (price == 0 and getattr(hotel,"cost_status","estimated") == "known"):
            items.append(_cost_item("hotel", name, None, "unknown", "unknown",
                                    note="酒店价格未能估算"))
        else:
            # 用户自己指定的酒店也不代表我们知道它的真实价格 —— 高德基本不提供住宿价格，
            # 所以这里统一按**估算**处理（诚实优先于好看）。
            amount = int(price) * nights
            hotel_status=getattr(hotel,"cost_status","estimated")
            hotel_source=getattr(hotel,"cost_source","llm_estimated")
            if hotel_status=="unknown":
                items.append(_cost_item("hotel",name,None,"unknown","unknown",note="酒店价格状态未知，不能按零元通过。"))
                hotel_amount=0
            else:
                items.append(_cost_item("hotel", name, amount, hotel_status, hotel_source,
                                        note=(f"每晚预算预留 {price} 元 × {nights} 晚；尚未取得酒店报价，以实际预订为准。"
                                              if hotel_source == "static_estimate" else
                                              f"每晚参考 {price} 元 × {nights} 晚；以实际预订为准。")))
                hotel_amount=amount
            hotel_estimated += hotel_amount

    # ---- 交通：固定表估算 ----
    transport_amount = _transport_cost(getattr(request, "transportation", "") or "", state["travel_days"])
    items.append(_cost_item("transportation", f"市内交通（{getattr(request, 'transportation', '')}）",
                            transport_amount, "estimated", "static_estimate",
                            note=f"按{'一车' if getattr(request, 'transportation', '') == '自驾' else '每人'}每天 {TRANSPORT_PER_DAY.get(getattr(request, 'transportation', ''), DEFAULT_TRANSPORT_PER_DAY)} 元的固定表估算"
                                + ('；仅一车日常用车预留，不包含租车、长途和景区附加费。' if getattr(request, 'transportation', '') == '自驾' else '')))

    if dated and any(a.get('retrieval_scope') == 'surrounding' for d in days for a in d.get('attractions', [])):
        items.append(_cost_item('transportation', '周边长途/景区接驳附加交通费', None, 'unknown', 'unknown',
                                note='市内固定交通预留不覆盖长途、山区接驳、停车和过路费；需取得报价。'))

    # Platform fares are per adult. Do not silently omit a missing direction.
    if request and request.intercity_mode != 'none':
        import math
        legs = plan.get('intercity_legs') or []
        expected_directions = ['outbound', *(['return'] if request.return_trip else [])]
        for direction in expected_directions:
            leg = next((l for l in legs if l.get('direction') == direction), None)
            price = leg.get('amount_per_adult') if leg else None
            if leg:
                from ..services.intercity import _dt, TZ
                from datetime import datetime
                if _dt(leg['expires_at']) <= datetime.now(TZ):
                    leg.update(amount_per_adult=None, price_status='unknown', price_note='交通参考价已失效；重新核价前金额未知。')
                    price = None
            label = '去程' if direction == 'outbound' else '返程'
            items.append(_cost_item('transportation', label+'城际交通'+(' · '+leg['transport_no'] if leg else ' · 尚无班次'),
                math.ceil(price) if price is not None else None, 'estimated' if price is not None else 'unknown',
                'imported' if price is not None else 'unknown', day_index=0 if direction == 'outbound' else len(days)-1,
                note=(leg.get('price_note', '') if leg else '未找到真实班次与报价，不能按零元通过预算。')))
            if leg and leg.get('mode') == 'flight':
                items.append(_cost_item('transportation', label+'机票税费及可能的行李附加费', None, 'unknown', 'unknown',
                    day_index=0 if direction == 'outbound' else len(days)-1,
                    note='搜索接口未确认含税总额和行李额度，不能据票面参考价承诺完整交通预算。'))
    adults = getattr(request, 'adult_count', 1)
    rooms = getattr(request, 'room_count', 1)
    for item in items:
        quantity = rooms if item['category'] == 'hotel' else adults
        if request and request.transportation == '自驾' and item['name'].startswith('市内交通（'):
            quantity = 1  # One shared vehicle reference; do not charge it per passenger.
        if item['amount'] is not None:
            item['amount'] *= quantity
        if quantity > 1:
            item['note'] += f' 按{quantity}{"间房" if item["category"] == "hotel" else "位成人"}汇总。'
    known_total = sum(int(i["amount"]) for i in items if i["status"] == "known")
    estimated_total = sum(int(i["amount"]) for i in items if i["status"] == "estimated")
    unknown_items = [i["name"] for i in items if i["status"] == "unknown"]
    total = known_total + estimated_total

    # 分类小计：保留旧字段（前端仍在用），但**只统计已知与估算**，未知不在其中。
    total_attractions = sum(int(i["amount"]) for i in items
                            if i["category"] == "attraction" and i["amount"] is not None)
    total_hotels = sum(int(i['amount']) for i in items if i['category'] == 'hotel' and i['amount'] is not None)
    total_meals = sum(int(i["amount"]) for i in items
                      if i["category"] == "meal" and i["amount"] is not None)
    total_transportation = sum(int(i['amount']) for i in items if i['category'] == 'transportation' and i['amount'] is not None)

    unknown_attractions = [i["name"] for i in items
                           if i["category"] == "attraction" and i["status"] == "unknown"]
    if reserved_tickets:
        warnings.append(f"{len(reserved_tickets)} 项门票尚未核实，暂按每处 {settings.ticket_budget_reserve_amount} 元预留并计入参考预算；非景点报价，以官网和实际预约为准。")
    if unknown_attractions:
        # 单独给景点门票一条更具体的说明：它是**最常见**的未知项（高德不提供），
        # 混在"共 N 项未知"里用户看不出该怎么补信息。
        warnings.append(
            f"有 {len(unknown_attractions)} 处门票价未获取或到店消费未核实（逐项原因见费用明细），"
            "未计入总价 —— 实际开销可能更高。"
        )
    if nights > 0 and hotel is None and not dated:
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
    extras = sum(int(i['amount']) for i in items if i.get('day_index') is None and i['amount'] is not None)
    per_day, remainder = divmod(extras, len(days))
    for index, day in enumerate(days):
        day["day_total_cost"] = sum(int(i['amount']) for i in items if i.get('day_index') == day['day_index'] and i['amount'] is not None) + per_day + (remainder if index == len(days) - 1 else 0)

    budget = {
        "ticket_reserve_amount": settings.ticket_budget_reserve_amount if settings.ticket_budget_reserve_enabled else None,
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
    return {"plan": {**plan, 'adult_count': adults, 'room_count': rooms, 'origin_city': getattr(request, 'origin_city', ''), "days": days, "budget": budget}, "warnings": warnings}
