"""Official FlyAI CLI boundary: fixed commands, no shell, no automatic retries.

Prices here are sales search observations, NOT ordinary admission policies or
confirmed room inventory. Keep provider identities separate from Amap IDs.
"""
from collections import OrderedDict
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import re
import subprocess
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from ..config import BACKEND_DIR, flyai_process_env, get_settings
from ..models.travel import HotelQuery, PoiQuery, TransportQuery, cny_amount
from .request_gate import AdmissionTimeout, RequestGate, parse_retry_after
from .tool_result import ToolResult

VERSION = '1.0.16'
BUNDLE = BACKEND_DIR / '.runtime' / f'flyai-{VERSION}' / 'flyai-bundle.cjs'
_gate = RequestGate()
_lock = Lock()
_cache = OrderedDict()
_MODELS = {'attractions': PoiQuery, 'hotels': HotelQuery, 'flights': TransportQuery, 'trains': TransportQuery}
_COMMANDS = {'attractions': 'search-poi', 'hotels': 'search-hotel', 'flights': 'search-flight', 'trains': 'search-train'}


def ready() -> bool:
    s = get_settings()
    return bool(s.flyai_enabled and s.flyai_api_key.get_secret_value() and BUNDLE.is_file())


def _text(value, limit=500):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ''
    secret = get_settings().flyai_api_key.get_secret_value()
    value = str(value)
    if secret:
        value = value.replace(secret, '[REDACTED]')
    return ''.join(c for c in value if ord(c) >= 32 and ord(c) != 127)[:limit]


def _url(value):
    value = _text(value, 2048)
    try:
        u = urlsplit(value)
        return value if u.scheme == 'https' and u.hostname and not u.username and not u.password and u.port in (None, 443) else None
    except ValueError:
        return None


