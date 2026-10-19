import copy
import json
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from app.services import ticket_estimates, execution_lease, store, delivery_logging
from tests.factories import make_request


@pytest.fixture
def ticket_model(monkeypatch, settings):
    monkeypatch.setattr(settings, 'ticket_model_estimate_enabled', True)
    monkeypatch.setattr(settings, 'llm_api_key', 'sk-test')
    return settings


def unknown_plan():
    return {'days':[{'day_index':0,'date':'2026-10-20','attractions':[
        {'poi_id':'P1','name':'曲院风荷','ticket_price':None,'ticket_price_source':'unknown'},
        {'poi_id':'P2','name':'城隍阁','ticket_price':None,'ticket_price_source':'unknown'}]}]}


def test_model_ticket_batch_budget_and_cache(ticket_model, monkeypatch):
    calls=[]
    def model(*args, **kwargs):
        calls.append(args)
        return json.dumps({'prices':[{'id':'0','amount':0,'basis':'普通公园入场参考'}, {'id':'1','amount':30,'basis':'成人日常票参考'}]}), {'llm_calls':1,'prompt_tokens':11,'completion_tokens':22}
    monkeypatch.setattr(ticket_estimates.llm,'chat_json',model)
    state={}
    first=ticket_estimates.fill(unknown_plan(),make_request(),state)
    spots=first['days'][0]['attractions']
    assert [s['ticket_price'] for s in spots]==[0,30]
    assert all(s['ticket_price_source']=='llm_estimated' and s['ticket_price_evidence_url'] is None for s in spots)
    assert state['_ticket_usage']['completion_tokens']==22
    ticket_estimates.fill(unknown_plan(),make_request(),{})
    assert len(calls)==1
    from app.agents.budget import patch_budget
    from tests.factories import make_day, make_attraction
    day=make_day(0,'2026-10-20',[make_attraction('a',ticket_price=30,ticket_price_status='estimated',ticket_price_source='llm_estimated')])
    result=patch_budget({'request':make_request(nights=0,travel_days=1), 'plan':{'days':[day]},'nights':0,'travel_days':1})
    assert result['plan']['budget']['total_attractions']==30


@pytest.mark.parametrize('bad', [True,-1,50000,'30',None])
def test_bad_model_prices_cannot_become_quotes(ticket_model, monkeypatch,bad):
    monkeypatch.setattr(ticket_estimates.llm,'chat_json',lambda *a,**k:(json.dumps({'prices':[{'id':'0','amount':bad,'basis':'guess'}]}),{'llm_calls':1}))
    result=ticket_estimates.fill(unknown_plan(),make_request(),{})
    assert all(s['ticket_price_source']=='static_estimate' and s['ticket_price']>0 for s in result['days'][0]['attractions'])


def test_model_failure_once_preserves_sourced_price(ticket_model,monkeypatch):
    calls=[]
    def fail(*a,**k):
        calls.append(1)
        raise ticket_estimates.llm.LlmUnreachable('timeout')
    monkeypatch.setattr(ticket_estimates.llm,'chat_json',fail)
    plan=unknown_plan()
    plan['days'][0]['attractions'][0].update(ticket_price=40,ticket_price_status='known',ticket_price_source='imported')
    state={}
    first=ticket_estimates.fill(copy.deepcopy(plan),make_request(),state)
    ticket_estimates.fill(copy.deepcopy(plan),make_request(),state)
    assert first['days'][0]['attractions'][0]['ticket_price']==40
    assert len(calls)==1 and state['_ticket_usage']['llm_calls']==1


def test_lease_cross_process_exclusion_and_expiry(temp_db):
    lease=execution_lease.Lease('run:test').acquire()
    try:
        code="from pathlib import Path; from app.services import store,execution_lease; store.DB_PATH=Path(%r); execution_lease.Lease('run:test').acquire()" % str(temp_db)
        child=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=15)
        assert child.returncode != 0 and 'Busy' in child.stderr
        with store._connect() as db:
            db.execute('UPDATE execution_lease SET expires=0 WHERE key=?',('run:test',))
        replacement=execution_lease.Lease('run:test').acquire()
        try:
            with pytest.raises(execution_lease.Busy):
                lease.check()
            lease.release()
            replacement.check()
        finally:
            replacement.release()
    finally:
        lease.release()


