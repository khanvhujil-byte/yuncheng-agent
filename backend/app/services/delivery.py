"""Independent, deterministic delivery audit, including cache and resumed runs."""
from pydantic import ValidationError

from ..agents.planner_agent import _validate_constraints, _validate_structure
from ..agents.verify_plan import verify_plan
from ..models.schemas import TripPlan
from .itinerary_experience import validate_experience


def audit(payload, req, state=None):
    try:
        parsed = TripPlan.model_validate(payload)
    except (ValidationError, TypeError, ValueError):
        return None, ["生成结果的数据结构不合法，请重新编排；本次结果不会保存。"]
    reasons = []
    error = _validate_structure(parsed.days, req.start_date, req.travel_days)
    if error:
        reasons.append(error)
    state = state or {}
    candidates = state.get("attractions")
    if candidates is not None:
        _, error = _validate_constraints(parsed.days, state.get("must_visit_resolved") or [], candidates)
        if error:
            reasons.append(error)
    nights = req.nights if req.nights is not None else req.travel_days - 1
    if parsed.hotel_nights != nights:
        reasons.append("住宿晚数与本次要求不一致。")
    if nights > 0 and parsed.hotel is None:
        reasons.append("尚未找到可用酒店。")
    if nights == 0 and parsed.hotel is not None:
        reasons.append("本次无需住宿，结果却包含酒店。")
    if req.fixed_hotel:
        anchor = state.get("hotel_anchor")
        if anchor and parsed.hotel and anchor.poi_id and parsed.hotel.poi_id != anchor.poi_id:
            reasons.append("酒店未遵守用户指定的住宿约束。")
        report = parsed.constraint_report
        if not report or report.hotel_source != "user":
            reasons.append("指定酒店的约束对账未完成。")
    checks = verify_plan({"plan": parsed.model_dump(), "retries": 0, "validate_error": None})
    if checks.get("validate_error"):
        reasons.append(checks["validate_error"])
    reasons.extend(validate_experience(parsed.model_dump(), state))
    from .requirements import audit_requirements
    reasons.extend(audit_requirements(parsed.model_dump(), req))
    return parsed.model_dump(), reasons
