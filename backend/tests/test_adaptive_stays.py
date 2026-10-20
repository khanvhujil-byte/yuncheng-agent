"""Dated lodging invariants, cross-day routing and edit dependency regressions."""
from copy import deepcopy
from datetime import date, timedelta

from fastapi import HTTPException
import pytest

from app.models.dialog import ModuleEdit
from app.models.schemas import Hotel, Location
from app.services import stays
from tests.factories import make_request


@pytest.fixture
def setup_stays(monkeypatch):
    from app.services import price_catalog
    monkeypatch.setattr(price_catalog, 'apply_prices', lambda city, kind, rows, **kw: rows)
    req = make_request(start_date='2026-10-20', travel_days=3, must_visit=[], transportation='自驾')
    hotels = [Hotel(name='市区酒店', poi_id='CITY', location=Location(longitude=120, latitude=30), estimated_cost=200,
                    cost_status='known', cost_source='imported'),
              Hotel(name='远郊酒店', poi_id='REMOTE', location=Location(longitude=121, latitude=30), estimated_cost=300,
                    cost_status='known', cost_source='imported')]
    state = dict(request=req, nights=2, travel_days=3, accommodation_version=11, hotels=hotels)
    payload = dict(stays=[dict(check_in='2026-10-20', hotel_id='CITY'), dict(check_in='2026-10-21', hotel_id='REMOTE')])
    return req, state, payload


def plan_for(req, state, payload):
    plan = dict(city=req.city, start_date=req.start_date, end_date=(date.fromisoformat(req.start_date)+timedelta(days=req.travel_days-1)).isoformat(),
                accommodation_version=11, hotel_nights=state['nights'], stays=stays.bind_stays(payload, state),
                days=[dict(date=(date.fromisoformat(req.start_date)+timedelta(days=i)).isoformat(), day_index=i, attractions=[], meals=[])
                      for i in range(req.travel_days)])
    return stays.attach_boundaries(plan, req)


def test_cross_day_origins_and_distinct_destinations(setup_stays):
    req, state, payload = setup_stays
    plan = plan_for(req, state, payload)
    assert plan['days'][0]['route_start'] is None
    assert stays.identity(plan['days'][0]['route_end']) == 'CITY'
    assert stays.identity(plan['days'][1]['route_start']) == 'CITY'
    assert stays.identity(plan['days'][1]['route_end']) == 'REMOTE'
    assert stays.identity(plan['days'][2]['route_start']) == 'REMOTE'
    assert plan['days'][2]['route_end'] is None
    assert not stays.audit_stays(plan, req, state)


@pytest.mark.parametrize('bad', ['missing', 'duplicate', 'foreign_date', 'foreign_id', 'bad_type', 'empty'])
def test_invalid_nights_rejected_without_fact_invention(setup_stays, bad):
    _, state, payload = setup_stays
    if bad == 'missing': payload['stays'].pop()
    if bad == 'duplicate': payload['stays'][1]['check_in'] = payload['stays'][0]['check_in']
    if bad == 'foreign_date': payload['stays'][1]['check_in'] = '2026-10-23'
    if bad == 'foreign_id': payload['stays'][1]['hotel_id'] = 'MODEL_INVENTED'
    if bad == 'bad_type': payload['stays'][1] = 'REMOTE'
    if bad == 'empty': payload['stays'] = []
    with pytest.raises(ValueError): stays.bind_stays(payload, state)


def test_model_cannot_overwrite_price_coordinates_or_lock(setup_stays):
    _, state, payload = setup_stays
    payload['stays'][1].update(estimated_cost=1, hotel={'name':'假的','location':None}, locked=True)
    actual = stays.bind_stays(payload, state)[1]
    assert actual['hotel']['estimated_cost'] == 300 and actual['hotel']['location']['longitude'] == 121
    assert actual['locked'] is False