def test_monitor_failure_preserves_business_result(monkeypatch):
    monkeypatch.setattr(delivery_logging.monitoring,'record',lambda *a:(_ for _ in ()).throw(OSError('full')))
    warnings=[]
    delivery_logging.record('run',{},False,0,warnings)
    assert warnings


def test_production_session_auth_rejects_forged_identity(settings,monkeypatch):
    from app.api.main import app
    from app.services import identity
    monkeypatch.setattr(settings,'deployment_mode','production')
    identity.register('launch-test','long-password-123')
    token=identity.login('launch-test','long-password-123')['token']
    client=TestClient(app)
    assert client.get('/api/auth/community',headers={'X-User-Id':'99'}).status_code==401
    one=client.get('/api/auth/community',headers={'X-Session-Token':token}).json()
    two=client.get('/api/auth/community',headers={'X-Session-Token':token,'X-User-Id':'999'}).json()
    assert one==two and one['userId']>0
    assert client.post('/api/community/publish',json={'plan':{}},headers={'X-User-Id':'1'}).status_code==401


def test_production_introspection_limits_accounts_not_the_java_host(settings,monkeypatch):
    from app.api.main import app
    from app.services import identity
    monkeypatch.setattr(settings,'deployment_mode','production')
    monkeypatch.setattr(settings,'rate_limit_per_minute',1)
    monkeypatch.setattr(settings,'read_rate_limit_per_minute',1)
    tokens=[]
    for name in ('limit-one','limit-two'):
        identity.register(name,'password-123')
        tokens.append(identity.login(name,'password-123')['token'])
    client=TestClient(app)
    for token in tokens:
        assert client.get('/api/auth/community',headers={'X-Session-Token':token}).status_code==200
    assert client.get('/api/auth/community',headers={'X-Session-Token':tokens[0]}).status_code==429


def test_limits_survive_rounds_and_new_resume_routes(settings):
    from app.agents.graph import route_after_budget, route_after_plan
    from app.services.runner import can_resume_from
    state={'unified_planning':True,'experience_enabled':True,'composition_valid':True,'validate_error':'route too long',
           'planning_calls':2,'planning_started_at':time.time(),'plan':{'days':[{}]},'retries':2}
    assert route_after_plan(state)=='next'
    assert route_after_budget(state)=='repair'
    # 'repair' passes through its own bounded entry (prepare_repair increments
    # repair_rounds and resets retries); resume must follow the same graph edge.
    assert can_resume_from(['verify_budget'],state)=='prepare_repair'
    assert can_resume_from(['enrich_experience'],state)=='patch_budget'
    state['planning_calls']=settings.planning_max_calls
    assert route_after_budget(state)=='next'


def test_old_lease_cannot_overwrite_persisted_result():
    from app.services.execution_lease import current_lease
    first=execution_lease.Lease('run:fenced').acquire()
    token=current_lease.set(first)
    try:
        store.save_job('fenced',status='running',events=[])
        with store._connect() as db:
            db.execute('UPDATE execution_lease SET expires=0 WHERE key=?',('run:fenced',))
        second=execution_lease.Lease('run:fenced').acquire()
        try:
            replacement=current_lease.set(second)
            store.save_job('fenced',status='done',events=[],result={'success':True})
            current_lease.reset(replacement)
            with pytest.raises(execution_lease.Busy):
                store.save_job('fenced',status='error',events=[])
            assert store.load_job('fenced')['status']=='done'
        finally:
            second.release()
    finally:
        current_lease.reset(token)
        first.release()


def test_planning_clock_does_not_restart_on_replay():
    from app.services.node_timing import measured
    started=time.time()-30
    node=measured('compose_plan',lambda s:{'plan':{'days':[{}]}})
    update=node({'planning_started_at':started,'planning_calls':3})
    assert update['planning_started_at']==started and update['planning_calls']==4


def test_registry_failed_refresh_does_not_extend_evidence(monkeypatch):
    from app.services import ticket_sources,ticket_web
    ticket_sources.register('杭州','P1','曲院风荷','https://hangzhou.gov.cn/price')
    monkeypatch.setattr(ticket_web,'_get',lambda *a,**k:(_ for _ in ()).throw(TimeoutError()))
    assert ticket_sources.refresh()[0]['status']=='TimeoutError'
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='price_observation'").fetchone()[0]==0
