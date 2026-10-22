"""Application ownership, review commands and cross-database deletion saga."""

from collections.abc import Callable, Iterable
from dataclasses import asdict, replace
from hashlib import sha256
import math
from pathlib import Path
from threading import Event, Lock, RLock, Thread
from time import monotonic
from types import TracebackType
from typing import Any
from uuid import uuid4

from deskpal.agent.core import AgentCore
from deskpal.agent.models import AgentResult
from deskpal.config import AppConfig
from deskpal.conversation.runtime import ConversationRuntime
from deskpal.execution_log import ExecutionLog
from deskpal.goals import GoalManager
from deskpal.memory.contracts import DeletionReceipt, MemoryItem, MemoryRuntime, MemoryStatus
from deskpal.proactive import LocalRuleEngine, ProposalManager
from deskpal.scheduling import SchedulingService
from deskpal.tasks.models import Task, TaskStatus
from deskpal.workflow.journal import ExecutionJournal
from deskpal.workflow.humanization import HumanizationStore
from deskpal.workflow.models import DependencyRef, PlanSnapshot, WorkflowBlocked
from deskpal.workflow.store import WorkflowStore, canonical
from deskpal.workflow.tasks import PersistentTaskManager

from .audit import deliver_audit
from .dispatcher import SubmissionDispatcher
from .effects import CompanionEffects
from .interaction import InteractionCoordinator
from .privacy import PrivacyCoordinator


