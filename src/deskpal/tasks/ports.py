from typing import Any, Protocol, Sequence

from .models import Task, TaskStatus


class TaskManager(Protocol):
    def create(self, request: str) -> Task: ...

    def get(self, task_id: str) -> Task | None: ...

    def update_status(self, task_id: str, status: TaskStatus, *, result: Any = None, error: str | None = None) -> Task: ...

    def list_tasks(self) -> Sequence[Task]: ...

