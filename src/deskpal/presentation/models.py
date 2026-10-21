"""Immutable presentation DTOs; no task body or executable object crosses here."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from deskpal.tasks.models import TaskStatus


_FIELDS = frozenset(("schema_version", "session_id", "sequence", "memory_epoch",
                     "status", "task_id", "revision"))
_STATUSES = frozenset(status.value for status in TaskStatus) | {"Idle", "Unknown"}


@dataclass(frozen=True)
class PresentationSnapshot:
    schema_version: int
    session_id: str
    sequence: int
    memory_epoch: int
    status: str
    task_id: str | None
    revision: int

    def __post_init__(self) -> None:
        # Exact primitive types prevent bool-as-int and custom coercion hooks.
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported presentation schema")
        if type(self.session_id) is not str or not self.session_id.strip():
            raise ValueError("Presentation session must be a non-empty string")
        for value in (self.sequence, self.memory_epoch, self.revision):
            if type(value) is not int or value < 0:
                raise ValueError("Presentation versions must be non-negative integers")
        if type(self.status) is not str or self.status not in _STATUSES:
            raise ValueError("Unsupported presentation status")
        if self.task_id is not None and (type(self.task_id) is not str or not self.task_id.strip()):
            raise ValueError("Presentation task identifier must be a non-empty string or None")
        if self.task_id is None and (self.status not in {"Idle", "Unknown"} or self.revision != 0):
            raise ValueError("Taskless presentation must be idle or unknown with zero revision")
        if self.status == "Idle" and self.task_id is not None:
            raise ValueError("Idle presentation cannot identify a task")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PresentationSnapshot":
        """Accept exactly the public schema, without converting arbitrary objects."""
        if not isinstance(payload, Mapping) or any(type(key) is not str for key in payload):
            raise ValueError("Presentation payload must be a string-keyed mapping")
        if set(payload) != _FIELDS:
            raise ValueError("Presentation payload fields do not match the schema")
        return cls(schema_version=payload["schema_version"], session_id=payload["session_id"],
                   sequence=payload["sequence"], memory_epoch=payload["memory_epoch"],
                   status=payload["status"], task_id=payload["task_id"], revision=payload["revision"])
