"""Real tool and application pipelines with only upstream/model boundaries stubbed."""
import json
from datetime import datetime, timedelta, timezone

from app.agents import attraction_agent, city, graph, hotel_agent, planner_agent
from app.agents.react_loop import ReactResult
from app.models.schemas import Attraction, Location, Restaurant, TripPlan, WeatherInfo
from app.services import catalog, map_gateway, planning, price_catalog, tools
from tests.factories import make_request
from tests.joint_fixtures import joint_payload


def observation(kind, identifier, amount, *, service_to="2026-10-21"):
    now = datetime.now(timezone.utc)
    return {"city": "杭州", "kind": kind, "poi_id": identifier, "amount": amount,
            "unit": "night" if kind == "hotel" else "admission", "status": "known",
            "source_url": "https://official.example/verified-price", "observed_at": now.isoformat(),
            "expires_at": (now + timedelta(days=1)).isoformat(),
            "service_from": "2026-10-20", "service_to": service_to}


def poi(name, identifier, typecode="110200"):
    return Attraction(name=name, poi_id=identifier, typecode=typecode,
                      location=Location(longitude=120.1, latitude=30.2),
                      category="博物馆" if typecode.startswith("11") else "酒店", visit_duration=90)


def context():
    return tools.ToolContext(city="杭州", adcode="330100", start_date="2026-10-20", travel_days=2, nights=1)


def test_attraction_search_applies_source_quote_before_model_context(monkeypatch):
    price_catalog.import_prices([observation("attraction", "AT1", 80)])
    monkeypatch.setattr(map_gateway, "search_poi", lambda *a, **kw: [poi("真实景点", "AT1")])
    ctx = context()
    assert tools.execute("attractions", "search_attractions", {"keyword": "博物馆"}, ctx).status == "ok"
    candidate = ctx.attractions["AT1"]
    assert candidate.ticket_price == 80 and candidate.ticket_price_status == "known"
    assert candidate.ticket_price_source == "imported"
    assert candidate.ticket_price_evidence_url.startswith("https://")


def test_hotel_search_and_choose_keep_verified_price_despite_model_override(monkeypatch):
    price_catalog.import_prices([observation("hotel", "HT1", 350)])
    monkeypatch.setattr(map_gateway, "search_poi", lambda *a, **kw: [poi("真实酒店", "HT1", "100100")])
    ctx = context()
    assert tools.execute("hotels", "search_hotels", {"keyword": "酒店"}, ctx).status == "ok"
    assert ctx.hotels["HT1"].estimated_cost == 350
    result = tools.execute("hotels", "choose_hotel", {"hotel": "HT1", "price_per_night": 1}, ctx)
    assert result.status == "ok"
    assert ctx.chosen_hotel.estimated_cost == 350
    assert ctx.chosen_hotel.cost_source == "imported" and ctx.chosen_hotel.cost_status == "known"


def test_unmatched_hotel_quote_does_not_make_model_estimate_known(monkeypatch):
    price_catalog.import_prices([observation("hotel", "OTHER", 350)])
    monkeypatch.setattr(map_gateway, "search_poi", lambda *a, **kw: [poi("真实酒店", "HT1", "100100")])
    ctx = context()
    tools.execute("hotels", "search_hotels", {}, ctx)
    tools.execute("hotels", "choose_hotel", {"hotel": "HT1", "price_per_night": 260}, ctx)
    assert ctx.chosen_hotel.estimated_cost == 260
    assert ctx.chosen_hotel.cost_status == "estimated" and ctx.chosen_hotel.cost_source == "llm_estimated"


