"""真实持久层及可控模型：续办、统一复查和情绪时间影响，不调用真实模型。"""
from datetime import timedelta
import json

import pytest

from deskpal.application import create_application
from deskpal.companion import CompanionRuntime
from deskpal.llm.models import LLMResponse
from deskpal.memory.contracts import MemorySource, MemoryDraft, ClaimType
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolCall, ToolSpec, PermissionLevel
from tests.integration.agent.test_multi_round_loop import app_model as loop_setup, Probe
from tests.integration.agent.test_proactive_controls import (
    setup as proactive_setup, enable, until, user_turn, attention_task,
)
from tests.integration.agent.test_feedback_learning import (
    app_model as learning_setup, turn, empty, review,
)


@pytest.mark.parametrize('restart', [False, True])
def test_exact_memory_background_survives_confirmation_and_restart(loop_setup, restart):
    app, model, _ = loop_setup
    app.memory.remember('雪杉项目采用蓝色封面，预算是三百元')
    dangerous = Probe()
    dangerous.spec = ToolSpec('approved_probe', '合成需确认操作', Probe.spec.parameters, PermissionLevel.CONFIRM)
    app.tools.register(dangerous)
    original = model.chat
    seen = []
    def chat(messages, *, tools=()):
        if 'bounded request planner' in messages[0].content:
            seen.append('\n'.join(m.content for m in messages))
            if len(seen) == 1:
                return LLMResponse('', (ToolCall('approved_probe', {'value': 'one'}),))
            return LLMResponse('已根据原目标与真实结果完成。')
        return original(messages, tools=tools)
    model.chat = chat
    task = app.submit('按雪杉项目预算和封面要求完成操作')
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    active = app
    try:
        if restart:
            assert app.close(timeout=10)
            active = create_application(model.settings)
            active.tools.register(dangerous)
            active.resume(task.id)
        else:
            active._interaction.discard_tasks((task.id,))
        active.enqueue_confirmation(task.id, authorized_tools=['approved_probe'], plan_hash=active.journal.get_plan(task.id).hash)
        result = active.wait(task.id, 10)
        assert result.status == TaskStatus.COMPLETED, result
        assert len(seen) == 2
        assert all('三百元' in body and '[UNTRUSTED MEMORY DATA]' in body for body in seen)
        assert 'observed-one' in seen[-1] and dangerous.calls == ['one']
    finally:
        if active is not app:
            assert active.close(timeout=10)


def test_paused_task_keeps_memory_results_and_amendments(proactive_setup):
    app, model, probe, _, _ = proactive_setup
    app.memory.remember('项目雪杉必须保留蓝色封面')
    model.fn = lambda messages, tools: LLMResponse('', (ToolCall('probe', {'value': 'one'}), ToolCall('probe', {'value': 'two'})))
    probe.block = True
    task = app.submit('按项目雪杉要求做两步，再判断')
    assert probe.entered.wait(5)
    app.pause_task(task.id, 'pause-memory')
    probe.release.set()
    assert app.wait(task.id, 10).status == TaskStatus.PAUSED
    app.append_requirement(task.id, '保留原预算并缩小处理范围', 'new-scope')
    captured = []
    def finish(messages, tools):
        captured.append('\n'.join(m.content for m in messages))
        return LLMResponse('已继续判断。')
    model.fn = finish
    app.continue_task(task.id, 'continue-context')
    assert app.wait(task.id, 10).status == TaskStatus.COMPLETED
    assert 'actual-one' in captured[0] and '缩小处理范围' in captured[0]
    assert '蓝色封面' in captured[0] and '[UNTRUSTED MEMORY DATA]' in captured[0]
    assert probe.values == ['one', 'two']


def seed(app, number, *, due=None):
    return app.learning.repository.ingest(MemorySource(f'用户真实原话{number}'),
        MemoryDraft(f'待复核理解{number}', f'用户真实原话{number}', claim_type=ClaimType.HYPOTHESIS,
            review_after=due, reason='保留待确认', uncertainty='目前不确定'))


