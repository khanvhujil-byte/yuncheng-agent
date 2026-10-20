"""Source-specific official adult admission policies; never parse arbitrary prices.

Each adapter binds an exact city/name to a maintained official URL and a narrowly
scoped adult admission pattern. A changed page is a miss, not a guessed price.
"""
import copy
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import json
import logging
import re
import threading

import httpx

from ..config import get_settings
from . import catalog, price_catalog

logger = logging.getLogger(__name__)
SOURCES = (
    {"city": "北京", "names": ("故宫", "故宫博物院"), "url": "https://www.dpm.org.cn/Visit.html",
     "rules": ((tuple(range(4, 11)), r"旺季[，,\s]*大门票\s*(\d+)\s*元/人"),
               ((11, 12, 1, 2, 3), r"淡季[，,\s]*大门票\s*(\d+)\s*元/人")),
     "note": "官网成人大门票；珍宝馆、钟表馆等另收费项目不包含，需预约。"},
    {"city": "成都", "names": ("成都大熊猫繁育研究基地", "成都大熊猫繁育研究基地(成都熊猫基地)", "成都熊猫基地"),
     "url": "https://www.panda.org.cn/cn/service/ticket/",
     "rules": ((tuple(range(1, 13)), r"成人票\s*(\d+)\s*元/人"),),
     "note": "官网成人入园票；观光车等附加服务不包含，需按时段预约。"},
)
_LOCK = threading.RLock()
_TTL = timedelta(hours=24)
_FAILURE_TTL = timedelta(minutes=15)
_SCHEMA = """CREATE TABLE IF NOT EXISTS official_ticket_page(
 url TEXT PRIMARY KEY, policies_json TEXT NOT NULL, observed_at TEXT NOT NULL,
 expires_at TEXT NOT NULL, status TEXT NOT NULL)"""


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def parse_policy(source, html):
    parser = _Text()
    parser.feed(html)
    text = re.sub(r"\s+", " ", " ".join(parser.parts))
    policies = []
    for months, pattern in source["rules"]:
        matches = list(re.finditer(pattern, text))
        amounts = {int(match.group(1)) for match in matches}
        if len(amounts) != 1 or not 0 <= next(iter(amounts)) <= 10000:
            raise ValueError("official adult admission policy missing or ambiguous")
        policies.append({"months": months, "amount": amounts.pop(), "quote": matches[0].group(0)})
    return policies


def _load(source):
    # Bounded, public URLs are code-maintained; no model/user URL is fetched.
    # One in-process flight plus durable successful/failed page cache.
    with _LOCK:
        now = datetime.now(timezone.utc)
        with catalog._connection() as db:
            db.execute(_SCHEMA)
            row = db.execute("SELECT * FROM official_ticket_page WHERE url=?", (source["url"],)).fetchone()
        if row and datetime.fromisoformat(row["expires_at"]) > now:
            return dict(row)
        try:
            response = httpx.get(source["url"], timeout=4, follow_redirects=False,
                                 headers={"User-Agent": "TripPlanner/1.0 (public admission policy lookup)"})
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise ValueError("official page too large")
            policies = parse_policy(source, response.text)
            status, ttl = "ok", _TTL
        except httpx.HTTPStatusError as exc:
            policies, status, ttl = [], f'HTTP_{exc.response.status_code}', _FAILURE_TTL
        except ValueError:
            policies, status, ttl = [], 'OFFICIAL_POLICY_UNRESOLVED', _FAILURE_TTL
        except Exception as exc:
            logger.warning("Official ticket lookup unavailable: %s", type(exc).__name__)
            policies, status, ttl = [], 'OFFICIAL_NETWORK_ERROR', _FAILURE_TTL
        record = {"url": source["url"], "policies_json": json.dumps(policies, ensure_ascii=False),
                  "observed_at": now.isoformat(), "expires_at": (now + ttl).isoformat(), "status": status}
        with catalog._connection() as db:
            db.execute("INSERT OR REPLACE INTO official_ticket_page VALUES(?,?,?,?,?)", tuple(record.values()))
        return record


