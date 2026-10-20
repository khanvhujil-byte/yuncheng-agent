from datetime import timedelta
import json
import subprocess
from types import SimpleNamespace

from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
import pytest

from app.config import Settings, flyai_process_env
from app.models.travel import HotelQuery, TransportQuery, china_today, cny_amount
from app.services import flyai, identity
from app.services.request_gate import RequestGate


@pytest.fixture
def provider(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'flyai_enabled', True)
    monkeypatch.setattr(settings, 'flyai_api_key', SecretStr('test-key-never-log'))
    monkeypatch.setattr(settings, 'flyai_request_interval_seconds', .1)
    monkeypatch.setattr(settings, 'flyai_cache_ttl_seconds', 120)
    bundle = tmp_path / 'official.cjs'
    bundle.write_text('// fake runtime')
    monkeypatch.setattr(flyai, 'BUNDLE', bundle)
    monkeypatch.setattr(flyai, '_cache', flyai.OrderedDict())
    monkeypatch.setattr(flyai, '_gate', RequestGate())
    calls = []
    response = {'status': 0, 'data': {'itemList': []}}
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(response, ensure_ascii=False), stderr='')
    monkeypatch.setattr(flyai.subprocess, 'run', run)
    return response, calls


def params(kind):
    target = china_today()+timedelta(days=7)
    if kind == 'attractions':
        return {'city': '北京', 'keyword': '故宫'}
    if kind == 'hotels':
        return {'city': '杭州', 'check_in': str(target), 'check_out': str(target+timedelta(days=2)), 'max_price': 500}
    return {'origin': '杭州', 'destination': '上海', 'departure_date': str(target)}


@pytest.mark.parametrize('kind', ['attractions', 'hotels', 'flights', 'trains'])
def test_each_fixed_read_command_and_empty_success(provider, kind):
    _, calls = provider
    r = flyai.query(kind, params(kind))
    assert r['outcome']['status'] == 'ok' and not r['items']
    assert calls[0][0][2] == flyai._COMMANDS[kind]
    assert calls[0][1]['shell'] is False and calls[0][1]['timeout'] > 0
    assert 'test-key-never-log' not in str(calls[0][0])
    assert calls[0][1]['env']['FLYAI_API_KEY'] == 'test-key-never-log'


def test_cache_copy_key_isolation_date_and_upstream_count(provider, settings, monkeypatch):
    response, calls = provider
    response['data']['itemList'] = [{'id': '64', 'name': '故宫'}]
    one = flyai.query('attractions', params('attractions'))
    one['items'][0]['name'] = 'mutated'
    two = flyai.query('attractions', params('attractions'))
    assert two['cache_hit'] and two['items'][0]['name'] == '故宫'
    assert two['outcome']['stats']['upstream_calls'] == 0 and len(calls) == 1
    monkeypatch.setattr(settings, 'flyai_api_key', SecretStr('other-key'))
    assert not flyai.query('attractions', params('attractions'))['cache_hit']
    assert len(calls) == 2


def test_hotel_dates_are_cache_identity(provider):
    _, calls = provider
    a = params('hotels')
    flyai.query('hotels', a)
    b = {**a, 'check_out': str(china_today()+timedelta(days=10))}
    flyai.query('hotels', b)
    assert len(calls) == 2


@pytest.mark.parametrize('field,value', [('city', '--api-key'), ('keyword', 'abc\n--config'), ('keyword', '\x00')])
def test_rejects_option_and_control_character_inputs(provider, field, value):
    _, calls = provider
    r = flyai.query('attractions', {**params('attractions'), field: value})
    assert r['outcome']['error_code'] == 'FLYAI_INVALID_ARGUMENT' and not calls


def test_shell_metacharacters_remain_one_argument(provider):
    _, calls = provider
    value = '西湖;$(echo attack) & 北京'
    flyai.query('attractions', {'city': '杭州', 'keyword': value})
    assert value in calls[0][0] and calls[0][1]['shell'] is False


@pytest.mark.parametrize('kind,extra', [('config', {}), ('attractions', {'command': 'config'}), ('hotels', {'max_price': -1})])
def test_arbitrary_commands_and_unknown_fields_rejected(provider, kind, extra):
    _, calls = provider
    r = flyai.query(kind, {**params(kind), **extra})
    assert r['outcome']['error_code'] == 'FLYAI_INVALID_ARGUMENT' and not calls


