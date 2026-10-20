"""Regression oracles for failures observed in live travel planning.

External IO is controlled here. Live-model quality is measured separately by
scripts/eval_scenarios_30.py; these assertions must not be reported as live runs.
"""
import json

import pytest

from app.agents import attraction_agent, planner_agent
from app.models.requirements import DayGoal
from app.services import amap, day_goals, planner_json, planning, store
from app.services.node_timing import measured
from app.services.request_validation import validate_request
from app.services.schedule_repair import remove_one_optional
from tests.factories import make_attraction, make_day, make_request


@pytest.mark.parametrize('text,expected', [
    ('有两天想去周边的雪山', [('雪山', 2, 'surrounding')]),
    ('我想有2天去附近雪山', [('雪山', 2, 'surrounding')]),
    ('雪山玩两天，其余时间逛市区', [('雪山', 2, 'city')]),
    ('留三天去周边古镇', [('古镇', 3, 'surrounding')]),
    ('有一天逛博物馆', [('博物馆', 1, 'city')]),
    ('两天去海边', [('海边', 2, 'city')]),
    ('雪山至少两天', [('雪山', 2, 'city')]),
    ('三天旅行，其中两天去雪山', [('雪山', 2, 'city')]),
    ('第二天去雪山', []),
    ('第十二天去雪山', []),
    ('不要安排两天去雪山', []),
    ('不想去雪山', []),
    ('如果下雨，不去雪山', []),
    ('雪山有两天降雪预报', []),
    ('杭州旅游两天，想了解雪山照片', []),
    ('想去雪山', []),
])
def test_explicit_theme_counts_preserve_scope_without_inventing_durations(text, expected):
    assert [(g.theme, g.days, g.scope) for g in day_goals.text_goals(text)] == expected


def test_theme_duration_conflict_blocks_before_external_work():
    req = make_request(travel_days=1, free_text_input='有两天想去周边雪山')
    assert validate_request(req)[0] == 'CONSTRAINT_INFEASIBLE'


def test_structured_days_are_preserved_without_overriding_with_a_text_guess():
    req = make_request(free_text_input='想有两天去雪山', requirements={'attractions': {
        'day_goals': [{'theme': '雪山', 'days': 3, 'day_indices': [0, 1, 2]}]}})
    assert day_goals.effective_goals(req)[0].days == 3


@pytest.mark.parametrize('name,typecode,address,description,expected', [
    ('玉龙雪山景区', '110000', '', '', True),
    ('蓝月谷', '110000', '玉龙雪山风景区内', '', True),
    ('玉龙雪山停车场', '150900', '', '', False),
    ('玉龙雪山游客中心', '110000', '', '', False),
    ('雪山酒店', '100100', '', '', False),
    ('雪山服装店', '060000', '', '', False),
    ('城市公园', '110000', '', '这是一次雪山游玩', False),
    ('市区博物馆', '140100', '', '', False),
])
def test_theme_membership_uses_provider_place_facts(name, typecode, address, description, expected):
    spot = make_attraction(name, typecode=typecode, address=address, description=description)
    assert day_goals.matches(spot, DayGoal(theme='雪山', days=2)) == expected


@pytest.mark.parametrize('layout,ok', [
    ([['雪山甲'], ['雪山乙'], ['城市公园']], True),
    ([['雪山甲', '雪山乙'], ['市区公园'], ['市区博物馆']], False),
    ([['雪山甲'], ['市区公园'], ['市区博物馆']], False),
    ([['公园甲'], ['公园乙'], ['公园丙']], False),
    ([['雪山甲', '公园甲', '公园乙'], ['雪山乙'], ['城市公园']], False),
])
def test_theme_count_is_distinct_days_and_main_activities(layout, ok):
    req = make_request(free_text_input='有两天想去周边雪山')
    plan = {'days': [make_day(i, f'2026-10-{10+i}', [make_attraction(n) for n in names])
                     for i, names in enumerate(layout)]}
    assert (not day_goals.audit_days(plan, req)) == ok


