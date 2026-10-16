"""Bounded deterministic rules with durable reception and scope continuations."""

import json
import heapq
import hashlib
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol
from threading import RLock
from uuid import uuid4
from zoneinfo import ZoneInfo

from deskpal.goals import BindingStatus, GoalManager, GoalStatus, MilestoneStatus
from deskpal.goals.manager import canonical, stamp
from deskpal.scheduling.models import SourceSnapshot
from deskpal.scheduling.ports import SchedulingManager
from deskpal.tasks.models import TaskStatus
from deskpal.workflow.store import WorkflowStore

from .manager import ProposalManager
from .models import Proposal


class RuleConfig(Protocol):
    @property
    def enabled(self) -> bool: ...
    @property
    def event_batch_size(self) -> int: ...
    @property
    def max_proposals_per_poll(self) -> int: ...
    @property
    def proposal_ttl_seconds(self) -> int: ...
    @property
    def quiet_start(self) -> str: ...
    @property
    def quiet_end(self) -> str: ...
    @property
    def timezone(self) -> str: ...


class LocalRuleEngine:
    """No threads, model provider, screen capture or filesystem scan.

    Application calls poll; reception cursors acknowledge inbox durability only.
    Each inbox's scope offset commits with its proposals, never with reception.
    """

    def __init__(self, store: WorkflowStore, goals: GoalManager, proposals: ProposalManager,
                 scheduling: SchedulingManager, config: RuleConfig, *,
                 clock: Callable[[], datetime] | None = None) -> None:
        if goals.store is not store or proposals.store is not store:
            raise ValueError("rules must share the workflow store")
        self.store, self.goals, self.proposals = store, goals, proposals
        self.scheduling, self.config = scheduling, config
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()
        self._zone = ZoneInfo(config.timezone)
        self._quiet_start = self._minute(config.quiet_start)
        self._quiet_end = self._minute(config.quiet_end)
        self._cache_token = -1
        self._goal_cache: tuple[Any, ...] = ()
        self._binding_cache: tuple[tuple[Any, Any, Any], ...] = ()
        self._deadline_heap: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        if not 1 <= config.event_batch_size <= 100 or not 1 <= config.max_proposals_per_poll <= 10:
            raise ValueError("event batches must be 1..100 and proposal budgets 1..10")
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS proactive_cursor (
                stream TEXT PRIMARY KEY, store_id TEXT NOT NULL, generation TEXT NOT NULL,
                received_id INTEGER NOT NULL, anchor TEXT NOT NULL, status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '')""")
            db.execute("""CREATE TABLE IF NOT EXISTS proactive_inbox (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, delivery_key TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL, scopes TEXT NOT NULL, scope_offset INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'Pending', outcomes TEXT NOT NULL DEFAULT '[]')""")
            db.execute("CREATE INDEX IF NOT EXISTS proactive_pending ON proactive_inbox(status,sequence)")
            db.execute("""CREATE TABLE IF NOT EXISTS proactive_review_budget (
                stream TEXT PRIMARY KEY, end_id INTEGER NOT NULL, remaining INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS proactive_review_commands (
                command_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL)""")

    @staticmethod
    def _minute(value: str) -> int:
        try:
            hour, minute = value.split(":")
            if len(hour) != 2 or len(minute) != 2 or not 0 <= int(hour) <= 23 or not 0 <= int(minute) <= 59:
                raise ValueError
            return int(hour) * 60 + int(minute)
        except (ValueError, AttributeError):
            raise ValueError("quiet times must use HH:MM") from None

    def _now(self) -> datetime:
        return datetime.fromisoformat(stamp(self._clock()))

    def is_quiet(self, now: datetime | None = None) -> bool:
        local = (now or self._now()).astimezone(self._zone)
        minute = local.hour * 60 + local.minute
        start, end = self._quiet_start, self._quiet_end
        if start == end:
            return False
        return start <= minute < end if start < end else minute >= start or minute < end

    def summary(self, *, limit: int = 10) -> tuple[Proposal, ...]:
        """Only call at a command boundary; never interrupt user input."""
        if not self.config.enabled or self.is_quiet():
            return ()
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("summary limit must be 1..10")
        return self.proposals.list(limit=limit)

    def consumer_status(self) -> tuple[dict[str, Any], ...]:
        with self.store.transaction() as db:
            return tuple(dict(row) for row in db.execute("SELECT * FROM proactive_cursor ORDER BY stream"))

    def inbox_status(self) -> tuple[dict[str, Any], ...]:
        with self.store.transaction() as db:
            return tuple(dict(row) for row in db.execute(
                "SELECT sequence,delivery_key,scope_offset,status,outcomes FROM proactive_inbox ORDER BY sequence"))

    def review_consumer(self, stream: str, *, expected_store_id: str, expected_generation: str,
                        strategy: str = "tail", after_id: int | None = None,
                        max_events: int | None = None, command_id: str | None = None) -> dict[str, Any]:
        """Explicitly approve a source replacement/restore; never called by poll.

        Bounded history acknowledges at most max_events existing events after
        after_id, then tails the reviewed boundary. Fresh later events remain
        eligible normally. Existing delivery/business keys still prevent replay.
        """
        if stream not in ("timer", "calendar") or strategy not in ("tail", "bounded_history"):
            raise ValueError("review requires timer/calendar and tail/bounded_history")
        if strategy == "bounded_history":
            if type(after_id) is not int or after_id < 0 or type(max_events) is not int or not 1 <= max_events <= 10000:
                raise ValueError("bounded_history requires non-negative after_id and max_events in 1..10000")
        elif after_id is not None or max_events is not None:
            raise ValueError("tail strategy does not accept history bounds")
        key = command_id or str(uuid4())
        fingerprint = hashlib.sha256(canonical([stream, expected_store_id, expected_generation,
                                                strategy, after_id, max_events]).encode()).hexdigest()
        with self._lock:
            with self.store.transaction() as db:
                old = db.execute("SELECT fingerprint,result FROM proactive_review_commands WHERE command_id=?", (key,)).fetchone()
            if old is not None:
                if old[0] != fingerprint:
                    raise ValueError("review command_id was used with different arguments")
                return json.loads(old[1])  # type: ignore[no-any-return]
            identity = self.scheduling.store_identity
            if identity.store_id != expected_store_id or identity.generation != expected_generation:
                raise ValueError("source identity changed; inspect and explicitly review the current identity")
            tail = self.scheduling.event_cursor_state(stream)
            chosen = tail.tail_id if strategy == "tail" else int(after_id or 0)
            if chosen > tail.tail_id:
                raise ValueError("history after_id is beyond the current source tail")
            anchor = self.scheduling.event_anchor(stream, chosen)
            if anchor is None:
                raise ValueError("history after_id must identify an event of the selected stream, or zero")
            result = {"stream": stream, "store_id": identity.store_id, "generation": identity.generation,
                      "received_id": chosen, "anchor": anchor, "status": "Active", "error": "",
                      "strategy": strategy, "history_end_id": tail.tail_id, "max_events": max_events}
            with self.store.transaction() as db:
                db.execute("INSERT INTO proactive_cursor VALUES (?,?,?,?,?,'Active','') ON CONFLICT(stream) DO UPDATE SET store_id=excluded.store_id,generation=excluded.generation,received_id=excluded.received_id,anchor=excluded.anchor,status='Active',error=''",
                           (stream, identity.store_id, identity.generation, chosen, anchor))
                # Review terminates old source inbox work rather than silently
                # carrying scopes from an untrusted consumer generation forward.
                db.execute("UPDATE proactive_inbox SET status='Done',outcomes=? WHERE status='Pending' AND json_extract(payload,'$.event.source')=?",
                           (canonical(["consumer_explicitly_reanchored"]), stream))
                db.execute("DELETE FROM proactive_review_budget WHERE stream=?", (stream,))
                if strategy == "bounded_history":
                    db.execute("INSERT INTO proactive_review_budget VALUES (?,?,?)", (stream, tail.tail_id, max_events))
                self.store.record_event("proactive_consumer_reviewed", data=result)
                db.execute("INSERT INTO proactive_review_commands VALUES (?,?,?)", (key, fingerprint, canonical(result)))
            return result

    def _all_goals(self) -> tuple[Any, ...]:
        if self._cache_token == self.goals.change_token:
            return self._goal_cache
        result: list[Any] = []
        offset = 0
        while True:
            page = self.goals.list(limit=100, offset=offset)
            result.extend(page)
            if len(page) < 100:
                return tuple(result)
            offset += 100

    def _scope(self, goal: Any, milestone: Any = None, binding: Any = None) -> dict[str, Any]:
        return {"goal_id": goal.id, "goal_version": goal.execution_version,
                "milestone_id": None if milestone is None else milestone.id,
                "milestone_version": None if milestone is None else milestone.execution_version,
                "binding_id": None if binding is None else binding.id,
                "binding_revision": None if binding is None else binding.revision}

    def _bindings(self) -> tuple[tuple[Any, Any, Any], ...]:
        token = self.goals.change_token
        if token == self._cache_token:
            return self._binding_cache
        result = []
        goals = self._all_goals()
        for goal in goals:
            if goal.status != GoalStatus.ACTIVE:
                continue
            offset = 0
            while True:
                page = self.goals.list_bindings(goal.id, limit=100, offset=offset)
                for binding in page:
                    if binding.status != BindingStatus.ACTIVE:
                        continue
                    milestone = None if binding.milestone_id is None else self.goals.get_milestone(binding.milestone_id)
                    if milestone is None or milestone.status == MilestoneStatus.PENDING:
                        result.append((goal, milestone, binding))
                if len(page) < 100:
                    break
                offset += 100
        self._binding_cache = tuple(result)
        self._goal_cache = goals
        self._cache_token = token
        self._build_deadlines()
        return self._binding_cache

    def _build_deadlines(self) -> None:
        self._deadline_heap = []
        for goal in self._goal_cache:
            if goal.status != GoalStatus.ACTIVE:
                continue
            scopes = [(goal.deadline, self._scope(goal), f"goal:{goal.id}:{goal.execution_version}")]
            offset = 0
            while True:
                page = self.goals.list_milestones(goal.id, limit=100, offset=offset)
                scopes.extend((milestone.deadline, self._scope(goal, milestone),
                               f"goal:{goal.id}:{goal.execution_version}:milestone:{milestone.id}:{milestone.execution_version}")
                              for milestone in page if milestone.status == MilestoneStatus.PENDING)
                if len(page) < 100:
                    break
                offset += 100
            for deadline, scope, root in scopes:
                if deadline is not None:
                    trigger = stamp(datetime.fromisoformat(deadline) - timedelta(days=1))
                    payload = {"kind": "deadline_window", "rule_id": "goal_deadline_window",
                               "root_source_key": root, "due_at": deadline, "snapshot": None}
                    heapq.heappush(self._deadline_heap, (trigger, f"deadline:{root}", payload, scope))

    def _enqueue(self, db: sqlite3.Connection, key: str, payload: dict[str, Any], scopes: list[dict[str, Any]]) -> None:
        cursor = db.execute("INSERT OR IGNORE INTO proactive_inbox(delivery_key,payload,scopes) VALUES (?,?,?)",
                            (key, canonical(payload), canonical(scopes)))
        if cursor.rowcount:
            self.store.record_event("proactive_received", data={"delivery_key": key, "scope_count": len(scopes)})

    def _receive(self, stream: str, bindings: tuple[tuple[Any, Any, Any], ...]) -> bool:
        identity = self.scheduling.store_identity
        tail = self.scheduling.event_cursor_state(stream)
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM proactive_cursor WHERE stream=?", (stream,)).fetchone()
            state = None if row is None else dict(row)
            budget_row = db.execute("SELECT * FROM proactive_review_budget WHERE stream=?", (stream,)).fetchone()
            review_budget = None if budget_row is None else dict(budget_row)
        if state is None:
            # New consumers explicitly tail existing history, independently per stream.
            with self.store.transaction() as db:
                db.execute("INSERT OR IGNORE INTO proactive_cursor VALUES (?,?,?,?,?,'Active','')",
                           (stream, identity.store_id, identity.generation, tail.tail_id, tail.anchor))
                self.store.record_event("proactive_consumer_initialized", data={"stream": stream, "tail_id": tail.tail_id})
            return True
        if state["status"] != "Active":
            return False
        anchor = self.scheduling.event_anchor(stream, state["received_id"])
        if (state["store_id"] != identity.store_id or state["generation"] != identity.generation or
            tail.tail_id < state["received_id"] or anchor != state["anchor"]):
            with self.store.transaction() as db:
                db.execute("UPDATE proactive_cursor SET status='NeedsReview',error=? WHERE stream=?",
                           ("source identity, generation or cursor anchor changed", stream))
                self.store.record_event("proactive_consumer_needs_review", data={"stream": stream})
            return False
        batch_limit = self.config.event_batch_size if review_budget is None else min(
            self.config.event_batch_size, int(review_budget["remaining"]))
        events = self.scheduling.list_events(stream, after_id=state["received_id"], limit=batch_limit)
        if review_budget is not None:
            events = tuple(event for event in events if event.id <= review_budget["end_id"])
        finish_history = review_budget is not None and (len(events) < batch_limit or
            len(events) == review_budget["remaining"] or (events and events[-1].id == review_budget["end_id"]))
        review_anchor = (None if not finish_history or review_budget is None else
                         self.scheduling.event_anchor(stream, int(review_budget["end_id"])))
        received: list[tuple[Any, Any, list[dict[str, Any]], str]] = []
        for event in events:
            snapshot = self.scheduling.source_snapshot(stream, event.source_id)
            scopes = [self._scope(goal, milestone, binding) for goal, milestone, binding in bindings
                      if binding.source == stream and binding.source_id == event.source_id]
            event_anchor = self.scheduling.event_anchor(stream, event.id)
            if event_anchor is None:
                raise ValueError("schedule source changed while receiving events; retry and review")
            received.append((event, snapshot, scopes, event_anchor))
        with self.store.transaction() as db:
            current = db.execute("SELECT * FROM proactive_cursor WHERE stream=?", (stream,)).fetchone()
            if current is None or current["received_id"] != state["received_id"] or current["status"] != "Active":
                return False
            for event, snapshot, scopes, event_anchor in received:
                delivery = f"local_rules:{identity.store_id}:{identity.generation}:{stream}:{event.id}"
                root = f"{identity.store_id}:{identity.generation}:{stream}:{event.source_id}:{event.occurrence_revision}"
                self._enqueue(db, delivery, {"kind": "schedule_due", "rule_id": "bound_schedule_due",
                    "root_source_key": root, "due_at": event.due_at,
                    "event": event.as_dict(), "snapshot": None if snapshot is None else asdict(snapshot)}, scopes)
                db.execute("UPDATE proactive_cursor SET received_id=?,anchor=? WHERE stream=?",
                           (event.id, event_anchor, stream))
            if review_budget is not None:
                if finish_history:
                    if review_anchor is None:
                        raise ValueError("reviewed history anchor changed while receiving")
                    db.execute("UPDATE proactive_cursor SET received_id=?,anchor=? WHERE stream=?",
                               (review_budget["end_id"], review_anchor, stream))
                    db.execute("DELETE FROM proactive_review_budget WHERE stream=?", (stream,))
                    self.store.record_event("proactive_bounded_history_finished", data={"stream": stream,
                        "remaining_not_received": int(review_budget["remaining"]) - len(events),
                        "reviewed_tail": review_budget["end_id"]})
                else:
                    db.execute("UPDATE proactive_review_budget SET remaining=remaining-? WHERE stream=?", (len(events), stream))
        return True

    def _receive_local(self, bindings: tuple[tuple[Any, Any, Any], ...]) -> None:
        now = self._now()
        local: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
        # Pop only after durable receipt; rollback keeps due work retryable.
        now_stamp = stamp(now)
        due_deadlines = ([] if not self._deadline_heap or self._deadline_heap[0][0] > now_stamp else
                         [entry for entry in self._deadline_heap if entry[0] <= now_stamp])
        for _, key, deadline_payload, scope in due_deadlines:
            local.append((key, deadline_payload, [scope]))
        # Task completion is only evidence for an invitation to review a milestone.
        by_task: dict[str, list[dict[str, Any]]] = {}
        for goal, milestone, binding in bindings:
            if binding.source == "task" and milestone is not None:
                by_task.setdefault(binding.source_id, []).append(self._scope(goal, milestone, binding))
        for task_id, task_scopes in by_task.items():
            task = self.proposals.tasks.get(task_id)
            if task is not None and task.status == TaskStatus.COMPLETED:
                local.append((f"task_completed:{task_id}", {"kind": "milestone_review", "rule_id": "bound_task_completed",
                    "root_source_key": f"task:{task_id}:completed", "due_at": stamp(task.updated_at),
                    "snapshot": {"source": "task", "source_id": task_id, "status": task.status.value,
                                 "revision": task.revision, "use_policy": self.proposals.tasks.task_use_policy(task_id)}}, task_scopes))
        with self.store.transaction() as db:
            for key, payload, inbox_scopes in local:
                self._enqueue(db, key, payload, inbox_scopes)
        while self._deadline_heap and self._deadline_heap[0][0] <= now_stamp:
            heapq.heappop(self._deadline_heap)

    def _scope_valid(self, scope: dict[str, Any]) -> bool:
        goal = self.goals.get(scope["goal_id"])
        if goal.status != GoalStatus.ACTIVE or goal.execution_version != scope["goal_version"]:
            return False
        if scope["milestone_id"] is not None:
            milestone = self.goals.get_milestone(scope["milestone_id"])
            if milestone.status != MilestoneStatus.PENDING or milestone.execution_version != scope["milestone_version"]:
                return False
        if scope["binding_id"] is not None:
            binding = self.goals.get_binding(scope["binding_id"])
            if binding.status != BindingStatus.ACTIVE or binding.revision != scope["binding_revision"]:
                return False
        return True

    def _process(self) -> int:
        created = 0
        budget = self.config.max_proposals_per_poll
        # Every iteration commits one inbox scope with its proposal and continuation.
        while created < budget:
            with self.store.transaction() as db:
                row = db.execute("SELECT * FROM proactive_inbox WHERE status='Pending' ORDER BY sequence LIMIT 1").fetchone()
                if row is None:
                    break
                payload, scopes, outcomes = json.loads(row["payload"]), json.loads(row["scopes"]), json.loads(row["outcomes"])
                index = row["scope_offset"]
                if index >= len(scopes):
                    db.execute("UPDATE proactive_inbox SET status='Done',outcomes=? WHERE sequence=?",
                               (canonical(outcomes or ["no_explicit_binding"]), row["sequence"]))
                    continue
                scope = scopes[index]
                result = "source_missing_or_occurrence_changed"
                snapshot_data = payload.get("snapshot")
                event = payload.get("event")
                source_ok = event is None or (snapshot_data is not None and
                    snapshot_data["occurrence_revision"] == event["occurrence_revision"])
                if not self._scope_valid(scope):
                    result = "scope_version_changed"
                elif source_ok:
                    before = db.total_changes
                    item = self.proposals.create(scope["goal_id"], rule_id=payload["rule_id"],
                        root_source_key=payload["root_source_key"], kind=payload["kind"],
                        due_at=payload["due_at"], ttl_seconds=self.config.proposal_ttl_seconds,
                        milestone_id=scope["milestone_id"], binding_id=scope["binding_id"],
                        source_snapshot=None if snapshot_data is None else snapshot_data
                            if snapshot_data.get("source") == "task" else SourceSnapshot(**snapshot_data),
                        reason="A bound task completed; please explicitly review the milestone evidence."
                            if payload["kind"] == "milestone_review" else
                            "An explicit deadline or bound schedule occurrence is due for review.")
                    changed = db.total_changes > before
                    created += int(item is not None and changed)
                    result = "expired" if item is None else "created" if changed else "duplicate_business_key"
                outcomes.append({"scope_index": index, "result": result})
                index += 1
                db.execute("UPDATE proactive_inbox SET scope_offset=?,status=?,outcomes=? WHERE sequence=?",
                    (index, "Done" if index == len(scopes) else "Pending", canonical(outcomes), row["sequence"]))
                self.store.record_event("proactive_scope_processed", data={"sequence": row["sequence"],
                    "scope_index": index - 1, "result": result})
        return created

    def poll(self) -> int:
        with self._lock:
            return self._poll()

    def _poll(self) -> int:
        if not self.config.enabled:
            return 0
        bindings = self._bindings()
        if not self._receive("timer", bindings) or not self._receive("calendar", bindings):
            # Identity/anchor uncertainty stops all consumption until explicit review.
            return 0
        self._receive_local(bindings)
        self.proposals.refresh(limit=10)
        return self._process()
