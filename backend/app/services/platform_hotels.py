"""Merge sales observations with map-verified identities, never invent coordinates."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import re
from time import monotonic

from ..config import get_settings
from ..models.schemas import Hotel
from . import flyai, map_gateway


def _name(value):
    return re.sub(r'[\s（）()]', '', value or '')


def _address(value):
    return re.sub(r'[\s,，。]', '', re.sub(r'[（(][^）)]*[）)]', '', value or '')).replace('楼', '层')


def _same_property(hotel, offer, city):
    """Require a specific matching address, unique identity and compatible floor.

    Suppliers often rename the same property's branch. Brand-core agreement is
    permitted only with a matching street/building address; a brand alone is
    never enough to inherit a price.
    """
    a,b = _address(hotel.address),_address(offer.get('address'))
    short,long = sorted((a,b),key=len)
    if len(short)<6 or not re.search(r'\d', short) or short not in long:
        return False
    floors = [set(re.findall(r'(\d+)\s*[层楼]', v or '')) for v in (hotel.address, offer.get('address'))]
    if all(floors) and floors[0] != floors[1]:
        return False
    if _name(hotel.name) == _name(offer.get('name')):
        return True
    def core(value):
        value = re.split(r'[（(]', value or '')[0]
        value = re.sub(r'[A-Za-z\s]', '', value).replace(city.removesuffix('市'), '')
        for word in ('快捷酒店', '电竞酒店', '大酒店', '酒店', '宾馆', '饭店'):
            value = value.replace(word, '')
        return value
    first,second = core(hotel.name),core(offer.get('name'))
    return first == second and len(first)>=2 and (len(first)>=3 or len(short)>=8)


def quoted(hotel, service_date):
    h = Hotel.model_validate(hotel).model_copy(deep=True)
    now = datetime.now(timezone.utc)
    quotes = []
    for quote in h.platform_quotes:
        try:
            if (quote['check_in'] <= service_date < quote['check_out']
                    and datetime.fromisoformat(quote['expires_at']) > now
                    and type(quote['amount']) in (int, float) and 0 < quote['amount'] <= 100000):
                if flyai._url(quote.get('url')) and quote.get('observed_at'):
                    quotes.append(quote)
        except (KeyError, TypeError, ValueError):
            continue
    if not quotes:
        # Never retain an expired platform amount copied from an old snapshot.
        if h.price_note.startswith('飞猪'):
            h.estimated_cost, h.cost_status, h.cost_source = 0, 'unknown', 'unknown'
            h.price_evidence_url = None
            h.price_note = '平台房价已失效或不覆盖本晚；需重新核价。'
        return h
    quote = max(quotes, key=lambda q: q['observed_at'])
    h.estimated_cost = math.ceil(quote['amount'])
    h.cost_status, h.cost_source = 'estimated', 'imported'
    h.price_evidence_url = quote['url']
    h.price_note = ('飞猪指定日期的每房每晚搜索参考；非逐晚保证价，房型、可住人数、税费、早餐、取消规则与库存待确认。'
                    f"查询覆盖{quote['check_in']}至{quote['check_out']}；观测{quote['observed_at']}。")
    return h


def collect(state, *, budget=None):
    req = state['request']
    nights = state.get('nights', req.nights if req.nights is not None else req.travel_days-1)
    if nights <= 0 or req.fixed_hotel or not flyai.ready():
        return {}
    from ..models.travel import china_today
    if date.fromisoformat(req.start_date) <= china_today():
        return {}
    signature = json.dumps([req.city, req.start_date, nights, req.accommodation, req.requirements.hotel.model_dump()], sort_keys=True)
    key = hashlib.sha256(signature.encode()).hexdigest()
    if state.get('platform_hotel_key') == key:
        return {}
    cfg, started = get_settings(), monotonic()
    end = started + (budget if budget is not None else cfg.flyai_planning_budget_seconds)
    checkout = (date.fromisoformat(req.start_date)+timedelta(days=nights)).isoformat()
    result = flyai.query('hotels', {'city': req.city, 'check_in': req.start_date, 'check_out': checkout,
                        'max_price': req.requirements.hotel.max_price_per_night,
                        'keyword': req.requirements.hotel.keywords[0] if req.requirements.hotel.keywords else '',
                        'sort': 'no_rank'}, budget=max(.001, end-monotonic()))
    call = {'role': 'hotels', 'tool': 'search_flyai_hotels', 'args': {'city': req.city, 'check_in': req.start_date, 'check_out': checkout},
            'outcome': result['outcome'], 'attempt_count': 1,
            'attempts': [{'outcome': result['outcome'], 'duration_ms': round((monotonic()-started)*1000)}]}
    pool = {h.poi_id or h.name: h for raw in state.get('hotels', []) if (h := Hotel.model_validate(raw))}
    warnings = []
    if result['outcome']['status'] == 'error':
        warnings.append('酒店平台查询未完成：'+result['outcome']['message']+'；保留地图候选和明确预算预留。')
    verified = list(pool.values())
    offers = result['items'][:cfg.flyai_hotel_match_limit]
    # Verify each bounded offer within the shared deadline; a failed first
    # property must not prevent later properties from acquiring coordinates.
    for offer in offers:
        if monotonic() >= end:
            break
        if any(_name(h.name) == _name(offer['name']) for h in verified):
            continue
        try:
            from .tools import _is_hotel, _hotel_of
            from .tool_retry import deadline
            token = deadline.set(end)
            try:
                rows = map_gateway.search_poi(offer['name'], req.city, offset=10)
            finally:
                deadline.reset(token)
            verified.extend(_hotel_of(p, nights) for p in rows if _is_hotel(p) and p.location)
        except (map_gateway.AmapError, map_gateway.AmapUnreachable):
            warnings.append('平台酒店的地图坐标核验暂不可用，未把无坐标商品加入住宿候选。')
    matched = 0
    for offer in offers:
        matches = [h for h in verified if h.location and _same_property(h, offer, req.city)]
        matches = list({h.poi_id or h.name: h for h in matches}.values())
        if len(matches) != 1:
            continue
        h = matches[0].model_copy(deep=True)
        h.provider_id, h.provider_url = offer['provider_id'], offer.get('url')
        if (type(offer.get('amount')) in (int, float) and math.isfinite(offer['amount'])
                and 0 < offer['amount'] <= 100000 and flyai._url(offer.get('url'))):
            observed = datetime.fromisoformat(offer['observed_at'])
            h.platform_quotes = [dict(amount=offer['amount'], url=offer['url'], check_in=offer['check_in'], check_out=offer['check_out'],
                observed_at=offer['observed_at'], expires_at=(observed+timedelta(seconds=cfg.flyai_cache_ttl_seconds)).isoformat())]
        h = quoted(h, req.start_date)
        pool[h.poi_id or h.name] = h
        matched += 1
    call['outcome']['stats'].update(matched_count=matched, candidate_count=len(pool))
    if offers and not matched:
        warnings.append('平台酒店商品尚未与地图地点身份唯一对应；保留原候选，未套用同名分店报价。')
    from .requirements import hotel_allowed
    candidates = [h for h in pool.values() if hotel_allowed(h, req.requirements.hotel)]
    return {'hotels': candidates, 'platform_hotel_key': key, 'tool_calls': [call], 'warnings': warnings,
            'sources': {**state.get('sources', {}), 'hotel': 'flyai' if matched else state.get('sources', {}).get('hotel', 'amap')}}
