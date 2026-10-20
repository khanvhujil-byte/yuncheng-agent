"""Factual travel decisions, dated room quotes and party budget regressions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from app.agents.budget import patch_budget
from app.models.schemas import Hotel, Location
from app.models.travel import china_today
from app.services import intercity, platform_hotels, planner_patch, store, unified_planning
from tests.factories import make_attraction, make_day, make_hotel, make_request
from tests.joint_fixtures import restaurants_at


def request(**kwargs):
    return make_request(start_date=(china_today()+timedelta(days=7)).isoformat(), must_visit=[],
                        origin_city='上海', intercity_mode='train', **kwargs)


def row(req, direction='outbound', amount=100, dep='08:00', arr='10:00'):
    day = china_today()+timedelta(days=7+(req.travel_days-1 if direction == 'return' else 0))
    return dict(amount=amount, observed_at=datetime.now(timezone.utc).isoformat(), url='https://www.fliggy.com/',
        segments=[dict(depCityName=req.origin_city if direction == 'outbound' else req.city,
            arrCityName=req.city if direction == 'outbound' else req.origin_city,
            depDateTime=f'{day}T{dep}:00', arrDateTime=f'{day}T{arr}:00',
            depStationName='上海虹桥' if direction == 'outbound' else '杭州东',
            arrStationName='杭州东' if direction == 'outbound' else '上海虹桥',
            marketingTransportNo='G123', seatClassName='二等座')])


def state_with_offers(monkeypatch, req=None):
    req = req or request()
    pools = {d: [intercity._offer(row(req, d, dep='18:00' if d == 'return' else '08:00',
                           arr='20:00' if d == 'return' else '10:00'), req, d, 'trains')]
             for d in ['outbound', *(['return'] if req.return_trip else [])]}
    endpoint = dict(name='杭州东', poi_id='STATION', location=dict(longitude=120.2, latitude=30.2))
    monkeypatch.setattr(intercity, '_endpoint', lambda *_: deepcopy(endpoint))
    return dict(request=req, transport_candidates=pools)


@pytest.mark.parametrize('failure', ['transfer', 'wrong_city', 'wrong_date', 'backwards', 'missing_station', 'overnight'])
def test_invalid_transport_facts_are_not_candidates(failure):
    req = request()
    r = row(req)
    s = r['segments'][0]
    if failure == 'transfer': r['segments'].append(deepcopy(s))
    if failure == 'wrong_city': s['arrCityName'] = '北京'
    if failure == 'wrong_date': s['depDateTime'] = '2020-01-01T08:00:00'
    if failure == 'backwards': s['arrDateTime'] = s['depDateTime']
    if failure == 'missing_station': s['arrStationName'] = ''
    if failure == 'overnight': s['arrDateTime'] = f'{china_today()+timedelta(days=8)}T06:00:00'
    assert intercity._offer(r, req, 'outbound', 'trains') is None


@pytest.mark.parametrize('amount', [None, -1, 0, True, float('nan'), float('inf'), '100'])
def test_bad_fares_remain_unknown_not_free(amount):
    req = request()
    offer = intercity._offer(row(req, amount=amount), req, 'outbound', 'trains')
    assert offer['amount_per_adult'] is None and offer['price_status'] == 'unknown'


def test_binding_copies_facts_caches_endpoints_and_rejects_foreign_id(monkeypatch):
    state = state_with_offers(monkeypatch)
    calls = []
    monkeypatch.setattr(intercity, '_endpoint', lambda *a: calls.append(a) or dict(name='车站', poi_id='S', location={'longitude':120, 'latitude':30}))
    payload = {'transport_choices': {d: pool[0]['offer_id'] for d, pool in state['transport_candidates'].items()}}
    legs = intercity.bind(payload, state)
    assert not intercity.audit({'intercity_legs':legs}, state['request'], state)
    second = intercity.bind(payload, state)
    assert not any(k.startswith('_') for leg in second for k in leg)
    assert len(calls) == 2
    legs[0]['amount_per_adult'] = 1
    assert any('票价' in e for e in intercity.audit({'intercity_legs':legs}, state['request'], state))
    payload['transport_choices']['outbound'] = 'fake'
    with pytest.raises(ValueError): intercity.bind(payload, state)


@pytest.mark.parametrize('field,value', [('seat_class','商务座'), ('boarding_buffer_minutes',0), ('exit_buffer_minutes',0),
                                        ('duration_minutes',1), ('local_endpoint',{'poi_id':'FAKE'})])
def test_final_audit_detects_modified_transport_facts(monkeypatch, field, value):
    state = state_with_offers(monkeypatch)
    legs = intercity.bind({'transport_choices':{d:p[0]['offer_id'] for d,p in state['transport_candidates'].items()}},state)
    legs[0][field] = value
    assert intercity.audit({'intercity_legs':legs}, state['request'], state)


@pytest.mark.parametrize('legs', [['bad'], [None], 'invalid'])
def test_malformed_transport_reports_error_instead_of_crashing(legs):
    assert intercity.audit({'intercity_legs':legs}, request())


def test_one_day_clocks_meals_and_collision_are_code_derived(monkeypatch):
    state = state_with_offers(monkeypatch, request(travel_days=1))
    windows = intercity.time_windows(state)[0]
    assert windows['feasible']
    assert windows['daily_time_windows'][state['request'].start_date]['meal_types'] == ['lunch']
    state['transport_candidates']['return'][0]['departure_at'] = state['transport_candidates']['outbound'][0]['arrival_at']
    assert not intercity.time_windows(state)[0]['feasible']


def test_no_transport_accepts_null_schema_but_rejects_invention():
    state = dict(request=make_request())
    assert intercity.bind({'transport_choices':{'outbound':None,'return':None}},state) == []
    with pytest.raises(ValueError): intercity.bind({'transport_choices':{'outbound':'invented'}},state)


def test_collector_auto_queries_both_modes_and_never_repeats(monkeypatch):
    req = request().model_copy(update={'intercity_mode':'auto'})
    calls = []
    def query(kind, params, **kwargs):
        calls.append((kind, params))
        direction = 'outbound' if params['origin'] == req.origin_city else 'return'
        return dict(items=[row(req,direction)],outcome={'status':'ok','stats':{}})
    monkeypatch.setattr(intercity.flyai, 'query', query)
    result = intercity.collect({'request':req})
    assert len(calls) == 4 and len(result['transport_candidates']['outbound']) == 2
    assert intercity.collect({'request':req, **result}) == {} and len(calls) == 4


def hotel_quote(**changes):
    now = datetime.now(timezone.utc)
    day = (china_today()+timedelta(days=7)).isoformat()
    q = dict(amount=279.2,url='https://www.fliggy.com/',check_in=day,
             check_out=(china_today()+timedelta(days=9)).isoformat(),observed_at=now.isoformat(),
             expires_at=(now+timedelta(minutes=10)).isoformat())
    q.update(changes)
    return q


def hotel_with_quote(**changes):
    return Hotel(name='分店酒店',poi_id='H1',address='测试路123号',location=Location(longitude=120.1,latitude=30.2),
                 platform_quotes=[hotel_quote(**changes)])


@pytest.mark.parametrize('changes', [{'amount':float('nan')},{'url':'javascript:alert(1)'},
    {'expires_at':'2000-01-01T00:00:00+00:00'},{'expires_at':'bad'},{'check_out':'2000-01-01'}, {'amount':0}])
def test_invalid_room_quote_never_becomes_price(changes):
    h = hotel_with_quote(**changes)
    h.price_note = '飞猪旧参考'
    h.estimated_cost = 279
    result = platform_hotels.quoted(h, request().start_date)
    assert result.cost_status == 'unknown' and result.estimated_cost == 0


def test_room_quote_survives_stay_binding_and_refresh(monkeypatch):
    from app.services import stays, partial_edit
    req = request()
    h = hotel_with_quote()
    state = dict(request=req,hotels=[h],nights=2,accommodation_version=11)
    bound = stays.bind_stays({'stay_segments':[dict(check_in=req.start_date,check_out=hotel_quote()['check_out'],hotel_id='H1')]},state)
    assert [s['hotel']['estimated_cost'] for s in bound] == [280,280]
    plan = dict(stays=bound,hotel_nights=2,accommodation_version=11)
    partial_edit._refresh_stay_prices(plan,req)
    assert [s['hotel']['estimated_cost'] for s in plan['stays']] == [280,280]
    assert h.estimated_cost != 280  # Input candidate is not mutated.


@pytest.mark.parametrize('same_address', [True,False])
def test_hotel_identity_prevents_cross_branch_prices(monkeypatch, same_address):
    req = request()
    h = hotel_with_quote().model_copy(update={'platform_quotes':[]})
    offer = dict(name=h.name,address=h.address if same_address else '另一个地址9号',provider_id='F1',
                 amount=279.2,url='https://www.fliggy.com/',observed_at=datetime.now(timezone.utc).isoformat(),
                 check_in=req.start_date,check_out=hotel_quote()['check_out'])
    monkeypatch.setattr(platform_hotels.flyai,'ready',lambda:True)
    monkeypatch.setattr(platform_hotels.flyai,'query',lambda *a,**kw:dict(items=[offer],outcome={'status':'ok','stats':{}}))
    result = platform_hotels.collect(dict(request=req,hotels=[h],nights=2))
    assert bool(result['hotels'][0].platform_quotes) == same_address


@pytest.mark.parametrize('map_name,map_address,platform_name,platform_address,expected', [
    ('花田赞酒店(西湖中河北路地铁站店)','中河北路69号意盛商务大厦18楼','花田赞酒店(西湖武林凤起路店)','中河北路69号意盛商务大厦18层',True),
    ('我的家快捷酒店(九堡直街)','九堡镇杭海路1012号','杭州我的家快捷酒店','杭州上城区九堡镇杭海路1012号',True),
    ('杭州科尼的快捷酒店','顺达路51号(附近地标)','杭州科尼的快捷酒店','杭州余杭经济开发区顺达路51号',True),
    ('花田赞酒店(别店)','中河北路69号意盛商务大厦17层','花田赞酒店(西湖店)','中河北路69号意盛商务大厦18层',False),
    ('其他酒店','中河北路69号意盛商务大厦18层','花田赞酒店','中河北路69号意盛商务大厦18层',False),
    ('花田赞酒店','中河北路70号','花田赞酒店','中河北路69号',False),
])
def test_supplier_branch_alias_requires_matching_specific_address(map_name,map_address,platform_name,platform_address,expected):
    h=hotel_with_quote().model_copy(update={'name':map_name,'address':map_address})
    assert platform_hotels._same_property(h,dict(name=platform_name,address=platform_address),'杭州') is expected


def test_party_budget_reconciles_days_and_is_idempotent(monkeypatch):
    req = request(adult_count=3,room_count=2)
    state = state_with_offers(monkeypatch,req)
    legs = intercity.bind({'transport_choices':{d:p[0]['offer_id'] for d,p in state['transport_candidates'].items()}},state)
    h = make_hotel(price=200).model_copy(update={'cost_status':'known','cost_source':'imported'})
    days = [make_day(i,(china_today()+timedelta(days=7+i)).isoformat(),[make_attraction('景点',ticket_price=50,ticket_price_status='known')]) for i in range(3)]
    plan = dict(days=days, hotel_nights=2, intercity_legs=legs)
    state.update(plan=plan,hotel=h,travel_days=req.travel_days,nights=2)
    result = patch_budget(state)['plan']
    assert result['budget']['total_hotels'] == 800
    assert result['budget']['total_attractions'] == 450
    assert result['budget']['total_transportation'] == 960  # 3 days * 40 * 3 people + 2 fares * 100 * 3.
    assert sum(d['day_total_cost'] for d in result['days']) == result['budget']['total']
    assert patch_budget({**state,'plan':result})['plan']['budget'] == result['budget']
    assert plan['days'][0]['day_total_cost'] == 0


def test_missing_fare_expiry_and_flight_extras_never_pass_as_free(monkeypatch):
    req = request().model_copy(update={'intercity_mode':'flight'})
    leg = intercity._offer(row(req),req,'outbound','flights')
    leg['expires_at'] = '2000-01-01T00:00:00+00:00'
    plan = dict(days=[make_day(0,req.start_date,[])],hotel_nights=0,intercity_legs=[leg])
    result = patch_budget(dict(request=req,plan=plan,travel_days=req.travel_days))['plan']
    assert result['intercity_legs'][0]['amount_per_adult'] is None
    assert len([n for n in result['budget']['unknown_items'] if '城际' in n]) == 2
    assert any('税费' in n for n in result['budget']['unknown_items'])


def test_self_drive_reference_is_shared_instead_of_per_passenger():
    req=make_request(adult_count=3,room_count=1,transportation='自驾')
    result=patch_budget(dict(request=req,travel_days=3,nights=0,plan={'days':[make_day(0,req.start_date,[])]}))['plan']
    assert result['budget']['total_transportation']==360
    assert '一车' in next(i['note'] for i in result['budget']['items'] if i['category']=='transportation')


def test_room_quote_expiring_during_route_work_is_demoted_at_budget():
    req=make_request(start_date=request().start_date,travel_days=2)
    h=platform_hotels.quoted(hotel_with_quote(), req.start_date)
    h.platform_quotes[0]['expires_at']='2000-01-01T00:00:00+00:00'
    plan=dict(accommodation_version=11,hotel=h.model_dump(),stays=[dict(check_in=req.start_date,
        check_out=str(china_today()+timedelta(days=8)),hotel=h.model_dump(),locked=False)],
        days=[make_day(0,req.start_date,[])])
    result=patch_budget(dict(request=req,travel_days=2,nights=1,hotel=h,plan=plan))['plan']
    assert result['budget']['total_hotels']==0
    assert any('住宿' in n for n in result['budget']['unknown_items'])
    assert result['stays'][0]['hotel']['cost_status']=='unknown'
    assert plan['stays'][0]['hotel']['estimated_cost']==280


def test_meal_patch_retains_selected_transport_ids(monkeypatch):
    state = state_with_offers(monkeypatch)
    req = state['request']
    restaurants = restaurants_at(Location(longitude=120,latitude=30))
    legs = intercity.bind({'transport_choices':{d:p[0]['offer_id'] for d,p in state['transport_candidates'].items()}},state)
    plan = dict(accommodation_version=11,stays=[],days=[dict(day_index=0,date=req.start_date,attractions=[],
                meals=[dict(type='lunch',restaurant_id='J0')])],intercity_legs=legs)
    state.update(plan=plan,restaurants=restaurants,accommodation_version=11)
    result = planner_patch.expand({'meal_patches':[dict(day_index=0,type='lunch',restaurant_id='J1')]},state)
    assert result['transport_choices'] == {l['direction']:l['offer_id'] for l in legs}
    assert result['days'][0]['meals'][0]['restaurant_id'] == 'J1'


def test_joint_binding_uses_transport_window_not_three_meal_default(monkeypatch):
    req = request(travel_days=1,nights=0,return_trip=False)
    state = state_with_offers(monkeypatch,req)
    state['transport_candidates']['outbound'][0] = intercity._offer(row(req,dep='17:00',arr='19:00'),req,'outbound','trains')
    state.update(nights=0,accommodation_version=11,hotels=[],attractions=[],restaurants=restaurants_at(Location(longitude=120,latitude=30)))
    payload = dict(transport_choices={'outbound':state['transport_candidates']['outbound'][0]['offer_id'],'return':None},
        stays=[],days=[dict(date=req.start_date,day_index=0,attractions=[],meals=[dict(type='dinner',restaurant_id='J0')])])
    bound,_ = unified_planning.bind_choices(payload,state)
    assert bound['days'][0]['meal_types'] == ['dinner']
    assert req.arrival_time is None


def test_party_and_transport_are_cache_identity():
    req = request()
    base = store.fingerprint_of(req)
    for change in ({'origin_city':'北京'},{'adult_count':2},{'room_count':2},{'return_trip':False},{'intercity_mode':'flight'}):
        assert store.fingerprint_of(req.model_copy(update=change)) != base


def test_state_codec_keeps_transport_and_quote_facts(monkeypatch):
    from app.services.state_codec import restore_state_types
    state = state_with_offers(monkeypatch)
    state.update(hotels=[hotel_with_quote()],transport_candidate_key='signature',platform_hotel_key='hotel-signature')
    result = restore_state_types(json.loads(json.dumps(state, default=lambda v: v.model_dump(mode='json'))))
    assert result['transport_candidates'] == state['transport_candidates']
    assert result['hotels'][0].platform_quotes == state['hotels'][0].platform_quotes
