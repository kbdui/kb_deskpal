"""D1 behavioral cases prepared for the separately authorized test phase.

All data is synthetic. No GUI, worker thread or model invocation is required.
"""

from pathlib import Path
import sqlite3
from typing import cast

import pytest

from deskpal.agent.core import AgentCore
from deskpal.application.dispatcher import SubmissionDispatcher
from deskpal.conversation.repository import ConversationRepository
from deskpal.conversation.runtime import ConversationConflict, ConversationRuntime
from deskpal.tasks.models import TaskStatus
from deskpal.workflow.humanization import HumanizationStore
from deskpal.workflow.journal import ExecutionJournal
from deskpal.workflow.migrations import SCHEMA_2
from deskpal.workflow.models import WorkflowBlocked
from deskpal.workflow.store import WorkflowStore, _SCHEMA
from deskpal.workflow.tasks import PersistentTaskManager


def bound(store, tasks, repository, conversation, command, request="synthetic request"):
    with store.transaction():
        task = tasks.submit(request, command_id=command)
        repository.bind(task.id, conversation["id"], conversation["context_epoch"], command_id=command)
    return task


def complete(tasks, task_id, response):
    tasks.update_status(task_id, TaskStatus.PLANNING)
    tasks.update_status(task_id, TaskStatus.RUNNING)
    tasks.update_status(task_id, TaskStatus.COMPLETED, result={"response": response})


