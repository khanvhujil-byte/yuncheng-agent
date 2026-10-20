"""Conservative link between platform products and exact itinerary POIs."""
from datetime import datetime, timedelta, timezone
import json
import math
import re
import sqlite3
from time import monotonic
import unicodedata

from ..config import get_settings
from . import flyai, price_catalog


def _name(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value or ''))


def match_admission(spot, target, offers):
    """Only an unambiguous, dated, exact-name adult basic-admission product.

    A child POI, absent date, discount/package/start-price is evidence of a
    product search, never evidence for this venue's ordinary adult admission.
    """
    matches = []
    for offer in offers:
        if _name(offer.get('name')) != _name(spot.get('name')):
            continue
        # Same-name branches require matching address, not fuzzy name overlap.
        if not spot.get('address') or _name(offer.get('address')) != _name(spot['address']):
            continue
        ticket = _name(offer.get('ticket_name'))
        if not re.fullmatch(r'(?:大门票|门票|入园票|入场票)(?:成人票|成人)', ticket):
            continue
        amount = offer.get('amount')
        if (type(amount) not in (int, float) or not math.isfinite(amount) or not 0 < amount <= 10000
                or offer.get('price_date') != target or not offer.get('product_id')
                or not offer.get('provider_id') or not offer.get('url')):
            continue
        matches.append(offer)
    # Multiple products may hide different conditions. Don't choose the lowest.
    return matches[0] if len(matches) == 1 else None


def refresh(plan, request, selected, runtime):
    s = get_settings()
    if not flyai.ready() or not s.flyai_ticket_query_limit:
        return plan
    outcomes = runtime.setdefault('flyai_ticket_outcomes', {})
    count = int(runtime.get('flyai_ticket_queries', 0))
    elapsed = float(runtime.get('flyai_ticket_elapsed_seconds', 0))
    started = monotonic()
    deadline = started + max(0, s.flyai_ticket_budget_seconds-elapsed)
    try:
        for day in plan.get('days', []):
            if selected is not None and day.get('day_index') not in selected:
                continue
            for spot in day.get('attractions', []):
                if not spot.get('poi_id') or spot.get('admission_policy') in ('ordinary_access', 'consumption'):
                    continue
                if spot.get('ticket_price') is not None and spot.get('ticket_price_status') in ('known', 'estimated') and spot.get('ticket_price_source') not in ('unknown', 'static_estimate', 'llm_estimated'):
                    continue
                key = json.dumps([request.city, spot['poi_id'], spot['name'], spot.get('address'), day['date']], ensure_ascii=False)
                if key in outcomes:
                    saved = outcomes[key]
                    if saved.get('expires_at') and datetime.fromisoformat(saved['expires_at']) <= datetime.now(timezone.utc):
                        spot['ticket_price_note'] = '[FLYAI_QUOTE_EXPIRED] 先前平台参考价已失效，继续查证日常票价。'
                    else:
                        spot.update(saved['fields'])
                    continue
                if count >= s.flyai_ticket_query_limit or monotonic() >= deadline:
                    continue
                count += 1
                call_started = monotonic()
                result = flyai.query('attractions', {'city': request.city, 'keyword': spot['name']}, budget=deadline-monotonic())
                duration_ms = round((monotonic()-call_started)*1000, 3)
                offers = result['items']
                match = match_admission(spot, day['date'], offers) if result['outcome']['status'] == 'ok' else None
                runtime.setdefault('_ticket_search_calls', []).append({
                    'role': 'ticket', 'tool': 'search_flyai_tickets',
                    'args': {'city': request.city, 'poi_id': spot['poi_id'], 'date': day['date']},
                    'outcome': result['outcome'], 'result': result['outcome']['status'], 'attempt_count': 1,
                    'attempts': [{'attempt': 1, 'duration_ms': duration_ms, 'outcome': result['outcome']}],
                    'stop_reason': 'single_read_no_automatic_retry',
                    'evidence': {'provider': 'fliggy_flyai', 'offers': offers[:5],
                                 'matched_product_id': match['product_id'] if match else None}})
                if match:
                    # Search price is estimated budget, never a verified ordinary tariff.
                    amount = math.ceil(match['amount'])
                    note = ('飞猪同地点成人基础门票商品参考，非日常挂牌价或已确认库存；'
                            f"票种：{match['ticket_name']}；商品：{match['product_id']}；"
                            f"平台景点：{match['provider_id']}；适用日：{match['price_date']}；"
                            f"观察时间：{match['observed_at']}。支付前确认预约及适用条件。")
                    expires = datetime.fromisoformat(match['observed_at']) + timedelta(seconds=max(1, s.flyai_cache_ttl_seconds))
                    record = {
                        'city': request.city.removesuffix('市'), 'kind': 'attraction', 'poi_id': spot['poi_id'],
                        'amount': amount, 'unit': 'admission', 'status': 'estimated', 'source_url': match['url'],
                        'observed_at': match['observed_at'], 'expires_at': expires.isoformat(),
                        'service_from': day['date'], 'service_to': day['date'], 'note': note}
                    changes = dict(ticket_price=amount, ticket_price_status='estimated', ticket_price_source='imported',
                                   ticket_price_evidence_url=match['url'], ticket_price_note=note)
                    try:
                        price_catalog.import_prices([record])
                    except (ValueError, sqlite3.Error):
                        # Optional product evidence must never abort itinerary generation.
                        changes = {'ticket_price_note': '[FLYAI_PRICE_PERSIST_ERROR] 平台参考价未能安全绑定或保存，继续查证日常票价。'}
                else:
                    code = result['outcome'].get('error_code') or ('FLYAI_PRODUCT_SCOPE_UNCONFIRMED' if offers else 'FLYAI_NO_PRODUCTS')
                    changes = {'ticket_price_note': (spot.get('ticket_price_note') or '') +
                               f' [{code}] 平台未返回可绑定到该地点及游玩日的唯一成人基础票，继续查官网或日常票价估算。'}
                outcomes[key] = {'fields': changes, 'expires_at': expires.isoformat() if match else None}
                spot.update(changes)
    finally:
        runtime['flyai_ticket_queries'] = count
        runtime['flyai_ticket_elapsed_seconds'] = elapsed + monotonic()-started
    return plan
