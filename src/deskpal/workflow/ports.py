"""Execution-facing persistence boundary; Core never imports a SQLite adapter."""

from typing import Protocol

from deskpal.planner.models import Plan
from deskpal.tasks.models import Task
from deskpal.tools.models import ToolResult, ToolSpec

from .models import PlanSnapshot, StepAttempt


class ExecutionJournalPort(Protocol):
    def save_plan(self, task_id: str, plan: Plan, tool_specs: tuple[ToolSpec, ...],
                  workspace_dir: str, memory_refs: tuple[tuple[str, int], ...] = ()) -> PlanSnapshot: ...

    def get_plan(self, task_id: str) -> PlanSnapshot | None: ...

    def check_task(self, task_id: str) -> Task: ...

    def set_dependencies(self, task_id: str, refs: tuple[tuple[str, int], ...]) -> None: ...

    def start_step(self, task_id: str, index: int) -> StepAttempt: ...

    def finish_step(self, attempt_id: str, result: ToolResult) -> None: ...

    def attempts(self, task_id: str) -> tuple[StepAttempt, ...]: ...

    def mark_uncertain(self, task_id: str, error: str) -> Task: ...
