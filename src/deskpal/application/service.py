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
from deskpal.assets import AssetDecoder, AssetService
from deskpal.assets.models import AssetPreview
from deskpal.config import AppConfig
from deskpal.conversation.runtime import ConversationRuntime
from deskpal.conversation.repository import ConversationRepository
from deskpal.execution_log import ExecutionLog
from deskpal.goals import GoalManager
from deskpal.memory.contracts import DeletionReceipt, MemoryItem, MemoryRuntime, MemoryStatus
from deskpal.proactive import LocalRuleEngine, ProposalManager
from deskpal.proactive.policy import NotificationPolicy
from deskpal.proactive.coordinator import ProactiveCoordinator
from deskpal.workflow.control import TaskControl
from deskpal.scheduling import SchedulingService
from deskpal.tasks.models import Task, TaskStatus
from deskpal.workflow.journal import ExecutionJournal
from deskpal.workflow.humanization import HumanizationStore
from deskpal.workflow.models import DependencyRef, PlanSnapshot, WorkflowBlocked
from deskpal.workflow.store import WorkflowStore, canonical
from deskpal.workflow.tasks import PersistentTaskManager

from .audit import deliver_audit
from .dispatcher import SubmissionDispatcher
from .interaction import InteractionCoordinator
from .privacy import PrivacyCoordinator
from .conversation_service import ConversationService
from .reminders import ReminderInbox, ReminderPump
from .task_review import TaskReviewService, required_permissions
from .tool_catalog import ToolCatalog
from .learning import FeedbackLearning
from deskpal.memory.coordinator import MemoryCoordinator


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
        self.assets = AssetService(store, config.storage.data_dir)
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
        self._conversation_repository = ConversationRepository(store)
        self._conversation_repository.ensure_default()
        self._conversations = ConversationRuntime(repository=self._conversation_repository)
        self._interaction = InteractionCoordinator(persistence=self._humanization,
            privacy=self._privacy, conversations=self._conversations, tasks=tasks,
            memory=runtime, companion=core.companion_runtime, provider=core.llm,
            cloud=config.llm.is_cloud, profile_id=config.storage.data_dir.name,
            timezone_name=config.proactive.timezone)
        if not isinstance(runtime, MemoryCoordinator):
            raise ValueError('Application learning requires MemoryCoordinator')
        self.learning = FeedbackLearning(self, runtime)
        core.attach_feedback(self.learning)
        core.attach_interaction(self._interaction)
        journal.add_validator(self._validate_memory)
        journal.add_validator(proposals.validate_task)
        self._dispatcher = SubmissionDispatcher(core, tasks, journal,
            guard=self.execution_guard, on_idle=self._maintenance)
        self._conversation_service = ConversationService(self._conversation_repository,
            self._conversations, tasks, self._privacy, self._dispatcher, core.revoke_pending,
            self._interaction.discard_tasks,
            getattr(core.companion_runtime, "clear_conversation", None))
        self._conversation_service.clear_learning = self.learning.store.redact_conversation
        self._task_review = TaskReviewService(self)
        self._tool_catalog = ToolCatalog(self.tools)
        from .management_tools import management_tools
        for tool in management_tools(self):
            self.tools.register(tool)
        self.task_control = TaskControl(store)
        core._task_control = self.task_control
        self.notification_policy = NotificationPolicy(store, quiet_start=config.proactive.quiet_start,
            quiet_end=config.proactive.quiet_end, zone=config.proactive.timezone)
        self.rules.quiet_check = self.notification_policy.is_quiet
        self.proactive = ProactiveCoordinator(self, self.notification_policy)
        core._proactive_publication = self.proactive.can_publish
        core._publication_control = lambda: self.notification_policy.publication
        journal.add_validator(self.proactive.validate_task)
        self._reminders = ReminderInbox(store, scheduling, policy=self.notification_policy)
        self._reminder_pump = ReminderPump(self._reminders)
        observe = getattr(self.tools, "set_result_observer", None)
        if callable(observe):
            observe(self._reminders.register_result)
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
            self._reminder_pump.start()
            if self.config.proactive.enabled:
                self._rule_thread = Thread(target=self._poll_rules, name="deskpal-proposals", daemon=True)
                self._rule_thread.start()
            self._dispatcher.start()
            self._started = True
        except BaseException:
            self._reminder_pump.shutdown(self.config.workflow.shutdown_timeout_seconds)
            self._rule_stop.set()
            if self._rule_thread is not None and self._rule_thread.is_alive():
                self._rule_thread.join()
            raise

    def enqueue_learning_review(self, command_id: str) -> None:
        self._ready()
        if not command_id or len(command_id) > 200:
            raise ValueError('Review requires bounded work identity')
        self._dispatcher.enqueue_internal('learning:' + command_id, lambda: self.learning.run_review(command_id))

    def learning_records(self, limit: int = 20) -> dict[str, Any]:
        self._ready()
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('Learning page must contain 1..20 records')
        return {'items': self.learning.store.records(limit), 'mode': self.config.memory.mode}

    @property
    def closed(self) -> bool:
        return self._closed

    def _open(self) -> None:
        if self._closed or self._closing:
            raise RuntimeError("Application is closed or closing")

    def configure_asset_decoder(self, decoder: AssetDecoder) -> None:
        """Inject the optional codec from the GUI initialization worker."""
        self._open()
        self.assets.set_decoder(decoder)

    def appearance_snapshot(self) -> dict[str, Any]:
        self._ready()
        return {"profiles": [asdict(profile) for profile in self.assets.snapshot()]}

    def asset_import_prepare(self, path: str, slot: str, generation: int) -> Any:
        self._ready()
        if not isinstance(path, str) or not path:
            raise ValueError("A local image path is required")
        return self.assets.prepare(Path(path), slot, generation=generation)

    def appearance_apply(self, slot: str, token: str, expected_revision: int, generation: int,
                         display_mode: str = "contain", crop: tuple[float, float, float, float] | None = None) -> Any:
        self._ready()
        return self.assets.apply(slot, token, expected_revision, generation=generation,
                                 display_mode=display_mode, crop=crop)

    def appearance_reset(self, slot: str, expected_revision: int) -> Any:
        self._ready()
        return self.assets.reset(slot, expected_revision)

    def asset_import_discard(self, token: str) -> dict[str, Any]:
        self._ready()
        return {"discarded": self.assets.discard(token)}

    def asset_collect(self, limit: int = 32) -> Any:
        self._ready()
        return self.assets.collect(limit=limit)

    def asset_preview(self, **parameters: Any) -> AssetPreview:
        """Private local image channel; bytes never enter DesktopFacade JSON."""
        self._ready()
        return self.assets.preview(**parameters)

    def task_review(self, task_id: str) -> dict[str, Any]:
        self._ready()
        return self._task_review.review(task_id)

    def task_page(self, *, offset: int = 0, limit: int = 50,
                  statuses: tuple[TaskStatus, ...] | None = None) -> dict[str, Any]:
        """Bounded human-readable cards, without materializing Task bodies."""
        self._ready()
        with self.store.read_connection() as connection:
            self.store.check_privacy(connection)
            result = self.tasks.page_summaries(offset=offset, limit=limit, statuses=statuses)
            for item in result["items"]:
                row = connection.execute("SELECT CASE WHEN t.scrubbed=1 THEN '已清理任务' ELSE substr(t.request,1,120) END AS title,r.conversation_id FROM task t LEFT JOIN turn_ref r ON r.task_id=t.id WHERE t.id=?", (item["id"],)).fetchone()
                assert row is not None
                item.update(title=row["title"], conversation_id=row["conversation_id"])
            return result

    def task_updates(self, task_ids: list[str], known_revisions: dict[str, int] | None = None) -> dict[str, Any]:
        self._ready()
        return self._task_review.updates(task_ids, known_revisions)

    def tools_catalog(self, **parameters: Any) -> dict[str, Any]:
        self._ready()
        return self._tool_catalog.catalog(**parameters)

    def tool_detail(self, id: str, catalog_revision: str | None = None) -> dict[str, Any]:
        self._ready()
        return self._tool_catalog.detail(id, catalog_revision=catalog_revision)

    def plugins_catalog(self) -> dict[str, Any]:
        self._ready()
        return self._tool_catalog.plugins()

    def reminder_feed(self, *, cursor: str | None = None, limit: int = 50, unread: bool = False) -> dict[str, Any]:
        self._ready()
        return self._reminders.feed(cursor=cursor, limit=limit, unread=unread)

    def reminder_detail(self, id: str) -> dict[str, Any]:
        self._ready()
        return self._reminders.detail(id)

    def reminder_ack(self, id: str, command_id: str) -> dict[str, Any]:
        self._ready()
        return self._reminders.ack(id, command_id)

    def schedule_items(self, *, source: str = "all", offset: int = 0,
                       limit: int = 50, status: str = "all") -> dict[str, Any]:
        self._ready()
        result = self.scheduling.page_items(source=source, offset=offset, limit=limit, status=status)
        return {"schema_version": 1, **result}

    def command_status(self, command_id: str) -> dict[str, Any]:
        self._ready()
        if not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 200:
            raise ValueError("A bounded command identity is required")
        with self.store.read_connection() as connection:
            self.store.check_privacy(connection)
            control = connection.execute('SELECT task_id,operation FROM task_control_receipt WHERE command_id=?', (command_id,)).fetchone()
            if control:
                task = self.tasks.get(control['task_id'])
                return dict(known=True, accepted=True, task_id=control['task_id'], operation=control['operation'], status=task.status.value if task else 'Unknown', current_process_execution_authority=False)
            command = connection.execute("SELECT task_id FROM command WHERE id=?", (command_id,)).fetchone()
            receipt = connection.execute("SELECT * FROM conversation_receipt WHERE command_id=?", (command_id,)).fetchone()
            if command is not None and receipt is not None and not (receipt["operation"] == "submit" and receipt["result_identity"] == command["task_id"]):
                raise WorkflowBlocked("Command identity has conflicting operation namespaces")
            if command is not None:
                task = connection.execute("SELECT id,status,revision FROM task WHERE id=?", (command["task_id"],)).fetchone()
                assert task is not None
                return {"schema_version": 1, "command_id": command_id, "known": True, "accepted": True,
                    "namespace": "workflow", "task_id": task["id"], "status": task["status"], "revision": task["revision"],
                    "current_process_execution_authority": False}
            if receipt is not None:
                operation = receipt["operation"]
                return {"schema_version": 1, "command_id": command_id, "known": True, "accepted": True,
                    "namespace": "conversation", "operation": operation, "conversation_id": receipt["conversation_id"],
                    "result_identity": receipt["result_identity"], "current_process_execution_authority": False}
            return {"schema_version": 1, "command_id": command_id, "known": False, "accepted": False,
                    "current_process_execution_authority": False}

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
        if self.config.llm.is_cloud and self.tasks.task_use_policy(task_id) == "local_only":
            raise WorkflowBlocked("This source is local_only; cloud planning requires a new source version")
        snapshot = self.journal.get_plan(task_id)
        if snapshot is not None:
            self._verify_snapshot(snapshot)

    def submit(self, request: str, *, command_id: str | None = None,
               authorized_tools: Iterable[str] = (), high_risk_authorized_tools: Iterable[str] = (),
               dependencies: tuple[Any, ...] = (), use_policy: str | None = None,
               conversation_id: str | None = None,
               expected_conversation_epoch: int | None = None,
               expected_context_epoch: int | None = None) -> Task:
        self._ready()
        if not self.config.llm.ready:
            raise WorkflowBlocked("模型连接尚未配置，请在设置 → 模型与连接中完成配置。")
        resolved_policy = ("cloud_allowed" if self.config.llm.request_use_default else "local_only") if use_policy is None else use_policy
        if expected_context_epoch is not None:
            if expected_conversation_epoch is not None and expected_conversation_epoch != expected_context_epoch:
                raise ValueError("Conflicting conversation context epochs")
            expected_conversation_epoch = expected_context_epoch
        if (conversation_id is None) != (expected_conversation_epoch is None):
            raise ValueError("Conversation identity and expected epoch must be supplied together")

        submission_command = str(uuid4()) if command_id is None else command_id

        def register(task: Task) -> None:
            with self.store.read_connection() as connection:
                receipt = connection.execute("SELECT operation,result_identity FROM conversation_receipt WHERE command_id=?", (submission_command,)).fetchone()
                if receipt is not None and (receipt["operation"] != "submit" or receipt["result_identity"] != task.id):
                    raise ValueError("Command ID already belongs to another application operation")
            if conversation_id is not None:
                assert expected_conversation_epoch is not None
                self._conversation_repository.bind(task.id, conversation_id,
                    expected_conversation_epoch, command_id=submission_command)
                return
            prior = self._humanization.turn(task.id)
            identity = conversation_id or "single:" + task.id
            epoch = expected_conversation_epoch if expected_conversation_epoch is not None else 0
            if prior is not None:
                if prior["conversation_id"] != identity or prior["epoch"] != epoch:
                    raise ValueError("Command retry cannot change its conversation")
                return
            sequence = 1
            self._humanization.bind_turn(task.id, identity, epoch, sequence)
            for role in ("user", "assistant"):
                self.store.set_dependency_version("dialogue_turn", task.id + ":" + role,
                    sequence, use_policy=resolved_policy)

        return self._dispatcher.submit(request, command_id=submission_command,
            authorized_tools=AgentCore._authorization(authorized_tools),
            high_risk_authorized_tools=AgentCore._authorization(high_risk_authorized_tools),
            dependencies=dependencies, use_policy=resolved_policy, register=register)

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
        if action not in {"character_card", "reset"}:
            raise ValueError("Use the versioned interaction interface for this command")
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

    def conversation_ensure_default(self, command_id: str | None = None) -> dict[str, Any]:
        self._ready()
        return self._conversation_repository.ensure_default(command_id)

    def conversation_list(self, *, cursor: str | None = None, limit: int = 50,
                          archived: bool = False) -> dict[str, Any]:
        self._ready()
        return self._conversation_repository.page(cursor=cursor, limit=limit, archived=archived)

    def conversation_create(self, title: str, command_id: str) -> dict[str, Any]:
        self._ready()
        return self._conversation_repository.create(title, command_id)

    def conversation_update(self, id: str, expected_revision: int, command_id: str, *,
                            title: str | None = None, archived: bool | None = None) -> dict[str, Any]:
        self._ready()
        return self._conversation_repository.update(id, expected_revision, command_id, title=title, archived=archived)

    def conversation_timeline(self, id: str, *, before: int | None = None,
                              after: int | None = None, limit: int = 50,
                              known_revision: int | None = None) -> dict[str, Any]:
        self._ready()
        return self._conversation_repository.timeline(id, before=before, after=after,
            limit=limit, known_revision=known_revision)

    def conversation_item(self, id: str, item_id: str, *, offset: int = 0,
                          limit: int = 32768) -> dict[str, Any]:
        self._ready()
        return self._conversation_repository.detail(id, item_id, offset=offset, limit=limit)

    def conversation_task_location(self, id: str, task_id: str) -> dict[str, Any]:
        self._ready()
        return self._conversation_repository.task_location(id, task_id)

    def conversation_clear(self, id: str, expected_revision: int, command_id: str) -> dict[str, Any]:
        self._ready()
        return self._conversation_service.cleanup(id, expected_revision, command_id, delete=False)

    def conversation_delete(self, id: str, expected_revision: int, command_id: str) -> dict[str, Any]:
        self._ready()
        return self._conversation_service.cleanup(id, expected_revision, command_id, delete=True)

    def conversation_cleanup_status(self, job_id: str) -> dict[str, Any]:
        self._ready()
        return self._conversation_repository.cleanup_status(job_id)

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
        allowed = {"character_card", "reset"}
        if action not in allowed:
            raise ValueError("Unknown interaction command")
        runtime = self.core.companion_runtime
        if runtime is None:
            raise ValueError("Interaction settings are unavailable")
        current_epoch = int(self._humanization.privacy()["epoch"])
        with self._privacy.publication(current_epoch):
            runtime.command(action, **parameters)
        result = self.interaction_snapshot()
        result["privacy_invalidated"] = False
        result["privacy_epoch"] = int(self._humanization.privacy()["epoch"])
        return result

    def forget_during_task(self, memory_id: str, expected_revision: int, task_id: str) -> dict[str, Any]:
        """唯一执行线程内的已授权遗忘；本轮移除旧依赖后继续组织回复。"""
        turn = self._interaction.current_turn(task_id)
        with self._privacy.publication(turn.privacy_epoch):
            receipt = self._memory_runtime.forget(memory_id, expected_revision)
            self._interaction.settle_learning(task_id, {memory_id}, drop_summary=True)
            self.core._task_dependencies[task_id] = tuple(ref for ref in
                self.core._task_dependencies.get(task_id, ()) if ref[0] != memory_id)
            affected = self.journal.scrub_memory(memory_id, keep_task=task_id)
            self._conversation_repository.invalidate_memory_summaries(memory_id)
        self.core.revoke_pending(affected)
        self._dispatcher.clear_cached(affected)
        return dict(memory_id=memory_id, revision=receipt.revision, status='Completed',
            cleared=['指定记忆及其旧版本、检索索引'],
            retained=['聊天记录', '其他记忆与人格', '执行记录与日程提醒'])

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
                     plan_hash: str | None = None, expected_revision: int | None = None) -> AgentResult:
        self.enqueue_confirmation(task_id, authorized_tools=authorized_tools,
            high_risk_authorized_tools=high_risk_authorized_tools, plan_hash=plan_hash,
            expected_revision=expected_revision)
        return self.wait(task_id)

    def enqueue_confirmation(self, task_id: str, *, authorized_tools: Iterable[str] = (),
                             high_risk_authorized_tools: Iterable[str] = (),
                             plan_hash: str | None = None, command_id: str | None = None,
                             expected_revision: int | None = None) -> Task:
        """Validate and queue grants; return without waiting for the unique worker."""
        self._ready()
        authorization = AgentCore._authorization(authorized_tools)
        high_risk = AgentCore._authorization(high_risk_authorized_tools)
        if not isinstance(task_id, str) or not task_id or len(task_id) > 200:
            raise ValueError("Invalid task identity")
        if command_id is not None and (not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 200):
            raise ValueError("Confirmation command_id must be bounded and non-empty")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise ValueError("Confirmation requires a positive task revision")
        with self._confirmation_lock:
            expected = plan_hash if plan_hash is not None else self._reviewed.get(task_id)
            fingerprint = sha256(canonical({"operation": "confirm", "task_id": task_id,
                "plan_hash": expected, "expected_revision": expected_revision,
                "ordinary": sorted(authorization), "high_risk": sorted(high_risk)}).encode()).hexdigest()
            if command_id is not None:
                with self.store.transaction() as conn:
                    if conn.execute("SELECT 1 FROM conversation_receipt WHERE command_id=?", (command_id,)).fetchone() is not None:
                        raise ValueError("Command ID already belongs to another application operation")
                    previous = conn.execute("SELECT payload_hash,task_id FROM command WHERE id=?", (command_id,)).fetchone()
                    if previous is not None:
                        if previous["payload_hash"] != fingerprint or previous["task_id"] != task_id:
                            raise ValueError("Command ID already used with a different payload")
                        task = self.tasks.get(task_id)
                        assert task is not None
                        return task
            return self._enqueue_confirmation(task_id, authorization, high_risk, expected, command_id, fingerprint, expected_revision)

    def _enqueue_confirmation(self, task_id: str, authorization: frozenset[str], high_risk: frozenset[str],
                              expected: str | None, command_id: str | None, fingerprint: str,
                              expected_revision: int | None) -> Task:
        with self._dispatcher.control():
            # Queued/active acceptance is process authority, never reconstructed
            # from a receipt. A second command cannot replace those grants.
            if self._dispatcher.confirmation_state(task_id)["state"] == "accepted":
                raise WorkflowBlocked("This exact plan is already queued or executing")
            snapshot = self.journal.get_plan(task_id)
            if snapshot is None:
                raise WorkflowBlocked("No intact persisted plan")
            if len(snapshot.plan.steps) > self.core.max_tool_steps:
                raise WorkflowBlocked("任务计划超过步骤上限；请仅提出尚未完成的要求。")
            self._verify_snapshot(snapshot)
            if expected != snapshot.hash:
                raise WorkflowBlocked("Review the exact persisted plan and provide its plan hash")
            ordinary_required, high_required = required_permissions(snapshot, len(self.journal.attempts(task_id)))
            if authorization - ordinary_required or high_risk - high_required:
                raise ValueError("Tool grants must match the exact persisted plan and permission levels")
            old_ordinary, old_high = self.core.pending_grants(task_id)
            if ordinary_required - (authorization | old_ordinary) or high_required - (high_risk | old_high):
                raise WorkflowBlocked("Explicit grants for every required plan tool are required")
            self.store.validate_external(task_id)
            # Match submit/worker lock order: dispatcher control, then SQLite.
            queued = False
            try:
                with self.store.transaction() as conn:
                    if command_id is not None and (conn.execute("SELECT 1 FROM conversation_receipt WHERE command_id=?", (command_id,)).fetchone() is not None or conn.execute("SELECT 1 FROM command WHERE id=?", (command_id,)).fetchone() is not None):
                        raise ValueError("Command identity was concurrently used by another operation")
                    self.store.check_privacy(conn, task_id)
                    task = self.tasks.get(task_id)
                    if task is None:
                        raise KeyError(task_id)
                    if expected_revision is not None and task.revision != expected_revision:
                        raise WorkflowBlocked("Task revision changed; refresh the exact plan before confirming")
                    if task.status not in {TaskStatus.WAITING, TaskStatus.NEEDS_REVIEW} or task.cancel_requested:
                        raise WorkflowBlocked("Only an unstarted plan awaiting review may be confirmed")
                    if any(item.status not in {"Succeeded", "Failed"} for item in self.journal.attempts(task_id)):
                        raise WorkflowBlocked("Previously attempted plans cannot be resumed; create only the remaining intent")
                    self.store.check_dependencies(conn, task_id)
                    if task.status == TaskStatus.NEEDS_REVIEW:
                        # External authorities were checked before entering this
                        # transaction. Re-entering journal.resume_unstarted here
                        # would validate them while holding the workflow lock.
                        task = self.tasks.resume(task_id)
                    self.store.record_event("plan_confirmation", task_id, {"plan_hash": snapshot.hash})
                    if command_id is not None:
                        conn.execute("INSERT INTO command VALUES(?,?,?)", (command_id, fingerprint, task_id))
                    self._dispatcher.confirm(task_id, authorization, high_risk,
                                             _external_validated=True)
                    queued = True
            except BaseException:
                if queued:
                    self._dispatcher.clear_cached((task_id,))
                raise
            return task

    def cancel_task(self, task_id: str) -> Task:
        return self._cancel_task_impl(task_id)

    def append_requirement(self, task_id: str, content: str, command_id: str) -> dict[str, Any]:
        self._ready()
        # 短控制入口，不等待唯一执行 worker，也不产生第二个 Agent。
        with self._dispatcher.control():
            return self.journal.append_amendment(task_id, content, command_id)

    def submit_proactive(self, goal: str, basis: str, command_id: str, *, use_policy: str = "cloud_allowed") -> Task:
        """P5 协调器的统一入口：仍由 Dispatcher / Core 工作，不伪造用户消息。"""
        self._ready()
        if not goal.strip() or len(goal.encode("utf-8")) > 8192:
            raise ValueError("Invalid proactive goal")
        return self._dispatcher.submit(goal, command_id=command_id, authorized_tools=frozenset(),
            high_risk_authorized_tools=frozenset(), use_policy=use_policy, register=lambda task: self._register_proactive(task, basis, command_id))

    def _register_proactive(self, task: Task, basis: str, command_id: str) -> None:
        self.journal.set_origin(task.id, "proactive", basis)
        default = self._conversation_repository.ensure_default()
        self._conversation_repository.bind(task.id, default['id'], default['context_epoch'],
            command_id=command_id, proactive=True)

    def pause_task(self, task_id: str, command_id: str) -> Task:
        self._ready()
        with self._dispatcher.control():
            task = self.task_control.pause(task_id, command_id, active=self._dispatcher.diagnostics()["active_task_id"] == task_id)
            if task.status == TaskStatus.PAUSED:
                self._dispatcher.clear_cached((task_id,))
            self._dispatcher.wake()
            return task

    def continue_task(self, task_id: str, command_id: str) -> Task:
        self._ready()
        with self._dispatcher.control():
            if self._dispatcher.diagnostics()["active_task_id"] == task_id:
                raise WorkflowBlocked("正在完成暂停保存，请稍后继续。")
            self.store.validate_external(task_id)
            task = self.task_control.resume(task_id, command_id)
            self._dispatcher.clear_cached((task_id,))
            self._dispatcher.wake()
            return task

    def notification_control(self, kind: str, paused: bool, command_id: str,
                             until_at: str | None = None) -> dict[str, Any]:
        self._ready()
        return self.notification_policy.set(kind, paused, until_at, command_id)

    def answer_task(self, task_id: str, content: str, command_id: str) -> dict[str, Any]:
        self._ready()
        with self._dispatcher.control():
            with self.store.transaction() as conn:
                if conn.execute("SELECT 1 FROM task_amendment WHERE command_id=?", (command_id,)).fetchone():
                    return self.journal.append_amendment(task_id, content, command_id)
                task = self.tasks.get(task_id)
                if task is None or task.status != TaskStatus.AWAITING_INPUT:
                    raise WorkflowBlocked("只有等待补充信息的任务可以在这里续办。")
                receipt = self.journal.append_amendment(task_id, content, command_id)
                snapshot = self.journal.get_plan(task_id)
                if snapshot is None:
                    raise WorkflowBlocked("缺少可续办的上下文。")
                conn.execute("UPDATE task SET status='Waiting',revision=revision+1 WHERE id=?", (task_id,))
                self._dispatcher.confirm(task_id, frozenset(), frozenset())
            return receipt

    def _cancel_task_impl(self, task_id: str) -> Task:
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
                    self._conversation_repository.invalidate_memory_summaries(parameters['memory_id'])
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
                # 只撤销仍依赖已遗忘记忆的工作上下文，不擦除对话、执行记录或同轮学习。
                affected = set(self.journal.scrub_memory(job.memory_id))
                self._conversation_repository.invalidate_memory_summaries(job.memory_id)
                if isinstance(result, DeletionReceipt):
                    result = replace(result, affected_task_ids=tuple(sorted(affected)),
                        cleared=("selected memory, its revisions and retrieval index",),
                        retained=("chat history, execution records, other memories and personality",
                                  "goals, calendar, historical logs and backups"))
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
        if self.config.proactive.enabled:
            raise WorkflowBlocked("主动关注已改为自动复查，无需接受旧建议；实际操作仍单独确认。")
        task = self.proposals.accept(proposal_id, expected_revision, command_id=command_id)
        self._dispatcher.wake()
        return task

    def proposal_summary(self, limit: int = 10) -> Any:
        self._ready()
        return ()

    def flush_audit(self) -> None:
        with self._audit_lock:
            if self._cleanup_pending or self.journal.pending_deletions():
                return
            deliver_audit(self.store, self._audit_sink)

    def _maintenance(self) -> None:
        """Existing worker startup/idle boundary, without an active Core call."""
        if self._closing:
            return
        self._conversation_service.drain()
        if self._cleanup_pending or self.journal.pending_deletions():
            try:
                self.retry_cleanup()
            except RuntimeError:
                return
        if not self._cleanup_pending:
            try:
                self._humanization.discard_effects()
                self.learning.recover()
                self.learning.resume_work()
            finally:
                self.flush_audit()

    def _poll_rules(self) -> None:
        while not self._rule_stop.is_set():
            try:
                if not self._cleanup_pending:
                    self.proactive.poll()
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
                "reminders": self._reminders.diagnostics(),
                "model_calls": self._humanization.call_diagnostics(),
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
            self._reminder_pump.stop()
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
            if not self._reminder_pump.shutdown(max(0.0, deadline - monotonic())):
                return False
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
