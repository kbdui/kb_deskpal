"""合成临时档案：已知校验问题经过超时后仍进入下一次纠错。"""
from datetime import datetime, timedelta, timezone
import json
import pytest

from deskpal.llm.errors import RetryableLLMError
from tests.integration.agent.test_feedback_learning import app_model, draft, empty, turn


def recording(model):
    original = model.chat
    systems = []
    def chat(messages, *, tools=()):
        if messages[0].content.startswith('[MEMORY_FEEDBACK]'):
            systems.append(messages[0].content)
        return original(messages, tools=tools)
    model.chat = chat
    return systems


@pytest.mark.parametrize('final_timeout', [False, True])
def test_invalid_review_time_then_timeout_keeps_correction_and_refreshes_now(app_model, final_timeout):
    app, model = app_model
    clock = [datetime.now(timezone.utc)]
    app.proactive.clock = lambda: clock[0]
    systems = recording(model)
    def feedback(data):
        attempt = len(model.inputs)
        if attempt == 2:
            clock[0] += timedelta(minutes=2)
            raise RetryableLLMError('synthetic timeout', code='timeout')
        if attempt == 3 and final_timeout:
            raise RetryableLLMError('synthetic timeout', code='timeout')
        row = draft('用户正在学绘画', '最近在学绘画')
        row['review_after'] = None if attempt == 1 else (datetime.fromisoformat(data['now']) + timedelta(days=1)).isoformat()
        return dict(memories=[row])
    model.fn = feedback
    result = turn(app, '最近在学绘画')
    assert len(systems) == 3
    assert '[CORRECTION]' not in systems[0]
    for system in systems[1:]:
        assert '[CORRECTION]' in system
        assert 'memories[0].review_after' in system
        assert '"processing_rule":"review_schedule"' in system
    assert datetime.fromisoformat(model.inputs[2]['now']) > datetime.fromisoformat(model.inputs[1]['now'])
    assert app.learning.store.job('turn:' + result.task_id)['status'] == ('failed' if final_timeout else 'completed')
    assert len(app.memory.list_items()) == (0 if final_timeout else 1) and len(model.expressions) == 1
    rows = sorted([row for row in app._humanization.call_diagnostics() if row['task_id'] == result.task_id
                   and row['purpose'] == 'memory_evaluation'], key=lambda row: row['slot'])
    assert [row['metadata']['learning_correction'] for row in rows] == [0, 1, 1]
    assert rows[1]['metadata']['processing_code'] == 'timeout'
    assert rows[2]['metadata']['learning_outcome'] == ('provider_failure' if final_timeout else 'saved')


def test_incomplete_update_then_timeout_keeps_exact_missing_fields(app_model):
    app, model = app_model
    model.fn = lambda data: dict(memories=[draft('最初的理解', '最初依据')])
    turn(app, '最初依据')
    item = app.memory.list_items()[0]
    model.inputs.clear()
    systems = recording(model)
    def feedback(data):
        attempt = len(model.inputs)
        if attempt == 1:
            return dict(reviews=[dict(id=item.id, decision='update', reason='根据新依据修订')])
        if attempt == 2:
            raise RetryableLLMError('synthetic timeout', code='timeout')
        row = draft('修订后的理解', '新的依据')
        row.update(id=item.id, decision='update', archive_after=item.draft.archive_after.isoformat())
        return dict(reviews=[row])
    model.fn = feedback
    result = turn(app, '新的依据')
    assert len(systems) == 3
    for system in systems[1:]:
        assert '[CORRECTION]' in system and 'reviews[0]' in system
        details = json.loads(system.split('仅修正以下宿主诊断：', 1)[1].split('。missing_fields', 1)[0])
        assert details['processing_missing_fields'] == ['claim_type', 'content', 'destination', 'quote', 'sources']
    updated = app.memory.repository.get(item.id)
    assert updated.revision == item.revision + 1 and updated.draft.content == '修订后的理解'
    assert app.learning.store.job('turn:' + result.task_id)['status'] == 'completed'


def test_timeouts_without_a_prior_validation_error_do_not_invent_corrections(app_model):
    app, model = app_model
    systems = recording(model)
    def feedback(data):
        if len(model.inputs) < 3:
            raise RetryableLLMError('synthetic timeout', code='timeout')
        return empty()
    model.fn = feedback
    result = turn(app, '合成请求')
    assert len(systems) == 3 and all('[CORRECTION]' not in system for system in systems)
    assert app.learning.store.job('turn:' + result.task_id)['attempts'] == 3
