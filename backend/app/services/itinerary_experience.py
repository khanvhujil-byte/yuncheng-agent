"""Fact-bound meals, bounded route queries and deterministic comfort scheduling.

The model chooses attractions; this module owns restaurant identity, money and
the clock. Geometric fallbacks are labelled estimates, never AMap route results.
"""
import copy
import math

from ..config import get_settings
from ..models.schemas import Hotel, Location, Restaurant
from . import map_gateway as amap
from .tools import haversine_km
from .tool_result import ToolResult
from .tool_retry import RetryPolicy, run

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


def _select_meals(day, restaurants, hotel, used, preserve=False, budget_limit=None, needs=None):
    from ..models.requirements import FoodNeeds
    from .requirements import restaurant_allowed
    needs = needs or FoodNeeds()
    restaurants = [r for r in restaurants if restaurant_allowed(r, needs)]
    pool = {r.poi_id: r for r in restaurants}
    existing = {m.get("type"): m for m in day.get("meals", [])}
    spots = day.get("attractions") or []
    meals = []
    for kind, index in (("breakfast", 0), ("lunch", max(0, (len(spots)-1)//2)), ("dinner", -1)):
        old = existing.get(kind) or {}
        if preserve and old.get("restaurant_id") and old["restaurant_id"] not in pool:
            # A local edit must not silently replace another saved meal to make
            # a new/expired constraint pass. Audit it and retain the old plan.
            meals.append(copy.deepcopy(old))
            continue
        chosen = pool.get(old.get("restaurant_id")) if preserve or needs.priority == "food_first" else None
        target = (_loc(hotel) if kind == "breakfast" and hotel else None) or (_loc(spots[index]) if spots else None)
        if chosen is None and restaurants:
            def score(r):
                distance = haversine_km(target, r.location) if target else 0
                # Verified ranking is a preference within a sensible neighbourhood.
                if needs.travel_scope == "citywide":
                    return (r.poi_id in used, r.ranking_rank is None, r.ranking_rank or 9999,
                            -(r.rating or 0), distance, r.poi_id)
                return (distance > 3, r.poi_id in used, distance + (0 if r.ranking_name else 1.5)
                        + ((r.avg_cost or MEAL_ESTIMATES[kind])/100 if budget_limit else 0),
                        -(r.rating or 0), r.poi_id)
            chosen = min(restaurants, key=score)
        if chosen:
            used.add(chosen.poi_id)
            meals.append(copy.deepcopy(old) if preserve and old.get("restaurant_id") == chosen.poi_id else meal_from_restaurant(chosen, kind))
        else:
            meals.append({"type": kind, "name": {"breakfast":"早餐", "lunch":"午餐", "dinner":"晚餐"}[kind] + "（尚无已核验餐厅）",
                          "estimated_cost": None, "cost_status":"unknown", "cost_source":"unknown",
                          "source":"degraded", "cost_note":"未获取真实餐饮候选，餐厅与费用待确认。"})
    return meals


def _ordered(day):
    spots = day.get("attractions") or []
    meals = {m["type"]:m for m in day.get("meals", [])}
    result = []
    if meals.get("breakfast"):
        result.append(("meal", meals["breakfast"], 30))
    split = max(1, math.ceil(len(spots)/2))
    for i, spot in enumerate(spots):
        result.append(("attraction", spot, max(30, int(spot.get("visit_duration") or 120))))
        if i+1 == split and meals.get("lunch"):
            result.append(("meal", meals["lunch"],45))
    if meals.get("dinner"):
        result.append(("meal",meals["dinner"],60))
    return result


def _route(origin, destination, mode, city, cache, quota, calls):
    a, b = _loc(origin), _loc(destination)
    key = f"{a.longitude},{a.latitude}>{b.longitude},{b.latitude}|{mode}|{city}"
    note = ""
    if key in cache:
        raw = cache[key]
    elif quota[0] < MAX_ROUTE_QUERIES:
        raw={}
        def attempt():
            nonlocal raw
            if quota[0]>=MAX_ROUTE_QUERIES:
                return ToolResult.failure("已达到本次路线查询上限","ROUTE_QUERY_LIMIT")
            quota[0]+=1
            try:
                raw=amap.travel_route(a,b,mode=mode,city=city)
                if isinstance(raw,dict) and raw.get("status")=="known" and raw.get("duration_minutes") is not None:
                    return ToolResult.success("已取得真实路线时间",returned_count=1)
                return ToolResult.success("未找到可用路线",returned_count=0)
            except (amap.AmapError,amap.AmapUnreachable,ValueError,TypeError) as exc:
                code=getattr(exc,"code","INVALID_ROUTE_RESULT")
                if code in ("AMAP_KEY_INVALID","AMAP_QUOTA_EXCEEDED"):
                    quota[0]=MAX_ROUTE_QUERIES
                return ToolResult.failure("路线接口查询失败",code,retryable=bool(getattr(exc,"retryable",False)),
                                          retry_after_seconds=getattr(exc,"retry_after_seconds",None))
        settings=get_settings()
        report=run(attempt,safe=True,policy=RetryPolicy(settings.tool_max_attempts,settings.tool_retry_budget_seconds,settings.tool_retry_base_delay))
        calls.append({"role":"experience","tool":"travel_route","args":{"origin":a.model_dump(),"destination":b.model_dump(),"mode":mode,"city":city},
                      "result":report.result.message,"outcome":report.result.to_dict(),"attempts":report.attempts,"attempt_count":len(report.attempts),
                      "stop_reason":report.stop_reason,"next_action":report.next_action})
        if report.result.status=="error":
            note=f"路线接口未取得结果（{report.result.error_code}）。"
    else:
        raw = {}
        note = "已达到本次路线查询上限。"
    raw = raw if isinstance(raw, dict) else {}
    if raw.get("status") == "known" and isinstance(raw.get("duration_minutes"), (int,float)) and math.isfinite(raw["duration_minutes"]) and raw["duration_minutes"] >= 0:
        result = {"duration_minutes":math.ceil(raw["duration_minutes"]), "distance_meters":raw.get("distance_meters"),
                  "status":"known", "source":"amap", "note":"高德路线接口结果，出发时路况仍可能变化。"}
    else:
        distance = haversine_km(a,b)
        walking = mode in ("步行","walking")
        speed = 4 if walking else (12 if mode in ("公共交通","transit") else 22)
        result = {"duration_minutes":max(5, math.ceil(distance*1.5/speed*60)+(0 if walking else 10)),
                  "distance_meters":None,"status":"estimated","source":"static_estimate",
                  "note":note+"按直线距离×1.5与交通速度估算；未获取实际道路路线。"}
    cache[key] = result
    return result


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


def enrich_experience(plan, state, day_indices=None):
    result=copy.deepcopy(plan)
    if not state.get("experience_enabled"):
        return result, [], []
    request=state["request"]
    restaurants=[r for raw in state.get("restaurants",[]) if (r:=_restaurant(raw)) is not None]
    hotel=_dump(state.get("hotel") or result.get("hotel"))
    if hotel is not None:
        hotel=_dump(Hotel.model_validate(hotel))
    cache=state.setdefault("route_cache",{})
    quota=[int(state.get("route_query_count") or 0)]
    warnings=[]
    calls=[]
    used=set()
    selected=set(day_indices) if day_indices is not None else None
    if selected is not None:
        used={m.get("restaurant_id") for d in result.get("days",[]) if d.get("day_index") not in selected for m in d.get("meals",[])}
    for day in result.get("days",[]):
        if selected is not None and day.get("day_index") not in selected:
            continue
        day["meals"]=_select_meals(day,restaurants,hotel,used,state.get("preserve_meals",False),getattr(request,"budget_limit",None),request.requirements.food)
        sequence=_ordered(day)
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
            minimum = {"lunch": 12*60, "dinner": 18*60}.get(item.get("type"), cursor) if kind == "meal" else cursor
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
        severe,hints=_weather_problems(day,state.get("weather",[]))
        if any(r["status"]!="known" for r in legs):
            hints.append("部分路程时间为规则估算，已计入交通缓冲；请出发前查看实时路线。")
        day.update(route_legs=legs,schedule=schedule,visit_minutes=sum(d for k,_,d in sequence if k=="attraction"),
                   travel_minutes=sum(l["duration_minutes"] for l in legs),rest_minutes=rest,total_minutes=cursor-(8*60+30),comfort_warnings=hints+severe)
        warnings.extend(f"{day['date']}：{h}" for h in hints)
    result["city_characteristics"]=city_characteristics(request.city)
    state["_experience_tool_calls"]=calls
    state["route_query_count"]=quota[0]
    from .requirements import audit_requirements
    errors = validate_experience(result, state)
    errors.extend(audit_requirements({**result, "hotel": hotel,
                                     "weather_info": [_dump(w) for w in state.get("weather", [])]}, request))
    return result, list(dict.fromkeys(errors)), warnings


def enrich_node(state):
    if not state.get("experience_enabled") or not (state.get("plan") or {}).get("days"):
        return {}
    plan, errors, warnings=enrich_experience(state["plan"],state)
    return {"plan":plan,"warnings":warnings,"route_cache":state.get("route_cache",{}),
            "route_query_count":state.get("route_query_count",0),
            "tool_calls":state.get("_experience_tool_calls",[]),
            "validate_error":state.get("validate_error") or (errors[0] if errors else None),
            "errors":[f"experience: {e}" for e in errors]}
