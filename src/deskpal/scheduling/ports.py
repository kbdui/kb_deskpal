"""Scheduling clock contract; no Core or UI dependency."""

from datetime import datetime
from typing import Any, Protocol

from .models import CalendarEntry, ScheduleEvent, TimerEntry


class Clock(Protocol):
    def now(self) -> datetime:
        """Return an aware wall-clock timestamp."""
        ...

    def monotonic(self) -> float:
        """Return elapsed-time seconds independent of wall-clock adjustments."""
        ...


class SchedulingManager(Protocol):
    """Local scheduling capability injected into tools, independent of storage."""

    def start(self) -> None: ...
    def close(self) -> None: ...
    def poll_due(self) -> int: ...

    def create_calendar(self, title: str, start_at: str, timezone: str, *,
                        end_at: str | None = None, reminder_at: str | None = None,
                        task_id: str | None = None) -> CalendarEntry: ...

    def list_calendar(self, *, start_after: str | None = None, start_before: str | None = None,
                      limit: int = 100) -> tuple[CalendarEntry, ...]: ...

    def update_calendar(self, entry_id: str, *, title: Any = ..., start_at: Any = ...,
                        timezone: Any = ..., end_at: Any = ..., reminder_at: Any = ...,
                        task_id: str | None = None) -> CalendarEntry: ...

    def delete_calendar(self, entry_id: str, *, task_id: str | None = None) -> bool: ...

    def create_timer(self, duration_seconds: float, *, label: str = "Timer",
                     task_id: str | None = None) -> TimerEntry: ...

    def list_timers(self, *, status: str = "all", limit: int = 100) -> tuple[TimerEntry, ...]: ...

    def cancel_timer(self, timer_id: str, *, task_id: str | None = None) -> bool: ...

    def list_events(self, source: str, *, after_id: int = 0,
                    limit: int = 100) -> tuple[ScheduleEvent, ...]: ...

