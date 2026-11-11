"""三次调用、带依据补查、反馈按原因重试；仅真实临时库和可控模型。"""

import json

import pytest

from deskpal.llm.errors import LLMError, RetryableLLMError
from deskpal.llm.models import LLMResponse, LLMUsage
from deskpal.tools.models import ToolCall
from deskpal.tasks.models import TaskStatus
from tests.integration.agent.test_role_context import app_model
from tests.integration.agent.test_multi_round_loop import Probe


def install(model, *, planning=None, feedback=None):
    calls = []
    def chat(messages, *, tools=()):
        system = messages[0].content
        calls.append((system, messages, tools))
        if system.startswith('[MEMORY_FEEDBACK]'):
            data = json.loads(messages[-1].content)
            return feedback(system, data) if feedback else LLMResponse('{}')
        if system.startswith('[EXPRESSION]'):
            return LLMResponse('我明白了，慢慢聊。')
        assert system.startswith('[PLANNING]')
        return planning(messages, tools) if planning else LLMResponse(json.dumps(dict(action='answer',reply='草稿',
            context=dict(entries=[dict(layer='interaction_approach',content='先听用户分享，不急着建议',reason='当前是在分享',
                nature='inferred',scope='turn')])),ensure_ascii=False), usage=LLMUsage(100,80,20,5),request_id='response_test_1')
    model.chat = chat
    return calls


def run(app, text):
    conversation = app.conversation_ensure_default()
    task = app.submit(text, conversation_id=conversation['id'],expected_context_epoch=conversation['context_epoch'])
    return app.wait(task.id,10)


def test_plain_turn_has_three_calls_and_keeps_actual_usage_and_processing_receipt(app_model):
    app, model = app_model
    calls = install(model)
    result = run(app,'今天想随便说说')
    assert result.status == TaskStatus.COMPLETED, result
    assert len(calls)==3
    assert not any('[CONTEXT_UNDERSTANDING]' in call[0] for call in calls)
    data=json.loads(calls[-1][1][-1].content)
    assert data['role_context']['interaction_approach'][0]['content']=='先听用户分享，不急着建议'
    assert data['learning_receipt']['status']=='completed'
    rows=[row for row in app.diagnostics()['model_calls'] if row['task_id']==result.task_id]
    assert len(rows)==3
    first=next(row for row in rows if row['purpose']=='planning')['metadata']
    assert first['input_tokens']==100 and first['cached_tokens']==80 and first['reasoning_tokens']==5
    assert first['request_id']=='response_test_1'
    assert all(row['started_at'] and row['finished_at'] and row['metadata']['request_bytes']>0 for row in rows)


def test_contextual_search_defers_native_effects_until_new_model_decision(app_model):
    app,model=app_model
    memory=app.memory_command('remember',content='雪杉项目预算763元')
    probe=Probe();app.tools.register(probe)
    count=[0]
    def plan(messages,tools):
        count[0]+=1
        if count[0]==1:
            return LLMResponse('',(ToolCall('turn_context',dict(entries=[],retrieval=dict(query='雪杉项目预算763元',sources=['request'])),'meta'),
                ToolCall('probe',{'value':'must-not-run'},'deferred')))
        if count[0]==2:
            assert '763' in str(messages)
            return LLMResponse('',(ToolCall('probe',{'value':'after-search'},'actual'),))
        return LLMResponse('完成')
    install(model,planning=plan)
    result=run(app,'继续处理那件事')
    assert result.status==TaskStatus.COMPLETED,result
    assert probe.calls==['after-search'] and count[0]==3
    assert (memory.id,memory.revision) in app.journal.get_plan(result.task_id).memory_refs


@pytest.mark.parametrize('failure',['network','validation'])
def test_feedback_retry_diagnostics_remain_after_success_and_correct_only_validation(app_model,failure):
    app,model=app_model
    attempts=[]
    def feedback(system,data):
        attempts.append(system)
        if len(attempts)==1:
            if failure=='network':raise RetryableLLMError('untrusted secret sk-12345678901234567890',code='timeout')
            return LLMResponse(json.dumps({'memories':[dict(content='想养猫',destination='facts',claim_type='user_report',reason='用户原话',
                quote='想养猫',sources=['user'],review_after='2026-11-03')]}))
        return LLMResponse('{}')
    install(model,feedback=feedback)
    result=run(app,'我想养猫')
    assert result.status==TaskStatus.COMPLETED,result
    assert len(attempts)==2
    assert ('CORRECTION' in attempts[1]) == (failure=='validation')
    rows=sorted([row for row in app.diagnostics()['model_calls'] if row['task_id']==result.task_id and row['purpose']=='memory_evaluation'],key=lambda row:row['slot'])
    assert len(rows)==2 and rows[1]['metadata']['processing_code']=='ok'
    assert rows[0]['metadata']['processing_code']==('timeout' if failure=='network' else 'time_invalid')
    if failure=='validation':
        assert 'memories[0].review_after' in attempts[1]
    else:
        assert rows[0]['status']=='unknown' and rows[0]['metadata']['retry_delay_ms']==250
    assert 'sk-' not in json.dumps(rows)


