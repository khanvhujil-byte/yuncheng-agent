"""Bounded, verified food collection with optional public AMap ranking evidence."""
import logging
from time import monotonic

from ..config import get_settings
from . import catalog, map_gateway, price_catalog
from .tool_result import ToolResult
from .tool_retry import RetryPolicy, run

logger = logging.getLogger(__name__)


def search_restaurants(city, keywords="当地特色美食", limit=12):
    return map_gateway.search_restaurants(keywords, city, limit)


def search_restaurants_nearby(city, center, keywords, limit=20):
    return map_gateway.search_restaurants_nearby(keywords, city, center, radius=4000, limit=limit)


def _with_ranking(place, ranking):
    return place.model_copy(update={"ranking_name": ranking["ranking_name"],
                                   "ranking_rank": ranking["ranking_rank"],
                                   "ranking_url": ranking["source_url"]})


def collect_food(state):
    """Local-first collection; at most four specific and one broad live query."""
    if not state.get("experience_enabled"):
        return {}
    request = state["request"]
    from .requirements import policy, restaurant_allowed
    from .candidate_search import decision, local_candidates, minimum_candidates, save_fresh, with_stats
    needs = policy(request).food
    settings = get_settings()
    days = state["travel_days"]
    start_date = getattr(request, "start_date", None)
    warnings, errors, calls = [], [], []
    by_id, fresh_by_id = {}, {}
    try:
        ranking_rows = catalog.rankings(request.city)
    except Exception:
        logger.exception("餐饮榜单读取失败")
        ranking_rows = []
        warnings.append("餐饮榜单资料暂不可用，未将普通餐厅标记为榜单。")
    ranked = {entry["poi_id"]: entry for entry in ranking_rows}
    keywords = list(dict.fromkeys(needs.keywords))[:4]
    if len(dict.fromkeys(needs.keywords)) > 4:
        warnings.append("本次仅检索前4个指定餐饮关键词，其余要求已保留，尚未逐项检索核实。")
    coverage = set()
    local_reasons, search_reports = [], []
    for keyword in keywords or ["当地特色美食"]:
        places, info = local_candidates(request.city, "restaurant", keyword, needs,
                                        start_date=start_date, days=days)
        search_reports.append(info.get("local_search", {}))
        local_reasons.extend(info["retrieval_reasons"])
        if places:
            coverage.add(keyword)
        for place in places:
            by_id[place.poi_id] = place.model_copy(update={"ranking_name": None,
                                                          "ranking_rank": None, "ranking_url": None})
    local_count = len(by_id)
    minimum = minimum_candidates("restaurant", days)

    def sufficient():
        return len(by_id) >= minimum and all(word in coverage for word in keywords)

    retrieval = decision({"local_count": local_count, "upstream_count": 0, "upstream_calls": 0,
                          "cache_hit": 0, "retrieval_decision": "upstream_fill",
                          "retrieval_reasons": list(dict.fromkeys(local_reasons)),
                          "query_coverage": sorted(coverage), "local_search": search_reports}, sufficient())
    if retrieval["cache_hit"]:
        outcome = with_stats(ToolResult.success("本地餐饮候选满足当前关键词、数量和价格限制。",
                                               returned_count=local_count), retrieval)
        calls.append({"role": "food", "tool": "search_restaurants", "args": {"city": request.city},
                      "result": outcome.message, "outcome": outcome.to_dict(), "attempts": [],
                      "attempt_count": 0, "stop_reason": "local_hit", "duration_ms": 0})
    else:
        named = [(word, None) for word in keywords if word not in coverage]
        named += [(word, None) for word in keywords if word in coverage]
        ranked_queries = [(entry["name"], entry["poi_id"]) for entry in ranking_rows
                          if entry["poi_id"] not in by_id]
        queries = list({word: (word, identifier) for word, identifier in [*named, *ranked_queries]}.values())[:4]
        queries.append(("当地特色美食", None))
        for keyword, expected_id in queries:
            if sufficient():
                break
            found = []

            def attempt():
                nonlocal found
                try:
                    found = search_restaurants(request.city, keyword, limit=12 if expected_id is None else 5)
                    return with_stats(ToolResult.success("餐饮查询完成", returned_count=len(found)),
                                      {**retrieval, "upstream_calls": 1, "upstream_count": len(found)})
                except map_gateway.AmapError as exc:
                    return with_stats(ToolResult.failure(exc.detail, exc.code),
                                      {**retrieval, "upstream_calls": 1, "upstream_count": 0})
                except map_gateway.AmapUnreachable as exc:
                    return with_stats(ToolResult.failure(str(exc), exc.code, retryable=exc.retryable,
                                      retry_after_seconds=exc.retry_after_seconds),
                                      {**retrieval, "upstream_calls": 1, "upstream_count": 0})

            before = monotonic()
            report = run(attempt, safe=True, policy=RetryPolicy(max_attempts=2, budget_seconds=settings.tool_retry_budget_seconds))
            retrieval["upstream_calls"] += len(report.attempts)
            calls.append({"role": "food", "tool": "search_restaurants", "args": {"keywords": keyword, "city": request.city},
                          "result": report.result.message, "outcome": report.result.to_dict(), "attempts": report.attempts,
                          "attempt_count": len(report.attempts),
                          "duration_ms": round((monotonic() - before) * 1000), "stop_reason": report.stop_reason})
            if report.result.status == "error":
                calls[-1]["outcome"]["stats"]["retrieval_decision"] = (
                    "local_partial_upstream_error" if local_count else "upstream_error")
                errors.append(f"餐饮查询失败 [{report.result.error_code}]：{report.result.message}")
                if report.result.error_code in {"AMAP_KEY_INVALID", "AMAP_QUOTA_EXCEEDED"}:
                    break
                continue
            retrieval["upstream_count"] += len(found)
            priced = price_catalog.apply_prices(request.city, "restaurant", found,
                                                service_date=start_date, travel_days=days)
            usable = [p for p in priced if restaurant_allowed(p, needs)]
            for word in keywords:
                if any(catalog.matches_query(p.model_dump(), "restaurant", word) for p in usable):
                    coverage.add(word)
            for place in usable:
                clean = place.model_copy(update={"ranking_name": None, "ranking_rank": None, "ranking_url": None})
                fresh_by_id[place.poi_id] = clean
                by_id[place.poi_id] = clean
        # Reused catalog observations are never written back as freshly fetched.
        save_fresh(request.city, "restaurant", list(fresh_by_id.values()), retrieval)
        if "catalog_write_failed" in retrieval["retrieval_reasons"]:
            warnings.append("餐饮候选本次未能入库，当前规划可继续。")
        retrieval["query_coverage"] = sorted(coverage)
        if errors:
            retrieval["retrieval_decision"] = "local_partial_upstream_error" if local_count else "upstream_error"
        elif not sufficient():
            retrieval["retrieval_decision"] = "partial"
            retrieval["retrieval_reasons"].append("insufficient_after_upstream_fill")
        if calls:
            calls[-1]["retrieval"] = retrieval
            calls[-1]["outcome"]["stats"].update(retrieval)
    for identifier in list(by_id):
        if identifier in ranked:
            by_id[identifier] = _with_ranking(by_id[identifier], ranked[identifier])
    # A city-wide rating cut must not discard most neighbourhood candidates
    # before the actual itinerary can evaluate geography and meal suitability.
    places = sorted(by_id.values(), key=lambda p: (p.ranking_rank is None, p.ranking_rank or 9999,
                                                   -(p.rating or 0)))[:min(100, max(24, days * 12))]
    if keywords and not all(word in coverage for word in keywords):
        warnings.append("部分餐饮关键词尚未找到可用候选，未宣称已全部满足。")
    if not any(p.ranking_name for p in places):
        warnings.append("当前无已核实的高德扫街榜餐厅，展示普通高德餐饮 POI 推荐，未冒充榜单。")
    if not places:
        warnings.append("未获得可核实的餐厅，餐饮推荐留空，预算仍需预留餐费。")
    source = ("catalog" if retrieval["cache_hit"] else
              ("catalog+amap" if fresh_by_id else "catalog_partial") if local_count else "amap")
    return {"restaurants": places, "warnings": warnings, "errors": errors,
            "sources": {"food": source if places else "degraded"}, "tool_calls": calls}


