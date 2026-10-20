"""Staged, provenance-preserving edits of one committed itinerary module.

This path never invokes the planning graph. Models may select identifiers from a
verified pool; POI facts, prices and locations are copied by code. Confirmation
recomputes only dependent schedules/routes and the global budget.
"""
import copy
import hashlib
import json
import logging
import time

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..agents.budget import patch_budget
from ..agents.planner_agent import _validate_constraints, _validate_structure
from ..agents.verify_budget import verify_budget
from ..agents.verify_plan import verify_plan
from ..models.dialog import ModuleEdit
from ..models.schemas import Attraction, Hotel, Restaurant, TripPlan, TripPlanResponse, TripRequest
from . import llm, store, delivery_logging
from .delivery import audit
from .state_codec import restore_state_types

logger = logging.getLogger(__name__)


def _dict(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)


def _id(value):
    raw = _dict(value)
    if raw.get("poi_id"):
        return raw["poi_id"]
    # Historical hotel candidates may lack a provider ID. The server derives an
    # opaque identity from their recorded coordinates, never from client input.
    identity = {key: raw.get(key) for key in ("name", "address", "location")}
    return "recorded-" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]


def _context(current):
    previous = current.get("result") or {}
    if not previous.get("data") or not current.get("committed"):
        raise HTTPException(422, "请先生成并确认一份有效行程，再修改具体模块。")
    if current["slots"] != current["committed"]:
        raise HTTPException(409, "还有尚未确认的整体要求修改，请先取消或完成该修改，再编辑具体模块。")
    try:
        req = TripRequest.model_validate(current["committed"])
        plan = TripPlan.model_validate(previous["data"]).model_dump(mode="json")
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(409, "原行程的保存格式不兼容，无法安全局部修改；原结果仍保留。") from exc
    with store._connect() as db:
        row = db.execute("SELECT state_json FROM run_snapshot WHERE run_id=?", (previous.get("thread_id"),)).fetchone()
    try:
        state = restore_state_types(json.loads(row[0])) if row else {}
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(409, "原行程的事实快照格式不兼容，无法安全局部修改；原结果仍保留。") from exc
    # Cached/legacy runs may only retain the delivered facts. Those remain
    # usable, but an absent candidate pool must never be invented.
    attractions = state.get("attractions") or [a for day in plan["days"] for a in day["attractions"]]
    hotels = state.get("hotels") or [s["hotel"] for s in plan.get("stays", [])] or ([plan["hotel"]] if plan.get("hotel") else [])
    restaurants = state.get("restaurants") or []
    pools = {"attractions": [Attraction.model_validate(a) for a in attractions],
             "hotel": [Hotel.model_validate(h) for h in hotels],
             "meals": [Restaurant.model_validate(r) for r in restaurants]}
    # Cache-hit monitors do not have the full original collection state. A
    # delivered, source-bound meal still carries enough recorded POI facts to
    # preserve it; a historical model-written restaurant without ID never does.
    for day in plan["days"]:
        for meal in day.get("meals", []):
            if meal.get("restaurant_id") and meal.get("location") and meal.get("source") in ("amap", "imported"):
                source = meal.get("cost_source")
                pools["meals"].append(Restaurant(
                    name=meal["name"], poi_id=meal["restaurant_id"], address=meal.get("address") or "",
                    location=meal["location"], source=meal["source"],
                    avg_cost=meal.get("estimated_cost") if source in ("amap", "imported") else None,
                    avg_cost_status="estimated" if source in ("amap", "imported") else "unknown",
                    cost_source=source if source in ("amap", "imported") else "unknown",
                    ranking_name=meal.get("ranking_name"), ranking_rank=meal.get("ranking_rank"),
                    ranking_url=meal.get("ranking_url"), evidence_url=meal.get("evidence_url")))
    try:
        from . import catalog
        for key, kind, model in (("attractions", "attraction", Attraction), ("hotel", "hotel", Hotel),
                                 ("meals", "restaurant", Restaurant)):
            pools[key].extend(model.model_validate(raw) for raw in catalog.list_places(req.city, kind))
    except Exception:
        logger.exception("局部编辑候选资料库读取失败，保留当前行程已记录的事实")
    for key in pools:
        # Recorded facts win for existing IDs, new catalogue IDs add choices.
        unique = {}
        for item in pools[key]:
            unique.setdefault(_id(item), item)
        pools[key] = list(unique.values())
    # Keep the currently chosen hotel available even if ranked candidate storage
    # was narrower than the final resolver output.
    if plan.get("hotel") and _id(plan["hotel"]) not in {_id(h) for h in pools["hotel"]}:
        pools["hotel"].append(Hotel.model_validate(plan["hotel"]))
    for stay in plan.get('stays', []):
        if _id(stay['hotel']) not in {_id(h) for h in pools['hotel']}:
            pools['hotel'].append(Hotel.model_validate(stay['hotel']))
    state.update(request=req, attractions=pools["attractions"], hotels=pools["hotel"], restaurants=pools["meals"],
                 weather=plan["weather_info"], hotel=Hotel.model_validate(plan["hotel"]) if plan.get("hotel") else None,
                 nights=plan["hotel_nights"], travel_days=req.travel_days, end_date=plan["end_date"])
    if "must_visit_resolved" not in state:
        required = set(req.must_visit)
        state["must_visit_resolved"] = [a for a in pools["attractions"] if a.required or a.name in required]
    return req, plan, state, pools


