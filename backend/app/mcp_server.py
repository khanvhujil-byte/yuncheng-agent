"""Read-only map MCP server. Run: python -m app.mcp_server (stdio)."""
from mcp.server.fastmcp import FastMCP

from .models.schemas import Location
from .services import amap

server = FastMCP("trip-map")


def _run(function, *args, **kwargs):
    try:
        result = function(*args, **kwargs)
        from fastapi.encoders import jsonable_encoder
        return {"status": "ok", "data": jsonable_encoder(result)}
    except amap.AmapError as exc:
        return {"status": "error", "error_code": exc.code, "retryable": False}
    except amap.AmapUnreachable as exc:
        return {"status": "error", "error_code": exc.code, "retryable": exc.retryable,
                "retry_after_seconds": exc.retry_after_seconds}


@server.tool()
def geocode(address: str, city: str | None = None) -> dict:
    return _run(amap.geocode, address, city)


@server.tool()
def search_poi(keywords: str, city: str, offset: int = 20, page: int = 1) -> dict:
    if not 1 <= offset <= 20 or not 1 <= page <= 10:
        return {"status": "error", "error_code": "INVALID_REQUEST", "retryable": False}
    return _run(amap.search_poi, keywords, city, offset=offset, page=page)


@server.tool()
def get_weather(adcode: str) -> dict:
    return _run(amap.get_weather, adcode)


@server.tool()
def measure_distance(origin: Location, destination: Location, mode: int = 1) -> dict:
    if mode not in (0, 1, 3):
        return {"status": "error", "error_code": "INVALID_REQUEST", "retryable": False}
    return _run(amap.measure_distance, origin, destination, mode=mode)


@server.tool()
def search_restaurants(keywords: str, city: str, limit: int = 12) -> dict:
    if not 1 <= limit <= 20:
        return {"status": "error", "error_code": "INVALID_REQUEST", "retryable": False}
    return _run(amap.search_restaurants, keywords, city, limit)


@server.tool()
def travel_route(origin: Location, destination: Location, mode: str = "driving", city: str | None = None) -> dict:
    return _run(amap.travel_route, origin, destination, mode, city)


if __name__ == "__main__":
    server.run(transport="stdio")