@pytest.mark.parametrize('policy', ['must_same', 'prefer_same', 'auto'])
def test_stay_policy_hard_vs_soft(setup_stays, policy):
    req, state, payload = setup_stays
    req.requirements.hotel.stay_policy = policy
    if policy == 'must_same':
        with pytest.raises(ValueError, match='连住'): stays.bind_stays(payload, state)
    else:
        assert len(stays.bind_stays(payload, state)) == 2


def test_date_lock_does_not_lock_other_nights(setup_stays):
    _, state, payload = setup_stays
    state['stay_locks'] = {'2026-10-21':state['hotels'][1].model_dump()}
    result = stays.bind_stays(payload, state)
    assert not result[0]['locked'] and result[1]['locked']
    payload['stays'][1]['hotel_id'] = 'CITY'
    with pytest.raises(ValueError, match='锁定'): stays.bind_stays(payload, state)


def test_fixed_hotel_is_all_night_lock(setup_stays):
    req, state, payload = setup_stays
    req.fixed_hotel = '市区酒店'
    state['hotel_anchor'] = state['hotels'][0]
    with pytest.raises(ValueError, match='锁定'): stays.bind_stays(payload, state)


def test_no_hotel_nights_and_final_extra_night(setup_stays):
    req, state, _ = setup_stays
    req.nights = 0
    assert stays.bind_stays({'stays':[]}, state) == []
    with pytest.raises(ValueError): stays.bind_stays({'stays':[{'check_in':req.start_date,'hotel_id':'CITY'}]},state)
    req.nights = 3
    rows = [{'check_in':d,'hotel_id':'CITY'} for d in stays.night_dates(req)]
    result = stays.bind_stays({'stays':rows},state)
    assert result[-1]['check_out'] == '2026-10-23'


def test_gap_in_overnights_is_not_certified(setup_stays):
    req, state, payload = setup_stays
    req.nights = state['nights'] = 1
    payload['stays'].pop()
    assert any('剩余夜晚' in e for e in stays.audit_stays(plan_for(req,state,payload), req, state))


def test_legacy_projection_does_not_mutate_saved_plan(setup_stays):
    req, state, _ = setup_stays
    old = dict(start_date=req.start_date, hotel_nights=2, hotel=state['hotels'][0].model_dump())
    before = deepcopy(old)
    rows = stays.plan_stays(old)
    assert len(rows) == 2 and old == before
    rows[0]['hotel']['name'] = 'modified'
    assert old == before
    assert stays.plan_stays({**old,'accommodation_version':11,'stays':[]}) == []


def test_daily_start_or_destination_corruption_is_caught(setup_stays):
    req, state, payload = setup_stays
    plan = plan_for(req,state,payload)
    plan['days'][2]['route_start'] = plan['days'][0]['route_end']
    assert any('route_start' in e for e in stays.audit_stays(plan, req, state))


def test_transfers_are_not_free_and_storage_is_not_assumed(setup_stays):
    req, state, payload = setup_stays
    plan = plan_for(req,state,payload)
    assert plan['days'][1]['transfer_minutes'] == 60
    req.requirements.hotel.luggage_policy = 'storage_required'
    assert any('寄存' in e for e in stays.audit_stays(plan,req,state))
    req.requirements.hotel.luggage_policy = 'carry'
    plan['days'][1]['transfer_minutes'] = 0
    assert any('换住时间' in e for e in stays.audit_stays(plan,req,state))


def test_estimated_remote_public_transit_not_success(setup_stays):
    req, state, payload = setup_stays
    req.transportation = '公共交通'
    plan = plan_for(req,state,payload)
    plan['days'][1]['route_legs'] = [dict(status='estimated',distance_meters=100000)]
    assert any('路线未核实' in e for e in stays.audit_stays(plan,req,state))


@pytest.mark.parametrize('nights', [1, 2, 7, 29, 30])
def test_date_coverage_across_month_and_long_trips(setup_stays, nights):
    req, state, _ = setup_stays
    req.travel_days, req.nights = 30, nights
    req.start_date = '2028-02-28'
    rows = stays.bind_stays({'stays':[dict(check_in=d,hotel_id='CITY') for d in stays.night_dates(req)]},state)
    assert len({s['check_in'] for s in rows}) == nights
    if nights >= 2: assert rows[1]['check_in'] == '2028-02-29'


