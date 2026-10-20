from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app.models.travel import china_today
from app.models.schemas import Attraction
from app.services import flyai, flyai_tickets, ticket_pricing
from app.services.tool_result import ToolResult
from tests.factories import make_attraction, make_day as factory_day, make_request


def make_day(index, target, spots):
    return factory_day(index, target, [Attraction.model_validate(s) for s in spots])


def fixture_data():
    target = str(china_today()+timedelta(days=7))
    spot = make_attraction('故宫博物院', poi_id='A001', address='北京市东城区景山前街4号', ticket_price=None).model_dump()
    offer = {'name': spot['name'], 'address': spot['address'], 'provider_id': '64', 'product_id': 'product123',
        'ticket_name': '大门票 成人票', 'price_date': target, 'amount': 60.0, 'url': 'https://www.fliggy.com/ticket',
        'observed_at': datetime.now(timezone.utc).isoformat()}
    return target, spot, offer


@pytest.mark.parametrize('changes', [
    {'name': '故宫珍宝馆'}, {'name': '故宫'}, {'price_date': None}, {'price_date': '2000-01-01'},
    {'ticket_name': '大门票 儿童票'}, {'ticket_name': '大门票 成人票 含讲解'}, {'ticket_name': '成人特惠票'},
    {'ticket_name': '成人联票'}, {'ticket_name': '大门票 成人票起'}, {'amount': None}, {'amount': 0},
    {'amount': -1}, {'amount': True}, {'amount': float('nan')}, {'amount': float('inf')},
    {'product_id': ''}, {'provider_id': ''}, {'url': None}, {'address': '另一个地点'}, {'address': ''}])
def test_ambiguous_child_discount_wrong_date_or_missing_price_never_becomes_parent_admission(changes):
    target, spot, offer = fixture_data()
    assert flyai_tickets.match_admission(spot, target, [{**offer, **changes}]) is None


def test_unique_exact_dated_adult_product_only():
    target, spot, offer = fixture_data()
    assert flyai_tickets.match_admission(spot, target, [offer]) == offer
    assert flyai_tickets.match_admission(spot, target, [offer, deepcopy(offer)]) is None
    assert flyai_tickets.match_admission({**spot, 'address': ''}, target, [offer]) is None


@pytest.fixture
def enabled(settings, monkeypatch):
    monkeypatch.setattr(settings, 'official_ticket_lookup_enabled', False)
    monkeypatch.setattr(settings, 'flyai_ticket_query_limit', 2)
    monkeypatch.setattr(settings, 'flyai_ticket_budget_seconds', 5)
    monkeypatch.setattr(flyai, 'ready', lambda: True)


def test_official_miss_platform_dated_reference_not_known_price_and_replan_dedup(enabled, monkeypatch):
    target, spot, offer = fixture_data()
    req = make_request(city='北京', start_date=target, travel_days=1, nights=0)
    calls = []
    def query(*args, **kwargs):
        calls.append(args)
        return {'outcome': ToolResult.success('ok').to_dict(), 'items': [offer]}
    monkeypatch.setattr(flyai, 'query', query)
    runtime = {}
    plan = {'days': [make_day(0, target, [spot])]}
    r = ticket_pricing.refresh_plan_tickets(plan, req, lookup_state=runtime)
    priced = r['days'][0]['attractions'][0]
    assert priced['ticket_price'] == 60 and priced['ticket_price_status'] == 'estimated'
    assert '非日常挂牌价' in priced['ticket_price_note'] and 'product123' in priced['ticket_price_note']
    assert runtime['_ticket_search_calls'][0]['tool'] == 'search_flyai_tickets'
    assert len(runtime['_ticket_search_calls'][0]['attempts']) == 1
    ticket_pricing.refresh_plan_tickets(plan, req, lookup_state=runtime)
    assert len(calls) == 1


def test_child_product_preserved_in_trace_and_does_not_fill_parent_price(enabled, monkeypatch):
    target, spot, offer = fixture_data()
    offer.update(name='故宫珍宝馆', provider_id='14294562', amount=16.8, price_date=None)
    monkeypatch.setattr(flyai, 'query', lambda *a, **kw: {'outcome': ToolResult.success('ok').to_dict(), 'items': [offer]})
    req = make_request(city='北京', start_date=target, travel_days=1, nights=0)
    runtime = {}
    r = ticket_pricing.refresh_plan_tickets({'days': [make_day(0, target, [spot])]}, req, lookup_state=runtime)
    priced = r['days'][0]['attractions'][0]
    assert priced['ticket_price'] is None and 'FLYAI_PRODUCT_SCOPE_UNCONFIRMED' in priced['ticket_price_note']
    assert runtime['_ticket_search_calls'][0]['evidence']['offers'][0]['amount'] == 16.8


