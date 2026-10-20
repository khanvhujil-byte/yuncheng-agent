"""Independent route clock for dated stays, preserving the planner's POI choices."""
from copy import deepcopy
from time import monotonic

from ..config import get_settings
from .stays import attach_boundaries, audit_stays, identity


def enrich_segmented(plan, state, day_indices=None):
    from .itinerary_experience import (_schedule_day, _ordered, _loc, _weather_problems,
                                       city_characteristics, validate_experience)
    from .ticket_pricing import refresh_plan_tickets
    from .route_batch import prefetch_routes, route_key, cached_route
    from .schedule_repair import estimated_minutes
    from .unified_planning import audit_choices
    from .requirements import audit_requirements
    req, cfg = state['request'], get_settings()
    result = attach_boundaries(deepcopy(plan), req, endpoints=state.get('trip_endpoints'))
    result = refresh_plan_tickets(result, req, day_indices, lookup_state=state)
    from .food import ensure_food_coverage
    coverage_calls, coverage_warnings = ensure_food_coverage(result, state) if day_indices is None and not state.get('preserve_meals') else ([], [])
    cache = state.setdefault('route_cache', {})
    quota = [int(state.get('route_query_count') or 0)]
    calls, warnings = [*coverage_calls, *state.pop('_ticket_search_calls', [])], list(coverage_warnings)
    selected = set(day_indices) if day_indices is not None else None
    deadline = monotonic() + cfg.route_batch_budget_seconds
    prepared = []
    def duration(a, b):
        fact = cached_route(cache.get(route_key(a, b, req.transportation, req.city)))
        return fact['duration_minutes'] if fact else estimated_minutes(a, b, req.transportation)
    for day in result.get('days', []):
        if selected is not None and day['day_index'] not in selected:
            continue
        from .day_window import policy
        window = policy(req, day['date'])
        day['start_minutes'] = window['start_minutes']
        day['meal_types'] = window['meal_types']
        sequence = _ordered(day, day.get('route_start'), req.transportation, duration, req.requirements.pace.max_day_minutes)
        prepared.append((day, sequence))
    def pairs_for(items):
        pairs = []
        for day, sequence in items:
            previous = day.get('route_start')
            end = day.get('route_end')
            for _, item, _ in sequence + ([('return', end, 0)] if end else []):
                if previous and _loc(previous) and _loc(item):
                    pairs.append((previous, item))
                if _loc(item):
                    previous = item
        # Long transfers determine whether the selected lodging layout works.
        # Resolve them before short local legs can exhaust the shared quota.
        return sorted(pairs, key=lambda pair: estimated_minutes(*pair, req.transportation), reverse=True)
    # A second bounded pass can refine meal boundaries using actual route facts.
    # Both passes share deadline, quota, retry state and cache.
    blocked = state.get('route_blocked_code')
    for pass_index in range(2):
        batch = prefetch_routes(pairs_for(prepared), mode=req.transportation, city=req.city, cache=cache,
                                query_count=quota[0], max_queries=48, max_workers=cfg.route_max_workers,
                                budget_seconds=max(0, deadline-monotonic()), blocked_code=blocked)
        quota[0], blocked = batch.query_count, batch.blocked_code
        calls.extend(batch.calls)
        if pass_index == 0:
            prepared = [(day, _ordered(day, day.get('route_start'), req.transportation, duration,
                                      req.requirements.pace.max_day_minutes)) for day, _ in prepared]
    for day, sequence in prepared:
        _schedule_day(day, sequence, None, req, cache, quota, calls)
    # Keep hotel and meal decisions; only unprotected optional visits can be
    # removed, and new route bridges still need measured facts and re-audit.
    from .schedule_repair import can_trim, remove_one_optional, simulate
    repaired = []
    if can_trim(state, day_indices):
        for day, sequence in prepared:
            timing = simulate(sequence, day.get('route_start'), duration, end_hotel=day.get('route_end'),
                              start_minutes=day.get('start_minutes', 510), transfer_minutes=day.get('transfer_minutes', 0))
            changed = False
            while timing['total_minutes'] > req.requirements.pace.max_day_minutes or timing['late_meal_minutes']:
                note = remove_one_optional(day, state, day.get('route_start'))
                if not note:
                    break
                warnings.append(note)
                changed = True
                sequence = _ordered(day, day.get('route_start'), req.transportation, duration,
                                    req.requirements.pace.max_day_minutes)
                timing = simulate(sequence, day.get('route_start'), duration, end_hotel=day.get('route_end'),
                                  start_minutes=day.get('start_minutes', 510), transfer_minutes=day.get('transfer_minutes', 0))
            if changed:
                repaired.append((day, sequence))
        batch = prefetch_routes(pairs_for(repaired), mode=req.transportation, city=req.city, cache=cache,
                                query_count=quota[0], max_queries=48, max_workers=cfg.route_max_workers,
                                budget_seconds=max(0, deadline-monotonic()), blocked_code=blocked)
        quota[0], blocked = batch.query_count, batch.blocked_code
        calls.extend(batch.calls)
        for day, sequence in repaired:
            _schedule_day(day, sequence, None, req, cache, quota, calls)
    for day, sequence in prepared:
        remote = any(s.get('retrieval_scope') == 'surrounding' for s in day.get('attractions', []))
        if remote:
            severe, hints = [], ['周边/山区天气与开放状态尚未现场核实，不能以市区预报保证适宜出行。']
        else:
            severe, hints = _weather_problems(day, state.get('weather', []))
        if day.get('route_boundary_note'):
            hints.append(day['route_boundary_note'])
        if any(l.get('status') != 'known' for l in day.get('route_legs', [])):
            hints.append('部分路线为估算，请核实实际可达性；远途公交未核实不会作为已验证计划交付。')
        if day.get('hotel_change'):
            hints.append('未自动预订或取消酒店；入住、退房及行李处理条件需确认。')
        day['comfort_warnings'] = severe + hints
        warnings.extend(f"{day['date']}：{h}" for h in hints)
    result['city_characteristics'] = city_characteristics(req.city)
    result['weather_info'] = [w.model_dump() if hasattr(w, 'model_dump') else w for w in state.get('weather', [])]
    result['stay_comparison'] = dict(
        strategy='segmented' if len({identity(s['hotel']) for s in result['stays']}) > 1 else 'same_hotel',
        hotel_changes=sum(d.get('hotel_change', False) for d in result['days']),
        travel_minutes=sum(d.get('travel_minutes', 0) for d in result['days']),
        transfer_minutes=sum(d.get('transfer_minutes', 0) for d in result['days']),
        basis='路线工具核算所选方案；未取得备选实测时，不宣称节省具体分钟。')
    state['route_query_count'], state['route_blocked_code'] = quota[0], blocked
    state['_experience_tool_calls'] = calls
    errors = validate_experience(result, {**state, 'regional_weather':result.get('regional_weather', [])})
    errors.extend(audit_stays(result, req, state))
    errors.extend(audit_requirements(result, req))
    if selected is None:
        errors.extend(audit_choices(result, state, state.get('hotel'), warnings_out=warnings))
    if req.departure_time and prepared:
        last = result['days'][-1]
        if last.get('schedule') and last['schedule'][-1]['end_time'] > req.departure_time:
            errors.append(f"{last['date']} 行程结束晚于用户离开时间 {req.departure_time}。")
    return result, list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))
