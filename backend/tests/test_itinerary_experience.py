import copy
import json
from tests.joint_fixtures import joint_payload

import pytest

from app.models.schemas import Attraction, Hotel, Location, Restaurant, TripRequest
from app.services import itinerary_experience as experience
from app.agents.budget import patch_budget


def setup_trip():
    req=TripRequest(city="杭州", start_date="2026-10-20", travel_days=1, transportation="公共交通", accommodation="经济型酒店")
    spots=[Attraction(name="室内博物馆",poi_id="a1",category="博物馆",location=Location(longitude=120,latitude=30),visit_duration=120,ticket_price=50,ticket_price_status="known")]
    restaurants=[Restaurant(name=f"真实餐厅{i}",poi_id=f"r{i}",location=Location(longitude=120+i*.001,latitude=30),avg_cost=40+i,avg_cost_status="estimated",cost_source="amap") for i in range(3)]
    plan={"city":"杭州","start_date":req.start_date,"end_date":req.start_date,"days":[{"date":req.start_date,"day_index":0,"attractions":[s.model_dump() for s in spots],"meals":[{"type":"lunch","name":"模型编造餐厅","estimated_cost":0}]}]}
    state={"request":req,"travel_days":1,"nights":0,"hotel":None,"restaurants":restaurants,"experience_enabled":True,"weather":[],"route_cache":{}}
    return plan,state


def test_real_food_routes_clock_and_complete_budget(monkeypatch):
    monkeypatch.setattr(experience.amap,"travel_route",lambda *a,**kw:{"duration_minutes":10,"distance_meters":1000,"status":"known","source":"amap"})
    plan,state=setup_trip()
    original=copy.deepcopy(plan)
    enriched,errors,warnings=experience.enrich_experience(plan,state)
    assert plan==original
    assert errors==[]
    day=enriched["days"][0]
    assert len(day["meals"])==3
    assert {m["restaurant_id"] for m in day["meals"]}=={"r0","r1","r2"}
    assert all(m["name"]!="模型编造餐厅" for m in day["meals"])
    assert day["travel_minutes"]==sum(l["duration_minutes"] for l in day["route_legs"])
    assert len(day["route_legs"])==3
    assert all(l["status"]=="known" for l in day["route_legs"])
    assert day["total_minutes"]==sum(s["duration_minutes"] for s in day["schedule"])
    assert next(s for s in day["schedule"] if s["name"]==day["meals"][-1]["name"])["start_time"] >= "18:00"
    budget=patch_budget({**state,"plan":enriched})["plan"]["budget"]
    assert budget["total_meals"]==123
    assert budget["total_attractions"]==50
    assert budget["total_transportation"]==40
    assert budget["total"]==213
    assert all(i["source"]=="amap" for i in budget["items"] if i["category"]=="meal")
    assert any("静态" in t for t in enriched["city_characteristics"])


def test_route_failure_estimated_and_cache_prevents_repeat(monkeypatch):
    calls=[]
    def unavailable(*a,**kw):
        calls.append(1)
        return {"status":"unknown","duration_minutes":None}
    monkeypatch.setattr(experience.amap,"travel_route",unavailable)
    plan,state=setup_trip()
    enriched,_,warnings=experience.enrich_experience(plan,state)
    first=len(calls)
    repeated,_,_=experience.enrich_experience(plan,state)
    assert first>0 and len(calls)==first
    assert all(l["status"]=="estimated" and l["distance_meters"] is None for l in enriched["days"][0]["route_legs"])
    assert any("估算" in w for w in warnings)
    assert repeated["days"][0]["travel_minutes"]>0


def test_food_failure_cannot_turn_invented_restaurant_into_success(monkeypatch):
    monkeypatch.setattr(experience.amap,"travel_route",lambda *a,**kw:{})
    plan,state=setup_trip()
    state["restaurants"]=[]
    enriched,errors,_=experience.enrich_experience(plan,state)
    assert errors and "核验餐饮" in errors[0]
    assert all(m["estimated_cost"] is None and m["cost_status"]=="unknown" for m in enriched["days"][0]["meals"])
    budget=patch_budget({**state,"plan":enriched})["plan"]["budget"]
    assert len([i for i in budget["items"] if i["category"]=="meal" and i["status"]=="unknown"])==3


