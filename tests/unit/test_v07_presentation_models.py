"""Presentation contracts and projections over synthetic workflow stores only."""

from dataclasses import FrozenInstanceError
from random import Random
import sqlite3
from types import MappingProxyType

import pytest

from deskpal.presentation import PresentationSnapshot, expression_for_status, status_label
from deskpal.tasks.models import TaskStatus
from deskpal.workflow import PersistentTaskManager, WorkflowStore


@pytest.fixture
def workflow(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    try:
        yield store, PersistentTaskManager(store)
    finally:
        store.close()


def payload(**changes):
    return {"schema_version": 1, "session_id": "synthetic-session", "sequence": 7,
            "memory_epoch": 2, "status": "Running", "task_id": "opaque-task", "revision": 3,
            **changes}


@pytest.mark.parametrize(("status", "expression", "label"), [
    ("Idle", "neutral", "空闲"), ("Created", "neutral", "待处理"),
    ("Planning", "thinking", "规划中"), ("Running", "focused", "执行中"),
    ("Waiting", "neutral", "等待确认"), ("Completed", "pleased", "已完成"),
    ("Failed", "alert", "执行失败"), ("Cancelled", "neutral", "已取消"),
    ("NeedsReview", "neutral", "需要审查"), ("Uncertain", "neutral", "结果待确认"),
    ("Unknown", "neutral", "状态未知"), ("Closing", "neutral", "正在关闭"),
    ("Closed", "neutral", "已关闭"), ("synthetic-private-body", "neutral", "状态未知"),
])
def test_fixed_neutral_mappings_cover_all_task_and_host_statuses(status, expression, label):
    assert expression_for_status(status) == expression
    assert status_label(status) == label


def test_snapshot_is_detached_immutable_and_accepts_readonly_mapping():
    source = payload()
    snapshot = PresentationSnapshot.from_payload(MappingProxyType(source))
    source["status"] = "Failed"
    assert snapshot.status == "Running" and snapshot.revision == 3
    with pytest.raises(FrozenInstanceError):
        snapshot.status = "Failed"
    for status in TaskStatus:
        assert PresentationSnapshot.from_payload(payload(status=status.value)).status == status.value
    for status in ("Idle", "Unknown"):
        assert PresentationSnapshot.from_payload(payload(status=status, task_id=None, revision=0)).task_id is None


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": 2}, {"schema_version": 1.0},
    {"sequence": True}, {"sequence": -1}, {"sequence": "7"},
    {"memory_epoch": False}, {"memory_epoch": -1}, {"memory_epoch": 2.0},
    {"revision": True}, {"revision": -1}, {"revision": object()},
    {"session_id": " "}, {"session_id": object()},
    {"status": "synthetic-private-body"}, {"status": TaskStatus.RUNNING},
    {"status": object()}, {"task_id": object()}, {"task_id": ""},
    {"task_id": None}, {"status": "Idle"},
    {"status": "Idle", "task_id": None, "revision": 1},
])
def test_snapshot_rejects_coercion_and_inconsistent_metadata(changes):
    with pytest.raises(ValueError):
        PresentationSnapshot.from_payload(payload(**changes))


def test_snapshot_rejects_missing_extra_or_non_mapping_payloads():
    missing = payload()
    missing.pop("revision")
    for invalid in (missing, {**payload(), "request": object()}, {**payload(), 1: "value"}, [], object()):
        with pytest.raises(ValueError):
            PresentationSnapshot.from_payload(invalid)


def test_projection_selects_active_then_fifo_queue_then_review_then_latest_outcome(workflow):
    _, tasks = workflow
    assert tasks.presentation_snapshot() == {"schema_version": 1, "sequence": 0,
        "status": "Idle", "task_id": None, "revision": 0}
    review = tasks.submit("synthetic waiting", command_id="review")
    assert tasks.claim_next().id == review.id
    tasks.update_status(review.id, TaskStatus.PLANNING)
    tasks.update_status(review.id, TaskStatus.WAITING)
    tasks.finish_submission(review.id, "Blocked")
    first = tasks.submit("synthetic first", command_id="first")
    second = tasks.submit("synthetic second", command_id="second")
    assert tasks.presentation_snapshot()["task_id"] == first.id
    assert tasks.claim_next().id == first.id
    assert tasks.presentation_snapshot()["status"] == "Created"
    for status in (TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.COMPLETED):
        current = tasks.update_status(first.id, status)
        snapshot = tasks.presentation_snapshot()
        if status != TaskStatus.COMPLETED:
            assert (snapshot["task_id"], snapshot["status"], snapshot["revision"]) == (
                first.id, status.value, current.revision)
    assert tasks.presentation_snapshot()["task_id"] == second.id
    tasks.request_cancel(second.id)
    assert tasks.presentation_snapshot()["task_id"] == review.id
    tasks.request_cancel(review.id)
    snapshot = tasks.presentation_snapshot()
    assert (snapshot["task_id"], snapshot["status"]) == (review.id, "Cancelled")
    assert tasks.presentation_snapshot() == snapshot


@pytest.mark.parametrize("review_status", [TaskStatus.NEEDS_REVIEW, TaskStatus.UNCERTAIN])
def test_review_and_uncertain_precede_newer_terminal_history(workflow, review_status):
    _, tasks = workflow
    review = tasks.create("synthetic review")
    tasks.update_status(review.id, TaskStatus.PLANNING)
    tasks.update_status(review.id, review_status)
    terminal = tasks.create("synthetic terminal")
    tasks.update_status(terminal.id, TaskStatus.FAILED, error="synthetic error")
    snapshot = tasks.presentation_snapshot()
    assert snapshot["task_id"] == review.id and snapshot["status"] == review_status.value


