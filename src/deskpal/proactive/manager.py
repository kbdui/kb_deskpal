"""Suggestion persistence and atomic, bounded submission on explicit acceptance."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

from deskpal.goals import BindingStatus, GoalManager, GoalStatus, MilestoneStatus, SourceBinding
from deskpal.goals.manager import canonical, instant, stamp, text
from deskpal.scheduling.ports import SchedulingManager
from deskpal.scheduling.models import SourceSnapshot
from deskpal.tasks.models import Task
from deskpal.workflow.models import DependencyRef, WorkflowBlocked
from deskpal.workflow.store import WorkflowStore
from deskpal.workflow.tasks import PersistentTaskManager

from .models import Proposal, ProposalConflict, ProposalStatus


def decode(payload: str) -> Proposal:
    value = json.loads(payload)
    value["status"] = ProposalStatus(value["status"])
    return Proposal(**value)


class ProposalManager:
    def __init__(self, store: WorkflowStore, goals: GoalManager, tasks: PersistentTaskManager,
                 scheduling: SchedulingManager | None = None, *,
                 clock: Callable[[], datetime] | None = None) -> None:
        if goals.store is not store or tasks.store is not store:
            raise ValueError("Goal, Proposal and Task managers must share the same workflow store")
        self.store, self.goals, self.tasks, self.scheduling = store, goals, tasks, scheduling
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._refresh_position = 0
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY, business_key TEXT NOT NULL UNIQUE, goal_id TEXT NOT NULL,
                accepted_task_id TEXT UNIQUE, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending', expires_at TEXT NOT NULL DEFAULT '')""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(proposals)")}
            if "status" not in columns:
                db.execute("ALTER TABLE proposals ADD COLUMN status TEXT NOT NULL DEFAULT 'Pending'")
                db.execute("ALTER TABLE proposals ADD COLUMN expires_at TEXT NOT NULL DEFAULT ''")
                db.execute("UPDATE proposals SET status=json_extract(payload,'$.status'),expires_at=json_extract(payload,'$.expires_at')")
            db.execute("CREATE INDEX IF NOT EXISTS proposal_order ON proposals(created_at,id)")
            db.execute("CREATE INDEX IF NOT EXISTS proposal_status_order ON proposals(status,created_at,id)")
            db.execute("CREATE INDEX IF NOT EXISTS proposal_expiry ON proposals(status,expires_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS proposal_commands (
                command_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                proposal_id TEXT NOT NULL, task_id TEXT, result TEXT NOT NULL)""")

    def _now(self) -> datetime:
        # stamp performs awareness validation before comparisons as well.
        return datetime.fromisoformat(stamp(self._clock()))

    def _save(self, db: sqlite3.Connection, item: Proposal) -> Proposal:
        db.execute("UPDATE proposals SET payload=?,accepted_task_id=?,status=?,expires_at=? WHERE id=?",
                   (canonical(asdict(item)), item.accepted_task_id, item.status.value, item.expires_at, item.id))
        self.store.record_event("proposal_changed", item.accepted_task_id,
                                {"id": item.id, "status": item.status.value, "revision": item.revision})
        return item

    def get(self, proposal_id: str) -> Proposal:
        with self.store.transaction() as db:
            row = db.execute("SELECT payload FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise ValueError("proposal does not exist")
            return decode(row[0])

    def list(self, *, status: ProposalStatus | str | None = ProposalStatus.PENDING,
             limit: int = 100, offset: int = 0) -> tuple[Proposal, ...]:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("limit must be 1..100 and offset non-negative")
        with self.store.transaction() as db:
            where = "" if status is None else " WHERE status=?"
            params: tuple[str, ...] = () if status is None else (ProposalStatus(status).value,)
            selected = tuple(decode(row[0]) for row in db.execute("SELECT payload FROM proposals" + where +
                " ORDER BY created_at,id LIMIT ? OFFSET ?", (*params, limit, offset)))
        result = []
        for item in selected:
            if item.status == ProposalStatus.PENDING:
                try:
                    item = self.preflight(item.id)
                except ProposalConflict:
                    item = self.get(item.id)
            if status is None or item.status == ProposalStatus(status):
                result.append(item)
        return tuple(result)

    def _local_valid(self, item: Proposal) -> bool:
        try:
            goal = self.goals.get(item.goal_id)
            if goal.status != GoalStatus.ACTIVE or goal.execution_version != item.goal_execution_version:
                return False
            if item.milestone_id is not None:
                milestone = self.goals.get_milestone(item.milestone_id)
                if milestone.status != MilestoneStatus.PENDING or milestone.execution_version != item.milestone_execution_version:
                    return False
            if item.binding_id is not None:
                binding = self.goals.get_binding(item.binding_id)
                if binding.status != BindingStatus.ACTIVE or binding.revision != item.binding_revision:
                    return False
            return True
        except ValueError:
            return False

    def _source_valid(self, item: Proposal) -> bool:
        if item.source_snapshot_json is None:
            return True
        expected = json.loads(item.source_snapshot_json)
        if expected["source"] == "task":
            task = self.tasks.get(expected["source_id"])
            return (task is not None and task.status.value == expected["status"] and
                    task.revision == expected["revision"] and
                    self.tasks.task_use_policy(task.id) == expected["use_policy"])
        if self.scheduling is None:
            return False
        current = self.scheduling.source_snapshot(expected["source"], expected["source_id"])
        return current is not None and asdict(current) == expected

    def preflight(self, proposal_id: str) -> Proposal:
        """Cross-store read before acceptance; this is deliberately not atomic."""
        item = self.get(proposal_id)
        if item.status == ProposalStatus.ACCEPTED:
            return item
        expired = self._now() >= datetime.fromisoformat(item.expires_at)
        valid = self._source_valid(item) and self._local_valid(item)
        if item.status == ProposalStatus.PENDING and (expired or not valid):
            with self.store.transaction() as db:
                latest = self.get(proposal_id)
                if latest.status == ProposalStatus.PENDING and latest.revision == item.revision:
                    self._save(db, replace(latest, status=ProposalStatus.EXPIRED if expired else ProposalStatus.INVALIDATED,
                        revision=latest.revision + 1, updated_at=stamp(self._now())))
            raise ProposalConflict("proposal expired or its current source/goal changed")
        if item.status != ProposalStatus.PENDING:
            raise ProposalConflict("proposal is no longer pending")
        return item

    def refresh(self, *, limit: int = 100) -> None:
        # Only references and local source reads; no cloud provider is reachable.
        with self.store.transaction() as db:
            # Expiry is indexed; ordinary validation advances a bounded sweep.
            expired = tuple(row[0] for row in db.execute(
                "SELECT id FROM proposals WHERE status='Pending' AND expires_at<=? ORDER BY expires_at LIMIT ?",
                (stamp(self._now()), limit)))
            rows = db.execute("SELECT rowid,id FROM proposals WHERE status='Pending' AND rowid>? ORDER BY rowid LIMIT ?",
                              (self._refresh_position, limit)).fetchall()
            self._refresh_position = int(rows[-1][0]) if rows else 0
            ids = tuple(dict.fromkeys((*expired, *(row[1] for row in rows))))
        for proposal_id in ids:
            try:
                self.preflight(proposal_id)
            except ProposalConflict:
                pass

    def create(self, goal_id: str, *, rule_id: str, root_source_key: str, kind: str,
               due_at: str, ttl_seconds: float = 86400, milestone_id: str | None = None,
               binding_id: str | None = None, source_snapshot: SourceSnapshot | dict[str, object] | None = None,
               reason: str = "") -> Proposal | None:
        """Called by deterministic local rules; uniqueness is independent of rule releases."""
        due = datetime.fromisoformat(instant(due_at))
        if type(ttl_seconds) not in (int, float) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        expires = due + timedelta(seconds=ttl_seconds)
        if self._now() >= expires:
            return None
        with self.store.transaction() as db:
            goal = self.goals.get(goal_id)
            if goal.status != GoalStatus.ACTIVE:
                return None
            milestone = None if milestone_id is None else self.goals.get_milestone(milestone_id)
            if milestone is not None and (milestone.goal_id != goal_id or milestone.status != MilestoneStatus.PENDING):
                return None
            binding: SourceBinding | None = None if binding_id is None else self.goals.get_binding(binding_id)
            if binding is not None and (binding.status != BindingStatus.ACTIVE or binding.goal_id != goal_id or
                                        binding.milestone_id != milestone_id):
                return None
            use_policy = "local_only" if goal.use_policy == "local_only" or (
                binding is not None and binding.use_policy == "local_only") else "cloud_allowed"
            if binding is not None and binding.source == "task" and self.tasks.task_use_policy(binding.source_id) == "local_only":
                use_policy = "local_only"
            key = canonical([goal.id, goal.execution_version,
                None if milestone is None else [milestone.id, milestone.execution_version],
                None if binding is None else [binding.id, binding.revision], rule_id, root_source_key, kind])
            old = db.execute("SELECT payload FROM proposals WHERE business_key=?", (key,)).fetchone()
            if old is not None:
                return decode(old[0])
            now = stamp(self._now())
            subject = goal.title if milestone is None else milestone.title
            criteria = goal.success_criteria if milestone is None else milestone.success_criteria
            request = f"Review progress for {subject}. Completion criteria: {criteria}. {reason}".strip()
            text(request, "proposal request", maximum=self.store.request_max_bytes)
            item = Proposal(str(uuid4()), 1, ProposalStatus.PENDING, goal.id, goal.execution_version,
                milestone_id, None if milestone is None else milestone.execution_version,
                binding_id, None if binding is None else binding.revision, rule_id, root_source_key, kind,
                reason, request, stamp(due), stamp(expires), use_policy,
                None if source_snapshot is None else canonical(source_snapshot if isinstance(source_snapshot, dict)
                                                               else asdict(source_snapshot)), None, now, now)
            db.execute("INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?)",
                (item.id, key, goal.id, None, canonical(asdict(item)), now, item.status.value, item.expires_at))
            self.store.record_event("proposal_created", data={"id": item.id, "goal_id": goal.id, "kind": kind})
            return item

    def accept(self, proposal_id: str, expected_revision: int, *, command_id: str) -> Task:
        text(command_id, "command_id", maximum=200)
        fingerprint = hashlib.sha256(canonical(["accept", proposal_id, expected_revision]).encode()).hexdigest()
        # Resolve completed command retries before cross-store source/TTL checks.
        with self.store.transaction() as db:
            old = db.execute("SELECT fingerprint,task_id FROM proposal_commands WHERE command_id=?", (command_id,)).fetchone()
            if old is not None:
                if old[0] != fingerprint or old[1] is None:
                    raise ProposalConflict("command_id was used with different arguments")
                task = self.tasks.get(old[1])
                assert task is not None
                return task
        checked = self.preflight(proposal_id)
        with self.store.transaction() as db:
            # Repeat receipt check handles concurrent double clicks.
            old = db.execute("SELECT fingerprint,task_id FROM proposal_commands WHERE command_id=?", (command_id,)).fetchone()
            if old is not None:
                if old[0] != fingerprint or old[1] is None:
                    raise ProposalConflict("command_id was used with different arguments")
                task = self.tasks.get(old[1])
                assert task is not None
                return task
            item = self.get(proposal_id)
            if item.status == ProposalStatus.ACCEPTED:
                task = self.tasks.get(item.accepted_task_id or "")
                assert task is not None
            else:
                if (item.status != ProposalStatus.PENDING or item.revision != expected_revision or
                    item.revision != checked.revision or self._now() >= datetime.fromisoformat(item.expires_at) or
                    not self._local_valid(item)):
                    raise ProposalConflict("stale proposal; acceptance requires a current pending revision")
                task = self.tasks.submit(item.request, command_id=f"proposal:{item.id}",
                                         dependencies=item.dependencies, use_policy=item.use_policy)
                item = self._save(db, replace(item, status=ProposalStatus.ACCEPTED, revision=item.revision + 1,
                    accepted_task_id=task.id, updated_at=stamp(self._now())))
            db.execute("INSERT INTO proposal_commands VALUES (?,?,?,?,?)",
                       (command_id, fingerprint, proposal_id, task.id, canonical(asdict(item))))
            return task

    def ignore(self, proposal_id: str, expected_revision: int, *, command_id: str | None = None) -> Proposal:
        key = command_id or str(uuid4())
        fingerprint = hashlib.sha256(canonical(["ignore", proposal_id, expected_revision]).encode()).hexdigest()
        with self.store.transaction() as db:
            old = db.execute("SELECT fingerprint,result FROM proposal_commands WHERE command_id=?", (key,)).fetchone()
            if old is not None:
                if old[0] != fingerprint:
                    raise ProposalConflict("command_id was used with different arguments")
                return decode(old[1])
            item = self.get(proposal_id)
            if item.status != ProposalStatus.PENDING or item.revision != expected_revision:
                raise ProposalConflict("ignore requires the current pending revision")
            result = self._save(db, replace(item, status=ProposalStatus.IGNORED, revision=item.revision + 1,
                                           updated_at=stamp(self._now())))
            db.execute("INSERT INTO proposal_commands VALUES (?,?,?,?,?)",
                       (key, fingerprint, proposal_id, None, canonical(asdict(result))))
            return result

    def validate_task(self, task_id: str, refs: tuple[DependencyRef, ...] = ()) -> None:
        """Call on dequeue and before each step, outside the workflow transaction."""
        with self.store.transaction() as db:
            row = db.execute("SELECT payload FROM proposals WHERE accepted_task_id=?", (task_id,)).fetchone()
        if row is None:
            return
        item = decode(row[0])
        if (self._now() >= datetime.fromisoformat(item.expires_at) or not self._local_valid(item) or
            not self._source_valid(item)):
            raise WorkflowBlocked("accepted proposal expired or a goal/source dependency changed")