def test_cached_wrong_theme_result_is_rejected_and_removed(monkeypatch):
    req = make_request(free_text_input='有两天想去周边雪山', nights=0, must_visit=[])
    days = [make_day(i, f'2026-10-{10+i}', [make_attraction('市区公园' + str(i))]) for i in range(3)]
    plan = {'city': req.city, 'start_date': req.start_date, 'end_date': '2026-10-12',
            'hotel_nights': 0, 'days': days}
    fingerprint = store.fingerprint_of(req)
    store.save_itinerary(fingerprint, req, plan)
    monkeypatch.setattr(planning, '_reuse_with_new_dates', lambda saved, request: saved)
    result = planning._cached_response(store.find_itinerary(fingerprint), req, 'snow-cache', fingerprint, 0)
    assert not result.success
    assert '雪山' in result.message and '2天' in result.message
    assert store.find_itinerary(fingerprint) is None


def test_regional_theme_search_expands_city_boundary_but_excludes_far_places(monkeypatch, settings):
    calls = []
    monkeypatch.setattr(settings, 'attraction_use_agent', False)
    near = make_attraction('周边雪山', lng=120.4)
    far = make_attraction('千里外雪山', lng=95)
    town = make_attraction('市区公园')
    def search(keyword, city, **kwargs):
        calls.append((keyword, kwargs))
        return [near, far] if keyword == '雪山' else [town]
    monkeypatch.setattr(amap, 'search_poi', search)
    req = make_request(must_visit=[], free_text_input='有两天想去周边的雪山')
    result = attraction_agent.collect_attractions({**planning._initial_state(req), 'city_center': town.location})
    snow_calls = [options for word, options in calls if word == '雪山']
    assert snow_calls and all(options['citylimit'] is False for options in snow_calls)
    assert near.poi_id in {p.poi_id for p in result['attractions']}
    assert far.poi_id not in {p.poi_id for p in result['attractions']}


def test_prompt_compaction_preserves_unrated_theme_candidates(monkeypatch, settings):
    monkeypatch.setattr(settings, 'prompt_compact', True)
    monkeypatch.setattr(settings, 'prompt_max_candidates', 5)
    snow = make_attraction('周边雪山', rating=None)
    candidates = [make_attraction(f'市区景点{i}', rating=5) for i in range(30)] + [snow]
    chosen, total = planner_agent._select_candidates(candidates, 3, [DayGoal(theme='雪山', days=2)])
    assert snow in chosen and total == 31


def test_comfort_repair_cannot_drop_a_requested_theme():
    req = make_request(free_text_input='有两天去雪山')
    snow = make_attraction('周边雪山', visit_duration=360)
    optional = make_attraction('可选公园', visit_duration=30)
    day = make_day(0, req.start_date, [snow, optional])
    removed = remove_one_optional(day, {'request': req})
    assert removed and [s['name'] for s in day['attractions']] == ['周边雪山']


@pytest.mark.parametrize('raw,expected', [
    ('{"day_index":3", "days":[]}', {'day_index': 3, 'days': []}),
    ('{"days":[{"day_index":0"},{"day_index":1"}]}', {'days': [{'day_index': 0}, {'day_index': 1}]}),
    ('```json\n{"days": []}\n```', {'days': []}),
    ('{"description":"数字:3\\\"","day_index":0"}', {'description': '数字:3"', 'day_index': 0}),
    ('{"id":"123","day_index":0}', {'id': '123', 'day_index': 0}),
])
def test_observed_json_syntax_repair_preserves_values(raw, expected):
    assert planner_json.loads(raw) == expected


@pytest.mark.parametrize('raw', [
    '{"days":[{"day_index":0}',
    '{"days":[],"days":[1]}',
    '{"price":NaN}',
    '{"price":Infinity}',
    '{"poi_id":unknown}',
    '{"days":[1 2]}',
])
def test_json_parser_never_invents_truncated_or_ambiguous_data(raw):
    with pytest.raises(json.JSONDecodeError):
        planner_json.loads(raw)


def test_planner_call_budget_counts_format_repair_calls():
    node = measured('compose_plan', lambda state: {'usage': {'llm_calls': 2}, 'plan': {'days': [{}]}})
    assert node({'planning_calls': 3})['planning_calls'] == 5


def test_comfort_can_remove_redundant_theme_stops_without_erasing_the_day():
    req = make_request(free_text_input='有两天去雪山')
    snow = [make_attraction('雪山' + str(i), visit_duration=180) for i in range(3)]
    day = make_day(0, req.start_date, snow)
    assert remove_one_optional(day, {'request': req})
    assert remove_one_optional(day, {'request': req})
    assert remove_one_optional(day, {'request': req}) is None
    assert len(day['attractions']) == 1


