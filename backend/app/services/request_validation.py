"""Local request rules shared by HTTP entry points and the planning graph."""
from datetime import date, timedelta

from ..models.schemas import TripRequest

MAX_SPOTS_PER_DAY = 3


def validate_request(req: TripRequest) -> tuple[str, str] | None:
    """Return the first input error without cache, network, or model calls."""
    if not req.city.strip() or not req.transportation.strip() or not req.accommodation.strip():
        return "INVALID_REQUEST", "城市、交通方式和住宿偏好不能为空"
    try:
        start = date.fromisoformat(req.start_date)
    except ValueError:
        return "DATE_RANGE_MISMATCH", f"start_date 不是合法日期：{req.start_date!r}"
    try:
        expected_end = (start + timedelta(days=req.travel_days - 1)).isoformat()
    except OverflowError:
        return "DATE_RANGE_MISMATCH", "行程结束日期超出支持的日期范围"
    if req.end_date and req.end_date != expected_end:
        return (
            "DATE_RANGE_MISMATCH",
            f"end_date={req.end_date} 与 start_date+travel_days-1={expected_end} 不一致",
        )

    nights = req.nights if req.nights is not None else req.travel_days - 1
    if req.room_count > req.adult_count:
        return 'INVALID_REQUEST', '房间数不能超过成人数；本期支持成人出行参考规划。'
    if req.intercity_mode != 'none':
        from ..models.travel import china_today
        if not req.origin_city.strip() or req.origin_city.removesuffix('市').strip() == req.city.removesuffix('市').strip():
            return 'INVALID_REQUEST', '安排城际交通时须填写与目的地不同的出发城市。'
        if start <= china_today():
            return 'INVALID_REQUEST', '平台城际查询需要未来的目的地游玩日期。'
        if req.return_trip and nights == req.travel_days:
            return 'INVALID_REQUEST', '末日返程与末晚住宿冲突；请减少一晚或选择单程。'
        if any((req.arrival_address, req.departure_address, req.arrival_time, req.departure_time)):
            return 'CLARIFICATION_REQUIRED', '自动安排城际交通时，到离地点和时间由班次确定；请先清除手动到离信息，或关闭城际交通。'
    for module in type(req.requirements).model_fields:
        needs = getattr(req.requirements, module)
        if needs.unverified:
            return "CLARIFICATION_REQUIRED", "以下硬性要求尚无核验依据，请补充资料或明确放宽：" + "、".join(needs.unverified)
    if not 0 <= nights <= req.travel_days:
        return "INVALID_REQUEST", f"nights={nights} 越界（应在 0~{req.travel_days}）"
    if nights == 0 and req.fixed_hotel:
        return "INVALID_REQUEST", "不住宿的行程不能同时指定酒店"

    from .stays import night_dates
    dates = night_dates(req)
    seen = set()
    for lock in req.stay_locks:
        if lock.check_in not in dates or lock.check_in in seen:
            return "INVALID_REQUEST", "锁定住宿日期重复或不在应住宿日期中"
        seen.add(lock.check_in)
        if req.fixed_hotel and lock.hotel_name != req.fixed_hotel:
            return "CLARIFICATION_REQUIRED", "日期锁定酒店与全程固定酒店冲突，请确认。"
    if req.arrival_time and req.departure_time and req.travel_days == 1 and req.arrival_time >= req.departure_time:
        return "INVALID_REQUEST", "同日离开时间必须晚于到达时间。"

    # Check the complete list; quota limits must never silently drop hard constraints.
    must_visit = [name.strip() for name in req.must_visit if name.strip()]
    capacity = req.travel_days * MAX_SPOTS_PER_DAY
    capacity = min(capacity, req.travel_days * req.requirements.attractions.max_per_day)
    from .requirements import excluded
    if any(excluded(name, req.requirements.attractions) for name in must_visit):
        return "CLARIFICATION_REQUIRED", "必去景点同时出现在排除名单，请确认保留哪项要求。"
    if req.fixed_hotel and excluded(req.fixed_hotel, req.requirements.hotel):
        return "CLARIFICATION_REQUIRED", "指定酒店同时出现在排除名单，请确认保留哪项要求。"
    if len(must_visit) > capacity:
        return (
            "CONSTRAINT_INFEASIBLE",
            f"必去 {len(must_visit)} 个，但 {req.travel_days} 天最多安排 {capacity} 个",
        )
    if len(must_visit) > 10:
        return "CONSTRAINT_INFEASIBLE", "必去景点最多 10 个，请分次规划，系统不会静默丢弃硬约束"
    from .day_goals import effective_goals
    for goal in effective_goals(req):
        if goal.days > req.travel_days or any(index >= req.travel_days for index in goal.day_indices):
            return "CONSTRAINT_INFEASIBLE", f"「{goal.theme}」要求{goal.days}天或指定日期超出本次{req.travel_days}天，请调整天数。"
        if len(goal.day_indices) != len(set(goal.day_indices)):
            return "INVALID_REQUEST", f"「{goal.theme}」的指定日期不能重复。"
    return None