@pytest.mark.parametrize('code', [401, 403, 'INVALID_API_KEY'])
def test_auth_errors_structured_no_retry_or_secret(provider, code):
    response, calls = provider
    response.update(status=code, message='test-key-never-log')
    r = flyai.query('attractions', params('attractions'))
    assert r['outcome']['error_code'] == 'FLYAI_AUTH_FAILED'
    assert not r['outcome']['retryable'] and len(calls) == 1
    assert 'test-key-never-log' not in json.dumps(r)


def test_rate_limit_shared_cooldown_and_no_immediate_second_call(provider):
    response, calls = provider
    response.update(status=429, retry_after_seconds=3)
    r = flyai.query('attractions', params('attractions'))
    assert r['outcome']['error_code'] == 'UPSTREAM_RATE_LIMITED'
    assert r['outcome']['retry_after_seconds'] == 3
    assert r['outcome']['stats']['upstream_calls'] == 1
    r = flyai.query('attractions', {'city': '杭州'}, budget=.1)
    assert r['outcome']['error_code'] == 'UPSTREAM_RATE_LIMITED' and len(calls) == 1
    assert r['outcome']['stats']['upstream_calls'] == 0


def test_timeout_is_not_retried_and_error_cannot_leak(provider, monkeypatch):
    _, calls = provider
    def run(args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired(args, kwargs['timeout'], stderr='test-key-never-log')
    monkeypatch.setattr(flyai.subprocess, 'run', run)
    r = flyai.query('attractions', params('attractions'))
    assert r['outcome']['error_code'] == 'FLYAI_TIMEOUT'
    assert not r['outcome']['retryable'] and len(calls) == 1
    assert r['outcome']['stats']['upstream_calls'] == 1
    assert 'test-key-never-log' not in json.dumps(r)


@pytest.mark.parametrize('payload', [[], {'status': False}, {'status': 0, 'data': {}},
    {'status': 0, 'data': {'itemList': [None]}}, {'status': 0, 'data': {'itemList': [{'name': '故宫'}]}}])
def test_malformed_response(provider, payload, monkeypatch):
    response, calls = provider
    # shared object replacement through fake run, covering list top level too
    monkeypatch.setattr(flyai.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=''))
    r = flyai.query('attractions', params('attractions'))
    assert r['outcome']['status'] == 'error'


def test_non_json_and_missing_node(provider, monkeypatch):
    monkeypatch.setattr(flyai.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='bad json', stderr='test-key-never-log'))
    assert flyai.query('attractions', params('attractions'))['outcome']['error_code'] == 'FLYAI_INVALID_RESPONSE'
    def missing(*a, **kw):
        raise FileNotFoundError('node not installed')
    monkeypatch.setattr(flyai.subprocess, 'run', missing)
    assert flyai.query('attractions', params('attractions'))['outcome']['error_code'] == 'FLYAI_RUNTIME_MISSING'


def test_disabled_and_missing_key_send_no_request(provider, settings, monkeypatch):
    _, calls = provider
    monkeypatch.setattr(settings, 'flyai_enabled', False)
    assert flyai.query('attractions', params('attractions'))['outcome']['error_code'] == 'FLYAI_DISABLED'
    monkeypatch.setattr(settings, 'flyai_enabled', True)
    monkeypatch.setattr(settings, 'flyai_api_key', SecretStr(''))
    assert flyai.query('attractions', params('attractions'))['outcome']['error_code'] == 'FLYAI_NOT_CONFIGURED'
    assert not calls


def test_secret_not_in_model_dump_or_repr(provider):
    s = Settings(_env_file=None, flyai_api_key='sentinel-secret')
    assert 'sentinel-secret' not in repr(s) and 'flyai_api_key' not in s.model_dump()


def test_child_env_excludes_other_api_keys(provider, monkeypatch):
    monkeypatch.setenv('LLM_API_KEY', 'other-secret')
    monkeypatch.setenv('NODE_OPTIONS', '--require malicious.js')
    env = flyai_process_env()
    assert 'LLM_API_KEY' not in env and 'NODE_OPTIONS' not in env


