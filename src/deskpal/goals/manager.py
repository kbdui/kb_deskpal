"""Transactional objective commands and execution dependency publication."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar, cast
from uuid import uuid4

from deskpal.workflow.store import WorkflowStore

from .models import (BindingStatus, Goal, GoalConflict, GoalStatus, Milestone,
                     MilestoneStatus, SourceBinding)

_UNSET = object()
T = TypeVar("T", Goal, Milestone, SourceBinding)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must have an explicit timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def instant(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("deadline must be an offset timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if "T" not in value or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("deadline must have an explicit offset")
    return stamp(parsed)


def policy(value: str) -> str:
    if value not in ("local_only", "cloud_allowed"):
        raise ValueError("use_policy must be local_only or cloud_allowed")
    return value


def text(value: str, field: str, *, empty: bool = False, maximum: int = 8000) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field} must be text of at most {maximum} UTF-8 bytes")
    return value


def _decode(kind: str, payload: str) -> Goal | Milestone | SourceBinding:
    data = json.loads(payload)
    if kind == "goal":
        data["status"] = GoalStatus(data["status"])
        return Goal(**data)
    if kind == "milestone":
        data["status"] = MilestoneStatus(data["status"])
        return Milestone(**data)
    data["status"] = BindingStatus(data["status"])
    return SourceBinding(**data)


class GoalManager:
    def __init__(self, store: WorkflowStore, *, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS goal_objects (
                kind TEXT NOT NULL, id TEXT PRIMARY KEY, goal_id TEXT NOT NULL,
                payload TEXT NOT NULL, created_at TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS goal_scope ON goal_objects(kind,goal_id,created_at,id)")
            db.execute("""CREATE TABLE IF NOT EXISTS goal_commands (
                command_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                kind TEXT NOT NULL, result TEXT NOT NULL)""")
            db.execute("CREATE TABLE IF NOT EXISTS goal_change_token (singleton INTEGER PRIMARY KEY CHECK(singleton=1), value INTEGER NOT NULL)")
            db.execute("INSERT OR IGNORE INTO goal_change_token VALUES (1,0)")

    @property
    def change_token(self) -> int:
        """Cheap authoritative cache token, advanced in the same object transaction."""
        with self.store.transaction() as db:
            return int(db.execute("SELECT value FROM goal_change_token WHERE singleton=1").fetchone()[0])

    def _now(self) -> str:
        return stamp(self._clock())

    def _load(self, db: sqlite3.Connection, kind: str, object_id: str) -> Any:
        row = db.execute("SELECT payload FROM goal_objects WHERE kind=? AND id=?", (kind, object_id)).fetchone()
        if row is None:
            raise ValueError(f"{kind} does not exist")
        return _decode(kind, row[0])

    def _save(self, db: sqlite3.Connection, kind: str, item: T) -> T:
        goal_id = item.id if isinstance(item, Goal) else item.goal_id
        db.execute("INSERT INTO goal_objects VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                   (kind, item.id, goal_id, canonical(asdict(item)), item.created_at))
        db.execute("UPDATE goal_change_token SET value=value+1 WHERE singleton=1")
        version = item.revision if isinstance(item, SourceBinding) else item.execution_version
        active = (item.status == BindingStatus.ACTIVE if isinstance(item, SourceBinding) else
                  item.status == MilestoneStatus.PENDING if isinstance(item, Milestone) else
                  item.status == GoalStatus.ACTIVE)
        use_policy = item.use_policy if isinstance(item, (Goal, SourceBinding)) else "cloud_allowed"
        self.store.set_dependency_version(kind, item.id, version, active=active, use_policy=use_policy)
        self.store.record_event(f"{kind}_changed", data={"id": item.id, "revision": item.revision,
                                                       "execution_version": version, "status": item.status.value})
        return item

    def _command(self, command_id: str | None, operation: str, payload: Any,
                 action: Callable[[sqlite3.Connection], T]) -> T:
        key = command_id or str(uuid4())
        text(key, "command_id", maximum=200)
        fingerprint = hashlib.sha256(canonical([operation, payload]).encode()).hexdigest()
        with self.store.transaction() as db:
            old = db.execute("SELECT fingerprint,kind,result FROM goal_commands WHERE command_id=?", (key,)).fetchone()
            if old is not None:
                if old[0] != fingerprint:
                    raise GoalConflict("command_id was already used for different arguments")
                return _decode(old[1], old[2])  # type: ignore[return-value]
            result = action(db)
            kind = "goal" if isinstance(result, Goal) else "milestone" if isinstance(result, Milestone) else "binding"
            db.execute("INSERT INTO goal_commands VALUES (?,?,?,?)", (key, fingerprint, kind, canonical(asdict(result))))
            return result

    @staticmethod
    def _cas(item: Goal | Milestone | SourceBinding, expected_revision: int) -> None:
        if type(expected_revision) is not int or item.revision != expected_revision:
            raise GoalConflict("revision conflict; read the current object before changing it")

    @staticmethod
    def _editable(goal: Goal) -> None:
        if goal.status in (GoalStatus.COMPLETED, GoalStatus.CANCELLED):
            raise GoalConflict("terminal goals cannot be revised")

    def create(self, title: str, success_criteria: str, *, deadline: str | None = None,
               use_policy: str = "local_only", command_id: str | None = None) -> Goal:
        text(title, "title", maximum=500)
        text(success_criteria, "success_criteria")
        policy(use_policy)
        deadline = None if deadline is None else instant(deadline)
        def action(db: sqlite3.Connection) -> Goal:
            now = self._now()
            return self._save(db, "goal", Goal(str(uuid4()), title, success_criteria, deadline, use_policy,
                GoalStatus.ACTIVE, 1, 1, "", "", now, now))
        return self._command(command_id, "create", [title, success_criteria, deadline, use_policy], action)

    def get(self, goal_id: str) -> Goal:
        with self.store.transaction() as db:
            return self._load(db, "goal", goal_id)  # type: ignore[no-any-return]

    def list(self, *, status: GoalStatus | str | None = None, limit: int = 100,
             offset: int = 0) -> tuple[Goal, ...]:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("limit must be 1..100 and offset non-negative")
        with self.store.transaction() as db:
            # SQL JSON extraction avoids unbounded materialization for status pages.
            clause = "" if status is None else " AND json_extract(payload,'$.status')=?"
            params: tuple[Any, ...] = () if status is None else (GoalStatus(status).value,)
            return tuple(cast(Goal, _decode("goal", row[0])) for row in db.execute(
                "SELECT payload FROM goal_objects WHERE kind='goal'" + clause +
                " ORDER BY created_at,id LIMIT ? OFFSET ?", (*params, limit, offset)))

    def revise(self, goal_id: str, expected_revision: int, *, title: Any = _UNSET,
               success_criteria: Any = _UNSET, deadline: Any = _UNSET, progress_note: Any = _UNSET,
               use_policy: Any = _UNSET, command_id: str | None = None) -> Goal:
        fields = {k: v for k, v in locals().copy().items() if k in
                  ("title", "success_criteria", "deadline", "progress_note", "use_policy") and v is not _UNSET}
        if not fields:
            raise ValueError("at least one changed field is required")
        for key in ("title", "success_criteria", "progress_note"):
            if key in fields:
                text(fields[key], key, empty=key == "progress_note", maximum=500 if key == "title" else 8000)
        if "deadline" in fields and fields["deadline"] is not None:
            fields["deadline"] = instant(fields["deadline"])
        if "use_policy" in fields:
            policy(fields["use_policy"])
        def action(db: sqlite3.Connection) -> Goal:
            old: Goal = self._load(db, "goal", goal_id)
            self._cas(old, expected_revision)
            self._editable(old)
            execution_changed = any(k != "progress_note" and getattr(old, k) != v for k, v in fields.items())
            return self._save(db, "goal", replace(old, **fields, revision=old.revision + 1,
                execution_version=old.execution_version + int(execution_changed), updated_at=self._now()))
        return self._command(command_id, "revise", [goal_id, expected_revision, fields], action)

    def set_status(self, goal_id: str, expected_revision: int, status: GoalStatus | str, *,
                   evidence: str = "", command_id: str | None = None) -> Goal:
        status = GoalStatus(status)
        text(evidence, "evidence", empty=status != GoalStatus.COMPLETED)
        def action(db: sqlite3.Connection) -> Goal:
            old: Goal = self._load(db, "goal", goal_id)
            self._cas(old, expected_revision)
            self._editable(old)
            return self._save(db, "goal", replace(old, status=status, revision=old.revision + 1,
                execution_version=old.execution_version + int(status != old.status),
                completion_evidence=evidence if status == GoalStatus.COMPLETED else old.completion_evidence,
                updated_at=self._now()))
        return self._command(command_id, "set_status", [goal_id, expected_revision, status, evidence], action)

    def add_milestone(self, goal_id: str, expected_revision: int, title: str, success_criteria: str, *,
                      deadline: str | None = None, command_id: str | None = None) -> Milestone:
        text(title, "title", maximum=500)
        text(success_criteria, "success_criteria")
        deadline = None if deadline is None else instant(deadline)
        def action(db: sqlite3.Connection) -> Milestone:
            goal: Goal = self._load(db, "goal", goal_id)
            self._cas(goal, expected_revision)
            self._editable(goal)
            now = self._now()
            self._save(db, "goal", replace(goal, revision=goal.revision + 1, updated_at=now))
            return self._save(db, "milestone", Milestone(str(uuid4()), goal_id, title, success_criteria,
                deadline, MilestoneStatus.PENDING, 1, 1, "", "", now, now))
        return self._command(command_id, "add_milestone", [goal_id, expected_revision, title, success_criteria, deadline], action)

    def get_milestone(self, milestone_id: str) -> Milestone:
        with self.store.transaction() as db:
            return self._load(db, "milestone", milestone_id)  # type: ignore[no-any-return]

    def _children(self, kind: str, goal_id: str, limit: int, offset: int) -> tuple[Any, ...]:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("limit must be 1..100 and offset non-negative")
        with self.store.transaction() as db:
            return tuple(_decode(kind, row[0]) for row in db.execute(
                "SELECT payload FROM goal_objects WHERE kind=? AND goal_id=? ORDER BY created_at,id LIMIT ? OFFSET ?",
                (kind, goal_id, limit, offset)))

    def list_milestones(self, goal_id: str, *, limit: int = 100, offset: int = 0) -> tuple[Milestone, ...]:
        return self._children("milestone", goal_id, limit, offset)

    def list_bindings(self, goal_id: str, *, limit: int = 100, offset: int = 0) -> tuple[SourceBinding, ...]:
        return self._children("binding", goal_id, limit, offset)

    def revise_milestone(self, milestone_id: str, expected_revision: int, *, title: Any = _UNSET,
                         success_criteria: Any = _UNSET, deadline: Any = _UNSET,
                         progress_note: Any = _UNSET, command_id: str | None = None) -> Milestone:
        fields = {k: v for k, v in locals().copy().items() if k in
                  ("title", "success_criteria", "deadline", "progress_note") and v is not _UNSET}
        if not fields:
            raise ValueError("at least one changed field is required")
        for key in ("title", "success_criteria", "progress_note"):
            if key in fields:
                text(fields[key], key, empty=key == "progress_note")
        if "deadline" in fields and fields["deadline"] is not None:
            fields["deadline"] = instant(fields["deadline"])
        def action(db: sqlite3.Connection) -> Milestone:
            old: Milestone = self._load(db, "milestone", milestone_id)
            self._cas(old, expected_revision)
            self._editable(self._load(db, "goal", old.goal_id))
            if old.status != MilestoneStatus.PENDING:
                raise GoalConflict("terminal milestones cannot be revised")
            changed = any(k != "progress_note" and getattr(old, k) != v for k, v in fields.items())
            return self._save(db, "milestone", replace(old, **fields, revision=old.revision + 1,
                execution_version=old.execution_version + int(changed), updated_at=self._now()))
        return self._command(command_id, "revise_milestone", [milestone_id, expected_revision, fields], action)

    def set_milestone_status(self, milestone_id: str, expected_revision: int, status: MilestoneStatus | str, *,
                             evidence: str = "", command_id: str | None = None) -> Milestone:
        status = MilestoneStatus(status)
        text(evidence, "evidence", empty=status != MilestoneStatus.COMPLETED)
        def action(db: sqlite3.Connection) -> Milestone:
            old: Milestone = self._load(db, "milestone", milestone_id)
            self._cas(old, expected_revision)
            self._editable(self._load(db, "goal", old.goal_id))
            if old.status != MilestoneStatus.PENDING or status == MilestoneStatus.PENDING:
                raise GoalConflict("milestone terminal transitions require a pending milestone")
            return self._save(db, "milestone", replace(old, status=status, revision=old.revision + 1,
                execution_version=old.execution_version + 1, completion_evidence=evidence,
                updated_at=self._now()))
        return self._command(command_id, "milestone_status", [milestone_id, expected_revision, status, evidence], action)

    def complete_milestone(self, milestone_id: str, expected_revision: int, *, evidence: str,
                           command_id: str | None = None) -> Milestone:
        return self.set_milestone_status(milestone_id, expected_revision, MilestoneStatus.COMPLETED,
                                        evidence=evidence, command_id=command_id)

    def bind_source(self, goal_id: str, expected_revision: int, source: str, source_id: str, *,
                    milestone_id: str | None = None, use_policy: str = "local_only",
                    command_id: str | None = None) -> SourceBinding:
        if source not in ("calendar", "timer", "task"):
            raise ValueError("only explicitly bound calendar, timer and task sources are supported")
        text(source_id, "source_id", maximum=200)
        policy(use_policy)
        def action(db: sqlite3.Connection) -> SourceBinding:
            goal: Goal = self._load(db, "goal", goal_id)
            self._cas(goal, expected_revision)
            self._editable(goal)
            if milestone_id is not None:
                milestone: Milestone = self._load(db, "milestone", milestone_id)
                if milestone.goal_id != goal_id or milestone.status != MilestoneStatus.PENDING:
                    raise ValueError("binding milestone must be pending and belong to the goal")
            now = self._now()
            self._save(db, "goal", replace(goal, revision=goal.revision + 1, updated_at=now))
            return self._save(db, "binding", SourceBinding(str(uuid4()), goal_id, milestone_id, source,
                source_id, 1, BindingStatus.ACTIVE, use_policy, now, now))
        return self._command(command_id, "bind_source", [goal_id, expected_revision, source, source_id,
                                                       milestone_id, use_policy], action)

    def get_binding(self, binding_id: str) -> SourceBinding:
        with self.store.transaction() as db:
            return self._load(db, "binding", binding_id)  # type: ignore[no-any-return]

    def revise_binding(self, binding_id: str, expected_revision: int, *, use_policy: str | None = None,
                       status: BindingStatus | str | None = None, command_id: str | None = None) -> SourceBinding:
        if use_policy is None and status is None:
            raise ValueError("binding change is required")
        if use_policy is not None:
            policy(use_policy)
        chosen_status = None if status is None else BindingStatus(status)
        def action(db: sqlite3.Connection) -> SourceBinding:
            old: SourceBinding = self._load(db, "binding", binding_id)
            self._cas(old, expected_revision)
            self._editable(self._load(db, "goal", old.goal_id))
            if old.status == BindingStatus.REMOVED:
                raise GoalConflict("removed binding cannot be revised")
            return self._save(db, "binding", replace(old, revision=old.revision + 1,
                use_policy=old.use_policy if use_policy is None else use_policy,
                status=old.status if chosen_status is None else chosen_status, updated_at=self._now()))
        return self._command(command_id, "revise_binding", [binding_id, expected_revision, use_policy, chosen_status], action)

    def remove_binding(self, binding_id: str, expected_revision: int, *,
                       command_id: str | None = None) -> SourceBinding:
        return self.revise_binding(binding_id, expected_revision, status=BindingStatus.REMOVED, command_id=command_id)
