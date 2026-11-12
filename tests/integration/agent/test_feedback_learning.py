"""P4：真实应用、合成档案、可控模型；不读取真实连接或用户数据。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from threading import Event
import time

import pytest

from deskpal.application import create_application, factory
from deskpal.config import load_config
from deskpal.llm.errors import RetryableLLMError, LLMError
from deskpal.llm.models import LLMResponse
from deskpal.memory.contracts import MemoryStatus, MemoryDraft, MemorySource, ClaimType, UsePolicy
from deskpal.memory.repository import SQLiteMemoryRepository
from deskpal.memory.models import MemoryKind
from deskpal.tasks.models import TaskStatus
from deskpal.workflow.models import WorkflowBlocked


def empty():
    return dict(memories=[], reviews=[], habits=[], emotion=None, summary=None)


def draft(content, quote, *, destination='facts', sources=None, **kwargs):
    return dict(content=content, quote=quote, destination=destination, claim_type='user_report',
        reason='来自实际交流，目前不推广到其他情境', uncertainty='仍需后续核对，适用于所述时间',
        sources=sources or ['user'], review_after=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), valid_from=None, valid_to=None, **kwargs)


def review(payload, *, decision='adopt', content=None, quote=None, sources=None, destination=None):
    item = payload['understandings'][0]
    key = item['original_source_key']
    raw = payload['catalog'][key]['content']
    return dict(draft(content or item['content'], quote or raw, destination=destination or item['destination'],
        sources=sources or [key]), id=item['id'], revision=item['revision'], decision=decision, target=None, temporal=False,
        review_after=(datetime.fromisoformat(payload['now']) + timedelta(days=1)).isoformat() if decision in ('retain', 'update') else None)


class Model:
    is_cloud = False

    def __init__(self):
        self.fn = lambda data: empty()
        self.inputs = []
        self.planning = []
        self.expressions = []
        self.failed = 0

    def chat(self, messages, *, tools=()):
        if messages[0].content.startswith('[CONTEXT_UNDERSTANDING]'):
            return LLMResponse('{"entries":[]}')
        if messages[0].content.startswith('[EXPRESSION]'):
            self.expressions.append(json.loads(messages[-1].content))
            return LLMResponse('{"reply_text":"这是本轮回复。"}' if 'OUTPUT PROTOCOL' in messages[0].content else '这是本轮回复。')
        if messages[0].content.startswith('[MEMORY_FEEDBACK]'):
            data = json.loads(messages[-1].content)
            self.inputs.append(data)
            result = self.fn(data)
            return result if isinstance(result, LLMResponse) else LLMResponse(json.dumps(result, ensure_ascii=False))
        self.planning.append(messages)
        return LLMResponse('这是实际生成的回答。')

    def generate(self, prompt):
        raise AssertionError('Unused provider method')


@pytest.fixture
def app_model(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    path.write_text('[llm]\nprovider="offline"\n[storage]\ndata_dir="data"\n[sandbox]\nworkspace_dir="workspace"\n[proactive]\nenabled=false\n', encoding='utf-8')
    model = Model()
    monkeypatch.setattr(factory, 'OfflineLLMProvider', lambda: model)
    model.settings = load_config(path, environ={})
    app = create_application(model.settings)
    try:
        yield app, model
    finally:
        assert app.close(timeout=10)


def turn(app, request, conversation=None):
    conversation = conversation or app.conversation_ensure_default()
    task = app.submit(request, conversation_id=conversation['id'], expected_context_epoch=conversation['context_epoch'])
    result = app.wait(task.id, timeout=10)
    assert result.status == TaskStatus.COMPLETED, result
    return result


def wait_until(check):
    until = time.monotonic() + 8
    while time.monotonic() < until:
        if check():
            return
        time.sleep(.02)
    raise AssertionError('Timed out waiting for owned worker')


def test_default_buffer_is_persistent_attributed_and_used_as_uncertain_context(app_model):
    app, model = app_model
    assert app.config.memory.mode == 'automatic'
    model.fn = lambda data: dict(empty(), memories=[draft('用户可能考虑搬到上海', '如果以后搬去上海') | {'claim_type': 'hypothesis'}])
    first = turn(app, '如果以后搬去上海，也许会租房')
    item = app.memory.list_items()[0]
    assert item.status == MemoryStatus.CANDIDATE
    assert item.draft.claim_type == ClaimType.HYPOTHESIS and item.draft.expires_at is None
    assert app.learning.store.job('turn:' + first.task_id)['status'] == 'completed'
    assert app.learning.store.refs(item.id)[0]['id'] == first.task_id + ':user'
    assert app.learning.store.records()[0]['reason'] == item.draft.reason
    from deskpal.application.desktop import DesktopFacade
    listed = DesktopFacade(app).execute('learning_records', {'limit': 1})
    assert listed['items'][0]['work_status'] == 'completed'
    assert listed['items'][0]['sources'][0]['id'] == first.task_id + ':user'
    model.fn = lambda data: empty()
    other = app.conversation_create('另一个聊天', 'other')
    turn(app, '上海租房的考虑', other)
    planning = '\n'.join(message.content for message in model.planning[-1])
    assert 'hypothesis' in planning and 'candidate' in planning and '用户可能考虑搬到上海' in planning
    assert app.core.memory_runtime.repository.get(item.id) is not None


@pytest.mark.parametrize('destination', ['facts', 'habit'])
def test_minimal_adoption_persists_without_retry_and_habit_reaches_role(app_model, destination):
    app, model = app_model
    model.fn = lambda data: dict(empty(), memories=[draft('交流时先倾听', '我希望先被倾听', destination=destination)])
    turn(app, '我希望先被倾听')
    original = app.memory.list_items()[0]
    previous_calls = len(model.inputs)
    def adopt(data):
        item = next(row for row in data['understandings'] if row['id'] == original.id)
        result = dict(empty(), reviews=[dict(id=item['id'], decision='adopt', reason='用户已明确表达')])
        if destination == 'habit':
            result['habits'] = [dict(content=item['content'], reason='从交流中形成的相处方式',
                sources=[item['original_source_key']], buffer_id=item['id'])]
        return result
    model.fn = adopt
    result = turn(app, '我们聊聊先倾听这件事')
    job = app.learning.store.job('turn:' + result.task_id)
    assert job['status'] == 'completed' and job['attempts'] == 1
    assert len(model.inputs) == previous_calls + 1
    current = app.memory.repository.get(original.id)
    assert current.revision == original.revision + 1
    assert current.draft.content == original.draft.content and current.draft.quote == original.draft.quote
    assert current.status == (MemoryStatus.ARCHIVED if destination == 'habit' else MemoryStatus.ACCEPTED)
    if destination == 'habit':
        role = app.core.companion_runtime.context('test', 0, result.task_id, allow_restricted=True)
        assert any(row['content'] == original.draft.content for row in role['learned_habits'])


def test_passive_review_revises_same_record_without_manual_acceptance(app_model):
    app, model = app_model
    model.fn = lambda data: dict(empty(), memories=[draft('疲惫时可能想先倾诉', '今天好累')])
    turn(app, '今天好累，我先说说')
    original = app.memory.list_items()[0]
    model.fn = lambda data: dict(empty(), reviews=[review(data, content='用户昨天疲惫，今天已经恢复',
        quote='那只是昨天，今天恢复了', sources=['user'])])
    turn(app, '那只是昨天，今天恢复了')
    current = app.memory.repository.get(original.id)
    assert current.status == MemoryStatus.ACCEPTED and current.revision == original.revision + 1
    assert current.draft.content == '用户昨天疲惫，今天已经恢复'
    assert len(app.memory.list_items()) == 1
    assert len(app.learning.store.refs(current.id)) == 2


@pytest.mark.parametrize('decision', ['retain', 'archive'])
def test_model_can_keep_important_uncertainty_or_clean_low_value(app_model, decision):
    app, model = app_model
    model.fn = lambda data: dict(empty(), memories=[draft('重要但不确定的搬家计划', '搬家计划还不确定')])
    turn(app, '搬家计划还不确定')
    item = app.memory.list_items()[0]
    model.fn = lambda data: dict(empty(), reviews=[review(data, decision=decision)])
    turn(app, '再看看搬家计划')
    current = app.memory.repository.get(item.id)
    assert current.status == (MemoryStatus.CANDIDATE if decision == 'retain' else MemoryStatus.ARCHIVED)
    assert current.draft.expires_at is None
    if decision == 'archive':
        assert not app.memory.retrieve('搬家计划').records


def test_exact_duplicate_and_repeated_job_do_not_create_new_memory(app_model):
    app, model = app_model
    model.fn = lambda data: dict(empty(), memories=[draft('最近喝咖啡', '最近喝咖啡')])
    turn(app, '最近喝咖啡')
    turn(app, '最近喝咖啡')
    assert len(app.memory.list_items()) == 1
    job = app.learning.store.records()[0]
    assert job['action'] == 'duplicate_retained'


def test_learning_blocks_publication_until_its_attempt_finishes(app_model):
    app, model = app_model
    entered, release = Event(), Event()
    def block(data):
        entered.set()
        assert release.wait(8)
        return empty()
    model.fn = block
    conversation = app.conversation_ensure_default()
    task = app.submit('先完成整理再发布', conversation_id=conversation['id'], expected_context_epoch=0)
    try:
        assert entered.wait(5)
        assert app.tasks.get(task.id).status == TaskStatus.RUNNING
        with app.store.read_connection() as db:
            assert db.execute('SELECT published FROM conversation_turn WHERE task_id=?', (task.id,)).fetchone()[0] == 0
            assert not db.execute("SELECT body FROM conversation_item WHERE task_id=? AND kind='assistant_message'", (task.id,)).fetchone()[0]
        assert app.learning.store.job('turn:' + task.id)['status'] == 'running'
    finally:
        release.set()
    assert app.wait(task.id, timeout=10).status == TaskStatus.COMPLETED
    assert app.learning.store.job('turn:' + task.id)['status'] == 'completed'


@pytest.mark.parametrize('failure,expected', [('temporary', 3), ('configuration', 1)])
def test_retry_budget_and_empty_success_are_distinct_from_claiming(app_model, failure, expected):
    app, model = app_model
    count = [0]
    def fail(data):
        count[0] += 1
        if failure == 'configuration':
            raise LLMError('synthetic invalid credential configuration')
        raise RetryableLLMError('synthetic timeout')
    model.fn = fail
    result = turn(app, '测试模型故障')
    job = app.learning.store.job('turn:' + result.task_id)
    assert count[0] == expected and job['attempts'] == expected and job['status'] == 'failed'
    assert result.warnings and not app.memory.list_items()
    assert app.core.companion_runtime.interaction_snapshot()['current_emotion'] is None


def test_temporary_failure_then_success_does_not_mark_first_claim_completed(app_model):
    app, model = app_model
    count = [0]
    def retry(data):
        count[0] += 1
        if count[0] < 3:
            raise RetryableLLMError('synthetic timeout')
        return empty()
    model.fn = retry
    result = turn(app, '第三次评估成功')
    job = app.learning.store.job('turn:' + result.task_id)
    assert job['status'] == 'completed' and job['attempts'] == 3
    assert not app.memory.list_items()


def test_atomic_commit_failure_keeps_no_partial_buffer_or_learning_log(app_model, monkeypatch):
    app, model = app_model
    model.fn = lambda data: dict(empty(), memories=[draft('临时理解', '临时理解')])
    original = app.learning.store.finish
    def fail(identity, status, error=''):
        if status == 'completed':
            raise OSError('synthetic disk commit failure')
        return original(identity, status, error)
    monkeypatch.setattr(app.learning.store, 'finish', fail)
    result = turn(app, '临时理解')
    assert app.learning.store.job('turn:' + result.task_id)['status'] == 'failed'
    assert not app.memory.list_items() and not app.learning.store.records()


def test_source_revision_change_during_model_wait_rejects_late_result(app_model):
    app, model = app_model
    item = app.memory.remember('最初的消息')
    entered, release = Event(), Event()
    def block(data):
        entered.set(); assert release.wait(8)
        return dict(empty(), memories=[draft('过时推测', '新的讨论')])
    model.fn = block
    conversation = app.conversation_ensure_default()
    task = app.submit('新的讨论', conversation_id=conversation['id'], expected_context_epoch=0)
    try:
        assert entered.wait(5)
        app.memory.correct(item.id, item.revision, '已经更正的消息')
    finally:
        release.set()
    try:
        app.wait(task.id, timeout=10)
    except WorkflowBlocked:
        pass
    assert not any(item.draft.content == '过时推测' for item in app.memory.list_items())


def test_clear_chat_preserves_memory_but_redacts_original_and_cancels_old_work(app_model):
    app, model = app_model
    model.fn = lambda data: dict(empty(), memories=[draft('喜欢绿茶', '我喜欢绿茶')])
    turn(app, '我喜欢绿茶，这段原话应该能清理')
    item = app.memory.list_items()[0]
    conversation = app.conversation_ensure_default()
    app.conversation_clear(conversation['id'], conversation['revision'], 'clear-memory-source')
    retained = app.memory.repository.get(item.id)
    assert retained.draft.content == '喜欢绿茶'
    assert retained.source.content == retained.draft.quote == '[原消息已清除]'
    assert all(not ref['available'] for ref in app.learning.store.refs(item.id) if ref['kind'] == 'dialogue_turn')
    assert all(not item.get('body') and item.get('redacted_at') for item in app.conversation_timeline(conversation['id'])['items'])


def test_forget_preserves_derived_buffer_and_its_learning_record(app_model):
    app, model = app_model
    model.fn = lambda data: dict(empty(), memories=[draft('共同准备的重要面试', '一起准备面试', destination='episode')])
    turn(app, '一起准备面试')
    parent = app.memory.list_items()[0]
    def derive(data):
        key = data['understandings'][0]['original_source_key']
        return dict(empty(), memories=[draft('面试可能值得后续关注', '一起准备面试', sources=[key])])
    model.fn = derive
    turn(app, '后续关注')
    child = next(item for item in app.memory.list_items() if item.id != parent.id)
    app.memory_command('forget', memory_id=parent.id, expected_revision=parent.revision)
    assert app.memory.repository.get(parent.id).status == MemoryStatus.DELETED
    assert app.memory.repository.get(child.id).status == MemoryStatus.CANDIDATE
    records = app.learning.store.records()
    assert not any(row['understanding'] or row['reason'] for row in records if row['memory_id'] == parent.id)
    assert any(row['understanding'] == child.draft.content for row in records if row['memory_id'] == child.id)


def test_emotion_and_explicit_habit_are_applied_once_and_used_next_turn(app_model):
    app, model = app_model
    before = app.core.companion_runtime.interaction_snapshot()['character_card']
    def learn(data):
        return dict(empty(), habits=[dict(id=None, content='适时自然结束，不机械追问', reason='用户明确要求以后如此',
            uncertainty='仍可按当次请求调整', sources=['user'], buffer_id=None, explicit=True)],
            emotion=dict(description_intensity='欣慰，同时有一点担心', reason_sources='共同面对难题', time_scope='当前交流',
                uncertainty='仍不知道最后效果', sources=['user'], expires_at=None))
    model.fn = learn
    result = turn(app, '以后不要每次都追问，我们一起面对这个难题')
    state = app.core.companion_runtime.interaction_snapshot()
    assert state['character_card'] == before
    assert state['current_emotion']['description_intensity'] == '欣慰，同时有一点担心'
    assert len(state['habits']) == 1
    app.learning.recover()
    assert app.core.companion_runtime.interaction_snapshot() == state
    assert app.learning.store.job('turn:' + result.task_id)['status'] == 'completed'
    model.fn = lambda data: empty()
    turn(app, '接着讨论')
    assert '不机械追问' in str(model.planning[-1])
    assert model.expressions[-1]['role_context']['learned_habits'][0]['content'] == '适时自然结束，不机械追问'


def test_inferred_habit_must_review_existing_buffer_and_preserve_identity(app_model):
    app, model = app_model
    model.fn = lambda data: dict(empty(), memories=[draft('倾诉时先倾听', '我想先倾诉', destination='habit')])
    turn(app, '我想先倾诉')
    buffered = app.memory.list_items()[0]
    def adopt(data):
        row = review(data, destination='habit')
        return dict(empty(), reviews=[row], habits=[dict(id=None, content='倾诉时先倾听', reason='根据原话和后续反馈复核',
            uncertainty='仅适用于倾诉情境', sources=row['sources'], buffer_id=row['id'], explicit=False)])
    model.fn = adopt
    turn(app, '这种相处方式也适合之后')
    assert app.memory.repository.get(buffered.id).status == MemoryStatus.ARCHIVED
    assert len(app.core.companion_runtime.interaction_snapshot()['habits']) == 1


def test_long_conversation_summary_is_generated_with_real_published_sources(app_model):
    app, model = app_model
    turn(app, '方案甲是读书，方案乙是实践，还未决定')
    model.fn = lambda data: dict(empty(), summary=dict(progress=['比较读书与实践'], decisions=[], open_questions=['尚未决定']))
    turn(app, '继续比较')
    conversation = app.conversation_ensure_default()
    saved = app._conversation_repository.context_summary(conversation['id'], conversation['context_epoch'])
    assert saved['content']['open_questions'] == ['尚未决定']
    with app.store.read_connection() as db:
        refs = db.execute("SELECT kind,object_id FROM conversation_summary_source WHERE conversation_id=?",
                          (conversation['id'],)).fetchall()
    assert refs and all(row['kind'] == 'dialogue_turn' for row in refs)
    turn(app, '第二种方案呢')
    assert 'conversation_summary' in str(model.planning[-1])


def test_internal_review_uses_dispatcher_without_creating_chat(app_model):
    app, model = app_model
    model.fn = lambda data: dict(empty(), memories=[draft('有价值的未定计划', '计划还不确定')])
    turn(app, '计划还不确定')
    conversation = app.conversation_ensure_default()
    before = deepcopy(app.conversation_timeline(conversation['id'])['items'])
    model.fn = lambda data: dict(empty(), reviews=[review(data, decision='retain')])
    from deskpal.application.desktop import DesktopFacade
    due = app.memory.list_items()[0].draft.review_after
    app.proactive.clock = lambda: due + timedelta(seconds=1)
    receipt = DesktopFacade(app).execute('learning_review', {'command_id': 'review-one'})
    assert receipt['accepted'] and receipt['work_id'] == 'review:review-one'
    wait_until(lambda: (app.learning.store.job('review:review-one') or {}).get('status') == 'completed')
    assert app.conversation_timeline(conversation['id'])['items'] == before
    assert len(app.memory.list_items()) == 1
    assert app.memory.list_items()[0].revision == 2


def test_temporal_change_and_negation_do_not_drop_related_history(app_model):
    app, model = app_model
    source = MemorySource('以前住北京')
    old = app.memory.repository.ingest(source, MemoryDraft('以前住北京', '以前住北京', valid_from=datetime(2020,1,1,tzinfo=timezone.utc)), accepted=True)
    def change(data):
        row = review(data, content='现在住杭州', quote='现在住杭州', sources=['user'])
        row['temporal'] = True
        return dict(empty(), reviews=[row])
    model.fn = change
    turn(app, '我以前住北京，现在住杭州')
    old_now = app.memory.repository.get(old.id)
    assert old_now.draft.valid_to is not None
    records = app.memory.retrieve('不是北京而是杭州了吗，2026年搬家').records
    assert any('以前住北京' in item.content for item in records)
    assert any('现在住杭州' in item.content for item in records)


def test_schema_two_candidate_ttl_and_tool_metadata_migration(tmp_path):
    path = tmp_path / 'memory.sqlite3'
    repo = SQLiteMemoryRepository(path)
    candidate = repo.ingest(MemorySource('保留不确定理解'), MemoryDraft('保留不确定理解', '保留不确定理解'))
    episode = repo.ingest(MemorySource('工具元信息', origin='tool_result'), MemoryDraft('工具元信息', '工具元信息',
        claim_type=ClaimType.OBSERVED_EVENT, kind=MemoryKind.EPISODIC, predicate='execution'), accepted=True)
    repo.close()
    with sqlite3.connect(path) as db:
        raw = json.loads(db.execute('SELECT draft FROM memory_revisions WHERE id=?', (candidate.id,)).fetchone()[0])
        raw['expires_at'] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        for key in ('destination', 'reason', 'uncertainty', 'review_after'):
            raw.pop(key)
        db.execute('UPDATE memory_revisions SET draft=?,default_expiry=1 WHERE id=?', (json.dumps(raw), candidate.id))
        for table in ('learning_summary_outbox', 'learning_role_outbox', 'learning_record', 'learning_link', 'learning_job'):
            db.execute(f'DROP TABLE {table}')
        db.execute('PRAGMA user_version=2')
    repo = SQLiteMemoryRepository(path)
    try:
        assert repo.get(candidate.id).status == MemoryStatus.CANDIDATE
        assert repo.get(candidate.id).draft.expires_at is None
        assert repo.get(episode.id).status == MemoryStatus.ARCHIVED
        assert repo._db.execute('PRAGMA user_version').fetchone()[0] == 4
    finally:
        repo.close()


def test_cross_database_role_receipt_recovers_after_apply_before_ack(app_model, monkeypatch):
    app, model = app_model
    def learn(data):
        return dict(empty(), habits=[dict(id=None, content='先观察再建议', reason='用户明确要求',
            uncertainty='仍以当前请求为先', sources=['user'], buffer_id=None, explicit=True)])
    model.fn = learn
    def fail_ack(*args, **kwargs):
        raise OSError('synthetic interruption after role commit')
    monkeypatch.setattr(app.learning.store, 'ack', fail_ack)
    result = turn(app, '以后先观察再建议')
    before = app.core.companion_runtime.interaction_snapshot()['habits']
    assert len(before) == 1
    calls = len(model.inputs)
    assert app.close(timeout=10)
    reopened = create_application(model.settings)
    try:
        wait_until(lambda: not reopened.learning.store.pending_effects())
        assert reopened.core.companion_runtime.interaction_snapshot()['habits'] == before
        assert len(model.inputs) == calls
        assert reopened.learning.store.job('turn:' + result.task_id)['status'] == 'completed'
    finally:
        assert reopened.close(timeout=10)


def test_restart_resumes_claimed_learning_without_replanning_or_tools(app_model):
    app, model = app_model
    result = turn(app, '一段有意义的真实交流')
    with app.learning.repository.batch():
        app.learning.repository._db.execute("DELETE FROM memory_learning_receipts WHERE token=?",
            (app.learning.store._organized_token(result.task_id),))
        app.learning.repository._db.execute("UPDATE learning_job SET status='running',attempts=1 WHERE id=?", ('turn:' + result.task_id,))
    planning_calls = len(model.planning)
    assert app.close(timeout=10)
    model.fn = lambda data: dict(empty(), memories=[draft('有意义的共同交流', '有意义的真实交流', destination='episode')])
    reopened = create_application(model.settings)
    try:
        wait_until(lambda: reopened.learning.store.job('turn:' + result.task_id)['status'] == 'completed')
        assert len(model.planning) == planning_calls
        assert len(reopened.memory.list_items()) == 1
        assert reopened.memory.list_items()[0].draft.kind == MemoryKind.EPISODIC
        assert reopened.learning.store.job('turn:' + result.task_id)['attempts'] == 2
    finally:
        assert reopened.close(timeout=10)


def test_summary_survives_more_than_recent_history_window(app_model):
    app, model = app_model
    model.fn = lambda data: dict(empty(), summary=dict(progress=['比较两个研究方案'], decisions=[], open_questions=['仍未决定']))
    first = turn(app, '我们正在比较两个研究方案，暂未决定')
    for index in range(20):
        result = turn(app, f'继续第{index}次讨论')
        assert not any('记忆已保存' in warning for warning in result.warnings)
    conversation = app.conversation_ensure_default()
    saved = app._conversation_repository.context_summary(conversation['id'], conversation['context_epoch'])
    assert saved['revision'] >= 20
    assert 0 < len(saved['refs']) <= 8
    with app.store.read_connection() as db:
        stored = {row[0] for row in db.execute(
            'SELECT object_id FROM conversation_summary_source WHERE conversation_id=?', (conversation['id'],))}
    assert first.task_id + ':user' in stored
    assert len(stored) >= 21
    assert '比较两个研究方案' in str(model.planning[-1])


def test_forget_during_evaluation_does_not_restore_buffer_or_habit(app_model):
    app, model = app_model
    original = app.memory.remember('需要遗忘的事情')
    entered, release = Event(), Event()
    def block(data):
        entered.set(); assert release.wait(8)
        return dict(empty(), memories=[draft('不应恢复的推测', '需要遗忘的事情', sources=[data['understandings'][0]['original_source_key']])])
    model.fn = block
    conversation = app.conversation_ensure_default()
    task = app.submit('讨论需要遗忘的事情', conversation_id=conversation['id'], expected_context_epoch=0)
    try:
        assert entered.wait(5)
        app.request_forget(original.id, original.revision, 'forget-mid-evaluation')
    finally:
        release.set()
    wait_until(lambda: not app.cleanup_status()['blocked'])
    assert app.memory.repository.get(original.id).status == MemoryStatus.DELETED
    assert not any(item.draft.content == '不应恢复的推测' for item in app.memory.list_items())
    assert app.learning.store.job('turn:' + task.id)['status'] == 'cancelled'


def test_correction_and_forget_invalidate_dependent_summary(app_model):
    app, model = app_model
    memory = app.memory.remember('用户住在北京')
    turn(app, '北京现在住得怎样')
    model.fn = lambda data: dict(empty(), summary=dict(progress=['讨论北京居住'], decisions=[], open_questions=['下一步安排']))
    turn(app, '继续聊北京')
    conversation = app.conversation_ensure_default()
    assert app._conversation_repository.context_summary(conversation['id'], 0) is not None
    app.memory_command('correct', memory_id=memory.id, expected_revision=memory.revision, content='用户住在杭州')
    assert app._conversation_repository.context_summary(conversation['id'], 0) is None


def test_local_review_of_legacy_restricted_source_keeps_derivation_restricted(app_model):
    app, model = app_model
    original = app.memory.remember('旧的受限记忆', use_policy=UsePolicy.LOCAL_ONLY)
    def derive(data):
        item = next(row for row in data['understandings'] if row['id'] == original.id)
        return dict(empty(), memories=[draft('从受限资料得到的推测', '旧的受限记忆', sources=[item['original_source_key']])])
    model.fn = derive
    turn(app, '聊旧的受限记忆')
    current = next(item for item in app.memory.list_items() if item.id != original.id)
    assert current.source.use_policy == UsePolicy.LOCAL_ONLY


def test_time_query_leaves_tool_ledger_without_automatic_life_episode(app_model):
    from deskpal.tools.models import ToolCall
    app, model = app_model
    original = model.chat
    count = [0]
    def chat(messages, *, tools=()):
        if messages[0].content.startswith('[PLANNING]'):
            count[0] += 1
            return LLMResponse('', (ToolCall('time', {}, 'time-call'),)) if count[0] % 2 else LLMResponse('已查看实际时间。')
        return original(messages, tools=tools)
    model.chat = chat
    first = turn(app, '现在几点')
    second = turn(app, '再看一下时间')
    assert len(app.journal.attempts(first.task_id)) == len(app.journal.attempts(second.task_id)) == 1
    assert app.memory.list_items() == ()


@pytest.mark.parametrize('fault', ['unknown_source', 'repeated_target', 'assistant_fact'])
def test_invalid_model_provenance_cannot_partially_save_or_grant_authority(app_model, fault):
    app, model = app_model
    original = app.memory.remember('真实来源')
    def invalid(data):
        good = draft('正常但不应部分保存', '新的交流')
        if fault == 'unknown_source':
            bad = draft('伪造来源', '新的交流', sources=['invented-call-id'])
            return dict(empty(), memories=[good, bad])
        if fault == 'repeated_target':
            row = review(data)
            return dict(empty(), memories=[good], reviews=[row, row])
        return dict(empty(), memories=[good, draft('把助手当成事实', data['catalog']['assistant']['content'], sources=['assistant'])])
    model.fn = invalid
    result = turn(app, '新的交流')
    assert app.learning.store.job('turn:' + result.task_id)['status'] == 'failed'
    assert [item.id for item in app.memory.list_items()] == [original.id]
    assert not app.learning.store.records()


def test_semantic_synonym_recall_is_used_across_conversations(app_model):
    from deskpal.memory.contracts import EmbeddingManifest
    app, model = app_model
    class Embedding:
        manifest = EmbeddingManifest('synthetic-synonym-map', 2, '1')
        def embed_query(self, text):
            return (1., 0.)
        def embed_documents(self, texts):
            return tuple((1., 0.) if '拿铁' in text else (0., 1.) for text in texts)
    app.memory._get_retriever().embedding = Embedding()
    item = app.memory.remember('早餐喜欢拿铁')
    other = app.conversation_create('另一段专项聊天', 'semantic-other')
    turn(app, '晨间饮品的习惯是什么', other)
    actual = '\n'.join(message.content for message in model.planning[-1])
    assert '早餐喜欢拿铁' in actual and item.id in actual
    assert app.memory.valid(((item.id, item.revision),))


def test_clear_conversation_preserves_habit_with_unavailable_original_refs(app_model):
    app, model = app_model
    model.fn = lambda data: dict(empty(), habits=[dict(id=None, content='以后先倾听', reason='用户明确提出长期要求',
        uncertainty='当次明确要求优先', sources=['user'], buffer_id=None, explicit=True)])
    turn(app, '以后先倾听')
    conversation = app.conversation_ensure_default()
    app.conversation_clear(conversation['id'], conversation['revision'], 'clear-habit-source')
    habits = app.core.companion_runtime.interaction_snapshot()['habits']
    assert len(habits) == 1 and habits[0]['content'] == '以后先倾听'
    assert all(not ref['available'] for ref in habits[0]['source']['refs'] if ref['kind'] == 'dialogue_turn')
    assert all(not ref['available'] for ref in app.learning.store.records()[0]['sources'] if ref['kind'] == 'dialogue_turn')


@pytest.mark.parametrize('decision', ['merge', 'update', 'archive'])
def test_internal_consolidation_really_merges_updates_or_archives_without_chat(app_model, decision):
    app, model = app_model
    first = app.memory.repository.ingest(MemorySource('我喜欢绿茶'), MemoryDraft('喜欢绿茶', '我喜欢绿茶'))
    target = app.memory.repository.ingest(MemorySource('喝茶时偏爱绿茶'), MemoryDraft('绿茶偏好', '喝茶时偏爱绿茶'))
    conversation = app.conversation_ensure_default()
    before = app.conversation_timeline(conversation['id'])['items']
    def consolidate(data):
        selected = next(row for row in data['understandings'] if row['id'] == first.id)
        row = dict(draft('喝茶时喜欢绿茶', '我喜欢绿茶', sources=[selected['original_source_key']]),
            id=first.id, revision=first.revision, decision=decision, target=target.id if decision == 'merge' else None,
            temporal=False)
        return dict(empty(), reviews=[row])
    model.fn = consolidate
    app.enqueue_learning_review('consolidate-' + decision)
    key = 'review:consolidate-' + decision
    wait_until(lambda: (app.learning.store.job(key) or {}).get('status') == 'completed')
    current = app.memory.repository.get(first.id)
    assert current.revision == 2
    assert current.status == (MemoryStatus.CANDIDATE if decision == 'update' else MemoryStatus.ARCHIVED)
    if decision == 'merge':
        merged = app.memory.repository.get(target.id)
        assert merged.revision == 2 and merged.draft.content == '喝茶时喜欢绿茶'
    assert app.conversation_timeline(conversation['id'])['items'] == before


def test_unused_input_candidate_does_not_become_a_habit_source_or_cause_its_deletion(app_model):
    app, model = app_model
    unrelated = app.memory.remember('无关的旧事实')
    for index in range(3):
        turn(app, f'之前第{index}次交流')
    model.fn = lambda data: dict(empty(), habits=[dict(id=None, content='先讲主要结论', reason='本轮用户明确提出',
        uncertainty='当前明确要求优先', sources=['user'], buffer_id=None, explicit=True)])
    result = turn(app, '以后先讲主要结论')
    before = app.core.companion_runtime.interaction_snapshot()['habits']
    assert len(before[0]['source']['refs']) == 1
    assert before[0]['source']['refs'][0]['id'] == result.task_id + ':user'
    assert not any(ref['kind'] == 'memory' and ref['id'] == unrelated.id
        for ref in app.learning.store.records()[0]['sources'])
    app.memory_command('forget', memory_id=unrelated.id, expected_revision=unrelated.revision)
    assert app.core.companion_runtime.interaction_snapshot()['habits'] == before


def test_role_growth_cannot_commit_an_unreadable_required_context(tmp_path):
    from deskpal.companion import CompanionRuntime
    runtime = CompanionRuntime(tmp_path / 'bounded-role.sqlite3')
    habit = dict(id=None, content='c' * 1000, reason='r' * 1000, uncertainty='',
                 explicit=True, host_refs=[], sources=['user'], buffer_id=None)
    try:
        runtime.apply_learning('first', expected_revision=1, source={'task_id': 'synthetic'},
            habits=[deepcopy(habit) for _ in range(8)], emotion=None, use_policy='cloud_allowed')
        before = runtime.interaction_snapshot()
        with pytest.raises(ValueError, match='过大'):
            runtime.apply_learning('overflow', expected_revision=before['revision'], source={'task_id': 'synthetic'},
                habits=[deepcopy(habit) for _ in range(8)], emotion=None, use_policy='cloud_allowed')
        assert runtime.interaction_snapshot() == before
        assert runtime.context('default', 0, 'next')['learned_habits']
        assert runtime._repository.connection.execute("SELECT 1 FROM role_receipt WHERE id='overflow'").fetchone() is None
    finally:
        runtime.close()

def test_summary_delivery_recovers_after_restart_without_relearning(app_model, monkeypatch):
    app, model = app_model
    conversation = app.conversation_ensure_default()
    model.fn = lambda data: dict(empty(), summary=dict(progress=['已收到更新'], decisions=[], open_questions=[]))
    def fail_delivery(*args, **kwargs):
        raise OSError('synthetic summary write failure')
    monkeypatch.setattr(app._conversation_repository, 'save_context_summary', fail_delivery)
    result = turn(app, '应在重启后保存的摘要', conversation)
    identity = 'turn:' + result.task_id
    assert app.learning.store.job(identity)['status'] == 'completed'
    assert any(row['job_id'] == identity for row in app.learning.store.pending_summaries())
    learned = len(model.inputs)
    assert app.close(timeout=10)
    reopened = create_application(model.settings)
    try:
        wait_until(lambda: reopened._conversation_repository.context_summary(
            conversation['id'], conversation['context_epoch']) is not None)
        saved = reopened._conversation_repository.context_summary(conversation['id'], conversation['context_epoch'])
        assert saved['content']['progress'] == ['已收到更新']
        assert saved['revision'] == 1
        assert len(model.inputs) == learned
    finally:
        assert reopened.close(timeout=10)


def test_rolling_summary_crosses_old_provenance_limit(app_model):
    app, model = app_model
    conversation = app.conversation_ensure_default()
    for index in range(386):
        model.fn = lambda data: dict(empty(), summary=dict(progress=[str(index)], decisions=[], open_questions=[]))
        turn(app, f'持续聊天第 {index} 轮', conversation)
    saved = app._conversation_repository.context_summary(conversation['id'], conversation['context_epoch'])
    assert saved['content']['progress'] == ['385']
    assert saved['revision'] == 386
    with app.store.read_connection() as db:
        count = db.execute('SELECT COUNT(*) FROM conversation_summary_source WHERE conversation_id=?',
                           (conversation['id'],)).fetchone()[0]
    assert count > 384


def test_review_pool_uses_deadlines_not_coverage_or_permanent_attempt_limit(app_model):
    app, _ = app_model
    now = datetime.now(timezone.utc)
    def seed(label, *, accepted=False, deadline=None):
        return app.memory.repository.ingest(MemorySource(label),
            MemoryDraft(label, label, review_after=deadline), accepted=accepted)
    missing = seed('无时间的旧候选')
    overdue = seed('已确认但需要复查', accepted=True, deadline=now - timedelta(hours=1))
    stable = seed('稳定事实无需复查', accepted=True)
    future = seed('尚未到期', deadline=now + timedelta(days=1))
    app.proactive._meta('review_coverage', json.dumps({missing.id: dict(revision=1, at=now.timestamp())}))
    app.proactive._meta('review_attempts', json.dumps({missing.id: dict(revision=1, at=now.timestamp(), count=99)}))
    ids = {row['id'] for row in app.learning.review_candidates(now)}
    assert ids == {missing.id, overdue.id}
    assert stable.id not in ids and future.id not in ids


def test_ordinary_learning_rotates_all_overdue_records_even_without_topic_match(app_model):
    app, model = app_model
    items = [app.memory.repository.ingest(MemorySource(f'旧资料{index}'), MemoryDraft(f'旧理解{index}', f'旧资料{index}'))
        for index in range(21)]
    model.fn = lambda data: empty()  # 不处理并不等于归档或复查成功。
    seen = set()
    for index in range(6):
        turn(app, f'完全不同的新话题{index}')
        rows = model.inputs[-1]['understandings']
        assert len(rows) <= 8
        seen.update(row['id'] for row in rows)
    assert seen == {item.id for item in items}
    assert all(app.memory.repository.get(item.id).draft.review_after is None for item in items)
    assert len(app.learning.review_candidates(datetime.now(timezone.utc))) == 16


def test_retained_candidate_reappears_when_due_then_can_archive(app_model):
    app, model = app_model
    now = datetime.now(timezone.utc)
    app.proactive.clock = lambda: now
    item = app.memory.repository.ingest(MemorySource('未决计划'), MemoryDraft('可能有计划', '未决计划'))
    model.fn = lambda data: dict(reviews=[dict(id=item.id, decision='retain', reason='暂时保留',
        review_after=(now + timedelta(hours=2)).isoformat())])
    app.learning.run_review('schedule-retain')
    assert app.learning.store.job('review:schedule-retain')['status'] == 'completed'
    assert not app.learning.review_candidates(now + timedelta(hours=1))
    now += timedelta(hours=2, seconds=1)
    assert [row['id'] for row in app.learning.review_candidates(now)] == [item.id]
    model.fn = lambda data: dict(reviews=[dict(id=item.id, decision='archive', reason='已无后续价值')])
    app.learning.run_review('schedule-archive')
    archived = app.memory.repository.get(item.id)
    assert archived.status == MemoryStatus.ARCHIVED and archived.draft.content == '可能有计划'
    assert archived.draft.review_after is None and not app.learning.review_candidates(now)


def test_failed_review_does_not_complete_task_or_prevent_later_archive(app_model):
    app, model = app_model
    item = app.memory.repository.ingest(MemorySource('旧依据'), MemoryDraft('待核对', '旧依据'))
    model.fn = lambda data: dict(reviews=[dict(id=item.id, decision='retain', reason='故意漏掉时间')])
    app.learning.run_review('missing-date')
    job = app.learning.store.job('review:missing-date')
    assert job['status'] == 'failed' and job['attempts'] == 3
    assert app.tasks.get(job['task_id']).status == TaskStatus.FAILED
    assert app.memory.repository.get(item.id).revision == 1
    assert [row['id'] for row in app.learning.review_candidates(app.proactive.clock())] == [item.id]
    assert len(model.inputs) == 3
    model.fn = lambda data: dict(reviews=[dict(id=item.id, decision='archive', reason='随后归档')])
    app.learning.run_review('retry-after-failure')
    assert app.learning.store.job('review:retry-after-failure')['status'] == 'completed'
    assert app.memory.repository.get(item.id).status == MemoryStatus.ARCHIVED


def test_deadline_that_passed_during_model_wait_cannot_be_committed(app_model):
    app, model = app_model
    now = datetime.now(timezone.utc)
    app.proactive.clock = lambda: now
    item = app.memory.repository.ingest(MemorySource('待复查依据'), MemoryDraft('旧理解', '待复查依据'))
    def feedback(data):
        nonlocal now
        deadline = datetime.fromisoformat(data['now']) + timedelta(minutes=1)
        now += timedelta(hours=1)
        return dict(reviews=[dict(id=item.id, decision='retain', reason='已错过复查时间', review_after=deadline.isoformat())])
    model.fn = feedback
    app.learning.run_review('late-date')
    assert app.learning.store.job('review:late-date')['status'] == 'failed'
    assert app.memory.repository.get(item.id).revision == 1
    assert app.memory.repository.get(item.id).draft.review_after is None
