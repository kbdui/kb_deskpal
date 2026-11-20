"""合成档案验证统一管理：真实存储、单次整理、删除边界及恢复。"""
import json
import pytest

from deskpal.llm.models import LLMResponse
from deskpal.memory.contracts import MemoryStatus
from deskpal.tools.models import ToolCall
from tests.integration.agent.test_feedback_learning import app_model, draft, empty, turn


def intent(action, quote, *targets):
    return dict(action=action, quote=quote, targets=list(targets))


def report(index=0, status='applied', reason='按本轮明确要求处理'):
    return dict(request=index, status=status, reason=reason)


def install(model, requests, *, search=None, reply='准备处理记忆要求。'):
    original = model.chat
    counts = []
    def chat(messages, *, tools=()):
        if messages[0].content.startswith('[PLANNING]'):
            counts.append([tool.name for tool in tools])
            if search and len(counts) == 1:
                return LLMResponse('', (ToolCall('memory_search', {'query': search}, 'lookup'),))
            return LLMResponse(json.dumps(dict(action='answer', reply=reply,
                organize_memory=False, memory_requests=requests), ensure_ascii=False))
        return original(messages, tools=tools)
    model.chat = chat
    return counts


def test_explicit_remember_forces_one_learning_call_and_is_not_a_tool(app_model):
    app, model = app_model
    text = '请记住我在学习水彩'
    planned = install(model, [intent('remember', text)])
    model.fn = lambda data: dict(memories=[draft('用户在学习水彩', text)], management=[report()])
    result = turn(app, text)
    assert len(model.inputs) == 1 and len(app.memory.list_items()) == 1
    assert result.tool_results == ()
    assert not {'memory_remember', 'memory_correct', 'memory_forget'}.intersection(planned[0])
    assert 'memory_search' in planned[0]
    assert app.tasks.get(result.task_id).result['organize_memory'] is True
    assert app.journal.get_plan(result.task_id).plan.memory_requests == (intent('remember', text),)
    assert len(model.expressions) == 1
    assert model.expressions[-1]['learning_receipt']['management'][0]['status'] == 'applied'
    assert model.expressions[-1]['unverified_assistant_draft'] == '准备处理记忆要求。'
    assert model.expressions[-1]['history'] == [] and model.expressions[-1]['role_context'] is None


def test_search_then_correction_refreshes_reply_and_preserves_other_memory_and_chat(app_model):
    app, model = app_model
    old = app.memory_command('remember', content='用户喜欢咖啡')
    other = app.memory_command('remember', content='用户喜欢散步')
    text = '之前记错了，请把喜欢咖啡更正为喜欢红茶'
    install(model, [intent('correct', text, old.id)], search='咖啡')
    def feedback(data):
        row = draft('用户喜欢红茶', text)
        row.update(id=old.id, decision='update')
        return dict(reviews=[row], management=[report()])
    model.fn = feedback
    result = turn(app, text)
    assert len(model.inputs) == 1 and len(result.tool_results) == 1
    current = app.memory.repository.get(old.id)
    assert current.draft.content == '用户喜欢红茶' and current.revision == old.revision + 1
    assert app.memory.repository.get(other.id).draft.content == other.draft.content
    expression = model.expressions[-1]
    assert expression['memory_claims'] == [] and expression['tool_results'] == []
    assert expression['learning_receipt']['saved_understandings'][0]['content'] == current.draft.content
    assert expression['learning_receipt']['status'] == 'completed'
    with app.store.read_connection() as db:
        assert db.execute('SELECT 1 FROM conversation_item WHERE body=?', (text,)).fetchone()


def test_forget_directly_preserves_chat_and_other_records(app_model):
    app, model = app_model
    old = app.memory_command('remember', content='用户喜欢咖啡')
    other = app.memory_command('remember', content='用户喜欢散步')
    text = '请遗忘关于我喜欢咖啡的那条记忆'
    install(model, [intent('forget', text, old.id)], search='咖啡', reply='复述旧记忆：用户喜欢咖啡')
    model.fn = lambda data: dict(reviews=[dict(id=old.id, decision='forget', reason='明确要求')], management=[report()])
    result = turn(app, text)
    assert len(model.inputs) == 1
    assert app.memory.repository.get(old.id).status == MemoryStatus.DELETED
    assert app.memory.repository.get(other.id).status != MemoryStatus.DELETED
    assert all(item.tool_name != 'memory_forget' for item in result.tool_results)
    receipt = model.expressions[-1]['learning_receipt']
    assert receipt['status'] == 'completed' and receipt['saved_understandings'][0]['action'] == 'forget'
    assert 'content' not in receipt['saved_understandings'][0]
    assert model.expressions[-1]['unverified_assistant_draft'] == ''
    assert model.expressions[-1]['tool_results'] == []
    with app.store.read_connection() as db:
        assert db.execute('SELECT 1 FROM conversation_item WHERE body=?', (text,)).fetchone()
        assert not db.execute("SELECT 1 FROM dependency WHERE task_id=? AND kind='memory' AND object_id=?", (result.task_id, old.id)).fetchone()


