"""Domain boundary: local AMap or an administrator-configured MCP transport."""
import math

from pydantic import TypeAdapter, ValidationError

from ..config import get_settings
from ..models.schemas import Attraction, Location, Restaurant, WeatherInfo
from . import amap

AmapError = amap.AmapError
AmapUnreachable = amap.AmapUnreachable


def _call(name, args):
    from .mcp_adapter import call
    return call(name, args)


def _validate(annotation, value):
    try:
        return TypeAdapter(annotation).validate_python(value)
    except ValidationError as exc:
        raise AmapUnreachable("MCP 领域数据结构不合法", retryable=False, code="MCP_INVALID_RESULT") from exc


def geocode(address, city=None):
    if get_settings().map_backend == "local":
        return amap.geocode(address) if city is None else amap.geocode(address, city)
    data = _call("geocode", {"address": address, "city": city})
    return _validate(tuple[Location | None, str | None], data)


def search_poi(keywords, city, offset=20, page=1):
    if get_settings().map_backend == "local":
        return amap.search_poi(keywords, city, offset=offset, page=page)
    return _validate(list[Attraction], _call("search_poi", {
        "keywords": keywords, "city": city, "offset": offset, "page": page}))


def get_weather(adcode):
    if get_settings().map_backend == "local":
        return amap.get_weather(adcode)
    return _validate(list[WeatherInfo], _call("get_weather", {"adcode": adcode}))


def measure_distance(origin, destination, mode=1):
    if get_settings().map_backend == "local":
        return amap.measure_distance(origin, destination) if mode == 1 else amap.measure_distance(origin, destination, mode=mode)
    value = _call("measure_distance", {"origin": origin.model_dump(), "destination": destination.model_dump(), "mode": mode})
    result = _validate(float | None, value)
    if result is not None and (result < 0 or not math.isfinite(result)):
        raise AmapUnreachable("MCP 距离必须为有限非负数", retryable=False, code="MCP_INVALID_RESULT")
    return result


def search_restaurants(keywords, city, limit=12):
    if get_settings().map_backend == "local":
        return amap.search_restaurants(keywords, city, limit)
    return _validate(list[Restaurant], _call("search_restaurants", {"keywords": keywords, "city": city, "limit": limit}))


def travel_route(origin, destination, mode="driving", city=None):
    if get_settings().map_backend == "local":
        return amap.travel_route(origin, destination, mode, city)
    result = _call("travel_route", {"origin": origin.model_dump(), "destination": destination.model_dump(),
                                    "mode": mode, "city": city})
    if not isinstance(result, dict) or result.get("status") not in {"known", "estimated", "unknown"} or result.get("source") not in {"amap", "estimate", "unknown"}:
        raise AmapUnreachable("MCP 路线结构不合法", retryable=False, code="MCP_INVALID_RESULT")
    for key in ("duration_minutes", "distance_meters"):
        value = result.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
            raise AmapUnreachable("MCP 路线数值不合法", retryable=False, code="MCP_INVALID_RESULT")
        if result["status"] == "known" and value is None:
            raise AmapUnreachable("MCP 已知路线缺失耗时/距离", retryable=False, code="MCP_INVALID_RESULT")
    return result
