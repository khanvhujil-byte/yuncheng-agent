"""Fact-bound enrichment and deterministic audit of a joint model decision.

For joint plans, restaurant/hotel identities are preserved. Code retrieves
ticket evidence, calculates routes and schedules, and reports failures for
bounded replanning. The former selector remains for old plans/local edits.
"""
import copy
from collections import Counter
import math
from time import monotonic

from ..config import get_settings
from ..models.schemas import Hotel, Location, Restaurant
from . import map_gateway as amap  # noqa: F401 -- compatibility for existing adapter integrations
from .tools import haversine_km
from .schedule_repair import (build_sequence, can_reduce, can_trim, estimated_minutes,
                              nearest_order, remove_one_optional,
                              simulate, visit_minutes)

MAX_ROUTE_QUERIES = 48
MAX_COMFORT_MINUTES = 660
MEAL_MINUTES = {"breakfast": 30, "lunch": 45, "dinner": 60}
MEAL_ESTIMATES = {"breakfast": 25, "lunch": 60, "dinner": 80}
CITY_GUIDES = {
    "杭州": ["西湖沿线步行较多，湖区与城区分片安排。", "龙井茶、杭帮菜可作检索关键词；餐厅仍需以真实 POI 为准。"],
    "成都": ["优先留出茶馆、川菜体验时间，减少跨区折返。", "川菜可能辛辣，点单时确认辣度；不要推定所有餐厅都可免辣。"],
    "重庆": ["山地高差和跨江通行明显，直线距离不能当成步行耗时。", "火锅、小面是城市饮食关键词，需确认辣度与具体餐厅。"],
    "北京": ["景点尺度大，博物馆、宫殿参观留足时间，预约与开放时间需出发前核验。", "烤鸭、京味小吃可用于餐饮检索。"],
    "西安": ["城墙、古城与远郊景点分片安排，避免同日反复跨区。", "面食、泡馍可用于餐饮检索，清真餐厅的饮食习惯需尊重。"],
    "上海": ["沿江、历史街区与博物馆按片区组合，给步行和排队留余量。", "本帮菜、生煎可用于餐饮检索。"],
    "广州": ["给早茶与粤菜体验留完整用餐时间，夏季户外安排注意炎热和降雨。", "早茶是文化建议，不代表每家店都提供早茶。"],
}


def city_characteristics(city):
    """Editorial rules, not a fabricated live city-data lookup."""
    key = str(city).removesuffix("市")
    return ["城市体验建议来源：项目维护的静态城市指南，非实时榜单。"] + CITY_GUIDES.get(
        key, ["按实际景点坐标分片游览，尊重当地饮食习惯，开放时间与预约需出发前核验。"])


def _dump(value):
    return value.model_dump() if hasattr(value, "model_dump") else value


def _loc(value):
    value = _dump(value)
    raw = value.get("location") if isinstance(value, dict) else None
    try:
        return Location.model_validate(raw) if raw else None
    except (ValueError, TypeError):
        return None


def _restaurant(raw):
    try:
        return Restaurant.model_validate(_dump(raw))
    except (ValueError, TypeError):
        return None


def meal_from_restaurant(restaurant, meal_type):
    item = restaurant if isinstance(restaurant, Restaurant) else Restaurant.model_validate(restaurant)
    amount = item.avg_cost
    # AMap human-average consumption is a guide, not a guaranteed bill.
    status = "estimated"
    source = item.cost_source if amount is not None else "static_estimate"
    note = "数据源提供的人均参考价，实际以点单为准。" if amount is not None else "餐厅未提供人均价格，按每人每餐固定规则预留；不是餐厅报价。"
    return {
        "type": meal_type, "name": item.name, "address": item.address,
        "location": item.location.model_dump(), "restaurant_id": item.poi_id,
        "description": "已核验餐饮地点；按当天动线推荐，营业时间需到店前确认。",
        "estimated_cost": amount if amount is not None else MEAL_ESTIMATES.get(meal_type, 60),
        "cost_status": status, "cost_source": source, "cost_note": note,
        "source": item.source, "ranking_name": item.ranking_name,
        "ranking_rank": item.ranking_rank, "ranking_url": item.ranking_url,
        "evidence_url":item.evidence_url,
    }