def test_snow_expansion_is_bounded_and_returns_queries_not_invented_places():
    queries = day_goals.search_queries([DayGoal(theme='雪山', days=2, scope='surrounding')], '成都市')
    assert ('西岭雪山', True) in queries and ('四姑娘山', True) in queries
    assert len(queries) <= 12


def test_partial_response_keeps_reason_without_internal_retry_matrix():
    errors = ['2026-10-20 合计707分钟，超过舒适上限。\n【上次方案和按相同规则计算的候选】\n' + '{"days":[]}' * 1000]
    message = planning._partial_message([], errors, True)
    assert '707分钟' in message
    assert '候选' not in message and len(message) < 300


def test_dinner_can_be_near_hotel_without_a_detour_back_to_the_last_attraction():
    from app.models.schemas import Restaurant
    from app.services.food_recommendation import nearby_pool, distances
    mountain = make_attraction('远郊景区', lng=120.6).model_dump()
    hotel = make_attraction('市区酒店', lng=120.1).model_dump()
    dinner = Restaurant(name='酒店旁餐厅', poi_id='dinner', location=hotel['location'])
    assert distances(dinner, mountain, hotel)[0] > 4
    assert distances(dinner, mountain, hotel, 'dinner')[0] == 0
    assert nearby_pool([dinner], mountain, hotel, 'dinner') == [dinner]


def test_actual_clock_can_move_dinner_to_hotel_and_preserves_required_visits(monkeypatch):
    from app.models.schemas import Hotel, Restaurant
    from app.services import itinerary_experience as experience
    req = make_request(travel_days=1, nights=1, must_visit=[])
    spot = make_attraction('必去展馆', lng=120.14, required=True, visit_duration=120)
    hotel = Hotel(name='酒店', poi_id='H', location=make_attraction('hotel', lng=120.10).location)
    far = Restaurant(name='景点旁面馆', poi_id='R1', location=spot.location)
    near = Restaurant(name='酒店旁餐厅', poi_id='R2', location=hotel.location)
    meals = [experience.meal_from_restaurant(far, kind) for kind in ('breakfast', 'lunch', 'dinner')]
    plan = {'days': [{**make_day(0, req.start_date, [spot]), 'meals': meals}]}
    def route(origin, destination, **kwargs):
        minutes = 90 if destination == hotel.location and origin != hotel.location else 10
        from app.services.tools import haversine_km
        return {'duration_minutes': minutes, 'distance_meters': max(1000,round(haversine_km(origin,destination)*1200)), 'status': 'known', 'source': 'amap'}
    monkeypatch.setattr(experience.amap, 'travel_route', route)
    monkeypatch.setattr(experience, '_select_meals', lambda day, *args, **kwargs: day['meals'])
    state = {**planning._initial_state(req), 'experience_enabled': True, 'unified_planning': True,
             'hotel': hotel, 'hotels': [hotel], 'restaurants': [far, near], 'attractions': [spot]}
    result, _, notes = experience.enrich_experience(plan, state)
    day = result['days'][0]
    assert day['attractions'][0]['poi_id'] == spot.poi_id
    assert next(m for m in day['meals'] if m['type'] == 'dinner')['restaurant_id'] == near.poi_id
    assert day['total_minutes'] <= req.requirements.pace.max_day_minutes
    assert any('晚餐后的长途返程' in note for note in notes)


def test_preplanning_reserves_retrieval_for_the_chosen_hotel(monkeypatch, settings):
    from app.services import food, unified_planning
    from app.agents.hotel_agent import _candidate_key
    req = make_request(must_visit=[])
    seen = []
    monkeypatch.setattr(food, 'ensure_food_coverage', lambda plan, state, **kwargs: (seen.append(kwargs) or [], []))
    state = {**planning._initial_state(req), 'experience_enabled': True}
    state['hotel_candidate_key'] = _candidate_key(req, state['nights'])
    unified_planning.prepare_planning_context(state)
    assert 0 <= seen[0]['query_limit'] <= 1
    assert settings.food_coverage_query_limit - seen[0]['query_limit'] >= min(2, settings.food_coverage_query_limit)
    assert seen[0]['budget_seconds'] == settings.food_coverage_budget_seconds / 2