def test_actual_planning_graph_persists_selected_verified_places(monkeypatch, settings, caplog):
    req = make_request(start_date="2026-10-20", travel_days=2, nights=1, must_visit=[], preferences=["博物馆"])
    spots = [poi("真实博物馆甲", "AT1"), poi("真实博物馆乙", "AT2")]
    hotel_poi = poi("真实酒店", "HT1", "100100")
    restaurants = [Restaurant(name=f"真实餐馆{i}", poi_id=f"RT{i}", location=Location(longitude=120.1, latitude=30.2),
                              avg_cost=40, avg_cost_status="estimated", cost_source="amap") for i in range(6)]
    price_catalog.import_prices([observation("attraction", "AT1", 50), observation("attraction", "AT2", 60),
                                 observation("hotel", "HT1", 350)])
    monkeypatch.setattr(settings, "weather_use_agent", False)
    monkeypatch.setattr(city, "geocode", lambda *a, **kw: (Location(longitude=120.1, latitude=30.2), "330100"))
    monkeypatch.setattr(city.java_bridge, "fetch_hot", lambda: [])
    monkeypatch.setattr(map_gateway, "search_poi", lambda keyword, *a, **kw: [hotel_poi] if "酒店" in keyword else spots)
    monkeypatch.setattr(map_gateway, "get_weather", lambda *a: [WeatherInfo(date=f"2026-10-{d}", day_weather="晴", day_temp=22) for d in (20, 21)])
    monkeypatch.setattr(map_gateway, "search_restaurants", lambda *a, **kw: restaurants)
    monkeypatch.setattr(map_gateway, "travel_route", lambda *a, **kw: {"duration_minutes": 10, "distance_meters": 900,
                                                                     "status": "known", "source": "amap", "mode": "transit"})

    def agent(role, system, prompt, ctx, **kwargs):
        if role == "attractions":
            tools.execute(role, "search_attractions", {"keyword": "博物馆"}, ctx)
        elif role == "hotels":
            tools.execute(role, "search_hotels", {"keyword": "酒店"}, ctx)
            tools.execute(role, "rank_hotels", {}, ctx)
            tools.execute(role, "choose_hotel", {"hotel": "HT1", "price_per_night": 1}, ctx)
        else:
            raise AssertionError(role)
        return ReactResult(steps=1, usage={"llm_calls": 1})

    monkeypatch.setattr(attraction_agent, "run_agent", agent)
    monkeypatch.setattr(hotel_agent, "run_agent", agent)

    def compose(*args, **kwargs):
        days = [{"date": f"2026-10-{20+i}", "day_index": i,
                 "attractions": [spot.model_dump()],
                 "meals": [{"type": "lunch", "name": "模型幻觉餐厅"}]} for i, spot in enumerate(spots)]
        return json.dumps(joint_payload(days, restaurants, "HT1"), ensure_ascii=False), {"llm_calls": 1}

    monkeypatch.setattr(planner_agent, "chat_json", compose)
    result = planning.plan(req, graph=graph.build_graph(), run_id="catalog-real-graph")
    assert result.success, result.message
    assert isinstance(result.data, TripPlan)
    assert "地点目录保存失败" not in caplog.text
    assert {p["poi_id"] for p in catalog.list_places("杭州", "attraction")} == {"AT1", "AT2"}
    assert {p["poi_id"] for p in catalog.list_places("杭州", "hotel")} == {"HT1"}
    assert {p["poi_id"] for p in catalog.list_places("杭州", "restaurant")} == {f"RT{i}" for i in range(6)}
    assert catalog.list_places("杭州", "hotel")[0]["estimated_cost"] == 350
    assert all("幻觉" not in p["name"] for p in catalog.list_places("杭州", "restaurant"))
    assert result.data.budget.total_attractions == 110
    assert result.data.budget.total_hotels == 350


def test_catalog_accepts_typed_plan_and_candidate_dicts():
    candidate = poi("真实景点", "AT1")
    plan = TripPlan(city="杭州", start_date="2026-10-20", end_date="2026-10-20",
                    days=[{"date": "2026-10-20", "day_index": 0, "attractions": [candidate]}])
    assert catalog.persist_plan(plan, "杭州", attractions=[candidate.model_dump()])["attractions"] == 1
    assert catalog.list_places("杭州", "attraction")[0]["poi_id"] == "AT1"


def test_cached_plan_picks_up_new_price_import_without_regenerating(monkeypatch):
    from app.models.schemas import Hotel
    from app.services import amap
    req = make_request(start_date="2026-10-20", travel_days=1, nights=1, must_visit=[])
    spot = poi("真实景点", "AT1")
    hotel = Hotel(name="真实酒店", poi_id="HT1", estimated_cost=200,
                  location=Location(longitude=120.1, latitude=30.2))
    restaurant = Restaurant(name="真实餐厅", poi_id="RT1", avg_cost=40,
                            avg_cost_status="estimated", cost_source="amap",
                            location=Location(longitude=120.1, latitude=30.2))
    from app.services.itinerary_experience import meal_from_restaurant
    original = TripPlan(city="杭州", start_date=req.start_date, end_date=req.start_date,
                        hotel=hotel, hotel_nights=1,
                        days=[{"date": req.start_date, "day_index": 0, "attractions": [spot],
                               "meals": [meal_from_restaurant(restaurant, "lunch")]}]).model_dump()
    food_quote = {**observation("restaurant", "RT1", 75), "unit": "person_meal", "status": "estimated"}
    price_catalog.import_prices([observation("attraction", "AT1", 80),
                                 observation("hotel", "HT1", 350), food_quote])
    monkeypatch.setattr(amap, "geocode", lambda *a: (None, None))
    refreshed = planning._reuse_with_new_dates(original, req)
    assert refreshed["budget"]["total_attractions"] == 80
    assert refreshed["budget"]["total_hotels"] == 350
    assert refreshed["budget"]["total_meals"] == 75
    assert refreshed["days"][0]["meals"][0]["evidence_url"] == food_quote["source_url"]
    assert original["hotel"]["estimated_cost"] == 200
    assert original["days"][0]["attractions"][0]["ticket_price"] is None
