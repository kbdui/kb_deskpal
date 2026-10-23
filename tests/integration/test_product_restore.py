"""Prepared production-entry restore cases. No real config, keys, models or data.

These cases intentionally remain unexecuted during the development-only round.
"""

from contextlib import ExitStack
import sqlite3

import pytest

from deskpal.application.factory import create_application
from deskpal.companion import CompanionRuntime
from deskpal.config import AppConfig, SandboxConfig, StorageConfig
from deskpal.maintenance.archives import create_backup, restore_backup, MARKER
from deskpal.maintenance.errors import MaintenanceError
from deskpal.memory.repository import SQLiteMemoryRepository
from deskpal.planner.models import Plan
from deskpal.scheduling import SchedulingService
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import PermissionLevel, ToolCall, ToolSpec
from deskpal.workflow import WorkflowStore, PersistentTaskManager, ExecutionJournal


@pytest.fixture
def populated(tmp_path):
    directory = tmp_path / "old"
    directory.mkdir()
    with ExitStack() as owned:
        workflow = WorkflowStore(directory / "workflow.sqlite3")
        owned.callback(workflow.close)
        for store in (SQLiteMemoryRepository(directory / "memory.sqlite3"),
                      SchedulingService(directory / "scheduling.sqlite3"),
                      CompanionRuntime(directory / "companion.sqlite3")):
            owned.callback(store.close)
        tasks, journal = PersistentTaskManager(workflow), ExecutionJournal(workflow)
        plan = Plan("synthetic retained reply", (ToolCall("echo", {"text": "synthetic retained argument"}),))
        spec = ToolSpec("echo", "synthetic tool", {"type": "object"}, PermissionLevel(0))
        waiting = tasks.submit("synthetic waiting request", command_id="waiting")
        tasks.update_status(waiting.id, TaskStatus.PLANNING)
        snapshot = journal.save_plan(waiting.id, plan, (spec,), str(tmp_path / "workspace"))
        tasks.update_status(waiting.id, TaskStatus.WAITING)
        uncertain = tasks.submit("synthetic started request", command_id="started")
        tasks.update_status(uncertain.id, TaskStatus.PLANNING)
        journal.save_plan(uncertain.id, plan, (spec,), str(tmp_path / "workspace"))
        tasks.update_status(uncertain.id, TaskStatus.RUNNING)
        journal.start_step(uncertain.id, 0)
        with workflow.transaction() as connection:
            connection.execute("INSERT INTO memory_block VALUES ('synthetic-preserved-privacy-block')")
        with workflow.read_connection() as connection:
            outbox = [tuple(row) for row in connection.execute("SELECT id,event FROM audit_outbox ORDER BY sequence")]
    path = tmp_path / "snapshot.zip"
    create_backup(directory, path, acknowledge_sensitive_data=True)
    return directory, path, waiting.id, uncertain.id, snapshot, outbox


class NeverPlanner:
    def __init__(self):
        self.calls = []

    def plan(self, request, *, memories=(), tools=()):
        self.calls.append(request)
        raise AssertionError("restore must not call a planner")