def ensure_food_coverage(plan, state):
    """Recheck coverage against actual stops; bounded fill runs only once/query.

    Local retrieval happens first. Up to three scoped searches share six
    seconds, use the same typed retries/rate gate as other tools, and persist
    query identities across graph repair rounds. Local edits bypass this step.
    """
    settings = get_settings()
    if not settings.food_coverage_enabled:
        return [], []
    from ..models.schemas import Restaurant
    from .candidate_search import local_candidates, priced_allowed, save_fresh
    from .food_recommendation import coverage_gaps, location
    from .requirements import policy
    request = state['request']
    needs = policy(request).food
    current = {r.poi_id: r for raw in state.get('restaurants', [])
               if (r := Restaurant.model_validate(raw))}
    local, _ = local_candidates(request.city, 'restaurant', '餐厅', needs,
                                start_date=request.start_date, days=state['travel_days'], limit=100)
    for restaurant in local:
        current.setdefault(restaurant.poi_id, restaurant)
    calls, warnings = [], []
    attempted = list(state.get('food_coverage_queries') or [])
    started_coverage = monotonic()
    elapsed = float(state.get('food_coverage_elapsed_seconds') or 0)
    end = started_coverage + max(0, settings.food_coverage_budget_seconds - elapsed)
    hotel = state.get('hotel') or plan.get('hotel')
    while (not state.get('food_coverage_blocked_code')
           and len(attempted) < settings.food_coverage_query_limit and monotonic() < end):
        gaps = coverage_gaps(plan, hotel, list(current.values()), needs)
        query = None
        for kind, anchor, _ in gaps:
            center = location(anchor)
            if center is None:
                continue
            keyword = '早餐' if kind == 'breakfast' else '餐厅'
            identity = f'{center.longitude:.3f},{center.latitude:.3f}|{keyword}'
            if identity not in attempted:
                query = keyword
                break
        if query is None:
            break
        attempted.append(identity)
        found = []

        def attempt():
            nonlocal found
            try:
                found = search_restaurants_nearby(request.city, center, query, limit=20)
                return ToolResult.success('按实际游览片区补充餐饮候选', returned_count=len(found))
            except (map_gateway.AmapError, map_gateway.AmapUnreachable) as exc:
                return ToolResult.failure('片区餐饮补充查询失败', exc.code,
                                          retryable=bool(getattr(exc, 'retryable', False)),
                                          retry_after_seconds=getattr(exc, 'retry_after_seconds', None))

        started = monotonic()
        report = run(attempt, safe=True, policy=RetryPolicy(max_attempts=2,
                     budget_seconds=min(settings.tool_retry_budget_seconds, max(.001, end-monotonic()))))
        calls.append({'role':'food', 'tool':'search_restaurants_nearby',
                      'args':{'city':request.city,'keywords':query,'center':center.model_dump(),'radius':4000},
                      'result':report.result.message, 'outcome':report.result.to_dict(), 'attempts':report.attempts,
                      'attempt_count':len(report.attempts), 'stop_reason':report.stop_reason,
                      'duration_ms':round((monotonic()-started)*1000)})
        if report.result.status == 'ok':
            valid = priced_allowed(request.city, 'restaurant', found, needs,
                                   start_date=request.start_date, days=state['travel_days'])
            save_fresh(request.city, 'restaurant', valid, {'retrieval_reasons':[]})
            if valid:
                state['sources'] = {**state.get('sources', {}),
                                    'food':'catalog+amap' if local or state.get('sources', {}).get('food') in {'catalog','catalog_partial','catalog+amap'} else 'amap'}
            current.update({r.poi_id:r for r in valid})
        elif report.result.error_code in {'AMAP_KEY_INVALID', 'AMAP_QUOTA_EXCEEDED'}:
            state['food_coverage_blocked_code'] = report.result.error_code
            warnings.append(f'餐饮补充查询停止 [{report.result.error_code}]，保留已核验候选。')
            break
        elif report.result.error_code in {'MCP_TOOL_NOT_FOUND', 'MCP_CONFIG_ERROR', 'MCP_INVALID_RESULT', 'INVALID_ARGUMENT'}:
            state['food_coverage_blocked_code'] = report.result.error_code
            warnings.append(f'地图服务不支持当前周边检索契约 [{report.result.error_code}]，已停止补搜，保留候选不足提示。')
            break
    # Ranking evidence is independent of a POI or a query match.
    try:
        ranked = {row['poi_id']:row for row in catalog.rankings(request.city)}
    except Exception:
        ranked = {}
    for identifier, restaurant in list(current.items()):
        clean = restaurant.model_copy(update={'ranking_name':None, 'ranking_rank':None, 'ranking_url':None})
        current[identifier] = _with_ranking(clean, ranked[identifier]) if identifier in ranked else clean
    state['restaurants'] = list(current.values())
    state['food_coverage_queries'] = attempted
    state['food_coverage_elapsed_seconds'] = elapsed + monotonic() - started_coverage
    if coverage_gaps(plan, hotel, state['restaurants'], needs):
        warnings.append('部分餐次附近的可用餐厅或早餐候选不足；已限次补充检索，营业时间仍需出发前核验。')
    return calls, warnings
