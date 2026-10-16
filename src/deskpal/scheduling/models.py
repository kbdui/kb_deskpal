"""Detached JSON-compatible local scheduling records."""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CalendarEntry:
    id: str
    title: str
    start_at: str
    timezone: str
    end_at: str | None
    reminder_at: str | None
    reminder_fired: bool
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TimerEntry:
    id: str
    label: str
    duration_seconds: float
    due_at: str
    status: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ScheduleEvent:
    id: int
    source: str
    source_id: str
    label: str
    due_at: str
    recorded_at: str
    overdue: bool
    occurrence_revision: int = 1
    source_store_id: str = ""
    source_generation: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ScheduleStoreIdentity:
    store_id: str
    generation: str


@dataclass(frozen=True)
class SourceSnapshot:
    """Current full source record; fingerprint includes every persisted field."""

    source: str
    source_id: str
    store_id: str
    generation: str
    occurrence_revision: int
    fingerprint: str
    payload_json: str


@dataclass(frozen=True)
class EventCursorState:
    source: str
    tail_id: int
    anchor: str