def candidates(current, module, day_index=None):
    if module not in ("hotel", "meals", "attractions", "day"):
        raise HTTPException(422, "可修改模块仅支持 hotel、meals、attractions、day。")
    req, plan, _, pools = _context(current)
    if day_index is not None and not 0 <= day_index < len(plan["days"]):
        raise HTTPException(422, "修改的日期索引超出当前行程范围。")
    kind = "attractions" if module == "day" else module
    from .price_catalog import apply_prices
    service_date = plan["days"][day_index]["date"] if day_index is not None else req.start_date
    priced = apply_prices(req.city, {"attractions": "attraction", "hotel": "hotel", "meals": "restaurant"}[kind],
                          pools[kind], service_date=service_date, nights=plan["hotel_nights"],
                          travel_days=1 if day_index is not None else req.travel_days)
    if kind == 'hotel':
        from .platform_hotels import quoted
        priced = [quoted(h, service_date) for h in priced]
    priced = _eligible(priced, kind, req)
    items = []
    for item in priced:
        raw = _dict(item)
        items.append({"candidate_id": _id(item), "poi_id": raw.get("poi_id"), "name": raw["name"],
                      "address": raw.get("address", ""),
                      "estimated_cost": raw.get("avg_cost") if kind == "meals" else raw.get("estimated_cost") if kind == "hotel" else raw.get("ticket_price"),
                      "price_status": raw.get("avg_cost_status", raw.get("ticket_price_status", raw.get("cost_status", "estimated"))),
                      "ranking_name": raw.get("ranking_name"), "ranking_rank": raw.get("ranking_rank")})
    current_ids = []
    if module == 'hotel' and plan.get('accommodation_version', 0) >= 11:
        relevant = [s for s in plan['stays'] if day_index is None or s['check_in'] == plan['days'][day_index]['date']]
        current_ids = [_id(s['hotel']) for s in relevant]
        locked = bool(req.fixed_hotel) or bool(relevant and all(s['locked'] for s in relevant))
    else:
        locked = bool(module == 'hotel' and req.fixed_hotel)
    if module == "hotel" and plan.get("accommodation_version", 0) < 11 and plan.get("hotel"):
        current_ids = [_id(plan["hotel"])]
    elif module != 'hotel' and day_index is not None:
        key = "attractions" if kind == "attractions" else "meals"
        current_ids = [a.get("restaurant_id") if key == "meals" else _id(a) for a in plan["days"][day_index][key]]
    return {"module": module, "day_index": day_index, "candidates": items,
            "current_ids": current_ids, "locked": locked}


class _Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidate_ids: list[str] = Field(min_length=1, max_length=3)


def _eligible(pool, kind, req):
    from .requirements import excluded, hotel_allowed, restaurant_allowed
    if kind == "meals":
        return [p for p in pool if restaurant_allowed(p, req.requirements.food)]
    if kind == "hotel":
        return [p for p in pool if hotel_allowed(p, req.requirements.hotel)]
    return [p for p in pool if not excluded(p.name, req.requirements.attractions)]