def test_restore_preserves_plan_hash_privacy_identity_and_startup_does_not_replay(populated, tmp_path, monkeypatch):
    original, archive, waiting_id, uncertain_id, snapshot, outbox = populated
    target = tmp_path / "restored"
    restore_backup(archive, target, acknowledge_data_rollback=True, current_data_dir=original)
    # Inspect original bytes before startup intentionally performs its normal
    # recovery and audit delivery; no source identity or plan is rewritten here.
    for database, query in (
        ("workflow.sqlite3", "SELECT hash,canonical_json FROM immutableplan ORDER BY id"),
        ("workflow.sqlite3", "SELECT memory_id FROM memory_block ORDER BY memory_id"),
        ("scheduling.sqlite3", "SELECT key,value FROM scheduling_identity ORDER BY key"),
    ):
        with sqlite3.connect(original / database) as old, sqlite3.connect(target / database) as new:
            assert old.execute(query).fetchall() == new.execute(query).fetchall()
    with sqlite3.connect(target / "workflow.sqlite3") as connection:
        assert connection.execute("SELECT id,event FROM audit_outbox ORDER BY sequence").fetchall() == outbox
    planner = NeverPlanner()
    config = AppConfig(storage=StorageConfig(target), sandbox=SandboxConfig(tmp_path / "workspace"))
    from deskpal.tools.manager import BuiltinToolManager
    def forbidden(*args, **kwargs):
        raise AssertionError("restored startup must not execute tools")
    monkeypatch.setattr(BuiltinToolManager, "execute", forbidden)
    with create_application(config, planner=planner) as application:
        assert application.tasks.get(waiting_id).status == TaskStatus.NEEDS_REVIEW
        assert application.tasks.get(uncertain_id).status == TaskStatus.UNCERTAIN
        restored = application.journal.get_plan(waiting_id)
        assert restored.hash == snapshot.hash
        assert restored.canonical_json == snapshot.canonical_json
        assert application.journal.attempts(waiting_id) == ()
        assert len(application.journal.attempts(uncertain_id)) == 1
        assert planner.calls == []


def test_factory_barrier_precedes_all_store_construction_and_releases_lock(tmp_path, monkeypatch):
    directory = tmp_path / "interrupted"
    directory.mkdir()
    (directory / MARKER).write_text("malformed synthetic marker", encoding="ascii")
    import deskpal.application.factory as factory
    import deskpal.application.audit as audit
    import deskpal.application.service as application_service
    import deskpal.goals as goals
    import deskpal.proactive as proactive
    import deskpal.workflow as workflow
    from deskpal.application.instance import InstanceLock
    calls = []

    def forbidden_constructor(name):
        def forbidden(*args, **kwargs):
            calls.append(name)
            raise AssertionError("no resource constructor may run before restore barrier")
        return forbidden

    # create_application imports workflow adapters locally; patch their actual
    # source module as well as the factory's already-bound constructor entries.
    for module, names in (
        (factory, ("CompanionRuntime", "SQLiteMemoryRepository", "SchedulingService", "_create_core", "JsonlExecutionLog")),
        (workflow, ("WorkflowStore", "PersistentTaskManager", "ExecutionJournal")),
        (goals, ("GoalManager",)),
        (proactive, ("ProposalManager", "LocalRuleEngine")),
        (audit, ("WorkflowExecutionLog",)),
        (application_service, ("ApplicationService",)),
    ):
        for name in names:
            monkeypatch.setattr(module, name, forbidden_constructor(name))
    config = AppConfig(storage=StorageConfig(directory), sandbox=SandboxConfig(tmp_path / "workspace"))
    with pytest.raises(MaintenanceError) as caught:
        factory.create_application(config, planner=NeverPlanner())
    assert caught.value.code == "restore_pending"
    assert calls == []
    assert not any((directory / name).exists() for name in ("workflow.sqlite3", "memory.sqlite3", "scheduling.sqlite3", "companion.sqlite3"))
    with InstanceLock(directory):
        pass


def test_interrupted_restore_cannot_boot_until_explicit_resume(populated, tmp_path):
    original, archive, waiting_id, uncertain_id, snapshot, outbox = populated
    target = tmp_path / "new"
    def stop(phase):
        if phase == "published:memory.sqlite3":
            raise RuntimeError("synthetic process interruption")
    with pytest.raises(RuntimeError):
        restore_backup(archive, target, acknowledge_data_rollback=True, fault_hook=stop)
    config = AppConfig(storage=StorageConfig(target), sandbox=SandboxConfig(tmp_path / "workspace"))
    with pytest.raises(MaintenanceError) as caught:
        create_application(config, planner=NeverPlanner())
    assert caught.value.code == "restore_pending"
    restore_backup(archive, target, acknowledge_data_rollback=True, resume=True, current_data_dir=original)
    with create_application(config, planner=NeverPlanner()) as application:
        assert application.tasks.get(waiting_id).status == TaskStatus.NEEDS_REVIEW
        assert application.tasks.get(uncertain_id).status == TaskStatus.UNCERTAIN