def test_budget_sums_every_night_once_not_summary_hotel(setup_stays):
    from app.agents.budget import patch_budget
    req,state,payload = setup_stays
    plan = plan_for(req,state,payload)
    result = patch_budget({**state,'plan':plan,'hotel':state['hotels'][0]})['plan']
    assert result['budget']['total_hotels'] == 500
    assert len([i for i in result['budget']['items'] if i['category']=='hotel']) == 2
    assert sum(d['day_total_cost'] for d in result['days']) == result['budget']['total']


def test_unknown_night_blocks_confirmed_budget(setup_stays):
    from app.agents.budget import patch_budget
    from app.agents.verify_budget import verify_budget
    req,state,payload = setup_stays
    req.budget_limit = 99999
    plan = plan_for(req,state,payload)
    plan['stays'][1]['hotel']['cost_status'] = 'unknown'
    update = {**state,'plan':plan}
    update.update(patch_budget(update))
    assert verify_budget(update)['budget_verdict']['status'] == 'unverified'


def test_night_and_segment_edit_dependency_targets(setup_stays):
    from app.services.partial_edit import _target_stays
    req,state,payload = setup_stays
    payload['stays'][1]['hotel_id'] = 'CITY'
    plan = plan_for(req,state,payload)
    assert len(_target_stays(plan, ModuleEdit(module='hotel',day_index=1,stay_scope='night'))) == 1
    assert len(_target_stays(plan, ModuleEdit(module='hotel',day_index=1,stay_scope='segment'))) == 2
    plan['stays'][0]['locked'] = True
    assert len(_target_stays(plan, ModuleEdit(module='hotel',stay_scope='all'))) == 1
    with pytest.raises(HTTPException): _target_stays(plan, ModuleEdit(module='hotel',day_index=1,stay_scope='segment'))


def test_real_route_clock_includes_cross_day_transfer(setup_stays, monkeypatch):
    from app.services.itinerary_experience import _schedule_day
    from app.services.schedule_repair import simulate
    from app.services.route_batch import route_key
    req,state,payload = setup_stays
    plan = plan_for(req,state,payload)
    day = plan['days'][1]
    cache = {}
    def route(a,b,*args):
        return dict(duration_minutes=90, distance_meters=90000,status='known')
    monkeypatch.setattr('app.services.itinerary_experience._route', route)
    _schedule_day(day, [], None, req, cache, [0], [])
    assert day['route_legs'][0]['from_id']=='CITY' and day['route_legs'][-1]['to_id']=='REMOTE'
    assert day['total_minutes'] == 90+18+60
    assert sum(s['duration_minutes'] for s in day['schedule']) == day['total_minutes']
    assert simulate([], day['route_start'], lambda a,b:90, end_hotel=day['route_end'],transfer_minutes=60)['total_minutes'] == day['total_minutes']


def test_no_regional_queries_for_city_only(setup_stays, monkeypatch):
    from app.services.regional_stays import collect_regional_context
    req,state,_ = setup_stays
    state['attractions'] = []
    monkeypatch.setattr('app.services.map_gateway.search_hotels_nearby', lambda *a,**kw:pytest.fail('city itinerary must not do remote queries'))
    assert collect_regional_context(state)['regional_hotel_queries'] == []


def test_locks_and_endpoint_times_change_cache_fingerprint(setup_stays):
    from app.services.store import fingerprint_of
    req,_,_ = setup_stays
    other = req.model_copy(deep=True)
    other.arrival_time = '12:00'
    assert fingerprint_of(req) != fingerprint_of(other)
    other = req.model_copy(deep=True)
    other.requirements.hotel.stay_policy = 'must_same'
    assert fingerprint_of(req) != fingerprint_of(other)


