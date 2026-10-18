"""Offline integration coverage for identity, durable turns, memory and recovery."""
from concurrent.futures import ThreadPoolExecutor
import json
import time

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app.api.main import app
from app.models.dialog import TurnInput
from app.models.schemas import TripPlanResponse
from app.services import conversation, dialog_store, identity, planning, store
from tests.factories import make_request


@pytest.fixture
def user():
    return identity.register("traveller", "valid-password")['user_id']


@pytest.fixture
def http():
    return TestClient(app)


def command(revision, *, action="message", request_id=None, **extra):
    return TurnInput(expected_revision=revision, request_id=request_id or f"request-{revision}", action=action, **extra)


def test_login_expiry_revocation_and_owner_isolation(http):
    for username in ("alice", "bob"):
        assert http.post('/api/auth/register', json={"username": username, "password": "valid-password"}).status_code == 200
    token = http.post('/api/auth/login', json={"username": "alice", "password": "valid-password"}).json()['token']
    headers = {'X-Session-Token': token}
    created = http.post('/api/conversations', headers=headers).json()
    bob = http.post('/api/auth/login', json={"username": "bob", "password": "valid-password"}).json()['token']
    assert http.get('/api/conversations/' + created['conversation_id'], headers={'X-Session-Token': bob}).status_code == 404
    assert http.post('/api/auth/logout', headers=headers).status_code == 200
    assert http.get('/api/auth/me', headers=headers).status_code == 401
    with store._connect() as db:
        db.execute('UPDATE auth_session SET expires_at=?', (time.time()-1,))
    assert http.get('/api/auth/me', headers={'X-Session-Token': bob}).status_code == 401
    assert http.post('/api/auth/login', json={"username": "alice", "password": "incorrect-password"}).status_code == 401


def test_preference_versions_explicit_write_and_revoke(user):
    dialog_store.write_preference(user, 'transportation', '自驾', 0, '用户明确保存')
    with pytest.raises(HTTPException) as exc:
        dialog_store.write_preference(user, 'transportation', '步行', 0, '冲突')
    assert exc.value.status_code == 409
    c = dialog_store.create_conversation(user, conversation.defaults(user))
    result = conversation.turn(c['conversation_id'], user, command(0, slot_updates={'transportation': '公共交通'}))
    assert result['slots']['transportation'] == '公共交通'
    assert conversation.defaults(user)['transportation'] == '自驾'
    assert conversation.defaults('other-user') == {}
    dialog_store.write_preference(user, 'transportation', None, 1, '用户撤回', active=False)
    assert conversation.defaults(user) == {}
    with store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM preference_event').fetchone()[0] == 2


def test_missing_fields_then_confirm_idempotent_and_failed_modification_retains_original(user, monkeypatch):
    c = dialog_store.create_conversation(user, {})
    identifier = c['conversation_id']
    result = conversation.turn(identifier, user, command(0, slot_updates={'city': '杭州'}))
    assert not result['ready'] and '出发日期' in result['reply']
    slots = make_request(must_visit=[]).model_dump()
    result = conversation.turn(identifier, user, command(1, slot_updates=slots))
    assert result['ready'] and result['result'] is None
    calls = []
    def generate(req, **kwargs):
        calls.append((req, kwargs))
        return TripPlanResponse(success=len(calls) == 1, thread_id=kwargs['run_id'], message='超预算' if len(calls)>1 else '')
    monkeypatch.setattr(planning, 'plan', generate)
    confirmed = command(2, action='confirm')
    original = conversation.turn(identifier, user, confirmed)
    assert conversation.turn(identifier, user, confirmed) == original
    assert len(calls) == 1 and calls[0][1]['cache_scope'] == user
    edited = conversation.turn(identifier, user, command(3, slot_updates={'budget_limit': 1}))
    assert edited['result'] == original['result']
    failed = conversation.turn(identifier, user, command(4, action='confirm'))
    assert failed['candidate_result']['success'] is False
    assert failed['result'] == original['result']
    rejected = conversation.turn(identifier, user, command(5, action='reject'))
    assert rejected['slots'].get('budget_limit') is None
    assert rejected['result'] == original['result']
    with store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM conversation_version').fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM owned_run').fetchone()[0] == 2


def test_same_id_different_content_and_revision_conflicts(user):
    identifier = dialog_store.create_conversation(user, {})['conversation_id']
    conversation.turn(identifier, user, command(0, slot_updates={'city': '杭州'}))
    for cmd in (command(0, slot_updates={'city': '上海'}), command(0, request_id='new-request', slot_updates={'city': '上海'})):
        with pytest.raises(HTTPException) as exc:
            conversation.turn(identifier, user, cmd)
        assert exc.value.status_code == 409