def _select(edit, pool, plan, req=None):
    if edit.candidate_id and edit.candidate_ids:
        raise HTTPException(422, "单个候选和候选列表不能同时提供。")
    supplied = [edit.candidate_id] if edit.candidate_id else edit.candidate_ids
    usage = {}
    if not supplied:
        if not edit.instruction.strip():
            raise HTTPException(422, "请选择替换候选，或描述该模块的具体修改要求。")
        if not pool:
            raise HTTPException(422, "当前没有已核验候选，无法安全替换；请先补充真实数据。")
        choices = [{"candidate_id": _id(item), **_dict(item)} for item in pool]
        from .requirements import context
        from ..agents.prompts import PARTIAL_EDIT_AGENT_PROMPT
        text, usage = llm.chat_json(
            PARTIAL_EDIT_AGENT_PROMPT + "你只为局部修改挑选已有候选，不生成地点、坐标、价格或整份行程。"
            "返回 JSON {\"candidate_ids\":[\"候选ID\"]}，ID 必须来自候选清单。"
            "hotel/meals/attractions 只返回一个 ID；day 返回 1 至 3 个不同景点 ID。",
            json.dumps({"edit": edit.model_dump(), "current_day": plan["days"][edit.day_index] if edit.day_index is not None else None,
                        "candidates": choices}, ensure_ascii=False)
            + (context(req, "food", "attractions", "hotel", "transport", "pace", "weather", "budget") if req else ""))
        try:
            supplied = _Selection.model_validate_json(text).candidate_ids
        except ValidationError as exc:
            raise HTTPException(502, "局部修改候选选择格式不合法，原行程已保留。") from exc
    if edit.module != "day" and len(supplied) != 1:
        raise HTTPException(422, "该模块一次只能替换一个条目。")
    if len(supplied) != len(set(supplied)):
        raise HTTPException(422, "候选列表包含重复地点。")
    index = {_id(item): item for item in pool}
    if any(identifier not in index for identifier in supplied):
        raise HTTPException(422, "替换地点不在已核验候选池中，原行程已保留。")
    return [index[identifier] for identifier in supplied], usage


def _meal(restaurant, old):
    from .itinerary_experience import meal_from_restaurant
    return meal_from_restaurant(restaurant, old["type"])


def _refresh_prices(plan, req, state):
    """Prices are derived, date-bound facts; venue identity is preserved.

    This runs at preview and again at confirmation, so a quote that expires
    while the user reviews a draft cannot silently remain a known budget item.
    """
    from .itinerary_experience import meal_from_restaurant
    from .price_catalog import apply_prices
    before = copy.deepcopy(plan)
    restaurant_pool = {r.poi_id: r for raw in state.get("restaurants", []) if (r := Restaurant.model_validate(raw))}
    for day in plan["days"]:
        day["attractions"] = [a.model_dump(mode="json") for a in apply_prices(
            req.city, "attraction", [Attraction.model_validate(a) for a in day["attractions"]],
            service_date=day["date"], travel_days=1)]
        for meal in day["meals"]:
            restaurant = restaurant_pool.get(meal.get("restaurant_id"))
            if restaurant is None:
                continue
            if meal.get("cost_source") in ("amap", "imported"):
                restaurant = restaurant.model_copy(update={"avg_cost": meal.get("estimated_cost"),
                                                           "cost_source": meal["cost_source"],
                                                           "evidence_url": meal.get("evidence_url")})
            refreshed = apply_prices(req.city, "restaurant", [restaurant], service_date=day["date"])[0]
            pricing = meal_from_restaurant(refreshed, meal["type"])
            # Updating money does not rewrite the recorded restaurant choice,
            # description, source attribution or rank in another module.
            meal.update({key: pricing.get(key) for key in ("estimated_cost", "cost_status", "cost_source", "cost_note", "evidence_url")})
    if plan.get('accommodation_version', 0) >= 11:
        _refresh_stay_prices(plan, req)
    elif plan.get("hotel"):
        hotel = apply_prices(req.city, "hotel", [Hotel.model_validate(plan["hotel"])],
                             service_date=req.start_date, nights=plan["hotel_nights"])[0]
        from .platform_hotels import quoted
        plan["hotel"] = quoted(hotel, req.start_date).model_dump(mode="json")
    if plan != before:
        plan["warnings"] = list(dict.fromkeys([*(plan.get("warnings") or []),
            "已按本次出行日期重新核验来源价格；失效报价不会继续沿用，餐饮预留费用仍明确标为估算。 "]))
    return plan