def test_storm_outdoor_and_visit_times_reject_uncomfortable_plan(monkeypatch):
    monkeypatch.setattr(experience.amap,"travel_route",lambda *a,**kw:{"duration_minutes":15,"status":"known"})
    plan,state=setup_trip()
    plan["days"][0]["attractions"][0].update(name="湖边公园",category="风景名胜",visit_duration=700)
    state["weather"]=[{"date":state["request"].start_date,"day_weather":"暴雨","day_temp":20}]
    _,errors,_=experience.enrich_experience(plan,state)
    assert any("舒适上限" in e for e in errors)
    assert any("暴雨" in e and "湖边公园" in e for e in errors)


def test_partial_derive_preserves_other_days_and_meal_selection(monkeypatch):
    monkeypatch.setattr(experience.amap,"travel_route",lambda *a,**kw:{"duration_minutes":10,"status":"known"})
    plan,state=setup_trip()
    enriched,_,_=experience.enrich_experience(plan,state)
    next_day=copy.deepcopy(enriched["days"][0])
    next_day.update(date="2026-10-21",day_index=1)
    enriched["days"].append(next_day)
    original_other=copy.deepcopy(next_day)
    original_meals=copy.deepcopy(enriched["days"][0]["meals"])
    updated,_,_=experience.enrich_experience(enriched,{**state,"preserve_meals":True},{0})
    assert updated["days"][1]==original_other
    assert updated["days"][0]["meals"]==original_meals


def test_unknown_restaurant_cost_uses_explained_reference_rule():
    r=Restaurant(name="有店无价格",poi_id="r",location=Location(longitude=120,latitude=30))
    meal=experience.meal_from_restaurant(r,"lunch")
    assert meal["estimated_cost"]==60
    assert meal["cost_status"]=="estimated" and meal["cost_source"]=="static_estimate"
    assert "不是餐厅报价" in meal["cost_note"]


def test_active_graph_food_fanin_delivery_and_monitoring(monkeypatch):
    from app.agents import graph as graph_module
    from app.agents import planner_agent
    from app.models.schemas import WeatherInfo
    from app.services.planning import _initial_state, _finalize
    from app.services.monitoring import summary
    plan,state=setup_trip()
    req=state["request"]
    spots=[Attraction.model_validate(s) for s in plan["days"][0]["attractions"]]
    monkeypatch.setattr(graph_module,"validate_city",lambda s:{"adcode":"330100","city_center":Location(longitude=120,latitude=30)})
    monkeypatch.setattr(graph_module,"collect_attractions",lambda s:{"attractions":spots,"sources":{"attractions":"amap"}})
    monkeypatch.setattr(graph_module,"collect_weather",lambda s:{"weather":[WeatherInfo(date=req.start_date,day_weather="晴",day_temp=22)],"sources":{"weather":"amap"}})
    monkeypatch.setattr(graph_module,"collect_food",lambda s:{"restaurants":state["restaurants"],"sources":{"food":"amap"}})
    monkeypatch.setattr(graph_module,"resolve_hotel",lambda s:{"hotel":None})
    monkeypatch.setattr(experience.amap,"travel_route",lambda *a,**kw:{"duration_minutes":10,"distance_meters":900,"status":"known"})
    def compose(system,prompt,**kwargs):
        assert "真实餐厅0" in prompt and "city_characteristics" in prompt
        # Food/weather must both have completed before the model sees context.
        assert "晴" in prompt and '"ticket_price": 50' in prompt
        day=copy.deepcopy(plan["days"][0])
        day["meals"]=[{"type":k,"name":"模型编造餐厅"} for k in ("breakfast","lunch","dinner")]
        return json.dumps(joint_payload([day],state["restaurants"]),ensure_ascii=False),{"llm_calls":1}
    monkeypatch.setattr(planner_agent,"chat_json",compose)
    final=graph_module.build_graph().invoke(_initial_state(req),config={"recursion_limit":80})
    response=_finalize(final,req,"experience-graph","experience-fingerprint",10)
    assert response.success, response.message
    assert response.data.budget.total==213
    assert response.data.days[0].total_minutes<=experience.MAX_COMFORT_MINUTES
    assert all(m.restaurant_id for m in response.data.days[0].meals)
    assert summary()["sample_tool_calls"]==3
    assert summary()["first_success_rate"]==1


