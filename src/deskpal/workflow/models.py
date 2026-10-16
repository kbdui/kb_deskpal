"""Persistent workflow values. No value contains executable authorization."""

from dataclasses import dataclass
from typing import Any, Mapping

from deskpal.planner.models import Plan
from deskpal.tools.models import ToolResult, ToolSpec


class WorkflowBlocked(ValueError):
    """A dependency or persisted safety gate prevents further work."""


class TaskCancellationRequested(WorkflowBlocked):
    """A durable cancellation request reached an execution boundary."""


class QueueFull(ValueError):
    pass


@dataclass(frozen=True)
class DependencyRef:
    kind: str
    object_id: str
    revision: int


@dataclass(frozen=True)
class PlanSnapshot:
    id: str
    task_id: str
    hash: str
    canonical_json: str
    plan: Plan
    tool_specs: tuple[ToolSpec, ...]
    workspace_dir: str
    memory_refs: tuple[tuple[str, int], ...] = ()
    revision: int = 1


@dataclass(frozen=True)
class StepAttempt:
    id: str
    task_id: str
    index: int
    status: str
    result: ToolResult | None = None


@dataclass(frozen=True)
class AuditEvent:
    id: str
    event: str
    task_id: str | None
    data: Mapping[str, Any]


@dataclass(frozen=True)
class DeletionJob:
    id: str
    memory_id: str
    expected_revision: int
    status: str
