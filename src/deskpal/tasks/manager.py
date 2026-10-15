"""Synchronous, single-process task storage and audited state transitions."""

import math
from dataclasses import replace
from datetime import timedelta
from threading import RLock
from typing import Any
from uuid import uuid4

from deskpal.execution_log import ExecutionLog, NullExecutionLog

from .models import Task, TaskStatus, utc_now


_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset((TaskStatus.PLANNING, TaskStatus.CANCELLED, TaskStatus.FAILED)),
    TaskStatus.PLANNING: frozenset((TaskStatus.RUNNING, TaskStatus.WAITING, TaskStatus.FAILED, TaskStatus.CANCELLED)),
    TaskStatus.RUNNING: frozenset((TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)),
    TaskStatus.WAITING: frozenset((TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED)),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


def _copy_json(value: Any, active: set[int] | None = None) -> Any:
    """Return an independent JSON value, rejecting custom objects and cycles."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Task result numbers must be finite")
        return value
    if type(value) not in (dict, list):
        raise ValueError("Task result must contain only JSON values")
    seen = set() if active is None else active
    identity = id(value)
    if identity in seen:
        raise ValueError("Task result must not contain cycles")
    seen.add(identity)
    try:
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise ValueError("Task result object keys must be strings")
            return {key: _copy_json(item, seen) for key, item in value.items()}
        return [_copy_json(item, seen) for item in value]
    finally:
        seen.remove(identity)


def _snapshot(task: Task) -> Task:
    return replace(task, result=_copy_json(task.result))


class InMemoryTaskManager:
    """Keep isolated task snapshots; cancellation changes state only.

    A same-state update is an idempotent read: result, error and updated_at
    remain unchanged, including for terminal tasks. Other updates replace
    result/error with the supplied values (or their None defaults).
    """

    def __init__(self, *, execution_log: ExecutionLog | None = None) -> None:
        self._log = execution_log if execution_log is not None else NullExecutionLog()
        self._tasks: dict[str, Task] = {}
        self._lock = RLock()

    def create(self, request: str) -> Task:
        if not isinstance(request, str) or not request.strip():
            raise ValueError("Task request must be a non-empty string")
        with self._lock:
            task_id = str(uuid4())
            while task_id in self._tasks:
                task_id = str(uuid4())
            now = utc_now()
            task = Task(task_id, request, created_at=now, updated_at=now)
            self._log.record("task_status_changed", task.id,
                             {"previous_status": None, "status": task.status.value, "request": task.request})
            self._tasks[task.id] = task
            return _snapshot(task)

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return None if task is None else _snapshot(task)

    def update_status(self, task_id: str, status: TaskStatus, *,
                      result: Any = None, error: str | None = None) -> Task:
        if not isinstance(status, TaskStatus):
            raise ValueError("Task status must be a TaskStatus member")
        with self._lock:
            current = self._tasks[task_id]
            if status == current.status:
                return _snapshot(current)
            if status not in _TRANSITIONS[current.status]:
                raise ValueError(f"Cannot transition task from {current.status.value} to {status.value}")
            if error is not None and not isinstance(error, str):
                raise ValueError("Task error must be a string or None")
            saved_result = _copy_json(result)
            now = utc_now()
            if now <= current.updated_at:
                now = current.updated_at + timedelta(microseconds=1)
            updated = replace(current, status=status, updated_at=now, result=saved_result, error=error)
            # Audit owns a separate copy: a logger cannot mutate stored results.
            # Exceptions propagate before the task's state is committed.
            self._log.record("task_status_changed", task_id,
                             {"previous_status": current.status.value, "status": status.value,
                              "result": _copy_json(saved_result), "error": error})
            self._tasks[task_id] = updated
            return _snapshot(updated)

    def list_tasks(self) -> tuple[Task, ...]:
        with self._lock:
            return tuple(_snapshot(task) for task in self._tasks.values())

    def scrub(self, task_id: str) -> Task:
        """Erase local copied content without pretending to erase historical logs."""
        with self._lock:
            current = self._tasks[task_id]
            cleared = replace(current, request="[forgotten]", result=None, error=None)
            self._tasks[task_id] = cleared
            return _snapshot(cleared)
