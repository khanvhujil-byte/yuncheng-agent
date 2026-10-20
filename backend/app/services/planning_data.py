"""Allowlisted planner data requests, using existing run-wide collector limits."""
from copy import deepcopy


def fulfill(requests, state):
    from .regional_stays import collect_regional_context
    from .food import ensure_food_coverage
    from .stays import raw
    if not isinstance(requests, list) or not 1 <= len(requests) <= 2:
        raise ValueError('data_requests 必须是1~2条补采请求。')
    previous = list(state.get('planning_data_requests') or [])
    if len(previous) + len(requests) > 2:
        raise ValueError('本次运行的模型补采请求已达到2项上限。')
    anchors = {a.poi_id or a.name: a for a in state.get('attractions', [])}
    anchors.update({h.poi_id or h.name: h for h in state.get('hotels', [])})
    validated = []
    for item in requests:
        if not isinstance(item, dict) or item.get('data_type') not in ('hotels', 'food', 'weather'):
            raise ValueError('补采仅支持hotels、food、weather。')
        identifier, kind = item.get('anchor_id'), item['data_type']
        if not isinstance(identifier, str) or identifier not in anchors or not anchors[identifier].location:
            raise ValueError('补采anchor_id必须来自有真实坐标的候选池。')
        if any(r['anchor_id'] == identifier and r['data_type'] == kind for r in previous + validated):
            raise ValueError('同一锚点和资料类型不可重复补采。')
        validated.append(dict(anchor_id=identifier, data_type=kind))
    working = dict(state)
    calls, warnings = [], []
    for item in validated:
        anchor = anchors[item['anchor_id']]
        if item['data_type'] == 'food':
            point = raw(anchor)
            preview = {'days': [dict(day_index=0, dated_boundaries=True, route_start=point, route_end=point,
                                     attractions=[point], meals=[])]}
            extra, notes = ensure_food_coverage(preview, working)
            calls.extend(extra)
            warnings.extend(notes)
            item['result'] = '按锚点复核餐饮覆盖；受已有次数和时间配额限制，未找到时仍为资料不足。'
        else:
            # Only an attraction's region is valid for this collector. A hotel
            # anchor cannot create an unverified new activity area.
            selected = [a for a in state.get('attractions', []) if (a.poi_id or a.name) == item['anchor_id']]
            update = collect_regional_context({**working, 'attractions': selected})
            calls.extend(update.pop('tool_calls', []))
            warnings.extend(update.pop('warnings', []))
            working.update(update)
            item['result'] = '已复核区域目录和有限补采；天气仅有行政区参考时仍不等于景区现场预报。'
    keys = ('hotels', 'restaurants', 'sources', 'regional_weather', 'regional_hotel_queries',
            'food_coverage_queries', 'food_coverage_elapsed_seconds', 'food_coverage_blocked_code')
    return {**{k: deepcopy(working[k]) for k in keys if k in working},
            'planning_data_requests': previous + validated, 'tool_calls': calls, 'warnings': warnings}
