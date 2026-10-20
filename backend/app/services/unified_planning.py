"""One planner owns POI choices. Code binds identities and audits decisions."""
from copy import deepcopy

from ..models.schemas import Hotel, Restaurant


def _weather_candidates(state):
    from .itinerary_experience import severe_weather
    from .tools import ToolContext
    from .tool_scheduler import execute_batch
    risky_days = sum(severe_weather(w) for w in state.get('weather', []))
    indoor = [a for a in state.get('attractions', [])
              if any(word in a.name for word in ('博物馆', '美术馆', '科技馆', '水族馆', '海洋馆'))]
    if not risky_days or len(indoor) >= risky_days:
        return {}
    req = state['request']
    ctx = ToolContext(city=req.city, adcode=state.get('adcode'), start_date=req.start_date,
                      travel_days=state['travel_days'], nights=state['nights'],
                      requirements=req.requirements, city_center=state.get('city_center'))
    ctx.attractions = {a.poi_id or a.name: a for a in state.get('attractions', [])}
    execute_batch('attractions', [({'id': 'weather-'+str(i)}, 'search_attractions',
                  {'keyword': keyword, 'limit': 12}) for i, keyword in enumerate(('博物馆', '水族馆'))], ctx)
    return {'attractions': list(ctx.attractions.values()), 'tool_calls': ctx.calls,
            'errors': ctx.errors,
            'warnings': ['强降雨、雷暴或高温日期已补充室内场馆候选；开放时间与预约仍需核实。']}


def prepare_planning_context(state):
    from ..config import get_settings
    from ..agents.hotel_candidates import collect_hotels
    from ..agents.hotel_agent import _candidate_key
    from ..agents.planner_agent import _select_candidates
    from .food import ensure_food_coverage
    update = {}
    # Seeded revisions and legacy resumptions may not have run the new collector.
    if state.get('hotel_candidate_key') != _candidate_key(state['request'], state['nights']):
        update = collect_hotels(state)
        state = {**state, **update}
    from .regional_stays import collect_regional_context
    regional = collect_regional_context(state) if get_settings().adaptive_stays_enabled else {}
    state = {**state, **regional}
    from concurrent.futures import ThreadPoolExecutor
    from .platform_hotels import collect as collect_platform_hotels
    from .intercity import collect as collect_transport
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='travel-data') as pool:
        hotel_future = pool.submit(collect_platform_hotels, state)
        transport_future = pool.submit(collect_transport, state)
        platform, transport = hotel_future.result(), transport_future.result()
    state = {**state, **platform, **transport,
             'sources': {**state.get('sources', {}), **platform.get('sources', {}), **transport.get('sources', {})}}
    from .stay_options import prepare_options
    options = prepare_options(state) if get_settings().adaptive_stays_enabled else {}
    state = {**state, **options}
    weather_update = _weather_candidates(state)
    if weather_update:
        update = {**update, **weather_update,
                  'tool_calls': [*update.get('tool_calls', []), *weather_update['tool_calls']],
                  'warnings': [*update.get('warnings', []), *weather_update['warnings']]}
        state = {**state, **weather_update}
    from .day_goals import effective_goals
    selected, _ = _select_candidates(list(state.get('attractions') or []), state['travel_days'], effective_goals(state['request']), state.get('weather'))
    preview = {'days': [{'day_index': i, 'attractions': [s.model_dump() for s in selected[i::state['travel_days']]], 'meals': []}
                        for i in range(state['travel_days'])]}
    cfg = get_settings()
    calls, warnings = ensure_food_coverage(preview, state,
                         query_limit=max(0, min(1, cfg.food_coverage_query_limit - 2)),
                         budget_seconds=cfg.food_coverage_budget_seconds / 2) if state.get('experience_enabled') else ([], [])
    return {**update, **regional, **platform, **transport, **options, 'accommodation_version': 11 if get_settings().adaptive_stays_enabled else 0, 'unified_planning': True, 'restaurants': state.get('restaurants', []),
            # ensure_food_coverage 原地升级了 sources.food（catalog -> catalog+amap）；
            # 不把 sources 一起返回的话，这个升级会被 LangGraph 丢掉，下游仍按
            # catalog_partial 判 degraded —— 数据其实已经补齐（假降级）。
            'sources': dict(state.get('sources') or {}),
            'food_coverage_queries': state.get('food_coverage_queries', []),
            'food_coverage_elapsed_seconds': state.get('food_coverage_elapsed_seconds', 0),
            'food_coverage_blocked_code': state.get('food_coverage_blocked_code'),
            'tool_calls': [*update.get('tool_calls', []), *regional.get('tool_calls', []), *platform.get('tool_calls', []), *transport.get('tool_calls', []), *options.get('tool_calls', []), *calls],
            'warnings': [*update.get('warnings', []), *regional.get('warnings', []), *platform.get('warnings', []), *transport.get('warnings', []), *warnings]}