def test_failure_keeps_reason_without_retry_same_poi(enabled, monkeypatch):
    target, spot, _ = fixture_data()
    calls = []
    def query(*args, **kwargs):
        calls.append(args)
        return {'outcome': ToolResult.failure('query failed', 'FLYAI_TIMEOUT').to_dict(), 'items': []}
    monkeypatch.setattr(flyai, 'query', query)
    req = make_request(city='北京', start_date=target, travel_days=1, nights=0)
    runtime, plan = {}, {'days': [make_day(0, target, [spot])]}
    for _ in range(2):
        r = ticket_pricing.refresh_plan_tickets(plan, req, lookup_state=runtime)
        assert 'FLYAI_TIMEOUT' in r['days'][0]['attractions'][0]['ticket_price_note']
    assert len(calls) == 1


def test_per_run_query_limit_and_partial_day_edit(enabled, monkeypatch):
    target, spot, _ = fixture_data()
    calls = []
    monkeypatch.setattr(flyai, 'query', lambda *a, **kw: calls.append(a) or {'outcome': ToolResult.success('ok').to_dict(), 'items': []})
    req = make_request(city='北京', start_date=target, travel_days=2, nights=1)
    later = str(china_today()+timedelta(days=8))
    spots = [{**spot, 'name': '地点'+str(i), 'poi_id': 'A'+str(i)} for i in range(5)]
    plan = {'days': [make_day(0, target, [spot]), make_day(1, later, spots)]}
    runtime = {}
    result = flyai_tickets.refresh(deepcopy(plan), req, {1}, runtime)
    assert len(calls) == 2 and runtime['flyai_ticket_queries'] == 2
    assert result['days'][0] == plan['days'][0]
    flyai_tickets.refresh(deepcopy(plan), req, {1}, runtime)
    assert len(calls) == 2


def test_existing_reliable_price_does_not_query_platform(enabled, monkeypatch):
    target, spot, _ = fixture_data()
    spot.update(ticket_price=60, ticket_price_status='known', ticket_price_source='amap')
    monkeypatch.setattr(flyai, 'query', lambda *a, **kw: pytest.fail('Should not send paid query'))
    req = make_request(city='北京', start_date=target, travel_days=1, nights=0)
    flyai_tickets.refresh({'days': [make_day(0, target, [spot])]}, req, None, {})


def test_persist_failure_degrades_without_aborting_plan(enabled, monkeypatch):
    target, spot, offer = fixture_data()
    monkeypatch.setattr(flyai, 'query', lambda *a, **kw: {'outcome': ToolResult.success('ok').to_dict(), 'items': [offer]})
    def fail(*a):
        raise ValueError('Cannot safely persist quote')
    monkeypatch.setattr(flyai_tickets.price_catalog, 'import_prices', fail)
    req = make_request(city='北京', start_date=target, travel_days=1, nights=0)
    result = flyai_tickets.refresh({'days': [make_day(0, target, [spot])]}, req, None, {})
    priced = result['days'][0]['attractions'][0]
    assert priced['ticket_price'] is None and 'FLYAI_PRICE_PERSIST_ERROR' in priced['ticket_price_note']


def test_expired_checkpoint_quote_not_reapplied_or_automatically_requeried(enabled, monkeypatch):
    import json
    target, spot, offer = fixture_data()
    monkeypatch.setattr(flyai, 'query', lambda *a, **kw: pytest.fail('No duplicate paid query'))
    key = json.dumps(['北京', spot['poi_id'], spot['name'], spot['address'], target], ensure_ascii=False)
    runtime = {'flyai_ticket_outcomes': {key: {'fields': {'ticket_price': 60},
        'expires_at': (datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()}}}
    req = make_request(city='北京', start_date=target, travel_days=1, nights=0)
    result = flyai_tickets.refresh({'days': [make_day(0, target, [spot])]}, req, None, runtime)
    priced = result['days'][0]['attractions'][0]
    assert priced['ticket_price'] is None and 'FLYAI_QUOTE_EXPIRED' in priced['ticket_price_note']