class ApplicationService:
    def __init__(self, *, config: AppConfig, core: AgentCore, store: WorkflowStore,
                 tasks: PersistentTaskManager, journal: ExecutionJournal,
                 scheduling: SchedulingService, goals: GoalManager, proposals: ProposalManager,
                 rules: LocalRuleEngine, audit_sink: ExecutionLog,
                 release_resources: Callable[[], None]) -> None:
        runtime = core.memory_runtime
        if runtime is None:
            raise ValueError("A persistent application requires MemoryRuntime")
        self.config, self.core, self.store = config, core, store
        self.tasks, self.journal = tasks, journal
        self.scheduling, self.goals, self.proposals, self.rules = scheduling, goals, proposals, rules
        self.tools, self.memory = core.tools, core.memory
        self._memory_runtime: MemoryRuntime = runtime
        self._audit_sink, self._release_resources = audit_sink, release_resources
        self._close_lock = Lock()
        self._shutdown_lock = Lock()
        self._shutdown_started = False
        self._confirmation_lock = RLock()
        self._audit_lock = Lock()
        self._closed = False
        self._closing = False
        self._reviewed: dict[str, str] = {}
        self._live_pending: set[str] = set()
        self._rule_stop = Event()
        self._rule_error: str | None = None
        self._rule_thread: Thread | None = None
        self._started = False
        self._cleanup_lock = RLock()
        self._humanization = HumanizationStore(store)
        self._privacy = PrivacyCoordinator(self._humanization, journal)
        self._conversations = ConversationRuntime()
        self._interaction = InteractionCoordinator(persistence=self._humanization,
            privacy=self._privacy, conversations=self._conversations, tasks=tasks,
            memory=runtime, companion=core.companion_runtime, provider=core.llm,
            cloud=config.llm.provider != "offline")
        self._effects = CompanionEffects(self._humanization, self._privacy, core.companion_runtime,
            should_stop=lambda: self._closing or self._cleanup_pending)
        core.attach_interaction(self._interaction)
        journal.add_validator(self._validate_memory)
        journal.add_validator(proposals.validate_task)
        self._dispatcher = SubmissionDispatcher(core, tasks, journal,
            guard=self.execution_guard, on_idle=self._maintenance)
        # Resume deletion before recovery can expose a persisted request/plan.
        self._cleanup_pending = False
        self._retry_deletions()
        self.recovered_tasks = journal.recover() if not self._cleanup_pending else ()

    def start(self) -> None:
        """Start workers after the factory has attached execution ownership."""
        self._open()
        if self._started or self._cleanup_pending:
            return
        try:
            if self.config.proactive.enabled:
                self._rule_thread = Thread(target=self._poll_rules, name="deskpal-proposals", daemon=True)
                self._rule_thread.start()
            self._dispatcher.start()
            self._started = True
        except BaseException:
            self._rule_stop.set()
            if self._rule_thread is not None and self._rule_thread.is_alive():
                self._rule_thread.join()
            raise

    @property
    def closed(self) -> bool:
        return self._closed

    def _open(self) -> None:
        if self._closed or self._closing:
            raise RuntimeError("Application is closed or closing")

    def _ready(self) -> None:
        self._open()
        if self._cleanup_pending or self._humanization.privacy()["blocked"]:
            raise WorkflowBlocked("Memory deletion is pending; retry cleanup before reading or executing work")

    def _validate_memory(self, task_id: str, refs: tuple[DependencyRef, ...]) -> None:
        memory_refs = tuple((ref.object_id, ref.revision) for ref in refs if ref.kind == "memory")
        if memory_refs and not self._memory_runtime.valid(memory_refs):
            raise WorkflowBlocked("Memory revision, expiry or use policy changed")

    def execution_guard(self, task_id: str) -> None:
        # Closing requests cooperation through cancel_requested; it does not
        # release resources from under a tool still completing its current call.
        if self._cleanup_pending or self.journal.pending_deletions():
            raise WorkflowBlocked("Memory cleanup is pending")
        if self.config.llm.provider != "offline" and self.tasks.task_use_policy(task_id) == "local_only":
            raise WorkflowBlocked("This source is local_only; cloud planning requires a new source version")
        snapshot = self.journal.get_plan(task_id)
        if snapshot is not None:
            self._verify_snapshot(snapshot)

    def submit(self, request: str, *, command_id: str | None = None,
               authorized_tools: Iterable[str] = (), high_risk_authorized_tools: Iterable[str] = (),
               dependencies: tuple[Any, ...] = (), use_policy: str = "cloud_allowed",
               conversation_id: str | None = None,
               expected_conversation_epoch: int | None = None) -> Task:
        self._ready()
        if (conversation_id is None) != (expected_conversation_epoch is None):
            raise ValueError("Conversation identity and expected epoch must be supplied together")

        def register(task: Task) -> None:
            prior = self._humanization.turn(task.id)
            identity = conversation_id or "single:" + task.id
            epoch = expected_conversation_epoch if expected_conversation_epoch is not None else 0
            if prior is not None:
                if prior["conversation_id"] != identity or prior["epoch"] != epoch:
                    raise ValueError("Command retry cannot change its conversation")
                return
            sequence = self._conversations.reserve(identity, epoch) if conversation_id is not None else 1
            self._humanization.bind_turn(task.id, identity, epoch, sequence)
            for role in ("user", "assistant"):
                self.store.set_dependency_version("dialogue_turn", task.id + ":" + role,
                    sequence, use_policy=use_policy)

        return self._dispatcher.submit(request, command_id=str(uuid4()) if command_id is None else command_id,
            authorized_tools=AgentCore._authorization(authorized_tools),
            high_risk_authorized_tools=AgentCore._authorization(high_risk_authorized_tools),
            dependencies=dependencies, use_policy=use_policy, register=register)

    def run(self, request: str, *, authorized_tools: Iterable[str] = (),
            high_risk_authorized_tools: Iterable[str] = ()) -> AgentResult:
        task = self.submit(request, authorized_tools=authorized_tools,
                           high_risk_authorized_tools=high_risk_authorized_tools)
        return self.wait(task.id)

    def handle_request(self, request: str, *, authorized_tools: Iterable[str] = (),
                       high_risk_authorized_tools: Iterable[str] = ()) -> AgentResult:
        return self.run(request, authorized_tools=authorized_tools,
                        high_risk_authorized_tools=high_risk_authorized_tools)

    def wait(self, task_id: str, timeout: float | None = None) -> AgentResult:
        self._ready()
        admitted_epoch = int(self._humanization.privacy()["epoch"])
        result = self._dispatcher.wait(task_id, timeout)
        self._privacy.check(admitted_epoch)
        self._ready()
        with self.store.transaction() as conn:
            self.store.check_privacy(conn, task_id)
        if result.status == TaskStatus.WAITING:
            snapshot = self.journal.get_plan(task_id)
            if snapshot is not None:
                self._reviewed[task_id] = snapshot.hash
                self._live_pending.add(task_id)
        else:
            self._reviewed.pop(task_id, None)
            self._live_pending.discard(task_id)
        return result

    def _verify_snapshot(self, snapshot: PlanSnapshot) -> None:
        if sha256(snapshot.canonical_json.encode("utf-8")).hexdigest() != snapshot.hash:
            raise WorkflowBlocked("Plan snapshot integrity failed")
        if Path(snapshot.workspace_dir).resolve() != self.core.workspace_dir:
            raise WorkflowBlocked("Plan workspace changed")
        current = {spec.name: spec for spec in self.tools.list_tools()}
        original = {spec.name: spec for spec in snapshot.tool_specs}
        for step in snapshot.plan.steps:
            before, after = original.get(step.name), current.get(step.name)
            if before is None or after is None or canonical(asdict(before)) != canonical(asdict(after)):
                raise WorkflowBlocked("Plan tool contract changed; create a new intent and review its new plan")
        if snapshot.memory_refs and not self._memory_runtime.valid(snapshot.memory_refs):
            raise WorkflowBlocked("Plan memory context changed")

    def review_task(self, task_id: str) -> dict[str, Any]:
        self._ready()
        return self.journal.review(task_id)

    def presentation_snapshot(self) -> dict[str, Any]:
        """Expose bounded task metadata through the existing lifecycle gates."""
        self._ready()
        return self.tasks.presentation_snapshot()

    def companion_snapshot(self) -> dict[str, Any]:
        """Read a detached local expression projection, including during work."""
        with self._close_lock:
            self._ready()
            return self.core.companion_snapshot()

    def companion_command(self, action: str, **parameters: Any) -> dict[str, Any]:
        """Serialize explicit local settings with the sole execution worker."""
        if action not in {"configure", "reset", "rollback", "preference", "cue"}:
            raise ValueError("Use the versioned interaction interface for this command")
        if action == "preference" and parameters.get("use_policy") == "local_only":
            self.interaction_command(action, **parameters)
            return self.companion_snapshot()
        with self._close_lock:
            self._ready()
            with self._dispatcher.management():
                epoch = int(self._humanization.privacy()["epoch"])
                with self._privacy.publication(epoch):
                    if action == "reset":
                        self._humanization.discard_effects()
                    result = self.core.companion_command(action, **parameters)
                    if action == "reset":
                        self._interaction.invalidate()
                    return result

    def conversation_open(self) -> dict[str, Any]:
        self._ready()
        return self._conversations.open()

    def conversation_snapshot(self, conversation_id: str) -> dict[str, Any]:
        self._ready()
        return self._conversations.snapshot(conversation_id)

    def conversation_close(self, conversation_id: str, expected_epoch: int) -> dict[str, Any]:
        self._ready()
        epoch = int(self._humanization.privacy()["epoch"])
        with self._privacy.publication(epoch):
            self._conversations.close(conversation_id, expected_epoch)
            close_scope = getattr(self.core.companion_runtime, "close_conversation", None)
            if callable(close_scope):
                close_scope(conversation_id)
        return {"closed": True, "conversation_id": conversation_id, "epoch": expected_epoch}

    def interaction_snapshot(self) -> dict[str, Any]:
        self._ready()
        read = getattr(self.core.companion_runtime, "interaction_snapshot", None)
        if not callable(read):
            raise ValueError("Interaction settings are unavailable")
        return dict(read())

    def interaction_command(self, action: str, **parameters: Any) -> dict[str, Any]:
        self._ready()
        allowed = {"cloud_permissions", "identity", "preference", "confirm_candidate", "reject_candidate",
                   "clear_cue", "clear_constraints"}
        if action not in allowed:
            raise ValueError("Unknown interaction command")
        runtime = self.core.companion_runtime
        if runtime is None:
            raise ValueError("Interaction settings are unavailable")
        snapshot = self.interaction_snapshot()
        requested = parameters.get("permissions", {})
        restriction = action == "cloud_permissions" and isinstance(requested, dict) and any(
            enabled and requested.get(key, enabled) is False
            for key, enabled in snapshot.get("cloud_permissions", {}).items())
        restriction = restriction or (action == "identity" and parameters.get("use_policy") == "local_only"
            and snapshot.get("identity_use_policy") == "cloud_allowed")
        restriction = restriction or (action == "preference" and parameters.get("use_policy") == "local_only")
        current_epoch = int(self._humanization.privacy()["epoch"])
        # Revoke before the cross-database configuration write. If it fails,
        # previous admissions stay invalid rather than risking a stale send.
        with self._privacy.publication(current_epoch):
            if restriction:
                self._humanization.block_privacy()
            try:
                runtime.command(action, **parameters)
                if restriction:
                    self._interaction.invalidate()
                    self._humanization.discard_effects()
            finally:
                if restriction:
                    self._humanization.unblock_privacy()
        result = self.interaction_snapshot()
        result["privacy_invalidated"] = bool(restriction)
        result["privacy_epoch"] = int(self._humanization.privacy()["epoch"])
        return result

    def request_forget(self, memory_id: str, expected_revision: int,
                       command_id: str) -> dict[str, Any]:
        """Accept durable use blocking even while a provider/tool is waiting."""
        self._open()
        if not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 200:
            raise ValueError("Forget requires a stable bounded command ID")
        if not isinstance(memory_id, str) or not memory_id or type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("Forget requires a source and positive revision")
        prior = self._privacy.prior(command_id, memory_id, expected_revision)
        if prior is not None:
            return prior
        current = self._memory_current(memory_id)
        if current is None or (current.revision != expected_revision and not (
                current.status == MemoryStatus.DELETED and current.revision == expected_revision + 1)):
            raise ValueError("Memory revision changed or source does not exist")
        receipt = self._privacy.begin(memory_id, expected_revision, command_id)
        self._cleanup_pending = True
        # No wait for the Core's running flag or its in-flight network call.
        try:
            self._interaction.invalidate()
        except Exception:
            # The durable block already prevents reads, new sends and steps.
            # Fallible physical/cue cleanup is retried by the deletion job.
            pass
        self._dispatcher.clear_cached()
        self._reviewed.clear()
        self._live_pending.clear()
        try:
            self.retry_cleanup()
        except RuntimeError:
            pass  # The sole worker will retry at its next safe idle boundary.
        return self._privacy.prior(command_id, memory_id, expected_revision) or receipt

    def _memory_current(self, memory_id: str) -> MemoryItem | None:
        getter = getattr(self._memory_runtime, "get_item", None)
        if callable(getter):
            item: MemoryItem | None = getter(memory_id)
            return item
        candidates = tuple(item for item in self._memory_runtime.list_items(include_history=True)
                           if item.id == memory_id)
        return max(candidates, key=lambda item: item.revision) if candidates else None

    def cleanup_status(self) -> dict[str, Any]:
        self._open()
        state = self._humanization.privacy()
        return {"schema_version": 1, "privacy_epoch": state["epoch"],
                "blocked": state["blocked"] or self._cleanup_pending,
                "status": "Pending" if state["blocked"] or self._cleanup_pending else "Completed",
                "pending": [{"job_id": job.id, "memory_id": job.memory_id, "status": job.status}
                            for job in self.journal.pending_deletions()],
                "waiting_for_worker": self._dispatcher.diagnostics().get("active_task_id") is not None}

    def resume(self, task_id: str) -> PlanSnapshot | Task:
        self._ready()
        with self._dispatcher.management():
            snapshot = self.journal.get_plan(task_id)
            if snapshot is not None:
                self._verify_snapshot(snapshot)
            task = self.journal.resume_unstarted(task_id)
            if snapshot is not None:
                self.core.restore_pending(task_id, snapshot.plan, snapshot.memory_refs)
                self._reviewed[task_id] = snapshot.hash
                self._live_pending.add(task_id)
                return snapshot
            self._dispatcher.wake()
            return task

    def confirm_task(self, task_id: str, *, authorized_tools: Iterable[str] = (),
                     high_risk_authorized_tools: Iterable[str] = (),
                     plan_hash: str | None = None) -> AgentResult:
        self.enqueue_confirmation(task_id, authorized_tools=authorized_tools,
            high_risk_authorized_tools=high_risk_authorized_tools, plan_hash=plan_hash)
        return self.wait(task_id)

    def enqueue_confirmation(self, task_id: str, *, authorized_tools: Iterable[str] = (),
                             high_risk_authorized_tools: Iterable[str] = (),
                             plan_hash: str | None = None, command_id: str | None = None) -> Task:
        """Validate and queue grants; return without waiting for the unique worker."""
        self._ready()
        authorization = AgentCore._authorization(authorized_tools)
        high_risk = AgentCore._authorization(high_risk_authorized_tools)
        if command_id is not None and (not isinstance(command_id, str) or not command_id.strip()):
            raise ValueError("Confirmation command_id must be non-empty")
        with self._confirmation_lock:
            expected = plan_hash if plan_hash is not None else self._reviewed.get(task_id)
            fingerprint = sha256(canonical({"operation": "confirm", "task_id": task_id,
                "plan_hash": expected, "ordinary": sorted(authorization), "high_risk": sorted(high_risk)}).encode()).hexdigest()
            if command_id is not None:
                with self.store.transaction() as conn:
                    previous = conn.execute("SELECT payload_hash,task_id FROM command WHERE id=?", (command_id,)).fetchone()
                    if previous is not None:
                        if previous["payload_hash"] != fingerprint or previous["task_id"] != task_id:
                            raise ValueError("Command ID already used with a different payload")
                        task = self.tasks.get(task_id)
                        assert task is not None
                        return task
            return self._enqueue_confirmation(task_id, authorization, high_risk, expected, command_id, fingerprint)

    def _enqueue_confirmation(self, task_id: str, authorization: frozenset[str], high_risk: frozenset[str],
                              expected: str | None, command_id: str | None, fingerprint: str) -> Task:
        with self._dispatcher.management():
            snapshot = self.journal.get_plan(task_id)
            if snapshot is None:
                raise WorkflowBlocked("No intact persisted plan")
            self._verify_snapshot(snapshot)
            if expected != snapshot.hash:
                raise WorkflowBlocked("Review the exact persisted plan and provide its plan hash")
            task = self.tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.status == TaskStatus.NEEDS_REVIEW:
                task = self.journal.resume_unstarted(task_id)
            self.journal.check_task(task_id)
            if self.journal.attempts(task_id):
                raise WorkflowBlocked("Previously attempted plans cannot be resumed; create only the remaining intent")
            # Preserve V0.3 grants for the same exact plan in this process.
            # Recovered snapshots always start with empty grants.
            if task_id not in self._live_pending:
                self.core.restore_pending(task_id, snapshot.plan, snapshot.memory_refs)
                self._live_pending.add(task_id)
            # Match submit/worker lock order: dispatcher control, then SQLite.
            with self._dispatcher._condition, self.store.transaction() as conn:
                self.store.record_event("plan_confirmation", task_id, {"plan_hash": snapshot.hash})
                if command_id is not None:
                    conn.execute("INSERT INTO command VALUES(?,?,?)", (command_id, fingerprint, task_id))
                self._dispatcher.confirm(task_id, authorization, high_risk)
            return task

    def cancel_task(self, task_id: str) -> Task:
        if self._closed:
            raise RuntimeError("Application is closed")
        task = self.tasks.request_cancel(task_id)
        self.core.revoke_pending((task_id,))
        self._dispatcher.clear_cached((task_id,))
        self._reviewed.pop(task_id, None)
        self._live_pending.discard(task_id)
        self._dispatcher.wake()
        if self._cleanup_pending:
            return replace(task, request="[redacted pending cleanup]", result=None, error=None)
        return task

    def review_uncertain(self, task_id: str, verdict: str, evidence: str) -> Task:
        self._ready()
        return self.journal.review_uncertain(task_id, verdict, evidence)

    def memory_command(self, action: str, **parameters: Any) -> Any:
        self._ready()
        with self._dispatcher.management():
            if action != "forget":
                value = self.core.memory_command(action, **parameters)
                if action in {"correct", "accept", "reject"}:
                    self._dispatcher.clear_cached()
                    self._reviewed.clear()
                    self._live_pending.clear()
                return value
            memory_id, revision = parameters["memory_id"], parameters["expected_revision"]
            if not isinstance(memory_id, str) or type(revision) is not int or revision < 1:
                raise ValueError("Forget requires an identifier and a positive revision")
            candidates = tuple(item for item in self._memory_runtime.list_items(include_history=True)
                               if item.id == memory_id)
            current = max(candidates, key=lambda item: item.revision) if candidates else None
            if current is None or (revision != current.revision and not (
                    current.status == MemoryStatus.DELETED and revision == current.revision - 1)):
                raise ValueError("Memory revision changed or the identifier does not exist")
            with self._audit_lock:
                self._privacy.begin(memory_id, revision, "legacy-forget:" + memory_id + ":" + str(revision))
                self._cleanup_pending = True
            self._interaction.invalidate()
            receipt = self._retry_deletions(target=memory_id)
            if receipt is None or self._cleanup_pending:
                return {"memory_id": memory_id, "status": "Pending", "blocked": True,
                        "retained": ("independent goals and calendar", "historical logs, backups and external copies")}
            return receipt

    def _retry_deletions(self, *, target: str | None = None) -> DeletionReceipt | None:
        with self._cleanup_lock:
            return self._continue_deletions(target=target)

    def _continue_deletions(self, *, target: str | None = None) -> DeletionReceipt | None:
        receipt: DeletionReceipt | None = None
        for job in self.journal.pending_deletions():
            try:
                result = self.core.memory_command("forget", memory_id=job.memory_id,
                                                  expected_revision=job.expected_revision)
                affected = set(self.journal.scrub_memory(job.memory_id))
                # A shared source may erase several IDs. Only actual deletion
                # tombstones authorize erasing history; expiry or correction
                # invalidates execution without erasing unrelated task copies.
                deleted_ids = {item.id for item in self._memory_runtime.list_items(
                    status=MemoryStatus.DELETED, include_history=True)}
                scrubbed_ids = {job.memory_id}
                for task in self.tasks.list_tasks():
                    for ref in self.journal.get_dependencies(task.id):
                        if ref.kind == "memory" and ref.object_id in deleted_ids and ref.object_id not in scrubbed_ids:
                            affected.update(self.journal.scrub_memory(ref.object_id))
                            scrubbed_ids.add(ref.object_id)
                if isinstance(result, DeletionReceipt):
                    affected.update(result.affected_task_ids)
                    # Optional candidates/episodes produced by a now-tainted
                    # request are derived copies too, even if they have a new
                    # MemorySource identity. Cleanup runs only after the worker
                    # leaves, so a late evaluator cannot recreate them afterward.
                    while True:
                        derived_items = tuple(item for item in self._memory_runtime.list_items()
                            if item.source.task_id in affected and item.status != MemoryStatus.DELETED)
                        if not derived_items:
                            break
                        for item in derived_items:
                            # Another item sharing this source may already have
                            # erased it; re-read only its current revision.
                            current_item = self._memory_current(item.id)
                            if current_item is None or current_item.status == MemoryStatus.DELETED:
                                continue
                            derived = self._memory_runtime.forget(current_item.id, current_item.revision)
                            affected.update(derived.affected_task_ids)
                            affected.update(self.journal.scrub_memory(current_item.id))
                    forget_preferences = getattr(self.core.companion_runtime, "forget_sources", None)
                    if callable(forget_preferences):
                        forget_preferences(affected)
                    for task_id in result.affected_task_ids:
                        if self.tasks.get(task_id) is not None:
                            self.tasks.scrub(task_id)
                    result = replace(result, affected_task_ids=tuple(sorted(affected)),
                        cleared=result.cleared + ("workflow requests, plans, results and pending audit copies",),
                        retained=result.retained + ("independently created goals and calendar",))
                    if target == job.memory_id:
                        receipt = result
                self.core.revoke_pending(affected)
                self._dispatcher.clear_cached(affected)
                self.recovered_tasks = ()
                for task_id in affected:
                    self._reviewed.pop(task_id, None)
                    self._live_pending.discard(task_id)
                self.journal.complete_deletion(job.id)
                # A newer explicitly authorized revision may have completed a
                # source whose older request was waiting on a CAS conflict.
                # Once the source is deleted, those older jobs can finish too.
                for related in self.journal.pending_deletions():
                    if related.id != job.id and related.memory_id == job.memory_id:
                        self.journal.complete_deletion(related.id)
            except Exception:
                # The persistent block is already committed. Startup retries the
                # idempotent memory and workflow stages before exposing work.
                continue
        with self._privacy.exclusive():
            self._cleanup_pending = bool(self.journal.pending_deletions())
            if not self._cleanup_pending:
                self._humanization.unblock_privacy()
        return receipt

    def retry_cleanup(self) -> bool:
        self._open()
        if not self._cleanup_pending and not self.journal.pending_deletions():
            return True
        with self._dispatcher.management():
            self._retry_deletions()
            if not self._cleanup_pending:
                self.recovered_tasks = self.journal.recover()
                self.start()
        return not self._cleanup_pending

    def accept_proposal(self, proposal_id: str, expected_revision: int, *, command_id: str) -> Task:
        self._ready()
        task = self.proposals.accept(proposal_id, expected_revision, command_id=command_id)
        self._dispatcher.wake()
        return task

    def proposal_summary(self, limit: int = 10) -> Any:
        self._ready()
        return self.rules.summary(limit=limit)

    def flush_audit(self) -> None:
        with self._audit_lock:
            if self._cleanup_pending or self.journal.pending_deletions():
                return
            deliver_audit(self.store, self._audit_sink)

    def _maintenance(self) -> None:
        """Existing worker startup/idle boundary, without an active Core call."""
        if self._closing:
            return
        if self._cleanup_pending or self.journal.pending_deletions():
            try:
                self.retry_cleanup()
            except RuntimeError:
                return
        if not self._cleanup_pending:
            try:
                self._effects.drain()
            finally:
                self.flush_audit()

    def _poll_rules(self) -> None:
        while not self._rule_stop.is_set():
            try:
                if not self._cleanup_pending:
                    self.rules.poll()
                    self._rule_error = None
            except Exception as exc:
                self._rule_error = type(exc).__name__
            self._rule_stop.wait(self.config.proactive.poll_interval_seconds)

    def diagnostics(self) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("Application is closed")
        return {"closing": self._closing, "cleanup_pending": self._cleanup_pending,
                "pending_deletions": len(self.journal.pending_deletions()),
                "pending_audit_page": len(self.store.pending_audit()), "proposal_error": self._rule_error,
                "dispatcher": self._dispatcher.diagnostics()}

    def begin_shutdown(self) -> None:
        """Stop new claims and request cooperation without closing owned resources.

        Safe to call while a facade query is still returning. A retry never
        writes another cancellation revision for the same shutdown request.
        """
        with self._shutdown_lock:
            if self._closed or self._shutdown_started:
                return
            self._shutdown_started = True
            self._closing = True
            self._rule_stop.set()
            self._dispatcher.stop_accepting()

    def close(self, timeout: float | None = None) -> bool:
        with self._close_lock:
            if self._closed:
                return True
            seconds = self.config.workflow.shutdown_timeout_seconds if timeout is None else timeout
            if isinstance(seconds, bool) or not math.isfinite(seconds) or seconds < 0:
                raise ValueError("Shutdown timeout must be finite and non-negative")
            deadline = monotonic() + seconds
            self.begin_shutdown()
            if self._rule_thread is not None:
                self._rule_thread.join(max(0.0, deadline - monotonic()))
                if self._rule_thread.is_alive():
                    return False
            if not self._dispatcher.shutdown(max(0.0, deadline - monotonic())):
                return False
            if not self.scheduling.shutdown(max(0.0, deadline - monotonic())):
                return False
            self.flush_audit()
            self._reviewed.clear()
            self._live_pending.clear()
            self._release_resources()
            self._closed = True
            return True

    def __enter__(self) -> "ApplicationService":
        self._open()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None,
                 traceback: TracebackType | None) -> None:
        if not self.close():
            raise RuntimeError("Application is still closing; active tool retains resources and instance lock")