def bind_choices(payload, state):
    """Hydrate ID-only output before schema validation; reject foreign IDs."""
    from ..agents.hotel_agent import HOTEL_BUDGET_RESERVES, DEFAULT_HOTEL_BUDGET_RESERVE
    from .itinerary_experience import meal_from_restaurant
    req = state['request']
    result = deepcopy(payload)
    from .intercity import bind as bind_transport, effective_request
    result['intercity_legs'] = bind_transport(result, state)
    req = effective_request(req, result)
    spots = {a.poi_id or a.name: a for a in state.get('attractions', [])}
    restaurants = {r.poi_id: r for raw in state.get('restaurants', []) if (r := Restaurant.model_validate(raw))}
    hotels = {h.poi_id or h.name: h for raw in state.get('hotels', []) if (h := Hotel.model_validate(raw))}
    if state.get('accommodation_version', 0) >= 11:
        from .stays import bind_stays
        result['stays'] = bind_stays(result, state)
        result['accommodation_version'] = 11
        result['regional_weather'] = state.get('regional_weather', [])
        # Compatibility summary: never represent a multi-hotel trip as one hotel.
        ids = {s['hotel'].get('poi_id') or s['hotel']['name'] for s in result['stays']}
        hotel = Hotel.model_validate(result['stays'][0]['hotel']) if len(ids) == 1 else None
        if hotel:
            hotel.nights = state['nights']
            from .platform_hotels import quoted
            hotel = quoted(hotel, req.start_date)
    else:
        hotel = None
    if state.get('accommodation_version', 0) < 11 and state['nights'] > 0:
        identity = result.get('hotel_id')
        if hotels and identity not in hotels:
            raise ValueError('hotel_id 必须从酒店候选选择，不能省略或虚构。')
        if identity is not None and identity not in hotels:
            raise ValueError('hotel_id 不在酒店候选池。')
        if identity in hotels:
            hotel = hotels[identity].model_copy(deep=True)
            if req.fixed_hotel:
                anchor = state.get('hotel_anchor')
                if anchor is None or identity != (anchor.poi_id or anchor.name):
                    raise ValueError('不得更换用户指定酒店。')
            hotel.nights = state['nights']
            if not req.fixed_hotel and (hotel.cost_source != 'imported' or hotel.cost_status == 'unknown'):
                # The model never supplies the room price.
                hotel.estimated_cost = HOTEL_BUDGET_RESERVES.get(req.accommodation, DEFAULT_HOTEL_BUDGET_RESERVE)
                hotel.cost_source, hotel.cost_status = 'static_estimate', 'estimated'
    elif state.get('accommodation_version', 0) < 11 and result.get('hotel_id') is not None:
        raise ValueError('本次无住宿晚数，hotel_id 必须为 null。')
    for day in result['days']:
        if not isinstance(day, dict):
            raise ValueError('days 中每项必须为对象。')
        hydrated = []
        for item in day.get('attractions', []):
            if not isinstance(item, dict) or item.get('poi_id') not in spots:
                raise ValueError(f"景点不在候选池，必须通过候选 poi_id 选择：{item!r}")
            factual = spots[item['poi_id']].model_dump()
            factual['description'] = str(item.get('description') or factual.get('description') or '')
            hydrated.append(factual)
        day['attractions'] = hydrated
        meals = []
        if not isinstance(day.get('meals'), list):
            raise ValueError('每天 meals 必须是数组。')
        for item in day['meals']:
            if not isinstance(item, dict) or item.get('type') not in ('breakfast', 'lunch', 'dinner'):
                raise ValueError('餐次必须是 breakfast/lunch/dinner。')
            identity = item.get('restaurant_id')
            if identity not in restaurants:
                raise ValueError(f"{day.get('date')} {item['type']} 的 restaurant_id={identity!r} 不在餐饮候选；请按本轮候选ID修正，不可编造。")
            meal = meal_from_restaurant(restaurants[identity], item['type'])
            meal['description'] = str(item.get('reason') or meal['description'])
            meals.append(meal)
        day['meals'] = meals
        day.setdefault('transportation', req.transportation)
        day.setdefault('accommodation', req.accommodation)
        from .day_window import policy
        expected = policy(req, day.get('date'))['meal_types'] if state.get('accommodation_version', 0) >= 11 else ['breakfast', 'lunch', 'dinner']
        day['meal_types'] = expected
        if sorted(m['type'] for m in meals) != sorted(expected):
            raise ValueError(f"{day.get('date')} 餐次必须完整匹配到离时间窗口：{expected}。")
    return result, hotel


