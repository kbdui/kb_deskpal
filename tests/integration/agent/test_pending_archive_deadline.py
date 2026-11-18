"""真实应用与合成模型：期限贯穿学习、复查与归档，不动聊天和人格。"""
from datetime import datetime, timedelta, timezone

from deskpal.memory.contracts import MemoryStatus
from tests.integration.agent.test_feedback_learning import app_model, draft, turn, wait_until


def test_learning_persists_model_deadline_and_adoption_clears_it(app_model):
    app, model = app_model
    limit = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    model.fn = lambda data: dict(memories=[draft('用户正在学画画', '我正在学画画') | {'archive_after': limit}])
    turn(app, '我正在学画画')
    item = app.memory.list_items()[0]
    assert item.draft.archive_after == datetime.fromisoformat(limit)
    def adopt(data):
        assert data['understandings'][0]['archive_after'] == item.draft.archive_after.isoformat()
        return dict(reviews=[dict(id=item.id, decision='adopt', reason='按本人的自述保存')])
    model.fn = adopt
    app.proactive.freeze_review('adopt-with-deadline', [dict(kind='memory', id=item.id, revision=item.revision)])
    app.learning.run_review('adopt-with-deadline')
    adopted = app.learning.repository.get(item.id)
    assert adopted.status == MemoryStatus.ACCEPTED and adopted.draft.archive_after is None


def test_archiving_preserves_chat_sources_and_role_state(app_model):
    app, model = app_model
    now = datetime.now(timezone.utc)
    limit = now + timedelta(days=3)
    model.fn = lambda data: dict(memories=[draft('用户正在学画画', '我正在学画画') | {'archive_after': limit.isoformat()}])
    result = turn(app, '我正在学画画')
    repo = app.learning.repository
    item = repo.list_items()[0]
    with app.store.read_connection() as db:
        before = [tuple(row) for row in db.execute('SELECT * FROM conversation_item ORDER BY id')]
    # 直接比较角色库完整快照，不将“没有更新”误当成清空习惯。
    role_before = app.core.companion_runtime.interaction_snapshot()
    calls = len(model.inputs)
    repo._clock = lambda: limit
    assert repo.archive_due_candidates() == 1
    archived = repo.get(item.id)
    assert archived.status == MemoryStatus.ARCHIVED and archived.source == item.source
    assert archived.draft.content == item.draft.content
    with app.store.read_connection() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM conversation_item ORDER BY id')] == before
    assert app.core.companion_runtime.interaction_snapshot() == role_before
    assert len(model.inputs) == calls


def test_missing_archive_time_is_corrected_without_committing_partial_memory(app_model):
    app, model = app_model
    def feedback(data):
        row = draft('用户正在学画画', '我正在学画画')
        if len(model.inputs) == 1:
            row.pop('archive_after')
        return dict(memories=[row])
    model.fn = feedback
    result = turn(app, '我正在学画画')
    assert len(model.inputs) == 2 and len(app.memory.list_items()) == 1
    assert app.learning.store.job('turn:' + result.task_id)['status'] == 'completed'
    diagnostics = [row for row in app._humanization.call_diagnostics()
        if row['task_id'] == result.task_id and row['purpose'] == 'memory_evaluation']
    failed = next(row for row in diagnostics if row['slot'] == 1)
    assert failed['metadata']['processing_field'] == 'memories[0].archive_after'
    assert failed['metadata']['processing_rule'] == 'archive_schedule'


def test_background_archiving_runs_even_with_proactive_behaviour_disabled(app_model):
    app, model = app_model
    assert app.config.proactive.enabled is False
    limit = datetime.now(timezone.utc) + timedelta(days=3)
    model.fn = lambda data: dict(memories=[draft('用户正在学画画', '我正在学画画') | {'archive_after': limit.isoformat()}])
    turn(app, '我正在学画画')
    repo = app.learning.repository
    item = repo.list_items()[0]
    calls = len(model.inputs)
    repo._clock = lambda: limit
    def archived_without_triggering_repository_reads():
        with repo._lock:
            return repo._db.execute('SELECT r.status FROM memory_heads h JOIN memory_revisions r '
                'ON r.id=h.id AND r.revision=h.revision WHERE h.id=?', (item.id,)).fetchone()[0] == 'archived'
    wait_until(archived_without_triggering_repository_reads)
    assert len(model.inputs) == calls