def test_unclear_management_target_returns_question_without_changes(app_model):
    app, model = app_model
    old = app.memory_command('remember', content='用户喜欢咖啡')
    text = '把那件事忘掉'
    install(model, [intent('forget', text)])
    model.fn = lambda data: dict(management=[report(status='clarify', reason='你希望遗忘哪一件事？')])
    turn(app, text)
    assert app.memory.repository.get(old.id).status != MemoryStatus.DELETED
    assert model.expressions[-1]['learning_receipt']['management'][0]['status'] == 'clarify'


@pytest.mark.parametrize('fault', ['no_request', 'extra_target', 'false_success', 'forgotten_evidence'])
def test_invalid_management_cannot_partially_write(app_model, fault):
    app, model = app_model
    old = app.memory_command('remember', content='用户喜欢咖啡')
    other = app.memory_command('remember', content='用户喜欢散步')
    text = '请遗忘关于咖啡的记忆'
    install(model, [] if fault == 'no_request' else [intent('forget', text, old.id)])
    # 无管理要求的常规整理仍运行，以核对模型不能自行授权遗忘。
    if fault == 'no_request':
        original = model.chat
        def force(messages, *, tools=()):
            response = original(messages, tools=tools)
            if messages[0].content.startswith('[PLANNING]'):
                value = json.loads(response.content); value['organize_memory'] = True
                return LLMResponse(json.dumps(value))
            return response
        model.chat = force
    def bad(data):
        value = dict(reviews=[dict(id=old.id, decision='forget', reason='请求')], management=[report()])
        if fault == 'no_request':
            value.pop('management')
        if fault == 'extra_target':
            value['reviews'].append(dict(id=other.id, decision='forget', reason='扩大删除'))
        if fault == 'false_success':
            value['reviews'] = []
        if fault == 'forgotten_evidence':
            source = next(row['original_source_key'] for row in data['understandings'] if row['id'] == old.id)
            value['memories'] = [draft('重新保存咖啡', '用户喜欢咖啡', sources=[source])]
        return value
    model.fn = bad
    result = turn(app, text)
    assert app.memory.repository.get(old.id).status != MemoryStatus.DELETED
    assert app.memory.repository.get(other.id).status != MemoryStatus.DELETED
    assert app.tasks.get(result.task_id).result['learning_receipt']['status'] == 'failed'
    if fault == 'no_request':
        assert not model.expressions
    else:
        assert model.expressions[-1]['learning_receipt']['status'] == 'failed'


def test_committed_forget_cleanup_recovers_without_another_model_call(app_model, monkeypatch):
    app, model = app_model
    old = app.memory_command('remember', content='用户喜欢咖啡')
    text = '请遗忘关于咖啡的记忆'
    install(model, [intent('forget', text, old.id)])
    model.fn = lambda data: dict(reviews=[dict(id=old.id, decision='forget', reason='请求')], management=[report()])
    cleanup = app.learning._settle_memory_changes
    attempts = []
    def fail_once(*args):
        if not attempts:
            attempts.append(True)
            raise OSError('synthetic delivery failure')
        return cleanup(*args)
    monkeypatch.setattr(app.learning, '_settle_memory_changes', fail_once)
    result = turn(app, text)
    assert model.expressions[-1]['learning_receipt']['status'] == 'delivery_pending'
    assert app.memory.repository.get(old.id).status == MemoryStatus.DELETED
    app.learning.recover()
    assert app.learning.receipt(result.task_id)['status'] == 'completed'
    assert len(model.inputs) == 1