def _refresh_stay_prices(plan, req):
    from .price_catalog import apply_prices
    from .platform_hotels import quoted
    for stay in plan.get('stays', []):
        h = apply_prices(req.city, 'hotel', [Hotel.model_validate(stay['hotel'])], service_date=stay['check_in'], nights=1)[0]
        stay['hotel'] = quoted(h, stay['check_in']).model_dump(mode='json')
    _stay_summary(plan)


def _stay_summary(plan):
    from .stays import identity
    hotels = [s['hotel'] for s in plan.get('stays', [])]
    plan['hotel'] = copy.deepcopy(hotels[0]) if hotels and len({identity(h) for h in hotels}) == 1 else None
    if plan['hotel']:
        plan['hotel']['nights'] = len(hotels)


def _target_stays(plan, edit):
    stays = plan.get('stays', [])
    if edit.stay_scope == 'all':
        if edit.day_index is not None:
            raise HTTPException(422, '全部住宿修改不接受日期；单晚请选择night。')
        targets = [s for s in stays if not s.get('locked')]
    else:
        if edit.day_index is None or edit.day_index >= len(plan['days']):
            raise HTTPException(422, '请明确要更换的入住日。')
        d = plan['days'][edit.day_index]['date']
        index = next((i for i, s in enumerate(stays) if s['check_in'] == d), None)
        if index is None:
            raise HTTPException(422, '该日没有住宿。')
        left = right = index
        if edit.stay_scope == 'segment':
            from .stays import identity
            while left > 0 and identity(stays[left-1]['hotel']) == identity(stays[index]['hotel']):
                left -= 1
            while right+1 < len(stays) and identity(stays[right+1]['hotel']) == identity(stays[index]['hotel']):
                right += 1
        targets = stays[left:right+1]
    if not targets or any(s.get('locked') for s in targets):
        raise HTTPException(422, '选定住宿已锁定或没有可修改夜晚；原行程保留。')
    return targets


