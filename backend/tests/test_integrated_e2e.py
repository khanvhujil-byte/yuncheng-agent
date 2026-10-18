"""Run the HTTP -> conversation -> real LangGraph -> tool runtime -> delivery pipeline.

Only external map, community and model I/O are fixtures. These are correctness
scenarios, not measurements of live model ability or production reliability.
"""
import json

from fastapi.testclient import TestClient
import pytest

from app.api.main import app
from app.agents import planner_agent, react_loop
from app.models.dialog import IntentDecision, SlotPatch
from app.models.schemas import Restaurant
from app.services import amap, conversation, java_bridge, monitoring, store
from tests.factories import make_attraction, make_day, make_request, make_weather


@pytest.fixture
def external_io(monkeypatch, settings):
    scenario = {'name': 'success', 'compositions': 0, 'searches': 0}
    spots = [make_attraction(name) for name in ('西湖', '灵隐寺', '博物馆')]
    hotel = make_attraction('湖边酒店', typecode='100100')
    monkeypatch.setattr(settings, 'weather_use_agent', False)
    monkeypatch.setattr(settings, 'map_backend', 'local')
    monkeypatch.setattr(amap, 'geocode', lambda *a, **k: (spots[0].location, '330100'))
    monkeypatch.setattr(java_bridge, 'fetch_hot', lambda: None)
    def search(keyword, city, **kwargs):
        scenario['searches'] += 1
        if '酒店' in keyword:
            return [] if scenario['name'] == 'hotel_empty' else [hotel]
        return spots
    def weather(adcode):
        if scenario['name'] == 'weather_failure':
            raise amap.AmapUnreachable('fixture timeout')
        return [make_weather(f'2026-10-{10+i}') for i in range(3)]
    monkeypatch.setattr(amap, 'search_poi', search)
    monkeypatch.setattr(amap, 'get_weather', weather)
    monkeypatch.setattr(amap, 'measure_distance', lambda *a, **k: 1.5)
    restaurants = [Restaurant(name=name, poi_id=f'R{i}', location=spots[0].location,
                               avg_cost=35+i*10, avg_cost_status='estimated', cost_source='amap')
                   for i, name in enumerate(('早餐店', '本地菜馆', '晚餐店'))]
    monkeypatch.setattr(amap, 'search_restaurants', lambda *a, **k: restaurants)
    monkeypatch.setattr(amap, 'travel_route', lambda *a, **k: {
        'duration_minutes': 5, 'distance_meters': 800, 'status': 'known', 'source': 'amap'})
    def tool_message(name, args):
        return {'content': '', 'tool_calls': [{'id': name, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}
    def model_tools(messages, tools):
        names = {t['function']['name'] for t in tools}
        steps = sum(m['role'] == 'assistant' for m in messages)
        usage = {'prompt_tokens': 10, 'completion_tokens': 5, 'llm_calls': 1}
        if 'search_attractions' in names:
            result = tool_message('search_attractions', {'keyword': '景点'}) if steps == 0 else {'content': '已完成'}
        elif steps == 0:
            result = tool_message('search_hotels', {'keyword': '经济型酒店'})
        elif steps == 1:
            result = tool_message('rank_hotels', {})
        elif steps == 2:
            result = tool_message('measure_distance', {'hotel': '湖边酒店', 'attraction': '西湖'})
        elif steps == 3:
            result = tool_message('choose_hotel', {'hotel': '湖边酒店', 'price_per_night': 200, 'reason': '按景点加权距离排序靠前，符合经济型住宿偏好。'})
        else:
            result = {'content': '已完成'}
        return result, usage
    def compose(*args, **kwargs):
        scenario['compositions'] += 1
        if scenario['name'] == 'json_array':
            return '[]', {'llm_calls': 1}
        selected = spots if scenario['name'] != 'hallucinated' else [make_attraction('模型编造景点'), *spots[1:]]
        return json.dumps({'days': [make_day(i, f'2026-10-{10+i}', [spot]) for i, spot in enumerate(selected)]}), {'prompt_tokens': 20, 'completion_tokens': 10, 'llm_calls': 1}
    monkeypatch.setattr(react_loop, 'chat_with_tools', model_tools)
    monkeypatch.setattr(planner_agent, 'chat_json', compose)
    return scenario


@pytest.mark.parametrize('scenario', ['success', 'hallucinated', 'json_array', 'weather_failure', 'hotel_empty', 'budget_exceeded'])
def test_full_pipeline_verdict_constraints_and_bounded_cost(external_io, scenario):
    external_io['name'] = scenario
    client = TestClient(app)
    req = make_request(must_visit=[], budget_limit=1 if scenario == 'budget_exceeded' else None)
    body = client.post('/api/trip/plan', json=req.model_dump()).json()
    assert body['success'] == (scenario == 'success'), body
    if scenario == 'success':
        assert len(body['data']['days']) == 3
        assert body['data']['hotel']['name'] == '湖边酒店'
        assert body['data']['hotel_reason']
        assert monitoring.summary()['first_success_rate'] == 1
        assert store.find_itinerary(store.fingerprint_of(req)) is not None
    else:
        assert body['message'] and store.find_itinerary(store.fingerprint_of(req)) is None
    assert external_io['compositions'] <= 6
    assert monitoring.summary()['sample_runs'] == 1


def test_multi_turn_confirm_modify_reuse_reject_with_real_pipeline(external_io, monkeypatch):
    client = TestClient(app)
    client.post('/api/auth/register', json={'username': 'interview', 'password': 'valid-password'})
    token = client.post('/api/auth/login', json={'username': 'interview', 'password': 'valid-password'}).json()['token']
    headers = {'X-Session-Token': token}
    identifier = client.post('/api/conversations', headers=headers).json()['conversation_id']
    base = f'/api/conversations/{identifier}/turns'
    monkeypatch.setattr(conversation, 'extract', lambda message, current: (IntentDecision(intent='modify_plan', slot_updates=SlotPatch.model_validate(make_request(must_visit=[]).model_dump())), {'llm_calls': 1}))
    clarified = client.post(base, headers=headers, json={'request_id': 'natural-input', 'expected_revision': 0, 'message': '帮我去杭州玩三天'}).json()
    assert clarified['ready'] and clarified['result'] is None
    confirm = {'request_id': 'confirm-request', 'expected_revision': 1, 'action': 'confirm'}
    first = client.post(base, headers=headers, json=confirm).json()
    assert first['result']['success']
    calls = external_io['compositions']
    assert client.post(base, headers=headers, json=confirm).json() == first
    assert external_io['compositions'] == calls
    searches = external_io['searches']
    client.post(base, headers=headers, json={'request_id': 'budget-change', 'expected_revision': 2, 'slot_updates': {'budget_limit': 100000}})
    second = client.post(base, headers=headers, json={'request_id': 'confirm-second', 'expected_revision': 3, 'action': 'confirm'}).json()
    assert second['result']['success']
    # Attraction facts reused; hotel re-searched because requirements changed.
    assert external_io['searches'] == searches + 1
    assert first['result']['thread_id'] != second['result']['thread_id']
    run_id = second['result']['thread_id']
    assert client.get(f'/api/observability/runs/{run_id}').status_code == 401
    assert client.get(f'/api/observability/runs/{run_id}', headers=headers).status_code == 200
    assert len(client.get(f'/api/conversations/{identifier}/versions', headers=headers).json()) == 2


@pytest.mark.parametrize('requirements,success', [
    ({'transport': {'max_leg_minutes': 1}}, False),
    ({'food': {'max_cost_per_meal': 40}}, True),
    ({'hotel': {'max_price_per_night': 100}}, False),
    ({'attractions': {'max_per_day': 1}}, True),
    ({'food': {'priority': 'food_first'}}, False),
])
def test_module_requirements_hold_across_real_graph_and_bounded_retries(external_io, requirements, success):
    req = make_request(must_visit=[], requirements=requirements)
    body = TestClient(app).post('/api/trip/plan', json=req.model_dump()).json()
    assert body['success'] is success, body
    assert external_io['compositions'] <= 2
    if not success:
        assert body['message']
        assert store.find_itinerary(store.fingerprint_of(req)) is None
    elif 'food' in requirements:
        assert all(meal['restaurant_id'] == 'R0' for day in body['data']['days'] for meal in day['meals'])
