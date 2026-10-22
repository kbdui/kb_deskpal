"""Single workflow database, explicit transactions and durable audit outbox."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock, local
from tempfile import mkstemp
from typing import Any
from uuid import uuid4

from deskpal.tasks.models import utc_now

from .models import AuditEvent, DependencyRef, WorkflowBlocked
from .migrations import SCHEMA_VERSION, migrate_schema_1


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


_SCHEMA = (
    "CREATE TABLE schema_version(version INTEGER NOT NULL)",
    "INSERT INTO schema_version VALUES(1)",
    "CREATE TABLE task(id TEXT PRIMARY KEY,request TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,result TEXT,error TEXT,revision INTEGER NOT NULL DEFAULT 1,cancel_requested INTEGER NOT NULL DEFAULT 0,use_policy TEXT NOT NULL DEFAULT 'cloud_allowed',scrubbed INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE submission(sequence INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL UNIQUE REFERENCES task(id),status TEXT NOT NULL DEFAULT 'Queued')",
    "CREATE TABLE command(id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,task_id TEXT NOT NULL REFERENCES task(id))",
    "CREATE TABLE immutableplan(id TEXT PRIMARY KEY,task_id TEXT NOT NULL UNIQUE REFERENCES task(id),hash TEXT NOT NULL,canonical_json TEXT NOT NULL,scrubbed INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE attempt(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES task(id),step_index INTEGER NOT NULL,status TEXT NOT NULL,result TEXT,review_verdict TEXT,review_evidence TEXT,UNIQUE(task_id,step_index))",
    "CREATE TABLE dependency(task_id TEXT NOT NULL REFERENCES task(id),kind TEXT NOT NULL,object_id TEXT NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(task_id,kind,object_id))",
    "CREATE INDEX dependency_object ON dependency(kind,object_id)",
    "CREATE TABLE dependency_version(kind TEXT NOT NULL,object_id TEXT NOT NULL,revision INTEGER NOT NULL,active INTEGER NOT NULL,use_policy TEXT NOT NULL,PRIMARY KEY(kind,object_id))",
    "CREATE TABLE audit_outbox(sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,event TEXT NOT NULL,task_id TEXT,data TEXT NOT NULL,delivered INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE deletionjob(id TEXT PRIMARY KEY,memory_id TEXT NOT NULL,expected_revision INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'Pending')",
    "CREATE TABLE memory_block(memory_id TEXT PRIMARY KEY)",
)

# Additive access paths for recognized schema 1 databases, including upgrades.
# Existing histories pay the index-build cost once during owned startup, inside
# the initialization transaction; snapshot reads never create or rebuild them.
_PRESENTATION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS task_presentation_status ON task(status,updated_at DESC,id DESC)",
    "CREATE INDEX IF NOT EXISTS task_presentation_latest ON task(updated_at DESC,id DESC)",
    "CREATE INDEX IF NOT EXISTS submission_presentation_status ON submission(status,sequence,task_id)",
)


class WorkflowStore:
    def __init__(self, path: Path, *, queue_capacity: int = 100, request_max_bytes: int = 65536,
                 plan_max_bytes: int = 262144, result_max_bytes: int = 262144) -> None:
        for value in (queue_capacity, request_max_bytes, plan_max_bytes, result_max_bytes):
            if type(value) is not int or value < 1:
                raise ValueError("Workflow limits must be positive integers")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.queue_capacity = queue_capacity
        self.request_max_bytes = request_max_bytes
        self.plan_max_bytes = plan_max_bytes
        self.result_max_bytes = result_max_bytes
        self._lock = RLock()
        self._local = local()
        self._closed = False
        self._validators: list[Callable[[str, tuple[DependencyRef, ...]], None]] = []
        self._conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False, timeout=5)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA foreign_keys=ON")
            exists = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
            if exists:
                versions = self._conn.execute("SELECT version FROM schema_version").fetchall()
                if len(versions) != 1 or versions[0][0] not in {1, SCHEMA_VERSION}:
                    raise ValueError("Unsupported workflow schema version")
                if versions[0][0] == 1:
                    self.backup(self.path.with_name(self.path.name + ".schema-1-" + uuid4().hex + ".backup"))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            with self.transaction() as conn:
                exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
                if exists:
                    rows = conn.execute("SELECT version FROM schema_version").fetchall()
                    if len(rows) != 1 or rows[0][0] not in {1, SCHEMA_VERSION}:
                        raise ValueError("Unsupported workflow schema version")
                    if rows[0][0] == 1:
                        migrate_schema_1(conn)
                else:
                    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
                    if tables:
                        raise ValueError("Workflow database has no recognized schema version")
                    for statement in _SCHEMA:
                        conn.execute(statement)
                    migrate_schema_1(conn)
                for statement in _PRESENTATION_INDEXES:
                    conn.execute(statement)
                # A previous process cannot know whether a reserved request was
                # sent. Its slot remains consumed and recovery never retries it.
                conn.execute("UPDATE task_call_attempt SET status='unknown' WHERE status='reserved'")
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        """Serialize a single read without acquiring SQLite's writer slot.

        A nested read observes the owning transaction on this same connection.
        This is a read projection only; sends, Started and publication still
        verify privacy inside their existing write/publication boundaries.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("WorkflowStore is closed")
            yield self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Nested calls reuse the outer transaction; any inner failure poisons it."""
        with self._lock:
            if self._closed:
                raise RuntimeError("WorkflowStore is closed")
            depth = getattr(self._local, "depth", 0)
            if depth == 0:
                self._conn.execute("BEGIN IMMEDIATE")
                self._local.failed = False
            self._local.depth = depth + 1
            try:
                yield self._conn
            except BaseException:
                self._local.failed = True
                if depth == 0:
                    self._conn.rollback()
                raise
            else:
                if depth == 0:
                    if self._local.failed:
                        self._conn.rollback()
                        raise RuntimeError("Nested workflow transaction failed")
                    try:
                        self._conn.commit()
                    except BaseException:
                        self._conn.rollback()
                        raise
            finally:
                self._local.depth = depth

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                if getattr(self._local, "depth", 0):
                    raise RuntimeError("Cannot close inside a workflow transaction")
                self._conn.close()
                self._closed = True

    def record_event(self, event: str, task_id: str | None = None,
                     data: Mapping[str, Any] | None = None) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO audit_outbox(id,event,task_id,data) VALUES(?,?,?,?)",
                         (str(uuid4()), event, task_id, canonical(dict(data or {}))))

    def pending_audit(self, limit: int = 100) -> tuple[AuditEvent, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("Audit page size must be 1..100")
        with self.transaction() as conn:
            rows = conn.execute("SELECT * FROM audit_outbox WHERE delivered=0 ORDER BY sequence LIMIT ?", (limit,)).fetchall()
            return tuple(AuditEvent(row["id"], row["event"], row["task_id"], json.loads(row["data"])) for row in rows)

    def mark_audit_delivered(self, event_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE audit_outbox SET delivered=1 WHERE id=?", (event_id,))

    def set_dependency_version(self, kind: str, object_id: str, revision: int, *,
                               active: bool = True, use_policy: str = "local_only") -> None:
        if revision < 1 or not kind or not object_id or use_policy not in {"local_only", "cloud_allowed"}:
            raise ValueError("Invalid dependency version")
        with self.transaction() as conn:
            conn.execute("INSERT INTO dependency_version VALUES(?,?,?,?,?) ON CONFLICT(kind,object_id) DO UPDATE SET revision=excluded.revision,active=excluded.active,use_policy=excluded.use_policy",
                         (kind, object_id, revision, int(active), use_policy))

    def dependencies(self, task_id: str) -> tuple[DependencyRef, ...]:
        with self.transaction() as conn:
            rows = conn.execute("SELECT kind,object_id,revision FROM dependency WHERE task_id=? ORDER BY kind,object_id", (task_id,)).fetchall()
            return tuple(DependencyRef(*row) for row in rows)

    def check_dependencies(self, conn: sqlite3.Connection, task_id: str) -> None:
        self.check_privacy(conn, task_id)
        rows = conn.execute("SELECT d.*,v.revision AS current_revision,v.active,v.use_policy FROM dependency d LEFT JOIN dependency_version v ON d.kind=v.kind AND d.object_id=v.object_id WHERE d.task_id=?", (task_id,)).fetchall()
        self._check_dependency_rows(conn, task_id, rows)

    def check_context_dependencies(self, conn: sqlite3.Connection, task_id: str, purpose: str) -> None:
        self.check_privacy(conn, task_id)
        rows = conn.execute("SELECT d.*,v.revision AS current_revision,v.active,v.use_policy FROM context_dependency d LEFT JOIN dependency_version v ON d.kind=v.kind AND d.object_id=v.object_id WHERE d.task_id=? AND d.purpose=?", (task_id, purpose)).fetchall()
        self._check_dependency_rows(conn, task_id, rows)

    def _check_dependency_rows(self, conn: sqlite3.Connection, task_id: str, rows: list[sqlite3.Row]) -> None:
        task = conn.execute("SELECT use_policy,scrubbed FROM task WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise KeyError(task_id)
        if task["scrubbed"]:
            raise WorkflowBlocked("Task content has been forgotten")
        for row in rows:
            if row["kind"] == "memory":
                pending = conn.execute("SELECT 1 FROM memory_block WHERE memory_id=?", (row["object_id"],)).fetchone()
                if pending:
                    raise WorkflowBlocked("Memory deletion is pending")
                # Memory authority is a separate database, checked by an external validator.
                continue
            if row["current_revision"] != row["revision"] or not row["active"]:
                raise WorkflowBlocked("Task dependency changed or is inactive")
            if row["use_policy"] == "local_only" and task["use_policy"] == "cloud_allowed":
                raise WorkflowBlocked("Task use policy exceeds its source policy")

    def check_privacy(self, conn: sqlite3.Connection, task_id: str | None = None) -> None:
        """Order new use/Started with the same transaction as privacy blocking."""
        privacy = conn.execute("SELECT epoch,blocked FROM privacy_control WHERE id=1").fetchone()
        assert privacy is not None
        if privacy["blocked"]:
            raise WorkflowBlocked("Privacy cleanup is pending")
        if task_id is not None:
            task = conn.execute("SELECT privacy_epoch FROM task WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            if task[0] != privacy["epoch"]:
                raise WorkflowBlocked("Task privacy generation expired")
            turn = conn.execute("SELECT privacy_epoch FROM turn_ref WHERE task_id=?", (task_id,)).fetchone()
            if turn is not None and turn[0] != privacy["epoch"]:
                raise WorkflowBlocked("Task privacy generation expired")

    def record_terminal_effect(self, conn: sqlite3.Connection, task_id: str, status: str) -> None:
        """Only durable, known tool outcomes qualify; text-only turns never do."""
        if status not in {"Completed", "Failed"}:
            return
        task = conn.execute("SELECT revision,scrubbed FROM task WHERE id=?", (task_id,)).fetchone()
        if task is None or task["scrubbed"]:
            return
        try:
            self.check_dependencies(conn, task_id)
        except WorkflowBlocked:
            return
        outcomes = conn.execute("SELECT step_index,status,result FROM attempt WHERE task_id=? ORDER BY step_index", (task_id,)).fetchall()
        if not outcomes or any(row["step_index"] != index for index, row in enumerate(outcomes)):
            return
        plan_row = conn.execute("SELECT canonical_json,hash,scrubbed FROM immutableplan WHERE task_id=?", (task_id,)).fetchone()
        if plan_row is None or plan_row["scrubbed"]:
            return
        # Do not infer a whole-task success from only a prefix of successful
        # calls, even if a caller writes an otherwise legal terminal status.
        if sha256(plan_row["canonical_json"].encode("utf-8")).hexdigest() != plan_row["hash"]:
            return
        try:
            plan = json.loads(plan_row["canonical_json"])
            calls = plan["steps"]
        except (ValueError, KeyError, TypeError):
            return
        if not isinstance(calls, list) or len(outcomes) > len(calls) or status == "Completed" and len(outcomes) != len(calls):
            return
        success = failure = 0
        for outcome in outcomes:
            if outcome["status"] not in {"Succeeded", "Failed"} or outcome["result"] is None:
                return
            try:
                result = json.loads(outcome["result"])
            except (ValueError, TypeError):
                return
            if not isinstance(result, dict) or type(result.get("success")) is not bool:
                return
            if result["success"] != (outcome["status"] == "Succeeded"):
                return
            call = calls[outcome["step_index"]]
            if not isinstance(call, dict) or result.get("tool_name") != call.get("name") or result.get("call_id") != call.get("call_id"):
                return
            success += int(result["success"])
            failure += int(not result["success"])
        if not outcomes or status == "Completed" and (not success or failure) or status == "Failed" and not failure:
            return
        epoch = conn.execute("SELECT epoch FROM privacy_control WHERE id=1").fetchone()[0]
        if conn.execute("SELECT COUNT(*) FROM companion_effect_outbox").fetchone()[0] >= 4096:
            self.record_event("companion_effect_skipped", task_id, {"reason": "projection_capacity"})
            return
        # The key survives policy upgrades and companion resets.
        conn.execute("INSERT OR IGNORE INTO companion_effect_outbox(id,task_id,status,success,failure,epoch,terminal_revision,created_at) VALUES(?,?,?,?,?,?,?,?)",
                     ("task:" + task_id, task_id, status, success, failure, epoch, task["revision"], utc_now().isoformat()))

    def add_validator(self, validator: Callable[[str, tuple[DependencyRef, ...]], None]) -> None:
        self._validators.append(validator)

    def validate_external(self, task_id: str) -> None:
        refs = self.dependencies(task_id)
        for validator in tuple(self._validators):
            validator(task_id, refs)

    def validate_external_context(self, task_id: str, purpose: str) -> None:
        with self.transaction() as conn:
            rows = conn.execute("SELECT kind,object_id,revision FROM dependency WHERE task_id=? UNION SELECT kind,object_id,revision FROM context_dependency WHERE task_id=? AND purpose=?", (task_id, task_id, purpose)).fetchall()
            refs = tuple(DependencyRef(*row) for row in rows)
        for validator in tuple(self._validators):
            validator(task_id, refs)

    def backup(self, destination: Path) -> None:
        """Publish a complete online backup at a new path, never overwrite one.

        The temporary SQLite lives beside the destination. A create-only hard
        link publishes it atomically after closing SQLite; a concurrent creator,
        existing symlink or hardlink cannot be overwritten by this operation.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("WorkflowStore is closed")
            if getattr(self._local, "depth", 0):
                raise RuntimeError("Cannot back up inside a workflow transaction")
            target = Path(destination)
            if target.resolve() == self.path.resolve():
                raise ValueError("Backup destination must differ from workflow database")
            if os.path.lexists(target):
                raise FileExistsError("Backup destination already exists; choose a new path")
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = mkstemp(prefix=".workflow-backup-", suffix=".sqlite3.tmp", dir=target.parent)
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                output = sqlite3.connect(str(temporary))
                try:
                    self._conn.backup(output)
                finally:
                    output.close()
                # os.link is create-new on both Windows and POSIX. In contrast,
                # replace/rename may overwrite another process's destination.
                os.link(temporary, target)
            finally:
                for suffix in ("-journal", "-wal", "-shm", ""):
                    Path(temporary_name + suffix).unlink(missing_ok=True)
