"""P5：合成档案、实际工厂和账本；无真实模型、通知或用户数据库。"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from threading import Event
import time

import pytest

from deskpal.application import create_application, factory
from deskpal.application.desktop import DesktopFacade
from deskpal.config import load_config
from deskpal.llm.models import LLMResponse
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolCall, ToolResult, ToolSpec, PermissionLevel
from deskpal.workflow.models import WorkflowBlocked
from deskpal.workflow.store import WorkflowStore


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class Model:
    def __init__(self):
        self.requests = []
        self.feedback = []
        self.feedback_fn = lambda data: dict(memories=[], reviews=[], habits=[], emotion=None, summary=None)
        self.fn = lambda messages, tools: LLMResponse('这是实际回复。')

    def chat(self, messages, *, tools=()):
        if messages[0].content.startswith('[CONTEXT_UNDERSTANDING]'):
            return LLMResponse('{"entries":[]}')
        if messages[0].content.startswith('[EXPRESSION]'):
            return LLMResponse('{"reply_text":"这是组织后的实际回复。"}')
        if messages[0].content.startswith('[MEMORY_FEEDBACK]'):
            payload = json.loads(messages[-1].content)
            self.feedback.append(payload)
            return LLMResponse(json.dumps(self.feedback_fn(payload), ensure_ascii=False))
        self.requests.append(messages)
        result = self.fn(messages, tools)
        if not result.tool_calls:
            decision = json.loads(result.content) if result.content.lstrip().startswith('{') else dict(action='answer', reply=result.content)
            decision.setdefault('organize_memory', decision['action'] == 'answer')
            return LLMResponse(json.dumps(decision, ensure_ascii=False))
        return result

    def generate(self, prompt):
        raise AssertionError('Unexpected model entry')


class Probe:
    spec = ToolSpec('probe', '仅测试的合成工具', {'type':'object','properties':{'value':{'type':'string'}},'required':['value'],'additionalProperties':False}, PermissionLevel.AUTOMATIC)

    def __init__(self):
        self.values = []
        self.entered, self.release = Event(), Event()
        self.block = False

    def execute(self, arguments, context):
        self.entered.set()
        if self.block:
            assert self.release.wait(10)
        self.values.append(arguments['value'])
        return ToolResult(True, 'actual-' + arguments['value'])


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    path.write_text('[llm]\nprovider="offline"\n[storage]\ndata_dir="data"\n[sandbox]\nworkspace_dir="workspace"\n[proactive]\nenabled=false\n', encoding='utf-8')
    model = Model()
    monkeypatch.setattr(factory, 'OfflineLLMProvider', lambda: model)
    config = load_config(path, environ={})
    app = create_application(config)
    probe = Probe()
    app.tools.register(probe)
    clock = Clock()
    app.proactive.clock = clock
    app.notification_policy.clock = clock
    try:
        yield app, model, probe, clock, config
    finally:
        probe.release.set()
        assert app.close(timeout=10)


def until(fn):
    deadline = time.monotonic() + 10
    while not fn():
        assert time.monotonic() < deadline
        time.sleep(.01)


def user_turn(app):
    conversation = app.conversation_ensure_default()
    task = app.submit('今天准备面试。', conversation_id=conversation['id'], expected_context_epoch=conversation['context_epoch'])
    assert app.wait(task.id, 10).status == TaskStatus.COMPLETED
    return task, conversation


def enable(app):
    app.config = replace(app.config, proactive=replace(app.config.proactive, enabled=True))
    app.rules.config = app.config.proactive
    app.notification_policy.set_quiet(0, 0, False, "test-disable-quiet")
    for method in ('event', 'opportunity', 'fallback'):
        app.proactive.method(method, False)


def offer(app, task, key='topic'):
    link = app._humanization.turn(task.id)
    return app.proactive.offer(key=key, method='review', goal='看看面试准备是否仍需关心，必要时安静。',
        basis='用户先前提到面试，结合最新消息再判断。', refs=[dict(kind='dialogue_turn', id=task.id+':user', revision=link['sequence'])])


def attention_task(app, identity):
    with app.store.read_connection() as db:
        row = db.execute('SELECT * FROM attention WHERE id=?', (identity,)).fetchone()
        return dict(row)


def pause_after_group(app, model, probe):
    count = [0]
    def decide(messages, tools):
        count[0] += 1
        if count[0] == 1:
            return LLMResponse('', (ToolCall('probe', {'value':'one'}), ToolCall('probe', {'value':'two'})))
        return LLMResponse('依据实际结果完成。')
    model.fn = decide
    probe.block = True
    task = app.submit('做两步，再判断。')
    assert probe.entered.wait(5)
    assert app.pause_task(task.id, 'pause').status == TaskStatus.RUNNING
    assert app.task_control.snapshot(task.id)['requested']
    probe.release.set()
    result = app.wait(task.id, 10)
    assert result.status == TaskStatus.PAUSED, result
    assert probe.values == ['one','two']
    assert count[0] == 1
    return task


def test_pause_group_continue_same_task_and_amendment(setup):
    app, model, probe, _, _ = setup
    task = pause_after_group(app, model, probe)
    app.append_requirement(task.id, '最终简短说明即可。', 'amend')
    assert app.tasks.get(task.id).status == TaskStatus.PAUSED
    app.continue_task(task.id, 'continue')
    result = app.wait(task.id, 10)
    assert result.status == TaskStatus.COMPLETED, result
    assert result.task_id == task.id
    assert probe.values == ['one','two']
    assert '最终简短说明' in '\n'.join(m.content for m in model.requests[-1])


def test_paused_worker_available_and_repeat_commands_no_duplicate(setup):
    app, model, probe, _, _ = setup
    task = pause_after_group(app, model, probe)
    assert app.pause_task(task.id, 'pause').status == TaskStatus.PAUSED
    assert app.run('另一项普通聊天').status == TaskStatus.COMPLETED
    assert app.tasks.get(task.id).status == TaskStatus.PAUSED
    assert app.command_status('pause')['accepted']
    app.continue_task(task.id, 'continue')
    app.continue_task(task.id, 'continue')
    assert app.wait(task.id, 10).status == TaskStatus.COMPLETED
    assert probe.values == ['one','two']


def test_pause_restart_retained_then_explicit_continue(setup):
    app, model, probe, _, config = setup
    task = pause_after_group(app, model, probe)
    assert app.close(10)
    other = create_application(config)
    other.tools.register(probe)
    try:
        assert other.tasks.get(task.id).status == TaskStatus.PAUSED
        assert probe.values == ['one','two']
        other.continue_task(task.id, 'continue-after-open')
        result = other.wait(task.id, 10)
        assert result.status == TaskStatus.COMPLETED, result
        assert probe.values == ['one','two']
    finally:
        assert other.close(10)


def test_paused_can_cancel_cannot_resume(setup):
    app, model, probe, _, _ = setup
    task = pause_after_group(app, model, probe)
    assert app.cancel_task(task.id).status == TaskStatus.CANCELLED
    with pytest.raises(WorkflowBlocked):
        app.continue_task(task.id, 'late-continue')
    assert probe.values == ['one','two']


def test_pause_terminal_honest_and_command_conflict(setup):
    app, _, _, _, _ = setup
    task, _ = user_turn(app)
    assert app.pause_task(task.id, 'late-pause').status == TaskStatus.COMPLETED
    with pytest.raises(ValueError):
        app.continue_task(task.id, 'late-pause')


def test_paused_budget_not_reset(setup):
    app, model, probe, _, _ = setup
    task = pause_after_group(app, model, probe)
    with app.store.transaction() as db:
        db.execute('UPDATE task_control SET elapsed=181 WHERE task_id=?', (task.id,))
    app.continue_task(task.id, 'continue-budget')
    assert app.wait(task.id, 10).status == TaskStatus.NEEDS_REVIEW
    assert len(model.requests) == 1


def test_pause_waiting_resume_does_not_authorize(setup):
    app, model, _, _, _ = setup
    model.fn = lambda messages, tools: LLMResponse('', (ToolCall('write_text', {'path':'draft.txt','text':'test'}),))
    task = app.submit('写文件')
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    assert app.pause_task(task.id, 'pause-wait').status == TaskStatus.PAUSED
    assert app.continue_task(task.id, 'continue-wait').status == TaskStatus.CREATED
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    assert not app.journal.attempts(task.id)
    assert 'confirm' in app.task_review(task.id)['available_actions']


def test_pause_persistence_failure_rolls_back(setup, monkeypatch):
    app, model, _, _, _ = setup
    model.fn = lambda messages, tools: LLMResponse('', (ToolCall('write_text', {'path':'draft.txt','text':'test'}),))
    task = app.submit('写文件')
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    original = app.tasks.update_status
    monkeypatch.setattr(app.task_control.tasks, 'update_status', lambda *a, **kw: (_ for _ in ()).throw(sqlite3.OperationalError('synthetic')))
    with pytest.raises(sqlite3.OperationalError):
        app.pause_task(task.id, 'fail-pause')
    assert app.tasks.get(task.id).status == TaskStatus.WAITING
    assert not app.task_control.snapshot(task.id)['requested']


def test_proactive_quiet_uses_core_no_fake_user_no_empty_message(setup):
    app, model, _, _, _ = setup
    task, conversation = user_turn(app)
    feedback_count = len(model.feedback)
    enable(app)
    identity = offer(app, task)
    model.fn = lambda messages, tools: LLMResponse('{"action":"quiet","reply":""}')
    app.proactive.poll()
    active = attention_task(app, identity)['task_id']
    result = app.wait(active, 10)
    assert result.status == TaskStatus.COMPLETED, result
    assert result.response == ''
    assert app.journal.origin(active)['kind'] == 'proactive'
    assert 'task_origin' in '\n'.join(m.content for m in model.requests[-1])
    assert len(model.feedback) == feedback_count  # quiet 不再创建一份反馈输入；此前用户轮次仍正常学习。
    assert app.learning.receipt(active)['reason'] == 'model_deferred'
    timeline = app.conversation_timeline(conversation['id'])['items']
    assert not [item for item in timeline if item['task_id'] == active]


def test_proactive_answer_published_once_and_never_user_instruction(setup):
    app, model, _, _, _ = setup
    task, conversation = user_turn(app)
    enable(app)
    identity = offer(app, task)
    app.proactive.poll()
    active = attention_task(app, identity)['task_id']
    assert app.wait(active, 10).status == TaskStatus.COMPLETED
    app.proactive.poll()
    app.proactive.poll()
    items = [item for item in app.conversation_timeline(conversation['id'])['items'] if item['task_id'] == active]
    assert len([i for i in items if i['kind']=='assistant_message' and i['body']]) == 1
    assert not [i for i in items if i['kind']=='user_message']
    assert attention_task(app, identity)['attempts'] == 1


def test_new_pause_during_model_suppresses_late_publish(setup):
    app, model, _, _, _ = setup
    task, conversation = user_turn(app)
    enable(app)
    identity = offer(app, task)
    entered, release = Event(), Event()
    def blocked(messages, tools):
        entered.set()
        assert release.wait(10)
        return LLMResponse('想主动说的话')
    model.fn = blocked
    app.proactive.poll()
    active = attention_task(app, identity)['task_id']
    try:
        assert entered.wait(5)
        app.notification_control('chat', True, 'mute-late')
    finally:
        release.set()
    assert app.wait(active, 10).response == ''
    assert not [i for i in app.conversation_timeline(conversation['id'])['items'] if i['task_id']==active]


def test_policy_scopes_deadline_and_restart(setup):
    app, _, _, clock, config = setup
    end = (clock() + timedelta(hours=2)).isoformat()
    app.notification_control('chat', True, 'mute-chat', end)
    assert not app.notification_policy.allowed('chat')
    assert app.notification_policy.allowed('reminder')
    app.notification_control('reminder', True, 'mute-reminder')
    assert app.close(10)
    other = create_application(config)
    try:
        other.notification_policy.clock = clock
        assert not other.notification_policy.allowed('chat')
        clock.advance(7201)
        assert other.notification_policy.allowed('chat')
        assert not other.notification_policy.allowed('reminder')
    finally:
        assert other.close(10)


def test_policy_invalid_and_deduplicated(setup):
    app, _, _, _, _ = setup
    first = app.notification_control('chat', True, 'mute')
    assert app.notification_control('chat', True, 'mute')['chat']['revision'] == first['chat']['revision']
    with pytest.raises(ValueError):
        app.notification_control('reminder', True, 'mute')
    with pytest.raises(ValueError):
        app.notification_control('chat', True, 'naive', '2026-11-01T08:00:00')


def test_attention_dedup_trigger_removal_and_sources(setup):
    app, _, _, _, _ = setup
    task, _ = user_turn(app)
    enable(app)
    first = offer(app, task)
    assert offer(app, task) == first
    app.proactive.method('review', False)
    assert attention_task(app, first)['status'] == 'cancelled'
    assert app.tasks.get(task.id).status == TaskStatus.COMPLETED
    assert app.notification_policy.allowed('chat')


def test_expired_and_revoked_attention_not_executed(setup):
    app, model, _, clock, _ = setup
    task, _ = user_turn(app)
    enable(app)
    identity = offer(app, task)
    with app.store.transaction() as db:
        db.execute("UPDATE dependency_version SET active=0 WHERE kind='dialogue_turn' AND object_id=?", (task.id+':user',))
    app.proactive.poll()
    assert attention_task(app, identity)['status'] == 'invalidated'
    assert len(model.requests) == 1
    clock.advance(3*86400)
    app.proactive.poll()
    assert not attention_task(app, identity)['task_id']


def test_model_review_clamped_and_no_silence_based_wakeup(setup):
    app, _, _, clock, _ = setup
    task, _ = user_turn(app)
    identity = app.proactive.schedule(task.id, '有依据的复查', '用户提到面试，之后检查是否仍适用', (clock()+timedelta(seconds=1)).isoformat())
    row = attention_task(app, identity)
    assert datetime.fromisoformat(row['due_at']) >= clock()+timedelta(seconds=900)
    enable(app)
    app.proactive.poll()
    assert not attention_task(app, identity)['task_id']


def test_proactive_cannot_mutate_persona_as_user(setup):
    app, model, _, _, _ = setup
    task, _ = user_turn(app)
    enable(app)
    identity = offer(app, task)
    model.fn = lambda messages, tools: LLMResponse('', (ToolCall('memory_remember', {'content':'凭空认为用户喜欢这个'}),))
    app.proactive.poll()
    active = attention_task(app, identity)['task_id']
    assert app.wait(active, 10).status == TaskStatus.NEEDS_REVIEW
    assert not app.journal.attempts(active)


def test_desktop_control_receipts_and_actual_status(setup):
    app, model, probe, _, _ = setup
    task = pause_after_group(app, model, probe)
    facade = DesktopFacade(app)
    review = facade.execute('task_review', {'task_id':task.id})
    assert 'task_continue' in review['available_actions']
    assert review['pause_control']['checkpoint'] == 'continue'
    facade.execute('task_continue', {'task_id':task.id, 'command_id':'desktop-continue'})
    assert app.wait(task.id, 10).status == TaskStatus.COMPLETED
    assert facade.execute('command_status', {'command_id':'desktop-continue'})['accepted']


def test_workflow_v4_upgrade_and_missing_current_schema_rejected(tmp_path):
    path = tmp_path/'workflow.sqlite3'
    store = WorkflowStore(path)
    with store.transaction() as db:
        from deskpal.workflow.schema_v5 import REQUIRED
        for table in REQUIRED:
            db.execute('DROP TABLE '+table)
        db.execute('UPDATE schema_version SET version=4')
    store.close()
    store = WorkflowStore(path)
    with store.read_connection() as db:
        assert db.execute('SELECT version FROM schema_version').fetchone()[0] == 5
    store.close()
    db = sqlite3.connect(path)
    db.execute('DROP TABLE attention'); db.commit(); db.close()
    with pytest.raises(ValueError):
        WorkflowStore(path)


def test_proactive_answer_is_assistant_only_history(setup):
    app, model, _, _, _ = setup
    task, conversation = user_turn(app)
    enable(app)
    identity = offer(app, task)
    app.proactive.poll()
    active = attention_task(app, identity)['task_id']
    assert app.wait(active, 10).status == TaskStatus.COMPLETED
    user_turn(app)
    historical = '\n'.join(m.content for m in model.requests[-1])
    assert active + ':assistant' in historical
    assert active + ':user' not in historical


def test_proactive_external_action_still_waits_for_permission(setup):
    app, model, _, _, _ = setup
    task, _ = user_turn(app)
    enable(app)
    identity = offer(app, task)
    model.fn = lambda messages, tools: LLMResponse('', (ToolCall('write_text', {'path':'plan.txt','text':'test'}),))
    app.proactive.poll()
    active = attention_task(app, identity)['task_id']
    assert app.wait(active, 10).status == TaskStatus.WAITING
    assert not app.journal.attempts(active)
    assert 'confirm' in app.task_review(active)['available_actions']
    assert not (app.config.sandbox.workspace_dir/'plan.txt').exists()


def test_active_user_task_defers_proactive_check(setup):
    app, model, probe, _, _ = setup
    previous, _ = user_turn(app)
    enable(app)
    identity = offer(app, previous)
    model.fn = lambda messages, tools: LLMResponse('', (ToolCall('probe', {'value':'work'}),))
    probe.block = True
    task = app.submit('正在工作')
    try:
        assert probe.entered.wait(5)
        app.proactive.poll()
        assert not attention_task(app, identity)['task_id']
        assert app.tasks.get(task.id).status == TaskStatus.RUNNING
    finally:
        model.fn = lambda messages, tools: LLMResponse('完成')
        probe.release.set()
    assert app.wait(task.id, 10).status == TaskStatus.COMPLETED


def test_source_clear_during_pause_does_not_repopulate_chat(setup):
    app, model, probe, _, _ = setup
    conversation = app.conversation_ensure_default()
    count = [0]
    def model_fn(messages, tools):
        count[0] += 1
        return LLMResponse('', (ToolCall('probe', {'value':'work'}),)) if count[0]==1 else LLMResponse('完成')
    model.fn = model_fn
    probe.block = True
    task = app.submit('处理这个事项', conversation_id=conversation['id'], expected_context_epoch=conversation['context_epoch'])
    assert probe.entered.wait(5)
    app.pause_task(task.id, 'pause-clear')
    probe.release.set()
    assert app.wait(task.id, 10).status == TaskStatus.PAUSED
    current = app.conversation_ensure_default()
    app.conversation_clear(current['id'], current['revision'], 'clear')
    with pytest.raises(WorkflowBlocked):
        app.continue_task(task.id, 'resume-clear')
    assert not [i for i in app.conversation_timeline(current['id'])['items'] if i['body']]


def test_transient_proactive_failure_retries_but_configuration_failure_does_not(setup):
    from deskpal.llm.errors import RetryableLLMError, LLMError
    app, model, _, clock, _ = setup
    task, _ = user_turn(app)
    enable(app)
    identity = offer(app, task)
    def failed(messages, tools):
        raise RetryableLLMError('temporary synthetic')
    model.fn = failed
    for index in range(3):
        app.proactive.poll()
        active = attention_task(app, identity)['task_id']
        assert app.wait(active, 10).status == TaskStatus.FAILED
        app.proactive.poll()
        clock.advance(901)
    assert attention_task(app, identity)['attempts']==3
    assert attention_task(app, identity)['status']=='done'
    second = offer(app, task, 'config-fail')
    def bad_config(messages, tools):
        raise LLMError('configuration synthetic')
    model.fn = bad_config
    app.proactive.poll()
    active = attention_task(app, second)['task_id']
    assert app.wait(active, 10).status == TaskStatus.FAILED
    app.proactive.poll()
    assert attention_task(app, second)['attempts']==1
    assert attention_task(app, second)['status']=='done'


def test_reminders_still_fulfil_without_model_and_pause_does_not_replay(tmp_path):
    from deskpal.application.reminders import ReminderInbox
    from deskpal.proactive.policy import NotificationPolicy
    from deskpal.scheduling import SchedulingService
    class ScheduleClock:
        def __init__(self):
            self.wall = datetime.now(timezone.utc)
            self.elapsed = 0.0
        def now(self): return self.wall
        def monotonic(self): return self.elapsed
        def advance(self, seconds):
            self.wall += timedelta(seconds=seconds)
            self.elapsed += seconds
    clock = ScheduleClock()
    store = WorkflowStore(tmp_path/'workflow.sqlite3')
    scheduling = SchedulingService(tmp_path/'scheduling.sqlite3', clock=clock)
    policy = NotificationPolicy(store, clock=clock.now, quiet_start='00:00', quiet_end='00:00')
    inbox = ReminderInbox(store, scheduling, now=clock.now, policy=policy)
    try:
        policy.set('chat', True, None, 'mute-chat')
        timer = scheduling.create_timer(1, label='真实的合成提醒')
        clock.advance(1); scheduling.poll_due(); inbox.poll()
        first = inbox.feed()['items'][0]
        assert inbox.notification(first['id']) is not None
        policy.set('reminder', True, None, 'mute-reminder')
        second = scheduling.create_timer(1, label='暂停期间提醒')
        clock.advance(1); scheduling.poll_due(); inbox.poll()
        assert json.loads(scheduling.source_snapshot('timer', second.id).payload_json)['status'] == 'fired'
        assert all(inbox.notification(item['id']) is None for item in inbox.feed()['items'])
        policy.set('reminder', False, None, 'unmute')
        assert all(inbox.notification(item['id']) is None for item in inbox.feed()['items'])
        assert len(inbox.feed()['items'])==2
    finally:
        scheduling.close(); store.close()


def test_fallback_review_reuses_feedback_and_no_repeat_without_new_revision(setup):
    from deskpal.memory.contracts import MemorySource, MemoryDraft, ClaimType, MemoryStatus
    app, model, _, clock, _ = setup
    repo = app.learning.repository
    item = repo.ingest(MemorySource('用户说今天疲惫', id='source-review'),
        MemoryDraft('可能更想先被倾听', '用户说今天疲惫', claim_type=ClaimType.HYPOTHESIS,
            reason='只是暂时理解', uncertainty='不推广到所有时候'), idempotency_key='seed-review')
    def feedback(data):
        result = dict(memories=[], reviews=[], habits=[], emotion=None, summary=None)
        if data['understandings']:
            current = data['understandings'][0]
            key = current['original_source_key']
            result['reviews'] = [dict(id=current['id'], revision=current['revision'], decision='retain',
                target=None, temporal=False, content=current['content'], quote=data['catalog'][key]['content'],
                destination='facts', claim_type='hypothesis', reason='复查后仍不确定，继续保留原理解',
                uncertainty='需要后续交流才能判断', sources=[key], review_after=(app.proactive.clock() + timedelta(days=1)).isoformat(), valid_from=None, valid_to=None)]
        return result
    model.feedback_fn = feedback
    enable(app)
    app.proactive.method('fallback', True)
    app.proactive.poll()
    until(lambda: bool(app.learning.store.records(20)))
    assert repo.get(item.id).status == MemoryStatus.CANDIDATE
    assert repo.get(item.id).revision == item.revision + 1
    assert any('复查后仍不确定' in row['reason'] for row in app.learning.store.records(20))
    assert not app.conversation_timeline(app.conversation_ensure_default()['id'])['items']
    previous = len(model.feedback)
    app.proactive.poll()
    app.proactive.poll()
    assert len(model.feedback)==previous


def test_real_event_source_reuses_proposal_reception_and_deduplicates(setup):
    app, model, _, clock, _ = setup
    # 显式目标就是原有事件来源，无须旧人工审核才能走到Core。
    goal = app.goals.create('准备面试', '核对准备进展', use_policy='cloud_allowed')
    proposal = app.proposals.create(goal.id, rule_id='synthetic-rule', root_source_key='event-source',
        kind='deadline', due_at=clock().isoformat(), reason='目标截止前核对准备进展')
    enable(app)
    app.proactive.method('event', True)
    app.rules._clock = clock
    model.fn = lambda messages, tools: LLMResponse('{"action":"quiet","reply":""}')
    app.proactive.poll()
    with app.store.read_connection() as db:
        row = db.execute("SELECT * FROM attention WHERE method='event'", ()).fetchone()
    assert row is not None
    assert app.wait(row['task_id'], 10).status == TaskStatus.COMPLETED
    app.proactive.poll()
    with app.store.read_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM attention WHERE method='event'").fetchone()[0]==1


def test_chat_notification_control_is_real_management_tool(setup):
    app, model, _, _, _ = setup
    count = [0]
    def decide(messages, tools):
        count[0]+=1
        return LLMResponse('', (ToolCall('notification_control', {'kind':'chat','paused':True}),)) if count[0]==1 else LLMResponse('已暂停主动聊天，提醒照常。')
    model.fn = decide
    result = app.run('今天先别主动找我')
    assert result.status == TaskStatus.COMPLETED
    assert not app.notification_policy.allowed('chat')
    assert app.notification_policy.allowed('reminder')
    assert app.journal.attempts(result.task_id)[0].result.success


def test_pause_queued_task_before_first_model(setup):
    app, model, probe, _, _ = setup
    count = [0]
    def decide(messages, tools):
        count[0]+=1
        return LLMResponse('', (ToolCall('probe', {'value':'first'}),)) if count[0]==1 else LLMResponse('完成')
    model.fn=decide
    probe.block=True
    first=app.submit('先处理前一项')
    try:
        assert probe.entered.wait(5)
        queued=app.submit('排队项')
        app.pause_task(queued.id, 'pause-before-model')
    finally:
        probe.release.set()
    assert app.wait(first.id, 10).status==TaskStatus.COMPLETED
    assert app.wait(queued.id, 10).status==TaskStatus.PAUSED
    assert count[0]==2
    app.continue_task(queued.id, 'continue-first-model')
    assert app.wait(queued.id, 10).status==TaskStatus.COMPLETED
    assert count[0]==3


def test_pause_during_final_answer_does_not_claim_already_paused(setup):
    app, model, _, _, _=setup
    entered, release=Event(), Event()
    def decide(messages, tools):
        entered.set()
        assert release.wait(10)
        return LLMResponse('直接完成的回复')
    model.fn=decide
    task=app.submit('普通聊天')
    try:
        assert entered.wait(5)
        assert app.pause_task(task.id, 'pause-no-next-round').status==TaskStatus.PLANNING
    finally:
        release.set()
    assert app.wait(task.id, 10).status==TaskStatus.COMPLETED
    assert app.tasks.get(task.id).status!=TaskStatus.PAUSED


def test_attention_page_readable_and_manual_approval_retired(setup):
    app, _, _, _, _=setup
    source,_=user_turn(app)
    enable(app)
    identity=offer(app, source)
    page=DesktopFacade(app).execute('attentions', {'limit':20})
    assert page['items'][0]['id']==identity
    assert page['items'][0]['title']
    assert 'refs' not in page['items'][0]
    assert app.proposal_summary()==()


def test_pause_clarification_preserves_question(setup):
    app, model, _, _, _=setup
    original = model.chat
    expressions = []
    def chat(messages, *, tools=()):
        if messages[0].content.startswith('[EXPRESSION]'):
            payload = json.loads(messages[-1].content)
            expressions.append(payload)
            assert payload['response_intent'] == 'clarify'
            # 提示词模式输出正文；替身也须保留原问题，不能用固定完成语替换。
            return LLMResponse('我想先确认一下：' + payload['unverified_assistant_draft'])
        return original(messages, tools=tools)
    model.chat = chat
    model.fn=lambda messages, tools: LLMResponse('{"action":"clarify","reply":"要处理哪一项？"}')
    task=app.submit('处理这个')
    first = app.wait(task.id, 10)
    assert first.status==TaskStatus.AWAITING_INPUT
    assert first.response == '我想先确认一下：要处理哪一项？'
    app.pause_task(task.id, 'pause-question')
    assert app.continue_task(task.id, 'continue-question').status==TaskStatus.AWAITING_INPUT
    assert app.wait(task.id, 10).response==first.response
    assert len(model.requests)==1 and len(expressions)==1


def test_internal_trigger_register_and_remove_only_its_pending_attention(setup):
    app, _, _, _, _=setup
    task,_=user_turn(app)
    enable(app)
    calls=[]
    app.proactive.register_trigger('synthetic_event', lambda: calls.append('checked'))
    source=app._humanization.turn(task.id)
    identity=app.proactive.offer(key='registered-event',method='synthetic_event',goal='检查真实事项',basis='真实来源，回应后停止关注',
        refs=[dict(kind='dialogue_turn',id=task.id+':user',revision=source['sequence'])])
    app.proactive.unregister_trigger('synthetic_event')
    app.proactive.poll()
    assert calls==[]
    assert attention_task(app,identity)['status']=='cancelled'
    assert app.tasks.get(task.id).status==TaskStatus.COMPLETED


def test_related_event_opportunities_merge_before_model_check(setup):
    app, _, _, clock, _=setup
    goal=app.goals.create('面试准备','核对准备进展',use_policy='cloud_allowed')
    for index in range(2):
        app.proposals.create(goal.id,rule_id='event-'+str(index),root_source_key='source-'+str(index),
            kind='deadline',due_at=clock().isoformat(),reason='准备事项'+str(index))
    enable(app)
    app.proactive.method('event',True)
    app.proactive.collect()
    app.proactive.collect()
    with app.store.read_connection() as db:
        rows=db.execute("SELECT * FROM attention WHERE method='event'").fetchall()
    assert len(rows)==1
    assert len(json.loads(rows[0]['refs']))==2
    assert rows[0]['task_id'] is None


def test_paused_authorization_reconsiders_amendment_before_any_write(setup):
    app, model, _, _, _=setup
    count=[0]
    def decide(messages,tools):
        count[0]+=1
        if count[0]==1:
            return LLMResponse('',(ToolCall('write_text',{'path':'old.txt','text':'old'}),))
        if count[0]==2:
            assert '改写到new.txt' in '\n'.join(m.content for m in messages)
            return LLMResponse('',(ToolCall('write_text',{'path':'new.txt','text':'new'}),))
        return LLMResponse('已写入新文件')
    model.fn=decide
    task=app.submit('写文件')
    assert app.wait(task.id,10).status==TaskStatus.WAITING
    original=app.journal.get_plan(task.id).canonical_json
    app.pause_task(task.id,'pause-old-plan')
    app.append_requirement(task.id,'改写到new.txt，不要写旧文件','change-destination')
    app.continue_task(task.id,'continue-new-plan')
    assert app.wait(task.id,10).status==TaskStatus.WAITING
    review=app.task_review(task.id)
    assert review['steps'][0]['arguments']['path']=='new.txt'
    assert not app.journal.attempts(task.id)
    with app.store.read_connection() as db:
        assert db.execute('SELECT canonical_json FROM immutableplan WHERE task_id=?',(task.id,)).fetchone()[0]==original
    app.enqueue_confirmation(task.id,authorized_tools=['write_text'],plan_hash=review['plan_hash'],expected_revision=review['task_revision'],command_id='approve-new')
    assert app.wait(task.id,10).status==TaskStatus.COMPLETED
    assert not (app.config.sandbox.workspace_dir/'old.txt').exists()
    assert (app.config.sandbox.workspace_dir/'new.txt').read_text(encoding='utf-8')=='new'
    assert len(app.journal.attempts(task.id))==1
    assert app.learning.store.job('turn:'+task.id)['status']=='completed'
    assert app.learning.store.job('turn:'+task.id)['conversation_id']=='single:'+task.id


def test_user_submission_waits_for_active_internal_review_without_preemption(setup):
    from deskpal.memory.contracts import MemorySource,MemoryDraft
    app,model,_,_,_=setup
    app.learning.repository.ingest(MemorySource('真实的整理依据'),MemoryDraft('待整理理解','真实的整理依据'),idempotency_key='review-source')
    enable(app);app.proactive.method('fallback',True)
    entered,release=Event(),Event()
    def review(data):
        if 'user' not in data['catalog']:
            entered.set()
            assert release.wait(10)
        return dict(memories=[],reviews=[],habits=[],emotion=None,summary=None)
    model.feedback_fn=review
    app.proactive.poll()
    try:
        assert entered.wait(5)
        task=app.submit('整理期间的新聊天')
        assert app.tasks.get(task.id).status==TaskStatus.CREATED
        assert app._dispatcher.diagnostics()['internal_active']
        assert not model.requests
    finally:
        release.set()
    assert app.wait(task.id,10).status==TaskStatus.COMPLETED


def test_reflection_quiet_defers_learning_until_explicit_silent_review(setup):
    from deskpal.proactive.reflection import read
    app, model, _, clock, _ = setup
    app.notification_policy.set('chat', True, None, 'pause-chat-for-thinking')
    decisions = iter([
        LLMResponse('', (ToolCall('reflection_record', dict(topic='自己的形象', content='我喜欢简洁而有辨识度的形象。', reason='来自自己的审美探索'), 'thought'),)),
        LLMResponse('{"action":"quiet","reply":""}'),
    ])
    model.fn = lambda messages, tools: next(decisions)
    def learn(data):
        key, row = next((key, row) for key, row in data['catalog'].items() if row['origin'] == 'character_reflection')
        assert row['speaker_kind'] == 'character' and 'user' not in data['catalog']
        assert not any(x['origin'] == 'tool_result' for x in data['catalog'].values())
        return dict(memories=[dict(content=row['content'], quote=row['content'], sources=[key],
            destination='facts', claim_type='hypothesis', reason='角色自身的兴趣', subject=row['speaker_id'],
            review_after=(clock() + timedelta(days=1)).isoformat())],
            followups=[dict(goal='继续探索形象', reason='还有可探索的方向', mode='reflection',
                due_at=(clock() + timedelta(hours=2)).isoformat(), sources=[key])])
    model.feedback_fn = learn
    task = app.submit_proactive('探索自己的形象', '合成自主思考机会', 'think-quiet')
    result = app.wait(task.id, 10)
    assert result.status == TaskStatus.COMPLETED and not result.response
    assert read(app, task.id)['content'] == '我喜欢简洁而有辨识度的形象。'
    assert not model.feedback and not app.memory.list_items()
    assert app.learning.store.job('turn:' + task.id) is None
    app.proactive.freeze_review('organize-own-thought', [], materials=app.learning.materials.select())
    app.learning.run_review('organize-own-thought')
    item, = app.memory.list_items()
    assert item.source.origin == 'character_reflection' and item.draft.subject.startswith('character:')
    assert item.draft.subject == item.source.speaker_id
    assert app.learning.store.job('review:organize-own-thought')['status'] == 'completed'
    assert not app.conversation_timeline(app.conversation_ensure_default()['id'])['items']
    followup, = app.proactive.page()['items']
    assert followup['method'] == 'reflection'
    model.feedback_fn = lambda data: dict(reviews=[dict(id=item.id, decision='adopt', reason='这是角色自己的兴趣，不是外部事实')])
    app.proactive.freeze_review('adopt-own-thought', [dict(kind='memory', id=item.id, revision=item.revision)])
    app.learning.run_review('adopt-own-thought')
    from deskpal.memory.contracts import MemoryStatus
    assert app.memory.repository.get(item.id).status == MemoryStatus.ACCEPTED
    with app.store.transaction() as db:
        db.execute("DELETE FROM context_snapshot WHERE task_id=? AND purpose='reflection'", (task.id,))
    with pytest.raises(WorkflowBlocked):
        app.learning._source_valid([dict(kind='reflection', id=task.id, revision=1)])
    with app.store.read_connection() as db:
        pending = dict(db.execute('SELECT * FROM attention WHERE id=?', (followup['id'],)).fetchone())
    assert not app.proactive.valid(pending)


def test_reflection_opportunity_persistent_budget_pause_and_no_catchup(setup):
    app, model, _, clock, _ = setup
    enable(app)
    app.notification_policy.set('chat', True, None, 'silent-thinking')
    model.fn = lambda messages, tools: LLMResponse('{"action":"quiet","reply":""}')
    app.proactive.poll()
    assert not model.requests
    for index in range(3):
        clock.advance(4 * 3600)
        app.proactive.poll()
        until(lambda: len(model.requests) == index + 1)
        until(lambda: not app._dispatcher.diagnostics()['active_task_id'])
    clock.advance(4 * 3600)
    app.proactive.poll()
    assert len(model.requests) == 3 and not app.memory.list_items()
    saved = app.proactive._meta('reflection_next')
    app.proactive.method('reflection', False)
    clock.advance(48 * 3600)
    app.proactive.poll()
    assert len(model.requests) == 3 and app.proactive._meta('reflection_next') == saved
    app.proactive.method('reflection', True)
    app.proactive.poll()
    until(lambda: len(model.requests) == 4)
    app.proactive.poll()
    assert len(model.requests) == 4  # 只做当前一次，不补做关闭期间的全部机会。


def test_reflection_followup_reenters_core_under_chat_pause(setup):
    app, model, _, clock, _ = setup
    enable(app)
    app.notification_policy.set('chat', True, None, 'no-chat')
    decisions = iter([
        LLMResponse('', (ToolCall('reflection_record', dict(topic='主题', content='还想继续研究自己的审美。', reason='尚未想清楚')),)),
        LLMResponse('{"action":"quiet","reply":""}'),
    ])
    model.fn = lambda messages, tools: next(decisions)
    task = app.submit_proactive('合成思考', '合成依据', 'reflection-parent')
    assert app.wait(task.id, 10).status == TaskStatus.COMPLETED
    identity = app.proactive.schedule(task.id, '继续探索审美', '需要进一步考虑',
        (clock() + timedelta(hours=2)).isoformat(), 'reflection')
    model.fn = lambda messages, tools: LLMResponse('{"action":"quiet","reply":""}')
    before = len(model.requests)
    clock.advance(2 * 3600)
    app.proactive.poll()
    until(lambda: len(model.requests) == before + 1)
    assert attention_task(app, identity)['task_id'] != task.id
    assert not app.notification_policy.allowed('chat')


def test_reflection_budget_survives_restart(setup):
    app, model, _, clock, config = setup
    enable(app)
    app.proactive._meta('reflection_runs', json.dumps([clock().isoformat()] * 3))
    app.proactive._meta('reflection_next', (clock() - timedelta(hours=1)).isoformat())
    assert app.close(timeout=10)
    with create_application(config) as reopened:
        reopened.proactive.clock = clock
        assert not reopened.proactive._think()
        assert not model.requests


def test_reflection_cannot_be_submitted_by_user_or_on_finished_task(setup):
    from deskpal.proactive.reflection import record
    app, _, _, _, _ = setup
    task, _ = user_turn(app)
    with pytest.raises(WorkflowBlocked):
        record(app, task.id, '主题', '正文', '依据')
    task = app.submit_proactive('思考', '合成依据', 'done-thought')
    assert app.wait(task.id, 10).status == TaskStatus.COMPLETED
    with pytest.raises(WorkflowBlocked):
        record(app, task.id, '主题', '正文', '依据')


def test_reflection_alone_cannot_adopt_external_guess(setup):
    from deskpal.memory.contracts import MemorySource, MemoryDraft, ClaimType, MemoryError
    app, _, _, _, _ = setup
    source = MemorySource('他一定不喜欢复杂形象', origin='character_reflection',
        speaker_id='character:test', speaker_kind='character')
    draft = MemoryDraft(source.content, source.content, claim_type=ClaimType.HYPOTHESIS, subject='user')
    with pytest.raises(MemoryError):
        app.memory.repository.ingest(source, draft, accepted=True)
    with pytest.raises(MemoryError):
        app.memory.repository.ingest(source, replace(draft, claim_type=ClaimType.USER_REPORT))