def _select_meals(day, restaurants, hotel, used, preserve=False, budget_limit=None, needs=None,
                  replace_invalid=False):
    from ..models.requirements import FoodNeeds
    from .requirements import restaurant_allowed
    needs = needs or FoodNeeds()
    legacy_used = used if isinstance(used, set) else None
    if legacy_used is not None:
        used = Counter(used)
    restaurants = [r for r in restaurants if restaurant_allowed(r, needs)]
    pool = {r.poi_id: r for r in restaurants}
    existing = {m.get("type"): m for m in day.get("meals", [])}
    meals = []
    from .food_recommendation import choose_restaurant, distances, meal_contexts, nearby_pool
    today = set()
    for kind, anchor, following in meal_contexts(day, hotel):
        old = existing.get(kind) or {}
        if preserve and old.get("restaurant_id") and old["restaurant_id"] not in pool:
            # A local edit must not silently replace another saved meal to make
            # a new/expired constraint pass. Audit it and retain the old plan.
            meals.append(copy.deepcopy(old))
            used[old["restaurant_id"]] += 1
            today.add(old["restaurant_id"])
            continue
        chosen = pool.get(old.get("restaurant_id")) if preserve or needs.priority == "food_first" else None
        if chosen is not None and replace_invalid:
            # Unified planning preserves the model's meal identities, but a
            # preserved choice that violates the same deterministic rules the
            # audit uses would otherwise force a full model replan (or be
            # delivered as a partial failure). Replace only when a feasible
            # alternative exists; shortages stay visible to the audit.
            feasible = nearby_pool(restaurants, anchor, following, kind,
                                   citywide=needs.travel_scope == "citywide")
            if feasible:
                feasible_ids = {r.poi_id for r in feasible}
                used_ids = {key for key, count in used.items() if count}
                duplicate_with_alternative = (chosen.poi_id in used_ids
                                              and bool(feasible_ids - today - used_ids))
                too_far = (needs.travel_scope != "citywide"
                           and distances(chosen, anchor, following)[0] > 4
                           and any(distances(r, anchor, following)[0] <= 4 for r in feasible))
                if chosen.poi_id not in feasible_ids or duplicate_with_alternative or too_far:
                    chosen = None
        if chosen is None and restaurants:
            chosen = choose_restaurant(restaurants, kind, anchor, following, used, today, needs, budget_limit)
        if chosen:
            used[chosen.poi_id] += 1
            today.add(chosen.poi_id)
            meals.append(copy.deepcopy(old) if preserve and old.get("restaurant_id") == chosen.poi_id else meal_from_restaurant(chosen, kind))
        else:
            meals.append({"type": kind, "name": {"breakfast":"早餐", "lunch":"午餐", "dinner":"晚餐"}[kind] + "（尚无已核验餐厅）",
                          "estimated_cost": None, "cost_status":"unknown", "cost_source":"unknown",
                          "source":"degraded", "cost_note":"未获取真实餐饮候选，餐厅与费用待确认。"})
    if legacy_used is not None:
        legacy_used.update(used)
    return meals


def _ordered(day, hotel=None, mode="公共交通", minutes=None, comfort_limit=660):
    return build_sequence(day, hotel, mode, minutes, comfort_limit)


def _route(origin, destination, mode, city, cache, quota, calls):
    from .route_batch import prefetch_routes, route_key
    key = route_key(origin, destination, mode, city)
    if key not in cache:
        batch = prefetch_routes([(origin, destination)], mode=mode, city=city, cache=cache,
                                query_count=quota[0], max_queries=MAX_ROUTE_QUERIES, budget_seconds=0)
        quota[0] = batch.query_count
        calls.extend(batch.calls)
    # Batch preparation owns cache validation. Do not normalize estimates as
    # if they were fresh provider results, and keep cache timestamps internal.
    return {name: value for name, value in cache[key].items() if not name.startswith("_")}


def _clock(minutes):
    return f"{minutes//60:02d}:{minutes%60:02d}"


