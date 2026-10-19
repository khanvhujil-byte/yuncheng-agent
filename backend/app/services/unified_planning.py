"""One planner owns POI choices. Code binds identities and audits decisions."""
from copy import deepcopy

from ..models.schemas import Hotel, Restaurant


def prepare_planning_context(state):
    from ..agents.hotel_candidates import collect_hotels
    from ..agents.hotel_agent import _candidate_key
    from ..agents.planner_agent import _select_candidates
    from .food import ensure_food_coverage
    update = {}
    # Seeded revisions and legacy resumptions may not have run the new collector.
    if state.get('hotel_candidate_key') != _candidate_key(state['request'], state['nights']):
        update = collect_hotels(state)
        state = {**state, **update}
    selected, _ = _select_candidates(list(state.get('attractions') or []), state['travel_days'])
    preview = {'days': [{'day_index': i, 'attractions': [s.model_dump() for s in selected[i::state['travel_days']]], 'meals': []}
                        for i in range(state['travel_days'])]}
    calls, warnings = ensure_food_coverage(preview, state) if state.get('experience_enabled') else ([], [])
    return {**update, 'unified_planning': True, 'restaurants': state.get('restaurants', []),
            # ensure_food_coverage 原地升级了 sources.food（catalog -> catalog+amap）；
            # 不把 sources 一起返回的话，这个升级会被 LangGraph 丢掉，下游仍按
            # catalog_partial 判 degraded —— 数据其实已经补齐（假降级）。
            'sources': dict(state.get('sources') or {}),
            'food_coverage_queries': state.get('food_coverage_queries', []),
            'food_coverage_elapsed_seconds': state.get('food_coverage_elapsed_seconds', 0),
            'food_coverage_blocked_code': state.get('food_coverage_blocked_code'),
            'tool_calls': [*update.get('tool_calls', []), *calls],
            'warnings': [*update.get('warnings', []), *warnings]}


def bind_choices(payload, state):
    """Hydrate ID-only output before schema validation; reject foreign IDs."""
    from ..agents.hotel_agent import HOTEL_BUDGET_RESERVES, DEFAULT_HOTEL_BUDGET_RESERVE
    from .itinerary_experience import meal_from_restaurant
    req = state['request']
    result = deepcopy(payload)
    spots = {a.poi_id or a.name: a for a in state.get('attractions', [])}
    restaurants = {r.poi_id: r for raw in state.get('restaurants', []) if (r := Restaurant.model_validate(raw))}
    hotels = {h.poi_id or h.name: h for raw in state.get('hotels', []) if (h := Hotel.model_validate(raw))}
    hotel = None
    if state['nights'] > 0:
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
    elif result.get('hotel_id') is not None:
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
                raise ValueError('restaurant_id 必须从餐饮候选选择；候选不足应明确失败，不可编造。')
            meal = meal_from_restaurant(restaurants[identity], item['type'])
            meal['description'] = str(item.get('reason') or meal['description'])
            meals.append(meal)
        day['meals'] = meals
        day.setdefault('transportation', req.transportation)
        day.setdefault('accommodation', req.accommodation)
        if sorted(m['type'] for m in meals) != ['breakfast', 'dinner', 'lunch']:
            raise ValueError('每天必须有且仅有早中晚三餐。')
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
    if state.get('nights', 0) > 0 and hotel is None:
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
                distance = distances(selected, anchor, following)[0]
                if distance > 4:
                    closer = [r for r in feasible if distances(r, anchor, following)[0] <= 4]
                    message = f"{day['date']} {kind} 距用餐片区超过4公里，需换候选或重排景点。"
                    if closer or warnings_out is None:
                        errors.append(message)
                    else:
                        warnings_out.append(
                            f"{day['date']} {kind} 附近没有4公里内的可用候选，保留当前餐厅"
                            f"（约{distance:.1f}公里）；营业时间与交通请出发前确认。")
            used.add(identity)
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
    for day in plan['days']:
        slots = []
        for kind, anchor, following in meal_contexts(day, hotel.model_dump() if hotel else None):
            feasible = nearby_pool(allowed, anchor, following, kind, citywide=needs.travel_scope == 'citywide')
            slots.append({'type': kind, 'eligible_restaurant_ids': [r.poi_id for r in feasible
                          if needs.travel_scope == 'citywide' or distances(r, anchor, following)[0] <= 4]})
        days.append({'date': day['date'], 'day_index': day['day_index'],
                     'attraction_ids': [a['poi_id'] for a in day['attractions']],
                     'previous_meals': [{'type':m['type'], 'restaurant_id':m.get('restaurant_id')} for m in day['meals']],
                     'meal_options_for_this_layout': slots})
    return '\n【上次方案和按相同规则计算的候选】\n' + json.dumps({
        'hotel_id': hotel.poi_id or hotel.name if hotel else None, 'days': days}, ensure_ascii=False) + (
        '\n若保持酒店和景点顺序，请从各餐次eligible_restaurant_ids选择并优先不重复。'
        '空数组表示当前布局无可用餐厅，需要更改酒店或景点布局；布局改变后这些列表需重新计算，不能照搬。')