def test_forget_and_unrelated_remember_commit_together(app_model):
    app, model = app_model
    old = app.memory_command('remember', content='用户喜欢咖啡')
    text = '忘掉咖啡偏好；另外记住我开始学习水彩了'
    install(model, [intent('forget', '忘掉咖啡偏好', old.id), intent('remember', '记住我开始学习水彩了')])
    model.fn = lambda data: dict(memories=[draft('用户开始学习水彩', '我开始学习水彩了')],
        reviews=[dict(id=old.id, decision='forget', reason='明确要求')], management=[report(), report(1)])
    result = turn(app, text)
    assert app.memory.repository.get(old.id).status == MemoryStatus.DELETED
    assert any(item.draft.content == '用户开始学习水彩' for item in app.memory.list_items())
    assert len(model.inputs) == 1
    assert app.learning.receipt(result.task_id)['status'] == 'completed'


def test_management_quote_must_be_current_user_words(app_model):
    app, model = app_model
    old = app.memory_command('remember', content='用户喜欢咖啡')
    install(model, [intent('forget', '伪造的当前用户授权', old.id)])
    conversation = app.conversation_ensure_default()
    task = app.submit('今天聊点别的', conversation_id=conversation['id'], expected_context_epoch=conversation['context_epoch'])
    app.wait(task.id, timeout=10)
    assert not model.inputs
    assert app.memory.repository.get(old.id).status != MemoryStatus.DELETED


def test_pending_management_cleanup_is_applied_on_reopen(app_model, monkeypatch):
    from deskpal.application import create_application
    app, model = app_model
    old = app.memory_command('remember', content='用户喜欢咖啡')
    text = '请遗忘关于咖啡的记忆'
    install(model, [intent('forget', text, old.id)])
    model.fn = lambda data: dict(reviews=[dict(id=old.id, decision='forget', reason='请求')], management=[report()])
    def unavailable():
        raise OSError('synthetic cleanup unavailable')
    monkeypatch.setattr(app.learning, 'recover', unavailable)
    result = turn(app, text)
    assert app.learning.receipt(result.task_id)['status'] == 'delivery_pending'
    assert app.close(timeout=10)
    with create_application(model.settings) as reopened:
        assert reopened.learning.receipt(result.task_id)['status'] == 'completed'
        assert reopened.memory.repository.get(old.id).status == MemoryStatus.DELETED
        assert reopened.journal.get_plan(result.task_id).plan.memory_requests == (intent('forget', text, old.id),)
    assert len(model.inputs) == 1


def test_storage_failure_rolls_back_new_memory_and_forget_together(app_model, monkeypatch):
    app, model = app_model
    old = app.memory_command('remember', content='用户喜欢咖啡')
    text = '忘掉咖啡偏好；另外记住我开始学习水彩了'
    install(model, [intent('forget', '忘掉咖啡偏好', old.id), intent('remember', '记住我开始学习水彩了')])
    model.fn = lambda data: dict(memories=[draft('用户开始学习水彩', '我开始学习水彩了')],
        reviews=[dict(id=old.id, decision='forget', reason='请求')], management=[report(), report(1)])
    original = app.learning.repository.forget
    def fail_after_delete(*args):
        original(*args)
        raise OSError('synthetic transaction failure')
    monkeypatch.setattr(app.learning.repository, 'forget', fail_after_delete)
    result = turn(app, text)
    assert app.memory.repository.get(old.id).status != MemoryStatus.DELETED
    assert all(item.draft.content != '用户开始学习水彩' for item in app.memory.list_items())
    assert app.learning.receipt(result.task_id)['status'] == 'failed'
    assert app.learning.store.job('turn:' + result.task_id)['status'] == 'failed'
    assert not app.learning.store.pending_effects()


def test_explicit_correction_cannot_be_justified_only_by_old_evidence(app_model):
    app, model = app_model
    old = app.memory_command('remember', content='用户喜欢咖啡')
    text = '请把喜欢咖啡改成喜欢红茶'
    install(model, [intent('correct', text, old.id)])
    def feedback(data):
        key = next(row['original_source_key'] for row in data['understandings'] if row['id'] == old.id)
        value = dict(draft('用户喜欢红茶', '用户喜欢咖啡', sources=[key]), id=old.id, decision='update')
        return dict(reviews=[value], management=[report()])
    model.fn = feedback
    result = turn(app, text)
    assert app.memory.repository.get(old.id).draft.content == old.draft.content
    assert app.learning.receipt(result.task_id)['status'] == 'failed'
