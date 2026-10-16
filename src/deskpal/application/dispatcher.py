"""One execution worker; SQLite remains the authority for queued work."""

from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from threading import Condition, Thread, current_thread
from time import monotonic
from typing import Any

from deskpal.agent.core import AgentCore
from deskpal.agent.models import AgentResult
from deskpal.tasks.models import Task, TaskStatus
from deskpal.tools.models import ToolResult
from deskpal.workflow.journal import ExecutionJournal
from deskpal.workflow.models import QueueFull, TaskCancellationRequested, WorkflowBlocked
from deskpal.workflow.tasks import PersistentTaskManager
from deskpal.workflow.store import canonical


_RETURNABLE = frozenset({TaskStatus.WAITING, TaskStatus.NEEDS_REVIEW, TaskStatus.UNCERTAIN,
                         TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})
_TERMINAL = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})


class DispatcherFailed(RuntimeError):
    """Execution stopped safely after an observable worker failure."""


def stored_result(task: Task, journal: ExecutionJournal) -> AgentResult:
    payload = task.result if isinstance(task.result, dict) else {}
    results = tuple(ToolResult(**value) for value in payload.get("tool_results", [])
                    if isinstance(value, dict) and "success" in value)
    warnings = tuple(payload.get("warnings", []))
    if payload.get("output_omitted") or not results:
        attempts = journal.attempts(task.id)
        known = tuple(item for item in attempts if item.status in {"Succeeded", "Failed"})
        if payload.get("output_omitted") or known:
            results = tuple(ToolResult(item.status == "Succeeded", {"output_omitted": True},
                                      tool_name=item.result.tool_name if item.result else "",
                                      call_id=item.result.call_id if item.result else "") for item in known)
            warnings += ("Result body omitted; persisted step outcomes are retained",)
    if payload.get("warning"):
        warnings += (str(payload["warning"]),)
    snapshot = journal.get_plan(task.id) if task.status == TaskStatus.WAITING else None
    return AgentResult(task.id, task.status,
        str(payload.get("response", payload.get("reply", task.error or task.status.value))), results,
        required_authorizations=tuple(payload.get("required_authorizations", [])), error=task.error,
        required_high_risk_authorizations=tuple(payload.get("required_high_risk_authorizations", [])),
        planned_calls=snapshot.plan.steps if snapshot else (), warnings=warnings,
        memory_effects=tuple(payload.get("memory_effects", [])))


