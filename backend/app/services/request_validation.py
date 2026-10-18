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
    for module in type(req.requirements).model_fields:
        needs = getattr(req.requirements, module)
        if needs.unverified:
            return "CLARIFICATION_REQUIRED", "以下硬性要求尚无核验依据，请补充资料或明确放宽：" + "、".join(needs.unverified)
    if not 0 <= nights <= req.travel_days:
        return "INVALID_REQUEST", f"nights={nights} 越界（应在 0~{req.travel_days}）"
    if nights == 0 and req.fixed_hotel:
        return "INVALID_REQUEST", "不住宿的行程不能同时指定酒店"

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
    return None
