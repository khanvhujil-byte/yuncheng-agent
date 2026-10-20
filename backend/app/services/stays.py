"""Dated accommodation facts and cross-day boundaries; no model-selected prices."""
from copy import deepcopy
from datetime import date, timedelta
import re

CONTRACT_VERSION = 11


def raw(value):
    return value.model_dump(mode='json') if hasattr(value, 'model_dump') else value


def identity(hotel):
    hotel = raw(hotel) or {}
    return hotel.get('poi_id') or hotel.get('name')


def night_dates(req):
    nights = req.nights if req.nights is not None else req.travel_days - 1
    return [(date.fromisoformat(req.start_date) + timedelta(days=i)).isoformat() for i in range(nights)]


def stay_policy(req):
    text = (req.free_text_input or '') + req.requirements.hotel.instructions
    # Explicit structured policy wins. Only clear hard-language becomes a lock.
    policy = req.requirements.hotel.stay_policy
    if policy != 'auto':
        return policy
    if re.search(r'(?:不能|不许|不要|不想|不允许|禁止)换酒店|必须(?:全程)?(?:连住|住同一家)', text):
        return 'must_same'
    if any(s in text for s in ('尽量连住', '尽量不换', '少折腾', '行李多', '带老人')):
        return 'prefer_same'
    return 'auto'


def plan_stays(plan):
    """Read-only legacy projection. Explicit new empty lists never fall back."""
    if plan.get('accommodation_version', 0) >= CONTRACT_VERSION:
        return deepcopy(plan.get('stays') or [])
    hotel = raw(plan.get('hotel'))
    if not hotel or not plan.get('start_date'):
        return []
    start = date.fromisoformat(plan['start_date'])
    return [dict(check_in=(start + timedelta(days=i)).isoformat(),
                 check_out=(start + timedelta(days=i+1)).isoformat(), hotel=deepcopy(hotel),
                 locked=False, reason=plan.get('hotel_reason', '历史行程住宿'))
            for i in range(int(plan.get('hotel_nights') or 0))]


def bind_stays(payload, state):
    from ..models.schemas import Hotel
    from ..agents.hotel_agent import HOTEL_BUDGET_RESERVES, DEFAULT_HOTEL_BUDGET_RESERVE
    from .price_catalog import apply_prices
    req = state['request']
    pool = {identity(h): Hotel.model_validate(h) for h in state.get('hotels', [])}
    dates = night_dates(req)
    selected = payload.get('stays')
    segments = payload.get('stay_segments')
    if segments is not None:
        if selected is not None:
            raise ValueError('stays与stay_segments只能提供一种。')
        if not isinstance(segments, list) or len(segments) > len(dates):
            raise ValueError('stay_segments必须为有界住宿段数组。')
        selected = []
        for segment in segments:
            if not isinstance(segment, dict):
                raise ValueError('住宿段必须是对象。')
            try:
                first, end = date.fromisoformat(segment['check_in']), date.fromisoformat(segment['check_out'])
            except (ValueError, TypeError, KeyError):
                raise ValueError('住宿段入住/退房日期不合法。') from None
            length = (end-first).days
            if not 1 <= length <= len(dates):
                raise ValueError('住宿段晚数不合法。')
            selected.extend(dict(check_in=(first+timedelta(days=i)).isoformat(), hotel_id=segment.get('hotel_id'),
                                 reason=segment.get('reason', '')) for i in range(length))
    if not dates and selected is None and payload.get('hotel_id') is None:
        selected = []
    # A single verified hotel ID has an unambiguous dated expansion. Explicit
    # malformed stays are never repaired this way. New prompts request dates.
    if selected is None and 'hotel_id' in payload:
        selected = [{'check_in': d, 'hotel_id': payload.get('hotel_id')} for d in dates]
    if not isinstance(selected, list) or len(selected) != len(dates):
        received = len(selected) if isinstance(selected, list) else type(selected).__name__
        raise ValueError(f'stays应有{len(dates)}晚，实际为{received}；入住日期仅限{dates}。连续入住可用stay_segments的check_in/check_out表达，不住宿为空数组。')
    if any(not isinstance(s, dict) for s in selected):
        raise ValueError('每晚住宿必须是对象。')
    if any(not isinstance(s.get('check_in'), str) for s in selected) or sorted(s.get('check_in', '') for s in selected) != dates:
        raise ValueError('住宿入住日期有遗漏、重复或越界。')
    locks = {d: raw(h) for d, h in (state.get('stay_locks') or {}).items()}
    if req.fixed_hotel:
        anchor = state.get('hotel_anchor')
        if not anchor:
            raise ValueError('用户固定酒店尚未核验。')
        locks.update({d: raw(anchor) for d in dates})
    result = []
    for row in sorted(selected, key=lambda s: s['check_in']):
        key, day = row.get('hotel_id'), row['check_in']
        if key not in pool:
            raise ValueError(f'{day} hotel_id 不在已核验酒店候选池。')
        if day in locks and key != identity(locks[day]):
            raise ValueError(f'{day} 不得更换用户锁定酒店。')
        h = apply_prices(req.city, 'hotel', [pool[key].model_copy(deep=True)], service_date=day, nights=1)[0]
        from .platform_hotels import quoted
        h = quoted(h, day)
        if day not in locks and (h.cost_source != 'imported' or h.cost_status == 'unknown'):
            h.estimated_cost = HOTEL_BUDGET_RESERVES.get(req.accommodation, DEFAULT_HOTEL_BUDGET_RESERVE)
            h.cost_source, h.cost_status = 'static_estimate', 'estimated'
        h.nights = 1
        result.append(dict(check_in=day, check_out=(date.fromisoformat(day)+timedelta(days=1)).isoformat(),
                           hotel=raw(h), locked=day in locks, reason=str(row.get('reason') or '')[:1000]))
    if stay_policy(req) == 'must_same' and len({identity(s['hotel']) for s in result}) > 1:
        raise ValueError('用户要求必须连住，不得更换酒店。')
    return result


