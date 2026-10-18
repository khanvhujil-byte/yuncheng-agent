"""Bounded, verified food collection with optional public AMap ranking evidence."""
import logging
from time import monotonic

from ..config import get_settings
from ..models.schemas import Restaurant
from . import catalog, map_gateway, price_catalog
from .tool_result import ToolResult
from .tool_retry import RetryPolicy, run

logger = logging.getLogger(__name__)


def search_restaurants(city, keywords="当地特色美食", limit=12):
    return map_gateway.search_restaurants(keywords, city, limit)


def _with_ranking(place, ranking):
    return place.model_copy(update={"ranking_name": ranking["ranking_name"],
                                   "ranking_rank": ranking["ranking_rank"],
                                   "ranking_url": ranking["source_url"]})


def collect_food(state):
    """No model call: at most 4 ranked names + 1 broad query, two safe tries each."""
    if not state.get("experience_enabled"):
        return {}
    request = state["request"]
    from .requirements import policy, restaurant_allowed
    needs = policy(request).food
    settings = get_settings()
    warnings, errors, calls = [], [], []
    by_id = {}
    try:
        ranking_rows = catalog.rankings(request.city)
        for raw in catalog.list_places(request.city, "restaurant", limit=20):
            place = Restaurant.model_validate(raw).model_copy(update={"ranking_name": None, "ranking_rank": None, "ranking_url": None})
            by_id[place.poi_id] = place
    except Exception:
        logger.exception("餐饮资料库读取失败")
        ranking_rows = []
        warnings.append("餐饮资料库暂不可用，使用当前高德查询结果。")
    ranked = {entry["poi_id"]: entry for entry in ranking_rows}
    named_queries = [(keyword, None) for keyword in needs.keywords]
    if len(dict.fromkeys(needs.keywords)) > 4:
        warnings.append("本次仅检索前4个指定餐饮关键词，其余要求已保留，尚未逐项检索核实。")
    ranked_queries = [(entry["name"], entry["poi_id"]) for entry in ranking_rows if entry["poi_id"] not in by_id]
    queries = list({keyword: (keyword, identifier) for keyword, identifier in [*named_queries, *ranked_queries]}.values())[:4]
    queries.append(("当地特色美食", None))
    for keyword, expected_id in queries:
        found = []

        def attempt():
            nonlocal found
            try:
                found = search_restaurants(request.city, keyword, limit=12 if expected_id is None else 5)
                return ToolResult.success("餐饮查询完成", returned_count=len(found))
            except map_gateway.AmapError as exc:
                return ToolResult.failure(exc.detail, exc.code)
            except map_gateway.AmapUnreachable as exc:
                return ToolResult.failure(str(exc), exc.code, retryable=exc.retryable,
                                          retry_after_seconds=exc.retry_after_seconds)

        before = monotonic()
        report = run(attempt, safe=True, policy=RetryPolicy(max_attempts=2, budget_seconds=settings.tool_retry_budget_seconds))
        calls.append({"role": "food", "tool": "search_restaurants", "args": {"keywords": keyword, "city": request.city},
                      "result": report.result.message, "outcome": report.result.to_dict(), "attempts": report.attempts,
                      "duration_ms": round((monotonic() - before) * 1000), "stop_reason": report.stop_reason})
        if report.result.status == "error":
            errors.append(f"餐饮查询失败 [{report.result.error_code}]：{report.result.message}")
            if report.result.error_code in {"AMAP_KEY_INVALID", "AMAP_QUOTA_EXCEEDED"}:
                break
            continue
        for place in found:
            # A fuzzy name match must not transfer a different branch's ranking.
            by_id[place.poi_id] = _with_ranking(place, ranked[place.poi_id]) if place.poi_id in ranked else place
    for identifier in list(by_id):
        if identifier in ranked:
            by_id[identifier] = _with_ranking(by_id[identifier], ranked[identifier])
    priced = price_catalog.apply_prices(request.city, "restaurant", list(by_id.values()),
                                        service_date=getattr(request, "start_date", None), travel_days=state["travel_days"])
    priced = [p for p in priced if restaurant_allowed(p, needs)]
    places = sorted(priced, key=lambda p: (p.ranking_rank is None, p.ranking_rank or 9999,
                                                   -(p.rating or 0)))[:min(24, max(6, state["travel_days"] * 4))]
    if not any(p.ranking_name for p in places):
        warnings.append("当前无已核实的高德扫街榜餐厅，展示普通高德餐饮 POI 推荐，未冒充榜单。")
    if not places:
        warnings.append("未获得可核实的餐厅，餐饮推荐留空，预算仍需预留餐费。")
    try:
        catalog.upsert_places(request.city, "restaurant", places)
    except Exception:
        logger.exception("餐饮候选入库失败")
        warnings.append("餐饮候选本次未能入库，当前规划可继续。")
    return {"restaurants": places, "warnings": warnings, "errors": errors,
            "sources": {"food": "amap" if places else "degraded"}, "tool_calls": calls}