@pytest.fixture
def adaptive_scenario(setup_stays, monkeypatch, settings):
    import json
    import math
    from app.agents import planner_agent
    from app.models.schemas import Restaurant
    from app.services import itinerary_experience
    from app.services.tools import haversine_km
    from tests.factories import make_attraction, make_weather
    req, state, payload = setup_stays
    monkeypatch.setattr(settings, 'food_coverage_enabled', False)
    monkeypatch.setattr(settings, 'official_ticket_lookup_enabled', False)
    city, remote = state['hotels']
    spots = [make_attraction(f'博物馆{i}', poi_id=f'A{i}', lng=(city if i == 0 else remote).location.longitude,
                             lat=(city if i == 0 else remote).location.latitude,
                             ticket_price=20, ticket_price_status='known', ticket_price_source='imported', visit_duration=90)
             for i in range(3)]
    restaurants = [Restaurant(name=f'豆浆面馆{i}', poi_id=f'R{i}', location=(city if i < 4 else remote).location,
                              avg_cost=30, avg_cost_status='estimated', cost_source='amap') for i in range(9)]
    payload['days'] = [dict(day_index=i, date=(date.fromisoformat(req.start_date)+timedelta(days=i)).isoformat(),
                            attractions=[{'poi_id':f'A{i}'}], meals=[dict(type=k, restaurant_id=f'R{i*3+j}')
                            for j,k in enumerate(('breakfast','lunch','dinner'))]) for i in range(3)]
    state.update(unified_planning=True, experience_enabled=True, attractions=spots, restaurants=restaurants,
                 end_date=payload['days'][-1]['date'], weather=[make_weather(d['date']) for d in payload['days']],
                 sources={'hotel':'amap','food':'amap','weather':'amap','attractions':'amap'},
                 must_visit_resolved=[], adcode='330100', route_cache={})
    monkeypatch.setattr(planner_agent, 'chat_json', lambda *a, **kw:(json.dumps(payload), {'llm_calls':1}))
    def route(a,b,**kw):
        km = haversine_km(a,b)
        return dict(duration_minutes=math.ceil(km/70*60),distance_meters=math.ceil(km*1200),status='known',source='amap')
    monkeypatch.setattr(itinerary_experience.amap, 'travel_route', route)
    update = planner_agent.compose_plan(state)
    assert not update['validate_error'], update['validate_error']
    state.update(update)
    plan, errors, warnings = itinerary_experience.enrich_experience(state['plan'], state)
    assert not errors, errors
    state.update(plan=plan, warnings=warnings)
    from app.agents.budget import patch_budget
    from app.agents.verify_budget import verify_budget
    state.update(patch_budget(state))
    state.update(verify_budget(state))
    return req, state


def test_joint_planner_two_stays_delivers_and_persists_all_hotels(adaptive_scenario):
    from app.services import planning, catalog
    req, state = adaptive_scenario
    result = planning._finalize(state, req, 'adaptive-integration', 'adaptive-integration', 1)
    assert result.success, result.message
    assert result.data.hotel is None
    assert [s.hotel.poi_id for s in result.data.stays] == ['CITY','REMOTE']
    assert result.data.budget.total_hotels == 500
    assert {r['poi_id'] for r in catalog.list_places(req.city, 'hotel')} == {'CITY','REMOTE'}
    assert result.data.days[1].route_start.poi_id == 'CITY'
    assert result.data.days[1].route_end.poi_id == 'REMOTE'
    assert result.data.days[2].route_start.poi_id == 'REMOTE'