def audit_choices(plan, state, hotel, warnings_out=None):
    """Return useful errors; never silently select another restaurant or hotel.

    ``warnings_out`` distinguishes "the model chose poorly but a better option
    exists" (an error) from "there is simply no 4km candidate in this area"
    (a data-coverage warning). The latter should not turn an otherwise valid
    plan into a partial failure.
    """
    from .food_recommendation import meal_contexts, nearby_pool, distances
    from .requirements import restaurant_allowed, hotel_allowed
    needs = state['request'].requirements.food
    restaurants = [Restaurant.model_validate(r) for r in state.get('restaurants', [])]
    allowed = [r for r in restaurants if restaurant_allowed(r, needs)]
    pool = {r.poi_id: r for r in allowed}
    errors, used = [], set()
    if not plan.get('stays') and state.get('nights', 0) > 0 and hotel is None:
        errors.append('住宿候选为空，无法选定酒店；请调整住宿条件或补充数据。')
    if hotel is not None and not hotel_allowed(hotel, state['request'].requirements.hotel):
        errors.append('所选酒店不满足住宿硬约束。')
    for day in plan['days']:
        meals = {m['type']: m for m in day['meals']}
        for kind, anchor, following in meal_contexts(day, hotel.model_dump() if hotel else None):
            identity = meals.get(kind, {}).get('restaurant_id')
            if not identity:
                # 餐饮候选为空/降级时 _select_meals 会给占位项，它没有
                # restaurant_id。这里必须显式报错，而不是 KeyError 让整条
                # 已算好的行程变成 500。
                errors.append(f"{day['date']} {kind} 缺少已核验餐厅，不能把占位菜名当成真实餐厅。")
                continue
            selected = pool.get(identity)
            if selected is None:
                errors.append(f"{day['date']} {kind} 不满足餐饮价格/排除条件。")
                continue
            feasible = nearby_pool(allowed, anchor, following, kind, citywide=needs.travel_scope == 'citywide')
            if feasible and identity not in {r.poi_id for r in feasible}:
                errors.append(f"{day['date']} {kind} 餐厅不符合就近/餐次条件，可选ID：" + ','.join(r.poi_id for r in feasible[:8]))
            if not feasible:
                errors.append(f"{day['date']} {kind} 没有符合餐次的候选。")
            alternatives = [r.poi_id for r in feasible if r.poi_id not in used]
            if identity in used and alternatives:
                errors.append(f"{day['date']} {kind} 重复餐厅 {identity}，附近尚有未使用候选：" + ','.join(alternatives[:8]))
            if needs.travel_scope != 'citywide':
                distance = distances(selected, anchor, following, kind)[0]
                if distance > 4:
                    closer = [r for r in feasible if distances(r, anchor, following, kind)[0] <= 4]
                    message = f"{day['date']} {kind} 距用餐片区超过4公里，需换候选或重排景点。"
                    if closer or warnings_out is None:
                        errors.append(message)
                    else:
                        warnings_out.append(
                            f"{day['date']} {kind} 附近没有4公里内的可用候选，保留当前餐厅"
                            f"（约{distance:.1f}公里）；营业时间与交通请出发前确认。")
            used.add(identity)
    if plan.get('accommodation_version', 0) >= 11:
        from .stays import audit_stays
        errors.extend(audit_stays(plan, state['request'], state))
    from .intercity import audit as audit_transport
    errors.extend(audit_transport(plan, state['request'], state))
    return errors


