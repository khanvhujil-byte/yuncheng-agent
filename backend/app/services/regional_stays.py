"""Bounded local-first regional context. No special cases for named cities."""
from datetime import date, timedelta
from time import monotonic

from ..models.schemas import Hotel
from . import map_gateway
from .tool_result import ToolResult
from .tool_retry import RetryPolicy, run
from .tools import haversine_km


def collect_regional_context(state):
    from ..config import get_settings
    from .candidate_search import local_candidates, save_fresh
    from .requirements import hotel_allowed
    cfg, req = get_settings(), state['request']
    hotels = {h.poi_id or h.name: h for h in (Hotel.model_validate(h) for h in state.get('hotels', []))}
    calls, warnings = [], []
    attempted = list(state.get('regional_hotel_queries') or [])
    weather = list(state.get('regional_weather') or [])
    deadline = monotonic() + cfg.regional_context_budget_seconds
    blocked = False

    def query(tool, args, function):
        nonlocal blocked
        values = []
        def attempt():
            nonlocal values
            try:
                values = function()
                return ToolResult.success('区域事实查询完成', returned_count=len(values))
            except (map_gateway.AmapError, map_gateway.AmapUnreachable) as exc:
                return ToolResult.failure(str(exc), exc.code, retryable=bool(getattr(exc, 'retryable', False)),
                                          retry_after_seconds=getattr(exc, 'retry_after_seconds', None))
        started = monotonic()
        report = run(attempt, safe=True, policy=RetryPolicy(max_attempts=2, budget_seconds=max(.001, min(cfg.tool_retry_budget_seconds, deadline-started))))
        calls.append(dict(role='regional_context', tool=tool, args=args, result=report.result.message,
                          outcome=report.result.to_dict(), attempts=report.attempts, attempt_count=len(report.attempts),
                          stop_reason=report.stop_reason, duration_ms=round((monotonic()-started)*1000)))
        if report.result.status == 'error':
            warnings.append(f"区域资料查询失败 [{report.result.error_code}]：{report.result.message}")
            blocked = not report.result.retryable or report.result.error_code == 'UPSTREAM_RATE_LIMITED'
            return []
        return values

    # Dated locks are already identity-resolved by the deterministic city gate.
    for h in state.get('stay_locks', {}).values():
        h = Hotel.model_validate(h)
        hotels[h.poi_id or h.name] = h
    centers = []
    for spot in sorted(state.get('attractions', []), key=lambda a: (not a.required, a.retrieval_scope != 'surrounding')):
        if not spot.location:
            continue
        center = state.get('city_center')
        if spot.retrieval_scope != 'surrounding' and (not center or haversine_km(center, spot.location) < 25):
            continue
        if any(haversine_km(spot.location, prior.location) < 15 for prior in centers):
            continue
        centers.append(spot)
        if len(centers) >= cfg.regional_context_max_regions:
            break
    for spot in centers:
        region = f"region:{spot.location.longitude:.3f},{spot.location.latitude:.3f}"
        if state.get('nights', 0) and not req.fixed_hotel and region not in attempted and len(attempted) < cfg.regional_context_max_regions:
            attempted.append(region)
            local, _ = local_candidates(region, 'hotel', '酒店', req.requirements.hotel,
                                        start_date=req.start_date, nights=1)
            found = [h for h in local if h.location and haversine_km(h.location, spot.location) <= 20]
            if len(found) < 3 and not blocked and monotonic() < deadline:
                fresh = query('search_hotels_nearby', {'center': spot.location.model_dump(), 'radius':20000},
                              lambda: map_gateway.search_hotels_nearby(spot.location, radius=20000, limit=12))
                save_fresh(region, 'hotel', fresh, {'retrieval_reasons': []})
                found += fresh
            for h in found:
                if h.location and haversine_km(h.location, spot.location) <= 20 and hotel_allowed(h, req.requirements.hotel):
                    hotels[h.poi_id or h.name] = h
            if not found:
                warnings.append(f'「{spot.name}」附近尚无符合条件的真实酒店，不能凭空安排当地过夜。')
        # District weather is useful but is not a mountain-site forecast.
        if not any(w.get('poi_id') == spot.poi_id for w in weather):
            note = '山区/周边活动点无现场预报，市区天气不能代替；出发前核实当地预警与开放状态。'
            district_forecast = {}
            if spot.address and not blocked and monotonic() < deadline:
                def lookup_district():
                    loc, code = map_gateway.geocode(spot.address)
                    if loc and code and haversine_km(loc, spot.location) < 20:
                        return [{'adcode':code}]
                    return []
                codes = query('regional_geocode', {'address':spot.address}, lookup_district)
                if codes and not blocked and monotonic() < deadline:
                    from .weather_cache import get_forecast
                    def lookup_weather():
                        values, _ = get_forecast(codes[0]['adcode'], loader=map_gateway.get_weather)
                        return [v.model_dump() if hasattr(v, 'model_dump') else v for v in values]
                    rows = query('regional_weather', {'adcode':codes[0]['adcode']}, lookup_weather)
                    district_forecast = {w['date']:w for w in rows}
            for i in range(req.travel_days):
                day = (date.fromisoformat(req.start_date)+timedelta(days=i)).isoformat()
                forecast = district_forecast.get(day, {})
                weather.append(dict(poi_id=spot.poi_id, name=spot.name, date=day,
                                    status='district_reference' if forecast else 'unknown', note=note,
                                    district_forecast=forecast))
            warnings.append(f'{spot.name}：{note}')
    # Keep weather facts explicit. Future authorized regional adapters can fill
    # known entries; don't label an administrative forecast as on-site weather.
    sources = dict(state.get('sources') or {})
    if hotels and sources.get('hotel') in ('degraded', 'catalog_partial'):
        sources['hotel'] = 'catalog+amap' if any(c['tool'] == 'search_hotels_nearby' and c['outcome']['status'] == 'ok' for c in calls) else 'catalog'
    return dict(hotels=list(hotels.values()), regional_hotel_queries=attempted, sources=sources,
                regional_weather=weather, tool_calls=calls, warnings=warnings)


def resolve_request_anchors(req):
    """Resolve user strings before any model call; reject ambiguous branches."""
    from .tools import _is_hotel, _hotel_of, is_relevant
    locks, endpoints = {}, {}
    for lock in req.stay_locks:
        rows = map_gateway.search_poi(lock.hotel_name, lock.city or req.city, offset=20,
                                     **({'citylimit':False} if lock.city else {}))
        matches = [p for p in rows if _is_hotel(p) and p.location and is_relevant(lock.hotel_name, p.name)]
        if lock.address:
            matches = [p for p in matches if lock.address in p.address]
        exact = [p for p in matches if p.name == lock.hotel_name]
        matches = list({p.poi_id or (p.name, p.address):p for p in exact or matches}.values())
        if len(matches) != 1:
            raise ValueError(f'{lock.check_in} 锁定酒店未找到或有重名分店，请补充区域和地址。')
        locks[lock.check_in] = _hotel_of(matches[0], 1).model_dump()
    for kind, address in (('arrival', req.arrival_address), ('departure', req.departure_address)):
        if address:
            location, _ = map_gateway.geocode(address, req.city)
            if location is None:
                raise ValueError(f'无法核实{address}的坐标，请补充准确地址。')
            endpoints[kind] = dict(name=address, location=location.model_dump())
    return locks, endpoints