def _weather_problems(day, weather):
    item = next((_dump(w) for w in weather if _dump(w).get("date")==day.get("date")), None)
    if not item or item.get("beyond_window"):
        return [], ["本日天气未知，保留室内备选，出发前重新查询。"]
    text = str(item.get("day_weather") or "")
    if not text:
        return [], ["本日未获取有效天气，不能据此保证户外游玩舒适。"]
    try:
        hot = int(item.get("day_temp") or 0) >= 38
    except (ValueError,TypeError):
        hot = False
    extreme = any(t in text for t in ("暴雨","大雨","雷","暴雪","沙尘","台风")) or hot
    rainy = any(t in text for t in ("雨","雪"))
    outdoor = [s for s in day.get("attractions",[]) if any(t in str(s.get("category") or "")+s.get("name","") for t in ("公园","风景","山","湖","海滩","动物园","植物园","古道","湿地"))]
    if extreme and outdoor:
        return [f"{day['date']} 预报 {text}{' / 高温' if hot else ''}，仍安排户外景点 {'、'.join(s['name'] for s in outdoor)}；请换到其他日期或选择室内候选。"], []
    if rainy and outdoor:
        return [], [f"本日预报{text}，户外景点需雨具与室内备选；没有将未知开放状态当成可游玩保证。"]
    return [], []


def _clock_minutes(value):
    try:
        hours, minutes = str(value).split(":")
        return int(hours) * 60 + int(minutes)
    except (ValueError, AttributeError):
        return None


def _day_over_limit(day, max_minutes):
    """Comfort check against the audited clock (same limits as validate_experience)."""
    if int(day.get("total_minutes") or 0) > max_minutes:
        return True
    for entry in day.get("schedule") or []:
        if entry.get("kind") != "meal":
            continue
        start = _clock_minutes(entry.get("start_time"))
        limit = {"lunch": 14 * 60, "dinner": 20 * 60 + 30}.get(entry.get("meal_type"))
        if start is not None and limit is not None and start > limit:
            return True
    return False


def validate_experience(plan, state=None):
    state = state or {}
    if not state.get("experience_enabled") and not any(d.get("schedule") for d in plan.get("days",[])):
        return []
    reasons=[]
    pool={str(_dump(r).get("poi_id")):_dump(r) for r in state.get("restaurants",[])} if "restaurants" in state else None
    for day in plan.get("days",[]):
        kinds=[m.get("type") for m in day.get("meals",[])]
        if sorted(kinds)!=["breakfast","dinner","lunch"]:
            reasons.append(f"{day.get('date')} 必须有且仅有早中晚三餐。")
        for meal in day.get("meals",[]):
            if not meal.get("restaurant_id") or (pool is not None and meal["restaurant_id"] not in pool):
                reasons.append(f"{day.get('date')} 的{meal.get('name')}缺少已核验餐饮候选。")
            elif pool is not None:
                factual=pool[meal["restaurant_id"]]
                if meal.get("name") != factual.get("name") or _loc(meal) != _loc(factual):
                    reasons.append(f"{day.get('date')} 的餐厅名称或坐标与真实候选不一致。")
        schedule=day.get("schedule") or []
        if not schedule:
            reasons.append(f"{day.get('date')} 尚未核算游玩、三餐与交通时间。")
        total=sum(int(s.get("duration_minutes") or 0) for s in schedule)
        route_minutes=sum(int(leg.get("duration_minutes") or 0) for leg in day.get("route_legs",[]))
        scheduled_travel=sum(int(s.get("duration_minutes") or 0) for s in schedule if s.get("kind")=="travel")
        if schedule and (day.get("total_minutes")!=total or day.get("travel_minutes")!=route_minutes or route_minutes!=scheduled_travel):
            reasons.append(f"{day.get('date')} 的总时长或交通时长无法逐项对账。")
        visits={s.get("reference_id") or s.get("name"):int(s.get("duration_minutes") or 0) for s in schedule if s.get("kind")=="attraction"}
        for spot in day.get("attractions",[]):
            if visits.get(spot.get("poi_id") or spot.get("name"),0)<max(30,int(spot.get("visit_duration") or 120)):
                reasons.append(f"{day.get('date')} 未给景点 {spot.get('name')} 留足建议游玩时间。")
        cursor=None
        meal_order=("breakfast","lunch","dinner")
        meal_position=0
        for entry in schedule:
            try:
                start=sum(int(p)*v for p,v in zip(entry["start_time"].split(":"),(60,1)))
                end=sum(int(p)*v for p,v in zip(entry["end_time"].split(":"),(60,1)))
                if (cursor is not None and cursor!=start) or end-start!=entry["duration_minutes"]:
                    raise ValueError("schedule conflict")
                cursor=end
                if entry.get("kind")=="meal":
                    # A restaurant can legitimately serve all three meals. Its
                    # POI/name identifies the place, never the meal occurrence.
                    expected_type=meal_order[meal_position] if meal_position<len(meal_order) else None
                    meal_type=entry.get("meal_type") or expected_type
                    meal_position+=1
                    if meal_type!=expected_type or expected_type is None:
                        reasons.append(f"{day.get('date')} 的时间表三餐顺序或餐次标识不正确。")
                    meal=next((m for m in day.get("meals",[]) if m.get("type")==meal_type),{})
                    if not meal or meal.get("restaurant_id")!=entry.get("reference_id") or meal.get("name")!=entry.get("name"):
                        reasons.append(f"{day.get('date')} 的时间表餐次与已安排餐厅无法对应。")
                    limit={"lunch":14*60,"dinner":20*60+30}.get(expected_type)
                    if limit is not None and start>limit:
                        reasons.append(f"{day.get('date')} 的{expected_type}开始时间{entry.get('start_time')}过晚，请减少前序游玩或通勤。")
            except (KeyError,ValueError,TypeError):
                reasons.append(f"{day.get('date')} 的时间表有重叠、缺口或时长不一致。")
                break
        if schedule and meal_position!=3:
            reasons.append(f"{day.get('date')} 的时间表必须分别安排早中晚三餐。")
        if total > MAX_COMFORT_MINUTES:
            reasons.append(f"{day.get('date')} 游玩、餐饮、交通与休息合计 {total} 分钟，超过舒适上限 {MAX_COMFORT_MINUTES} 分钟；请减少景点或按片区重排。")
        severe,_=_weather_problems(day,state.get("weather",plan.get("weather_info",[])))
        reasons.extend(severe)
    return reasons


