"""Regression for malls and bars incorrectly assigned 100-yuan tickets."""
import copy

import pytest

from app.agents.budget import patch_budget
from app.agents.verify_budget import verify_budget
from app.models.schemas import DayPlan
from app.agents.planner_agent import _enrich
from app.services.admission_policy import apply_admission_policy
from app.services import ticket_pricing
from tests.factories import make_attraction, make_day, make_request


@pytest.mark.parametrize('name,code,category', [
    ('领展中心城','060101','购物中心'),
    ('深圳One Avenue·卓悦中心','060101','购物中心'),
    ('中纺中心服装城','060102','普通商场'),
    ('清河坊步行街','061001','步行街'),
    ('某购物中心',None,'购物中心'),
])
def test_ordinary_shopping_has_no_admission_reserve_but_not_an_official_free_quote(name, code, category):
    spot = make_attraction(name, typecode=code, category=category, ticket_price=100,
                           ticket_price_status='estimated', ticket_price_source='static_estimate').model_dump()
    before = copy.deepcopy(spot)
    updated = apply_admission_policy(spot)
    assert updated['ticket_price'] == 0
    assert updated['ticket_price_source'] == 'venue_policy' and updated['ticket_price_status'] == 'estimated'
    assert updated['admission_policy'] == 'ordinary_access'
    assert '消费另计' in updated['ticket_price_note'] and '非官网' in updated['ticket_price_note']
    assert spot == before and apply_admission_policy(updated) == updated


@pytest.mark.parametrize('name,code,category', [
    ('购物中心付费展览','060101','购物中心'),
    ('购物中心海洋馆','080300','水族馆'),
    ('商场里的博物馆','140100','博物馆'),
    ('混合景区购物街','110200|061000','特色商业街'),
    ('名称像商场的收费景区','110200','风景名胜'),
])
def test_name_alone_or_mixed_categories_cannot_make_a_paid_activity_free(name, code, category):
    spot = make_attraction(name, typecode=code, category=category, ticket_price=None).model_dump()
    assert apply_admission_policy(spot)['ticket_price'] is None


def test_sourced_paid_event_at_mall_is_not_overridden_by_category_rule():
    spot = make_attraction('购物中心', typecode='060101', category='购物中心', ticket_price=88,
                           ticket_price_status='known', ticket_price_source='imported',
                           ticket_price_evidence_url='https://example.org/dated-event', admission_policy='ordinary_access').model_dump()
    updated = apply_admission_policy(spot)
    assert updated['ticket_price'] == 88 and updated['ticket_price_source'] == 'imported'
    assert updated['admission_policy'] == 'unknown'


def test_bar_spending_remains_unknown_instead_of_fabricated_admission(enabled):
    spot = make_attraction('SPARKING十八卿(深圳福田店)', typecode='080304', category='酒吧',
                           ticket_price=100, ticket_price_status='estimated', ticket_price_source='static_estimate')
    state = budget_state([spot])
    result = patch_budget(state)
    item = result['plan']['budget']['items'][0]
    assert item['amount'] is None and item['status'] == 'unknown'
    assert '最低消费' in item['note'] and '统一门票预留' in item['note']
    assert verify_budget({**state, **result})['budget_verdict']['status'] == 'unverified'


@pytest.fixture
def enabled(monkeypatch, settings):
    monkeypatch.setattr(settings, 'ticket_budget_reserve_enabled', True)
    monkeypatch.setattr(settings, 'official_ticket_lookup_enabled', True)


def budget_state(spots):
    return {'plan':{'days':[make_day(0,'2026-10-19',spots, meals=0)]},
            'request':make_request(city='深圳',start_date='2026-10-19',travel_days=1,nights=0,budget_limit=1000),
            'travel_days':1, 'nights':0}


def test_budget_removes_only_inapplicable_reserve_and_reconciles_all_totals(enabled):
    mall = make_attraction('领展中心城',typecode='060101',ticket_price=100,ticket_price_status='estimated',ticket_price_source='static_estimate')
    uncertain = make_attraction('真实待查景区',ticket_price=None,ticket_price_note='[OFFICIAL_SOURCE_UNSUPPORTED] 未接入该官网。')
    paid = make_attraction('官方票价景点',ticket_price=80,ticket_price_status='known',ticket_price_source='imported')
    state = budget_state([mall, uncertain, paid])
    before = copy.deepcopy(state['plan'])
    updated = patch_budget(state)['plan']
    assert [a['ticket_price'] for a in updated['days'][0]['attractions']] == [0,100,80]
    assert updated['budget']['total_attractions'] == 180
    assert updated['budget']['total'] == sum(i['amount'] for i in updated['budget']['items'] if i['amount'] is not None)
    assert updated['budget']['total'] == sum(d['day_total_cost'] for d in updated['days'])
    assert '[OFFICIAL_SOURCE_UNSUPPORTED]' in updated['days'][0]['attractions'][1]['ticket_price_note']
    assert patch_budget({**state,'plan':updated})['plan'] == updated
    assert state['plan'] == before


def test_official_lookup_does_not_fetch_malls_or_consumption_venues(enabled, monkeypatch):
    state = budget_state([make_attraction('领展中心城',typecode='060101',ticket_price=None),
                          make_attraction('普通酒吧',typecode='080304',ticket_price=None)])
    monkeypatch.setattr(ticket_pricing, '_load', lambda *a: pytest.fail('no admission source needed'))
    updated = ticket_pricing.refresh_plan_tickets(state['plan'], state['request'])
    assert [a['ticket_price'] for a in updated['days'][0]['attractions']] == [0,None]


def test_unsupported_official_source_has_specific_reason_not_fake_site_failure(enabled):
    state = budget_state([make_attraction('真实待查景区',ticket_price=None)])
    updated = ticket_pricing.refresh_plan_tickets(state['plan'], state['request'])
    note = updated['days'][0]['attractions'][0]['ticket_price_note']
    assert 'OFFICIAL_SOURCE_UNSUPPORTED' in note and '尚未接入' in note
    assert '拒绝访问' not in note


def test_planner_cannot_invent_a_shopping_type_to_avoid_a_paid_ticket():
    source = make_attraction('真实景点',typecode='110200',category='风景名胜',ticket_price=None)
    day = DayPlan.model_validate(make_day(0,'2026-10-19',[source]))
    day.attractions[0].typecode = '060101'
    day.attractions[0].category = '购物中心'
    _enrich([day],[source])
    assert day.attractions[0].typecode == '110200' and day.attractions[0].category == '风景名胜'


def test_old_zero_policy_is_removed_when_category_changes():
    spot = make_attraction('改为收费项目',typecode='110200',ticket_price=0,ticket_price_status='estimated',
                           ticket_price_source='venue_policy',admission_policy='ordinary_access').model_dump()
    updated = apply_admission_policy(spot)
    assert updated['ticket_price'] is None and updated['admission_policy'] == 'unknown'
