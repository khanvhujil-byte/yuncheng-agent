"""测试数据工厂：把"造一个合法对象"的样板集中到一处。

为什么单独一个模块而不是塞进 conftest：
    pytest 的导入模式在不同配置下会变，测试文件之间互相 import conftest 很容易踩坑。
    独立成普通模块后，所有测试统一 `from tests.factories import ...`。
"""

from app.models.schemas import Attraction, Hotel, Location, TripRequest, WeatherInfo


def make_request(**overrides) -> TripRequest:
    """一个合法的默认请求，测试里只覆盖关心的字段。"""
    payload = {
        "city": "杭州",
        "start_date": "2026-10-10",
        "travel_days": 3,
        "transportation": "公共交通",
        "accommodation": "经济型酒店",
        "preferences": ["自然风光", "博物馆"],
        "must_visit": ["西湖"],
    }
    payload.update(overrides)
    return TripRequest(**payload)


def make_attraction(
    name: str,
    *,
    address: str = "测试路 1 号",
    lng: float = 120.12,
    lat: float = 30.22,
    typecode: str | None = "110202",
    required: bool = False,
    poi_id: str | None = None,
    rating: float | None = 4.5,
    visit_duration: int = 120,
    ticket_price: int = 0,
    image_url: str | None = None,
    **kwargs,
) -> Attraction:
    return Attraction(
        name=name,
        address=address,
        location=Location(longitude=lng, latitude=lat),
        typecode=typecode,
        required=required,
        poi_id=poi_id if poi_id is not None else f"POI-{name}",
        rating=rating,
        visit_duration=visit_duration,
        ticket_price=ticket_price,
        image_url=image_url,
        **kwargs,
    )


def make_weather(date: str, *, beyond: bool = False) -> WeatherInfo:
    if beyond:
        return WeatherInfo(date=date, is_forecast=False, beyond_window=True)
    return WeatherInfo(
        date=date,
        day_weather="晴",
        night_weather="多云",
        day_temp=26,
        night_temp=18,
        wind_direction="东",
        wind_power="1-3级",
    )


def make_day(index: int, date: str, spots: list[Attraction], meals: int = 3) -> dict:
    return {
        "date": date,
        "day_index": index,
        "description": "测试日",
        "transportation": "公共交通",
        "accommodation": "经济型酒店",
        "attractions": [spot.model_dump() for spot in spots],
        "meals": [
            {"type": kind, "name": f"{kind}-餐厅", "estimated_cost": 50}
            for kind in ("breakfast", "lunch", "dinner")[:meals]
        ],
        "day_total_cost": 0,
    }


def make_plan(days: list[dict], **overrides) -> dict:
    plan = {
        "city": "杭州",
        "start_date": "2026-10-10",
        "end_date": "2026-10-12",
        "days": days,
        "overall_suggestions": "",
    }
    plan.update(overrides)
    return plan


def make_hotel(price: int = 260, nights: int = 2) -> Hotel:
    return Hotel(
        name="测试酒店",
        address="测试路 2 号",
        location=Location(longitude=120.15, latitude=30.25),
        estimated_cost=price,
        nights=nights,
    )
