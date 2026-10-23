"""Synchronous request orchestration, depending only on DTOs and protocols."""

import json
import re
from collections.abc import Callable, Iterable, Mapping
from contextlib import nullcontext
from pathlib import Path
from threading import Lock, RLock
from types import TracebackType
from typing import Any

from deskpal._json import snapshot_json as _json_copy
from deskpal.execution_log import ExecutionLog, NullExecutionLog
from deskpal.llm.errors import LLMError
from deskpal.llm.ports import LLMProvider
from deskpal.memory.models import MemoryKind, MemoryRecord
from deskpal.memory.ports import MemoryManager
from deskpal.memory.contracts import MemoryRuntime, RetrievalBundle, DeletionReceipt, UsePolicy
from deskpal.planner.models import Plan
from deskpal.planner.ports import Planner
from deskpal.response.composer import ResponseComposer
from deskpal.response.templates import tool_summary
from deskpal.sandbox.errors import PathViolation, PermissionDenied, ToolNotFound
from deskpal.tasks.models import Task, TaskStatus
from deskpal.tasks.ports import TaskManager
from deskpal.tools.models import PermissionLevel, ToolCall, ToolContext, ToolResult
from deskpal.tools.ports import ToolManager
from deskpal.workflow.models import TaskCancellationRequested, WorkflowBlocked
from deskpal.workflow.ports import ExecutionJournalPort

from .models import AgentResult, AuthorizationRequest
from .ports import CompanionPort
from .context import PreparedTurn, TurnInteractionPort


def _call_copy(call: ToolCall) -> ToolCall:
    return ToolCall(call.name, _json_copy(call.arguments), call.call_id)


def _result_data(result: ToolResult) -> dict[str, Any]:
    return {"success": result.success, "output": _json_copy(result.output), "error": result.error,
            "tool_name": result.tool_name, "call_id": result.call_id}


class _Cancelled(Exception):
    pass


