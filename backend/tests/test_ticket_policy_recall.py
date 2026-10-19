from datetime import datetime, timezone
import threading

import pytest

from app.services import ticket_pricing, ticket_web
from tests.factories import make_attraction
from tests.test_ticket_web import trip


def parse(html, name='杭州西湖风景名胜区-曲院风荷'):
    page = ticket_web.Page()
    page.feed(html)
    return ticket_web.extract_policy(page, name, '杭州', datetime.now(timezone.utc).date().isoformat())


@pytest.mark.parametrize(('html','price'), [
    ('<h1>杭州曲院风荷游览指南</h1><p>门票：免费</p>',0),
    ('<p>杭州曲院风荷免费开放，儿童需监护人陪同。</p>',0),
    ('<h1>杭州曲院风荷</h1><table><tr><td>门票价格</td><td>30元/人</td></tr></table>',30),
    ('<p>杭州曲院风荷成人票30元，儿童票15元。</p>',30),
    ('<h1>杭州曲院风荷</h1><p>全价票：30元/人</p><footer>Copyright 2015</footer>',30),
    ('<p>杭州曲院风荷票价30元/人。</p><p>发展历史始于2003年</p>',30),
])
def test_realistic_alias_free_tariff_table_and_footers(html, price):
    assert parse(html)['amount'] == price


@pytest.mark.parametrize('html', [
    '<p>杭州曲院风荷儿童票15元。</p>',
    '<p>杭州曲院风荷门票30元起。</p>',
    '<p>杭州曲院风荷不免费开放。</p>',
    '<p>杭州曲院风荷限时免费开放。</p>',
    '<p>杭州曲院风荷门票30元至60元。</p>',
    '<p>杭州曲院风荷免费预约，门票另购。</p>',
    '<h1>杭州曲院风荷</h1><p>另一景区成人票30元。</p>',
    '<h1>杭州西湖景区</h1><p>成人票30元。</p>',
    '<h1>杭州曲院风荷</h1><p>另一景区</p><p>门票30元。</p>',
])
def test_alias_does_not_borrow_parent_child_offer_or_another_venue_price(html):
    with pytest.raises(ValueError):
        parse(html)


def test_season_requires_explicit_dates_and_chooses_target_policy():
    today = datetime.now(timezone.utc).date()
    value = parse('<h1>杭州曲院风荷</h1><p>旺季(4月1日至10月31日)成人票60元</p>'
                  '<p>淡季(11月1日至3月31日)成人票40元</p>')
    assert value['amount'] == (60 if 4 <= today.month <= 10 else 40)


def test_child_name_with_administrative_scenic_suffix_matches_actual_venue():
    assert parse('<h1>杭州城隍阁</h1><p>门票价格：30元/人</p>', '吴山景区-城隍阁景区')['amount'] == 30


def test_generic_official_service_heading_can_be_fetched_but_unrelated_heading_is_skipped():
    page=ticket_web.Page()
    page.feed('<h2><a href="https://www.hangzhou.gov.cn/ticket">门票价格</a></h2>'
              '<h2><a href="https://www.hangzhou.gov.cn/news">交通新闻</a></h2>')
    assert ticket_web.result_links(page,'曲院风荷')==['https://www.hangzhou.gov.cn/ticket']


def test_batch_gives_multiple_pois_a_search_opportunity(settings, monkeypatch):
    req, plan = trip()
    plan['days'][0]['attractions'] = [make_attraction(f'景区{i}',poi_id=f'B{i}',ticket_price=None).model_dump() for i in range(6)]
    monkeypatch.setattr(settings,'official_ticket_lookup_enabled',False)
    monkeypatch.setattr(settings,'ticket_web_search_enabled',True)
    monkeypatch.setattr(settings,'ticket_web_query_limit',6)
    barrier = threading.Barrier(3, timeout=3)
    calls = []
    def lookup(city, spot, target, deadline):
        calls.append(spot['poi_id'])
        barrier.wait()
        return {'status':'WEB_SEARCH_NO_VERIFIED_SOURCE'}
    monkeypatch.setattr(ticket_web,'lookup',lookup)
    state = {}
    result = ticket_pricing.refresh_plan_tickets(plan,req,lookup_state=state)
    assert len(calls) == 6 and len(state['ticket_lookup_queries']) == 6
    assert all('WEB_SEARCH_NO_VERIFIED_SOURCE' in a['ticket_price_note'] for a in result['days'][0]['attractions'])
    assert all(c['attempt_count']==1 for c in state['_ticket_search_calls'])
    ticket_pricing.refresh_plan_tickets(plan,req,lookup_state=state)
    assert len(calls)==6