def test_due_items_outside_newest_page_are_selected_and_batch_is_frozen(proactive_setup):
    app, model, _, clock, _ = proactive_setup
    overdue = seed(app, 0, due=clock() - timedelta(days=1))
    for number in range(1, 27):
        seed(app, number)
    refs = app.learning.review_candidates(clock())
    assert overdue.id in {row['id'] for row in refs} and len(refs) == 16
    app.proactive.freeze_review('fixed', refs)
    later = seed(app, 'later')
    app.enqueue_learning_review('fixed')
    until(lambda: (app.learning.store.job('review:fixed') or {}).get('status') == 'completed')
    until(lambda: not app._dispatcher.diagnostics()['internal_active'])
    actual = {row['id'] for row in model.feedback[-1]['understandings']}
    assert actual == {row['id'] for row in refs} and later.id not in actual
    # 空回复未假记任何记录已复核；尝试轮转让下一批也有机会。
    next_refs = app.learning.review_candidates(clock())
    assert later.id in {row['id'] for row in next_refs}
    assert app.proactive._meta('review_coverage') in ('', '{}')


def test_changed_frozen_revision_is_cancelled_before_model(proactive_setup):
    app, model, _, clock, _ = proactive_setup
    item = seed(app, 1)
    app.proactive.freeze_review('old', [dict(kind='memory', id=item.id, revision=item.revision)])
    app.memory.correct(item.id, item.revision, '现在有新的理解')
    app.enqueue_learning_review('old')
    until(lambda: (app.learning.store.job('review:old') or {}).get('status') == 'cancelled')
    assert model.feedback == []


def test_only_actual_reviewed_targets_are_acknowledged(proactive_setup):
    app, model, _, clock, _ = proactive_setup
    a, b = seed(app, 1), seed(app, 2)
    model.feedback_fn = lambda data: dict(empty(), reviews=[review(data, decision='retain')])
    app.enqueue_learning_review('one-only')
    until(lambda: (app.learning.store.job('review:one-only') or {}).get('status') == 'completed')
    until(lambda: bool(app.proactive._meta('review_coverage')))
    covered = json.loads(app.proactive._meta('review_coverage'))
    assert len(covered) == 1
    remaining = {row['id'] for row in app.learning.review_candidates(clock())}
    assert remaining == {a.id, b.id} - set(covered)


def test_internal_followup_runs_without_chat_and_can_end_emotion_concern(proactive_setup):
    app, model, _, clock, _ = proactive_setup
    task, conversation = user_turn(app)
    before = app.conversation_timeline(conversation['id'])['items']
    enable(app)
    app.notification_policy.set('chat', True, None, 'quiet-maintenance')
    due = clock() + timedelta(minutes=16)
    identity = app.proactive.schedule(task.id, '复查准备事项及角色担忧是否仍适用', '准备工作尚未有结果', due.isoformat(), 'internal')
    observed = []
    def feedback(payload):
        observed.append(payload)
        source = next(key for key in payload['catalog'] if key.startswith('source:'))
        return dict(empty(), emotion=dict(description_intensity='平静，保持适度关心', reason_sources='目前没有新的坏消息，不放大担忧',
            time_scope='当前', uncertainty='仍不知道最终结果', sources=[source], expires_at=None, half_life_seconds=1800))
    model.feedback_fn = feedback
    clock.advance(1000)
    app.proactive.poll()
    until(lambda: (app.learning.store.job('review:attention-review:' + identity) or {}).get('status') == 'completed')
    until(lambda: not app._dispatcher.diagnostics()['internal_active'])
    assert '角色担忧' in observed[0]['review_goal']
    assert any('面试' in row['content'] for row in observed[0]['catalog'].values())
    assert app.core.companion_runtime.context('', 0, '')['current_emotion']['description_intensity'].startswith('平静')
    assert app.conversation_timeline(conversation['id'])['items'] == before
    app.proactive.poll()
    assert attention_task(app, identity)['status'] == 'done'


def test_due_followup_rechecks_using_corrected_memory(proactive_setup):
    app, model, _, clock, _ = proactive_setup
    old = app.memory.remember("旧项目资料，需要在到期时重新核对。")
    enable(app)
    identity = app.proactive.schedule_followup("memory-followup", goal="复查项目资料", reason="需要核对",
        due_at=(clock() + timedelta(minutes=16)).isoformat(), mode="internal",
        refs=[dict(kind="memory", id=old.id, revision=old.revision)])
    new = app.memory_command("correct", memory_id=old.id, expected_revision=old.revision,
                             content="更正后的项目资料")
    clock.advance(1000)
    app.proactive.poll()
    until(lambda: (app.learning.store.job('review:attention-review:' + identity) or {}).get('status') == 'completed')
    job = next(payload for payload in model.feedback if payload.get("review_goal") == "复查项目资料")
    assert any(row["id"] == new.id and row["revision"] == new.revision and
               row["content"] == "更正后的项目资料" for row in job["understandings"])