def attach_boundaries(plan, req=None, *, endpoints=None):
    """Materialize both daily ends from dated facts, never a whole-trip hotel."""
    stays = plan_stays(plan)
    by_date = {s['check_in']: s for s in stays}
    from .intercity import endpoints as transport_endpoints
    endpoints = transport_endpoints(plan, endpoints)
    days = plan.get('days') or []
    for i, day in enumerate(days):
        yesterday = (date.fromisoformat(day['date']) - timedelta(days=1)).isoformat()
        prior, current = by_date.get(yesterday), by_date.get(day['date'])
        day['dated_boundaries'] = True
        day['route_start'] = deepcopy(prior['hotel']) if prior else (deepcopy(endpoints.get('arrival')) if i == 0 else None)
        day['route_end'] = deepcopy(current['hotel']) if current else (deepcopy(endpoints.get('departure')) if i == len(days)-1 else None)
        switch = bool(prior and current and identity(prior['hotel']) != identity(current['hotel']))
        day['hotel_change'] = switch
        day['stay_check_in'] = current['check_in'] if current else None
        day['transfer_minutes'] = 60 if switch else 0
        day['route_boundary_note'] = ('换住含60分钟整理、退房及入住预留；不默认酒店提供寄存，行李须可随行。' if switch else '')
        if i == 0 and not day['route_start']:
            day['route_boundary_note'] += '首日未提供到达点，路线从首个活动开始，抵达交通未计入。'
        if i == len(days)-1 and not day['route_end']:
            day['route_boundary_note'] += '末日未提供离开点，路线止于末个活动，返程接驳未计入。'
    return plan