def test_clock_tampering_cannot_pass_independent_experience_audit(monkeypatch):
    monkeypatch.setattr(experience.amap,"travel_route",lambda *a,**kw:{"duration_minutes":10,"status":"known"})
    plan,state=setup_trip()
    enriched,errors,_=experience.enrich_experience(plan,state)
    assert not errors
    enriched["days"][0]["schedule"][0]["duration_minutes"]=1
    reasons=experience.validate_experience(enriched,state)
    assert any("对账" in r or "时间表" in r for r in reasons)


def test_route_retry_is_visible_even_when_eventually_successful(monkeypatch,settings):
    settings.tool_retry_base_delay=0
    calls=[]
    def route(*a,**kw):
        calls.append(1)
        if len(calls)==1:
            raise experience.amap.AmapUnreachable("timeout",retryable=True)
        return {"duration_minutes":10,"status":"known"}
    monkeypatch.setattr(experience.amap,"travel_route",route)
    plan,state=setup_trip()
    experience.enrich_experience(plan,state)
    logs=state["_experience_tool_calls"]
    assert logs[0]["attempt_count"]==2
    assert logs[0]["attempts"][0]["outcome"]["status"]=="error"
    assert logs[0]["outcome"]["status"]=="ok"


def test_route_physical_attempt_cap_survives_recomposition(monkeypatch):
    calls=[]
    monkeypatch.setattr(experience,"MAX_ROUTE_QUERIES",1)
    def route(*a,**kw):
        calls.append(1)
        return {"duration_minutes":10,"status":"known"}
    monkeypatch.setattr(experience.amap,"travel_route",route)
    plan,state=setup_trip()
    experience.enrich_experience(plan,state)
    state["route_cache"]={}
    experience.enrich_experience(plan,state)
    assert len(calls)==1 and state["route_query_count"]==1


def test_price_evidence_sources_and_free_hotel_survive_budget():
    plan,state=setup_trip()
    day=plan["days"][0]
    day["attractions"][0].update(ticket_price=70,ticket_price_status="known",ticket_price_source="imported")
    day["meals"]=[{"type":"lunch","name":"餐厅","estimated_cost":90,"cost_status":"estimated","cost_source":"imported"}]
    hotel=Hotel(name="已核验免房费",estimated_cost=0,cost_status="known",cost_source="imported")
    patched=patch_budget({**state,"plan":plan,"hotel":hotel,"nights":1})["plan"]
    items=patched["budget"]["items"]
    assert next(i for i in items if i["category"]=="hotel")["amount"]==0
    assert next(i for i in items if i["category"]=="attraction")["source"]=="imported"
    assert patched["budget"]["known_total"]==70
    assert patched["budget"]["estimated_total"]==130


def test_cache_date_shift_rechecks_imported_ticket_hotel_prices(monkeypatch):
    from app.services import catalog, planning
    from app.models.schemas import WeatherInfo
    plan,state=setup_trip()
    req=state["request"].model_copy(update={"start_date":"2026-11-10","nights":1,"budget_limit":1000})
    old=plan["days"][0]
    old["attractions"][0].update(ticket_price=80,ticket_price_status="known",ticket_price_source="imported")
    old["meals"]=[experience.meal_from_restaurant(r,k) for r,k in zip(state["restaurants"],("breakfast","lunch","dinner"))]
    # Keep current meal evidence valid so this case isolates dated ticket/room
    # expiry; stale meal evidence is covered by test_cached_food_freshness.
    catalog.upsert_places(req.city, "restaurant", state["restaurants"])
    plan.update(hotel=Hotel(name="旧报价酒店",poi_id="H123",estimated_cost=200,cost_status="known",cost_source="imported").model_dump(),
                hotel_nights=1,sources={"weather":"amap"})
    monkeypatch.setattr(planning.amap,"geocode",lambda *a,**kw:(Location(longitude=120,latitude=30),"330100"))
    monkeypatch.setattr(planning.amap,"get_weather",lambda *a,**kw:[WeatherInfo(date=req.start_date,day_weather="晴")])
    response=planning._cached_response({"plan":plan,"hit_count":1,"created_at":"2026-10-20"},req,"cache-price-date","cache-price-fingerprint",1)
    assert response.data.days[0].attractions[0].ticket_price is None
    assert response.data.hotel.cost_status=="unknown"
    assert response.data.budget.limit_status=="unverified"
    assert len(response.data.budget.unknown_items)==2
    # The source snapshot remains intact.
    assert plan["hotel"]["estimated_cost"]==200


