"""Single workflow database, explicit transactions and durable audit outbox."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock, local
from tempfile import mkstemp
from typing import Any
from uuid import uuid4

from .models import AuditEvent, DependencyRef, WorkflowBlocked


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
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            with self.transaction() as conn:
                exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
                if exists:
                    rows = conn.execute("SELECT version FROM schema_version").fetchall()
                    if len(rows) != 1 or rows[0][0] != 1:
                        raise ValueError("Unsupported workflow schema version")
                else:
                    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
                    if tables:
                        raise ValueError("Workflow database has no recognized schema version")
                    for statement in _SCHEMA:
                        conn.execute(statement)
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

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
        rows = conn.execute("SELECT d.*,v.revision AS current_revision,v.active,v.use_policy FROM dependency d LEFT JOIN dependency_version v ON d.kind=v.kind AND d.object_id=v.object_id WHERE d.task_id=?", (task_id,)).fetchall()
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

    def add_validator(self, validator: Callable[[str, tuple[DependencyRef, ...]], None]) -> None:
        self._validators.append(validator)

    def validate_external(self, task_id: str) -> None:
        refs = self.dependencies(task_id)
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
