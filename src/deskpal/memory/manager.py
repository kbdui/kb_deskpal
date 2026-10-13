"""Local SQLite storage and literal text search for basic memories."""

import math
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from types import TracebackType
from uuid import uuid4

from .models import MemoryKind, MemoryRecord


_SCHEMA_VERSION = 1
_COLUMNS = (
    "id, content, kind, importance, confidence, created_at, "
    "last_access_at, decay_rate, task_id"
)


def _number(value: float, field: str, *, bounded: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Memory {field} must be a finite number")
    try:
        normalized = float(value)
    except OverflowError:
        raise ValueError(f"Memory {field} must be a finite number") from None
    if not math.isfinite(normalized) or normalized < 0 or (bounded and normalized > 1):
        interval = "between 0 and 1" if bounded else "non-negative"
        raise ValueError(f"Memory {field} must be finite and {interval}")
    return normalized


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"Memory {field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    # A fixed UTC offset and fractional precision make text ordering chronological.
    return value.isoformat(timespec="microseconds")


def _normalize(record: MemoryRecord) -> MemoryRecord:
    if not isinstance(record, MemoryRecord):
        raise ValueError("Memory must be a MemoryRecord")
    if not isinstance(record.content, str) or not record.content.strip():
        raise ValueError("Memory content must be a non-empty string")
    if not isinstance(record.kind, MemoryKind):
        raise ValueError("Memory kind must be a MemoryKind member")
    if not isinstance(record.id, str):
        raise ValueError("Memory id must be a string")
    if record.task_id is not None and not isinstance(record.task_id, str):
        raise ValueError("Memory task_id must be a string or None")
    return replace(
        record,
        id=record.id or str(uuid4()),
        importance=_number(record.importance, "importance", bounded=True),
        confidence=_number(record.confidence, "confidence", bounded=True),
        decay_rate=_number(record.decay_rate, "decay_rate"),
        created_at=_utc(record.created_at, "created_at"),
        last_access_at=(
            None if record.last_access_at is None else _utc(record.last_access_at, "last_access_at")
        ),
    )


def _record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        content=row["content"],
        kind=MemoryKind(row["kind"]),
        id=row["id"],
        importance=row["importance"],
        confidence=row["confidence"],
        created_at=datetime.fromisoformat(row["created_at"]),
        last_access_at=(
            None if row["last_access_at"] is None else datetime.fromisoformat(row["last_access_at"])
        ),
        decay_rate=row["decay_rate"],
        task_id=row["task_id"],
    )


class SQLiteMemoryManager:
    """Serialize operations on one connection, with explicit resource ownership.

    Searches return detached records and update access times in one transaction.
    A context manager closes its connection on exit; close is also idempotent.
    Persistence failures propagate to the caller. Audit events belong to Core.
    """

    def __init__(self, database_path: str | Path) -> None:
        if not isinstance(database_path, (str, Path)) or not str(database_path).strip():
            raise ValueError("Memory database_path must be a non-empty string or Path")
        database = str(database_path)
        if database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._closed = False
        self._connection = sqlite3.connect(database, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        try:
            with self._connection:
                version = self._connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, _SCHEMA_VERSION):
                    raise RuntimeError("Unsupported memory database schema version")
                self._connection.execute(
                    """CREATE TABLE IF NOT EXISTS memories (
                        id TEXT PRIMARY KEY NOT NULL,
                        content TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN (
                            'working', 'episodic', 'semantic', 'goal', 'relationship'
                        )),
                        importance REAL NOT NULL CHECK(importance BETWEEN 0 AND 1),
                        confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                        created_at TEXT NOT NULL,
                        last_access_at TEXT,
                        decay_rate REAL NOT NULL CHECK(decay_rate >= 0),
                        task_id TEXT
                    )"""
                )
                self._connection.execute(
                    "CREATE INDEX IF NOT EXISTS memories_created_at "
                    "ON memories(created_at DESC, id ASC)"
                )
                self._connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        except BaseException:
            self._connection.close()
            self._closed = True
            raise

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Memory manager is closed")

    def save(self, record: MemoryRecord) -> MemoryRecord:
        with self._lock:
            self._ensure_open()
            saved = _normalize(record)
            with self._connection:
                self._connection.execute(
                    f"INSERT INTO memories ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "content = excluded.content, kind = excluded.kind, "
                    "importance = excluded.importance, confidence = excluded.confidence, "
                    "created_at = excluded.created_at, last_access_at = excluded.last_access_at, "
                    "decay_rate = excluded.decay_rate, task_id = excluded.task_id",
                    (
                        saved.id, saved.content, saved.kind.value, saved.importance, saved.confidence,
                        _timestamp(saved.created_at),
                        None if saved.last_access_at is None else _timestamp(saved.last_access_at),
                        saved.decay_rate, saved.task_id,
                    ),
                )
            return saved

    def search(self, query: str, *, limit: int = 5,
               kind: MemoryKind | None = None) -> tuple[MemoryRecord, ...]:
        with self._lock:
            self._ensure_open()
            if not isinstance(query, str):
                raise ValueError("Memory query must be a string")
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError("Memory limit must be a positive integer")
            if kind is not None and not isinstance(kind, MemoryKind):
                raise ValueError("Memory kind must be a MemoryKind member or None")
            clauses: list[str] = []
            parameters: list[str | int] = []
            if query:
                escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                clauses.append("content LIKE ? ESCAPE '\\'")
                parameters.append(f"%{escaped}%")
            if kind is not None:
                clauses.append("kind = ?")
                parameters.append(kind.value)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            # SQLite binds signed 64-bit integers; larger limits still mean all rows.
            parameters.append(min(limit, 2**63 - 1))
            with self._connection:
                rows = self._connection.execute(
                    f"SELECT {_COLUMNS} FROM memories{where} ORDER BY created_at DESC, id ASC LIMIT ?",
                    parameters,
                ).fetchall()
                accessed_at = datetime.now(timezone.utc)
                records = tuple(replace(_record(row), last_access_at=accessed_at) for row in rows)
                self._connection.executemany(
                    "UPDATE memories SET last_access_at = ? WHERE id = ?",
                    ((_timestamp(accessed_at), record.id) for record in records),
                )
            return records

    def delete(self, memory_id: str) -> bool:
        with self._lock:
            self._ensure_open()
            if not isinstance(memory_id, str):
                raise ValueError("Memory id must be a string")
            with self._connection:
                cursor = self._connection.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
                return cursor.rowcount > 0

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> "SQLiteMemoryManager":
        with self._lock:
            self._ensure_open()
            return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc_value: BaseException | None, traceback: TracebackType | None) -> None:
        self.close()