def test_task_location_reads_one_card_beyond_recent_page_and_rejects_redaction(tmp_path: Path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    try:
        repository, tasks = ConversationRepository(store), PersistentTaskManager(store)
        conversation = repository.ensure_default()
        first = bound(store, tasks, repository, conversation, "old-task", "private original turn")
        complete(tasks, first.id, "private original response")
        for index in range(20):
            task = bound(store, tasks, repository, conversation, "later-" + str(index))
            complete(tasks, task.id, "later response")
        assert all(item["task_id"] != first.id for item in repository.timeline(conversation["id"])["items"])
        location = repository.task_location(conversation["id"], first.id)
        assert location["before"] == 4
        assert "private" not in str(location)
        page = repository.timeline(conversation["id"], before=location["before"])
        assert [item["kind"] for item in page["items"]] == ["user_message", "task_card", "assistant_message"]
        assert all(item["task_id"] == first.id for item in page["items"])
        other = repository.create("other conversation", "other-conversation")
        with pytest.raises(KeyError):
            repository.task_location(other["id"], first.id)
        with store.transaction() as connection:
            connection.execute("UPDATE privacy_control SET epoch=epoch+1 WHERE id=1")
        assert repository.task_location(conversation["id"], first.id)["before"] == 4
        tasks.scrub(first.id)
        with pytest.raises(KeyError):
            repository.task_location(conversation["id"], first.id)
        with store.transaction() as connection:
            connection.execute("UPDATE privacy_control SET blocked=1 WHERE id=1")
        with pytest.raises(WorkflowBlocked):
            repository.task_location(conversation["id"], task.id)
    finally:
        store.close()


def test_schema_two_backup_default_protection_and_no_fabricated_history(tmp_path: Path):
    path = tmp_path / "workflow.sqlite3"
    with sqlite3.connect(path) as connection:
        for statement in (*_SCHEMA, *SCHEMA_2):
            connection.execute(statement)
        connection.execute("UPDATE schema_version SET version=2")
        connection.execute("INSERT INTO task(id,request,status,created_at,updated_at,result) VALUES('old-task','old synthetic request','Completed','2026-01-01','2026-01-01','{\"response\":\"old result\"}')")
    store = WorkflowStore(path)
    try:
        repository = ConversationRepository(store)
        default = repository.ensure_default("ensure-one")
        assert repository.ensure_default("ensure-two")["id"] == default["id"]
        assert repository.timeline(default["id"])["items"] == []
        with store.read_connection() as connection:
            assert connection.execute("SELECT request,result FROM task WHERE id='old-task'").fetchone()[:] == ("old synthetic request", '{"response":"old result"}')
            assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 5
        with pytest.raises(ValueError, match="archived"):
            repository.update(default["id"], default["revision"], "archive-default", archived=True)
        with pytest.raises(ValueError, match="title"):
            repository.update(default["id"], default["revision"], "rename-default", title="private synthetic title")
        with pytest.raises(ValueError, match="deleted"):
            repository.cleanup(default["id"], default["revision"], "delete-default", delete=True, scrub=lambda _: None)
        with pytest.raises(sqlite3.IntegrityError):
            with store.transaction() as connection:
                connection.execute("DELETE FROM conversation WHERE id=?", (default["id"],))
    finally:
        store.close()
    backups = list(tmp_path.glob("workflow.sqlite3.schema-2-*.backup"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        assert connection.execute("SELECT request FROM task WHERE id='old-task'").fetchone()[0] == "old synthetic request"


def test_persistent_directory_timeline_and_bounded_context_survive_cache_expiry(tmp_path: Path):
    path = tmp_path / "workflow.sqlite3"
    store = WorkflowStore(path)
    repository = ConversationRepository(store)
    tasks = PersistentTaskManager(store)
    conversations = [repository.create("synthetic project " + str(i), "project-" + str(i)) for i in range(6)]
    for number in range(12):
        task = bound(store, tasks, repository, conversations[0], "turn-" + str(number), "question " + str(number))
        complete(tasks, task.id, "answer " + str(number))
    clock = [0.0]
    runtime = ConversationRuntime(repository=repository, clock=lambda: clock[0])
    first = runtime.freeze(conversations[0]["id"], 0, 100, cloud=False, history_allowed=True)
    clock[0] = 3600.0
    for conversation in conversations:
        runtime.snapshot(conversation["id"])
    assert runtime.freeze(conversations[0]["id"], 0, 100, cloud=False, history_allowed=True)["history"] == first["history"]
    assert len(first["history"]) <= 16
    assert first["history"][-2]["content"] == "question 11"
    store.close()
    reopened = WorkflowStore(path)
    try:
        repository = ConversationRepository(reopened)
        assert len(repository.page(limit=100)["items"]) == 7
        messages = []
        cursor = None
        while True:
            page = repository.timeline(conversations[0]["id"], before=cursor, limit=7)
            messages.extend(page["items"])
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        assert len(messages) == 36
        assert len({message["id"] for message in messages}) == 36
        assert len([message for message in messages if message["kind"] == "user_message"]) == 12
    finally:
        reopened.close()


def test_dispatcher_retry_binds_once_and_registration_failure_rolls_back(tmp_path: Path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    try:
        tasks, repository = PersistentTaskManager(store), ConversationRepository(store)
        conversation = repository.ensure_default()
        dispatcher = SubmissionDispatcher(cast(AgentCore, object()), tasks, ExecutionJournal(store), guard=lambda _: None, on_idle=lambda: None)

        def submit(command, identity):
            return dispatcher.submit("synthetic send", command_id=command, authorized_tools=frozenset(), high_risk_authorized_tools=frozenset(),
                register=lambda task: repository.bind(task.id, identity, 0, command_id=command))

        original = submit("same-send", conversation["id"])
        assert submit("same-send", conversation["id"]).id == original.id
        other = repository.create("other", "other-project")
        with pytest.raises(ConversationConflict):
            submit("same-send", other["id"])
        with pytest.raises(ConversationConflict):
            submit("rejected-send", "missing-conversation")
        assert len(tasks.list_tasks()) == 1
        with store.read_connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM turn_ref").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM conversation_item").fetchone()[0] == 3
            assert connection.execute("SELECT COUNT(*) FROM command").fetchone()[0] == 1
    finally:
        store.close()


def test_waiting_reply_updates_same_item_then_clear_rejects_late_publication(tmp_path: Path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    try:
        tasks, repository = PersistentTaskManager(store), ConversationRepository(store)
        conversation = repository.ensure_default()
        task = bound(store, tasks, repository, conversation, "send")
        tasks.update_status(task.id, TaskStatus.PLANNING)
        tasks.update_status(task.id, TaskStatus.WAITING)
        waiting = next(item for item in repository.timeline(conversation["id"])["items"] if item["kind"] == "assistant_message")
        assert waiting["body"]
        assert repository.load(conversation["id"], 0, 10) == ()
        tasks.update_status(task.id, TaskStatus.RUNNING)
        tasks.update_status(task.id, TaskStatus.COMPLETED, result={"response": "synthetic final reply"})
        completed = next(item for item in repository.timeline(conversation["id"])["items"] if item["kind"] == "assistant_message")
        assert completed["id"] == waiting["id"]
        assert completed["revision"] > waiting["revision"]
        assert completed["body"] == "synthetic final reply"
        assert len(repository.load(conversation["id"], 0, 10)) == 1
        result = repository.cleanup(conversation["id"], repository.get(conversation["id"])["revision"], "clear", delete=False, scrub=tasks.scrub)
        assert result["status"] == "Completed"
        assert result["context_epoch"] == 1
        with pytest.raises(WorkflowBlocked):
            repository.publish(task.id, "late private reply", (), (), "cloud_allowed")
        assert all(item["body"] == "" and item["redacted_at"] for item in repository.timeline(conversation["id"])["items"])
        with pytest.raises(ConversationConflict):
            bound(store, tasks, repository, conversation, "old-epoch-send")
        assert len(tasks.list_tasks()) == 1
    finally:
        store.close()


def test_unrelated_history_can_be_readmitted_after_global_privacy_barrier(tmp_path: Path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    try:
        tasks, repository = PersistentTaskManager(store), ConversationRepository(store)
        conversation = repository.ensure_default()
        task = bound(store, tasks, repository, conversation, "synthetic")
        complete(tasks, task.id, "independent reply")
        privacy = HumanizationStore(store)
        privacy.block_privacy()
        with pytest.raises(WorkflowBlocked):
            repository.load(conversation["id"], 0, 10)
        privacy.unblock_privacy()
        assert len(repository.load(conversation["id"], 0, 10)) == 1
        with pytest.raises(WorkflowBlocked):
            with store.transaction() as connection:
                store.check_privacy(connection, task.id)
    finally:
        store.close()
