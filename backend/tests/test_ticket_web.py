from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.services import price_catalog, ticket_pricing, ticket_web
from tests.factories import make_attraction, make_day, make_request


def trip():
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    req = make_request(city='深圳', start_date=today, travel_days=1, nights=0, must_visit=[])
    return req, {'days': [make_day(0, today, [make_attraction('测试景区', poi_id='A', ticket_price=None)])]}


@pytest.fixture
def enabled(settings, monkeypatch):
    monkeypatch.setattr(settings, 'ticket_web_search_enabled', True)
    monkeypatch.setattr(settings, 'official_ticket_lookup_enabled', True)


def evidence():
    now = datetime.now(timezone.utc)
    return {'status': 'ok', 'amount': 88, 'quote': '深圳测试景区成人票88元',
            'url': 'https://www.sz.gov.cn/ticket-policy', 'observed_at': now.isoformat(),
            'expires_at': (now + timedelta(hours=24)).isoformat()}


def test_internal_miss_searches_web_then_persists_and_next_request_hits_internal(enabled, monkeypatch):
    req, plan = trip()
    calls = []
    monkeypatch.setattr(ticket_web, 'lookup', lambda *a: calls.append(a) or evidence())
    result = ticket_pricing.refresh_plan_tickets(plan, req)
    assert len(calls) == 1
    spot = result['days'][0]['attractions'][0]
    assert spot['ticket_price'] == 88 and spot['ticket_price_status'] == 'estimated'
    assert spot['ticket_price_evidence_url'] == evidence()['url']
    again = ticket_pricing.refresh_plan_tickets(plan, req)
    assert len(calls) == 1 and again['days'][0]['attractions'][0]['ticket_price'] == 88


def test_expired_internal_quote_searches_again(enabled, monkeypatch):
    req, plan = trip()
    now = datetime.now(timezone.utc)
    price_catalog.import_prices([{'city': req.city, 'kind': 'attraction', 'poi_id': 'A', 'amount': 20,
        'unit': 'admission', 'status': 'known', 'source_url': 'https://www.sz.gov.cn/old',
        'observed_at': (now-timedelta(days=2)).isoformat(), 'expires_at': (now-timedelta(days=1)).isoformat(),
        'service_from': req.start_date, 'service_to': req.start_date}])
    monkeypatch.setattr(ticket_web, 'lookup', lambda *a: evidence())
    result = ticket_pricing.refresh_plan_tickets(plan, req)
    assert result['days'][0]['attractions'][0]['ticket_price'] == 88


def test_failed_search_keeps_reason_and_retry_does_not_repeat_request(enabled, monkeypatch):
    req, plan = trip()
    calls, state = [], {}
    monkeypatch.setattr(ticket_web, 'lookup', lambda *a: calls.append(a) or {
        'status': 'WEB_EVIDENCE_UNRESOLVED', 'reasons': ['HTTP_403']})
    result = ticket_pricing.refresh_plan_tickets(plan, req, lookup_state=state)
    assert result['days'][0]['attractions'][0]['ticket_price'] is None
    assert 'HTTP_403' in result['days'][0]['attractions'][0]['ticket_price_note']
    again = ticket_pricing.refresh_plan_tickets(plan, req, lookup_state=state)
    assert len(calls) == 1
    assert 'HTTP_403' in again['days'][0]['attractions'][0]['ticket_price_note']


@pytest.mark.parametrize('text', ['深圳测试景区成人票88元。', '深圳测试景区门票：免费开放。'])
def test_extracts_exact_venue_adult_or_free_policy(text):
    req, _ = trip()
    page = ticket_web.Page()
    page.feed('<p>'+text+'</p>')
    assert ticket_web.extract_policy(page, '测试景区', '深圳', req.start_date)['amount'] in (0, 88)


@pytest.mark.parametrize('text', ['深圳测试景区儿童票20元。', '深圳测试景区成人票88元起。',
    '深圳测试景区成人票88元，成人票99元。', '深圳测试景区旺季成人票88元。',
    '深圳另一景区成人票88元。', '深圳测试景区学生优惠成人票88元。',
    '深圳测试景区2020年成人票88元。'])
def test_rejects_wrong_product_ambiguous_outdated_or_unmatched_price(text):
    req, _ = trip()
    page = ticket_web.Page()
    page.feed('<p>'+text+'</p>')
    with pytest.raises(ValueError):
        ticket_web.extract_policy(page, '测试景区', '深圳', req.start_date)


