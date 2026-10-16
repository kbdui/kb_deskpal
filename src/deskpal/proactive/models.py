"""Local suggestions are invitations to review, never executable grants."""

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class ProposalStatus(str, Enum):
    PENDING = "Pending"
    ACCEPTED = "Accepted"
    IGNORED = "Ignored"
    EXPIRED = "Expired"
    INVALIDATED = "Invalidated"


@dataclass(frozen=True)
class Proposal:
    id: str
    revision: int
    status: ProposalStatus
    goal_id: str
    goal_execution_version: int
    milestone_id: str | None
    milestone_execution_version: int | None
    binding_id: str | None
    binding_revision: int | None
    rule_id: str
    root_source_key: str
    kind: str
    reason: str
    request: str
    due_at: str
    expires_at: str
    use_policy: str
    source_snapshot_json: str | None
    accepted_task_id: str | None
    created_at: str
    updated_at: str

    @property
    def dependencies(self) -> tuple[tuple[str, str, int], ...]:
        refs = [("goal", self.goal_id, self.goal_execution_version)]
        if self.milestone_id is not None and self.milestone_execution_version is not None:
            refs.append(("milestone", self.milestone_id, self.milestone_execution_version))
        if self.binding_id is not None and self.binding_revision is not None:
            refs.append(("binding", self.binding_id, self.binding_revision))
        return tuple(refs)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProposalConflict(ValueError):
    pass