def prepare(current, edit: ModuleEdit):
    req, plan, state, pools = _context(current)
    if edit.module != "hotel":
        if edit.day_index is None or not 0 <= edit.day_index < len(plan["days"]):
            raise HTTPException(422, "请明确要修改第几天，日期索引必须在当前行程范围内。")
        day = plan["days"][edit.day_index]
        if edit.module != "day":
            entries = day[edit.module]
            if edit.item_index is None or not 0 <= edit.item_index < len(entries):
                raise HTTPException(422, "请明确该日要替换的景点或餐次。")
    elif (edit.day_index is not None and plan.get("accommodation_version", 0) < 11) or edit.item_index is not None:
        raise HTTPException(422, "酒店是全行程模块，不接受日期或条目索引。")
    if edit.module == "hotel" and req.fixed_hotel:
        raise HTTPException(422, "当前酒店由用户指定，不能局部替换；请先明确修改指定酒店要求。")
    if edit.module == "hotel" and plan["hotel_nights"] == 0:
        raise HTTPException(422, "本行程无需住宿，不能添加酒店费用。")
    if edit.module == "day" and edit.item_index is not None:
        raise HTTPException(422, "整日景点调整不接受单个条目索引。")
    targets = _target_stays(plan, edit) if edit.module == 'hotel' and plan.get('accommodation_version', 0) >= 11 else []
    from .price_catalog import apply_prices
    kind = "attractions" if edit.module == "day" else edit.module
    service_date = day["date"] if edit.module != "hotel" else targets[0]["check_in"] if targets else req.start_date
    priced = apply_prices(req.city, {"attractions": "attraction", "hotel": "hotel", "meals": "restaurant"}[kind],
                          pools[kind], service_date=service_date, nights=plan["hotel_nights"])
    if kind == 'hotel':
        from .platform_hotels import quoted
        priced = [quoted(h, service_date) for h in priced]
    selected, usage = _select(edit, _eligible(priced, kind, req), plan, req)
    selected_ids = [_id(item) for item in selected]
    if edit.module == "hotel":
        existing_ids = list(dict.fromkeys(_id(s["hotel"]) for s in targets)) if targets else ([_id(plan["hotel"])] if plan.get("hotel") else [])
    elif edit.module == "day":
        existing_ids = [_id(a) for a in day["attractions"]]
    elif edit.module == "attractions":
        existing_ids = [_id(day["attractions"][edit.item_index])]
    else:
        existing_ids = [day["meals"][edit.item_index].get("restaurant_id")]
    if selected_ids == existing_ids:
        raise HTTPException(422, "选择与现有安排相同，没有产生修改。")
    before = copy.deepcopy(plan)
    if edit.module == 'hotel' and targets:
        from .stays import attach_boundaries
        for stay in targets:
            stay['hotel'] = _dict(selected[0])
            stay['hotel']['nights'] = 1
            stay['reason'] = '用户明确选择；按本晚日期重算价格与跨日路线。'
        _stay_summary(plan)
        attach_boundaries(plan, req, endpoints=state.get('trip_endpoints'))
        changed_dates = {s['check_in'] for s in targets}
        affected = [i for i, d in enumerate(plan['days']) if d['date'] in changed_dates or (i > 0 and plan['days'][i-1]['date'] in changed_dates)]
        summary = '仅更换所选住宿夜晚，保留其他酒店、景点和三餐，重算关联日期路线和整程预算。'
    elif edit.module == "hotel":
        plan["hotel"] = _dict(selected[0])
        plan["hotel"]["nights"] = plan["hotel_nights"]
        plan["hotel_reason"] = "用户在已核验酒店候选中明确选择；路线和预算按新酒店重新核算。"
        affected = list(range(len(plan["days"])))
        summary = f"仅将全程酒店替换为「{selected[0].name}」，保留景点和三餐，重算通勤与预算。"
    else:
        if edit.module == "day":
            day["attractions"] = [_dict(item) for item in selected]
        elif edit.module == "attractions":
            day["attractions"][edit.item_index] = _dict(selected[0])
        else:
            day["meals"][edit.item_index] = _meal(selected[0], day["meals"][edit.item_index])
        if edit.module in ("day", "attractions"):
            day["description"] = "按本次局部修改安排：" + "、".join(a["name"] for a in day["attractions"]) + "。"
        affected = [edit.day_index]
        label = {"meals": "餐饮", "attractions": "景点", "day": "景点顺序"}[edit.module]
        summary = f"仅修改第 {edit.day_index + 1} 天的{label}，保留其他模块，重算该日路线和总预算。"
    if plan == before:
        raise HTTPException(422, "选择与现有安排相同，没有产生修改。")
    parsed = TripPlan.model_validate(plan)
    from .intercity import effective_request
    error = _validate_structure(parsed.days, req.start_date, req.travel_days, effective_request(req, plan) if parsed.accommodation_version >= 11 else None)
    report, constraint_error = _validate_constraints(parsed.days, state.get("must_visit_resolved") or [], pools["attractions"])
    if error or constraint_error:
        raise HTTPException(422, error or constraint_error)
    independent = verify_plan({"plan": plan, "validate_error": None})
    if independent.get("validate_error") and current['result'].get('success'):
        raise HTTPException(422, independent["validate_error"])
    # Copy the prior report's hotel provenance while updating attraction checks.
    old_report = plan.get("constraint_report") or {}
    report.hotel_source = old_report.get("hotel_source", "agent")
    report.hotel_anchor = old_report.get("hotel_anchor")
    plan["constraint_report"] = report.model_dump()
    # Old route/schedule values are invalid once a stop changes. The preview
    # deliberately leaves these derived fields empty until confirmation.
    for index in affected:
        for key in ("route_legs", "schedule", "comfort_warnings"):
            plan["days"][index][key] = []
        for key in ("travel_minutes", "visit_minutes", "rest_minutes", "total_minutes"):
            plan["days"][index][key] = 0
    plan = _refresh_prices(plan, req, state)
    budget_state = {**state, "plan": plan, "hotel": Hotel.model_validate(plan["hotel"]) if plan.get("hotel") else None}
    budget_state.update(patch_budget(budget_state))
    budget_state.update(verify_budget(budget_state))
    preview = TripPlanResponse(success=False, error_code="EDIT_PENDING", data=budget_state["plan"], thread_id=current["result"].get("thread_id"),
                               message="局部修改草稿尚未确认；确认后重新计算受影响路线和时间，并校验预算与舒适度。")
    pending = {"edit": edit.model_dump(), "base_thread_id": current["result"].get("thread_id"),
               "summary": summary, "changed_days": affected, "preview": preview.model_dump(mode="json")}
    return pending, usage


