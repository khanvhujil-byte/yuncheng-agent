"""独立评估者节点 verify_plan（纯代码，**独立于生成者**）。

对应 learn-claude-code 的 s17 Goal Loop：*"A goal decides when the loop may stop ——
an independent evaluator reviews each proposed stop"*。

它审的是「生成出来的行程本身合不合理」，四个检查全部**由坐标与时长算出来**，
不让模型自评 —— 让生成者评价自己的产物，等于没评价：

    ① 一天的地理跨度   同一天景点两两最大距离 > 25 km  → 提示（不要跨半座城）
    ② 一天的可行性     Σ游览时长 + 通勤 > 10 小时       → **严重**（回喂重试）
    ③ 同一景点排两天   同一 poi_id/名字出现在多天        → **严重**（回喂重试）
    ④ 单日费用畸高     某天费用 > 总预算 60%（天数 > 1）  → 提示

为什么分成「提示」与「严重」两档：
    提示写进 warnings（透明、不影响结果）；严重写进 validate_error，
    借 compose_plan 已有的回喂重试机制再试一次（封顶 2 次，由条件边控制）。
    两档都由**同一套判据**决定，不是拍脑袋 —— 严重项都是"这份行程明显是错的"。

为什么它要"独立"：
    compose_plan 里的两层校验审的是「用户要求满足了没」；
    本节点审的是「这份行程自己站不站得住」。两者视角不同、实现也不共享。
    这就是"生成者 / 评估者分离"——如果评估者和生成者是同一段逻辑，它只会同意自己。
"""

import logging

from ..models.schemas import Attraction
from ..models.state import TripState
from ..services.tools import haversine_km

logger = logging.getLogger(__name__)

MAX_DAY_SPAN_KM = 25.0  # 一天内景点两两最大距离上限（直线距离）
MAX_DAY_MINUTES = 600  # 一天可用时间上限（10 小时），含游览 + 通勤
MINUTES_PER_HOP = 30  # 景点之间通勤的粗估（每跳 30 分钟）
SINGLE_DAY_BUDGET_SHARE = 0.6  # 单日费用占比上限


def _day_span_km(spots: list) -> float:
    """一天里两两最远的直线距离（无坐标的景点跳过）。"""
    located = [s for s in spots if getattr(s, "location", None) is not None]
    worst = 0.0
    for i in range(len(located)):
        for j in range(i + 1, len(located)):
            worst = max(worst, haversine_km(located[i].location, located[j].location))
    return worst


def verify_plan(state: TripState) -> dict:
    """审一遍行程。返回要写回 state 的部分（warnings / validate_error）。"""
    # 上一层（compose_plan 的两层校验）已经判定不合格 → 不重复审，
    # 直接把控制权交给条件边，让它去重试。
    if state.get("validate_error"):
        return {}

    plan = state.get("plan") or {}
    days = plan.get("days") or []
    if not days:
        return {}

    hints: list[str] = []
    severe: list[str] = []
    seen: dict[str, int] = {}

    for day in days:
        spots = day.get("attractions") or []
        date = day.get("date") or f"第{day.get('day_index', 0) + 1}天"

        # ① 地理跨度
        parsed = []
        for raw in spots:
            try:
                parsed.append(Attraction.model_validate(raw))
            except Exception:  # noqa: BLE001 —— 审不了的就跳过，不能因为一条脏数据让整节点崩
                continue
        span = _day_span_km(parsed)
        if span > MAX_DAY_SPAN_KM:
            hints.append(f"{date} 的景点跨度约 {span:.0f} km，同一天跨得有点远，建议按片区重排。")

        # ② 一天的可行性
        visit_minutes = sum(int(getattr(s, "visit_duration", 0) or 0) for s in parsed)
        commute = max(len(parsed) - 1, 0) * MINUTES_PER_HOP
        total_minutes = visit_minutes + commute
        if total_minutes > MAX_DAY_MINUTES:
            severe.append(
                f"{date} 排不下：{len(parsed)} 个景点共需约 {visit_minutes} 分钟游览 + "
                f"{commute} 分钟通勤 = {total_minutes} 分钟，超过一天 {MAX_DAY_MINUTES} 分钟的上限。"
                f"请减少景点或拆到其他天。"
            )

        # ③ 重复排期
        for spot in parsed:
            key = spot.poi_id or spot.name
            if key in seen:
                severe.append(f"同一景点「{spot.name}」被排在 {date} 和更早的一天，请去掉重复。")
            else:
                seen[key] = 1

    # ④ 单日费用畸高
    total_budget = int((plan.get("budget") or {}).get("total") or 0)
    if len(days) > 1 and total_budget > 0:
        worst_day = max(days, key=lambda d: int(d.get("day_total_cost") or 0))
        share = int(worst_day.get("day_total_cost") or 0) / total_budget
        if share > SINGLE_DAY_BUDGET_SHARE:
            hints.append(
                f"{worst_day.get('date')} 一天就占了总预算的 {share * 100:.0f}%，"
                f"看看是不是某项估算偏高。"
            )

    logger.info(
        "独立评估 | %d 天 | 严重 %d 项 | 提示 %d 项", len(days), len(severe), len(hints)
    )

    result: dict = {}
    if hints:
        result["warnings"] = hints
    if severe:
        # 借 compose_plan 的回喂重试机制：把**具体哪一天排不下**告诉它，再试一次。
        # 不在这里动 retries —— 计数由 compose_plan 每次尝试自增，条件边据此封顶。
        result["validate_error"] = severe[0]
        result["errors"] = [f"verify_plan: {item}" for item in severe]
    return result