def _schedule_day(day, sequence, hotel, request, cache, quota, calls):
    """Build the auditable clock from actual cached routes, never from scores."""
    schedule=[]
    legs=[]
    cursor=8*60+30
    previous=hotel if hotel and _loc(hotel) else None
    active=0
    rest=0
    for position,(kind,item,duration) in enumerate(sequence + ([("return",hotel,0)] if hotel and _loc(hotel) else [])):
        if previous and _loc(previous) and _loc(item):
            route=_route(previous,item,request.transportation,request.city,cache,quota,calls)
            leg={"from_name":previous["name"],"to_name":item["name"],"from_id":previous.get("poi_id") or previous.get("restaurant_id"),
                 "to_id":item.get("poi_id") or item.get("restaurant_id"),"mode":request.transportation,**route}
            legs.append(leg)
            minutes=route["duration_minutes"]
            schedule.append({"kind":"travel","name":f"{previous['name']} → {item['name']}","start_time":_clock(cursor),"end_time":_clock(cursor+minutes),"duration_minutes":minutes,"status":route["status"]})
            cursor+=minutes
            buffer=max(5,math.ceil(minutes*.2))
            schedule.append({"kind":"rest","name":"交通缓冲","start_time":_clock(cursor),"end_time":_clock(cursor+buffer),"duration_minutes":buffer})
            cursor+=buffer
            rest+=buffer
        if kind=="return":
            break
        minimum = item.get("_earliest_start", {"lunch": 12*60, "dinner": 18*60}.get(item.get("type"), cursor)) if kind == "meal" else cursor
        if cursor < minimum:
            spare = minimum-cursor
            schedule.append({"kind":"rest","name":"自由活动与用餐前弹性时间","start_time":_clock(cursor),"end_time":_clock(minimum),"duration_minutes":spare})
            rest+=spare
            cursor=minimum
        schedule.append({"kind":kind,"name":item["name"],"start_time":_clock(cursor),"end_time":_clock(cursor+duration),"duration_minutes":duration,
                         "reference_id":item.get("poi_id") or item.get("restaurant_id"),"status":"estimated",
                         **({"meal_type":item["type"]} if kind=="meal" else {})})
        cursor+=duration
        active+=duration if kind=="attraction" else 0
        if active>=120 and position<len(sequence)-1:
            schedule.append({"kind":"rest","name":"休息与弹性时间","start_time":_clock(cursor),"end_time":_clock(cursor+15),"duration_minutes":15})
            cursor+=15
            rest+=15
            active=0
        if _loc(item):
            previous=item
    day.update(route_legs=legs,schedule=schedule,visit_minutes=sum(d for k,_,d in sequence if k=="attraction"),
               travel_minutes=sum(l["duration_minutes"] for l in legs),rest_minutes=rest,total_minutes=cursor-(8*60+30))


