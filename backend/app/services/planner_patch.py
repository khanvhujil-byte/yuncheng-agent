"""Apply a bounded joint-planner decision patch, never model-written POI facts."""
from copy import deepcopy


def expand(payload, state):
    if 'meal_patches' not in payload:
        return payload
    plan = state.get('plan') or {}
    draft = state.get('planner_draft')
    patches = payload['meal_patches']
    if (payload.get('days') is not None or state.get('accommodation_version', 0) < 11
            or not ((isinstance(draft, dict) and draft.get('days')) or (plan.get('days') and plan.get('accommodation_version', 0) >= 11))):
        raise ValueError('餐次补丁只能用于已有v11候选方案，不能同时提供days。')
    if not isinstance(patches, list) or not 1 <= len(patches) <= 12:
        raise ValueError('meal_patches必须为1~12项数组。')
    result = deepcopy(draft) if draft else dict(stays=[dict(check_in=s['check_in'],hotel_id=s['hotel'].get('poi_id') or s['hotel']['name'],reason=s.get('reason',''))
                         for s in plan['stays']], hotel_reason=state.get('hotel_reason',''),
                  days=[dict(day_index=d['day_index'],date=d['date'],description=d.get('description',''),
                             attractions=[dict(poi_id=a.get('poi_id') or a['name'],description=a.get('description','')) for a in d['attractions']],
                             meals=[dict(type=m['type'],restaurant_id=m['restaurant_id'],reason=m.get('description','')) for m in d['meals']])
                        for d in plan['days']], overall_suggestions=plan.get('overall_suggestions',''))
    if not draft and state['request'].intercity_mode != 'none':
        result['transport_choices'] = {leg['direction']: leg['offer_id'] for leg in plan.get('intercity_legs', [])}
        result['transport_choices'].setdefault('outbound', None)
        result['transport_choices'].setdefault('return', None)
    seen = set()
    pool = {r.poi_id for r in state.get('restaurants', [])}
    for patch in patches:
        if not isinstance(patch, dict) or set(patch) != {'day_index','type','restaurant_id'}:
            raise ValueError('餐次补丁只能包含day_index、type、restaurant_id。')
        index, kind, identifier = patch['day_index'], patch['type'], patch['restaurant_id']
        if (type(index) is not int or not 0 <= index < len(result['days']) or not isinstance(kind, str)
                or kind not in ('breakfast','lunch','dinner') or not isinstance(identifier, str) or identifier not in pool):
            raise ValueError('餐次补丁日期、餐次或候选ID不合法。')
        if (index,kind) in seen:
            raise ValueError('同一餐次只能修改一次。')
        seen.add((index,kind))
        meal = next((m for m in result['days'][index]['meals'] if m['type']==kind),None)
        if meal is None:
            raise ValueError('原方案缺少要修改的餐次。')
        meal['restaurant_id'], meal['reason'] = identifier, '联合编排根据独立复核反馈修正本餐选择。'
    return deepcopy(result)