def test_search_fetches_page_not_snippet_and_caches_failure(monkeypatch):
    req, plan = trip()
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        html = ('<h2><a href="https://www.sz.gov.cn/tickets">测试景区成人票1元</a></h2>'
                if 'bing.com' in url else '<p>深圳测试景区成人票88元。</p>')
        return httpx.Response(200, text=html, request=httpx.Request('GET', url))
    monkeypatch.setattr(httpx, 'get', get)
    monkeypatch.setattr(ticket_web, '_public', lambda url: None)
    from time import monotonic
    result = ticket_web.lookup(req.city, plan['days'][0]['attractions'][0], req.start_date, monotonic()+10)
    assert result['amount'] == 88 and len(calls) == 2
    again = ticket_web.lookup(req.city, plan['days'][0]['attractions'][0], req.start_date, monotonic()+10)
    assert again['cached'] and len(calls) == 2


@pytest.mark.parametrize('url', ['http://www.sz.gov.cn/t', 'https://www.sz.gov.cn.evil.com/t',
                               'https://user:password@www.sz.gov.cn/t', 'https://127.0.0.1/t'])
def test_untrusted_or_local_source_urls_rejected(url):
    assert not ticket_web.trusted_url(url)


def test_irrelevant_search_uses_sogou_then_fetches_actual_source(monkeypatch):
    req, plan = trip()
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        if 'bing.com' in url:
            html = '<h2><a href="https://www.sz.gov.cn/">城市首页</a></h2><a href="https://www.sz.gov.cn/license">页脚备案</a>'
        elif '/web?' in url:
            html = '<h3><a name="dttl" href="/link?url=verified-result">测试景区门票</a></h3>'
        elif '/link?' in url:
            html = '<script>window.location.replace("https://www.sz.gov.cn/verified-ticket")</script>'
        else:
            html = '<p>深圳测试景区成人票88元。</p>'
        return httpx.Response(200, text=html, request=httpx.Request('GET', url))
    monkeypatch.setattr(httpx, 'get', get)
    monkeypatch.setattr(ticket_web, '_public', lambda url: None)
    from time import monotonic
    result = ticket_web.lookup(req.city, plan['days'][0]['attractions'][0], req.start_date, monotonic()+10)
    assert result['amount'] == 88
    assert len(result['http_calls']) == len(calls) == 4
    assert not any('license' in url for url in calls)


def test_true_internal_zero_is_not_a_cache_miss(enabled, monkeypatch):
    req, plan = trip()
    record = evidence()
    price_catalog.import_prices([{'city': req.city, 'kind': 'attraction', 'poi_id': 'A', 'amount': 0,
        'unit': 'admission', 'status': 'known', 'source_url': record['url'],
        'observed_at': record['observed_at'], 'expires_at': record['expires_at'],
        'service_from': req.start_date, 'service_to': req.start_date}])
    monkeypatch.setattr(ticket_web, 'lookup', lambda *a: pytest.fail('zero quote must not search'))
    result = ticket_pricing.refresh_plan_tickets(plan, req)
    assert result['days'][0]['attractions'][0]['ticket_price'] == 0


def test_queries_share_limit_across_graph_replans(enabled, settings, monkeypatch):
    req, plan = trip()
    plan['days'][0]['attractions'].append(make_attraction('第二个景区', poi_id='B', ticket_price=None).model_dump())
    monkeypatch.setattr(settings, 'ticket_web_query_limit', 1)
    calls, state = [], {}
    monkeypatch.setattr(ticket_web, 'lookup', lambda *a: calls.append(a) or {'status': 'WEB_SEARCH_NO_VERIFIED_SOURCE'})
    result = ticket_pricing.refresh_plan_tickets(plan, req, lookup_state=state)
    assert len(calls) == 1
    assert 'WEB_QUERY_BUDGET' in result['days'][0]['attractions'][1]['ticket_price_note']
    ticket_pricing.refresh_plan_tickets(plan, req, lookup_state=state)
    assert len(calls) == 1


def test_private_dns_address_is_blocked(monkeypatch):
    monkeypatch.setattr(ticket_web.socket, 'getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(ValueError, match='UNSAFE_SOURCE_ADDRESS'):
        ticket_web._public('https://www.sz.gov.cn/tickets')


@pytest.mark.parametrize('days', [-1, 365])
def test_current_web_policy_not_applied_to_past_or_far_future(days):
    page = ticket_web.Page()
    page.feed('<p>深圳测试景区成人票88元。</p>')
    target = (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()
    with pytest.raises(ValueError, match='OUTSIDE_POLICY_WINDOW'):
        ticket_web.extract_policy(page, '测试景区', '深圳', target)