def _refresh_official(plan, request, day_indices=None):
    result = copy.deepcopy(plan)
    from .admission_policy import apply_admission_policy
    enabled = get_settings().official_ticket_lookup_enabled
    selected = set(day_indices) if day_indices is not None else None
    used_sources = set()
    for day in result.get("days", []):
        if selected is not None and day.get("day_index") not in selected:
            continue
        for spot in day.get("attractions", []):
            spot.update(apply_admission_policy(spot))
            if spot.get('admission_policy') in {'ordinary_access', 'consumption'} or not enabled:
                continue
            if spot.get("ticket_price") is not None and spot.get('ticket_price_status') in ('known', 'estimated') and spot.get("ticket_price_source") not in {"unknown", "static_estimate"}:
                continue
            source = next((s for s in SOURCES if s["city"] == request.city.removesuffix("市") and spot.get("name") in s["names"]), None)
            if source is None:
                spot['ticket_price_note'] = '[OFFICIAL_SOURCE_UNSUPPORTED] 尚未接入该地点的官网票价解析；高德 POI 不提供门票字段，当前实际票价待核实。'
                continue
            if not spot.get("poi_id"):
                spot['ticket_price_note'] = '[POI_ID_MISSING] 缺少唯一地点标识，无法安全绑定官网票价。'
                continue
            if source["url"] not in used_sources and len(used_sources) >= 2:
                spot['ticket_price_note'] = '[OFFICIAL_QUERY_LIMIT] 已达到本轮官网查询上限，保留未知票价。'
                continue
            used_sources.add(source["url"])
            try:
                target = date.fromisoformat(day["date"])
                today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).date()
                if not today <= target <= today + timedelta(days=90):
                    spot['ticket_price_note'] = '[OUTSIDE_POLICY_WINDOW] 出行日期不在当前官方政策核验窗口内，未把当前价格当作该日有效报价。'
                    continue
                record = _load(source)
                if not record or record.get('status') != 'ok':
                    code = (record or {}).get('status', 'OFFICIAL_LOOKUP_UNAVAILABLE')
                    reason = ('官网拒绝访问或返回 HTTP 错误。' if code.startswith('HTTP_') else
                              '官网页面未找到唯一明确的成人入场票价，可能为页面结构变化或价格歧义。'
                              if code == 'OFFICIAL_POLICY_UNRESOLVED' else '官网查询暂不可用，未取得可核验的票价。')
                    spot['ticket_price_note'] = f'[{code}] {reason}'
                    continue
                policy = next(p for p in json.loads(record["policies_json"]) if target.month in p["months"])
                price_catalog.import_prices([{
                    "city": request.city.removesuffix("市"), "kind": "attraction", "poi_id": spot["poi_id"],
                    "amount": policy["amount"], "unit": "admission", "status": "known", "source_url": source["url"],
                    "observed_at": record["observed_at"], "expires_at": record["expires_at"],
                    "service_from": day["date"], "service_to": day["date"],
                    "note": source["note"] + " 原文：" + policy["quote"],
                }])
                spot.update(ticket_price=policy["amount"], ticket_price_status="known",
                            ticket_price_source="imported", ticket_price_evidence_url=source["url"],
                            ticket_price_note=source["note"] + " 原文：" + policy["quote"])
            except Exception as exc:
                logger.warning("Official ticket evidence could not be applied: %s", type(exc).__name__)
                spot['ticket_price_note'] = '[OFFICIAL_LOOKUP_ERROR] 官网证据读取、解析或持久化失败，未确认实际票价。'
    return result