@pytest.mark.parametrize('value,expected', [('¥618', 618), ('￥16.80', 16.8), ('CNY 20', 20),
    ('0元', 0), (12.5, 12.5), (None, None), (True, None), ('¥100起', None),
    ('100-200', None), ('$15', None), ('-10', None), ('nan', None), ('inf', None)])
def test_money_units_and_unknown_not_free(value, expected):
    assert cny_amount(value) == expected


def test_attraction_retains_child_identity_date_and_no_ordinary_price_claim(provider):
    response, _ = provider
    response['data']['itemList'] = [
        {'id': '64', 'name': '故宫博物院', 'freePoiStatus': 'NOT_FREE'},
        {'id': '14294562', 'name': '故宫珍宝馆', 'ticketInfo': {
            'itemId': 'sales123', 'price': '¥16.8', 'ticketName': '大门票 成人票',
            'skuCommissionStruct': {'儿童票': {'price': '2'}}}}]
    r = flyai.query('attractions', params('attractions'))
    parent, child = r['items']
    assert parent['amount'] is None and child['amount'] == 16.8
    assert child['provider_id'] == '14294562' and child['product_id'] == 'sales123'
    assert child['price_date'] is None and not child['ordinary_admission_confirmed']


def test_hotel_is_nightly_reference_not_total(provider):
    response, _ = provider
    response['data']['itemList'] = [{'shId': 123, 'name': '酒店', 'price': '¥618'}]
    item = flyai.query('hotels', params('hotels'))['items'][0]
    assert item['nights'] == 2 and item['amount'] == 618 and item['unit'] == 'room_night_reference'
    assert not item['inventory_confirmed'] and not item['room_occupancy_confirmed']


@pytest.mark.parametrize('kind', ['flights', 'trains'])
def test_transport_segments_and_adult_quote(provider, kind):
    response, _ = provider
    response['data']['itemList'] = [{'adultPrice': '¥20', 'journeys': [{'segments': [
        {'marketingTransportNo': 'G123', 'depCityName': '杭州', 'arrCityName': '上海'}]}]}]
    item = flyai.query(kind, params(kind))['items'][0]
    assert item['amount'] == 20 and item['segments'][0]['marketingTransportNo'] == 'G123'


@pytest.mark.parametrize('kind', ['flights', 'trains'])
def test_actual_transport_display_price_field(provider, kind):
    response, _ = provider
    response['data']['itemList'] = [{'price': '57.00', 'journeys': [{'segments': [
        {'marketingTransportNo': 'G123', 'depCityName': '杭州', 'arrCityName': '上海'}]}]}]
    item = flyai.query(kind, params(kind))['items'][0]
    assert item['amount'] == 57 and item['fare_basis'] == 'search_display_reference'


@pytest.mark.parametrize('payload', [
    {'city': '杭州', 'check_in': str(china_today()), 'check_out': str(china_today()+timedelta(days=1))},
    {'city': '杭州', 'check_in': 'bad', 'check_out': 'bad'},
    {'city': '杭州', 'check_in': str(china_today()+timedelta(days=2)), 'check_out': str(china_today()+timedelta(days=1))}])
def test_invalid_hotel_dates(payload):
    with pytest.raises(ValidationError):
        HotelQuery.model_validate(payload)


@pytest.mark.parametrize('delta', [0, -1])
def test_transport_requires_future_date(delta):
    with pytest.raises(ValidationError):
        TransportQuery(origin='杭州', destination='上海', departure_date=china_today()+timedelta(days=delta))


def test_authenticated_api_and_validation(provider):
    from app.api.main import app
    client = TestClient(app)
    for kind in ['attractions', 'hotels', 'flights', 'trains']:
        assert client.post('/api/travel/'+kind, json=params(kind)).status_code == 401
    app.dependency_overrides[identity.current_user] = lambda: 'test-user'
    try:
        assert client.get('/api/travel/status').json()['key_configured']
        for kind in ['attractions', 'hotels', 'flights', 'trains']:
            assert client.post('/api/travel/'+kind, json=params(kind)).json()['outcome']['status'] == 'ok'
        assert client.post('/api/travel/attractions', json={'city': '--config'}).status_code == 422
    finally:
        app.dependency_overrides.pop(identity.current_user, None)
