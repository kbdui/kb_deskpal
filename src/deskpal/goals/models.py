"""Explicit user-owned objectives; memory categories are never authority here."""

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class GoalStatus(str, Enum):
    ACTIVE = "Active"
    PAUSED = "Paused"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"


class MilestoneStatus(str, Enum):
    PENDING = "Pending"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"


class BindingStatus(str, Enum):
    ACTIVE = "Active"
    REMOVED = "Removed"


class GoalConflict(ValueError):
    """A stale revision or terminal object cannot overwrite current state."""


@dataclass(frozen=True)
class Goal:
    id: str
    title: str
    success_criteria: str
    deadline: str | None
    use_policy: str
    status: GoalStatus
    revision: int
    execution_version: int
    progress_note: str
    completion_evidence: str
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Milestone:
    id: str
    goal_id: str
    title: str
    success_criteria: str
    deadline: str | None
    status: MilestoneStatus
    revision: int
    execution_version: int
    progress_note: str
    completion_evidence: str
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SourceBinding:
    id: str
    goal_id: str
    milestone_id: str | None
    source: str
    source_id: str
    revision: int
    status: BindingStatus
    use_policy: str
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
