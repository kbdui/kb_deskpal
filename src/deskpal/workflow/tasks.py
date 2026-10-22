"""Persistent task snapshots and bounded, atomically claimed submissions."""

from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
import json
import sqlite3
from typing import Any
from uuid import uuid4

from deskpal.tasks.manager import _TRANSITIONS, _copy_json
from deskpal.tasks.models import Task, TaskStatus, utc_now

from .models import DependencyRef, QueueFull, WorkflowBlocked
from .store import WorkflowStore, canonical


# Fixed metadata candidates, with indexed seeks and lower-priority result gates.
# The nonqueued/queued split preserves SQLite's original NULL-first secondary
# ordering for active tasks. Only the tiny candidate sets need a temporary sort.
# Healthy-store work relies on the existing single-claim/queue-capacity rules;
# LIMIT alone is not a bound on scanned rows in arbitrary/corrupt databases.
# Priority gates sit outside the indexed LIMIT subqueries: even if SQLite
# evaluates a gated-out branch, it cannot scan all review/terminal history.
_PRESENTATION_SQL = """
WITH
active_nonqueued AS (
    SELECT id,status,revision,updated_at FROM (
        SELECT * FROM (
            SELECT t.id,t.status,t.revision,t.updated_at
            FROM task AS t INDEXED BY task_presentation_status
            WHERE t.status='Planning' AND NOT EXISTS (
                SELECT 1 FROM submission AS s WHERE s.task_id=t.id AND s.status='Queued'
            ) ORDER BY t.updated_at DESC,t.id DESC LIMIT 1
        )
        UNION ALL
        SELECT * FROM (
            SELECT t.id,t.status,t.revision,t.updated_at
            FROM task AS t INDEXED BY task_presentation_status
            WHERE t.status='Running' AND NOT EXISTS (
                SELECT 1 FROM submission AS s WHERE s.task_id=t.id AND s.status='Queued'
            ) ORDER BY t.updated_at DESC,t.id DESC LIMIT 1
        )
        UNION ALL
        SELECT * FROM (
            SELECT t.id,t.status,t.revision,t.updated_at
            FROM submission AS s INDEXED BY submission_presentation_status
            CROSS JOIN task AS t
            WHERE t.id=s.task_id AND s.status='Claimed' AND t.status IN ('Created','Waiting')
            ORDER BY t.updated_at DESC,t.id DESC LIMIT 1
        )
    ) ORDER BY updated_at DESC,id DESC LIMIT 1
),
active_queued AS (
    SELECT * FROM (
        SELECT t.id,t.status,t.revision,t.updated_at
        FROM submission AS s INDEXED BY submission_presentation_status
        CROSS JOIN task AS t
        WHERE t.id=s.task_id AND s.status='Queued' AND t.status IN ('Planning','Running')
        ORDER BY s.sequence LIMIT 1
    ) WHERE NOT EXISTS (SELECT 1 FROM active_nonqueued)
),
active AS (
    SELECT * FROM active_nonqueued UNION ALL SELECT * FROM active_queued
),
queued AS (
    SELECT * FROM (
        SELECT t.id,t.status,t.revision,t.updated_at
        FROM submission AS s INDEXED BY submission_presentation_status
        CROSS JOIN task AS t
        WHERE t.id=s.task_id AND s.status='Queued'
        ORDER BY s.sequence LIMIT 1
    ) WHERE NOT EXISTS (SELECT 1 FROM active)
),
review AS (
    SELECT id,status,revision,updated_at FROM (
        SELECT * FROM (
            SELECT id,status,revision,updated_at FROM task INDEXED BY task_presentation_status
            WHERE status='Created'
            ORDER BY updated_at DESC,id DESC LIMIT 1
        )
        UNION ALL
        SELECT * FROM (
            SELECT id,status,revision,updated_at FROM task INDEXED BY task_presentation_status
            WHERE status='Waiting'
            ORDER BY updated_at DESC,id DESC LIMIT 1
        )
        UNION ALL
        SELECT * FROM (
            SELECT id,status,revision,updated_at FROM task INDEXED BY task_presentation_status
            WHERE status='NeedsReview'
            ORDER BY updated_at DESC,id DESC LIMIT 1
        )
        UNION ALL
        SELECT * FROM (
            SELECT id,status,revision,updated_at FROM task INDEXED BY task_presentation_status
            WHERE status='Uncertain'
            ORDER BY updated_at DESC,id DESC LIMIT 1
        )
    ) WHERE NOT EXISTS (SELECT 1 FROM active) AND NOT EXISTS (SELECT 1 FROM queued)
    ORDER BY updated_at DESC,id DESC LIMIT 1
),
outcome AS (
    SELECT * FROM (
        SELECT id,status,revision,updated_at FROM task INDEXED BY task_presentation_latest
        ORDER BY updated_at DESC,id DESC LIMIT 1
    )
    WHERE NOT EXISTS (SELECT 1 FROM active) AND NOT EXISTS (SELECT 1 FROM queued)
        AND NOT EXISTS (SELECT 1 FROM review)
)
SELECT id,status,revision FROM (
    SELECT * FROM active UNION ALL SELECT * FROM queued
    UNION ALL SELECT * FROM review UNION ALL SELECT * FROM outcome
) LIMIT 1
"""


