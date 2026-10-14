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

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
