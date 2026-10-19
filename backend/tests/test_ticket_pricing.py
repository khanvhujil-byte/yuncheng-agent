import copy
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from app.agents.budget import patch_budget
from app.agents.verify_budget import verify_budget
from app.services import catalog, ticket_pricing as pricing
from tests.factories import make_attraction, make_day, make_request

PALACE = '<p>每年4月1日至10月31日为旺季，大门票60元/人；</p><p>每年11月1日至次年3月31日为淡季，大门票40元/人；</p><p>珍宝馆门票：10元/人；儿童免费</p>'


@pytest.fixture
def enabled(monkeypatch, settings):
    monkeypatch.setattr(settings, 'official_ticket_lookup_enabled', True)
    monkeypatch.setattr(settings, 'ticket_budget_reserve_enabled', True)
    monkeypatch.setattr(settings, 'ticket_budget_reserve_amount', 100)


def trip(name='故宫博物院', city='北京'):
    today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).date().isoformat()
    req = make_request(city=city, start_date=today, travel_days=1, nights=0, budget_limit=1000, must_visit=[])
    spot = make_attraction(name, poi_id='DPM123', ticket_price=None)
    day = make_day(0, today, [spot], meals=0)
    return req, {'city': city, 'days': [day]}


def response(html=PALACE, status=200):
    return httpx.Response(status, text=html, request=httpx.Request('GET', pricing.SOURCES[0]['url']))


def assert_unknown_reason(updated, original, code):
    assert code in updated['days'][0]['attractions'][0]['ticket_price_note']
    restored = copy.deepcopy(updated)
    restored['days'][0]['attractions'][0]['ticket_price_note'] = original['days'][0]['attractions'][0]['ticket_price_note']
    assert restored == original


def test_palace_extracts_adult_seasons_not_exhibition_or_child_price():
    policies = pricing.parse_policy(pricing.SOURCES[0], PALACE)
    assert [p['amount'] for p in policies] == [60, 40]
    assert 10 not in [p['amount'] for p in policies]
    assert 4 in policies[0]['months'] and 11 in policies[1]['months']


@pytest.mark.parametrize('html', ['学生票20元/人 儿童免费', '<script>成人票55元/人</script>', '成人票55元/人 成人票60元/人'])
def test_missing_or_ambiguous_adult_price_is_rejected(html):
    with pytest.raises(ValueError):
        pricing.parse_policy(pricing.SOURCES[1], html)


def test_official_lookup_retains_evidence_and_cached_observation_time(enabled, monkeypatch):
    req, plan = trip()
    before = copy.deepcopy(plan)
    calls = []
    monkeypatch.setattr(httpx, 'get', lambda *a, **kw: calls.append(kw) or response())
    updated = pricing.refresh_plan_tickets(plan, req)
    again = pricing.refresh_plan_tickets(plan, req)
    spot = updated['days'][0]['attractions'][0]
    assert spot['ticket_price'] == (60 if req.start_date[5:7] in ('04','05','06','07','08','09','10') else 40)
    assert spot['ticket_price_status'] == 'known'
    assert spot['ticket_price_evidence_url'] == pricing.SOURCES[0]['url']
    assert len(calls) == 1 and calls[0]['follow_redirects'] is False
    assert updated == again and plan == before
    with catalog._connection() as db:
        row = db.execute('SELECT payload_json FROM price_observation').fetchone()
        quote = json.loads(row[0])
        assert quote['service_from'] == quote['service_to'] == req.start_date
        assert '原文' in quote['note']


def test_failed_official_page_is_cooled_down_and_budget_reserves_instead(enabled, monkeypatch):
    req, plan = trip()
    calls = []
    monkeypatch.setattr(httpx, 'get', lambda *a, **kw: calls.append(1) or response('Forbidden', 403))
    failed = pricing.refresh_plan_tickets(plan, req)
    assert_unknown_reason(failed, plan, 'HTTP_403')
    assert pricing.refresh_plan_tickets(plan, req) == failed
    assert len(calls) == 1
    updated = patch_budget({'plan': failed, 'request': req, 'travel_days': 1, 'nights': 0})['plan']
    assert updated['budget']['total_attractions'] == 100
    assert updated['budget']['items'][0]['source'] == 'static_estimate'
    assert '不是景点报价' in updated['days'][0]['attractions'][0]['ticket_price_note']
    assert 'HTTP_403' in updated['days'][0]['attractions'][0]['ticket_price_note']