def _search_batch(plan, request, selected, queries, outcomes, deadline, lookup):
    """Bounded three-way IO; divide time between waves so one POI cannot starve all others."""
    from concurrent.futures import ThreadPoolExecutor
    from math import ceil
    from time import monotonic
    from .ticket_web import cached_lookup
    pending, results, seen = [], {}, set()
    limit = max(0, get_settings().ticket_web_query_limit - len(queries))
    for day in plan.get('days', []):
        if selected is not None and day.get('day_index') not in selected:
            continue
        for spot in day.get('attractions', []):
            if not spot.get('poi_id') or spot.get('admission_policy') in ('ordinary_access', 'consumption'):
                continue
            if spot.get('ticket_price') is not None and spot.get('ticket_price_status') in ('known', 'estimated') and spot.get('ticket_price_source') not in ('unknown', 'static_estimate'):
                continue
            identity = json.dumps([request.city, spot['poi_id'], day['date']], ensure_ascii=False)
            if identity in seen or identity in outcomes or identity in queries:
                continue
            seen.add(identity)
            cached = cached_lookup(request.city, spot, day['date'])
            if cached is not None:
                results[identity] = (cached, 0)
            elif len(pending) < limit:
                pending.append((identity, spot, day['date']))

    def search(item, end):
        identity, spot, target = item
        started = monotonic()
        try:
            result = lookup(request.city, spot, target, end)
        except (ValueError, httpx.HTTPError, OSError, TimeoutError) as exc:
            result = {'status': 'WEB_LOOKUP_ERROR', 'reasons': [type(exc).__name__]}
        return identity, (result, int((monotonic()-started)*1000))

    with ThreadPoolExecutor(max_workers=3, thread_name_prefix='ticket') as pool:
        for offset in range(0, len(pending), 3):
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            waves = ceil((len(pending)-offset)/3)
            end = min(deadline, monotonic() + remaining/waves)
            futures = [pool.submit(search, item, end) for item in pending[offset:offset+3]]
            for future in futures:
                identity, result = future.result()
                results[identity] = result
    return results