def test_single_night_edit_reaudits_both_days_and_keeps_choices(adaptive_scenario):
    import time
    from app.models.schemas import TripPlanResponse
    from app.services import partial_edit, store
    req, state = adaptive_scenario
    alternative = state['hotels'][0].model_copy(deep=True)
    alternative.poi_id, alternative.name, alternative.estimated_cost = 'CITY2', '市区备选酒店', 250
    alternative.location.longitude += .001
    state['hotels'].append(alternative)
    original = TripPlanResponse(success=True, data=state['plan'], thread_id='adaptive-original').model_dump(mode='json')
    with store._connect() as db:
        from app.services.dialog_store import encode
        db.execute('INSERT INTO run_snapshot VALUES(?,?,?)', ('adaptive-original',encode(state),time.time()))
    current = dict(slots=req.model_dump(), committed=req.model_dump(), result=original)
    assert partial_edit.candidates(current,'hotel',day_index=1)['current_ids'] == ['REMOTE']
    pending, usage = partial_edit.prepare(current, ModuleEdit(module='hotel', day_index=0, stay_scope='night', candidate_id='CITY2'))
    assert pending['changed_days'] == [0,1]
    assert original['data']['stays'][0]['hotel']['poi_id'] == 'CITY'
    response = partial_edit.apply(current, pending, 'adaptive-edit')
    assert response.success, response.message
    result = response.data.model_dump(mode='json')
    assert result['stays'][1] == original['data']['stays'][1]
    assert result['stays'][0]['hotel']['poi_id'] == 'CITY2'
    for old, new in zip(original['data']['days'], result['days']):
        assert old['attractions'] == new['attractions']
        assert old['meals'] == new['meals']
    assert result['days'][2]['schedule'] == original['data']['days'][2]['schedule']
    assert result['days'][0]['route_end']['poi_id'] == result['days'][1]['route_start']['poi_id'] == 'CITY2'
    assert result['budget']['total_hotels'] == 550


def test_data_request_allowlist_and_total_limit(setup_stays, monkeypatch):
    from app.services.planning_data import fulfill
    _, state, _ = setup_stays
    state['attractions'] = []
    monkeypatch.setattr('app.services.food.ensure_food_coverage', lambda *a,**kw:([],[]))
    for invalid in ([{'data_type':'url','anchor_id':'CITY'}], [{'data_type':'food','anchor_id':'MADE_UP'}], 'bad'):
        with pytest.raises(ValueError): fulfill(invalid, state)
    update = fulfill([{'data_type':'food','anchor_id':'CITY'}], state)
    state.update(update)
    with pytest.raises(ValueError, match='重复'): fulfill([{'data_type':'food','anchor_id':'CITY'}], state)
    state['planning_data_requests'] *= 2
    with pytest.raises(ValueError, match='上限'): fulfill([{'data_type':'food','anchor_id':'REMOTE'}], state)


def test_supplier_route_anomaly_retains_evidence_and_is_bounded(monkeypatch):
    from app.services import route_batch
    from app.models.schemas import Location
    a = Location(longitude=120,latitude=30)
    b = Location(longitude=120.01,latitude=30)
    monkeypatch.setattr(route_batch.amap, 'travel_route', lambda *a,**kw:dict(duration_minutes=900,distance_meters=999999,status='known',source='amap'))
    result = route_batch.prefetch_routes([(a,b)],mode='自驾',city='杭州',cache={},budget_seconds=3,max_queries=2)
    assert result.query_count == 2
    assert next(iter(result.cache.values()))['status'] == 'estimated'
    assert result.calls[0]['outcome']['error_code'] == 'ANOMALOUS_ROUTE_RESULT'
    assert result.calls[0]['outcome']['stats']['observed_route']['duration_minutes'] == 900


def test_known_hotel_windows_reject_late_arrival(setup_stays):
    req,state,payload = setup_stays
    state['hotels'][1].operations_evidence_url='https://example.com/hotel'
    state['hotels'][1].check_in_before='18:00'
    plan = plan_for(req,state,payload)
    plan['days'][1]['schedule'] = [dict(end_time='19:00')]
    assert any('最晚入住' in e for e in stays.audit_stays(plan,req,state))