def test_projection_sql_cannot_read_bodies_or_write_and_returns_one_metadata_row(workflow, monkeypatch):
    store, tasks = workflow
    task = tasks.submit("synthetic-private-request", command_id="private")
    tasks.claim_next()
    tasks.update_status(task.id, TaskStatus.PLANNING)
    tasks.update_status(task.id, TaskStatus.RUNNING)
    tasks.update_status(task.id, TaskStatus.COMPLETED,
                        result={"body": "synthetic-private-result"}, error="synthetic-private-error")
    monkeypatch.setattr(tasks, "get", lambda *_: pytest.fail("must not materialize Task"))
    monkeypatch.setattr(tasks, "list_tasks", lambda **_: pytest.fail("must not list task bodies"))
    statements = []
    denied = []

    def authorize(action, table, column, database, trigger):
        if (action == sqlite3.SQLITE_READ and (
                (table == "task" and column in {"request", "result", "error"})
                or (table == "audit_outbox" and column == "data")
                or table in {"immutableplan", "attempt"})) or action in {
                    sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            denied.append((action, table, column))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with store.transaction() as conn:
        conn.set_trace_callback(statements.append)
        conn.set_authorizer(authorize)
        try:
            snapshot = tasks.presentation_snapshot()
        finally:
            conn.set_authorizer(None)
            conn.set_trace_callback(None)
    assert not denied
    assert set(snapshot) == {"schema_version", "sequence", "status", "task_id", "revision"}
    assert snapshot["task_id"] == task.id
    assert len(statements) == 2 and all("LIMIT 1" in statement for statement in statements)
    assert "synthetic-private" not in str(snapshot)


def test_outbox_cleanup_cannot_roll_back_sequence_or_change_stable_outcome(workflow):
    store, tasks = workflow
    task = tasks.create("synthetic")
    tasks.update_status(task.id, TaskStatus.FAILED)
    first = tasks.presentation_snapshot()
    with store.transaction() as conn:
        conn.execute("DELETE FROM audit_outbox")
    assert tasks.presentation_snapshot() == first
    store.record_event("synthetic_later_event")
    later = tasks.presentation_snapshot()
    assert later["sequence"] > first["sequence"]
    assert (later["task_id"], later["revision"]) == (first["task_id"], first["revision"])


def test_projection_failures_and_corrupt_status_are_not_idle(workflow):
    store, tasks = workflow
    task = tasks.create("synthetic")
    with store.transaction() as conn:
        conn.execute("UPDATE task SET status='unsupported' WHERE id=?", (task.id,))
    with pytest.raises(ValueError):
        tasks.presentation_snapshot()
    store.close()
    with pytest.raises(RuntimeError):
        tasks.presentation_snapshot()


def test_existing_schema_two_gains_indexes_once_without_changing_tables(workflow):
    store, tasks = workflow
    tasks.create("synthetic existing history")
    names = {"task_presentation_status", "task_presentation_latest", "submission_presentation_status"}
    with store.transaction() as conn:
        tables = tuple(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"))
        for name in names:
            conn.execute(f"DROP INDEX {name}")
    store.close()
    reopened = WorkflowStore(store.path)
    try:
        with reopened.transaction() as conn:
            assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 2
            assert [tuple(row) for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name")] == [tuple(row) for row in tables]
            actual = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            assert names <= actual
    finally:
        reopened.close()


@pytest.mark.parametrize("seed", [7, 17, 29, 43])
def test_indexed_projection_matches_original_order_even_for_unusual_metadata(workflow, seed):
    store, tasks = workflow
    random = Random(seed)
    statuses = [status.value for status in TaskStatus] + ["unsupported"]
    # The original query is an independent ordering oracle, not the new CTE.
    original = (
        "SELECT t.id,t.status,t.revision FROM task AS t "
        "LEFT JOIN submission AS s ON s.task_id=t.id ORDER BY CASE "
        "WHEN t.status IN ('Planning','Running') THEN 0 "
        "WHEN s.status='Claimed' AND t.status IN ('Created','Waiting') THEN 0 "
        "WHEN s.status='Queued' THEN 1 "
        "WHEN t.status IN ('Created','Waiting','NeedsReview','Uncertain') THEN 2 "
        "ELSE 3 END, CASE WHEN s.status='Queued' THEN s.sequence END, "
        "t.updated_at DESC,t.id DESC LIMIT 1")
    for case in range(12):
        with store.transaction() as conn:
            conn.execute("DELETE FROM submission")
            conn.execute("DELETE FROM task")
            for index in range(24):
                task_id = f"synthetic-{case:02d}-{index:02d}"
                status = random.choice(statuses)
                # Deliberate timestamp ties exercise the stable id tie-breaker.
                timestamp = f"2026-01-01T00:00:0{random.randrange(3)}+00:00"
                conn.execute("INSERT INTO task(id,request,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                             (task_id, "synthetic body", status, timestamp, timestamp))
                submission = random.choice([None, "Queued", "Claimed", "Blocked", "Finished"])
                if submission is not None:
                    conn.execute("INSERT INTO submission(task_id,status) VALUES(?,?)", (task_id, submission))
            expected = dict(conn.execute(original).fetchone())
        if expected["status"] == "unsupported":
            with pytest.raises(ValueError):
                tasks.presentation_snapshot()
        else:
            actual = tasks.presentation_snapshot()
            assert (actual["task_id"], actual["status"], actual["revision"]) == (
                expected["id"], expected["status"], expected["revision"])