def refresh_plan_tickets(plan, request, day_indices=None, *, lookup_state=None):
    """Dated internal evidence -> maintained official adapter -> public search.

    Unresolved admissions use a separately labelled daily model estimate;
    model failure retains an explicit budget reserve, never a sourced quote.
    """
    from time import monotonic
    from ..models.schemas import Attraction
    from .admission_policy import apply_admission_policy
    from .ticket_web import lookup
    result = copy.deepcopy(plan)
    selected = set(day_indices) if day_indices is not None else None
    for day in result.get('days', []):
        if selected is not None and day.get('day_index') not in selected:
            continue
        for spot in day.get('attractions', []):
            factual = Attraction.model_validate(spot)
            priced = price_catalog.apply_prices(request.city, 'attraction', [factual], service_date=day['date'])[0]
            spot.update(apply_admission_policy(priced.model_dump()))
    result = _refresh_official(result, request, day_indices)
    settings = get_settings()
    runtime = lookup_state if lookup_state is not None else {}
    from .flyai_tickets import refresh as refresh_platform_tickets
    result = refresh_platform_tickets(result, request, selected, runtime)
    if not settings.ticket_web_search_enabled:
        from .ticket_estimates import fill
        return fill(result, request, runtime, selected)
    queries = list(runtime.get('ticket_lookup_queries') or [])
    outcomes = dict(runtime.get('ticket_lookup_results') or {})
    started = monotonic()
    elapsed = float(runtime.get('ticket_lookup_elapsed_seconds') or 0)
    deadline = started + max(0, settings.ticket_web_budget_seconds - elapsed)
    prefetched = _search_batch(result, request, selected, queries, outcomes, deadline, lookup)
    observed = {}
    for day in result.get('days', []):
        if selected is not None and day.get('day_index') not in selected:
            continue
        for spot in day.get('attractions', []):
            if spot.get('admission_policy') in ('ordinary_access', 'consumption'):
                continue
            if spot.get('ticket_price') is not None and spot.get('ticket_price_status') in ('known', 'estimated') and spot.get('ticket_price_source') not in ('unknown', 'static_estimate'):
                continue
            if not spot.get('poi_id'):
                spot['ticket_price_note'] = '[POI_ID_MISSING] 缺少唯一地点标识，无法绑定搜索到的门票。'
                continue
            identity = json.dumps([request.city, spot['poi_id'], day['date']], ensure_ascii=False)
            if identity in observed:
                spot.update(observed[identity])
                continue
            if identity in outcomes:
                spot.update(outcomes[identity])
                continue
            if identity in queries:
                spot['ticket_price_note'] = '[WEB_QUERY_ALREADY_ATTEMPTED] 本次运行已搜索该地点和日期，未取得可核验门票，停止重复请求。'
                continue
            if identity not in prefetched:
                spot['ticket_price_note'] = '[WEB_QUERY_BUDGET] 已达到本次门票网页搜索次数或时间上限，实际票价待核实。'
                continue
            queries.append(identity)
            previous = spot.get('ticket_price_note', '')
            try:
                evidence, duration_ms = prefetched[identity]
                from .tool_result import ToolResult
                http_calls = evidence.get('http_calls') or []
                outcome = (ToolResult.success('已取得门票证据') if evidence['status'] == 'ok'
                           else ToolResult.failure('未取得可核验门票证据', evidence['status'])).to_dict()
                outcome['stats'] = {'upstream_calls': len(http_calls), 'cache_hit': bool(evidence.get('cached')),
                                    'lookup_status': evidence['status'], 'http_request_count': len(http_calls)}
                runtime.setdefault('_ticket_search_calls', []).append({
                    'role': 'ticket', 'tool': 'search_ticket_web',
                    'args': {'city': request.city, 'poi_id': spot['poi_id'], 'date': day['date']},
                    'result': evidence['status'], 'outcome': outcome,
                    'evidence': evidence, 'attempt_count': 1,
                    'attempts': [{'outcome': outcome, 'duration_ms': duration_ms}],
                    'http_attempts': http_calls})
                if evidence['status'] != 'ok':
                    details = '、'.join(evidence.get('reasons', []))
                    spot['ticket_price_note'] = (f"[{evidence['status']}] 内部门票数据未命中后已执行网页搜索，"
                                                 '未取得地点、成人入场产品与日期可对应的官网/政府证据。'
                                                 + (f'原因：{details}。' if details else '') + previous)
                    outcomes[identity] = {'ticket_price_note': spot['ticket_price_note']}
                    continue
                note = ('内部数据未命中后通过网页搜索取得的公开成人入场政策参考，非该日订单报价；'
                        '预约和适用条件需出发前确认。原文：' + evidence['quote'])
                price_catalog.import_prices([{
                    'city': request.city.removesuffix('市'), 'kind': 'attraction', 'poi_id': spot['poi_id'],
                    'amount': evidence['amount'], 'unit': 'admission', 'status': 'estimated',
                    'source_url': evidence['url'], 'observed_at': evidence['observed_at'],
                    'expires_at': evidence['expires_at'], 'service_from': day['date'], 'service_to': day['date'],
                    'note': note}])
                spot.update(ticket_price=evidence['amount'], ticket_price_status='estimated',
                            ticket_price_source='imported', ticket_price_evidence_url=evidence['url'], ticket_price_note=note)
                from .ticket_sources import register
                register(request.city, spot['poi_id'], spot['name'], evidence['url'])
                observed[identity] = {key: value for key, value in spot.items() if key.startswith('ticket_price')}
                outcomes[identity] = observed[identity]
            except (ValueError, httpx.HTTPError, OSError, TimeoutError) as exc:
                spot['ticket_price_note'] = f'[WEB_LOOKUP_ERROR] 网页搜索或证据入库失败：{type(exc).__name__}；实际门票未知。'
                outcomes[identity] = {'ticket_price_note': spot['ticket_price_note']}
    runtime['ticket_lookup_queries'] = queries
    runtime['ticket_lookup_results'] = outcomes
    runtime['ticket_lookup_elapsed_seconds'] = elapsed + monotonic() - started
    from .ticket_estimates import fill
    return fill(result, request, runtime, selected)