def enrich_experience(plan, state, day_indices=None):
    result=copy.deepcopy(plan)
    if not state.get("experience_enabled"):
        return result, [], []
    request=state["request"]
    from .ticket_pricing import refresh_plan_tickets
    result = refresh_plan_tickets(result, request, day_indices, lookup_state=state)
    extra_calls, extra_warnings = [], []
    if not state.get("preserve_meals"):
        from .food import ensure_food_coverage
        extra_calls, extra_warnings = ensure_food_coverage(result, state)
    restaurants=[r for raw in state.get("restaurants",[]) if (r:=_restaurant(raw)) is not None]
    hotel=_dump(state.get("hotel") or result.get("hotel"))
    if hotel is not None:
        hotel=_dump(Hotel.model_validate(hotel))
    cache=state.setdefault("route_cache",{})
    quota=[int(state.get("route_query_count") or 0)]
    warnings=list(extra_warnings)
    calls=list(extra_calls)
    calls.extend(state.pop('_ticket_search_calls', []))
    used=Counter()
    selected=set(day_indices) if day_indices is not None else None
    if selected is not None:
        used=Counter(m.get("restaurant_id") for d in result.get("days",[]) if d.get("day_index") not in selected for m in d.get("meals",[]))
    from .route_batch import cached_route, prefetch_routes, route_key
    settings=get_settings()
    route_deadline=monotonic()+settings.route_batch_budget_seconds
    blocked_code=state.get("route_blocked_code")

    def duration(origin, destination):
        cached = cached_route(cache.get(route_key(origin, destination, request.transportation, request.city)))
        return (cached["duration_minutes"] if cached is not None
                else estimated_minutes(origin, destination, request.transportation))

    prepared=[]
    automatic=can_reduce(state, day_indices)
    # Unified planning owns the meal identities, but a preserved choice that
    # violates the same deterministic rules the audit uses would force a full
    # model replan. Allow a nearby eligible replacement only when one exists.
    replace_invalid_meals = (bool(state.get('unified_planning'))
                             and not bool(state.get('preserve_meals'))
                             and request.requirements.food.priority != 'food_first')
    meal_days=set()

    def select_day_meals(day):
        identity=day.get('day_index')
        if identity in meal_days:
            for meal in day.get('meals',[]):
                identifier=meal.get('restaurant_id')
                if identifier:
                    used[identifier]=max(0,used[identifier]-1)
        day['meals']=_select_meals(day,restaurants,hotel,used,state.get('preserve_meals',False) or state.get('unified_planning',False),
                                   getattr(request,'budget_limit',None),request.requirements.food,
                                   replace_invalid=replace_invalid_meals)
        meal_days.add(identity)

    for day in result.get("days",[]):
        if selected is not None and day.get("day_index") not in selected:
            continue
        if automatic:
            day["attractions"]=nearest_order(day.get("attractions",[]),hotel,request.transportation)
        select_day_meals(day)
        sequence=_ordered(day,hotel,request.transportation,duration,
                          request.requirements.pace.max_day_minutes)
        if automatic:
            # Solve obvious overload before any paid route calls. This uses
            # explicitly estimated geometry, then the independent audit below
            # still checks the actual route clock.
            while simulate(sequence,hotel,duration)["total_minutes"] > request.requirements.pace.max_day_minutes:
                note=remove_one_optional(day,state,hotel)
                if note is None:
                    break
                warnings.append(note)
                select_day_meals(day)
                sequence=_ordered(day,hotel,request.transportation,duration,
                          request.requirements.pace.max_day_minutes)
        prepared.append((day,sequence))

    def prefetch(items):
        nonlocal blocked_code
        pairs=[]
        for _,sequence in items:
            previous=hotel if hotel and _loc(hotel) else None
            for _,item,_ in sequence + ([("return",hotel,0)] if hotel and _loc(hotel) else []):
                if previous and _loc(previous) and _loc(item):
                    pairs.append((previous,item))
                if _loc(item):
                    previous=item
        batch=prefetch_routes(pairs,mode=request.transportation,city=request.city,cache=cache,
                              query_count=quota[0],max_queries=MAX_ROUTE_QUERIES,
                              max_workers=settings.route_max_workers,
                              budget_seconds=max(0,route_deadline-monotonic()),blocked_code=blocked_code)
        quota[0]=batch.query_count
        blocked_code=batch.blocked_code
        calls.extend(batch.calls)

    prefetch(prepared)
    for day,sequence in prepared:
        _schedule_day(day,sequence,hotel,request,cache,quota,calls)

    # At most one local repair pass after actual route times. Bridges created
    # by removing optional stops are fetched together; existing OD facts stay.
    repaired=[]
    if automatic:
        for day,sequence in prepared:
            timing=simulate(sequence,hotel,duration)
            changed=False
            while (timing["total_minutes"] > request.requirements.pace.max_day_minutes
                   or timing["late_meal_minutes"]):
                note=remove_one_optional(day,state,hotel)
                if note is None:
                    break
                warnings.append(note)
                changed=True
                select_day_meals(day)
                sequence=_ordered(day,hotel,request.transportation,duration,
                                  request.requirements.pace.max_day_minutes)
                timing=simulate(sequence,hotel,duration)
            if changed:
                repaired.append((day,sequence))
        if repaired:
            prefetch(repaired)
            for day,sequence in repaired:
                _schedule_day(day,sequence,hotel,request,cache,quota,calls)

    # Deterministic comfort fallback for unified planning. The model owns the
    # choice of stops and its retry budget is bounded, so when the audited clock
    # still overflows (or a meal would start too late) drop optional stops
    # locally instead of returning a partial result that cannot be executed.
    # Required anchors are protected by ``_protected``; user-edited meals are
    # protected by ``preserve_meals`` (can_trim is False then).
    if not automatic and can_trim(state):
        max_minutes = request.requirements.pace.max_day_minutes

        def estimate_clock(day, comfort):
            seq = _ordered(day, hotel, request.transportation, comfort_limit=comfort)
            timing = simulate(seq, hotel, duration)
            return timing["total_minutes"], timing["late_meal_minutes"]

        for round_index in range(3):
            over = [(day, sequence) for day, sequence in prepared if _day_over_limit(day, max_minutes)]
            if not over:
                break
            tightened = []
            for day, sequence in over:
                comfort = max(120, max_minutes - 20 * round_index)
                # Removing a stop changes the clock. Re-evaluate with the
                # simulated schedule after each removal; keeping the stale
                # audited schedule as the loop condition would delete stops
                # until the day has only one left (a real over-deletion defect).
                est_total, late = estimate_clock(day, comfort)
                removed = 0
                while (est_total > max_minutes or late) and removed < 6:
                    note = remove_one_optional(day, state, hotel)
                    if note is None:
                        break
                    warnings.append(note)
                    removed += 1
                    select_day_meals(day)
                    est_total, late = estimate_clock(day, comfort)
                sequence = _ordered(day, hotel, request.transportation, comfort_limit=comfort)
                tightened.append((day, sequence))
            prefetch(tightened)
            for day, sequence in tightened:
                _schedule_day(day, sequence, hotel, request, cache, quota, calls)

    for day,_ in prepared:
        severe,hints=_weather_problems(day,state.get("weather",[]))
        identities=[m.get("restaurant_id") for m in day.get("meals",[]) if m.get("restaurant_id")]
        if len(identities) != len(set(identities)):
            hints.append("本日重复安排同一餐厅：可能为保留已选餐次，或附近符合餐次与预算要求的候选不足；未为凑不同店增加远距离绕行。")
        if any(r["status"]!="known" for r in day["route_legs"]):
            hints.append("部分路程时间为规则估算，已计入交通缓冲；请出发前查看实时路线。")
        day["comfort_warnings"]=hints+severe
        warnings.extend(f"{day['date']}：{h}" for h in hints)
    result["city_characteristics"]=city_characteristics(request.city)
    state["_experience_tool_calls"]=calls
    state["route_query_count"]=quota[0]
    state["route_blocked_code"]=blocked_code
    from .requirements import audit_requirements
    errors = validate_experience(result, state)
    if state.get('unified_planning') and day_indices is None:
        from .unified_planning import audit_choices
        errors.extend(audit_choices(result, state, Hotel.model_validate(hotel) if hotel else None,
                                    warnings_out=warnings))
    errors.extend(audit_requirements({**result, "hotel": hotel,
                                     "weather_info": [_dump(w) for w in state.get("weather", [])]}, request))
    return result, list(dict.fromkeys(errors)), warnings