def test_new_attractions_after_replan_get_estimated_once_under_a_shared_cap(monkeypatch, settings):
    from app.services import ticket_estimates
    monkeypatch.setattr(settings, 'ticket_model_estimate_enabled', True)
    monkeypatch.setattr(settings, 'llm_api_key', 'offline-test-key')
    calls = []
    def model(system, user, **kwargs):
        places = json.loads(user)['places']
        calls.append(places)
        return json.dumps({'prices': [{'id': p['id'], 'amount': 42, 'basis': '离线测试数据'} for p in places]}), {'llm_calls': 1}
    monkeypatch.setattr(ticket_estimates.llm, 'chat_json', model)
    req = make_request(travel_days=1)
    state = {}
    for index in range(4):
        plan = {'days': [make_day(0, req.start_date, [make_attraction('换入景点'+str(index))])]}
        ticket_estimates.fill(plan, req, state)
        spot = plan['days'][0]['attractions'][0]
        assert spot['ticket_price_source'] == ('llm_estimated' if index < 3 else 'static_estimate')
        ticket_estimates.fill(plan, req, state)
        assert len(calls) == min(index + 1, 3)
    assert state['ticket_estimate_calls'] == 3
    assert len(state['ticket_estimate_attempted_ids']) == 3


def test_bad_weather_retrieves_real_indoor_options_and_preserves_anchors(monkeypatch):
    from app.models.schemas import WeatherInfo
    from app.services.unified_planning import _weather_candidates
    req = make_request(travel_days=1, must_visit=[])
    anchor = make_attraction('必去海滩', required=True)
    museum = make_attraction('城市博物馆', rating=None)
    seen = []
    def search(keyword, city, **kwargs):
        seen.append(keyword)
        return [museum]
    monkeypatch.setattr(amap, 'search_poi', search)
    weather = [WeatherInfo(date=req.start_date, day_weather='雷阵雨')]
    update = _weather_candidates({**planning._initial_state(req), 'weather': weather, 'attractions': [anchor]})
    assert set(seen) == {'博物馆', '水族馆'}
    assert anchor in update['attractions'] and museum in update['attractions']
    chosen, _ = planner_agent._select_candidates([make_attraction('高评分公园'+str(i), rating=5) for i in range(30)] + [museum], 1, weather=weather)
    assert museum in chosen


def test_omitted_required_anchor_is_restored_from_facts_into_an_available_day():
    from app.models.schemas import DayPlan
    from app.services.schedule_repair import restore_required_anchors
    req = make_request(must_visit=[])
    a, b, c = [make_attraction('必去'+str(i), required=True) for i in range(3)]
    optional = make_attraction('可选公园')
    days = [DayPlan.model_validate(make_day(i, f'2026-10-{10+i}', [s])) for i, s in enumerate((a, b, optional))]
    notes = restore_required_anchors(days, [a, b, c], req)
    assert c.poi_id in [s.poi_id for s in days[2].attractions]
    assert days[2].attractions[-1].required and notes
    assert restore_required_anchors(days, [a, b, c], req) == []
    assert c not in [optional]


def test_anchor_repair_does_not_guess_another_theme_or_overfill_a_day():
    from app.models.schemas import DayPlan
    from app.services.schedule_repair import restore_required_anchors
    req = make_request(travel_days=1, requirements={'attractions': {'max_per_day': 1}})
    day = DayPlan.model_validate(make_day(0, req.start_date, [make_attraction('原景点')]))
    anchor = make_attraction('遗漏必去', required=True)
    assert restore_required_anchors([day], [anchor], req) == []
    assert len(day.attractions) == 1


def test_execution_lease_releases_the_acquired_database_after_configuration_changes(monkeypatch, tmp_path):
    import sqlite3
    from app.services.execution_lease import Lease
    first, second = tmp_path/'first.sqlite', tmp_path/'second.sqlite'
    monkeypatch.setattr(store, 'DB_PATH', first)
    lease = Lease('run').acquire()
    monkeypatch.setattr(store, 'DB_PATH', second)
    try:
        lease.check()
    finally:
        lease.release()
    assert not second.exists()
    with sqlite3.connect(first) as db:
        assert db.execute('SELECT COUNT(*) FROM execution_lease').fetchone()[0] == 0