def test_exact_city_and_name_only_no_guessing_sub_attraction_or_homonym(enabled, monkeypatch):
    monkeypatch.setattr(httpx, 'get', lambda *a, **kw: pytest.fail('no matching official source'))
    for name, city in [('故宫珍宝馆', '北京'), ('故宫博物院', '南京'), ('故宫博物院停车场', '北京')]:
        req, plan = trip(name, city)
        assert_unknown_reason(pricing.refresh_plan_tickets(plan, req), plan, 'OFFICIAL_SOURCE_UNSUPPORTED')


def test_far_future_policy_is_not_treated_as_confirmed_quote(enabled, monkeypatch):
    monkeypatch.setattr(httpx, 'get', lambda *a, **kw: pytest.fail('far future quote must not fetch'))
    req, plan = trip()
    target = (datetime.now(timezone.utc).date() + timedelta(days=365)).isoformat()
    plan['days'][0]['date'] = target
    assert_unknown_reason(pricing.refresh_plan_tickets(plan, req), plan, 'OUTSIDE_POLICY_WINDOW')


def test_budget_reserve_counts_as_estimate_and_is_idempotent_without_mutating_input(enabled):
    req, plan = trip()
    before = copy.deepcopy(plan)
    state = {'plan': plan, 'request': req, 'travel_days': 1, 'nights': 0}
    result = patch_budget(state)['plan']
    assert result['budget']['total_attractions'] == 100
    assert result['budget']['total'] == sum(i['amount'] for i in result['budget']['items'] if i['amount'] is not None)
    assert result['budget']['estimated_total'] >= 100
    assert patch_budget({**state, 'plan': result})['plan']['budget'] == result['budget']
    assert plan == before
    limited = req.model_copy(update={'budget_limit': 50})
    assert verify_budget({**state, 'plan': result, 'request': limited})['budget_verdict']['status'] == 'estimated_exceeded'


def test_zero_unknown_gets_reserve_but_confirmed_free_and_official_prices_survive(enabled):
    req, plan = trip()
    plan['days'][0]['attractions'] = [
        make_attraction('未核实零元', ticket_price=0, ticket_price_status='unknown').model_dump(),
        make_attraction('确认免费', ticket_price=0, ticket_price_status='known', ticket_price_source='imported').model_dump(),
        make_attraction('官方成人票', ticket_price=60, ticket_price_status='known', ticket_price_source='imported').model_dump(),
    ]
    result = patch_budget({'plan':plan, 'request':req, 'travel_days':1, 'nights':0})['plan']
    assert [a['ticket_price'] for a in result['days'][0]['attractions']] == [100, 0, 60]
    assert result['budget']['total_attractions'] == 160


def test_database_failure_retains_unknown_identity_then_reserves(enabled, monkeypatch):
    req, plan = trip()
    monkeypatch.setattr(pricing, '_load', lambda *a: (_ for _ in ()).throw(OSError('DB unavailable')))
    assert_unknown_reason(pricing.refresh_plan_tickets(plan, req), plan, 'OFFICIAL_LOOKUP_ERROR')


def test_expired_official_page_refreshes_without_serving_old_policy(enabled, monkeypatch):
    req, plan = trip()
    monkeypatch.setattr(httpx, 'get', lambda *a, **kw: response())
    pricing.refresh_plan_tickets(plan, req)
    with catalog._connection() as db:
        db.execute('UPDATE official_ticket_page SET expires_at=?', ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
        db.execute('UPDATE price_observation SET expires_at=?', ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
    monkeypatch.setattr(httpx, 'get', lambda *a, **kw: response('<p>page changed</p>'))
    assert_unknown_reason(pricing.refresh_plan_tickets(plan, req), plan, 'OFFICIAL_POLICY_UNRESOLVED')
