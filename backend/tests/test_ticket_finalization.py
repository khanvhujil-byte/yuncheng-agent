import json
from time import monotonic

import httpx
import pytest

from app.services import monitoring, planning, store, ticket_pricing, ticket_web
from tests.test_api import _final_state
from tests.test_ticket_web import trip
from tests.factories import make_request


def report(run):
    with store._connect() as db:
        return json.loads(db.execute('SELECT report_json FROM quality_run WHERE run_id=?', (run,)).fetchone()[0])


@pytest.mark.parametrize('valid', [True, False])
def test_real_web_trace_crosses_ticket_refresh_and_delivery(monkeypatch, settings, valid):
    monkeypatch.setattr(settings, 'official_ticket_lookup_enabled', True)
    monkeypatch.setattr(settings, 'ticket_web_search_enabled', True)
    monkeypatch.setattr(ticket_web, '_public', lambda url: None)
    def get(url, **kwargs):
        html = ('<h2><a href="https://www.sz.gov.cn/tickets">门票</a></h2>' if 'bing.com' in url
                else '<p>深圳测试景区成人票88元。</p>')
        return httpx.Response(200, text=html, request=httpx.Request('GET', url))
    monkeypatch.setattr(httpx, 'get', get)
    req, plan = trip()
    runtime = {}
    refreshed = ticket_pricing.refresh_plan_tickets(plan, req, lookup_state=runtime)
    assert refreshed['days'][0]['attractions'][0]['ticket_price'] == 88
    final = _final_state()
    final['tool_calls'] = runtime['_ticket_search_calls']
    if not valid:
        final['validate_error'] = '候选池外景点'
    response = planning._finalize(final, make_request(), 'web-delivery', 'fp', 10)
    assert response.success is valid
    if not valid:
        assert '候选池外景点' in response.message
    metrics = report('web-delivery')
    assert metrics['tools'][0]['attempt_count'] == 1
    assert metrics['tools'][0]['http_request_count'] == 2
    assert metrics['errors'] == {}
    assert monitoring.summary()['retry_rate'] == 0


@pytest.mark.parametrize('status', ['ok', 'WEB_EVIDENCE_UNRESOLVED'])
def test_persisted_url_only_attempts_do_not_crash_or_fabricate_http_success(status):
    final = _final_state()
    final['tool_calls'] = [{'tool':'search_ticket_web', 'outcome':{'status':status},
                            'attempts':[{'url':'https://www.sz.gov.cn/tickets'}]}]
    final = json.loads(json.dumps(final, default=store._json_default))
    response = planning._finalize(final, make_request(), 'legacy-web', 'legacy-fp', 10)
    assert response.success
    metrics = report('legacy-web')
    assert metrics['trace_warnings']
    tool = metrics['tools'][0]
    assert tool['http_unknown_count'] == 1
    assert tool['success'] is (status == 'ok')
    assert tool['lookup_error_code'] == (None if status == 'ok' else status)


def test_http_429_and_semantic_miss_remain_distinct(monkeypatch):
    monkeypatch.setattr(ticket_web, '_public', lambda url: None)
    monkeypatch.setattr(httpx, 'get', lambda url, **kw: httpx.Response(429, request=httpx.Request('GET', url)))
    attempts = []
    with pytest.raises(ValueError, match='HTTP_429'):
        ticket_web._get('https://www.sz.gov.cn/tickets', monotonic()+10, requests=attempts)
    outcome = {'status':'error','error_code':'WEB_EVIDENCE_UNRESOLVED','stats':{'upstream_calls':1}}
    monitoring.record('429', {'tool_calls':[{'tool':'search_ticket_web','outcome':outcome,
        'attempts':[{'outcome':outcome}], 'http_attempts':attempts}]}, False, 10)
    metrics = report('429')
    assert metrics['errors']['UPSTREAM_RATE_LIMITED'] == 1
    assert metrics['tools'][0]['lookup_error_code'] == 'WEB_EVIDENCE_UNRESOLVED'


def test_monitor_failure_preserves_delivery_and_surfaces_warning(monkeypatch):
    def fail(*args):
        raise OSError('monitor database unavailable')
    monkeypatch.setattr(planning, 'record', fail)
    response = planning._finalize(_final_state(), make_request(), 'monitor-down', 'fp-down', 10)
    assert response.success and response.data
    assert any('运行监控记录失败' in note for note in response.warnings)


def test_finalize_only_resume_of_persisted_legacy_trace_never_rebuilds_graph(monkeypatch):
    from app.api.routes import trip as api
    from app.services import runner
    req = make_request()
    final = _final_state()
    final.update(validate_error='餐厅超过4公里', budget_verdict={'status':'none'})
    final['tool_calls'] = [{'tool':'search_ticket_web','outcome':{'status':'WEB_SEARCH_FAILED'},
                            'attempts':[{'url':'https://www.sz.gov.cn/tickets'}]}]
    store.save_run('persisted-ticket-crash', req)
    store.journal_node('persisted-ticket-crash', 'verify_budget', 1, final)
    monkeypatch.setattr(api, 'build_graph', lambda **kw: pytest.fail('must only finalize'))
    result = api.resume_plan('persisted-ticket-crash')
    assert result.data and not result.success
    assert '餐厅超过4公里' in result.message
    assert runner.get_job('persisted-ticket-crash').status == 'done'
    assert api.resume_plan('persisted-ticket-crash') == result


def test_choice_retry_contains_rejected_layout_and_computed_options(monkeypatch):
    from app.agents import planner_agent
    from tests.test_unified_planning import scenario
    state, output = scenario()
    for meal in output['days'][0]['meals']:
        meal['restaurant_id'] = 'R32'
    monkeypatch.setattr(planner_agent, 'chat_json', lambda *a, **kw: (json.dumps(output), {'llm_calls':1}))
    first = planner_agent.compose_plan(state)
    assert 'eligible_restaurant_ids' in first['validate_error']
    assert 'previous_meals' in first['validate_error'] and 'H2' in first['validate_error']
    def corrected(system, prompt, **kw):
        assert 'eligible_restaurant_ids' in kw['extra_user']
        for i, meal in enumerate(output['days'][0]['meals']):
            meal['restaurant_id'] = f'R{i}'
        return json.dumps(output), {'llm_calls':1}
    monkeypatch.setattr(planner_agent, 'chat_json', corrected)
    second = planner_agent.compose_plan({**state, **first})
    assert second['validate_error'] is None and second['retries'] == 2