@pytest.mark.parametrize("legacy_schedule",[False,True],ids=["explicit-meal-type","legacy-occurrence-order"])
def test_same_restaurant_for_three_meals_cannot_hide_late_lunch(monkeypatch,legacy_schedule):
    monkeypatch.setattr(experience.amap,"travel_route",lambda *a,**kw:{"duration_minutes":5,"status":"known"})
    plan,state=setup_trip()
    state["restaurants"]=state["restaurants"][:1]
    plan["days"][0]["attractions"][0]["visit_duration"]=310
    enriched,errors,_=experience.enrich_experience(plan,state)
    day=enriched["days"][0]
    meals=[s for s in day["schedule"] if s["kind"]=="meal"]
    # The duration-aware scheduler now moves this indivisible long visit after
    # lunch. Independently corrupt the saved clock to test the audit itself.
    assert day["total_minutes"]<=experience.MAX_COMFORT_MINUTES
    assert [m["meal_type"] for m in meals]==["breakfast","lunch","dinner"]
    assert meals[1]["start_time"]<="14:00" and not errors
    _delay_lunch(day)
    assert len({m["restaurant_id"] for m in day["meals"]})==1
    if legacy_schedule:
        for entry in meals:
            entry.pop("meal_type")
    errors=experience.validate_experience(enriched,state)
    assert any("lunch" in error and "14:45" in error for error in errors)


def test_relabelling_same_restaurant_lunch_as_breakfast_does_not_bypass_late_check(monkeypatch):
    monkeypatch.setattr(experience.amap,"travel_route",lambda *a,**kw:{"duration_minutes":5,"status":"known"})
    plan,state=setup_trip()
    state["restaurants"]=state["restaurants"][:1]
    plan["days"][0]["attractions"][0]["visit_duration"]=310
    enriched,_,_=experience.enrich_experience(plan,state)
    meals=[s for s in enriched["days"][0]["schedule"] if s["kind"]=="meal"]
    meals[1]["meal_type"]="breakfast"
    _delay_lunch(enriched["days"][0])
    errors=experience.validate_experience(enriched,state)
    assert any("餐次标识" in error for error in errors)
    assert any("lunch" in error and "14:45" in error for error in errors)


def _delay_lunch(day):
    """Keep the clock contiguous while corrupting lunch to a clearly late time."""
    schedule=day["schedule"]
    lunch=next(index for index, entry in enumerate(schedule) if entry.get("meal_type")=="lunch") if any(
        entry.get("meal_type")=="lunch" for entry in schedule) else [index for index, entry in enumerate(schedule) if entry["kind"]=="meal"][1]
    def minutes(value):
        hour,minute=value.split(":")
        return int(hour)*60+int(minute)
    delta=14*60+45-minutes(schedule[lunch]["start_time"])
    schedule[lunch-1]["duration_minutes"]+=delta
    schedule[lunch-1]["end_time"]="14:45"
    for entry in schedule[lunch:]:
        for key in ("start_time","end_time"):
            value=minutes(entry[key])+delta
            entry[key]=f"{value//60:02d}:{value%60:02d}"
    day["total_minutes"]+=delta
    day["rest_minutes"]+=delta


def test_legacy_same_restaurant_three_on_time_meals_remain_valid(monkeypatch):
    monkeypatch.setattr(experience.amap,"travel_route",lambda *a,**kw:{"duration_minutes":5,"status":"known"})
    plan,state=setup_trip()
    state["restaurants"]=state["restaurants"][:1]
    enriched,errors,_=experience.enrich_experience(plan,state)
    assert errors==[]
    for entry in enriched["days"][0]["schedule"]:
        entry.pop("meal_type",None)
    assert experience.validate_experience(enriched,state)==[]
