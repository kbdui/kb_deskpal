"""清理与内部复查交错：唯一工作线程存活、恢复与真实异常边界。"""
from threading import Event
from time import monotonic, sleep

import pytest

from deskpal.application.factory import create_application
from deskpal.config import load_config
from deskpal.memory.contracts import MemoryStatus
from deskpal.tasks.models import TaskStatus
from deskpal.workflow.models import TaskCancellationRequested, WorkflowBlocked


def config(tmp_path, *, proactive=False):
    return load_config(environ={'DESKPAL_LLM_PROVIDER': 'offline',
        'DESKPAL_DATA_DIR': str(tmp_path / 'data'), 'DESKPAL_WORKSPACE_DIR': str(tmp_path / 'workspace'),
        'DESKPAL_PROACTIVE_ENABLED': 'true' if proactive else 'false'})


def eventually(check, timeout=5):
    until = monotonic() + timeout
    while monotonic() < until:
        if check():
            return
        sleep(.01)
    assert check()


@pytest.mark.parametrize('proactive', [False, True])
def test_queued_review_defers_during_failed_forget_and_same_worker_recovers(tmp_path, monkeypatch, proactive):
    app = create_application(config(tmp_path, proactive=proactive))
    entered, release, reached = Event(), Event(), Event()
    try:
        def gate():
            entered.set()
            assert release.wait(8)
        app._dispatcher.enqueue_internal('test-gate', gate)
        assert entered.wait(5)
        item = app.memory.remember('合成目标记忆')
        other = app.memory.remember('必须保留的合成记忆')
        identity = 'queued-before-forget'
        app.proactive.freeze_review(identity, [dict(kind='memory', id=item.id, revision=item.revision)])
        app.proactive._enqueue_review(identity)
        app._dispatcher.enqueue_internal('test-after-review', reached.set)
        original = app.core.memory_command
        def fail(action, **kwargs):
            if action == 'forget':
                raise OSError('synthetic storage failure')
            return original(action, **kwargs)
        monkeypatch.setattr(app.core, 'memory_command', fail)
        receipt = app.request_forget(item.id, item.revision, 'forget-with-queued-review')
        assert receipt['status'] == 'AcceptedPending'
        release.set()
        assert reached.wait(5), app._dispatcher.diagnostics()
        assert app.learning.store.job('review:' + identity) is None
        assert app.journal.pending_deletions()
        assert app._humanization.privacy()['blocked']
        assert app._dispatcher.diagnostics()['worker_alive']
        monkeypatch.setattr(app.core, 'memory_command', original)
        def retry():
            try:
                return app.retry_cleanup()
            except RuntimeError:
                return False  # 仅允许短时维护竞争，不允许线程失败后永久 busy。
        eventually(retry)
        assert app.memory.repository.get(item.id).status == MemoryStatus.DELETED
        assert app.memory.repository.get(other.id).status != MemoryStatus.DELETED
        assert not app._humanization.privacy()['blocked']
        assert app.request_forget(item.id, item.revision, 'forget-with-queued-review')['status'] == 'Completed'
        task = app.submit('echo 清理后继续执行')
        assert app.wait(task.id, 8).status == TaskStatus.COMPLETED
        assert not app._dispatcher.diagnostics()['failed']
    finally:
        release.set()
        assert app.close(timeout=10)


@pytest.mark.parametrize('blocked', [WorkflowBlocked, TaskCancellationRequested])
def test_expected_internal_block_does_not_stop_dispatcher(tmp_path, blocked):
    with create_application(config(tmp_path)) as app:
        reached = Event()
        def interrupt():
            raise blocked('synthetic expected interruption')
        app._dispatcher.enqueue_internal('expected-interruption', interrupt)
        app._dispatcher.enqueue_internal('after-interruption', reached.set)
        assert reached.wait(5), app._dispatcher.diagnostics()
        assert app._dispatcher.diagnostics()['worker_alive']
        assert not app._dispatcher.diagnostics()['failed']
        with app.store.read_connection() as db:
            assert db.execute("SELECT COUNT(*) FROM audit_outbox WHERE event='internal_work_blocked'").fetchone()[0] == 1
        task = app.submit('echo 内部工作阻断后继续')
        assert app.wait(task.id, 8).status == TaskStatus.COMPLETED


def test_unexpected_internal_storage_failure_still_fails_visibly(tmp_path):
    with create_application(config(tmp_path)) as app:
        def broken():
            raise OSError('synthetic unexpected storage failure')
        app._dispatcher.enqueue_internal('broken-internal-work', broken)
        eventually(lambda: app._dispatcher.diagnostics()['failed'])
        state = app._dispatcher.diagnostics()
        assert state['error_stage'] == 'internal_work' and state['error_type'] == 'OSError'


def test_review_creation_race_does_not_claim_learning_or_consume_model_budget(tmp_path, monkeypatch):
    with create_application(config(tmp_path)) as app:
        item = app.memory.remember('合成复查依据')
        identity = 'review-creation-race'
        app.proactive.freeze_review(identity, [dict(kind='memory', id=item.id, revision=item.revision)])
        original = app.tasks.create
        def blocked(*args, **kwargs):
            raise WorkflowBlocked('cleanup started after readiness check')
        monkeypatch.setattr(app.tasks, 'create', blocked)
        app.learning.run_review(identity)
        assert app.learning.store.job('review:' + identity) is None
        assert not app._humanization.call_diagnostics()
        assert app.proactive._meta('review_batch:' + identity)
        monkeypatch.setattr(app.tasks, 'create', original)
        app.learning.run_review(identity)
        assert app.learning.store.job('review:' + identity)['status'] == 'completed'


def test_unexpected_review_creation_error_is_not_silently_ignored(tmp_path, monkeypatch):
    with create_application(config(tmp_path)) as app:
        identity = 'review-storage-failure'
        app.proactive.freeze_review(identity, [])
        def broken(*args, **kwargs):
            raise OSError('synthetic task storage failure')
        monkeypatch.setattr(app.tasks, 'create', broken)
        with pytest.raises(OSError):
            app.learning.run_review(identity)
        assert app.learning.store.job('review:' + identity) is None