def test_parallel_claim_has_only_one_winner(user):
    identifier = dialog_store.create_conversation(user, {})['conversation_id']
    def claim(i):
        try:
            dialog_store.claim(identifier, user, 0, f'request-{i}', str(i))
            return True
        except HTTPException:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(claim, range(2))) == 1


def test_crash_recovery_never_implicitly_restarts_unknown_generation(user, monkeypatch):
    identifier = dialog_store.create_conversation(user, make_request().model_dump())['conversation_id']
    def fail(*args, **kwargs):
        raise RuntimeError('process interrupted after upstream call')
    monkeypatch.setattr(planning, 'plan', fail)
    cmd = command(0, action='confirm')
    with pytest.raises(RuntimeError):
        conversation.turn(identifier, user, cmd)
    dialog_store.recover_interrupted()
    with pytest.raises(HTTPException) as exc:
        conversation.turn(identifier, user, cmd)
    assert exc.value.status_code == 409
    assert not dialog_store.load_conversation(identifier, user)['busy']


def test_completed_candidate_recovered_without_regeneration(user, monkeypatch):
    identifier = dialog_store.create_conversation(user, make_request().model_dump())['conversation_id']
    generated = []
    monkeypatch.setattr(planning, 'plan', lambda req, **kw: generated.append(kw) or TripPlanResponse(success=True, thread_id=kw['run_id']))
    finish = dialog_store.finish
    monkeypatch.setattr(dialog_store, 'finish', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('commit lost')))
    cmd = command(0, action='confirm')
    with pytest.raises(RuntimeError):
        conversation.turn(identifier, user, cmd)
    dialog_store.recover_interrupted()
    monkeypatch.setattr(dialog_store, 'finish', finish)
    assert conversation.turn(identifier, user, cmd)['result']['success']
    assert len(generated) == 1


def test_slot_invalidations_do_not_drop_unrelated_hard_constraints():
    slots = make_request(nights=2, end_date='2026-10-12', fixed_hotel='已订酒店').model_dump()
    budget = conversation.merge(slots, {'budget_limit': 1000})
    assert budget['must_visit'] == ['西湖'] and budget['fixed_hotel'] == '已订酒店'
    changed = conversation.merge(slots, {'city': '上海', 'start_date': '2026-10-20'})
    assert not any(k in changed for k in ('must_visit', 'fixed_hotel', 'fixed_hotel_address', 'nights', 'end_date'))


def test_invalid_intent_retains_slots_and_releases_lock(user, monkeypatch):
    identifier = dialog_store.create_conversation(user, {'city': '杭州'})['conversation_id']
    monkeypatch.setattr(conversation.llm, 'chat_json', lambda *a: ('{"intent":"delete_everything"}', {}))
    with pytest.raises(HTTPException) as exc:
        conversation.turn(identifier, user, command(0, message='换个地方'))
    assert exc.value.status_code == 502
    current = dialog_store.load_conversation(identifier, user)
    assert current['slots'] == {'city': '杭州'} and current['revision'] == 0 and not current['busy']


def test_fact_reuse_ttl_date_and_candidate_changes(user):
    from tests.factories import make_attraction, make_weather
    req = make_request()
    state = {'request': req.model_dump(), 'adcode': '330100', 'attractions': [make_attraction('西湖').model_dump()],
             'weather': [make_weather(req.start_date).model_dump()], 'sources': {'attractions': 'amap', 'weather': 'amap'},
             'usage': {'prompt_tokens': 100}, 'validate_error': 'old error'}
    with store._connect() as db:
        db.execute('INSERT INTO run_snapshot VALUES(?,?,?)', ('run-a', json.dumps(state), time.time()))
    seed = conversation.reusable_seed({'thread_id': 'run-a'}, req.model_copy(update={'budget_limit': 100}))
    assert seed['attractions'] and 'usage' not in seed and 'validate_error' not in seed
    assert conversation.reusable_seed({'thread_id': 'run-a'}, req.model_copy(update={'start_date': '2026-10-20'})) is None
    assert conversation.reusable_seed({'thread_id': 'run-a'}, req.model_copy(update={'preferences': ['美食']})) is None
    with store._connect() as db:
        db.execute('UPDATE run_snapshot SET recorded_at=?', (time.time()-301,))
    assert conversation.reusable_seed({'thread_id': 'run-a'}, req) is None