def test_feedback_schedules_same_followup_queue_with_idempotent_delivery(learning_setup):
    app, model = learning_setup
    due = (app.proactive.clock() + timedelta(hours=1)).isoformat()
    model.fn = lambda payload: dict(empty(), followups=[dict(goal='重新判断未决事项与情绪', reason='用户尚在等待结果',
        due_at=due, sources=['user'], mode='internal')])
    result = turn(app, '我正在等待结果')
    rows = app.proactive.page()['items']
    assert len(rows) == 1 and rows[0]['method'] == 'maintenance'
    app.learning.recover()
    assert len(app.proactive.page()['items']) == 1
    assert app.learning.store.job('turn:' + result.task_id)['status'] == 'completed'


def test_followup_delivery_recovers_after_cross_store_interruption(learning_setup, monkeypatch):
    app, model = learning_setup
    due = (app.proactive.clock() + timedelta(hours=1)).isoformat()
    model.fn = lambda payload: dict(empty(), followups=[dict(goal='复核等待结果的事项', reason='实际用户交流',
        due_at=due, sources=['user'], mode='internal')])
    schedule = app.proactive.schedule_followup
    blocked = [True]
    def interrupted(*args, **kwargs):
        if blocked[0]:
            raise OSError('synthetic workflow publication interruption')
        return schedule(*args, **kwargs)
    monkeypatch.setattr(app.proactive, 'schedule_followup', interrupted)
    result = turn(app, '还在等待结果')
    assert app.learning.store.job('turn:' + result.task_id)['status'] == 'completed'
    assert app.learning.store.pending_effects() and not app.proactive.page()['items']
    blocked[0] = False
    app.learning.recover()
    app.learning.recover()
    assert len(app.proactive.page()['items']) == 1
    assert not app.learning.store.pending_effects()


def test_model_own_output_cannot_authorize_followup(learning_setup):
    app, model = learning_setup
    model.fn = lambda payload: dict(empty(), followups=[dict(goal='无依据的反复跟进', reason='助手自己提过',
        due_at=(app.proactive.clock() + timedelta(hours=1)).isoformat(), sources=['assistant'], mode='internal')])
    result = turn(app, '普通聊天')
    assert not app.proactive.page()['items']
    assert app.learning.store.job('turn:' + result.task_id)['status'] == 'failed'


def test_frozen_batch_survives_backend_restart(proactive_setup):
    app, model, _, clock, config = proactive_setup
    for number in range(4):
        seed(app, number)
    refs = app.learning.review_candidates(clock())
    app.proactive.freeze_review('restart-batch', refs)
    assert app.close(timeout=10)
    reopened = create_application(config)
    try:
        reopened.enqueue_learning_review('restart-batch')
        until(lambda: (reopened.learning.store.job('review:restart-batch') or {}).get('status') == 'completed')
        assert {row['id'] for row in model.feedback[-1]['understandings']} == {row['id'] for row in refs}
    finally:
        assert reopened.close(timeout=10)


def test_unchanged_unhandled_revision_stops_automatic_retries(proactive_setup):
    app, model, _, clock, _ = proactive_setup
    item = seed(app, 'unchanged')
    for index in range(3):
        assert app.learning.review_candidates(clock())[0]['id'] == item.id
        app.enqueue_learning_review('unhandled-' + str(index))
        until(lambda: (app.learning.store.job('review:unhandled-' + str(index)) or {}).get('status') == 'completed')
        until(lambda: not app._dispatcher.diagnostics()['internal_active'])
        clock.advance(1000)
    assert app.learning.review_candidates(clock()) == []
    assert json.loads(app.proactive._meta('review_coverage')) == {}
    assert app.learning.repository.get(item.id).revision == item.revision
    app.memory.correct(item.id, item.revision, '出现新信息后允许重新复核')
    assert app.learning.review_candidates(clock())[0]['id'] == item.id