def test_learning_shape_diagnostics_survive_retry_in_the_actual_ledger(app_model):
    app, model = app_model
    attempts = []
    def feedback(system, data):
        attempts.append(system)
        if len(attempts) > 1:
            return LLMResponse('{}')
        return LLMResponse(json.dumps({'memories': [dict(content='合成事实', destination='facts',
            claim_type='user_report', reason='合成原因', quote='合成事实', sources=['user'], status='不应写入日志的正文')]}))
    install(model, feedback=feedback)
    result = run(app, '合成事实')
    assert result.status == TaskStatus.COMPLETED
    rows = sorted([row for row in app.diagnostics()['model_calls']
        if row['task_id'] == result.task_id and row['purpose'] == 'memory_evaluation'], key=lambda row: row['slot'])
    assert len(rows) == 2
    first, second = [row['metadata'] for row in rows]
    assert first['processing_extra_fields'] == ['status']
    assert first['processing_missing_fields'] == []
    assert first['processing_stage'] == 'validate' and first['learning_attempt'] == 1
    assert second['processing_code'] == 'ok' and second['learning_attempt'] == 2
    assert second['processing_stage'] == 'commit' and second['learning_correction'] == 1
    assert '"processing_extra_fields":["status"]' in attempts[1]
    assert first['learning_kind'] == 'turn' and first['learning_attempt_limit'] == 3
    assert '不应写入日志' not in json.dumps(rows, ensure_ascii=False)


def test_foreground_feedback_excludes_unrelated_old_understandings(app_model):
    app,model=app_model
    related=app.memory_command('remember',content='雪杉项目预算763元')
    unrelated=app.memory_command('remember',content='海岛旅行喜欢住靠海的房间')
    seen=[]
    install(model,feedback=lambda system,data:(seen.append(data),LLMResponse('{}'))[1])
    result=run(app,'雪杉项目预算是多少')
    assert result.status==TaskStatus.COMPLETED,result
    ids={row['id'] for row in seen[0]['understandings']}
    assert related.id in ids and unrelated.id not in ids
    assert all('refs' not in row and 'use_policy' not in row for row in seen[0]['catalog'].values())
    assert all(set(row)=={'source'} for row in seen[0]['published_history'])
    assert 'request' not in seen[0]['current_exchange']


def test_storage_failure_is_not_retried_by_regenerating_model_proposal(app_model,monkeypatch):
    app,model=app_model
    calls=[]
    install(model,feedback=lambda system,data:(calls.append(data),LLMResponse('{}'))[1])
    monkeypatch.setattr(app.learning,'_commit',lambda *args:(_ for _ in ()).throw(OSError('disk unavailable')))
    result=run(app,'随便聊聊')
    assert result.status==TaskStatus.COMPLETED and len(calls)==1
    row=next(row for row in app.diagnostics()['model_calls'] if row['task_id']==result.task_id and row['purpose']=='memory_evaluation')
    assert row['metadata']['processing_code']=='commit_failed'


@pytest.mark.parametrize('code,status',[('http_error',401),('output_limit',None)])
def test_permanent_provider_failure_does_not_repeat_identical_learning_request(app_model,code,status):
    app,model=app_model
    attempts=[]
    def feedback(system,data):
        attempts.append(data)
        raise LLMError('untrusted provider body',code=code,http_status=status)
    calls=install(model,feedback=feedback)
    result=run(app,'随便聊聊')
    assert result.status==TaskStatus.COMPLETED and len(attempts)==1 and len(calls)==3
    expression=json.loads(calls[-1][1][-1].content)
    assert expression['learning_receipt']['status']=='failed'
    row=next(row for row in app.diagnostics()['model_calls'] if row['purpose']=='memory_evaluation')
    assert row['metadata']['error_code']==code and row['metadata']['processing_code']==code
