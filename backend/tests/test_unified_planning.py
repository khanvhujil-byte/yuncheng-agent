import copy
import json

import pytest

from app.agents import planner_agent
from app.models.schemas import Hotel, Restaurant
from app.services import itinerary_experience as experience, runner
from app.services.planning import _initial_state
from app.services.unified_planning import audit_choices, bind_choices
from tests.factories import make_attraction, make_request


def scenario():
    req = make_request(travel_days=1, nights=1, must_visit=[])
    state = {**_initial_state(req), 'unified_planning': True}
    spot = make_attraction('真实博物馆', poi_id='A', visit_duration=90)
    state['attractions'] = [spot]
    state['hotels'] = [Hotel(name='附近酒店', poi_id='H1', location=spot.location),
                       Hotel(name='另一家酒店', poi_id='H2', location=spot.location)]
    state['restaurants'] = [Restaurant(name=f'餐厅{i}', poi_id=f'R{i}', location=spot.location,
                                      avg_cost=30+i, cost_source='amap') for i in range(40)]
    output = {'hotel_id': 'H2', 'hotel_reason': '位于首末景点附近，预留住宿后仍留有用餐空间',
              'days': [{'date': req.start_date, 'day_index': 0, 'attractions': [{'poi_id': 'A'}],
                        'meals': [{'type': kind, 'restaurant_id': f'R{i+32}'}
                                  for i, kind in enumerate(('breakfast', 'lunch', 'dinner'))]}]}
    return state, output


def test_planner_sees_all_food_and_hotel_candidates_and_hydrates_ids(monkeypatch):
    state, output = scenario()
    def model(system, user, **kwargs):
        assert '唯一' in system and 'R39' in user and 'H1' in user and 'H2' in user
        return json.dumps(output), {'llm_calls': 1, 'prompt_tokens': 20}
    monkeypatch.setattr(planner_agent, 'chat_json', model)
    result = planner_agent.compose_plan(state)
    assert not result['validate_error']
    assert result['hotel'].poi_id == 'H2'
    assert result['plan']['days'][0]['meals'][0]['name'] == '餐厅32'
    assert result['usage']['llm_calls'] == 1


@pytest.mark.parametrize('kind', ['hotel', 'meal', 'attraction', 'meal_types'])
def test_outside_pool_ids_and_duplicate_meal_types_fail_with_usage(monkeypatch, kind):
    state, output = scenario()
    if kind == 'hotel':
        output['hotel_id'] = 'FAKE'
    elif kind == 'meal':
        output['days'][0]['meals'][0]['restaurant_id'] = 'FAKE'
    elif kind == 'attraction':
        output['days'][0]['attractions'][0]['poi_id'] = 'FAKE'
    else:
        output['days'][0]['meals'][0]['type'] = 'lunch'
    monkeypatch.setattr(planner_agent, 'chat_json', lambda *a, **k: (json.dumps(output), {'llm_calls': 1}))
    result = planner_agent.compose_plan(state)
    assert result['validate_error'] and result['retries'] == 1
    assert result['usage']['llm_calls'] == 1
    assert not result.get('plan')


def test_fact_binding_rejects_model_prices_and_coordinates():
    state, output = scenario()
    output['days'][0]['attractions'][0].update(ticket_price=1, name='伪造', visit_duration=1)
    output['days'][0]['meals'][0].update(estimated_cost=1, name='伪造')
    bound, hotel = bind_choices(output, state)
    assert bound['days'][0]['attractions'][0]['name'] == '真实博物馆'
    assert bound['days'][0]['attractions'][0]['visit_duration'] == 90
    assert bound['days'][0]['meals'][0]['estimated_cost'] == 62
    assert hotel.cost_source == 'static_estimate' and hotel.nights == 1


def test_repeated_food_returns_alternatives_instead_of_silent_replacement(monkeypatch):
    state, output = scenario()
    for meal in output['days'][0]['meals']:
        meal['restaurant_id'] = 'R32'
    payload, hotel = bind_choices(output, state)
    errors = audit_choices(payload, state, hotel)
    assert any('重复' in error and 'R0' in error for error in errors)
    state['retries'] = 1
    monkeypatch.setattr(planner_agent, 'chat_json', lambda *a, **k: (json.dumps(output), {'llm_calls': 1}))
    final = planner_agent.compose_plan(state)
    assert '重复' in final['validate_error']
    assert {m['restaurant_id'] for m in final['plan']['days'][0]['meals']} == {'R32'}


def test_postprocessing_preserves_model_hotel_and_meal_ids(monkeypatch, settings):
    state, output = scenario()
    payload, hotel = bind_choices(output, state)
    state['hotel'] = hotel
    monkeypatch.setattr(settings, 'food_coverage_enabled', False)
    monkeypatch.setattr(settings, 'official_ticket_lookup_enabled', False)
    monkeypatch.setattr(experience.amap, 'travel_route', lambda *a, **k: {
        'duration_minutes': 5, 'distance_meters': 10, 'status': 'known', 'source': 'amap'})
    before = copy.deepcopy(payload)
    enriched, errors, _ = experience.enrich_experience(payload, state)
    assert not errors
    assert [m['restaurant_id'] for m in enriched['days'][0]['meals']] == ['R32', 'R33', 'R34']
    assert state['hotel'].poi_id == 'H2' and payload == before


def test_new_recovery_barrier_needs_all_four_collectors():
    nodes = ['validate_city', 'collect_food', 'collect_attractions', 'collect_weather']
    state = {'planning_contract_version': 9, 'experience_enabled': True}
    assert runner.can_resume_from(nodes, state) is None
    assert runner.can_resume_from(nodes + ['collect_hotels'], state) == 'prepare_planning_context'
    assert runner.can_resume_from(['prepare_planning_context'], state) == 'compose_plan'
    assert runner.can_resume_from(['verify_plan'], {**state, 'unified_planning': True}) == 'enrich_experience'
