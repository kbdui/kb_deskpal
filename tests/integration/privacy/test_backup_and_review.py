"""Regression evidence for reviewed deletion scope and create-only backups."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3

import pytest

from deskpal.config import load_config
from deskpal.memory.contracts import MemoryDraft, MemorySource
from deskpal.planner.models import Plan
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolCall, ToolSpec
from deskpal.workflow import ExecutionJournal, PersistentTaskManager, QueueFull, WorkflowBlocked, WorkflowStore


def test_forget_erases_only_actual_shared_source_closure_not_other_invalid_history(tmp_path, monkeypatch):
    from deskpal.application.factory import create_application

    config = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
        "DESKPAL_DATA_DIR": str(tmp_path / "data"), "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace")})
    with create_application(config) as app:
        shared = MemorySource("synthetic shared deletion source", id="shared-deletion-source")
        repository = app.memory.repository
        first = repository.ingest(shared, MemoryDraft("synthetic first", "synthetic"), accepted=True)
        sibling = repository.ingest(shared, MemoryDraft("synthetic sibling", "synthetic"), accepted=True)
        corrected = app.memory_command("remember", content="synthetic unrelated original")
        app.memory_command("correct", memory_id=corrected.id, expected_revision=corrected.revision,
                           content="synthetic unrelated corrected")
        now = datetime.now(timezone.utc)
        expired = repository.ingest(MemorySource("synthetic unrelated expiry", created_at=now),
            MemoryDraft("synthetic unrelated expiry", "synthetic", expires_at=now + timedelta(days=1)), accepted=True)
        monkeypatch.setattr(repository, "_clock", lambda: now + timedelta(days=2))
        monkeypatch.setattr(app.memory, "_clock", lambda: now + timedelta(days=2))
        records = (first, sibling, corrected, expired)
        tasks = {}
        for item in records:
            task = app.tasks.create("synthetic derived " + item.id)
            app.journal.set_dependencies(task.id, ((item.id, item.revision),))
            tasks[item.id] = task
        assert not app.memory.valid(((corrected.id, corrected.revision),))
        assert not app.memory.valid(((expired.id, expired.revision),))
        receipt = app.memory_command("forget", memory_id=first.id, expected_revision=first.revision)
        assert set(receipt.affected_task_ids) >= {tasks[first.id].id, tasks[sibling.id].id}
        for item in (first, sibling):
            assert app.tasks.get(tasks[item.id].id).request == "[forgotten]"
        for item in (corrected, expired):
            assert app.tasks.get(tasks[item.id].id).request == tasks[item.id].request
            assert tasks[item.id].id not in receipt.affected_task_ids
        assert app.journal.pending_deletions() == ()


@pytest.fixture
def source_store(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    task = PersistentTaskManager(store).submit("synthetic WAL-backed request", command_id="source")
    yield store, task
    store.close()


def test_backup_new_destination_contains_live_source_and_leaves_no_temporary_artifacts(source_store, tmp_path):
    store, task = source_store
    target = tmp_path / "snapshots" / "new.sqlite3"
    store.backup(target)
    backup = WorkflowStore(target)
    try:
        assert PersistentTaskManager(backup).get(task.id).request == task.request
    finally:
        backup.close()
    assert PersistentTaskManager(store).get(task.id).request == task.request
    assert list(target.parent.glob(".workflow-backup-*")) == []


@pytest.mark.parametrize("kind", ["arbitrary", "database", "directory", "hardlink"])
def test_backup_refuses_every_existing_destination_without_changing_it(source_store, tmp_path, kind):
    store, task = source_store
    target = tmp_path / "memory.sqlite3"
    if kind == "directory":
        target.mkdir()
        marker = target / "retained.txt"
        marker.write_bytes(b"independent content")
    elif kind == "database":
        with sqlite3.connect(target) as conn:
            conn.execute("CREATE TABLE independent(value TEXT)")
            conn.execute("INSERT INTO independent VALUES('retained')")
        before = target.read_bytes()
    elif kind == "hardlink":
        independent = tmp_path / "independent.bin"
        independent.write_bytes(b"independent content")
        os.link(independent, target)
        before = target.read_bytes()
    else:
        target.write_bytes(b"arbitrary non-SQLite bytes")
        before = target.read_bytes()
    with pytest.raises(FileExistsError):
        store.backup(target)
    if kind == "directory":
        assert marker.read_bytes() == b"independent content"
    else:
        assert target.read_bytes() == before
    assert PersistentTaskManager(store).get(task.id) == task
    assert list(tmp_path.glob(".workflow-backup-*")) == []


def test_backup_refuses_lexically_existing_path_even_when_exists_check_is_false(source_store, tmp_path, monkeypatch):
    # A dangling symlink is lexically present despite Path.exists() being false.
    # This tests that distinction without requiring Windows symlink privileges.
    store, _ = source_store
    target = tmp_path / "existing.bin"
    target.write_bytes(b"retained")
    monkeypatch.setattr(Path, "exists", lambda self: False)
    with pytest.raises(FileExistsError):
        store.backup(target)
    assert target.read_bytes() == b"retained"


def test_backup_refuses_source_database_as_destination(source_store):
    store, task = source_store
    with pytest.raises(ValueError, match="differ"):
        store.backup(store.path)
    assert PersistentTaskManager(store).get(task.id) == task


def test_backup_inside_write_transaction_fails_without_hanging_or_partial_target(source_store, tmp_path):
    store, task = source_store
    target = tmp_path / "inside-transaction.sqlite3"
    with store.transaction():
        with pytest.raises(RuntimeError, match="transaction"):
            store.backup(target)
    assert not target.exists()
    assert PersistentTaskManager(store).get(task.id) == task


@pytest.mark.parametrize("failure", ["connect", "backup", "publish"])
def test_failed_backup_removes_incomplete_files_and_keeps_source(source_store, tmp_path, monkeypatch, failure):
    store, task = source_store
    target = tmp_path / "new.sqlite3"
    with monkeypatch.context() as patch:
        if failure == "connect":
            def fail_connect(*args, **kwargs):
                raise OSError("synthetic target open failure")
            patch.setattr("deskpal.workflow.store.sqlite3.connect", fail_connect)
        elif failure == "backup":
            real_source = store._conn
            class Interrupted:
                def backup(self, output):
                    output.execute("CREATE TABLE incomplete(value TEXT)")
                    output.commit()
                    raise OSError("synthetic backup interruption")
                def __getattr__(self, name):
                    return getattr(real_source, name)
            patch.setattr(store, "_conn", Interrupted())
        else:
            def fail_publish(*args, **kwargs):
                raise OSError("synthetic target publish failure")
            patch.setattr("deskpal.workflow.store.os.link", fail_publish)
        with pytest.raises(OSError):
            store.backup(target)
    assert not target.exists()
    assert list(tmp_path.glob(".workflow-backup-*")) == []
    assert PersistentTaskManager(store).get(task.id) == task


def test_backup_publication_race_cannot_overwrite_concurrent_creator(source_store, tmp_path, monkeypatch):
    store, task = source_store
    target = tmp_path / "new.sqlite3"
    original = os.link
    def concurrent_create(source, destination):
        Path(destination).write_bytes(b"concurrent independent content")
        return original(source, destination)
    monkeypatch.setattr("deskpal.workflow.store.os.link", concurrent_create)
    with pytest.raises(FileExistsError):
        store.backup(target)
    assert target.read_bytes() == b"concurrent independent content"
    assert list(tmp_path.glob(".workflow-backup-*")) == []
    assert PersistentTaskManager(store).get(task.id) == task


def test_resumed_plan_remains_capacity_counted_until_confirmation_or_cancel(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3", queue_capacity=1)
    tasks, journal = PersistentTaskManager(store), ExecutionJournal(store)
    task = tasks.submit("synthetic planned request", command_id="planned")
    tasks.claim_next()
    tasks.update_status(task.id, TaskStatus.PLANNING)
    journal.save_plan(task.id, Plan("review", (ToolCall("synthetic"),)),
                      (ToolSpec("synthetic", "synthetic", {}),), "workspace")
    tasks.update_status(task.id, TaskStatus.WAITING)
    tasks.block_for_review(task.id, "synthetic interruption")
    assert journal.resume_unstarted(task.id).status == TaskStatus.WAITING
    assert tasks.claim_next() is None
    with store.transaction() as db:
        assert db.execute("SELECT status FROM submission WHERE task_id=?", (task.id,)).fetchone()[0] == "Blocked"
    with pytest.raises(QueueFull):
        tasks.submit("another request", command_id="overflow")
    tasks.requeue(task.id)
    assert tasks.claim_next().id == task.id
    tasks.request_cancel(task.id)
    assert tasks.submit("new request", command_id="new").status == TaskStatus.CREATED
    store.close()


def test_resume_without_persistent_submission_is_rejected(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    tasks, journal = PersistentTaskManager(store), ExecutionJournal(store)
    task = tasks.create("synthetic low-level task")
    tasks.update_status(task.id, TaskStatus.NEEDS_REVIEW)
    with pytest.raises(WorkflowBlocked, match="submission"):
        journal.resume_unstarted(task.id)
    assert tasks.get(task.id).status == TaskStatus.NEEDS_REVIEW
    store.close()


def test_resume_cannot_expand_reduced_queue_capacity_or_old_finished_receipts(tmp_path):
    path = tmp_path / "workflow.sqlite3"
    initial = WorkflowStore(path, queue_capacity=2)
    tasks = PersistentTaskManager(initial)
    first = tasks.submit("synthetic first", command_id="first")
    tasks.update_status(first.id, TaskStatus.NEEDS_REVIEW)
    tasks.finish_submission(first.id)
    second = tasks.submit("synthetic second", command_id="second")
    initial.close()
    store = WorkflowStore(path, queue_capacity=1)
    tasks = PersistentTaskManager(store)
    with pytest.raises(QueueFull):
        tasks.resume(first.id)
    assert tasks.get(first.id).status == TaskStatus.NEEDS_REVIEW
    assert tasks.get(second.id).status == TaskStatus.CREATED
    store.close()