@pytest.mark.parametrize('kind', ['overlap','gap','backward','outside','valid'])
def test_compact_stay_segments_expand_then_use_normal_date_audit(setup_stays,kind):
    _,state,_ = setup_stays
    segments = [dict(check_in='2026-10-20',check_out='2026-10-22',hotel_id='CITY')]
    if kind == 'overlap': segments.append(dict(segments[0]))
    if kind == 'gap': segments[0]['check_out'] = '2026-10-21'
    if kind == 'backward': segments[0]['check_out'] = '2026-10-19'
    if kind == 'outside': segments[0].update(check_in='2026-10-21',check_out='2026-10-23')
    if kind != 'valid':
        with pytest.raises(ValueError): stays.bind_stays({'stay_segments':segments},state)
    else:
        rows = stays.bind_stays({'stay_segments':segments},state)
        assert [s['check_in'] for s in rows] == ['2026-10-20','2026-10-21']
        assert all(s['hotel']['nights']==1 for s in rows)


def test_invalid_day_date_is_a_validation_error_not_crash(setup_stays):
    req,state,payload=setup_stays
    plan=plan_for(req,state,payload)
    plan['days'][0]['date']='not-a-date'
    assert any('日期格式' in e for e in stays.audit_stays(plan,req,state))


def test_fixed_hotel_short_name_is_audited_with_factual_anchor(setup_stays):
    from app.services.requirements import audit_requirements
    req,state,payload=setup_stays
    req.fixed_hotel='市区'
    state['hotel_anchor']=state['hotels'][0]
    payload['stays'][1]['hotel_id']='CITY'
    plan=plan_for(req,state,payload)
    assert not stays.audit_stays(plan,req,state)
    assert not audit_requirements(plan,req)


def test_regional_extreme_weather_is_not_ignored(setup_stays):
    from tests.factories import make_attraction
    req,state,payload=setup_stays
    plan=plan_for(req,state,payload)
    spot=make_attraction('周边山地',retrieval_scope='surrounding')
    plan['days'][1]['attractions']=[spot.model_dump()]
    plan['regional_weather']=[dict(poi_id=spot.poi_id,date=plan['days'][1]['date'],status='district_reference',
                                    district_forecast=dict(day_weather='暴雨'))]
    assert any('恶劣天气' in e for e in stays.audit_stays(plan,req,state))


def test_joint_model_repairs_only_chosen_meal_slots(adaptive_scenario,monkeypatch):
    import json
    from app.agents import planner_agent
    _,state=adaptive_scenario
    before=deepcopy(state['plan'])
    monkeypatch.setattr(planner_agent,'chat_json',lambda *a,**kw:(json.dumps({'meal_patches':[
        dict(day_index=0,type='lunch',restaurant_id='R2'),dict(day_index=0,type='dinner',restaurant_id='R1')]}),{'llm_calls':1}))
    update=planner_agent.compose_plan(state)
    assert not update['validate_error'],update['validate_error']
    assert update['usage']['llm_calls']==1
    assert update['plan']['stays']==before['stays']
    assert update['plan']['days'][0]['meals'][1]['restaurant_id']=='R2'
    assert [m['restaurant_id'] for m in update['plan']['days'][1]['meals']]==[m['restaurant_id'] for m in before['days'][1]['meals']]
    assert state['plan']==before


def test_segmented_routes_prioritize_long_transfers(adaptive_scenario, monkeypatch):
    from app.services import route_batch, itinerary_experience
    from app.services.schedule_repair import estimated_minutes
    req, state = adaptive_scenario
    observed = []
    original = route_batch.prefetch_routes
    def capture(pairs, **kwargs):
        observed.append([estimated_minutes(*pair, req.transportation) for pair in pairs])
        return original(pairs, **kwargs)
    monkeypatch.setattr(route_batch, 'prefetch_routes', capture)
    itinerary_experience.enrich_experience(state['plan'], state)
    assert observed and observed[0]
    assert max(observed[0]) > 100
    assert observed[0] == sorted(observed[0], reverse=True)


