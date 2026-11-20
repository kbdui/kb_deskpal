import json
from deskpal.llm.models import LLMResponse
from tests.integration.agent.test_feedback_learning import app_model, turn, empty, draft
from tests.integration.agent.test_unified_memory_management import install, intent, report


def test_failed_learning_is_known_to_expression_and_not_replaced_by_successful_plan(app_model):
    app, model = app_model
    text = '请记住预算763元'
    install(model, [intent('remember', text)], reply='已经永久记住了。')
    original = model.chat
    seen = []
    model.fn = lambda data: {'invalid': 'feedback'}
    def chat(messages, *, tools=()):
        if messages[0].content.startswith('[EXPRESSION]'):
            payload = json.loads(messages[-1].content)
            seen.append(payload)
            assert payload['learning_receipt']['status'] == 'failed'
            return LLMResponse('本轮没有保存成功，请稍后重试。')
        return original(messages, tools=tools)
    model.chat = chat
    result = turn(app, text)
    assert len(seen) == 1 and '没有保存成功' in result.response
    assert '已经永久记住' not in result.response
    assert not app.memory.list_items()


def test_buffer_receipt_is_real_and_does_not_imply_confirmed_long_term_memory(app_model):
    app, model = app_model
    model.fn = lambda data: dict(empty(), memories=[draft('预算763元', '预算763元')])
    result = turn(app, '预算763元')
    receipt = app.learning.receipt(result.task_id)
    assert receipt['status'] == 'completed'
    assert receipt['saved_understandings'][0]['status'] == 'candidate'
    assert receipt['saved_understandings'][0]['id']
    assert not model.expressions
    assert result.response == app.journal.get_plan(result.task_id).plan.reply


def test_expression_failure_never_publishes_unverified_save_claim_from_plan(app_model):
    app, model = app_model
    text = '请记住预算763元'
    install(model, [intent('remember', text)], reply='已经永久记住了。')
    model.fn = lambda data: dict(memories=[draft('预算763元', text)], management=[report()])
    original = model.chat
    def chat(messages, *, tools=()):
        if messages[0].content.startswith('[EXPRESSION]'):
            from deskpal.llm.errors import LLMError
            raise LLMError('synthetic expression failure')
        return original(messages, tools=tools)
    model.chat = chat
    result = turn(app, text)
    assert '已经永久记住' not in result.response
    assert result.response == '已记录。'
    assert app.memory.list_items()


def test_unavailable_receipt_does_not_discard_actual_task_result(app_model):
    app, model = app_model
    def unavailable(task_id):
        raise OSError('synthetic receipt storage unavailable')
    app.learning.receipt = unavailable
    result = turn(app, '你好')
    assert '回执暂不可用' in '\n'.join(result.warnings)
    assert app.tasks.get(result.task_id).result['learning_receipt']['status'] == 'unavailable'
    assert model.expressions == []


def test_invalid_feedback_is_corrected_within_existing_three_attempt_budget(app_model):
    app, model = app_model
    def feedback(data):
        if len(model.inputs) == 1:
            return {'invalid': True}
        return dict(empty(), memories=[draft('预算763元', '预算763元')])
    model.fn = feedback
    result = turn(app, '预算763元')
    assert len(model.inputs) == 2
    assert app.learning.store.job('turn:' + result.task_id)['attempts'] == 2
    assert app.learning.receipt(result.task_id)['status'] == 'completed'


def test_observed_event_uses_matching_tool_evidence_not_request(app_model):
    from types import SimpleNamespace
    app, _ = app_model
    row = draft('读取完成', 'actual output', destination='episode', sources=['user', 'tool0'])
    row['claim_type'] = 'observed_event'
    catalog = {'user': dict(content='请读取', role='user', origin='user_message', created_at='2026-11-01T00:00:00+00:00'),
        'tool0': dict(content='actual output', role='tool', origin='tool_result', created_at='2026-11-01T00:00:00+00:00')}
    context = SimpleNamespace(use_policy='cloud_allowed', task_id='synthetic', conversation_id='daily')
    source, value = app.learning._draft(row, catalog, context)
    assert source.origin == 'tool_result' and value.claim_type.value == 'observed_event'