def correction_context(plan, state, hotel):
    """Give retry the rejected decision and exact per-slot feasible IDs."""
    import json
    from .food_recommendation import meal_contexts, nearby_pool, distances
    from .requirements import restaurant_allowed
    needs = state['request'].requirements.food
    allowed = [r for raw in state.get('restaurants', [])
               if restaurant_allowed(r := Restaurant.model_validate(raw), needs)]
    days = []
    used = set()
    matching_slots = []
    for day in plan['days']:
        slots = []
        for kind, anchor, following in meal_contexts(day, hotel.model_dump() if hotel else None):
            feasible = nearby_pool(allowed, anchor, following, kind, citywide=needs.travel_scope == 'citywide')
            ids = [r.poi_id for r in feasible if needs.travel_scope == 'citywide' or distances(r, anchor, following, kind)[0] <= 4]
            slots.append({'type': kind, 'eligible_restaurant_ids': ids[:8],
                          'unused_restaurant_ids': [i for i in ids if i not in used][:8]})
            selected = next((m.get('restaurant_id') for m in day['meals'] if m['type'] == kind), None)
            matching_slots.append((slots[-1], ([selected] if selected in ids else []) + [i for i in ids if i != selected]))
            used.add(next((m.get('restaurant_id') for m in day['meals'] if m['type'] == kind), None))
        days.append({'date': day['date'], 'day_index': day['day_index'],
                     'total_minutes': day.get('total_minutes'),
                     'attraction_names': [a['name'] for a in day['attractions']],
                     'overloaded_routes': [{k: leg.get(k) for k in ('from_name', 'to_name', 'duration_minutes', 'status')}
                          for leg in day.get('route_legs', [])] if day.get('total_minutes', 0) > state['request'].requirements.pace.max_day_minutes else [],
                     'attraction_ids': [a['poi_id'] for a in day['attractions']],
                     'previous_meals': [{'type':m['type'], 'restaurant_id':m.get('restaurant_id')} for m in day['meals']],
                     'meal_options_for_this_layout': slots})
    # A small bipartite matching supplies joint, feasible diversity advice.
    # It never replaces the planner's choices or bypasses route/budget audits.
    assignments = {}
    def assign(index, visited):
        for identifier in matching_slots[index][1]:
            if identifier in visited:
                continue
            visited.add(identifier)
            if identifier not in assignments or assign(assignments[identifier], visited):
                assignments[identifier] = index
                return True
        return False
    for index in range(len(matching_slots)):
        assign(index, set())
    for identifier, index in assignments.items():
        matching_slots[index][0]['suggested_restaurant_id'] = identifier
    return '\n【上次方案和按相同规则计算的候选】\n' + json.dumps({
        'stays': [{'check_in': s['check_in'], 'hotel_id': s['hotel'].get('poi_id') or s['hotel']['name']} for s in plan.get('stays', [])], 'hotel_id': hotel.poi_id or hotel.name if hotel else None, 'days': days}, ensure_ascii=False) + (
        '\n若保持酒店和景点顺序，请从各餐次eligible_restaurant_ids选择并优先不重复。'
        'suggested_restaurant_id是跨餐次匹配后的真实候选建议，保持布局时可优先采用以避免局部贪心导致重复；实际路线和预算仍需复核。'
        '空数组表示当前布局无可用餐厅，需要更改酒店或景点布局；布局改变后这些列表需重新计算，不能照搬。'
        'overloaded_routes是该日实际复核的路程耗时；注意同名不同区景点。'
        '超时日应减少可选景点、换近处候选或跨日重分必去点，不能重复提交相同布局。')
