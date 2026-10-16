"""Versioned SQLite memory storage; indexes never confer factual eligibility."""

import hashlib
import json
import math
import sqlite3
import struct
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Iterator, cast
from uuid import uuid4

from .contracts import (
    ClaimType, Clock, DeletionReceipt, EmbeddingManifest, MemoryDraft, MemoryError,
    MemoryItem, MemoryNotFound, MemoryPage, MemorySource, MemoryStatus, RevisionConflict,
    UsePolicy, utc_now,
)
from .models import MemoryKind


_VERSION = 2
_ORIGINS = frozenset(("user_message", "user_confirmation", "tool_result", "imported_document",
                      "assistant_output", "legacy"))


def _text(value: object, name: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise MemoryError(f"{name} must be a {'possibly empty' if empty else 'non-empty'} string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise MemoryError(f"{name} must contain valid Unicode") from None
    if size > maximum:
        raise MemoryError(f"{name} exceeds the size limit")
    return value


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MemoryError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _optional_utc(value: object, name: str) -> datetime | None:
    return None if value is None else _utc(value, name)


def _finite(value: object, name: str, *, bounded: bool = False, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MemoryError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError:
        raise MemoryError(f"{name} must be a finite number") from None
    if not math.isfinite(result) or result < 0 or (positive and result == 0) or (bounded and result > 1):
        raise MemoryError(f"{name} is outside its permitted range")
    return result


def _revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MemoryError("revision must be a positive integer")
    return value


def _stamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat(timespec="microseconds")


def _load_time(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MemoryError("Stored memory timestamp is invalid")
    return _utc(datetime.fromisoformat(value), "Stored memory timestamp")


def _required_time(value: object) -> datetime:
    result = _load_time(value)
    if result is None:
        raise MemoryError("Stored memory timestamp is missing")
    return result


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _object(value: str) -> dict[str, object]:
    decoded: object = json.loads(value)
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        raise MemoryError("Stored memory object is invalid")
    return cast(dict[str, object], decoded)


def _source_data(source: MemorySource) -> dict[str, object]:
    return {"content": source.content, "origin": source.origin, "id": source.id,
            "owner_id": source.owner_id, "session_id": source.session_id, "task_id": source.task_id,
            "created_at": _stamp(source.created_at), "use_policy": source.use_policy.value}


def _draft_data(draft: MemoryDraft) -> dict[str, object]:
    return {"content": draft.content, "quote": draft.quote, "claim_type": draft.claim_type.value,
            "kind": draft.kind.value, "subject": draft.subject, "predicate": draft.predicate,
            "value": draft.value, "scope": draft.scope, "profile_key": draft.profile_key,
            "importance": draft.importance, "extraction_confidence": draft.extraction_confidence,
            "valid_from": _stamp(draft.valid_from), "valid_to": _stamp(draft.valid_to),
            "expires_at": _stamp(draft.expires_at), "pinned": draft.pinned,
            "half_life_days": draft.half_life_days}


def _source_from(data: dict[str, object]) -> MemorySource:
    return MemorySource(
        content=str(data["content"]), origin=str(data["origin"]), id=str(data["id"]),
        owner_id=str(data["owner_id"]), session_id=str(data["session_id"]),
        task_id=None if data["task_id"] is None else str(data["task_id"]),
        created_at=_required_time(data["created_at"]), use_policy=UsePolicy(str(data["use_policy"])),
    )


def _draft_from(data: dict[str, object]) -> MemoryDraft:
    return MemoryDraft(
        content=str(data["content"]), quote=str(data["quote"]),
        claim_type=ClaimType(str(data["claim_type"])), kind=MemoryKind(str(data["kind"])),
        subject=str(data["subject"]), predicate=str(data["predicate"]), value=str(data["value"]),
        scope=str(data["scope"]), profile_key=str(data["profile_key"]),
        importance=_finite(data["importance"], "Stored importance", bounded=True),
        extraction_confidence=_finite(data["extraction_confidence"], "Stored confidence", bounded=True),
        valid_from=_load_time(data["valid_from"]), valid_to=_load_time(data["valid_to"]),
        expires_at=_load_time(data["expires_at"]), pinned=data["pinned"] is True,
        half_life_days=_finite(data["half_life_days"], "Stored half life", positive=True),
    )


def _normalize_source(source: MemorySource) -> MemorySource:
    if not isinstance(source, MemorySource):
        raise MemoryError("source must be a MemorySource")
    _text(source.content, "Source content", 65536)
    _text(source.id, "Source id", 256, empty=True)
    _text(source.owner_id, "Owner id", 128)
    _text(source.session_id, "Session id", 128)
    if source.task_id is not None:
        _text(source.task_id, "Task id", 256)
    if not isinstance(source.origin, str) or source.origin not in _ORIGINS:
        raise MemoryError("Source origin is unsupported")
    if not isinstance(source.use_policy, UsePolicy):
        raise MemoryError("Source use_policy must be a UsePolicy")
    return replace(source, created_at=_utc(source.created_at, "Source created_at"))


def _normalize_draft(source: MemorySource, draft: MemoryDraft, now: datetime,
                     *, accepted: bool) -> tuple[MemoryDraft, bool]:
    if not isinstance(draft, MemoryDraft):
        raise MemoryError("draft must be a MemoryDraft")
    _text(draft.content, "Memory content", 8192)
    _text(draft.quote, "Memory quote", 16384)
    if draft.quote not in source.content:
        raise MemoryError("Memory quote must belong to its source")
    for name, value, maximum, empty in (
        ("subject", draft.subject, 128, False), ("predicate", draft.predicate, 128, False),
        ("value", draft.value, 8192, True), ("scope", draft.scope, 256, False),
        ("profile_key", draft.profile_key, 128, True),
    ):
        _text(value, name, maximum, empty=empty)
    if not isinstance(draft.kind, MemoryKind) or not isinstance(draft.claim_type, ClaimType):
        raise MemoryError("Memory kind and claim_type must be enum members")
    if type(draft.pinned) is not bool:
        raise MemoryError("Memory pinned must be a boolean")
    permitted = {
        ClaimType.USER_REPORT: ("user_message", "user_confirmation"),
        ClaimType.OBSERVED_EVENT: ("tool_result",),
        ClaimType.EXTERNAL_CLAIM: ("imported_document", "user_message", "user_confirmation"),
        ClaimType.HYPOTHESIS: ("assistant_output", "user_message", "user_confirmation"),
        ClaimType.LEGACY: ("legacy",),
    }
    if source.origin not in permitted[draft.claim_type]:
        raise MemoryError("Memory claim type is incompatible with source origin")
    if draft.profile_key and (draft.claim_type != ClaimType.USER_REPORT or draft.subject != "user"
                              or not draft.value.strip()):
        raise MemoryError("Profile fields require a user self-report and explicit value")
    if accepted and (draft.claim_type in (ClaimType.LEGACY, ClaimType.HYPOTHESIS)
                     or source.origin == "assistant_output"):
        raise MemoryError("This source cannot be automatically accepted")
    begin = _optional_utc(draft.valid_from, "valid_from")
    end = _optional_utc(draft.valid_to, "valid_to")
    expiry = _optional_utc(draft.expires_at, "expires_at")
    if begin is not None and end is not None and begin >= end:
        raise MemoryError("Memory validity must be a non-empty half-open interval")
    if (expiry is not None and expiry <= now) or (accepted and end is not None and end <= now):
        raise MemoryError("Memory is already expired")
    default_expiry = not accepted and expiry is None
    return replace(
        draft, valid_from=begin, valid_to=end,
        expires_at=now + timedelta(days=7) if default_expiry else expiry,
        importance=_finite(draft.importance, "importance", bounded=True),
        extraction_confidence=_finite(draft.extraction_confidence, "extraction_confidence", bounded=True),
        half_life_days=_finite(draft.half_life_days, "half_life_days", positive=True),
    ), default_expiry


class SQLiteMemoryRepository:
    """A single locked connection owns immutable revisions and atomic invalidation.

    Accepted means approved attribution, not independently verified truth. Each
    item has one source; explicit forgetting conservatively clears all records
    sharing any source in its revision history. Supersession is not derivation.
    """

    def __init__(self, database_path: str | Path, *, clock: Clock = utc_now) -> None:
        if not isinstance(database_path, (str, Path)) or not str(database_path).strip():
            raise MemoryError("database_path must be a non-empty string or Path")
        if not callable(clock):
            raise MemoryError("clock must be callable")
        path = str(database_path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = RLock()
        self._closed = False
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        try:
            version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
            if version not in (0, 1, _VERSION):
                raise MemoryError("Unsupported memory database schema version")
            tables = {str(row[0]) for row in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version == 0 and "memories" in tables:
                raise MemoryError("Unversioned legacy memory database requires explicit recovery")
            if version == 1 and path != ":memory:":
                self._backup_v1(Path(path))
            with self._transaction():
                self._schema()
                if version == 1:
                    self._migrate_v1()
                self._db.execute(f"PRAGMA user_version = {_VERSION}")
        except BaseException:
            self._db.close()
            self._closed = True
            raise

    def _backup_v1(self, path: Path) -> None:
        backup_path = Path(str(path) + ".v1.bak")
        try:
            with backup_path.open("xb"):
                pass
        except FileExistsError:
            raise MemoryError("A migration backup already exists; verify it before retrying") from None
        try:
            backup = sqlite3.connect(backup_path)
            try:
                self._db.backup(backup)
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise MemoryError("Memory migration backup verification failed")
            finally:
                backup.close()
        except BaseException:
            backup_path.unlink(missing_ok=True)
            raise

    def _schema(self) -> None:
        statements = (
            "CREATE TABLE IF NOT EXISTS memory_meta (name TEXT PRIMARY KEY, value INTEGER NOT NULL)",
            "INSERT OR IGNORE INTO memory_meta VALUES ('epoch', 0)",
            "CREATE TABLE IF NOT EXISTS memory_sources (id TEXT PRIMARY KEY, payload TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS memory_revisions ("
            "id TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL, source_id TEXT NOT NULL, "
            "draft TEXT NOT NULL, recorded_at TEXT NOT NULL, accepted_at TEXT, supersedes TEXT, "
            "reason TEXT NOT NULL, default_expiry INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(id, revision), "
            "FOREIGN KEY(source_id) REFERENCES memory_sources(id))",
            "CREATE TABLE IF NOT EXISTS memory_heads (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, "
            "FOREIGN KEY(id,revision) REFERENCES memory_revisions(id,revision))",
            "CREATE INDEX IF NOT EXISTS memory_source_refs ON memory_revisions(source_id)",
            "CREATE INDEX IF NOT EXISTS memory_status_refs ON memory_revisions(status,id,revision)",
            "CREATE TABLE IF NOT EXISTS memory_tombstones (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, "
            "deleted_at TEXT NOT NULL, epoch INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS memory_deletion_tasks (memory_id TEXT NOT NULL, task_id TEXT NOT NULL, "
            "PRIMARY KEY(memory_id,task_id), FOREIGN KEY(memory_id) REFERENCES memory_tombstones(id))",
            "CREATE TABLE IF NOT EXISTS memory_source_tombstones (id TEXT PRIMARY KEY)",
            "CREATE TABLE IF NOT EXISTS memory_ingestions (token TEXT PRIMARY KEY, memory_id TEXT NOT NULL, "
            "fingerprint TEXT)",
            "CREATE TABLE IF NOT EXISTS memory_learning_receipts (token TEXT PRIMARY KEY, claimed_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS memory_manifests (version TEXT PRIMARY KEY, model TEXT NOT NULL, "
            "dimensions INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS memory_embeddings (memory_id TEXT NOT NULL, revision INTEGER NOT NULL, "
            "version TEXT NOT NULL, vector BLOB NOT NULL, PRIMARY KEY(memory_id,revision,version), "
            "FOREIGN KEY(version) REFERENCES memory_manifests(version))",
            "CREATE TABLE IF NOT EXISTS memory_legacy_metadata (id TEXT PRIMARY KEY, payload TEXT NOT NULL)",
        )
        for statement in statements:
            self._db.execute(statement)

    def _migrate_v1(self) -> None:
        rows = self._db.execute("SELECT * FROM memories ORDER BY id").fetchall()
        for row in rows:
            source = MemorySource(str(row["content"]), origin="legacy", id=str(uuid4()),
                                  task_id=row["task_id"], created_at=_required_time(row["created_at"]))
            draft = MemoryDraft(source.content, source.content, claim_type=ClaimType.LEGACY,
                                kind=MemoryKind(row["kind"]), importance=float(row["importance"]),
                                extraction_confidence=0.0)
            item = MemoryItem(str(row["id"]), 1, MemoryStatus.LEGACY, source, draft, source.created_at)
            self._store_source(source)
            self._append(item)
            self._db.execute("INSERT INTO memory_legacy_metadata VALUES (?, ?)",
                             (item.id, _json({key: row[key] for key in row.keys()})))
        if rows:
            self._bump()

    def _open(self) -> None:
        if self._closed:
            raise RuntimeError("Memory repository is closed")

    def _now(self) -> datetime:
        return _utc(self._clock(), "clock result")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def _epoch(self) -> int:
        return int(self._db.execute("SELECT value FROM memory_meta WHERE name='epoch'").fetchone()[0])

    def _bump(self) -> int:
        self._db.execute("UPDATE memory_meta SET value=value+1 WHERE name='epoch'")
        return self._epoch()

    @property
    def epoch(self) -> int:
        with self._lock:
            self._open()
            self._expire_candidates(self._now())
            return self._epoch()

    def _store_source(self, source: MemorySource) -> MemorySource:
        if not source.id:
            source = replace(source, id=str(uuid4()))
        if self._db.execute("SELECT 1 FROM memory_source_tombstones WHERE id=?", (source.id,)).fetchone():
            raise MemoryError("A forgotten source cannot be replayed")
        payload = _json(_source_data(source))
        previous = self._db.execute("SELECT payload FROM memory_sources WHERE id=?", (source.id,)).fetchone()
        if previous is not None:
            if str(previous[0]) != payload:
                raise MemoryError("Source ids are immutable and cannot be reused with different content")
        else:
            self._db.execute("INSERT INTO memory_sources VALUES (?, ?)", (source.id, payload))
        return source

    def _append(self, item: MemoryItem, *, default_expiry: bool = False) -> None:
        self._db.execute(
            "INSERT INTO memory_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item.id, item.revision, item.status.value, item.source.id, _json(_draft_data(item.draft)),
             _stamp(item.recorded_at), _stamp(item.accepted_at), item.supersedes, item.change_reason,
             int(default_expiry)),
        )
        self._db.execute("INSERT INTO memory_heads VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision",
                         (item.id, item.revision))

    def _decode(self, row: sqlite3.Row) -> MemoryItem:
        if "source_payload" in row.keys():
            payload = row["source_payload"]
        else:
            source_row = self._db.execute("SELECT payload FROM memory_sources WHERE id=?", (row["source_id"],)).fetchone()
            payload = None if source_row is None else source_row[0]
        if payload is None:
            raise MemoryError("Memory evidence is missing")
        return MemoryItem(str(row["id"]), int(row["revision"]), MemoryStatus(row["status"]),
                          _source_from(_object(str(payload))), _draft_from(_object(str(row["draft"]))),
                          _required_time(row["recorded_at"]), _load_time(row["accepted_at"]),
                          row["supersedes"], str(row["reason"]))

    def _deleted(self, row: sqlite3.Row) -> MemoryItem:
        when = _required_time(row["deleted_at"])
        return MemoryItem(str(row["id"]), int(row["revision"]), MemoryStatus.DELETED,
                          MemorySource("", origin="deleted", created_at=when),
                          MemoryDraft("", "", claim_type=ClaimType.LEGACY), when)

    def _get(self, memory_id: str, revision: int | None = None) -> MemoryItem | None:
        tombstone = self._db.execute("SELECT * FROM memory_tombstones WHERE id=?", (memory_id,)).fetchone()
        if tombstone is not None:
            return self._deleted(tombstone)
        if revision is None:
            row = self._db.execute("SELECT r.* FROM memory_revisions r JOIN memory_heads h "
                                   "ON r.id=h.id AND r.revision=h.revision WHERE r.id=?", (memory_id,)).fetchone()
        else:
            row = self._db.execute("SELECT * FROM memory_revisions WHERE id=? AND revision=?",
                                   (memory_id, revision)).fetchone()
        return None if row is None else self._decode(row)

    def get(self, memory_id: str, *, revision: int | None = None) -> MemoryItem | None:
        with self._lock:
            self._open()
            _text(memory_id, "Memory id", 256)
            if revision is not None:
                _revision(revision)
            self._expire_candidates(self._now())
            return self._get(memory_id, revision)

    def list_items(self, *, status: MemoryStatus | None = None,
                   include_history: bool = False) -> tuple[MemoryItem, ...]:
        with self._lock:
            self._open()
            if status is not None and not isinstance(status, MemoryStatus):
                raise MemoryError("status must be a MemoryStatus")
            if type(include_history) is not bool:
                raise MemoryError("include_history must be a boolean")
            self._expire_candidates(self._now())
            query = "SELECT r.*,s.payload AS source_payload FROM memory_revisions r"
            if not include_history:
                query += " JOIN memory_heads h ON r.id=h.id AND r.revision=h.revision"
            query += " LEFT JOIN memory_sources s ON s.id=r.source_id"
            parameters = () if status is None else (status.value,)
            if status is not None:
                query += " WHERE r.status=?"
            rows = self._db.execute(query + " ORDER BY r.recorded_at DESC, r.id ASC, r.revision DESC", parameters).fetchall()
            items = [self._decode(row) for row in rows]
            if status in (None, MemoryStatus.DELETED):
                items.extend(self._deleted(row) for row in self._db.execute("SELECT * FROM memory_tombstones ORDER BY id"))
            return tuple(item for item in items if status is None or item.status == status)

    def page_items(self, *, status: MemoryStatus | None = None, limit: int = 100,
                   offset: int = 0) -> MemoryPage:
        """Page current heads and erased tombstones in SQL under one epoch."""
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise MemoryError("limit must be 1..100 and offset non-negative")
        if status is not None and not isinstance(status, MemoryStatus):
            raise MemoryError("status must be a MemoryStatus")
        with self._lock:
            self._open()
            self._expire_candidates(self._now())
            query = ("SELECT r.id,r.revision,r.recorded_at AS ordering,0 AS erased FROM memory_revisions r "
                     "JOIN memory_heads h ON r.id=h.id AND r.revision=h.revision")
            parameters: tuple[object, ...] = ()
            if status is not None:
                query += " WHERE r.status=?"
                parameters = (status.value,)
            if status in (None, MemoryStatus.DELETED):
                query += " UNION ALL SELECT id,revision,deleted_at AS ordering,1 AS erased FROM memory_tombstones"
            rows = self._db.execute("SELECT * FROM (" + query +
                ") ORDER BY ordering DESC,id ASC,revision DESC LIMIT ? OFFSET ?",
                (*parameters, limit + 1, offset)).fetchall()
            items: list[MemoryItem] = []
            for row in rows[:limit]:
                item = self._get(str(row["id"]), int(row["revision"]))
                assert item is not None
                items.append(item)
            return MemoryPage(tuple(items), offset, len(rows) > limit, self._epoch())

    def _current(self, memory_id: str, expected_revision: int) -> MemoryItem:
        _text(memory_id, "Memory id", 256)
        _revision(expected_revision)
        item = self._get(memory_id)
        if item is None:
            raise MemoryNotFound("Memory was not found")
        if item.revision != expected_revision:
            raise RevisionConflict("Memory revision has changed")
        return item

    @staticmethod
    def _usable(item: MemoryItem, now: datetime) -> bool:
        draft = item.draft
        return (item.status == MemoryStatus.ACCEPTED
                and (draft.valid_from is None or draft.valid_from <= now)
                and (draft.valid_to is None or now < draft.valid_to)
                and (draft.expires_at is None or now < draft.expires_at))

    def _profile_conflict(self, source: MemorySource, draft: MemoryDraft,
                          *, now: datetime, ignored_id: str = "") -> None:
        if not draft.profile_key:
            return
        rows = self._db.execute("SELECT r.* FROM memory_revisions r JOIN memory_heads h "
                                "ON r.id=h.id AND r.revision=h.revision WHERE r.status='accepted'").fetchall()
        for row in rows:
            other = self._decode(row)
            if (other.id == ignored_id or other.source.owner_id != source.owner_id
                    or (other.draft.expires_at is not None and other.draft.expires_at <= now)
                    or other.draft.profile_key != draft.profile_key or other.draft.scope != draft.scope
                    or other.draft.value == draft.value):
                continue
            left, right = other.draft, draft
            if ((left.valid_to is None or right.valid_from is None or right.valid_from < left.valid_to)
                    and (right.valid_to is None or left.valid_from is None or left.valid_from < right.valid_to)):
                raise RevisionConflict("A conflicting accepted profile value requires explicit correction")

    def ingest(self, source: MemorySource, draft: MemoryDraft, *, accepted: bool = False,
               idempotency_key: str = "") -> MemoryItem:
        with self._lock:
            self._open()
            if type(accepted) is not bool:
                raise MemoryError("accepted must be a boolean")
            _text(idempotency_key, "Idempotency key", 256, empty=True)
            source = _normalize_source(source)
            now = self._now()
            token = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest() if idempotency_key else ""
            self._expire_candidates(now)
            with self._transaction():
                existing: MemoryItem | None = None
                original_time = now
                previous_fingerprint: str | None = None
                if token:
                    previous = self._db.execute("SELECT memory_id, fingerprint FROM memory_ingestions WHERE token=?", (token,)).fetchone()
                    if previous is not None:
                        existing = self._get(str(previous[0]))
                        if existing is None:
                            raise MemoryError("Idempotency record is inconsistent")
                        if existing.status == MemoryStatus.DELETED:
                            return existing
                        first_revision = self._get(existing.id, 1)
                        if first_revision is None:
                            raise MemoryError("Idempotency record is inconsistent")
                        original_time = first_revision.recorded_at
                        previous_fingerprint = str(previous[1])
                # A replay validates against its original commit time, so an
                # elapsed expiry cannot turn a completed command into a new one.
                normalized, default_expiry = _normalize_draft(source, draft, original_time, accepted=accepted)
                fingerprint_draft = replace(normalized, expires_at=None) if default_expiry else normalized
                fingerprint = hashlib.sha256(_json({"source": _source_data(source), "draft": _draft_data(fingerprint_draft),
                                                   "accepted": accepted}).encode("utf-8")).hexdigest()
                if existing is not None:
                    if previous_fingerprint != fingerprint:
                        raise RevisionConflict("Idempotency key was already used for a different command")
                    return existing
                if accepted:
                    self._profile_conflict(source, normalized, now=now)
                source = self._store_source(source)
                item = MemoryItem(str(uuid4()), 1, MemoryStatus.ACCEPTED if accepted else MemoryStatus.CANDIDATE,
                                  source, normalized, now, now if accepted else None)
                self._append(item, default_expiry=default_expiry)
                if token:
                    self._db.execute("INSERT INTO memory_ingestions VALUES (?, ?, ?)", (token, item.id, fingerprint))
                self._bump()
                return item

    def claim_learning(self, key: str) -> bool:
        """Consume a trusted opaque request id before evaluation, including failures.

        Callers supply task/request identifiers, never message text or its hash.
        The receipt has no content association and survives forgetting a memory.
        """
        with self._lock:
            self._open()
            _text(key, "Learning request key", 256)
            token = hashlib.sha256(key.encode("utf-8")).hexdigest()
            with self._transaction():
                cursor = self._db.execute("INSERT OR IGNORE INTO memory_learning_receipts VALUES (?, ?)",
                                          (token, _stamp(self._now())))
                return cursor.rowcount == 1

    def accept(self, memory_id: str, expected_revision: int) -> MemoryItem:
        return self._review(memory_id, expected_revision, accept=True)

    def reject(self, memory_id: str, expected_revision: int) -> MemoryItem:
        return self._review(memory_id, expected_revision, accept=False)

    def _review(self, memory_id: str, expected_revision: int, *, accept: bool) -> MemoryItem:
        with self._lock:
            self._open()
            now = self._now()
            self._expire_candidates(now)
            with self._transaction():
                item = self._current(memory_id, expected_revision)
                if item.status != MemoryStatus.CANDIDATE:
                    raise MemoryError("Only a current candidate can be reviewed")
                draft = item.draft
                if accept:
                    row = self._db.execute("SELECT default_expiry FROM memory_revisions WHERE id=? AND revision=?",
                                           (item.id, item.revision)).fetchone()
                    if row is not None and row[0]:
                        draft = replace(draft, expires_at=None)
                    draft, _ = _normalize_draft(item.source, draft, now, accepted=True)
                    self._profile_conflict(item.source, draft, now=now)
                updated = replace(item, revision=item.revision + 1,
                                  status=MemoryStatus.ACCEPTED if accept else MemoryStatus.REJECTED,
                                  draft=draft, recorded_at=now, accepted_at=now if accept else None,
                                  change_reason="user_acceptance" if accept else "user_rejection")
                self._append(updated)
                self._bump()
                return updated

    def revise(self, memory_id: str, expected_revision: int, source: MemorySource,
               draft: MemoryDraft, *, temporal: bool = False) -> MemoryItem:
        with self._lock:
            self._open()
            if type(temporal) is not bool:
                raise MemoryError("temporal must be a boolean")
            source = _normalize_source(source)
            now = self._now()
            self._expire_candidates(now)
            normalized, _ = _normalize_draft(source, draft, now, accepted=True)
            with self._transaction():
                previous = self._current(memory_id, expected_revision)
                if previous.status != MemoryStatus.ACCEPTED:
                    raise MemoryError("Only an accepted current assertion can be corrected")
                if previous.source.owner_id != source.owner_id:
                    raise MemoryError("Corrections cannot change the memory owner")
                reason = "temporal_change" if temporal else "factual_correction"
                old_draft = previous.draft
                if temporal:
                    boundary = normalized.valid_from or now
                    if (old_draft.valid_from is not None and boundary <= old_draft.valid_from) or (
                            old_draft.valid_to is not None and boundary > old_draft.valid_to):
                        raise MemoryError("Temporal correction is outside the previous validity interval")
                    old_draft = replace(old_draft, valid_to=boundary)
                    normalized = replace(normalized, valid_from=boundary)
                self._profile_conflict(source, normalized, now=now, ignored_id=memory_id)
                source = self._store_source(source)
                old = replace(previous, revision=previous.revision + 1,
                              status=MemoryStatus.ACCEPTED if temporal else MemoryStatus.SUPERSEDED,
                              draft=old_draft, recorded_at=now, change_reason=reason)
                self._append(old)
                self._db.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,))
                updated = MemoryItem(str(uuid4()), 1, MemoryStatus.ACCEPTED, source, normalized,
                                     now, now, supersedes=memory_id, change_reason=reason)
                self._append(updated)
                self._bump()
                return updated

    def _delete_ids(self, ids: set[str], now: datetime, *, shared_closure: bool = False) -> tuple[int, tuple[str, ...]]:
        source_ids: set[str] = set()
        tasks: set[str] = set()
        item_tasks: dict[str, set[str]] = {}
        epoch = self._bump()
        for memory_id in sorted(ids):
            item = self._get(memory_id)
            if item is None or item.status == MemoryStatus.DELETED:
                continue
            affected: set[str] = set()
            item_tasks[memory_id] = affected
            rows = self._db.execute("SELECT DISTINCT source_id FROM memory_revisions WHERE id=?", (memory_id,)).fetchall()
            for row in rows:
                source_ids.add(str(row[0]))
                source_row = self._db.execute("SELECT payload FROM memory_sources WHERE id=?", (row[0],)).fetchone()
                if source_row is not None:
                    source = _source_from(_object(str(source_row[0])))
                    if source.task_id is not None:
                        tasks.add(source.task_id)
                        affected.add(source.task_id)
            self._db.execute("DELETE FROM memory_heads WHERE id=?", (memory_id,))
            self._db.execute("DELETE FROM memory_revisions WHERE id=?", (memory_id,))
            self._db.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,))
            self._db.execute("DELETE FROM memory_legacy_metadata WHERE id=?", (memory_id,))
            # Retain only the opaque idempotency token and tombstone reference.
            self._db.execute("UPDATE memory_ingestions SET fingerprint=NULL WHERE memory_id=?", (memory_id,))
            self._db.execute("INSERT INTO memory_tombstones VALUES (?, ?, ?, ?)",
                             (memory_id, item.revision + 1, _stamp(now), epoch))
            if self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'").fetchone():
                self._db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        # Preserve only opaque task identifiers, never source text or content
        # fingerprints. Every member of an explicit shared-source deletion
        # retains the full closure's task scope for later idempotent receipts.
        # Unrelated candidates expiring in one sweep retain their own scopes.
        for memory_id, affected in item_tasks.items():
            self._db.executemany("INSERT OR IGNORE INTO memory_deletion_tasks VALUES (?, ?)",
                                 ((memory_id, task_id) for task_id in sorted(tasks if shared_closure else affected)))
        for source_id in sorted(source_ids):
            if not self._db.execute("SELECT 1 FROM memory_revisions WHERE source_id=?", (source_id,)).fetchone():
                self._db.execute("DELETE FROM memory_sources WHERE id=?", (source_id,))
                self._db.execute("INSERT OR IGNORE INTO memory_source_tombstones VALUES (?)", (source_id,))
        return epoch, tuple(sorted(tasks))

    def _expire_candidates(self, now: datetime) -> None:
        # Stored timestamps are normalized to UTC by ingestion. Filter expiry
        # in SQLite and materialize identifiers only; querying an epoch/page
        # must not decode every live candidate before returning its bounded page.
        rows = self._db.execute("SELECT r.id FROM memory_revisions r JOIN memory_heads h "
            "ON r.id=h.id AND r.revision=h.revision WHERE r.status='candidate' "
            "AND json_extract(r.draft,'$.expires_at') IS NOT NULL "
            "AND json_extract(r.draft,'$.expires_at')<=?", (_stamp(now),)).fetchall()
        expired = {str(row["id"]) for row in rows}
        if expired:
            with self._transaction():
                self._delete_ids(expired, now)

    def forget(self, memory_id: str, expected_revision: int) -> DeletionReceipt:
        with self._lock:
            self._open()
            now = self._now()
            self._expire_candidates(now)
            with self._transaction():
                _text(memory_id, "Memory id", 256)
                _revision(expected_revision)
                tombstone = self._db.execute("SELECT * FROM memory_tombstones WHERE id=?", (memory_id,)).fetchone()
                if tombstone is not None:
                    if expected_revision not in (int(tombstone["revision"]), int(tombstone["revision"]) - 1):
                        raise RevisionConflict("Memory revision has changed")
                    tasks = tuple(str(row[0]) for row in self._db.execute(
                        "SELECT task_id FROM memory_deletion_tasks WHERE memory_id=? ORDER BY task_id", (memory_id,)))
                    return DeletionReceipt(memory_id, int(tombstone["revision"]), int(tombstone["epoch"]), tasks)
                self._current(memory_id, expected_revision)
                ids, sources = {memory_id}, set[str]()
                # A shared source cannot be cropped safely without a span model.
                while True:
                    before = len(ids), len(sources)
                    for item_id in tuple(ids):
                        sources.update(str(row[0]) for row in self._db.execute(
                            "SELECT source_id FROM memory_revisions WHERE id=?", (item_id,)))
                    for source_id in tuple(sources):
                        ids.update(str(row[0]) for row in self._db.execute(
                            "SELECT id FROM memory_revisions WHERE source_id=?", (source_id,)))
                    if before == (len(ids), len(sources)):
                        break
                epoch, tasks = self._delete_ids(ids, now, shared_closure=True)
                deleted = self._get(memory_id)
                if deleted is None:
                    raise MemoryError("Deletion result is inconsistent")
                return DeletionReceipt(memory_id, deleted.revision, epoch, tasks)

    @staticmethod
    def _manifest(manifest: EmbeddingManifest) -> None:
        if not isinstance(manifest, EmbeddingManifest):
            raise MemoryError("manifest must be an EmbeddingManifest")
        _text(manifest.model, "Embedding model", 512)
        _text(manifest.version, "Embedding version", 512)
        if isinstance(manifest.dimensions, bool) or not isinstance(manifest.dimensions, int) or not 1 <= manifest.dimensions <= 65536:
            raise MemoryError("Embedding dimensions are invalid")

    def _check_manifest(self, manifest: EmbeddingManifest) -> None:
        row = self._db.execute("SELECT model,dimensions FROM memory_manifests WHERE version=?", (manifest.version,)).fetchone()
        if row is not None and (str(row[0]) != manifest.model or int(row[1]) != manifest.dimensions):
            raise MemoryError("Embedding version was reused for an incompatible manifest")

    def put_embedding(self, memory_id: str, revision: int, manifest: EmbeddingManifest,
                      vector: tuple[float, ...]) -> bool:
        with self._lock:
            self._open()
            _text(memory_id, "Memory id", 256)
            _revision(revision)
            self._manifest(manifest)
            if not isinstance(vector, tuple) or len(vector) != manifest.dimensions:
                raise MemoryError("Embedding vector has an invalid shape")
            numbers: list[float] = []
            for value in vector:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise MemoryError("Embedding values must be finite numbers")
                try:
                    number = float(value)
                except OverflowError:
                    raise MemoryError("Embedding values must be finite numbers") from None
                if not math.isfinite(number):
                    raise MemoryError("Embedding values must be finite numbers")
                numbers.append(number)
            if not any(numbers):
                raise MemoryError("Embedding vectors must not be zero")
            now = self._now()
            self._expire_candidates(now)
            with self._transaction():
                self._check_manifest(manifest)
                item = self._get(memory_id)
                if item is None or item.revision != revision or not self._indexable(item, now):
                    return False
                self._db.execute("INSERT OR IGNORE INTO memory_manifests VALUES (?, ?, ?)",
                                 (manifest.version, manifest.model, manifest.dimensions))
                self._db.execute("INSERT INTO memory_embeddings VALUES (?, ?, ?, ?) ON CONFLICT(memory_id,revision,version) "
                                 "DO UPDATE SET vector=excluded.vector",
                                 (memory_id, revision, manifest.version, struct.pack(f"<{len(numbers)}d", *numbers)))
                return True

    def embeddings(self, manifest: EmbeddingManifest) -> tuple[tuple[str, int, tuple[float, ...]], ...]:
        with self._lock:
            self._open()
            self._manifest(manifest)
            self._check_manifest(manifest)
            now = self._now()
            self._expire_candidates(now)
            # Only current accepted heads can confer index eligibility. Push the
            # join into SQLite instead of issuing two source/head lookups for
            # every vector; actual validity/privacy still belongs to retrieval.
            rows = self._db.execute(
                "SELECT e.memory_id,e.revision,e.vector,r.draft FROM memory_embeddings e "
                "JOIN memory_heads h ON h.id=e.memory_id AND h.revision=e.revision "
                "JOIN memory_revisions r ON r.id=h.id AND r.revision=h.revision "
                "WHERE e.version=? AND r.status='accepted' ORDER BY e.memory_id,e.revision",
                (manifest.version,),
            ).fetchall()
            result: list[tuple[str, int, tuple[float, ...]]] = []
            for row in rows:
                draft = _draft_from(_object(str(row[3])))
                if (draft.claim_type != ClaimType.LEGACY
                        and (draft.expires_at is None or now < draft.expires_at)):
                    vector = cast(tuple[float, ...], struct.unpack(f"<{manifest.dimensions}d", row[2]))
                    result.append((str(row[0]), int(row[1]), vector))
            return tuple(result)

    @staticmethod
    def _indexable(item: MemoryItem, now: datetime) -> bool:
        # A temporal historical head can support as_of retrieval. Actual time
        # eligibility belongs to the retriever; expiry/deletion still dominate.
        return (item.status == MemoryStatus.ACCEPTED and item.draft.claim_type != ClaimType.LEGACY
                and (item.draft.expires_at is None or now < item.draft.expires_at))

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def __enter__(self) -> "SQLiteMemoryRepository":
        with self._lock:
            self._open()
            return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc_value: BaseException | None, traceback: TracebackType | None) -> None:
        self.close()
