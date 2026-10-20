"""Small shared-quota route comparison supplied to the planning Agent."""
from ..config import get_settings
from .stays import raw, identity
from .tools import haversine_km


def prepare_options(state):
    from .route_batch import prefetch_routes, route_key, cached_route
    req = state['request']
    spots = [s for s in state.get('attractions', []) if s.retrieval_scope == 'surrounding']
    hotels = [h for h in state.get('hotels', []) if h.location]
    if not spots or not hotels or state.get('stay_route_hints'):
        return {}
    center = state.get('city_center')
    if not center:
        return {}
    city_hotel = min(hotels, key=lambda h:haversine_km(center, h.location))
    chosen = []
    for spot in sorted(spots, key=lambda s:not s.required):
        if any(haversine_km(spot.location, p.location) < 15 for p in chosen):
            continue
        chosen.append(spot)
        if len(chosen) == 2:
            break
    pairs, records = [], []
    for spot in chosen:
        near = min(hotels, key=lambda h:haversine_km(spot.location, h.location))
        for h in {identity(h):h for h in (city_hotel, near)}.values():
            for a, b in ((h, spot), (spot, h)):
                pairs.append((raw(a),raw(b)))
                records.append(dict(from_id=identity(a), to_id=identity(b), from_name=a.name, to_name=b.name))
    cfg, cache = get_settings(), state.setdefault('route_cache', {})
    used = int(state.get('route_query_count') or 0)
    batch = prefetch_routes(pairs, mode=req.transportation, city=req.city, cache=cache,
                            query_count=used, max_queries=min(48, used+8),
                            max_workers=cfg.route_max_workers, budget_seconds=min(6, cfg.route_batch_budget_seconds),
                            blocked_code=state.get('route_blocked_code'))
    for record, (a,b) in zip(records,pairs):
        fact = cached_route(cache.get(route_key(a,b,req.transportation,req.city)))
        if fact:
            record.update({k:v for k,v in fact.items() if not k.startswith('_')})
        else:
            record.update(status='unknown', duration_minutes=None)
    return dict(stay_route_hints=records, route_cache=cache, route_query_count=batch.query_count,
                route_blocked_code=batch.blocked_code, tool_calls=batch.calls)
