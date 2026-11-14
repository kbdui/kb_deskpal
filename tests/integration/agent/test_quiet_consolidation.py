"""模型独立决定回复与学习、可信静默整理、延后材料覆盖；无真实模型或用户数据。"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import time

import pytest

from deskpal.application import create_application, factory
from deskpal.config import load_config
from deskpal.llm.errors import RetryableLLMError
from deskpal.llm.models import LLMResponse
from deskpal.tools.models import ToolCall, ToolResult, ToolSpec, PermissionLevel
from deskpal.tasks.models import TaskStatus


class Model:
    def __init__(self):
        self.decide = lambda: LLMResponse('这是回答草稿。')
        self.learn = lambda data: {}
        self.organize = True
        self.feedback, self.planning, self.expressions, self.order = [], [], [], []

    def chat(self, messages, *, tools=()):
        system = messages[0].content
        if system.startswith('[MEMORY_FEEDBACK]'):
            data = json.loads(messages[-1].content)
            self.feedback.append(data); self.order.append('learning')
            value = self.learn(data)
            return value if isinstance(value, LLMResponse) else LLMResponse(json.dumps(value, ensure_ascii=False))
        if system.startswith('[EXPRESSION]'):
            self.expressions.append(json.loads(messages[-1].content)); self.order.append('expression')
            return LLMResponse('这是实际发表的回答。')
        self.planning.append(messages); self.order.append('planning')
        result = self.decide()
        if not result.tool_calls and not result.content.lstrip().startswith('{'):
            return LLMResponse(json.dumps(dict(action='answer', reply=result.content,
                organize_memory=self.organize), ensure_ascii=False))
        return result

    def generate(self, prompt):
        raise AssertionError('No real model calls')


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc)
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    path.write_text('[llm]\nprovider="offline"\n[storage]\ndata_dir="data"\n[sandbox]\nworkspace_dir="workspace"\n[proactive]\nenabled=false\n', encoding='utf-8')
    model = Model()
    settings = load_config(path, environ={})
    monkeypatch.setattr(factory, 'OfflineLLMProvider', lambda: model)
    app = create_application(settings)
    app._rule_stop.set()  # 测试主动检查由用例明确推进，不与真实计时线程竞争。
    clock = Clock()
    app.proactive.clock = app.notification_policy.clock = clock
    try:
        yield app, model, clock, settings
    finally:
        assert app.close(timeout=10)


def until(fn):
    end = time.monotonic() + 10
    while not fn():
        assert time.monotonic() < end
        time.sleep(.01)


def quiet_turn(app, model, number=0, *, thought=False, organize=False):
    first = True
    def decide():
        nonlocal first
        if first and thought:
            first = False
            return LLMResponse('', (ToolCall('reflection_record', dict(topic=f'主题{number}',
                content=f'我正在探索第{number}种审美想法。', reason='一份内部设想，尚待后续理解'), 'thought'),))
        return LLMResponse(json.dumps(dict(action="quiet", reply="", organize_memory=organize)))
    model.decide = decide
    task = app.submit_proactive('合成主动思考', '合成机会', 'quiet-' + str(number))
    result = app.wait(task.id, 10)
    assert result.status == TaskStatus.COMPLETED, result
    return task, result


def run_batch(app, identity, materials):
    app.proactive.freeze_review(identity, [], materials=materials)
    app._dispatcher.enqueue_internal(identity, lambda: app.learning.run_review(identity))
    until(lambda: bool(app.learning.store.job('review:' + identity)) and
        app.learning.store.job('review:' + identity)['status'] in {'completed', 'failed', 'cancelled'})
    # 等待同一工作项完成任务状态提交，避免仅看见记忆库已提交的中间态。
    until(lambda: app.tasks.get(app.learning.store.job('review:' + identity)['task_id']).status in
          {TaskStatus.COMPLETED, TaskStatus.FAILED})
    return app.learning.store.job('review:' + identity)


def test_plain_quiet_skips_learning_and_expression(env):
    app, model, _, _ = env
    task, result = quiet_turn(app, model)
    assert result.response == ''
    assert not model.feedback and not model.expressions
    assert app.learning.store.job('turn:' + task.id) is None
    assert app.learning.receipt(task.id) == dict(status='skipped', reason='model_deferred', saved_understandings=[])
    assert not app.learning.materials.pending()
    with app.store.read_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM audit_outbox WHERE task_id=? AND event='memory_learning_skipped'", (task.id,)).fetchone()[0] == 1


def test_quiet_thought_is_saved_but_not_organized(env):
    app, model, _, _ = env
    task, result = quiet_turn(app, model, thought=True)
    assert len(result.tool_results) == 1 and result.tool_results[0].success
    assert not model.feedback and not model.expressions and not app.memory.list_items()
    pending = app.learning.materials.pending()
    assert len(pending) == 1 and pending[0]['ref']['kind'] == 'reflection'
    assert pending[0]['ref']['id'] == task.id
    assert not app.learning.store.organized_messages({'reflection:' + task.id: result.tool_results[0].output['content']})


def test_hidden_answer_still_learns_before_expression(env):
    app, model, _, _ = env
    app.notification_control('chat', True, 'pause-chat')
    model.decide = lambda: LLMResponse('我想说一段话。')
    task = app.submit_proactive('合成主动交流', '合成机会', 'hidden-answer')
    result = app.wait(task.id, 10)
    assert result.status == TaskStatus.COMPLETED and result.response == ''
    assert len(model.feedback) == 1 and len(model.expressions) == 1
    assert model.order == ['planning', 'learning', 'expression']
    assert app.tasks.get(task.id).result['final_action'] == 'answer'
    assert app.learning.receipt(task.id)['status'] == 'completed'


def test_normal_answer_absorbs_a_single_deferred_thought_without_extra_call(env):
    app, model, clock, _ = env
    quiet, _ = quiet_turn(app, model, thought=True)
    def learn(data):
        key, row = next((key, row) for key, row in data['catalog'].items() if row['origin'] == 'character_reflection')
        return dict(memories=[dict(content='角色正在探索自己的审美。', quote=row['content'],
            claim_type='hypothesis', destination='episode', subject=row['speaker_id'],
            reason='来自角色的实际内部成果，仍是设想', sources=[key],
            review_after=(clock() + timedelta(days=1)).isoformat())])
    model.learn = learn
    model.decide = lambda: LLMResponse('继续聊这件事。')
    conversation = app.conversation_ensure_default()
    task = app.submit('我们接着聊', conversation_id=conversation['id'], expected_context_epoch=conversation['context_epoch'])
    assert app.wait(task.id, 10).status == TaskStatus.COMPLETED
    assert len(model.feedback) == 1 and len(model.expressions) == 1
    assert any(item.source.origin == 'character_reflection' for item in app.memory.list_items())
    assert all(item['ref']['id'] != quiet.id for item in app.learning.materials.pending())


def test_eight_materials_no_longer_start_review_without_model_decision(env):
    app, model, clock, _ = env
    for number in range(8):
        quiet_turn(app, model, number, thought=True)
    app.config = replace(app.config, proactive=replace(app.config.proactive, enabled=True))
    clock.advance(86400 * 30)
    app.proactive._review()
    assert not model.feedback and not app.proactive._meta('pending_review')
    assert len(app.learning.materials.pending()) == 8
    task, result = quiet_turn(app, model, 9, organize=True)
    assert result.response == '' and len(model.feedback) == 1 and not model.expressions
    assert app.learning.receipt(task.id)['status'] == 'completed'
    assert not app.learning.materials.pending()
    assert any('unorganized_materials' in message.content for message in model.planning[-1])


def test_explicit_silent_review_does_not_require_eight_or_chat_permission(env):
    app, model, _, _ = env
    quiet_turn(app, model, thought=True)
    app.notification_control('chat', True, 'pause-chat-review')
    batch = app.learning.materials.select()
    job = run_batch(app, 'explicit-review', batch)
    assert job['status'] == 'completed'
    assert len(model.feedback) == 1 and not model.expressions
    task = app.tasks.get(job['task_id'])
    assert task.result['task_purpose'] == 'memory_review' and task.result['delivery'] == 'silent'
    assert task.result['learning_receipt']['status'] == 'completed'
    assert not app.learning.materials.pending()


def test_failed_review_does_not_mark_sources_organized(env):
    app, model, _, _ = env
    quiet_turn(app, model, thought=True)
    materials = app.learning.materials.select()
    def fail(data):
        raise RetryableLLMError('synthetic connection failure')
    model.learn = fail
    assert run_batch(app, 'failed-review', materials)['status'] == 'failed'
    assert len(model.feedback) == 3
    assert len(app.learning.materials.pending()) == 1
    model.learn = lambda data: {}
    assert run_batch(app, 'retry-review', app.learning.materials.select())['status'] == 'completed'
    assert not app.learning.materials.pending()


def test_unorganized_scan_advances_past_already_known_sources(env):
    app, model, _, _ = env
    for number in range(3):
        quiet_turn(app, model, number, thought=True)
    # 规划现在会预览积压材料；单独从空索引开始验证分页发现，避免依赖之前预览次数。
    app.proactive._meta('learning_pending_materials', '[]')
    app.proactive._meta('learning_material_cursor', '["", ""]')
    app.learning.materials.PAGE_SIZE = 1
    assert len(app.learning.materials.pending()) == 1
    assert len(app.learning.materials.pending()) == 2
    assert len(app.learning.materials.pending()) == 3
    assert not model.feedback


def test_clearing_chat_invalidates_unorganized_published_messages(env):
    app, model, _, _ = env
    conversation = app.conversation_ensure_default()
    task = app.submit('普通聊天', conversation_id=conversation['id'], expected_context_epoch=conversation['context_epoch'])
    assert app.wait(task.id, 10).status == TaskStatus.COMPLETED
    pending = app.learning.materials.pending()
    assert any(item['ref']['id'] == task.id + ':assistant' for item in pending)
    current = app.conversation_ensure_default()
    app.conversation_clear(current['id'], current['revision'], 'clear-material')
    assert not app.learning.materials.pending()
    assert len(model.feedback) == 1


def test_quiet_skip_and_unorganized_thought_survive_restart(env):
    app, model, _, settings = env
    task, _ = quiet_turn(app, model, thought=True)
    assert app.close(timeout=10)
    with create_application(settings) as reopened:
        assert reopened.learning.receipt(task.id)['reason'] == 'model_deferred'
        assert reopened.learning.materials.pending()[0]['ref']['id'] == task.id
        assert not model.feedback


def test_old_pending_turn_learning_is_not_retried_for_quiet(env):
    app, model, _, _ = env
    task, _ = quiet_turn(app, model, thought=True)
    link = app._humanization.turn(task.id)
    app.learning.store.register('turn:' + task.id, kind='turn', task_id=task.id,
        conversation_id=link['conversation_id'], conversation_epoch=link['epoch'],
        privacy_epoch=link['privacy_epoch'], role_revision=1, refs=[])
    app.learning.resume_work()
    assert app.learning.store.job('turn:' + task.id)['status'] == 'cancelled'
    assert not model.feedback


def test_quiet_keeps_actual_external_tool_results(env):
    app, model, _, _ = env
    sent = []
    class Send:
        spec = ToolSpec('synthetic_send', '合成发送工具', {'type': 'object', 'properties': {}, 'additionalProperties': False}, PermissionLevel.CONFIRM)
        def execute(self, arguments, context):
            sent.append(context.task_id)
            return ToolResult(True, {'sent': True, 'message_id': 'synthetic-confirmed'})
    app.tools.register(Send())
    model.decide = lambda: LLMResponse('', (ToolCall('synthetic_send', {}, 'send'),)) if not sent else LLMResponse('{"action":"quiet","reply":"","organize_memory":false}')
    task = app.submit_proactive('合成发送', '合成测试机会', 'send-and-quiet')
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    reviewed = app.task_review(task.id)
    app.enqueue_confirmation(task.id, authorized_tools=['synthetic_send'], plan_hash=reviewed['plan_hash'],
        expected_revision=reviewed['task_revision'], command_id='allow-synthetic-send')
    result = app.wait(task.id, 10)
    assert result.status == TaskStatus.COMPLETED and result.response == ''
    assert sent == [task.id] and result.tool_results[0].output['message_id'] == 'synthetic-confirmed'
    assert app.journal.attempts(task.id)[0].status == 'Succeeded'
    assert not model.feedback and not model.expressions


def test_answer_can_defer_learning_and_preserve_actual_published_text(env):
    app, model, _, _ = env
    model.organize = False
    conversation = app.conversation_ensure_default()
    task = app.submit('你好', conversation_id=conversation['id'], expected_context_epoch=conversation['context_epoch'])
    result = app.wait(task.id, 10)
    assert result.status == TaskStatus.COMPLETED and result.response
    assert not model.feedback and len(model.expressions) == 1
    assert app.tasks.get(task.id).result['organize_memory'] is False
    assert app.learning.receipt(task.id)['reason'] == 'model_deferred'
    assert app.journal.get_plan(task.id).plan.organize_memory is False
    assert {m['ref']['id'] for m in app.learning.materials.pending()} == {task.id + ':user', task.id + ':assistant'}


def test_quiet_can_organize_current_thought_in_same_turn(env):
    app, model, _, _ = env
    task, result = quiet_turn(app, model, thought=True, organize=True)
    assert result.response == '' and len(model.feedback) == 1 and not model.expressions
    assert app.journal.get_plan(task.id).plan.organize_memory is True
    assert app.learning.receipt(task.id)['status'] == 'completed'
    assert not app.learning.materials.pending()


def test_final_model_decision_can_defer_after_tool_result(env):
    app, model, _, _ = env
    task, _ = quiet_turn(app, model, thought=True)
    assert app.journal.get_plan(task.id).plan.organize_memory is False
    assert not model.feedback
    assert app.tasks.get(task.id).result['organize_memory'] is False


def test_model_deferred_answer_learning_is_not_retried_after_restart(env):
    app, model, _, _ = env
    model.organize = False
    task = app.submit('以后再整理这轮')
    assert app.wait(task.id, 10).status == TaskStatus.COMPLETED
    link = app._humanization.turn(task.id)
    app.learning.store.register('turn:' + task.id, kind='turn', task_id=task.id,
        conversation_id=link['conversation_id'], conversation_epoch=link['epoch'],
        privacy_epoch=link['privacy_epoch'], role_revision=1, refs=[])
    app.learning.resume_work()
    assert app.learning.store.job('turn:' + task.id)['status'] == 'cancelled'
    assert not model.feedback


@pytest.mark.parametrize('organize', [False, True])
def test_final_learning_decision_survives_reopening_profile(env, organize):
    app, model, _, settings = env
    task, _ = quiet_turn(app, model, thought=True, organize=organize)
    count = len(model.feedback)
    assert app.close(timeout=10)
    with create_application(settings) as reopened:
        assert reopened.journal.get_plan(task.id).plan.organize_memory is organize
        assert reopened.tasks.get(task.id).result['organize_memory'] is organize
        assert reopened.learning.receipt(task.id)['status'] == ('completed' if organize else 'skipped')
        assert len(model.feedback) == count


def test_legacy_material_only_automatic_batch_is_retired_without_learning(env):
    app, model, clock, _ = env
    quiet_turn(app, model, thought=True)
    app.proactive.freeze_review('proactive-review:old-threshold', [], materials=app.learning.materials.select())
    app.proactive._meta('pending_review', 'proactive-review:old-threshold')
    clock.advance(901)
    app.proactive._review()
    assert not app.proactive._meta('pending_review')
    assert not app.proactive._meta('review_batch:proactive-review:old-threshold')
    assert not model.feedback and len(app.learning.materials.pending()) == 1
