"""Factual intercity candidates, joint-planner ID binding, clocks and audits."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from time import monotonic

from ..config import get_settings
from . import flyai

TZ = timezone(timedelta(hours=8))


def _city(value):
    return (value or '').strip().removesuffix('市')


def _dt(value):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)


def key(req):
    return json.dumps([req.origin_city, req.city, req.start_date, req.travel_days, req.intercity_mode, req.return_trip], ensure_ascii=False)


def _offer(row, req, direction, kind):
    segments = row.get('segments', [])
    # Direct adult journeys only in this release. Never flatten a transfer into
    # a non-stop leg or silently omit the transfer buffer.
    if len(segments) != 1:
        return None
    segment = segments[0]
    origin, destination = (req.origin_city, req.city) if direction == 'outbound' else (req.city, req.origin_city)
    if _city(segment.get('depCityName')) != _city(origin) or _city(segment.get('arrCityName')) != _city(destination):
        return None
    try:
        dep, arr = _dt(segment['depDateTime']), _dt(segment['arrDateTime'])
        expected = date.fromisoformat(req.start_date)+timedelta(days=0 if direction == 'outbound' else req.travel_days-1)
        if dep.date() != expected or arr <= dep or (arr-dep).total_seconds() > 24*3600:
            return None
        # Outbound overnight arrivals require a separate departure-date UI.
        if direction == 'outbound' and arr.date() != expected:
            return None
        exit_minutes, boarding = (60, 120) if kind == 'flights' else (30, 45)
        available = arr+timedelta(minutes=exit_minutes) if direction == 'outbound' else dep-timedelta(minutes=boarding)
        if available.date() != expected:
            return None
        amount = row.get('amount')
        if amount is not None and (type(amount) not in (int, float) or not math.isfinite(amount) or not 0 < amount <= 100000):
            amount = None
        facts = dict(direction=direction, mode='flight' if kind == 'flights' else 'train', origin=origin, destination=destination,
            departure_at=dep.isoformat(), arrival_at=arr.isoformat(), departure_station=segment.get('depStationName', ''),
            arrival_station=segment.get('arrStationName', ''), transport_no=segment.get('marketingTransportNo', ''),
            seat_class=segment.get('seatClassName', ''), duration_minutes=round((arr-dep).total_seconds()/60),
            amount_per_adult=amount, price_status='estimated' if amount is not None else 'unknown',
            provider='fliggy_flyai', url=row.get('url'), observed_at=row['observed_at'],
            expires_at=(_dt(row['observed_at'])+timedelta(seconds=get_settings().flyai_cache_ttl_seconds)).isoformat(),
            exit_buffer_minutes=exit_minutes, boarding_buffer_minutes=boarding, inventory_confirmed=False,
            local_window_time=available.strftime('%H:%M'),
            price_note=('平台搜索展示价，按每位成人暂估；成人适用条件尚待确认。' if row.get('fare_basis') == 'search_display_reference' else '每位成人搜索参考；')
                +'机票税费、行李和退改条件/铁路席别与余票待平台确认。')
        if not all(facts[k] for k in ('departure_station', 'arrival_station', 'transport_no')):
            return None
        facts['offer_id'] = 'travel-'+hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()[:24]
        return facts
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


def collect(state):
    req = state['request']
    if req.intercity_mode == 'none':
        return {'transport_candidates': {}, 'transport_warnings': [], 'transport_candidate_key': key(req)}
    if state.get('transport_candidate_key') == key(req):
        return {}
    directions = ['outbound', *(['return'] if req.return_trip else [])]
    candidates = {d: [] for d in directions}
    warnings, calls = [], []
    deadline = monotonic()+get_settings().flyai_planning_budget_seconds
    kinds = ['trains', 'flights'] if req.intercity_mode == 'auto' else ['trains' if req.intercity_mode == 'train' else 'flights']
    for kind in kinds:
        for direction in directions:
            origin, dest = (req.origin_city, req.city) if direction == 'outbound' else (req.city, req.origin_city)
            dep = date.fromisoformat(req.start_date)+timedelta(days=0 if direction == 'outbound' else req.travel_days-1)
            started = monotonic()
            result = flyai.query(kind, {'origin': origin, 'destination': dest, 'departure_date': dep.isoformat()},
                                 budget=max(0, deadline-monotonic()))
            calls.append({'role': 'transport', 'tool': 'search_flyai_'+kind, 'args': {'origin': origin, 'destination': dest, 'date': str(dep)},
                'outcome': result['outcome'], 'attempt_count': 1, 'stop_reason': 'single_read_no_automatic_retry',
                'attempts': [{'outcome': result['outcome'], 'duration_ms': round((monotonic()-started)*1000)}]})
            rows = [o for row in result['items'] if (o := _offer(row, req, direction, kind))]
            # Rank as a useful shortlist; the planner chooses from both modes.
            rows.sort(key=lambda o: (o['amount_per_adult'] is None,
                o['amount_per_adult'] if o['amount_per_adult'] is not None else 0, o['duration_minutes']))
            candidates[direction].extend(rows[:3])
            if result['outcome']['status'] == 'error':
                warnings.append(f"{'去程' if direction == 'outbound' else '返程'}{'火车' if kind == 'trains' else '飞机'}查询：{result['outcome']['message']}")
    for direction, offers in candidates.items():
        candidates[direction] = list({o['offer_id']: o for o in offers}.values())
        if not offers:
            warnings.append(f"{'去程' if direction == 'outbound' else '返程'}没有符合方向、日期和时间窗口的直达班次，尚未完成交通安排。")
    return {'transport_candidates': candidates, 'transport_candidate_key': key(req),
            'transport_warnings': warnings, 'tool_calls': calls, 'warnings': warnings,
            'sources': {'intercity': 'flyai' if all(candidates.values()) else 'degraded'}}


def _endpoint(name, city):
    from . import map_gateway
    from .tools import _codes
    try:
        rows = map_gateway.search_poi(name, city, offset=10)
    except (map_gateway.AmapError, map_gateway.AmapUnreachable):
        return None
    def normalized(v):
        return v.replace(' ', '').replace('（', '(').replace('）', ')').removeprefix(_city(city)).removesuffix('站')
    rows = [p for p in rows if p.location and normalized(p.name) == normalized(name)
            and (not p.typecode or any(c.startswith('15') for c in _codes(p.typecode)))]
    rows = list({p.poi_id or p.name: p for p in rows}.values())
    if len(rows) != 1:
        return None
    p = rows[0]
    return {'name': name, 'poi_id': p.poi_id, 'location': p.location.model_dump()}


def bind(payload, state):
    req = state['request']
    if req.intercity_mode == 'none':
        choices = payload.get('transport_choices')
        if (choices is not None and (not isinstance(choices, dict) or set(choices)-{'outbound', 'return'} or any(v is not None for v in choices.values()))) or payload.get('intercity_legs'):
            raise ValueError('用户没有要求城际交通，不得额外添加交通班次。')
        return []
    pools = state.get('transport_candidates', {})
    choices = payload.get('transport_choices')
    if not isinstance(choices, dict) or set(choices)-{'outbound', 'return'}:
        raise ValueError('transport_choices须为去程outbound和返程return的候选ID对象。')
    legs = []
    for direction in ['outbound', *(['return'] if req.return_trip else [])]:
        pool = {o['offer_id']: o for o in pools.get(direction, [])}
        selected = choices.get(direction)
        if not pool:
            if selected is not None:
                raise ValueError('没有真实交通候选时必须选null，不能编造班次。')
            continue
        if selected not in pool:
            raise ValueError(f'{direction}必须选择该方向真实交通候选ID。')
        leg = deepcopy({k:v for k,v in pool[selected].items() if not k.startswith('_')})
        if _dt(leg['expires_at']) <= datetime.now(TZ):
            # Keep timed route facts for review; never describe stale fare as current.
            leg.update(amount_per_adult=None, price_status='unknown', price_note='交通查询参考价已失效，当前票价及余票待核实。')
        station = leg['arrival_station'] if direction == 'outbound' else leg['departure_station']
        from .tool_retry import deadline
        if '_endpoint_checked' not in pool[selected]:
            token = deadline.set(monotonic()+4)
            try:
                pool[selected]['local_endpoint'] = _endpoint(station, req.city)
                pool[selected]['_endpoint_checked'] = True
            finally:
                deadline.reset(token)
        leg['local_endpoint'] = deepcopy(pool[selected].get('local_endpoint'))
        legs.append(leg)
    if not req.return_trip and choices.get('return') is not None:
        raise ValueError('单程行程不应安排返程。')
    return legs


def effective_request(req, plan):
    updates = {}
    for leg in plan.get('intercity_legs', []) or []:
        try:
            if leg['direction'] == 'outbound':
                updates['arrival_time'] = (_dt(leg['arrival_at'])+timedelta(minutes=leg['exit_buffer_minutes'])).strftime('%H:%M')
            else:
                updates['departure_time'] = (_dt(leg['departure_at'])-timedelta(minutes=leg['boarding_buffer_minutes'])).strftime('%H:%M')
        except (KeyError, TypeError, ValueError, OverflowError):
            continue  # audit reports malformed legs; display paths never crash.
    return req.model_copy(update=updates)


def endpoints(plan, fallback=None):
    result = dict(fallback or {})
    for leg in plan.get('intercity_legs', []) or []:
        if isinstance(leg, dict) and leg.get('direction') in ('outbound', 'return') and leg.get('local_endpoint'):
            result['arrival' if leg['direction'] == 'outbound' else 'departure'] = leg['local_endpoint']
    return result


def audit(plan, req, state=None):
    legs = plan.get('intercity_legs', []) or []
    if not isinstance(legs, list) or any(not isinstance(l, dict) for l in legs):
        return ['交通班次结构不合法。']
    if req.intercity_mode == 'none':
        return ['未要求城际交通却出现额外班次。'] if legs else []
    errors = []
    directions = ['outbound', *(['return'] if req.return_trip else [])]
    if sorted(l.get('direction', '') for l in legs) != sorted(directions):
        errors.append('去程或返程尚未取得真实班次，城际交通安排未完成。')
    for leg in legs:
        try:
            direction = leg['direction']
            if direction not in directions or leg['mode'] not in ('flight', 'train'):
                errors.append('交通方向或方式不合法。')
                continue
            origin, dest = (req.origin_city, req.city) if direction == 'outbound' else (req.city, req.origin_city)
            expected = date.fromisoformat(req.start_date)+timedelta(days=0 if direction == 'outbound' else req.travel_days-1)
            if _city(leg['origin']) != _city(origin) or _city(leg['destination']) != _city(dest) or _dt(leg['departure_at']).date() != expected:
                errors.append('城际班次方向或日期与本次请求不一致。')
            if req.intercity_mode != 'auto' and leg['mode'] != req.intercity_mode:
                errors.append('城际班次不符合用户选择的交通方式。')
            if not leg.get('local_endpoint'):
                errors.append('机场/车站接驳坐标尚未核验，不能确认首末日路线。')
            if _dt(leg['arrival_at']) <= _dt(leg['departure_at']):
                errors.append('交通到达时间须晚于出发时间。')
            expected_exit, expected_boarding = (60, 120) if leg['mode'] == 'flight' else (30, 45)
            if leg.get('exit_buffer_minutes') != expected_exit or leg.get('boarding_buffer_minutes') != expected_boarding:
                errors.append('机场/车站预留时间被修改。')
            amount = leg.get('amount_per_adult')
            if amount is not None and (type(amount) not in (int, float) or not math.isfinite(amount) or not 0 < amount <= 100000):
                errors.append('交通报价不合法。')
            if state is not None and 'transport_candidates' in state:
                source = next((o for o in state['transport_candidates'].get(direction, []) if o['offer_id'] == leg.get('offer_id')), None)
                factual = ('departure_at', 'arrival_at', 'transport_no', 'departure_station', 'arrival_station', 'mode', 'origin', 'destination',
                           'seat_class', 'duration_minutes', 'exit_buffer_minutes', 'boarding_buffer_minutes', 'local_window_time',
                           'observed_at', 'expires_at', 'provider', 'url', 'local_endpoint')
                if source is None or any(leg.get(k) != source.get(k) for k in factual):
                    errors.append('交通班次不在事实候选池，或班次事实被修改。')
                elif leg.get('amount_per_adult') is not None and leg['amount_per_adult'] != source.get('amount_per_adult'):
                    errors.append('城际票价与平台事实不一致。')
            if _dt(leg['expires_at']) <= datetime.now(TZ) and leg.get('amount_per_adult') is not None:
                errors.append('交通参考价已失效，须重新核价。')
        except (KeyError, ValueError, TypeError, OverflowError):
            errors.append('交通班次结构或日期不合法。')
    effective = effective_request(req, plan) if len(legs) == len(directions) and not errors else req
    if effective.travel_days == 1 and effective.arrival_time and effective.departure_time and effective.arrival_time >= effective.departure_time:
        errors.append('去返程及候车预留冲突，同日已无可用游玩时间。')
    return list(dict.fromkeys(errors))


def time_windows(state):
    """Expose code-derived meal windows for each bounded pair of factual IDs."""
    from itertools import product
    from .day_window import policy
    req = state['request']
    if req.intercity_mode == 'none':
        return []
    pools = state.get('transport_candidates', {})
    result = []
    for outbound, inbound in product(pools.get('outbound') or [None], (pools.get('return') or [None]) if req.return_trip else [None]):
        effective = effective_request(req, {'intercity_legs': [l for l in (outbound, inbound) if l]})
        dates = list(dict.fromkeys([req.start_date, (date.fromisoformat(req.start_date)+timedelta(days=req.travel_days-1)).isoformat()]))
        result.append({'outbound': outbound['offer_id'] if outbound else None, 'return': inbound['offer_id'] if inbound else None,
                       'feasible': not (req.travel_days == 1 and effective.arrival_time and effective.departure_time and effective.arrival_time >= effective.departure_time),
                       'daily_time_windows': {d: policy(effective, d) for d in dates}})
    return result