@pytest.mark.parametrize('patch', [dict(day_index=0,type='lunch',restaurant_id='FAKE'),dict(day_index=5,type='lunch',restaurant_id='R1'),dict(day_index=True,type='lunch',restaurant_id='R1')])
def test_invalid_model_patch_does_not_mutate_candidate(adaptive_scenario,patch):
    from app.services.planner_patch import expand
    _,state=adaptive_scenario
    before=deepcopy(state['plan'])
    with pytest.raises(ValueError): expand({'meal_patches':[patch]},state)
    assert state['plan']==before


@pytest.mark.parametrize('arrival,departure,expected', [
    (None,None,['breakfast','lunch','dinner']), ('13:00',None,['lunch','dinner']),
    ('19:00',None,['dinner']), ('22:00',None,[]), (None,'09:00',['breakfast']),
    (None,'06:00',[]), ('13:00','16:00',['lunch'])])
def test_boundary_day_meals_follow_actual_window(arrival,departure,expected):
    from app.services.day_window import policy
    req=make_request(travel_days=1,arrival_time=arrival,departure_time=departure)
    assert policy(req,req.start_date)['meal_types']==expected


def test_late_arrival_early_departure_full_delivery(adaptive_scenario,monkeypatch):
    import json
    from app.agents import planner_agent, budget, verify_budget
    from app.services import itinerary_experience, planning
    from app.services.day_window import policy
    req,state=adaptive_scenario
    req.arrival_time,req.departure_time='22:00','09:00'
    old=state['plan']
    payload=dict(stays=[dict(check_in=s['check_in'],hotel_id=s['hotel']['poi_id']) for s in old['stays']], days=[])
    for d in old['days']:
        kinds=policy(req,d['date'])['meal_types']
        payload['days'].append(dict(date=d['date'],day_index=d['day_index'],
            attractions=[dict(poi_id=a['poi_id']) for a in d['attractions']] if d['day_index']==1 else [],
            meals=[dict(type=m['type'],restaurant_id=m['restaurant_id']) for m in d['meals'] if m['type'] in kinds]))
    monkeypatch.setattr(planner_agent,'chat_json',lambda *a,**kw:(json.dumps(payload),{'llm_calls':1}))
    update=planner_agent.compose_plan(state)
    assert not update['validate_error'],update['validate_error']
    state.update(update)
    plan,errors,warnings=itinerary_experience.enrich_experience(state['plan'],state)
    assert not errors,errors
    state.update(plan=plan,warnings=warnings)
    state.update(budget.patch_budget(state))
    state.update(verify_budget.verify_budget(state))
    response=planning._finalize(state,req,'boundary-window','boundary-window',1)
    assert response.success,response.message
    assert response.data.days[0].meals==[]
    assert response.data.days[0].schedule[0].start_time=='22:00'
    assert [m.type for m in response.data.days[-1].meals]==['breakfast']
    assert response.data.days[-1].schedule[-1].end_time<='09:00'
    assert response.data.budget.total_meals==120
    # Display metadata cannot erase the user's real meal obligations.
    wrong=response.data.model_dump()
    wrong['days'][1]['meal_types']=[]
    wrong['days'][1]['meals']=[]
    assert any('餐次' in e for e in itinerary_experience.validate_experience(wrong,state))