def _iso_date(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        return None


def _args(kind, q):
    if kind == 'attractions':
        values = {'city-name': q.city, 'keyword': q.keyword}
    elif kind == 'hotels':
        values = {'dest-name': q.city, 'key-words': q.keyword, 'check-in-date': q.check_in.isoformat(),
                  'check-out-date': q.check_out.isoformat(), 'max-price': q.max_price, 'sort': q.sort}
    else:
        values = {'origin': q.origin, 'destination': q.destination, 'dep-date': q.departure_date.isoformat(), 'max-price': q.max_price}
    return [part for key, value in values.items() if value is not None and value != '' for part in (f'--{key}', str(value))]


def _failure(code, *, retryable=False, wait=None, upstream_calls=0):
    messages = {
        'FLYAI_DISABLED': '旅游平台查询未开启。', 'FLYAI_NOT_CONFIGURED': '旅游平台凭据尚未配置。',
        'FLYAI_RUNTIME_MISSING': '请先安装固定版本的官方 FlyAI 客户端及 Node.js。',
        'FLYAI_AUTH_FAILED': '旅游平台鉴权失败，请检查平台凭据与权限。',
        'UPSTREAM_RATE_LIMITED': '旅游平台限流，请等待后再查询。',
        'FLYAI_TIMEOUT': '旅游平台查询超时，本次未自动重复请求。',
        'FLYAI_INVALID_RESPONSE': '旅游平台返回格式异常。',
        'FLYAI_UPSTREAM_ERROR': '旅游平台查询失败，未取得可靠结果。',
        'FLYAI_INVALID_ARGUMENT': '旅游平台查询参数不合法。',
    }
    outcome = ToolResult.failure(messages[code], code, retryable=retryable, retry_after_seconds=wait).to_dict()
    outcome['stats'] = {'upstream_calls': upstream_calls, 'cache_hit': False}
    return {'outcome': outcome,
            'provider': 'fliggy_flyai', 'items': [], 'cache_hit': False}


def _error(payload, stderr=''):
    # Use structured codes / HTTP protocol markers, never guesses from Chinese prose.
    code = payload.get('error_code', payload.get('code', payload.get('status'))) if isinstance(payload, dict) else None
    if isinstance(payload, dict) and isinstance(payload.get('error'), dict):
        code = payload['error'].get('code', code)
    marker = re.search(r'\b(?:HTTP(?:\s+error)?|status(?:\s+code)?)[\s:=]+(401|403|429|5\d\d)\b', stderr, re.I)
    code = str(code).upper() if code is not None else (marker.group(1) if marker else '')
    if code in ('401', '403', 'UNAUTHORIZED', 'FORBIDDEN', 'INVALID_API_KEY'):
        return _failure('FLYAI_AUTH_FAILED', upstream_calls=1)
    if code in ('429', 'RATE_LIMITED', 'TOO_MANY_REQUESTS'):
        raw_wait = payload.get('retry_after_seconds', payload.get('retryAfter')) if isinstance(payload, dict) else None
        wait = min(60, max(1, parse_retry_after(str(raw_wait)) or 5))
        _gate.defer(wait)
        return _failure('UPSTREAM_RATE_LIMITED', retryable=True, wait=wait, upstream_calls=1)
    return _failure('FLYAI_UPSTREAM_ERROR', upstream_calls=1)


def _normalize(kind, row, q, observed):
    if not isinstance(row, dict):
        raise ValueError('Non-object offer')
    offer = {'provider': 'fliggy_flyai', 'observed_at': observed, 'currency': 'CNY',
             'inventory_confirmed': False, 'price_kind': 'search_reference'}
    if kind in ('attractions', 'hotels'):
        provider_id = row.get('id') if kind == 'attractions' else row.get('shId')
        if provider_id is None or not row.get('name'):
            raise ValueError('Missing provider identity')
        offer.update(provider_id=_text(provider_id, 100), name=_text(row['name'], 200),
                     city=q.city, address=_text(row.get('address')), image_url=_url(row.get('mainPic')))
    if kind == 'attractions':
        ticket = row.get('ticketInfo')
        ticket = ticket if isinstance(ticket, dict) else {}
        amount = cny_amount(ticket.get('price'))
        offer.update(url=_url(row.get('jumpUrl')), free_status=_text(row.get('freePoiStatus'), 30),
                     amount=amount, unit='ticket_product', product_id=_text(ticket.get('itemId'), 100),
                     ticket_name=_text(ticket.get('ticketName'), 200), price_date=_iso_date(ticket.get('priceDate')),
                     price_raw=_text(ticket.get('price'), 100), ordinary_admission_confirmed=False)
    elif kind == 'hotels':
        offer.update(url=_url(row.get('detailUrl')), amount=cny_amount(row.get('price')), unit='room_night_reference',
                     price_raw=_text(row.get('price'), 100), rating=_text(row.get('score'), 30),
                     check_in=q.check_in.isoformat(), check_out=q.check_out.isoformat(),
                     nights=(q.check_out-q.check_in).days,
                     room_occupancy_confirmed=False)
    else:
        journeys = row.get('journeys')
        if not isinstance(journeys, list):
            raise ValueError('Missing journeys')
        segments = []
        for journey in journeys[:5]:
            if not isinstance(journey, dict) or not isinstance(journey.get('segments'), list):
                raise ValueError('Invalid segments')
            for segment in journey['segments'][:10]:
                if not isinstance(segment, dict):
                    raise ValueError('Invalid segment')
                segments.append({k: _text(segment.get(k), 150) for k in (
                    'depCityName', 'depStationName', 'depDateTime', 'arrCityName', 'arrStationName',
                    'arrDateTime', 'duration', 'transportType', 'marketingTransportNo', 'seatClassName')})
        offer.update(provider_id=_text(row.get('id') or row.get('transportId'), 100) or None,
                     origin=q.origin, destination=q.destination, departure_date=q.departure_date.isoformat(),
                     amount=cny_amount(row.get('adultPrice') if row.get('adultPrice') is not None else row.get('price')), unit='adult_journey_reference',
                     fare_basis='adult_price' if row.get('adultPrice') is not None else 'search_display_reference',
                     price_raw=_text(row.get('adultPrice') if row.get('adultPrice') is not None else row.get('price'), 100),
                     url=_url(row.get('jumpUrl')), segments=segments)
    return offer


def query(kind: str, params, *, budget: float | None = None) -> dict:
    """One bounded read. Cache+mutex coalesce simultaneous identical requests.

    Never hand a timeout/unknown CLI failure to the generic tool retry loop.
    """
    from pydantic import ValidationError
    if kind not in _MODELS:
        return _failure('FLYAI_INVALID_ARGUMENT')
    try:
        q = _MODELS[kind].model_validate(params.model_dump() if hasattr(params, 'model_dump') else params)
    except (ValidationError, ValueError, TypeError):
        return _failure('FLYAI_INVALID_ARGUMENT')
    s = get_settings()
    if not s.flyai_enabled:
        return _failure('FLYAI_DISABLED')
    key = s.flyai_api_key.get_secret_value()
    if not key:
        return _failure('FLYAI_NOT_CONFIGURED')
    if not BUNDLE.is_file():
        return _failure('FLYAI_RUNTIME_MISSING')
    limit = min(s.flyai_timeout_seconds, budget) if budget is not None else s.flyai_timeout_seconds
    if limit <= 0:
        return _failure('FLYAI_TIMEOUT')
    deadline = monotonic() + limit
    identity = (hashlib.sha256(key.encode()).digest(), kind, q.model_dump_json())
    if not _lock.acquire(timeout=max(.001, limit)):
        return _failure('FLYAI_TIMEOUT')
    try:
        now = monotonic()
        # Remove expired entries and keep cache bounded even with arbitrary queries.
        for stale in [k for k, v in _cache.items() if v[0] <= now]:
            _cache.pop(stale, None)
        cached = _cache.get(identity)
        if cached:
            result = deepcopy(cached[1])
            result['cache_hit'] = True
            result['outcome']['stats']['cache_hit'] = True
            result['outcome']['stats']['upstream_calls'] = 0
            return result
        try:
            _gate.acquire(interval=s.flyai_request_interval_seconds, budget=max(.001, deadline-monotonic()))
        except AdmissionTimeout as exc:
            return _failure('UPSTREAM_RATE_LIMITED', retryable=True, wait=exc.wait_seconds)
        try:
            if monotonic() >= deadline:
                return _failure('FLYAI_TIMEOUT')
            completed = subprocess.run([s.flyai_node_command, str(BUNDLE), _COMMANDS[kind], *_args(kind, q)],
                env=flyai_process_env(), shell=False, capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=max(.001, deadline-monotonic()), cwd=str(BUNDLE.parent),
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired:
            return _failure('FLYAI_TIMEOUT', upstream_calls=1)
        except OSError:
            return _failure('FLYAI_RUNTIME_MISSING')
        # No raw stdout/stderr or exceptions enter logs/model messages/API errors.
        if len(completed.stdout) > 2_000_000:
            return _failure('FLYAI_INVALID_RESPONSE', upstream_calls=1)
        try:
            payload = json.loads(completed.stdout)
        except (ValueError, TypeError):
            return _error(None, completed.stderr) if completed.returncode else _failure('FLYAI_INVALID_RESPONSE', upstream_calls=1)
        if not isinstance(payload, dict):
            return _failure('FLYAI_INVALID_RESPONSE', upstream_calls=1)
        if completed.returncode or type(payload.get('status')) not in (int, str) or str(payload['status']) != '0':
            return _error(payload, completed.stderr)
        data = payload.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('itemList'), list):
            return _failure('FLYAI_INVALID_RESPONSE', upstream_calls=1)
        observed = datetime.now(timezone.utc).isoformat()
        try:
            items = [_normalize(kind, row, q, observed) for row in data['itemList'][:50]]
        except (ValueError, TypeError, OverflowError):
            return _failure('FLYAI_INVALID_RESPONSE', upstream_calls=1)
        result = {'outcome': ToolResult.success('已查询旅游平台；报价为商品搜索参考，库存与适用条件待确认。',
                    returned_count=len(items), upstream_calls=1, cache_hit=False).to_dict(),
                  'provider': 'fliggy_flyai', 'items': items, 'cache_hit': False, 'observed_at': observed}
        ttl = s.flyai_cache_ttl_seconds if items else min(30, s.flyai_cache_ttl_seconds)
        _cache[identity] = (monotonic()+ttl, deepcopy(result))
        while len(_cache) > 256:
            _cache.popitem(last=False)
        return result
    finally:
        _lock.release()
