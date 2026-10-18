"""Recall/citation ownership, online first-attempt health and final-delivery guardrails."""
from datetime import date, timedelta

import pytest

from app.models.dialog import DocumentInput
from app.services import delivery, knowledge, monitoring, store, tools
from tests.factories import make_attraction, make_day, make_request


def test_retrieval_rewrites_chunks_and_preserves_private_citations():
    text = '旅行注意事项。' * 75 + '退款规则：预约成功后可以申请退款。' + '结束。' * 100
    identifier = knowledge.add_document('alice', DocumentInput(title='预约退款须知', source='official-guide.txt', content=text))
    results, stats = knowledge.retrieve('alice', '退钱')
    assert results and stats['variants'] == ['退钱', '退款']
    assert results[0]['document_id'] == identifier and results[0]['source'] == 'official-guide.txt'
    assert any('退款规则' in c['content'] for c in results)
    assert knowledge.retrieve('bob', '退款')[0] == []
    reply, citations = knowledge.answer('alice', '退款')
    assert citations and all(c['content'] in reply for c in citations)
    assert knowledge.answer('alice', '火星签证费用')[1] == []
    with store._connect() as db:
        db.execute('DELETE FROM knowledge_document WHERE document_id=?', (identifier,))
    assert knowledge.retrieve('alice', '退款')[0] == []


def test_expired_documents_never_support_answers():
    expired = (date.today()-timedelta(days=1)).isoformat()
    knowledge.add_document('alice', DocumentInput(title='过期预约规则', source='old', content='预约后可退款', valid_until=expired))
    assert knowledge.retrieve('alice', '预约退款')[0] == []
    with pytest.raises(Exception):
        knowledge.add_document('alice', DocumentInput(title='错误日期', source='invalid', content='预约', valid_until='not-date'))


def test_rrf_recalls_title_and_body_without_mixing_raw_scores():
    knowledge.add_document('u', DocumentInput(title='轮椅无障碍通道', source='access.txt', content='请从南门进入，无需台阶。'))
    knowledge.add_document('u', DocumentInput(title='入园须知', source='entry.txt', content='园区提供无障碍坡道，轮椅可以通行。'))
    hits, stats = knowledge.retrieve('u', '轮椅')
    assert {h['source'] for h in hits} == {'access.txt', 'entry.txt'}
    assert stats['candidate_count'] == 2


def test_monitor_does_not_hide_sixty_retries_behind_final_success():
    calls = []
    for i in range(100):
        outcomes = [{'status': 'ok'}] if i >= 60 else [{'status': 'error', 'error_code': 'UPSTREAM_RATE_LIMITED' if i < 50 else 'UPSTREAM_TIMEOUT'}, {'status': 'ok'}]
        calls.append({'tool': 'search_attractions', 'tool_call_id': str(i), 'outcome': {'status': 'ok'},
                      'attempts': [{'outcome': o, 'duration_ms': 1} for o in outcomes]})
    monitoring.record('actual-fixture', {'tool_calls': calls}, True, 100)
    result = monitoring.summary()
    assert result['final_success_rate'] == 1 and result['first_success_rate'] == .4
    assert result['retry_rate'] == .6 and result['tool_attempts'] == 160
    assert result['error_codes'] == {'UPSTREAM_RATE_LIMITED': 50, 'UPSTREAM_TIMEOUT': 10}
    assert {a['code'] for a in result['alerts']} == {'HIGH_RETRY_RATE', 'UPSTREAM_RATE_LIMITED'}
    assert monitoring.summary()['sample_runs'] == 1
    monitoring.record('actual-fixture', {'tool_calls': calls}, True, 100)
    assert monitoring.summary()['sample_runs'] == 1


def test_run_logs_idempotent_and_no_data_is_not_invented_health():
    assert monitoring.summary()['status'] == 'no_data'
    for _ in range(2):
        store.log_run(thread_id='same', fingerprint='same', cached=False, success=True, elapsed_ms=3)
    assert store.stats()['total_runs'] == 1


@pytest.mark.parametrize('args', [{'keyword': 2}, {'keyword': '景点', 'limit': []}, {'keyword': '景点', 'extra': 'injection'}])
def test_bad_tool_arguments_are_non_retryable_and_never_reach_adapter(args, monkeypatch):
    monkeypatch.setattr(tools.amap, 'search_poi', lambda *a, **k: pytest.fail('invalid input reached map'))
    result = tools.execute('attractions', 'search_attractions', args, tools.ToolContext(city='杭州', adcode='330100', start_date='2026-10-10', travel_days=1, nights=0))
    assert result.error_code == 'INVALID_ARGUMENT' and not result.retryable


def test_delivery_rechecks_day_count_and_candidate_id_spoofing():
    req = make_request(travel_days=1, nights=0, must_visit=[])
    spot = make_attraction('西湖')
    payload = {'city': req.city, 'start_date': req.start_date, 'end_date': req.start_date,
               'hotel_nights': 0, 'days': [make_day(0, req.start_date, [spot])]}
    assert delivery.audit(payload, req, {'attractions': [spot]})[1] == []
    payload['days'][0]['attractions'][0]['poi_id'] = 'invented-id'
    assert any('不在候选' in error for error in delivery.audit(payload, req, {'attractions': [spot]})[1])
    payload['days'] = []
    assert any('days' in error for error in delivery.audit(payload, req)[1])
    assert delivery.audit({**payload, 'days': 'not-list'}, req)[0] is None


@pytest.mark.parametrize('arguments', ['[]', '{', 'null', {'invalid': 'object-instead-of-json-string'}])
def test_invalid_tool_json_is_not_silently_replaced_by_valid_empty_arguments(arguments, monkeypatch):
    from app.agents import react_loop
    turns = iter([{'content': '', 'tool_calls': [{'id': 'bad', 'function': {'name': 'get_weather', 'arguments': arguments}}]}, {'content': 'done'}])
    monkeypatch.setattr(react_loop, 'chat_with_tools', lambda *a: (next(turns), {'llm_calls': 1}))
    monkeypatch.setattr(tools.amap, 'get_weather', lambda *a: pytest.fail('malformed JSON triggered a request'))
    ctx = tools.ToolContext(city='杭州', adcode='330100', start_date='2026-10-10', travel_days=1, nights=0)
    react_loop.run_agent('weather', 'system', 'user', ctx)
    assert ctx.calls[0]['outcome']['error_code'] == 'INVALID_ARGUMENT'