class SubmissionDispatcher:
    def __init__(self, core: AgentCore, tasks: PersistentTaskManager, journal: ExecutionJournal,
                 *, guard: Callable[[str], None], on_idle: Callable[[], None]) -> None:
        self.core, self.tasks, self.journal = core, tasks, journal
        self._guard, self._on_idle = guard, on_idle
        self._condition = Condition()
        self._stop = False
        self._paused = False
        self._active_id: str | None = None
        self._operations: dict[str, tuple[bool, frozenset[str], frozenset[str]]] = {}
        self._results: OrderedDict[str, AgentResult] = OrderedDict()
        self._thread = Thread(target=self._work, name="deskpal-workflow", daemon=True)
        self._started = False
        self._failure: tuple[str, str] | None = None
        self._stage = "start"

    def start(self) -> None:
        with self._condition:
            self._accepting()
            if not self._started:
                try:
                    self._thread.start()
                except BaseException as exc:
                    self._fail("start", exc)
                    raise
                self._started = True

    def submit(self, request: str, *, command_id: str, authorized_tools: frozenset[str],
               high_risk_authorized_tools: frozenset[str], dependencies: tuple[Any, ...] = (),
               use_policy: str = "cloud_allowed") -> Task:
        with self._condition:
            self._accepting()
            self._prune_operations()
            registered: str | None = None
            try:
                with self.tasks.store.transaction():
                    task = self.tasks.submit(request, command_id=command_id,
                                             dependencies=dependencies, use_policy=use_policy)
                    # claim_next can commit while Task still says Created. Its
                    # popped grants belong solely to the active execution.
                    if task.status == TaskStatus.CREATED and task.id != self._active_id and task.id not in self._operations:
                        if len(self._operations) >= self.tasks.store.queue_capacity:
                            raise QueueFull("In-memory submission metadata is full")
                        self._operations[task.id] = (False, authorized_tools, high_risk_authorized_tools)
                        registered = task.id
            except BaseException:
                if registered is not None:
                    self._operations.pop(registered, None)
                raise
            # A command retry cannot add grants to a task already executing or
            # awaiting confirmation; it returns the original submission only.
            self._condition.notify_all()
            return task

    def confirm(self, task_id: str, authorization: frozenset[str], high_risk: frozenset[str]) -> None:
        with self._condition:
            self._accepting()
            self._prune_operations()
            if task_id not in self._operations and len(self._operations) >= self.tasks.store.queue_capacity:
                raise QueueFull("In-memory submission metadata is full")
            self.tasks.requeue(task_id)
            self._results.pop(task_id, None)
            self._operations[task_id] = (True, authorization, high_risk)
            self._condition.notify_all()

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def clear_cached(self, task_ids: Iterable[str] | None = None) -> None:
        with self._condition:
            if task_ids is None:
                self._results.clear()
            for task_id in (() if task_ids is None else tuple(task_ids)):
                self._results.pop(task_id, None)
                self._operations.pop(task_id, None)
            self._condition.notify_all()

    @contextmanager
    def management(self) -> Iterator[None]:
        """Serialize short local management with claiming, never with control writes."""
        with self._condition:
            if self._stop or self._active_id is not None or self._paused:
                raise RuntimeError("Application is closing or an execution is active")
            self._paused = True
        try:
            yield
        finally:
            with self._condition:
                self._paused = False
                self._condition.notify_all()

    def wait(self, task_id: str, timeout: float | None = None) -> AgentResult:
        deadline = None if timeout is None else monotonic() + timeout
        with self._condition:
            while True:
                if self._failure is not None:
                    stage, error_type = self._failure
                    raise DispatcherFailed(f"Execution worker stopped during {stage} ({error_type}); inspect task recovery state")
                result = self._results.get(task_id)
                if result is not None and task_id != self._active_id:
                    return deepcopy(result)
                task = self.tasks.get(task_id)
                if task is None:
                    raise KeyError(task_id)
                if task.status in _RETURNABLE and task_id != self._active_id and task_id not in self._operations:
                    return stored_result(task, self.journal)
                if self._stop and task_id != self._active_id:
                    raise RuntimeError("Application stopped before this queued submission ran")
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("Task is still queued or running")
                self._condition.wait(min(remaining, 0.2) if remaining is not None else 0.2)

    def stop_accepting(self) -> None:
        with self._condition:
            self._stop = True
            active = self._active_id
            self._condition.notify_all()
        if active is not None:
            try:
                self.tasks.request_cancel(active)
            except Exception as exc:
                # A failed control write cannot prevent joining the actual
                # running call or authorize releasing its live dependencies.
                self._fail("shutdown_cancel", exc)

    def shutdown(self, timeout: float) -> bool:
        self.stop_accepting()
        if self._started:
            if current_thread() is self._thread:
                return False
            self._thread.join(timeout)
        return not self._started or not self._thread.is_alive()

    def diagnostics(self) -> dict[str, Any]:
        with self._condition:
            return {"started": self._started, "worker_alive": self._thread.is_alive(),
                    "stopping": self._stop, "failed": self._failure is not None,
                    "error_stage": None if self._failure is None else self._failure[0],
                    "error_type": None if self._failure is None else self._failure[1],
                    "active_task_id": self._active_id, "pending_operations": len(self._operations),
                    "cached_results": len(self._results)}

    def _accepting(self) -> None:
        if self._failure is not None:
            raise DispatcherFailed("Execution worker failed; close and review persisted recovery state")
        if self._stop:
            raise RuntimeError("Application is closing")

    def _fail(self, stage: str, error: BaseException) -> None:
        with self._condition:
            if self._failure is None:
                self._failure = (stage, type(error).__name__)
            self._stop = True
            self._operations.clear()
            self._condition.notify_all()

    def _prune_operations(self) -> None:
        # Cancelled/invalidated queued submissions may never be claimed.
        # Their grants must not accumulate beyond the persistent queue bound.
        for task_id in tuple(self._operations):
            task = self.tasks.get(task_id)
            if task_id == self._active_id or task is None or task.status not in {TaskStatus.CREATED, TaskStatus.WAITING}:
                self._operations.pop(task_id, None)

    def _interrupted(self, task_id: str, error: Exception) -> tuple[AgentResult, bool]:
        attempts = self.journal.attempts(task_id)
        if any(item.status in {"Started", "Uncertain"} for item in attempts):
            task = self.journal.mark_uncertain(task_id, "Execution worker interrupted")
            return stored_result(task, self.journal), True
        current = self.tasks.get(task_id)
        if current is None:
            raise KeyError(task_id)
        if current.status in _TERMINAL:
            return stored_result(current, self.journal), False
        if isinstance(error, TaskCancellationRequested) or current.cancel_requested:
            task = self.tasks.update_status(task_id, TaskStatus.CANCELLED, result=current.result)
        else:
            task = self.tasks.block_for_review(task_id, "Execution interrupted; explicit review required")
        return stored_result(task, self.journal), False

    def _work(self) -> None:
        try:
            self._work_until_stopped()
        except BaseException as exc:
            # Last-resort containment also covers result classification and
            # bookkeeping failures, never just exceptions from the Core call.
            self._fail(self._stage, exc)
        finally:
            with self._condition:
                self._active_id = None
                self._condition.notify_all()

    def _work_until_stopped(self) -> None:
        while True:
            with self._condition:
                if self._stop:
                    return
                if self._paused:
                    self._condition.wait(0.2)
                    continue
                # Claim under this control lock so shutdown cannot pass between
                # checking _stop and ownership of a new active task.
                self._stage = "claim"
                task = self.tasks.claim_next()
                self._prune_operations()
                if task is None:
                    self._condition.wait(0.2)
                    continue
                self._active_id = task.id
                confirm, authorization, high_risk = self._operations.pop(
                    task.id, (False, frozenset(), frozenset()))
            try:
                self._stage = "execution"
                self._guard(task.id)
                if confirm:
                    result = self.core.confirm_task(task.id, authorized_tools=authorization,
                                                   high_risk_authorized_tools=high_risk)
                else:
                    result = self.core.execute_task(task.id, authorized_tools=authorization,
                                                   high_risk_authorized_tools=high_risk)
            except Exception as exc:
                self._stage = "execution_outcome"
                result, uncertain = self._interrupted(task.id, exc)
                if uncertain or not isinstance(exc, WorkflowBlocked):
                    self._fail("execution", exc)
            with self._condition:
                self._stage = "finish_submission"
                current = self.tasks.get(task.id)
                if current is not None and current.status != result.status:
                    result = stored_result(current, self.journal)
                status = "Blocked" if result.status in {TaskStatus.WAITING, TaskStatus.NEEDS_REVIEW,
                                                        TaskStatus.UNCERTAIN} else "Finished"
                self.tasks.finish_submission(task.id, status=status)
                try:
                    cacheable = len(canonical(asdict(result)).encode("utf-8")) <= self.tasks.store.result_max_bytes
                except (TypeError, ValueError):
                    cacheable = False
                if cacheable:
                    self._results[task.id] = deepcopy(result)
                while len(self._results) > 128:
                    self._results.popitem(last=False)
                self._active_id = None
                self._condition.notify_all()
            try:
                self._on_idle()
            except Exception:
                # Audit delivery and local suggestions cannot replay the task.
                pass
