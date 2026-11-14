"""Agent 请求的同步协调器：组织规划、授权、工具执行和结果提交。"""

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
from deskpal.response.templates import terminal_response, tool_response_fallback
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
from .feedback import TurnFeedback


def _call_copy(call: ToolCall) -> ToolCall:
    """复制工具调用及参数，避免调用方修改 Agent 内部保存的计划。"""
    return ToolCall(call.name, _json_copy(call.arguments), call.call_id)


def _result_data(result: ToolResult) -> dict[str, Any]:
    """把工具结果转换为脱离原对象的 JSON 兼容快照。"""
    return {"success": result.success, "output": _json_copy(result.output), "error": result.error,
            "tool_name": result.tool_name, "call_id": result.call_id, "outcome": result.outcome}


class _Cancelled(Exception):
    """内部控制流信号：任务已取消，不应被当作普通执行失败。"""

    pass


class AgentCore:
    """执行单个静态计划的核心协调器。

    同一实例不允许并发或重入请求。具体依赖由外部注入；关闭时只调用显式
    交给本实例管理的清理回调，避免误关调用方拥有的资源。
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
        """保存请求依赖并初始化并发、授权计划及上下文状态。"""
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
        # 已确认计划依赖的记忆版本；版本变化或过期时必须撤销待执行计划。
        self._pending_dependencies: dict[str, tuple[tuple[str, int], ...]] = {}
        self._task_dependencies: dict[str, tuple[tuple[str, int], ...]] = {}
        self._interaction: TurnInteractionPort | None = None
        self._task_control: Any = None
        self._proactive_publication: Any = None
        self._publication_control: Any = None
        self._feedback: TurnFeedback | None = None

    def attach_feedback(self, feedback: TurnFeedback) -> None:
        with self._lock:
            if self._closed or self._running or self._feedback is not None:
                raise RuntimeError("Feedback ownership cannot be replaced")
            self._feedback = feedback

    @property
    def companion_runtime(self) -> CompanionPort | None:
        return self._companion

    def attach_interaction(self, interaction: TurnInteractionPort) -> None:
        """仅允许生产工厂在分发开始前设置唯一的应用交互协调器。"""
        with self._lock:
            if self._closed or self._running or self._interaction is not None:
                raise RuntimeError("Interaction ownership cannot be replaced")
            self._interaction = interaction

    @property
    def memory_runtime(self) -> MemoryRuntime | None:
        """返回可选的新记忆运行时，供应用层做生命周期协调。"""
        return self._memory_runtime

    def companion_snapshot(self) -> dict[str, Any]:
        """读取陪伴状态；存储故障时返回中性降级快照供界面展示。"""
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
        """串行执行显式陪伴设置命令，不允许与请求同时修改状态。"""
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
        """读取可选表达风格；状态不可用或代际变化时退回中性表达。"""
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


    def close(self) -> None:
        """关闭实例、清空进程内授权并释放明确归本实例所有的资源。"""
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
        """支持 with 用法，并拒绝重新进入已关闭的实例。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("AgentCore is closed")
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None,
                 traceback: TracebackType | None) -> None:
        """离开 with 代码块时关闭 AgentCore。"""
        self.close()

    def handle_request(self, request: str, *, authorized_tools: Iterable[str] = (),
                       high_risk_authorized_tools: Iterable[str] = ()) -> AgentResult:
        """兼容性别名；实际请求统一由 run 处理。"""
        return self.run(request, authorized_tools=authorized_tools,
                        high_risk_authorized_tools=high_risk_authorized_tools)

    @staticmethod
    def _authorization(names: Iterable[str]) -> frozenset[str]:
        """校验并冻结授权工具名，拒绝把单个字符串误当作名称集合。"""
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
        """只执行本实例保存的待确认计划；新授权仅追加到该任务。"""
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
                raise ValueError("Plan memory changed or expired; submit a new request and confirm its new plan")
            self._running = True
        try:
            if self._task_control is not None and self._interaction is not None:
                state = self._task_control.snapshot(task_id)
                if state['checkpoint'] == 'Waiting' and not state['requested']:
                    self._interaction.prepare_continuation(task_id)
            return self._run(request, old_authorization | authorization, old_high_risk | high_risk,
                             task_id=task_id, confirmed_plan=plan)
        finally:
            with self._lock:
                self._running = False
                self._release_durable_context(task_id)

    def cancel_task(self, task_id: str) -> Task:
        """先记录取消审计事件再更新状态；重复取消保持幂等。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("AgentCore is closed")
        current = self.tasks.get(task_id)
        if current is None:
            raise KeyError(task_id)
        if current.status == TaskStatus.CANCELLED:
            with self._lock:
                self._pending.pop(task_id, None)
            return current
        if current.status not in {TaskStatus.CREATED, TaskStatus.PLANNING,
                                  TaskStatus.RUNNING, TaskStatus.WAITING}:
            raise ValueError("A completed or failed task cannot be cancelled")
        self._event("task_status_changed", task_id,
                    {"previous_status": current.status.value, "status": TaskStatus.CANCELLED.value})
        result = self.tasks.update_status(task_id, TaskStatus.CANCELLED)
        with self._lock:
            self._pending.pop(task_id, None)
        return result

    def run(self, request: str, *, authorized_tools: Iterable[str] = (),
            high_risk_authorized_tools: Iterable[str] = ()) -> AgentResult:
        """提交新请求，检查输入和授权后进入单请求执行临界区。"""
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
        """执行已有排队任务，不重新提交或创建任务。

        单工作线程及当前进程授权由 Application 管理；恢复的计划必须先经过
        Application 对持久快照的审核。
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
            if self._task_control is not None:
                if self._task_control.checkpoint(task_id, "initial", 0):
                    return AgentResult(task_id, TaskStatus.PAUSED, "任务已暂停。")
                state = self._task_control.snapshot(task_id)
                if state['checkpoint'] == 'continue' and self._journal is not None:
                    snapshot = self._journal.get_plan(task_id)
                    if snapshot is None:
                        raise WorkflowBlocked('Missing paused task checkpoint')
                    confirmed_plan = snapshot.plan
                    self._task_dependencies[task_id] = snapshot.memory_refs
                    if self._interaction is not None:
                        self._interaction.prepare_continuation(task_id)
            return self._run(task.request, authorization, high_risk,
                             task_id=task_id, confirmed_plan=confirmed_plan)
        finally:
            with self._lock:
                self._running = False
                self._release_durable_context(task_id)

    def _release_durable_context(self, task_id: str) -> None:
        """任务不再等待授权时，释放内存计划和它引用的记忆版本。"""
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
        """恢复经审核且尚未开始的计划，并确保恢复后授权为空。"""
        with self._lock:
            if self._closed or self._running:
                raise RuntimeError("AgentCore is closed or already running")
            task = self.tasks.get(task_id)
            if task is None or task.status != TaskStatus.WAITING:
                raise ValueError("Only a waiting, reviewed plan can be restored")
            self._pending[task_id] = (task.request, self._plan_copy(plan), frozenset(), frozenset())
            self._pending_dependencies[task_id] = memory_refs
            self._task_dependencies[task_id] = memory_refs

    def pending_grants(self, task_id: str) -> tuple[frozenset[str], frozenset[str]]:
        """只读返回当前进程中的任务授权；持久执行回执不会构成授权。"""
        with self._lock:
            pending = self._pending.get(task_id)
            return (frozenset(), frozenset()) if pending is None else (pending[2], pending[3])

    def has_pending_plan(self, task_id: str) -> bool:
        """仅暴露是否存在待执行计划，不泄漏可变执行状态。"""
        with self._lock:
            return task_id in self._pending

    def revoke_pending(self, task_ids: Iterable[str]) -> None:
        """持久失效或删除发生后，撤销对应的进程内计划和授权。"""
        with self._lock:
            for task_id in task_ids:
                self._pending.pop(task_id, None)
                self._pending_dependencies.pop(task_id, None)

    def memory_command(self, action: str, **parameters: Any) -> Any:
        """通过串行入口执行显式记忆管理，不把记忆正文发送给模型或写入执行日志。

        普通请求此前已生成的历史日志和备份遵循各自的保留策略。
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
            result = getattr(runtime, action)(**parameters)
            if action in {"correct", "forget", "accept", "reject"}:
                self._invalidate_memory_plans()
            # 记忆遗忘只撤销依赖该版本的待执行计划，不清空历史任务或角色状态。
            self._log.record("memory_command", None, {"action": action})
            return result
        finally:
            with self._lock:
                self._running = False

    def _invalidate_memory_plans(self) -> None:
        """记忆被更正/审核/遗忘后，取消依赖已失效版本的待执行计划。"""
        runtime = self._memory_runtime
        if runtime is None:
            return
        with self._lock:
            candidates = tuple((task_id, self._pending_dependencies.get(task_id, ())) for task_id in self._pending)
        for task_id, refs in candidates:
            if not runtime.valid(refs):
                # Remove the grant-bearing snapshot before fallible cleanup.
                with self._lock:
                    self._pending.pop(task_id, None)
                    self._pending_dependencies.pop(task_id, None)
                current = self.tasks.get(task_id)
                if current is not None and current.status == TaskStatus.WAITING:
                    self.tasks.update_status(task_id, TaskStatus.CANCELLED)

    def _boundary(self, task_id: str) -> None:
        """在阶段边界集中检查取消、隐私屏障、执行日志和记忆有效性。"""
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
        """记录 JSON 快照，避免审计日志与调用方共享可变对象。"""
        self._log.record(event, task_id, _json_copy(data))

    def _stage(self, task_id: str, stage: str) -> None:
        """记录阶段开始，并在记录前后再次检查任务边界条件。"""
        self._boundary(task_id)
        self._event("stage_started", task_id, {"stage": stage})
        self._boundary(task_id)

    def _transition(self, task_id: str, status: TaskStatus, *, result: Any = None,
                    error: str | None = None, completed: bool = False) -> None:
        """审计并更新任务状态；完成状态会额外记录最终结果事件。"""
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
        """验证计划结构、调用数量与唯一标识，并复制所有参数。"""
        if (not isinstance(plan, Plan) or not isinstance(plan.reply, str)
                or not isinstance(plan.steps, (list, tuple))):
            raise ValueError("Invalid plan")
        if len(plan.steps) > self.max_tool_steps:
            raise ValueError("Plan exceeds max_tool_steps")
        if plan.context is not None and (not isinstance(plan.context, dict)
                or len(json.dumps(plan.context, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 16384):
            raise ValueError('Invalid context decision')
        if plan.action not in {"answer", "clarify", "quiet", "continue"}:
            raise ValueError("Unknown decision action")
        if type(plan.organize_memory) is not bool:
            raise ValueError('Invalid memory organization decision')
        if not plan.steps and not plan.reply.strip() and plan.action != "quiet" and plan.context is None:
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
        return Plan(plan.reply, tuple(calls), plan.action, _json_copy(plan.context) if plan.context is not None else None,
                    plan.organize_memory)

    def _planning_tools(self, task_id: str) -> tuple[Any, ...]:
        from .loop import READ_ONLY_TOOLS
        tools = tuple(self.tools.list_tools())
        if self._journal and self._journal.origin(task_id)['kind'] == 'proactive':
            return tuple(spec for spec in tools if spec.name not in {'memory_forget', 'role_update'} and
                (spec.name in READ_ONLY_TOOLS | {'attention_schedule', 'attention_end', 'reflection_record'} or int(spec.permission_level) > 0))
        return tuple(spec for spec in tools if spec.name != 'reflection_record')

    def _learning_references(self, task_id: str, references: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """在原有规划中提供有界未整理资料，不另起一次判断或据数量自动整理。"""
        preview = getattr(self._feedback, 'planning_materials', None)
        if not callable(preview):
            return references
        from deskpal.llm.context import references_view
        material = preview(task_id)
        if not material['items']:
            return references
        header: dict[str, Any] = dict(kind='unorganized_materials', instruction=False, items=[])
        admitted = []
        for item in material['items']:
            candidate = {**header, 'items': [*header['items'], {key: value for key, value in item.items() if key != 'refs'}]}
            encoded = json.dumps(references_view([*references, candidate]), ensure_ascii=False,
                                 separators=(',', ':')).encode('utf-8')
            if len(encoded) <= 60000:  # 为追加要求及实际观察留出空间，完整省略材料而不截断原话。
                header = candidate
                admitted.extend(item.get('refs', []))
        if header['items']:
            if self._journal:
                self._journal.set_dependencies(task_id, tuple(admitted))
            references.append(header)
        return references

    def _run(self, request: str, authorization: frozenset[str], high_risk: frozenset[str],
             *, task_id: str | None = None, confirmed_plan: Plan | None = None) -> AgentResult:
        """运行请求完整生命周期：观察、规划、授权、执行、表达、记忆与提交。"""
        with self._companion_lock:
            companion_generation = self._companion_generation
        if task_id is None:
            task_id = self.tasks.create(request).id
        results: list[ToolResult] = []
        warnings: list[str] = []
        memory_effects: list[str] = []
        bundle = RetrievalBundle()
        prepared: PreparedTurn | None = None
        response_signals: dict[str, Any] = {}
        phase = "observe"
        try:
            if confirmed_plan is None:
                # 新请求先建立任务并审计输入；记忆管理仍由模型规划对应工具。
                self._event("task_status_changed", task_id, {"previous_status": None, "status": TaskStatus.CREATED.value})
                self._stage(task_id, "Observe")
                self._event("request_received", task_id, {"request": request,
                                                          "authorized_tools": sorted(authorization),
                                                          "high_risk_authorized_tools": sorted(high_risk)})
                self._boundary(task_id)
                if self._memory_runtime is not None and self._interaction is None:
                    # 检索结果携带 revision 引用，后续计划确认和执行都会验证它们仍有效。
                    bundle = self._memory_runtime.retrieve(request)
                    warnings.extend(bundle.warnings)
                    memories = bundle.records
                    self._task_dependencies[task_id] = bundle.revisions
                    if self._journal is not None:
                        self._journal.set_dependencies(task_id, bundle.revisions)
                elif self._interaction is None:
                    memories = tuple(self.memory.search(request, limit=5))
                else:
                    memories = ()
                phase = "planning"
                # 统一准备角色、交流方式和前情检索；角色参与理解与规划，最后再组织表达。
                if self._interaction is not None:
                    prepared = self._interaction.prepare(task_id, request, bundle)
                    memories = prepared.memory_records
                    bundle = prepared.retrieval
                    self._task_dependencies[task_id] = bundle.revisions
                    if self._journal is not None:
                        self._journal.set_dependencies(task_id, bundle.revisions)
                    warnings.extend(prepared.warnings)
                self._transition(task_id, TaskStatus.PLANNING)
                self._stage(task_id, "Think")
                self._stage(task_id, "Plan")
                if self._memory_runtime is not None and not self._memory_runtime.valid(bundle.revisions):
                    raise ValueError("Memory context expired before planning")
                contextual = getattr(self.planner, "plan_context", None)
                if self._interaction is not None and callable(contextual):
                    self._event("model_decision_attempt", task_id, {"round": 0, "attempt": 1, "observation_count": 0})
                    initial_references = self._learning_references(task_id, _json_copy(list(prepared.planning_items)) if prepared else [])
                    if self._journal is not None:
                        initial_references.append({"kind": "current_task_amendments", "items": self._journal.consume_amendments(task_id, 0)})
                    plan = self._plan_copy(contextual(request, memories=memories,
                        tools=self._planning_tools(task_id), references=initial_references,
                        llm=self._interaction.calls(task_id, "planning")))
                    recheck = self._interaction.apply_decision(task_id, plan.context)
                    prepared = self._interaction.current_turn(task_id)
                    bundle, memories = prepared.retrieval, prepared.memory_records
                    self._task_dependencies[task_id] = bundle.revisions
                    if self._journal is not None:
                        self._journal.set_dependencies(task_id, bundle.revisions)
                    warnings.extend(item for item in prepared.warnings if item not in warnings)
                    if recheck or (not plan.steps and not plan.reply.strip() and plan.action != 'quiet'):
                        # 补查后的模型才决定行动；首份草稿和工具提议不会执行。
                        references = self._learning_references(task_id, _json_copy(list(prepared.planning_items)))
                        if self._journal is not None:
                            references.append({'kind': 'current_task_amendments', 'items': self._journal.consume_amendments(task_id, 0)})
                        self._event('model_decision_attempt', task_id, {'round': 0, 'attempt': 2, 'observation_count': 0})
                        plan = self._plan_copy(contextual(request, memories=memories,
                            tools=self._planning_tools(task_id), references=references,
                            llm=self._interaction.calls(task_id, 'planning')))
                else:
                    if prepared is not None and prepared.planning_items:
                        raise ValueError("This custom planner does not support historical execution context")
                    plan = self._plan_copy(self.planner.plan(request, memories=memories, tools=self.tools.list_tools()))
                if self._memory_runtime is not None and not self._memory_runtime.valid(bundle.revisions):
                    raise ValueError("Memory context changed while planning")
                self._boundary(task_id)
                if self._journal is not None:
                    plan = Plan(plan.reply, tuple(ToolCall(call.name, _json_copy(call.arguments), f"round-0-step-{index}")
                        for index, call in enumerate(plan.steps)), plan.action, organize_memory=plan.organize_memory)
                    snapshot = self._journal.save_plan(task_id, plan, tuple(self.tools.list_tools()),
                        str(self.workspace_dir), self._task_dependencies.get(task_id, ()))
                    plan = snapshot.plan
                self._event("plan_created", task_id, {"reply": plan.reply, "steps": [
                    {"name": call.name, "arguments": _json_copy(call.arguments), "call_id": call.call_id}
                    for call in plan.steps]})
            else:
                plan = self._plan_copy(confirmed_plan)
                current = self.tasks.get(task_id)
                if current is not None and current.status == TaskStatus.CREATED:
                    self._transition(task_id, TaskStatus.PLANNING)
                if self._interaction is not None:
                    prepared = self._interaction.prepare(task_id, request, bundle)
                memories = prepared.memory_records if prepared is not None else ()
                self._event("plan_confirmation_received", task_id,
                            {"authorized_tools": sorted(authorization),
                             "high_risk_authorized_tools": sorted(high_risk)})
            from .loop import execute_rounds
            loop_result = execute_rounds(self, task_id, request, plan, prepared,
                memories, authorization, high_risk, results, warnings,
                resuming=confirmed_plan is not None)
            if isinstance(loop_result, AgentResult):
                return loop_result
            plan = loop_result
            if self._interaction is not None:
                prepared = self._interaction.current_turn(task_id)
            if plan.action == "quiet" and (prepared is None or prepared.origin != "proactive"):
                raise ValueError("Only proactive tasks may end quietly")
            clarifying = plan.action == "clarify"
            current = self.tasks.get(task_id)
            if current is not None and current.status != TaskStatus.RUNNING:
                self._transition(task_id, TaskStatus.RUNNING)
            phase = "memory"
            # P4：只有统一反馈入口负责学习；工具明细保留在执行账本。
            skip_learning = not plan.organize_memory
            self._event('memory_learning_decided', task_id,
                        {'organize_memory': plan.organize_memory, 'final_action': plan.action})
            if skip_learning:
                self._event("memory_learning_skipped", task_id, {"reason": "model_deferred", "final_action": plan.action})
            if self._feedback is not None and not clarifying and not skip_learning:
                try:
                    warnings.extend(self._feedback.finish(task_id=task_id, request=request, response=plan.reply,
                        observations=tuple(_result_data(item) for item in results), context=prepared))
                except (TaskCancellationRequested, WorkflowBlocked):
                    raise
                except Exception:
                    warnings.append("本轮反馈整理失败；已完成操作保留，继续发布实际结果。")
            self._boundary(task_id)
            if self._interaction is not None and prepared is not None:
                # 学习可能通过既有受控入口刷新角色或撤下旧记忆引用。
                prepared = self._interaction.current_turn(task_id)
            learning_receipt: dict[str, Any] = ({"status": "skipped", "reason": "model_deferred", "saved_understandings": []}
                                               if skip_learning else {"status": "not_run", "saved_understandings": []})
            if self._feedback is not None and not clarifying and not skip_learning:
                receipt = getattr(self._feedback, 'receipt', None)
                if callable(receipt):
                    try:
                        learning_receipt = receipt(task_id)
                    except (TaskCancellationRequested, WorkflowBlocked):
                        raise
                    except Exception:
                        learning_receipt = {"status": "unavailable", "saved_understandings": []}
                        warnings.append("本轮学习回执暂不可用，不能确认保存成功。")
            phase = "reflection"
            # 工具结果是事实来源；回复模型只负责组织表达，不获得工具调用能力。
            self._stage(task_id, "Reflect")
            response = plan.reply
            style = prepared.style if prepared is not None else self._companion_style(
                request, companion_generation, warnings)
            if plan.action != "quiet" and (results or style or (prepared is not None and (prepared.history or prepared.role_json))):
                try:
                    self._boundary(task_id)
                    client = self._interaction.calls(task_id, "expression") if self._interaction else self.llm
                    composed = ResponseComposer(client).compose(request=request, draft=plan.reply,
                        style=style, history=tuple(dict(item) for item in prepared.history) if prepared else (),
                        memories=prepared.memories if prepared else (),
                        role_context=json.loads(prepared.role_json) if prepared and prepared.role_json else None,
                        conversation_summary=prepared.conversation_summary if prepared else None,
                        response_intent="clarify" if clarifying else "answer",
                        context_warnings=tuple(warnings), learning_receipt=learning_receipt,
                        tool_results=tuple(_result_data(item) for item in results),
                        structured=getattr(client, "supports_json_output", False) is True)
                    self._boundary(task_id)
                    # Only host-generated, bounded protocol paths/codes cross
                    # this boundary; provider text and exception details do not.
                    warnings.extend(
                        f"response_diagnostic:{item.group}:{item.field}:{item.code}"
                        for item in composed.diagnostics[:8]
                    )
                    if not composed.reply_text:
                        raise ValueError(composed.degraded_reason or "Empty expression response")
                    if self._companion_generation_current(companion_generation):
                        response = composed.reply_text
                        response_signals = {}
                        if composed.degraded_reason:
                            warnings.append(composed.degraded_reason)
                    else:
                        response = tool_response_fallback(tuple(_result_data(item) for item in results)) if results else "回复整理暂不可用，请重试。"
                        warnings.append("Expression was reset; generated text and signals were discarded")
                except (_Cancelled, TaskCancellationRequested):
                    raise
                except WorkflowBlocked:
                    # Privacy/source invalidation cannot fall back to a draft
                    # containing the same revoked data. Only expression-only
                    # changes permit a neutral, still-valid response.
                    if self._interaction is not None:
                        self._interaction.check(task_id)
                    response = tool_response_fallback(tuple(_result_data(item) for item in results)) if results else "回复整理暂不可用，请重试。"
                    warnings.append("Expression context changed; using neutral feedback")
                except Exception:
                    response = tool_response_fallback(tuple(_result_data(item) for item in results)) if results else "回复整理暂不可用，请重试。"
                    warnings.append("Response generation failed; authoritative evidence is unchanged")
            self._boundary(task_id)
            if prepared is not None and prepared.origin == 'proactive':
                if plan.action == 'quiet' or (self._proactive_publication is not None and not self._proactive_publication(task_id)):
                    response = ''
            phase = "completion audit and task storage"
            # 终态提交与对话发布按隐私屏障排序，避免清理后又发布旧上下文。
            with self._companion_lock, (self._publication_control() if self._publication_control is not None else nullcontext()), (self._interaction.publication(task_id)
                                       if self._interaction is not None else nullcontext()):
                # Order publication with privacy reset as well as generation.
                # Optional memory work may have outlasted the expression call.
                if companion_generation != self._companion_generation:
                    response = tool_response_fallback(tuple(_result_data(item) for item in results)) if results else "回复整理暂不可用，请重试。"
                    response_signals = {}
                    warnings.append("Expression context was reset before completion; optional expression was discarded")
                if self._interaction is not None:
                    try:
                        self._interaction.check(task_id, expression=True)
                    except WorkflowBlocked:
                        self._interaction.check(task_id)
                        response = tool_response_fallback(tuple(_result_data(item) for item in results)) if results else "回复整理暂不可用，请重试。"
                        response_signals = {}
                if self._journal is not None and self._journal.unconsumed_amendments(task_id):
                    warnings.append("有追加要求未赶上最后一次模型判断，未纳入本次执行；请继续发起续办。")
                if plan.action == 'quiet' or (prepared is not None and prepared.origin == 'proactive' and self._proactive_publication is not None and not self._proactive_publication(task_id)):
                    response = ''
                payload = {"response": response, "tool_results": [_result_data(item) for item in results],
                           "warnings": list(warnings), "memory_effects": memory_effects,
                           "final_action": plan.action, "organize_memory": plan.organize_memory,
                           "learning_receipt": learning_receipt}
                if clarifying:
                    # 问题也经过统一表达，但尚未完成的交流不触发反馈学习或完成发布。
                    self._transition(task_id, TaskStatus.AWAITING_INPUT, result=payload)
                    return AgentResult(task_id, TaskStatus.AWAITING_INPUT, response, tuple(results),
                        warnings=tuple(warnings))
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
            response = "执行依赖或用途已变化，需要人工复核。"
            self.tasks.update_status(task_id, TaskStatus.NEEDS_REVIEW,
                result={"response": response, "tool_results": [_result_data(item) for item in results]},
                error="Execution dependencies changed; review the source and create a new intent")
            return AgentResult(task_id, TaskStatus.NEEDS_REVIEW,
                response, tuple(results))
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
            from deskpal.llm.errors import RetryableLLMError
            return self._fail(task_id, error, results, retryable=isinstance(exc, RetryableLLMError))

    def _fail(self, task_id: str, error: str, results: list[ToolResult], *, retryable: bool = False) -> AgentResult:
        """将请求收敛到失败或不确定状态，避免重放可能已发生的外部副作用。"""
        with self._lock:
            self._pending.pop(task_id, None)
        if self._journal is not None:
            try:
                if any(item.status in {"Started", "Uncertain"} for item in self._journal.attempts(task_id)):
                    self._journal.mark_uncertain(task_id, error)
                    return AgentResult(task_id, TaskStatus.UNCERTAIN,
                        terminal_response("uncertain", tuple(_result_data(item) for item in results)), tuple(results), error=error)
            except Exception:
                # A broken ledger is never permission to execute the effect again.
                return AgentResult(task_id, TaskStatus.UNCERTAIN,
                    terminal_response("uncertain", tuple(_result_data(item) for item in results)), tuple(results), error=error)
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
        response = terminal_response("failed", tuple(_result_data(item) for item in results))
        try:
            latest = self.tasks.get(task_id)
            if latest is not None and latest.status == TaskStatus.CANCELLED:
                return self._cancelled_result(task_id, results)
            self.tasks.update_status(task_id, TaskStatus.FAILED,
                                     result={"response": response, "tool_results": [_result_data(item) for item in results], "retryable_model_failure": retryable}, error=error)
        except Exception:
            error += "; Failed task state could not be persisted"
        return AgentResult(task_id, TaskStatus.FAILED, response, tuple(results), error=error)

    def _cancelled_result(self, task_id: str, results: list[ToolResult]) -> AgentResult:
        """构造取消终态并尽力写审计事件；不把取消当作执行失败。"""
        with self._lock:
            self._pending.pop(task_id, None)
        response = terminal_response("cancelled", tuple(_result_data(item) for item in results))
        if self._journal is not None:
            self.tasks.update_status(task_id, TaskStatus.CANCELLED,
                result={"response": response, "tool_results": [_result_data(item) for item in results]})
        error = None
        try:
            self._event("request_cancelled", task_id,
                        {"status": TaskStatus.CANCELLED.value,
                         "tool_results": [_result_data(item) for item in results]})
        except Exception as exc:
            error = f"Cancellation audit could not be written ({type(exc).__name__})"
        return AgentResult(task_id, TaskStatus.CANCELLED, response, tuple(results), error=error)