class AgentCore:
    """Run one static plan. This instance rejects concurrent or reentrant runs.

    Injected dependencies remain available for task and memory inspection. Resource
    ownership is explicit: only the supplied on_close callback is invoked on close.
    """

    def __init__(self, *, llm: LLMProvider, planner: Planner, tasks: TaskManager,
                 memory: MemoryManager, tools: ToolManager, workspace_dir: str | Path,
                 max_tool_steps: int = 8, execution_log: ExecutionLog | None = None,
                 on_close: Callable[[], None] | None = None,
                 memory_runtime: MemoryRuntime | None = None,
                 journal: ExecutionJournalPort | None = None,
                 execution_guard: Callable[[str], None] | None = None,
                 companion_runtime: CompanionPort | None = None,
                 companion_cloud: bool = True,
                 companion_unavailable: bool = False) -> None:
        if type(max_tool_steps) is not int or max_tool_steps <= 0:
            raise ValueError("max_tool_steps must be a positive integer")
        if not isinstance(workspace_dir, (str, Path)) or not str(workspace_dir).strip():
            raise ValueError("workspace_dir must be a non-empty string or Path")
        if on_close is not None and not callable(on_close):
            raise ValueError("on_close must be callable")
        if type(companion_cloud) is not bool:
            raise ValueError("companion_cloud must be a boolean")
        self.llm, self.planner, self.tasks = llm, planner, tasks
        self.memory, self.tools = memory, tools
        self._memory_runtime = memory_runtime
        self._journal = journal
        self._execution_guard = execution_guard
        self._companion = companion_runtime
        self._companion_unavailable = companion_unavailable
        self._companion_degraded = False
        self._companion_cloud = companion_cloud
        self._companion_lock = RLock()
        self._companion_generation = 0
        self.workspace_dir = Path(workspace_dir).resolve()
        self.max_tool_steps = max_tool_steps
        self._log = execution_log if execution_log is not None else NullExecutionLog()
        self._on_close = on_close
        self._lock = Lock()
        self._closed = False
        self._running = False
        # Private snapshots never share argument dictionaries with returned DTOs.
        self._pending: dict[str, tuple[str, Plan, frozenset[str], frozenset[str]]] = {}
        self._pending_dependencies: dict[str, tuple[tuple[str, int], ...]] = {}
        self._task_dependencies: dict[str, tuple[tuple[str, int], ...]] = {}
        self._interaction: TurnInteractionPort | None = None

    @property
    def companion_runtime(self) -> CompanionPort | None:
        return self._companion

    def attach_interaction(self, interaction: TurnInteractionPort) -> None:
        """The production factory attaches one owner before starting dispatch."""
        with self._lock:
            if self._closed or self._running or self._interaction is not None:
                raise RuntimeError("Interaction ownership cannot be replaced")
            self._interaction = interaction

    @property
    def memory_runtime(self) -> MemoryRuntime | None:
        return self._memory_runtime

    def companion_snapshot(self) -> dict[str, Any]:
        with self._companion_lock:
            if self._closed:
                raise RuntimeError("AgentCore is closed")
            if self._companion is not None:
                try:
                    snapshot: dict[str, Any] = _json_copy(self._companion.snapshot())
                    if self._companion_degraded:
                        # The store is readable: retain real configuration/CAS
                        # so explicit settings can recover a transient fault.
                        # Only optional expression is masked in this projection.
                        snapshot["affect"] = {"valence": 0.0, "arousal": 0.0, "mood": 0.0,
                                              "mode": "neutral", "expression": "neutral"}
                        snapshot["cue"] = None
                        warnings = list(snapshot.get("warnings", ()))
                        if "companion_expression_degraded" not in warnings:
                            warnings.append("companion_expression_degraded")
                        snapshot["warnings"] = warnings
                    return snapshot
                except Exception:
                    self._companion_degraded = True
            return {"schema_version": 1, "revision": 0, "reset_epoch": 0,
                    "enabled": False, "emotions_enabled": False,
                    "persona": {"version": 1, "preset": "balanced",
                                "style": {"formality": 0.5, "warmth": 0.5, "humor": 0.2,
                                          "verbosity": 0.5, "expressiveness": 0.4, "curiosity": 0.3}},
                    "persona_versions": [], "preferences": {}, "constraints": {"humor_allowed": False},
                    "cue": None,
                    "affect": {"valence": 0.0, "arousal": 0.0, "mood": 0.0,
                               "mode": "neutral", "expression": "neutral"},
                    "warnings": ["companion_storage_unavailable"]}

    def companion_command(self, action: str, **parameters: Any) -> dict[str, Any]:
        with self._lock:
            if self._closed or self._running:
                raise RuntimeError("AgentCore is closed or has a running request")
            with self._companion_lock:
                if self._companion is None:
                    raise ValueError("Companion management is unavailable")
                result: dict[str, Any] = _json_copy(self._companion.command(action, **parameters))
                self._companion_degraded = False
                return result

    def reset_companion_for_privacy(self) -> None:
        """Invalidate in-flight projections before fallible local cleanup.

        This lock orders reset against outcome writes. A running request keeps
        its admission generation and cannot restore state after the barrier.
        Reset failure propagates so deletion remains pending.
        """
        with self._companion_lock:
            self._companion_generation += 1
            if self._companion_unavailable:
                self._companion_degraded = True
                raise RuntimeError("Companion privacy reset is unavailable; cleanup remains pending")
            if self._companion is not None:
                try:
                    self._companion.reset_for_privacy()
                except Exception:
                    self._companion_degraded = True
                    raise
                self._companion_degraded = False

    def _companion_style(self, request: str, generation: int, warnings: list[str]) -> str:
        with self._companion_lock:
            if self._companion is None or self._companion_degraded or generation != self._companion_generation:
                return ""
            try:
                if self._companion.snapshot().get("enabled") is not True:
                    return ""
                instruction = self._companion.style_instruction(request, cloud=self._companion_cloud)
                if not isinstance(instruction, str) or len(instruction) > 4096:
                    raise ValueError("Invalid companion style instruction")
                return instruction
            except Exception:
                self._companion_degraded = True
                warnings.append("Optional companion expression unavailable; using neutral response style")
                return ""

    def _companion_generation_current(self, generation: int) -> bool:
        with self._companion_lock:
            return generation == self._companion_generation

    def _record_companion_outcome(self, task_id: str, results: list[ToolResult], generation: int) -> None:
        with self._companion_lock:
            if self._companion is None or generation != self._companion_generation:
                return
            try:
                current = self.tasks.get(task_id)
                if current is None or current.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
                    return
                self._companion.record_outcome(task_id, current.status.value,
                    tool_success=all(item.success for item in results) if results else None)
                self._companion_degraded = False
            except Exception:
                # Optional state never changes task truth or retries an effect.
                self._companion_degraded = True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._running:
                raise RuntimeError("Cannot close AgentCore while a request is running")
            self._closed = True
            self._pending.clear()
            self._pending_dependencies.clear()
            self._task_dependencies.clear()
            if self._interaction is not None:
                self._interaction.close()
            # Cleanup can fail after releasing some resources; the facade must
            # remain closed instead of accepting requests against partial state.
            if self._on_close is not None:
                with self._companion_lock:
                    self._on_close()

    def __enter__(self) -> "AgentCore":
        with self._lock:
            if self._closed:
                raise RuntimeError("AgentCore is closed")
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()

    def handle_request(self, request: str, *, authorized_tools: Iterable[str] = (),
                       high_risk_authorized_tools: Iterable[str] = ()) -> AgentResult:
        return self.run(request, authorized_tools=authorized_tools,
                        high_risk_authorized_tools=high_risk_authorized_tools)

    @staticmethod
    def _authorization(names: Iterable[str]) -> frozenset[str]:
        if isinstance(names, (str, bytes)):
            raise ValueError("Authorization must be an iterable of tool names")
        try:
            result = frozenset(names)
        except (TypeError, ValueError):
            raise ValueError("Authorization must contain tool names") from None
        if any(not isinstance(name, str) or not name.strip() for name in result):
            raise ValueError("Authorization must contain non-empty tool names")
        return result

    def confirm_task(self, task_id: str, *, authorized_tools: Iterable[str] = (),
                     high_risk_authorized_tools: Iterable[str] = ()) -> AgentResult:
        """Execute only this instance's stored plan, using task-scoped grants."""
        authorization = self._authorization(authorized_tools)
        high_risk = self._authorization(high_risk_authorized_tools)
        with self._lock:
            if self._closed:
                raise RuntimeError("AgentCore is closed")
            if self._running:
                raise RuntimeError("AgentCore already has a running request")
            task = self.tasks.get(task_id)
            pending = self._pending.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.status != TaskStatus.WAITING or pending is None:
                self._pending.pop(task_id, None)
                raise ValueError("Task has no pending plan in this AgentCore")
            request, plan, old_authorization, old_high_risk = pending
            if self._memory_runtime is not None and not self._memory_runtime.valid(
                    self._pending_dependencies.get(task_id, ())):
                self._pending.pop(task_id, None)
                self._pending_dependencies.pop(task_id, None)
                self.tasks.update_status(task_id, TaskStatus.CANCELLED)
                scrub = getattr(self.tasks, "scrub", None)
                if callable(scrub):
                    scrub(task_id)
                raise ValueError("Plan memory changed or expired; submit a new request and confirm its new plan")
            self._running = True
        try:
            return self._run(request, old_authorization | authorization, old_high_risk | high_risk,
                             task_id=task_id, confirmed_plan=plan)
        finally:
            with self._lock:
                self._running = False
                self._release_durable_context(task_id)

    def cancel_task(self, task_id: str) -> Task:
        """Audit cancellation before storage; repeated cancellation is idempotent."""
        with self._lock:
            if self._closed:
                raise RuntimeError("AgentCore is closed")
        current = self.tasks.get(task_id)
        if current is None:
            raise KeyError(task_id)
        if current.status == TaskStatus.CANCELLED:
            self._pending.pop(task_id, None)
            return current
        if current.status not in {TaskStatus.CREATED, TaskStatus.PLANNING,
                                  TaskStatus.RUNNING, TaskStatus.WAITING}:
            raise ValueError("A completed or failed task cannot be cancelled")
        self._event("task_status_changed", task_id,
                    {"previous_status": current.status.value, "status": TaskStatus.CANCELLED.value})
        result = self.tasks.update_status(task_id, TaskStatus.CANCELLED)
        self._pending.pop(task_id, None)
        return result

    def run(self, request: str, *, authorized_tools: Iterable[str] = (),
            high_risk_authorized_tools: Iterable[str] = ()) -> AgentResult:
        if not isinstance(request, str) or not request.strip():
            raise ValueError("Request must be a non-empty string")
        authorization = self._authorization(authorized_tools)
        high_risk = self._authorization(high_risk_authorized_tools)
        with self._lock:
            if self._closed:
                raise RuntimeError("AgentCore is closed")
            if self._running:
                raise RuntimeError("AgentCore already has a running request")
            self._running = True
        try:
            return self._run(request, authorization, high_risk)
        finally:
            with self._lock:
                self._running = False

    def execute_task(self, task_id: str, *, authorized_tools: Iterable[str] = (),
                     high_risk_authorized_tools: Iterable[str] = (),
                     confirmed_plan: Plan | None = None) -> AgentResult:
        """Execute an existing queued task without submitting or creating another.

        Application owns the single worker and supplies grants only from this
        process. A recovered plan must first pass Application's snapshot review.
        """
        authorization = self._authorization(authorized_tools)
        high_risk = self._authorization(high_risk_authorized_tools)
        with self._lock:
            if self._closed or self._running:
                raise RuntimeError("AgentCore is closed or already running")
            task = self.tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            self._running = True
        try:
            return self._run(task.request, authorization, high_risk,
                             task_id=task_id, confirmed_plan=confirmed_plan)
        finally:
            with self._lock:
                self._running = False
                self._release_durable_context(task_id)

    def _release_durable_context(self, task_id: str) -> None:
        if self._journal is None:
            return
        try:
            task = self.tasks.get(task_id)
        except Exception:
            task = None
        if task is None or task.status != TaskStatus.WAITING:
            self._pending.pop(task_id, None)
            self._pending_dependencies.pop(task_id, None)
            self._task_dependencies.pop(task_id, None)

    def restore_pending(self, task_id: str, plan: Plan,
                        memory_refs: tuple[tuple[str, int], ...] = ()) -> None:
        """Restore a reviewed, never-started snapshot with empty grants."""
        with self._lock:
            if self._closed or self._running:
                raise RuntimeError("AgentCore is closed or already running")
            task = self.tasks.get(task_id)
            if task is None or task.status != TaskStatus.WAITING:
                raise ValueError("Only a waiting, reviewed plan can be restored")
            self._pending[task_id] = (task.request, self._plan_copy(plan), frozenset(), frozenset())
            self._pending_dependencies[task_id] = memory_refs
            self._task_dependencies[task_id] = memory_refs

    def revoke_pending(self, task_ids: Iterable[str]) -> None:
        """Drop in-memory grants after a durable invalidation or deletion."""
        with self._lock:
            for task_id in task_ids:
                self._pending.pop(task_id, None)
                self._pending_dependencies.pop(task_id, None)

    def memory_command(self, action: str, **parameters: Any) -> Any:
        """Run explicit local memory management through the serialized facade.

        Payloads are not sent to the LLM or copied into execution logs. Historical
        logs/backups already created by ordinary requests have separate retention.
        """
        runtime = self._memory_runtime
        if runtime is None:
            raise ValueError("V0.3 memory management is unavailable on this custom agent")
        allowed = {"remember", "list_items", "profile", "accept", "reject", "correct", "forget", "export", "rebuild_index"}
        if action not in allowed:
            raise ValueError("Unknown memory command")
        with self._lock:
            if self._closed or self._running:
                raise RuntimeError("Agent is closed or has a running request")
            self._running = True
        try:
            if action == "forget":
                self.reset_companion_for_privacy()
            result = getattr(runtime, action)(**parameters)
            if action in {"correct", "forget", "accept", "reject"}:
                self._invalidate_memory_plans()
            if isinstance(result, DeletionReceipt):
                affected = set(result.affected_task_ids)
                for task_id, refs in self._task_dependencies.items():
                    if not runtime.valid(refs):
                        affected.add(task_id)
                scrub = getattr(self.tasks, "scrub", None)
                if callable(scrub):
                    for task_id in affected:
                        if self.tasks.get(task_id) is not None:
                            scrub(task_id)
                else:
                    from dataclasses import replace
                    result = replace(result, retained=result.retained + ("custom task storage",))
            self._log.record("memory_command", None, {"action": action})
            return result
        finally:
            with self._lock:
                self._running = False

    def _invalidate_memory_plans(self) -> None:
        runtime = self._memory_runtime
        if runtime is None:
            return
        for task_id in tuple(self._pending):
            if not runtime.valid(self._pending_dependencies.get(task_id, ())):
                # Remove the grant-bearing snapshot before fallible cleanup.
                self._pending.pop(task_id, None)
                self._pending_dependencies.pop(task_id, None)
                current = self.tasks.get(task_id)
                if current is not None and current.status == TaskStatus.WAITING:
                    self.tasks.update_status(task_id, TaskStatus.CANCELLED)
                scrub = getattr(self.tasks, "scrub", None)
                if callable(scrub):
                    scrub(task_id)

    def _boundary(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            raise RuntimeError("Task storage lost the current task")
        if task.status == TaskStatus.CANCELLED or task.cancel_requested:
            raise _Cancelled
        if self._execution_guard is not None:
            self._execution_guard(task_id)
        if self._interaction is not None:
            self._interaction.check(task_id)
        if self._journal is not None:
            try:
                self._journal.check_task(task_id)
            except TaskCancellationRequested:
                raise _Cancelled from None
        refs = self._task_dependencies.get(task_id, ())
        if refs and self._memory_runtime is not None and not self._memory_runtime.valid(refs):
            raise ValueError("Memory dependencies changed or expired")

    def _event(self, event: str, task_id: str, data: Mapping[str, Any]) -> None:
        self._log.record(event, task_id, _json_copy(data))

    def _stage(self, task_id: str, stage: str) -> None:
        self._boundary(task_id)
        self._event("stage_started", task_id, {"stage": stage})
        self._boundary(task_id)

    def _transition(self, task_id: str, status: TaskStatus, *, result: Any = None,
                    error: str | None = None, completed: bool = False) -> None:
        self._boundary(task_id)
        current = self.tasks.get(task_id)
        if current is None:
            raise RuntimeError("Task storage lost the current task")
        self._event("task_status_changed", task_id, {"previous_status": current.status.value,
                                                    "status": status.value, "result": result, "error": error})
        if completed:
            self._boundary(task_id)
            self._event("request_completed", task_id, {"status": status.value, "result": result})
        self._boundary(task_id)
        self.tasks.update_status(task_id, status, result=_json_copy(result), error=error)

    def _plan_copy(self, plan: Plan) -> Plan:
        if (not isinstance(plan, Plan) or not isinstance(plan.reply, str)
                or not isinstance(plan.steps, (list, tuple))):
            raise ValueError("Invalid plan")
        if len(plan.steps) > self.max_tool_steps:
            raise ValueError("Plan exceeds max_tool_steps")
        if not plan.steps and not plan.reply.strip():
            raise ValueError("Plan must contain a reply or tool calls")
        calls: list[ToolCall] = []
        ids: set[str] = set()
        for call in plan.steps:
            if (not isinstance(call, ToolCall) or not isinstance(call.name, str) or not call.name.strip()
                    or not isinstance(call.call_id, str) or not isinstance(call.arguments, Mapping)):
                raise ValueError("Invalid planned tool call")
            if call.call_id and call.call_id in ids:
                raise ValueError("Duplicate planned call_id")
            ids.add(call.call_id)
            calls.append(_call_copy(call))
        return Plan(plan.reply, tuple(calls))

    def _run(self, request: str, authorization: frozenset[str], high_risk: frozenset[str],
             *, task_id: str | None = None, confirmed_plan: Plan | None = None) -> AgentResult:
        with self._companion_lock:
            companion_generation = self._companion_generation
        explicit_fact = re.match(r"^记住(?:：|:|\s+)([\s\S]+)$", request.strip())
        explicit_memory = self._memory_runtime is not None and explicit_fact is not None
        # Failure before create returns has no valid task id and propagates clearly.
        if task_id is None:
            task_id = self.tasks.create("[explicit memory request]" if explicit_memory else request).id
        results: list[ToolResult] = []
        warnings: list[str] = []
        memory_effects: list[str] = []
        bundle = RetrievalBundle()
        prepared: PreparedTurn | None = None
        response_signals: dict[str, Any] = {}
        phase = "observe"
        try:
            if confirmed_plan is None:
                self._event("task_status_changed", task_id, {"previous_status": None, "status": TaskStatus.CREATED.value})
                self._stage(task_id, "Observe")
                self._event("request_received", task_id, {"request": "[explicit memory request]" if explicit_memory else request,
                                                          "authorized_tools": sorted(authorization),
                                                          "high_risk_authorized_tools": sorted(high_risk)})
                self._boundary(task_id)
                fact = re.match(r"^记住(?:：|:|\s+)([\s\S]+)$", request.strip())
                if self._memory_runtime is not None and fact and fact[1].strip():
                    # Explicit storage must not depend on planning or cloud health.
                    self._transition(task_id, TaskStatus.PLANNING)
                    self._transition(task_id, TaskStatus.RUNNING)
                    saved_item = self._memory_runtime.remember(fact[1].strip(), task_id=task_id,
                        use_policy=UsePolicy(self._interaction.source_policy(task_id))
                        if self._interaction is not None else UsePolicy.CLOUD_ALLOWED)
                    self._task_dependencies[task_id] = ((saved_item.id, saved_item.revision),)
                    if self._journal is not None:
                        self._journal.set_dependencies(task_id, self._task_dependencies[task_id])
                    response = "已保存用户陈述：" + saved_item.draft.content
                    self._event("memory_saved", task_id, {"memory_id": saved_item.id, "kind": saved_item.draft.kind.value})
                    self._transition(task_id, TaskStatus.COMPLETED, result={"memory_id": saved_item.id}, completed=True)
                    return AgentResult(task_id, TaskStatus.COMPLETED, response,
                                       memory_effects=("saved:" + saved_item.id,))
                if self._memory_runtime is not None:
                    bundle = (self._interaction.retrieve(request) if self._interaction is not None
                              else self._memory_runtime.retrieve(request))
                    warnings.extend(bundle.warnings)
                    memories = bundle.records
                    self._task_dependencies[task_id] = bundle.revisions
                    if self._journal is not None:
                        self._journal.set_dependencies(task_id, bundle.revisions)
                else:
                    memories = tuple(self.memory.search(request, limit=5))
                phase = "planning"
                if self._interaction is not None:
                    prepared = self._interaction.prepare(task_id, request, bundle)
                    warnings.extend(prepared.warnings)
                self._transition(task_id, TaskStatus.PLANNING)
                self._stage(task_id, "Think")
                self._stage(task_id, "Plan")
                if self._memory_runtime is not None and not self._memory_runtime.valid(bundle.revisions):
                    raise ValueError("Memory context expired before planning")
                contextual = getattr(self.planner, "plan_context", None)
                if self._interaction is not None and callable(contextual):
                    plan = self._plan_copy(contextual(request, memories=memories,
                        tools=self.tools.list_tools(), references=prepared.planning_items if prepared else (),
                        llm=self._interaction.calls(task_id, "planning")))
                else:
                    if prepared is not None and prepared.planning_items:
                        raise ValueError("This custom planner does not support historical execution context")
                    plan = self._plan_copy(self.planner.plan(request, memories=memories, tools=self.tools.list_tools()))
                if self._memory_runtime is not None and not self._memory_runtime.valid(bundle.revisions):
                    raise ValueError("Memory context changed while planning")
                self._boundary(task_id)
                if self._journal is not None:
                    snapshot = self._journal.save_plan(task_id, plan, tuple(self.tools.list_tools()),
                        str(self.workspace_dir), self._task_dependencies.get(task_id, ()))
                    plan = snapshot.plan
                self._event("plan_created", task_id, {"reply": plan.reply, "steps": [
                    {"name": call.name, "arguments": _json_copy(call.arguments), "call_id": call.call_id}
                    for call in plan.steps]})
            else:
                plan = self._plan_copy(confirmed_plan)
                if self._interaction is not None:
                    prepared = self._interaction.prepare(task_id, request, bundle)
                self._event("plan_confirmation_received", task_id,
                            {"authorized_tools": sorted(authorization),
                             "high_risk_authorized_tools": sorted(high_risk)})
            phase = "permission preflight"
            context = ToolContext(task_id, self.workspace_dir, authorization, high_risk)
            required: list[str] = []
            required_high_risk: list[str] = []
            details: list[AuthorizationRequest] = []
            specs = {spec.name: spec for spec in self.tools.list_tools()}
            for call in plan.steps:
                self._boundary(task_id)
                try:
                    self.tools.check_permission(_call_copy(call), context)
                except PermissionDenied:
                    if call.name not in required:
                        required.append(call.name)
                    level = specs[call.name].permission_level
                    if level == PermissionLevel.HIGH_RISK and call.name not in required_high_risk:
                        required_high_risk.append(call.name)
                    details.append(AuthorizationRequest(call.name, _json_copy(call.arguments), call.call_id, level))
            self._boundary(task_id)
            if required:
                response = "Explicit authorization required for: " + ", ".join(required) + ". Confirm this task's displayed plan."
                current = self.tasks.get(task_id)
                if current is None or current.status != TaskStatus.WAITING:
                    self._transition(task_id, TaskStatus.WAITING, result={
                        "required_authorizations": required, "required_high_risk_authorizations": required_high_risk})
                self._pending[task_id] = (request, self._plan_copy(plan), authorization, high_risk)
                self._pending_dependencies[task_id] = self._task_dependencies.get(task_id, ())
                self._boundary(task_id)
                return AgentResult(task_id, TaskStatus.WAITING, response,
                                   required_authorizations=tuple(required),
                                   required_high_risk_authorizations=tuple(required_high_risk),
                                   authorization_requests=tuple(details),
                                   planned_calls=tuple(_call_copy(call) for call in plan.steps),
                                   warnings=tuple(warnings))
            self._pending.pop(task_id, None)
            phase = "tool execution"
            self._transition(task_id, TaskStatus.RUNNING)
            self._stage(task_id, "Act")
            for index, call in enumerate(plan.steps):
                self._boundary(task_id)
                attempt = self._journal.start_step(task_id, index) if self._journal is not None else None
                raw = self.tools.execute(_call_copy(call), context)
                if (not isinstance(raw, ToolResult) or type(raw.success) is not bool
                        or (raw.error is not None and not isinstance(raw.error, str))
                        or not isinstance(raw.tool_name, str) or not isinstance(raw.call_id, str)
                        or (raw.success and raw.error is not None)):
                    raise ValueError("Invalid tool result")
                result = ToolResult(raw.success, _json_copy(raw.output), raw.error, raw.tool_name, raw.call_id)
                if self._journal is not None and attempt is not None:
                    self._journal.finish_step(attempt.id, result)
                results.append(result)
                self._boundary(task_id)
                if not result.success:
                    return self._fail(task_id, result.error or "Tool execution failed", results)
            phase = "reflection"
            self._stage(task_id, "Reflect")
            response = plan.reply
            style = prepared.style if prepared is not None else self._companion_style(
                request, companion_generation, warnings)
            if results or style or (prepared is not None and prepared.history):
                try:
                    self._boundary(task_id)
                    client = self._interaction.calls(task_id, "expression") if self._interaction else self.llm
                    composed = ResponseComposer(client).compose(request=request, draft=plan.reply,
                        style=style, history=tuple(dict(item) for item in prepared.history) if prepared else (),
                        memories=prepared.memories if prepared else (),
                        tool_results=tuple(_result_data(item) for item in results),
                        structured=bool(style) and self._interaction is not None)
                    self._boundary(task_id)
                    # Only host-generated, bounded protocol paths/codes cross
                    # this boundary; provider text and exception details do not.
                    warnings.extend(
                        f"response_diagnostic:{item.group}:{item.field}:{item.code}"
                        for item in composed.diagnostics[:8]
                    )
                    if not composed.reply_text and (not results or composed.degraded_reason):
                        raise ValueError(composed.degraded_reason or "Empty expression response")
                    if self._companion_generation_current(companion_generation):
                        if results:
                            response = tool_summary(tuple(_result_data(item) for item in results))
                            if composed.reply_text:
                                response += "\n补充说明：" + composed.reply_text
                        else:
                            response = composed.reply_text
                        response_signals = {"cues": composed.cues,
                                            "preference_candidates": composed.preference_candidates}
                        if composed.degraded_reason:
                            warnings.append(composed.degraded_reason)
                    else:
                        response = tool_summary(tuple(_result_data(item) for item in results)) if results else plan.reply
                        warnings.append("Expression was reset; generated text and signals were discarded")
                except (_Cancelled, TaskCancellationRequested):
                    raise
                except WorkflowBlocked:
                    # Privacy/source invalidation cannot fall back to a draft
                    # containing the same revoked data. Only expression-only
                    # changes permit a neutral, still-valid response.
                    if self._interaction is not None:
                        self._interaction.check(task_id)
                    response = tool_summary(tuple(_result_data(item) for item in results)) if results else plan.reply
                    warnings.append("Expression context changed; using neutral feedback")
                except Exception:
                    if results and self._memory_runtime is None:
                        raise
                    response = tool_summary(tuple(_result_data(item) for item in results)) if results else plan.reply
                    warnings.append("Response generation failed; authoritative evidence is unchanged")
            self._boundary(task_id)
            phase = "memory"
            fact = re.match(r"^记住(?:：|:|\s+)([\s\S]+)$", request.strip())
            if self._memory_runtime is not None:
                self._boundary(task_id)
                if results and getattr(self._memory_runtime, "mode", "explicit_only") != "paused":
                    # Keep complete bounded metadata, never raw file contents/pixels.
                    summary = json.dumps({"steps": [{"tool_name": item.tool_name, "call_id": item.call_id,
                        "success": item.success} for item in results]}, ensure_ascii=False, allow_nan=False)
                    try:
                        saved_item = self._memory_runtime.record_execution(task_id, summary,
                            use_policy=UsePolicy(prepared.use_policy) if prepared else UsePolicy.CLOUD_ALLOWED)
                        memory_effects.append("saved:" + saved_item.id)
                        self._event("memory_saved", task_id, {"memory_id": saved_item.id, "kind": "episodic"})
                    except Exception:
                        warnings.append("Tool execution completed; optional episode storage failed")
                # Cancellation is control flow, never an optional-memory warning.
                self._boundary(task_id)
                try:
                    controlled_learn = getattr(self._memory_runtime, "learn_with_client", None)
                    if self._interaction is not None and callable(controlled_learn):
                        warnings.extend(controlled_learn(request, task_id,
                            client=self._interaction.calls(task_id, "memory_evaluation"),
                            guard=lambda: self._boundary(task_id),
                            use_policy=UsePolicy(prepared.use_policy) if prepared else UsePolicy.CLOUD_ALLOWED,
                            excluded_profile_keys=(prepared.consumed_profile_keys if prepared else frozenset())
                                | frozenset(item.key for item in response_signals.get("preference_candidates", ())
                                            if item.key in {"preferred_name", "reply_language", "verbosity"})))
                    elif self._interaction is not None:
                        warnings.append("Custom memory learning does not support controlled calls; skipped")
                    else:
                        warnings.extend(self._memory_runtime.learn(request, task_id))
                except Exception:
                    warnings.append("Optional memory evaluation failed; execution results are unchanged")
                self._boundary(task_id)
            elif fact and fact[1].strip():
                self._boundary(task_id)
                saved = self.memory.save(MemoryRecord(fact[1].strip()[:2000], kind=MemoryKind.SEMANTIC, task_id=task_id))
                self._boundary(task_id)
                self._event("memory_saved", task_id, {"memory_id": saved.id, "kind": saved.kind.value})
            if results and self._memory_runtime is None:
                self._boundary(task_id)
                summary = json.dumps({"request": request[:500], "tool_results": [_result_data(item) for item in results]},
                                     ensure_ascii=False, allow_nan=False)[:2000]
                saved = self.memory.save(MemoryRecord(summary, kind=MemoryKind.EPISODIC, task_id=task_id))
                self._boundary(task_id)
                self._event("memory_saved", task_id, {"memory_id": saved.id, "kind": saved.kind.value})
            self._boundary(task_id)
            phase = "completion audit and task storage"
            with self._companion_lock, (self._interaction.publication(task_id)
                                       if self._interaction is not None else nullcontext()):
                # Order publication with privacy reset as well as generation.
                # Optional memory work may have outlasted the expression call.
                if companion_generation != self._companion_generation:
                    response = tool_summary(tuple(_result_data(item) for item in results)) if results else plan.reply
                    response_signals = {}
                    warnings.append("Expression context was reset before completion; optional expression was discarded")
                if self._interaction is not None:
                    try:
                        self._interaction.check(task_id, expression=True)
                    except WorkflowBlocked:
                        self._interaction.check(task_id)
                        response = tool_summary(tuple(_result_data(item) for item in results)) if results else plan.reply
                        response_signals = {}
                payload = {"response": response, "tool_results": [_result_data(item) for item in results],
                           "warnings": list(warnings), "memory_effects": memory_effects}
                self._transition(task_id, TaskStatus.COMPLETED, result=payload, completed=True)
                if self._interaction is not None and prepared is not None:
                    latest = self.tasks.get(task_id)
                    if latest is not None and latest.status == TaskStatus.COMPLETED:
                        try:
                            self._interaction.publish(prepared, request, response,
                                facts=tuple(_result_data(item) for item in results), signals=response_signals)
                        except Exception:
                            # Optional presentation/context cannot rewrite a
                            # reliably committed task or replay its tools.
                            warnings.append("Reply committed; optional interaction publication was incomplete")
            if self._journal is not None:
                latest = self.tasks.get(task_id)
                if latest is not None and latest.status == TaskStatus.CANCELLED:
                    return self._cancelled_result(task_id, results)
            return AgentResult(task_id, TaskStatus.COMPLETED, response, tuple(results),
                               warnings=tuple(warnings), memory_effects=tuple(memory_effects))
        except _Cancelled:
            return self._cancelled_result(task_id, results)
        except TaskCancellationRequested:
            return self._cancelled_result(task_id, results)
        except WorkflowBlocked:
            if self._journal is None:
                raise
            if any(item.status in {"Started", "Uncertain"} for item in self._journal.attempts(task_id)):
                return self._fail(task_id, "Execution dependencies changed; outcome requires review", results)
            self.tasks.update_status(task_id, TaskStatus.NEEDS_REVIEW,
                result={"tool_results": [_result_data(item) for item in results]},
                error="Execution dependencies changed; review the source and create a new intent")
            return AgentResult(task_id, TaskStatus.NEEDS_REVIEW,
                "执行依赖或用途已变化，需要人工复核。", tuple(results))
        except Exception as exc:
            if isinstance(exc, ToolNotFound):
                error = "Unknown tool in plan"
            elif isinstance(exc, PermissionDenied):
                error = "Tool authorization was denied during execution"
            elif isinstance(exc, PathViolation):
                error = "File path violates the workspace policy"
            elif isinstance(exc, LLMError):
                error = str(exc)
            elif isinstance(exc, ValueError):
                error = f"Invalid data during {phase}"
            else:
                error = f"Request failed during {phase} ({type(exc).__name__})"
            return self._fail(task_id, error, results)
        finally:
            if self._interaction is None:
                self._record_companion_outcome(task_id, results, companion_generation)

    def _fail(self, task_id: str, error: str, results: list[ToolResult]) -> AgentResult:
        self._pending.pop(task_id, None)
        if self._journal is not None:
            try:
                if any(item.status in {"Started", "Uncertain"} for item in self._journal.attempts(task_id)):
                    self._journal.mark_uncertain(task_id, error)
                    return AgentResult(task_id, TaskStatus.UNCERTAIN,
                        "工具调用结果未能可靠记录，请人工核对；系统不会自动重试。", tuple(results), error=error)
            except Exception:
                # A broken ledger is never permission to execute the effect again.
                return AgentResult(task_id, TaskStatus.UNCERTAIN,
                    "执行账本不可用，请人工核对；系统不会自动重试。", tuple(results), error=error)
        # A second audit/storage failure must not hide the primary failure. Failure
        # persistence uses a single attempt, without recursively retrying transitions.
        try:
            current = self.tasks.get(task_id)
        except Exception:
            current = None
            error += "; task state could not be read"
        if current is not None and current.status == TaskStatus.CANCELLED:
            return self._cancelled_result(task_id, results)
        try:
            self._event("request_failed", task_id, {"error": error, "tool_results": [_result_data(item) for item in results]})
            self._event("task_status_changed", task_id, {"previous_status": current.status.value if current else None,
                                                       "status": TaskStatus.FAILED.value, "error": error})
        except Exception:
            error += "; failure audit could not be written"
        try:
            latest = self.tasks.get(task_id)
            if latest is not None and latest.status == TaskStatus.CANCELLED:
                return self._cancelled_result(task_id, results)
            self.tasks.update_status(task_id, TaskStatus.FAILED,
                                     result={"tool_results": [_result_data(item) for item in results]}, error=error)
        except Exception:
            error += "; Failed task state could not be persisted"
        return AgentResult(task_id, TaskStatus.FAILED, error, tuple(results), error=error)

    def _cancelled_result(self, task_id: str, results: list[ToolResult]) -> AgentResult:
        self._pending.pop(task_id, None)
        if self._journal is not None:
            self.tasks.update_status(task_id, TaskStatus.CANCELLED,
                result={"tool_results": [_result_data(item) for item in results]})
        error = None
        response = "Task cancelled."
        try:
            self._event("request_cancelled", task_id,
                        {"status": TaskStatus.CANCELLED.value,
                         "tool_results": [_result_data(item) for item in results]})
        except Exception as exc:
            error = f"Cancellation audit could not be written ({type(exc).__name__})"
            response += " " + error
        return AgentResult(task_id, TaskStatus.CANCELLED, response, tuple(results), error=error)