def apply(current, pending, run_id):
    """Commit candidate construction, not conversation state; caller commits atomically."""
    if pending["base_thread_id"] != (current.get("result") or {}).get("thread_id"):
        raise HTTPException(409, "原行程已变化，请重新预览本次修改。")
    started = time.perf_counter()
    req, _, state, _ = _context(current)
    plan = copy.deepcopy(pending["preview"]["data"])
    plan = _refresh_prices(plan, req, state)
    hotel = Hotel.model_validate(plan["hotel"]) if plan.get("hotel") else None
    state.update(plan=plan, hotel=hotel, hotel_reason=plan.get("hotel_reason", ""),
                 experience_enabled=True, preserve_meals=True, usage={}, tool_calls=[], errors=[], trace=[],
                 validate_error=None, delivery_errors=[], budget_retries=2, retries=0,
                 route_query_count=0, _experience_tool_calls=[])
    from .itinerary_experience import enrich_experience
    plan, errors, warnings = enrich_experience(plan, state, day_indices=set(pending["changed_days"]))
    state["tool_calls"] = list(state.get("_experience_tool_calls") or [])
    state["plan"] = plan
    state.update(patch_budget(state))
    state.update(verify_budget(state))
    payload = {**state["plan"], "hotel": _dict(hotel) if hotel else None,
               "hotel_nights": state["nights"], "hotel_reason": plan.get("hotel_reason", "")}
    checked, delivery_errors = audit(payload, req, state)
    errors = list(dict.fromkeys([*errors, *delivery_errors]))
    verdict = state.get("budget_verdict") or {}
    if verdict.get("status") in ("known_exceeded", "estimated_exceeded"):
        errors.append("局部修改后预算超限：" + verdict.get("basis", ""))
    if state.get("validate_error"):
        errors.append(str(state["validate_error"]))
    state["delivery_errors"] = errors
    state["plan"] = checked or payload
    output_warnings = list(dict.fromkeys([*(plan.get("warnings") or []), *warnings, *(state.get("warnings") or [])]))
    state["plan"]["warnings"] = output_warnings
    state["warnings"] = output_warnings
    success = checked is not None and not errors
    if success:
        from . import catalog
        try:
            catalog.persist_plan(TripPlan.model_validate(state["plan"]), req.city,
                                 attractions=state["attractions"], hotels=state["hotels"],
                                 restaurants=[Restaurant.model_validate(r) for r in state["restaurants"]])
        except Exception:
            logger.exception("局部修改结果的已核验实体入库失败")
            output_warnings.append("本次行程已完成，但地点资料入库失败；可保留行程，稍后重试保存。")
            state["plan"]["warnings"] = output_warnings
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    state['usage'] = state.pop('_ticket_usage', {})
    delivery_logging.record(run_id, state, success, elapsed_ms, output_warnings)
    delivery_logging.log_run(thread_id=run_id, fingerprint="partial-edit:" + run_id, cached=False, success=success,
                  llm_calls=state['usage'].get('llm_calls',0), prompt_tokens=state['usage'].get('prompt_tokens',0), completion_tokens=state['usage'].get('completion_tokens',0),
                  elapsed_ms=elapsed_ms, tool_calls=len(state.get("tool_calls") or []), plan_days=len(plan["days"]),
                  budget_total=int((state["plan"].get("budget") or {}).get("total") or 0))
    return TripPlanResponse(success=success, data=state["plan"] if checked is not None else None, thread_id=run_id,
                            message="局部修改已完成，其他模块已保留。" if success else "局部修改未通过校验，原行程已保留：" + "；".join(errors[:3]),
                            error_code=None if success else "INVALID_MODULE_EDIT", warnings=output_warnings)