def enrich_node(state):
    if not state.get("experience_enabled") or not (state.get("plan") or {}).get("days"):
        return {}
    plan, errors, warnings=enrich_experience(state["plan"],state)
    # Re-run the independent audit on the *repaired* plan. A previous round's
    # validate_error may already have been solved by the deterministic repair
    # above; keeping it would mark a valid delivery as a partial result forever.
    # Any problem that still exists is reported again and stays visible.
    from ..agents.verify_plan import verify_plan as independent_audit
    audit = independent_audit({"plan": plan, "retries": state.get("retries", 0), "validate_error": None})
    audit_error = audit.get("validate_error")
    if audit.get("warnings"):
        warnings = [*warnings, *[str(item) for item in audit["warnings"]]]
    feedback = [str(audit_error)] if audit_error else []
    feedback.extend(errors)
    if feedback and state.get('unified_planning'):
        from .unified_planning import correction_context
        try:
            feedback.append(correction_context(plan, state, state.get('hotel')))
        except (ValueError, KeyError, TypeError):
            pass
    # Let a budget/comfort replan see prices actually obtained in this round.
    priced = {a.get('poi_id'): a for d in plan.get('days', []) for a in d.get('attractions', []) if a.get('poi_id')}
    candidates = []
    for candidate in state.get('attractions', []):
        evidence = priced.get(candidate.poi_id)
        fields = {k: v for k, v in (evidence or {}).items() if k.startswith('ticket_price') or k == 'admission_policy'}
        candidates.append(candidate.model_copy(update=fields))
    update={"plan":plan,"warnings":list(dict.fromkeys(warnings)),"route_cache":state.get("route_cache",{}),
            "attractions": candidates,
            "sources":({"food":state["sources"]["food"]} if state.get("sources",{}).get("food") else {}),
            "restaurants":state.get("restaurants",[]),
            "food_coverage_queries":state.get("food_coverage_queries",[]),
            "food_coverage_elapsed_seconds":state.get("food_coverage_elapsed_seconds",0),
            "food_coverage_blocked_code":state.get("food_coverage_blocked_code"),
            "ticket_lookup_queries":state.get("ticket_lookup_queries",[]),
            "ticket_lookup_results":state.get("ticket_lookup_results",{}),
            "ticket_estimates":state.get("ticket_estimates",{}),
            "ticket_estimate_attempted":state.get("ticket_estimate_attempted",False),
            "usage":state.pop('_ticket_usage', {}),
            "ticket_lookup_elapsed_seconds":state.get("ticket_lookup_elapsed_seconds",0),
            "route_query_count":state.get("route_query_count",0),
            "route_blocked_code":state.get("route_blocked_code"),
            "tool_calls":state.get("_experience_tool_calls",[]),
            "validate_error":"\n".join(dict.fromkeys(feedback)) if feedback else None,
            "errors":[f"experience: {e}" for e in errors]}
    # Optional-stop repair can change the distance explanation after selection.
    # Keep the chosen hotel and refresh the statement from the final stops.
    hotel=state.get("hotel")
    if hotel is not None:
        hotel=Hotel.model_validate(hotel).model_copy(deep=True)
        spots=[spot for day in plan.get("days",[]) for spot in day.get("attractions",[]) if _loc(spot)]
        if hotel.location and spots:
            weight=sum(max(1,visit_minutes(spot)) for spot in spots)
            distance=sum(haversine_km(hotel.location,_loc(spot))*max(1,visit_minutes(spot)) for spot in spots)/weight
            hotel.distance=f"距最终行程景点加权平均 {distance:.1f} km（直线估计）"
            update["hotel"]=hotel
    return update