def audit_stays(plan, req, state=None):
    state = state or {}
    errors = []
    stays = plan_stays(plan)
    expected = night_dates(req)
    if sorted(s.get('check_in', '') for s in stays) != expected:
        errors.append('住宿日期未完整且唯一覆盖本次晚数。')
    if 0 < len(expected) < req.travel_days - 1:
        errors.append('显式晚数少于行程中间夜晚，尚未明确剩余夜晚住宿；请补充安排。')
    pool = {identity(h): raw(h) for h in state.get('hotels', [])}
    locks = {d: raw(h) for d, h in state.get('stay_locks', {}).items()}
    from .requirements import hotel_allowed
    from ..models.schemas import Hotel
    for s in stays:
        d, h = s['check_in'], s['hotel']
        try:
            expected_checkout = (date.fromisoformat(d)+timedelta(days=1)).isoformat()
        except (ValueError, TypeError, OverflowError):
            errors.append('住宿日期格式不合法。')
            continue
        if s['check_out'] != expected_checkout:
            errors.append(f'{d} 退房日期不合法。')
        if not h.get('location'):
            errors.append(f'{d} 酒店缺少真实坐标。')
        if not hotel_allowed(Hotel.model_validate(h), req.requirements.hotel):
            errors.append(f'{d} 酒店不符合住宿条件。')
        if pool:
            factual = pool.get(identity(h))
            if not factual or any(h.get(k) != factual.get(k) for k in ('name', 'location', 'check_in_after', 'check_in_before', 'check_out_before', 'operations_evidence_url')):
                errors.append(f'{d} 酒店身份或坐标不在事实候选池中。')
        locked = locks.get(d) or (raw(state.get('hotel_anchor')) if req.fixed_hotel else None)
        if locked and identity(h) != identity(locked):
            errors.append(f'{d} 违反固定酒店约束。')
        if req.fixed_hotel and not locked:
            from .tools import is_relevant
            if not s.get('locked') or not h.get('poi_id') or not is_relevant(req.fixed_hotel, h.get('name', '')):
                errors.append(f'{d} 固定酒店缺少核验锚点。')
        requested_lock = next((x for x in req.stay_locks if x.check_in == d), None)
        if requested_lock and not locked:
            from .tools import is_relevant
            if not s.get('locked') or not h.get('poi_id') or not is_relevant(requested_lock.hotel_name, h.get('name', '')):
                errors.append(f'{d} 日期锁定酒店未兑现。')
    if stay_policy(req) == 'must_same' and len({identity(s['hotel']) for s in stays}) > 1:
        errors.append('违反必须连住的要求。')
    if plan.get('accommodation_version', 0) >= CONTRACT_VERSION:
        try:
            for day in plan.get('days', []):
                date.fromisoformat(day['date'])
        except (ValueError, TypeError, KeyError):
            return list(dict.fromkeys([*errors, '每日日期格式不合法，无法核验住宿边界。']))
        expected_plan = attach_boundaries(deepcopy(plan), req, endpoints=state.get('trip_endpoints'))
        for day, correct in zip(plan.get('days', []), expected_plan.get('days', [])):
            for key in ('route_start', 'route_end'):
                # User endpoints are validated at collection. Cached endpoints
                # may be absent in state; don't reinterpret them as hotel stays.
                managed = correct.get(key) is not None or (key == 'route_start' and day['day_index'] > 0) or (key == 'route_end' and day['day_index'] < len(plan['days'])-1)
                if managed:
                    if (identity(day.get(key)) != identity(correct.get(key))
                            or any((raw(day.get(key)) or {}).get(k) != (raw(correct.get(key)) or {}).get(k)
                                   for k in ('name', 'location', 'check_in_after', 'check_in_before', 'check_out_before', 'operations_evidence_url'))):
                        errors.append(f"{day['date']} {key} 与每晚住宿不一致。")
            if day is plan['days'][0] and req.arrival_address and (not day.get('route_start') or day['route_start'].get('name') != req.arrival_address):
                errors.append('用户到达地点未纳入路线核算。')
            if day is plan['days'][-1] and req.departure_address and (not day.get('route_end') or day['route_end'].get('name') != req.departure_address):
                errors.append('用户离开地点未纳入路线核算。')
            if day.get('hotel_change') != correct.get('hotel_change'):
                errors.append(f"{day['date']} 换住状态与住宿日期不一致。")
            if correct.get('hotel_change') and day.get('transfer_minutes', 0) < 60:
                errors.append(f"{day['date']} 缺少换住时间预留。")
            if day.get('hotel_change') and req.requirements.hotel.luggage_policy == 'storage_required':
                errors.append(f"{day['date']} 需要行李寄存，但尚无核验服务；请补充寄存安排或选择连住。")
            # Any remote route estimate is a missing transport fact, not proof
            # that public transit or mountain access exists.
            if req.transportation in ('公共交通', '步行'):
                for leg in day.get('route_legs', []):
                    if leg.get('status') != 'known' and (leg.get('distance_meters') or 0) > 20000:
                        errors.append(f"{day['date']} 远途{req.transportation}路线未核实，不能确认可执行。")
            for a, b in zip(day.get('route_legs', []), day.get('route_legs', [])[1:]):
                if a.get('to_name') != b.get('from_name'):
                    errors.append(f"{day['date']} 路线不连续。")
            start, end = day.get('route_start'), day.get('route_end')
            # Operating windows are read from bound overnight facts, not from
            # potentially malformed display boundary metadata.
            operating_start, operating_end = correct.get('route_start'), correct.get('route_end')
            changed = identity(start) != identity(end)
            if changed and operating_start and operating_start.get('operations_evidence_url') and operating_start.get('check_out_before'):
                departure = day.get('start_minutes', 510) + day.get('transfer_minutes', 0)//2
                h, m = map(int, operating_start['check_out_before'].split(':'))
                if departure > h*60+m:
                    errors.append(f"{day['date']} 退房晚于酒店已核验的{operating_start['check_out_before']}窗口。")
            if changed and operating_end and operating_end.get('operations_evidence_url') and day.get('schedule'):
                arrival = day['schedule'][-1]['end_time']
                if operating_end.get('check_in_after') and arrival < operating_end['check_in_after']:
                    errors.append(f"{day['date']} 尚未到酒店已核验的入住窗口。")
                if operating_end.get('check_in_before') and arrival > operating_end['check_in_before']:
                    errors.append(f"{day['date']} 抵达晚于酒店已核验的最晚入住时间{operating_end['check_in_before']}。")
            legs = day.get('route_legs') or []
            if day.get('schedule') and legs:
                if start and legs[0].get('from_name') != start.get('name'):
                    errors.append(f"{day['date']} 未从实际出发点开始核算。")
                if end and legs[-1].get('to_name') != end.get('name'):
                    errors.append(f"{day['date']} 未核算到当晚住处/离开点。")
            # Every remote visit needs relevant regional weather; don't reuse
            # city forecasts as a claim about mountain conditions.
            for spot in day.get('attractions', []):
                if spot.get('retrieval_scope') == 'surrounding':
                    w = next((w for w in plan.get('regional_weather', []) if w.get('poi_id') == spot.get('poi_id') and w.get('date') == day['date']), None)
                    if w and w.get('district_forecast'):
                        from .itinerary_experience import severe_weather
                        if severe_weather(w['district_forecast']):
                            errors.append(f"{day['date']} {spot['name']}所在行政区有恶劣天气预报，不能确认户外活动可执行；需核实当地预警与开放状态。")
                    if (not w or w.get('status') != 'known') and req.requirements.weather.avoid_rain_outdoors:
                        errors.append(f"{day['date']} 周边活动区域天气未知，无法确认避雨要求。")
    return list(dict.fromkeys(errors))