def dependency_ref(value: Any) -> DependencyRef:
    if isinstance(value, DependencyRef):
        ref = value
    elif isinstance(value, Mapping):
        ref = DependencyRef(str(value["kind"]), str(value.get("object_id", value.get("id", ""))), int(value["revision"]))
    elif len(value) == 2:
        ref = DependencyRef("memory", str(value[0]), int(value[1]))
    else:
        ref = DependencyRef(str(value[0]), str(value[1]), int(value[2]))
    if not ref.kind or not ref.object_id or ref.revision < 1:
        raise ValueError("Invalid task dependency")
    return ref


def task_from_row(row: sqlite3.Row) -> Task:
    return Task(row["id"], row["request"], TaskStatus(row["status"]),
                datetime.fromisoformat(row["created_at"]), datetime.fromisoformat(row["updated_at"]),
                json.loads(row["result"]) if row["result"] is not None else None,
                row["error"], row["revision"], bool(row["cancel_requested"]))


class PersistentTaskManager:
    def __init__(self, store: WorkflowStore) -> None:
        self.store = store

    def _create(self, conn: sqlite3.Connection, request: str, use_policy: str = "cloud_allowed") -> Task:
        self.store.check_privacy(conn)
        if not isinstance(request, str) or not request.strip():
            raise ValueError("Task request must be a non-empty string")
        if len(request.encode("utf-8")) > self.store.request_max_bytes:
            raise ValueError("Task request exceeds byte limit")
        if use_policy not in {"cloud_allowed", "local_only"}:
            raise ValueError("Invalid task use policy")
        task_id, now = str(uuid4()), utc_now().isoformat()
        privacy_epoch = conn.execute("SELECT epoch FROM privacy_control WHERE id=1").fetchone()[0]
        conn.execute("INSERT INTO task(id,request,status,created_at,updated_at,use_policy,privacy_epoch) VALUES(?,?,?,?,?,?,?)",
                     (task_id, request, TaskStatus.CREATED.value, now, now, use_policy, privacy_epoch))
        self.store.record_event("task_created", task_id)
        row = conn.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
        assert row is not None
        return task_from_row(row)

    def create(self, request: str) -> Task:
        with self.store.transaction() as conn:
            return self._create(conn, request)

    def get(self, task_id: str) -> Task | None:
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
            return None if row is None else task_from_row(row)

    def update_status(self, task_id: str, status: TaskStatus, *, result: Any = None,
                      error: str | None = None) -> Task:
        if not isinstance(status, TaskStatus):
            raise ValueError("Task status must be a TaskStatus member")
        with self.store.transaction() as conn:
            current = self.get(task_id)
            if current is None:
                raise KeyError(task_id)
            if current.status == status:
                return current
            if status not in _TRANSITIONS[current.status]:
                raise ValueError(f"Cannot transition task from {current.status.value} to {status.value}")
            if error is not None and not isinstance(error, str):
                raise ValueError("Task error must be a string or None")
            saved = canonical(_copy_json(result))
            if len(saved.encode("utf-8")) > self.store.result_max_bytes:
                saved = canonical({"output_omitted": True})
            scrubbed = conn.execute("SELECT scrubbed FROM task WHERE id=?", (task_id,)).fetchone()[0]
            try:
                self.store.check_privacy(conn, task_id)
            except WorkflowBlocked:
                scrubbed = True
            if scrubbed:
                saved, error = "null", None
            # A late cancellation cannot be overwritten by ordinary successful finalization.
            if status == TaskStatus.COMPLETED and current.cancel_requested:
                status = TaskStatus.CANCELLED
            conn.execute("UPDATE task SET status=?,result=?,error=?,revision=revision+1,updated_at=? WHERE id=?",
                         (status.value, saved, error, utc_now().isoformat(), task_id))
            self.store.record_terminal_effect(conn, task_id, status.value)
            self.store.record_event("task_status_changed", task_id, {"previous_status": current.status.value, "status": status.value})
            if status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                self.finish_submission(task_id)
            updated = self.get(task_id)
            assert updated is not None
            return updated

    def list_tasks(self, limit: int | None = None, offset: int = 0) -> tuple[Task, ...]:
        if offset < 0 or (limit is not None and not 1 <= limit <= 100):
            raise ValueError("Task page limit must be 1..100 and offset non-negative")
        with self.store.transaction() as conn:
            return tuple(task_from_row(row) for row in conn.execute("SELECT * FROM task ORDER BY created_at,id LIMIT ? OFFSET ?", (-1 if limit is None else limit, offset)))

    def submit(self, request: str, *, command_id: str, dependencies: tuple[Any, ...] = (),
               use_policy: str = "cloud_allowed") -> Task:
        if not isinstance(command_id, str) or not command_id.strip():
            raise ValueError("Submission command_id must be non-empty")
        refs = tuple(sorted((dependency_ref(value) for value in dependencies), key=lambda ref: (ref.kind, ref.object_id)))
        payload_hash = sha256(canonical({"request": request, "dependencies": [(r.kind, r.object_id, r.revision) for r in refs], "use_policy": use_policy}).encode()).hexdigest()
        with self.store.transaction() as conn:
            previous = conn.execute("SELECT * FROM command WHERE id=?", (command_id,)).fetchone()
            if previous:
                if previous["payload_hash"] != payload_hash:
                    raise ValueError("Command ID already used with a different payload")
                existing = self.get(previous["task_id"])
                assert existing is not None
                return existing
            count = conn.execute("SELECT COUNT(*) FROM submission WHERE status IN ('Queued','Claimed','Blocked')").fetchone()[0]
            if count >= self.store.queue_capacity:
                raise QueueFull("Persistent submission queue is full")
            task = self._create(conn, request, use_policy)
            for ref in refs:
                conn.execute("INSERT INTO dependency VALUES(?,?,?,?)", (task.id, ref.kind, ref.object_id, ref.revision))
            self.store.check_dependencies(conn, task.id)
            conn.execute("INSERT INTO command VALUES(?,?,?)", (command_id, payload_hash, task.id))
            conn.execute("INSERT INTO submission(task_id) VALUES(?)", (task.id,))
            self.store.record_event("submission_queued", task.id)
            return task

    def page_summaries(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Bounded metadata query never reads request or result body columns."""
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("Task page limit must be 1..100 and offset non-negative")
        with self.store.transaction() as conn:
            rows = conn.execute("SELECT id,status,revision,cancel_requested,created_at,updated_at "
                "FROM task ORDER BY created_at,id LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
            items = [{**dict(row), "cancel_requested": bool(row["cancel_requested"])} for row in rows[:limit]]
            version = int(conn.execute("SELECT COALESCE(MAX(sequence),0) FROM audit_outbox").fetchone()[0])
            return {"items": items, "offset": offset, "has_more": len(rows) > limit, "version": version}

    def presentation_snapshot(self) -> dict[str, Any]:
        """Read one authoritative metadata projection, never task/plan bodies.

        Running/planning or claimed work takes precedence over queued work,
        then work awaiting a decision and finally the most recent outcome.
        Claiming does not invent a new TaskStatus: Claimed + Created remains
        Created. The outbox AUTOINCREMENT high-water mark survives row cleanup.
        Indexes are initialized once by WorkflowStore, including existing schema
        1 databases. Reads seek fixed status candidates instead of sorting all
        history; queue scans rely on the existing bounded-pending-work invariant.
        """
        with self.store.transaction() as conn:
            sequence_row = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='audit_outbox' LIMIT 1").fetchone()
            sequence = 0 if sequence_row is None else int(sequence_row[0])
            row = conn.execute(_PRESENTATION_SQL).fetchone()
            if row is None:
                return {"schema_version": 1, "sequence": sequence, "status": "Idle",
                        "task_id": None, "revision": 0}
            # Corrupt/unsupported metadata must fail the read, never look idle.
            status = TaskStatus(row["status"])
            return {"schema_version": 1, "sequence": sequence, "status": status.value,
                    "task_id": row["id"], "revision": row["revision"]}

    def claim_next(self) -> Task | None:
        while True:
            with self.store.transaction() as conn:
                if conn.execute("SELECT 1 FROM submission WHERE status='Claimed'").fetchone():
                    return None
                row = conn.execute("SELECT task_id FROM submission WHERE status='Queued' ORDER BY sequence LIMIT 1").fetchone()
                if row is None:
                    return None
                task_id = row[0]
            try:
                self.store.validate_external(task_id)
            except (ValueError, WorkflowBlocked):
                with self.store.transaction() as conn:
                    self._block_queued(conn, task_id)
                continue
            with self.store.transaction() as conn:
                if conn.execute("SELECT 1 FROM submission WHERE status='Claimed'").fetchone():
                    return None
                try:
                    self.store.check_dependencies(conn, task_id)
                except WorkflowBlocked:
                    self._block_queued(conn, task_id)
                    continue
                claimed = conn.execute("UPDATE submission SET status='Claimed' WHERE task_id=? AND status='Queued'", (task_id,))
                if claimed.rowcount:
                    self.store.record_event("submission_claimed", task_id)
                    return self.get(task_id)

    def _block_queued(self, conn: sqlite3.Connection, task_id: str) -> None:
        # Cross-store validation releases our write lock. A cancellation may
        # already have finalized this submission before validation returns.
        row = conn.execute("SELECT s.status AS submission_status,t.status AS task_status FROM submission s JOIN task t ON t.id=s.task_id WHERE s.task_id=?", (task_id,)).fetchone()
        if row is None or row["submission_status"] != "Queued":
            return
        status = TaskStatus(row["task_status"])
        terminal = status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        conn.execute("UPDATE submission SET status=? WHERE task_id=?", ("Finished" if terminal else "Blocked", task_id))
        if not terminal and status != TaskStatus.UNCERTAIN:
            self.update_status(task_id, TaskStatus.NEEDS_REVIEW)

    def finish_submission(self, task_id: str, status: str = "Finished") -> None:
        if status not in {"Finished", "Blocked", "Cancelled"}:
            raise ValueError("Invalid submission finish status")
        with self.store.transaction() as conn:
            conn.execute("UPDATE submission SET status=? WHERE task_id=?", (status, task_id))

    def task_use_policy(self, task_id: str) -> str:
        with self.store.transaction() as conn:
            row = conn.execute("SELECT use_policy FROM task WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            return str(row[0])

    def block_for_review(self, task_id: str, error: str) -> Task:
        with self.store.transaction() as conn:
            task = self.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.status == TaskStatus.UNCERTAIN:
                return task
            result = self.update_status(task_id, TaskStatus.NEEDS_REVIEW, error=error)
            self.finish_submission(task_id, "Blocked")
            return result

    def requeue(self, task_id: str) -> Task:
        self.store.validate_external(task_id)
        with self.store.transaction() as conn:
            task = self.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.status != TaskStatus.WAITING or task.cancel_requested:
                raise WorkflowBlocked("Only a waiting task may be queued for confirmation")
            self.store.check_dependencies(conn, task_id)
            row = conn.execute("SELECT status FROM submission WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise WorkflowBlocked("Task has no persistent submission")
            if row[0] in {"Queued", "Claimed"}:
                raise WorkflowBlocked("Task is already queued or claimed")
            count = conn.execute("SELECT COUNT(*) FROM submission WHERE status IN ('Queued','Claimed','Blocked') AND task_id!=?", (task_id,)).fetchone()[0]
            if count >= self.store.queue_capacity:
                raise QueueFull("Persistent submission queue is full")
            conn.execute("UPDATE submission SET status='Queued' WHERE task_id=?", (task_id,))
            self.store.record_event("confirmation_queued", task_id)
            return task

    def request_cancel(self, task_id: str) -> Task:
        with self.store.transaction() as conn:
            current = self.get(task_id)
            if current is None:
                raise KeyError(task_id)
            if current.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                return current
            conn.execute("UPDATE task SET cancel_requested=1,revision=revision+1,updated_at=? WHERE id=?", (utc_now().isoformat(), task_id))
            self.store.record_event("task_cancel_requested", task_id)
            if current.status not in {TaskStatus.RUNNING, TaskStatus.PLANNING, TaskStatus.UNCERTAIN}:
                return self.update_status(task_id, TaskStatus.CANCELLED)
            result = self.get(task_id)
            assert result is not None
            return result

    def scrub(self, task_id: str) -> Task:
        with self.store.transaction() as conn:
            conn.execute("UPDATE task SET request='[forgotten]',result=NULL,error=NULL,scrubbed=1,revision=revision+1 WHERE id=?", (task_id,))
            conn.execute("UPDATE immutableplan SET canonical_json='{}',scrubbed=1 WHERE task_id=?", (task_id,))
            conn.execute("UPDATE attempt SET result=NULL,review_evidence=NULL WHERE task_id=?", (task_id,))
            conn.execute("UPDATE audit_outbox SET data='{}' WHERE task_id=?", (task_id,))
            conn.execute("DELETE FROM context_snapshot WHERE task_id=?", (task_id,))
            conn.execute("DELETE FROM companion_effect_outbox WHERE task_id=?", (task_id,))
            conn.execute("UPDATE dependency_version SET active=0,revision=revision+1 WHERE kind='dialogue_turn' AND object_id IN (?,?,?)", (task_id, task_id + ":user", task_id + ":assistant"))
            conn.execute("UPDATE submission SET status='Blocked' WHERE task_id=? AND status IN ('Queued','Claimed')", (task_id,))
            task = self.get(task_id)
            if task is None:
                raise KeyError(task_id)
            return task

    def resume(self, task_id: str) -> Task:
        with self.store.transaction() as conn:
            task = self.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.status != TaskStatus.NEEDS_REVIEW:
                raise WorkflowBlocked("Only unstarted tasks awaiting review can be resumed")
            if conn.execute("SELECT 1 FROM attempt WHERE task_id=?", (task_id,)).fetchone():
                raise WorkflowBlocked("A task with previous attempts cannot be resumed")
            self.store.check_dependencies(conn, task_id)
            if task.cancel_requested:
                raise WorkflowBlocked("Cancelled task cannot be resumed")
            submission = conn.execute("SELECT status FROM submission WHERE task_id=?", (task_id,)).fetchone()
            if submission is None:
                raise WorkflowBlocked("Task has no persistent submission to resume")
            count = conn.execute("SELECT COUNT(*) FROM submission WHERE status IN ('Queued','Claimed','Blocked') AND task_id!=?", (task_id,)).fetchone()[0]
            if count >= self.store.queue_capacity:
                raise QueueFull("Persistent submission queue is full")
            plan_exists = conn.execute("SELECT 1 FROM immutableplan WHERE task_id=? AND scrubbed=0", (task_id,)).fetchone() is not None
            result = self.update_status(task_id, TaskStatus.WAITING if plan_exists else TaskStatus.CREATED)
            conn.execute("UPDATE submission SET status=? WHERE task_id=?", ("Blocked" if plan_exists else "Queued", task_id))
            self.store.record_event("task_resumed", task_id)
            return result