def test_source_revision_changes_during_internal_maintenance_discard_emotion(proactive_setup):
    app, model, _, clock, _ = proactive_setup
    item = seed(app, 'source')
    app.proactive.offer(key='maintenance-source', method='maintenance', goal='复核未决事项与情绪',
        basis='已有实际记忆待复核', refs=[dict(kind='memory', id=item.id, revision=item.revision)])
    def feedback(payload):
        app.memory.correct(item.id, item.revision, '来源已经变化，不再采用旧反应')
        return dict(empty(), emotion=dict(description_intensity='担忧', reason_sources='旧来源', time_scope='现在',
            uncertainty='不确定', sources=['memory0'], expires_at=None))
    model.feedback_fn = feedback
    enable(app)
    app.proactive.poll()
    until(lambda: bool(model.feedback))
    until(lambda: not app._dispatcher.diagnostics()['internal_active'])
    assert app.core.companion_runtime.context('', 0, '')['current_emotion'] is None
    assert not app.learning.store.pending_effects()


@pytest.mark.parametrize('invalid', [True, 0, 899, 604801, float('inf')])
def test_invalid_emotion_half_life_cannot_commit(tmp_path, invalid):
    role = CompanionRuntime(tmp_path / 'invalid-role.sqlite3', wall_clock=lambda: 1000000.0)
    try:
        emotion = dict(description_intensity='担忧', reason_sources='真实原因', time_scope='当前', uncertainty='暂时',
            expires_at=None, half_life_seconds=invalid, host_refs=[])
        with pytest.raises(ValueError):
            role.apply_learning('invalid', expected_revision=1, source=dict(task_id='task'), habits=[],
                emotion=emotion, use_policy='cloud_allowed')
        assert role.context('', 0, '')['current_emotion'] is None
    finally:
        role.close()


@pytest.mark.parametrize('half_life', [900, 21600, 604800])
def test_emotion_weakens_without_rewriting_meaning_or_learning_on_read(tmp_path, half_life):
    now = [1000000.0]
    path = tmp_path / 'role.sqlite3'
    role = CompanionRuntime(path, wall_clock=lambda: now[0])
    emotion = dict(description_intensity='略有担忧', reason_sources='等待重要结果', time_scope='结果明确之前',
        uncertainty='结果未知', expires_at=None, half_life_seconds=half_life, host_refs=[])
    source = dict(task_id='real-task', conversation_id='chat', revision=1)
    role.apply_learning('emotion-1', expected_revision=1, source=source, habits=[], emotion=emotion, use_policy='cloud_allowed')
    revision = role.context('chat', 0, '')['revision']
    assert role.context('chat', 0, '')['current_emotion']['influence_weight'] == 1
    now[0] += half_life
    assert role.context('chat', 0, '')['current_emotion']['influence_weight'] == pytest.approx(.5)
    role.close()
    role = CompanionRuntime(path, wall_clock=lambda: now[0])
    try:
        assert role.context('chat', 0, '')['current_emotion']['influence_weight'] == pytest.approx(.5)
        now[0] += half_life
        state = role.context('chat', 0, '')
        assert state['current_emotion']['influence_weight'] == pytest.approx(.25)
        assert state['current_emotion']['reason_sources'] == '等待重要结果'
        assert state['revision'] == revision
        role.apply_learning('retain', expected_revision=revision, source=source, habits=[], emotion=None, use_policy='cloud_allowed')
        assert role.context('chat', 0, '')['current_emotion']['influence_weight'] == pytest.approx(.25)
    finally:
        role.close()


def test_emotion_tapers_to_expiry_and_delayed_delivery_keeps_original_time(tmp_path):
    now = [1000000.0]
    role = CompanionRuntime(tmp_path / 'deadline-role.sqlite3', wall_clock=lambda: now[0])
    try:
        emotion = dict(description_intensity='担忧', reason_sources='未决事项', time_scope='短期', uncertainty='尚不确定',
            expires_at=1003600.0, half_life_seconds=1800, host_refs=[])
        # 投递延迟半个半衰期，不应被当作一段全新的情绪。
        role.apply_learning('delayed', expected_revision=1, source=dict(task_id='source', learning_at=999100.0),
            habits=[], emotion=emotion, use_policy='cloud_allowed')
        first = role.context('', 0, '')['current_emotion']['influence_weight']
        assert 0 < first < 1
        now[0] = 1003599.0
        assert role.context('', 0, '')['current_emotion']['influence_weight'] < .001
        now[0] = 1003600.0
        assert role.context('', 0, '')['current_emotion'] is None
        assert role.interaction_snapshot()['current_emotion']['reason_sources'] == '未决事项'
    finally:
        role.close()