@pytest.mark.parametrize('return_departure,return_arrival,cutoff,feasible', [('10:00','12:00','09:15',False),('11:45','13:45','11:00',True)])
def test_intercity_joint_planning_enrichment_and_delivery(adaptive_scenario, monkeypatch,return_departure,return_arrival,cutoff,feasible):
    import json
    from app.agents import planner_agent, budget, verify_budget
    from app.models.travel import china_today
    from app.services import intercity, itinerary_experience, planning
    from app.services.day_window import policy
    from tests.test_intercity_integration import row
    req,state=adaptive_scenario
    req.start_date=str(china_today()+timedelta(days=7))
    req.origin_city,req.intercity_mode,req.adult_count='上海','train',2
    state['end_date']=str(date.fromisoformat(req.start_date)+timedelta(days=2))
    state['weather']=[w.model_copy(update={'date':str(date.fromisoformat(req.start_date)+timedelta(days=i))}) for i,w in enumerate(state['weather'])]
    state['transport_candidates']={
        'outbound':[intercity._offer(row(req,dep='20:00',arr='22:00'),req,'outbound','trains')],
        'return':[intercity._offer(row(req,'return',dep=return_departure,arr=return_arrival),req,'return','trains')]}
    endpoint=dict(name='杭州东站',poi_id='STATION',location=state['hotels'][0].location.model_dump())
    monkeypatch.setattr(intercity,'_endpoint',lambda *_:deepcopy(endpoint))
    payload=dict(transport_choices={d:p[0]['offer_id'] for d,p in state['transport_candidates'].items()},
        stays=[dict(check_in=str(date.fromisoformat(req.start_date)+timedelta(days=i)),hotel_id=s['hotel']['poi_id'])
               for i,s in enumerate(state['plan']['stays'])],days=[])
    effective=intercity.effective_request(req,{'intercity_legs':[p[0] for p in state['transport_candidates'].values()]})
    for i,d in enumerate(state['plan']['days']):
        day_date=str(date.fromisoformat(req.start_date)+timedelta(days=i))
        kinds=policy(effective,day_date)['meal_types']
        payload['days'].append(dict(date=day_date,day_index=i,
            attractions=[dict(poi_id=a['poi_id']) for a in d['attractions']] if i==1 else [],
            meals=[dict(type=m['type'],restaurant_id=m['restaurant_id']) for m in d['meals'] if m['type'] in kinds]))
    monkeypatch.setattr(planner_agent,'chat_json',lambda *a,**kw:(json.dumps(payload),{'llm_calls':1}))
    update=planner_agent.compose_plan(state)
    assert not update['validate_error'],update['validate_error']
    state.update(update)
    plan,errors,warnings=itinerary_experience.enrich_experience(state['plan'],state)
    if not feasible:
        assert any('离开时间' in e for e in errors), errors
        return  # Remote final hotel + early train is correctly rejected.
    assert not errors,errors
    state.update(plan=plan,warnings=warnings)
    state.update(budget.patch_budget(state))
    state.update(verify_budget.verify_budget(state))
    response=planning._finalize(state,req,'transport-delivery','transport-delivery',1)
    assert response.success,response.message
    assert response.data.days[0].meals==[]
    assert response.data.days[0].schedule[0].start_time=='22:30'
    assert response.data.days[-1].schedule[-1].end_time<=cutoff
    assert response.data.days[0].route_start.poi_id=='STATION'
    assert response.data.days[-1].route_end.poi_id=='STATION'
    assert response.data.adult_count==2 and len(response.data.intercity_legs)==2
    assert req.arrival_time is None and req.departure_time is None


def test_rejected_id_draft_can_be_repaired_without_delivering_it(adaptive_scenario,monkeypatch):
    import json
    from app.agents import planner_agent
    from app.services.planner_patch import expand
    _,state=adaptive_scenario
    draft=expand({'meal_patches':[dict(day_index=0,type='lunch',restaurant_id='R1')]},state)
    draft['days'][0]['meals'][1]['restaurant_id']='FAKE'
    original=deepcopy(state['plan'])
    monkeypatch.setattr(planner_agent,'chat_json',lambda *a,**kw:(json.dumps(draft),{'llm_calls':1}))
    rejected=planner_agent.compose_plan(state)
    assert rejected['validate_error'] and 'plan' not in rejected
    state.update(rejected)
    assert state['plan']==original
    assert state['planner_draft']['days'][0]['meals'][1]['restaurant_id']=='FAKE'
    monkeypatch.setattr(planner_agent,'chat_json',lambda *a,**kw:(json.dumps({'meal_patches':[dict(day_index=0,type='lunch',restaurant_id='R1')]}),{'llm_calls':1}))
    repaired=planner_agent.compose_plan(state)
    assert not repaired['validate_error'],repaired['validate_error']
    assert repaired['planner_draft'] is None
    assert repaired['plan']['days'][0]['meals'][1]['restaurant_id']=='R1'
