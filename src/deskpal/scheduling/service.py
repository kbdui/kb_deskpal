"""One local SQLite service with a wakeable, non-blocking scheduler.

Timers use monotonic deadlines while this process runs. A restart reconstructs
remaining duration from persisted UTC due_at, and overdue work is recorded once.
Events are durable local records, not system notifications. An event cursor is
consumer-owned; repeatedly peeking a record does not acknowledge it.
"""

import math
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone as utc_timezone
from pathlib import Path
from threading import Event, RLock, Thread, current_thread
from types import TracebackType
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from deskpal.execution_log import ExecutionLog, NullExecutionLog

from .models import CalendarEntry, ScheduleEvent, TimerEntry
from .ports import Clock

_UNSET = object()
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$")
MAX_DURATION_SECONDS = 31 * 24 * 60 * 60


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(utc_timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


def _stamp(value: datetime) -> str:
    return value.astimezone(utc_timezone.utc).isoformat(timespec="microseconds")


def _instant(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not _ISO.fullmatch(value):
        raise ValueError(f"{field} must be ISO 8601 with seconds and explicit offset or Z")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(utc_timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError(f"{field} must be a valid offset timestamp") from None


def _zone(value: str) -> None:
    _text(value, "timezone", 100)
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("timezone must be an available IANA timezone name") from None


def _text(value: str, field: str, maximum: int = 200) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field} must be non-empty and at most {maximum} characters")


def _limit(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 1000:
        raise ValueError("limit must be an integer between 1 and 1000")


def _calendar(row: sqlite3.Row) -> CalendarEntry:
    return CalendarEntry(row["id"], row["title"], row["start_at"], row["timezone"],
                         row["end_at"], row["reminder_at"], bool(row["reminder_fired"]),
                         row["created_at"], row["updated_at"])


def _timer(row: sqlite3.Row) -> TimerEntry:
    return TimerEntry(row["id"], row["label"], row["duration_seconds"], row["due_at"],
                      row["status"], row["created_at"])


class SchedulingService:
    """Application owns start/close; all connection access shares one RLock.

    Write-intent audit precedes each mutation, including background due events.
    Audit failures roll back that transaction and propagate. A worker failure
    stops scheduling and is exposed by every public operation and close().
    SQLite and JSONL are not one atomic transaction: an intent can be logged even
    when the following database commit fails. Queries never consume events.
    """

    def __init__(self, db_path: str | Path, *, execution_log: ExecutionLog | None = None,
                 clock: Clock | None = None, poll_interval: float = 1.0) -> None:
        if not isinstance(db_path, (str, Path)) or not str(db_path).strip():
            raise ValueError("db_path must be a non-empty string or Path")
        if type(poll_interval) not in (int, float) or not 0 < poll_interval <= 60 or not math.isfinite(poll_interval):
            raise ValueError("poll_interval must be between 0 and 60 seconds")
        self._clock = clock if clock is not None else SystemClock()
        self._log = execution_log if execution_log is not None else NullExecutionLog()
        self._interval = float(poll_interval)
        self._lock = RLock()
        self._wake = Event()
        self._stopping = False
        self._closed = False
        self._worker: Thread | None = None
        self._worker_error: Exception | None = None
        self._deadlines: dict[str, float] = {}
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            self._initialize()
            now, monotonic = self._now(), self._monotonic()
            for row in self._db.execute("SELECT id, due_at FROM timers WHERE status = 'pending'"):
                remaining = max(0.0, (datetime.fromisoformat(row["due_at"]) - now).total_seconds())
                self._deadlines[row["id"]] = monotonic + remaining
        except BaseException:
            self._db.close()
            self._closed = True
            raise

    def _initialize(self) -> None:
        with self._db:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("Unsupported scheduling database schema version")
            self._db.execute("""CREATE TABLE IF NOT EXISTS calendar (
                id TEXT PRIMARY KEY NOT NULL, title TEXT NOT NULL, start_at TEXT NOT NULL,
                timezone TEXT NOT NULL, end_at TEXT, reminder_at TEXT,
                reminder_fired INTEGER NOT NULL CHECK(reminder_fired IN (0,1)),
                revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS timers (
                id TEXT PRIMARY KEY NOT NULL, label TEXT NOT NULL, duration_seconds REAL NOT NULL,
                due_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','fired','cancelled')),
                created_at TEXT NOT NULL)""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL CHECK(source IN ('timer','calendar')),
                source_id TEXT NOT NULL, revision INTEGER NOT NULL, label TEXT NOT NULL,
                due_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
                overdue INTEGER NOT NULL CHECK(overdue IN (0,1)), UNIQUE(source,source_id,revision))""")
            self._db.execute("CREATE INDEX IF NOT EXISTS calendar_due ON calendar(reminder_fired,reminder_at)")
            self._db.execute("CREATE INDEX IF NOT EXISTS events_source_id ON events(source,id)")
            self._db.execute("PRAGMA user_version = 1")

    def _now(self) -> datetime:
        now = self._clock.now()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock.now must return an aware datetime")
        return now.astimezone(utc_timezone.utc)

    def _monotonic(self) -> float:
        value = self._clock.monotonic()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("clock.monotonic must return finite seconds")
        return float(value)

    def _ensure_open(self) -> None:
        if self._closed or self._stopping:
            raise RuntimeError("Scheduling service is closed")
        if self._worker_error is not None:
            raise RuntimeError("Scheduling worker failed; due events may remain pending") from self._worker_error

    def _audit(self, event: str, task_id: str | None, **data: Any) -> None:
        self._log.record(event, task_id, data)

    def create_calendar(self, title: str, start_at: str, timezone: str, *, end_at: str | None = None,
                        reminder_at: str | None = None, task_id: str | None = None) -> CalendarEntry:
        _text(title, "title")
        _zone(timezone)
        start, end, reminder = self._calendar_times(start_at, end_at, reminder_at)
        with self._lock:
            self._ensure_open()
            now, entry_id = _stamp(self._now()), str(uuid4())
            with self._db:
                self._audit("calendar_create_requested", task_id, id=entry_id)
                self._db.execute("INSERT INTO calendar VALUES (?,?,?,?,?,?,0,1,?,?)",
                                 (entry_id, title, start, timezone, end, reminder, now, now))
            self._wake.set()
            return self._get_calendar(entry_id)

    @staticmethod
    def _calendar_times(start_at: str, end_at: str | None, reminder_at: str | None) -> tuple[str, str | None, str | None]:
        start = _instant(start_at, "start_at")
        end = None if end_at is None else _instant(end_at, "end_at")
        reminder = None if reminder_at is None else _instant(reminder_at, "reminder_at")
        if end is not None and end <= start:
            raise ValueError("end_at must be later than start_at")
        if reminder is not None and reminder > start:
            raise ValueError("reminder_at must not be later than start_at")
        return _stamp(start), None if end is None else _stamp(end), None if reminder is None else _stamp(reminder)

    def _get_calendar(self, entry_id: str) -> CalendarEntry:
        row = self._db.execute("SELECT * FROM calendar WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            raise ValueError("Calendar entry does not exist")
        return _calendar(row)

    def list_calendar(self, *, start_after: str | None = None, start_before: str | None = None,
                      limit: int = 100) -> tuple[CalendarEntry, ...]:
        _limit(limit)
        after = None if start_after is None else _stamp(_instant(start_after, "start_after"))
        before = None if start_before is None else _stamp(_instant(start_before, "start_before"))
        if after is not None and before is not None and after >= before:
            raise ValueError("start_before must be later than start_after")
        clauses: list[str] = []
        params: list[str | int] = []
        if after is not None:
            clauses.append("start_at >= ?")
            params.append(after)
        if before is not None:
            clauses.append("start_at < ?")
            params.append(before)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            self._ensure_open()
            return tuple(_calendar(row) for row in self._db.execute(
                "SELECT * FROM calendar" + where + " ORDER BY start_at,id LIMIT ?", (*params, limit)))

    def update_calendar(self, entry_id: str, *, title: Any = _UNSET, start_at: Any = _UNSET,
                        timezone: Any = _UNSET, end_at: Any = _UNSET, reminder_at: Any = _UNSET,
                        task_id: str | None = None) -> CalendarEntry:
        _text(entry_id, "id", 100)
        if all(value is _UNSET for value in (title, start_at, timezone, end_at, reminder_at)):
            raise ValueError("Calendar update requires at least one changed field")
        with self._lock:
            self._ensure_open()
            old = self._get_calendar(entry_id)
            chosen_title = old.title if title is _UNSET else title
            chosen_zone = old.timezone if timezone is _UNSET else timezone
            _text(chosen_title, "title")
            _zone(chosen_zone)
            start, end, reminder = self._calendar_times(
                old.start_at if start_at is _UNSET else start_at,
                old.end_at if end_at is _UNSET else end_at,
                old.reminder_at if reminder_at is _UNSET else reminder_at)
            # Only a new reminder instant schedules another event. Editing text
            # or submitting the same instant cannot re-fire an existing reminder.
            reset = reminder != old.reminder_at
            with self._db:
                self._audit("calendar_update_requested", task_id, id=entry_id)
                self._db.execute("""UPDATE calendar SET title=?,start_at=?,timezone=?,end_at=?,reminder_at=?,
                    reminder_fired=?, revision=revision+?,updated_at=? WHERE id=?""",
                    (chosen_title, start, chosen_zone, end, reminder, 0 if reset else int(old.reminder_fired),
                     int(reset), _stamp(self._now()), entry_id))
            self._wake.set()
            return self._get_calendar(entry_id)

    def delete_calendar(self, entry_id: str, *, task_id: str | None = None) -> bool:
        _text(entry_id, "id", 100)
        with self._lock:
            self._ensure_open()
            with self._db:
                self._audit("calendar_delete_requested", task_id, id=entry_id)
                removed = self._db.execute("DELETE FROM calendar WHERE id=?", (entry_id,)).rowcount > 0
            self._wake.set()
            return removed

    def create_timer(self, duration_seconds: float, *, label: str = "Timer",
                     task_id: str | None = None) -> TimerEntry:
        if type(duration_seconds) not in (int, float) or not 1 <= duration_seconds <= MAX_DURATION_SECONDS:
            raise ValueError("duration_seconds must be finite and between 1 and 2678400")
        _text(label, "label")
        with self._lock:
            self._ensure_open()
            now, monotonic, timer_id = self._now(), self._monotonic(), str(uuid4())
            try:
                due_at = _stamp(now + timedelta(seconds=duration_seconds))
            except OverflowError:
                raise ValueError("Timer due time is outside the supported date range") from None
            with self._db:
                self._audit("timer_create_requested", task_id, id=timer_id, duration_seconds=duration_seconds)
                self._db.execute("INSERT INTO timers VALUES (?,?,?,?,'pending',?)",
                                 (timer_id, label, float(duration_seconds), due_at, _stamp(now)))
            self._deadlines[timer_id] = monotonic + duration_seconds
            self._wake.set()
            row = self._db.execute("SELECT * FROM timers WHERE id=?", (timer_id,)).fetchone()
            return _timer(row)

    def list_timers(self, *, status: str = "all", limit: int = 100) -> tuple[TimerEntry, ...]:
        _limit(limit)
        if status not in ("all", "pending", "fired", "cancelled"):
            raise ValueError("status must be all, pending, fired or cancelled")
        with self._lock:
            self._ensure_open()
            if status == "all":
                rows = self._db.execute("SELECT * FROM timers ORDER BY due_at,id LIMIT ?", (limit,))
            else:
                rows = self._db.execute("SELECT * FROM timers WHERE status=? ORDER BY due_at,id LIMIT ?", (status, limit))
            return tuple(_timer(row) for row in rows)

    def cancel_timer(self, timer_id: str, *, task_id: str | None = None) -> bool:
        _text(timer_id, "id", 100)
        with self._lock:
            self._ensure_open()
            with self._db:
                self._audit("timer_cancel_requested", task_id, id=timer_id)
                cancelled = self._db.execute("UPDATE timers SET status='cancelled' WHERE id=? AND status='pending'",
                                             (timer_id,)).rowcount > 0
            self._deadlines.pop(timer_id, None)
            self._wake.set()
            return cancelled

    def list_events(self, source: str, *, after_id: int = 0, limit: int = 100) -> tuple[ScheduleEvent, ...]:
        _limit(limit)
        if source not in ("timer", "calendar"):
            raise ValueError("source must be timer or calendar")
        if type(after_id) is not int or not 0 <= after_id <= 2**63 - 1:
            raise ValueError("after_id must be a non-negative SQLite integer cursor")
        with self._lock:
            self._ensure_open()
            rows = self._db.execute("SELECT * FROM events WHERE source=? AND id>? ORDER BY id LIMIT ?",
                                    (source, after_id, limit))
            return tuple(ScheduleEvent(row["id"], row["source"], row["source_id"], row["label"],
                                       row["due_at"], row["recorded_at"], bool(row["overdue"])) for row in rows)

    def poll_due(self) -> int:
        """Persist due events; manually callable without starting a thread."""
        with self._lock:
            self._ensure_open()
            return self._poll_due_locked()

    def _poll_due_locked(self) -> int:
        now, monotonic = self._now(), self._monotonic()
        timestamp = _stamp(now)
        timer_ids = [timer_id for timer_id, deadline in self._deadlines.items() if deadline <= monotonic]
        generated = 0
        with self._db:
            for timer_id in timer_ids:
                row = self._db.execute("SELECT * FROM timers WHERE id=? AND status='pending'", (timer_id,)).fetchone()
                if row is not None:
                    generated += self._event("timer", row["id"], 1, row["label"], row["due_at"], timestamp)
                    self._db.execute("UPDATE timers SET status='fired' WHERE id=?", (timer_id,))
            rows = self._db.execute("SELECT * FROM calendar WHERE reminder_at<=? AND reminder_fired=0",
                                    (timestamp,)).fetchall()
            for row in rows:
                generated += self._event("calendar", row["id"], row["revision"], row["title"], row["reminder_at"], timestamp)
                self._db.execute("UPDATE calendar SET reminder_fired=1 WHERE id=?", (row["id"],))
        for timer_id in timer_ids:
            self._deadlines.pop(timer_id, None)
        return generated

    def _event(self, source: str, source_id: str, revision: int, label: str, due_at: str, recorded_at: str) -> int:
        self._audit("schedule_due_requested", None, source=source, source_id=source_id, revision=revision, due_at=due_at)
        cursor = self._db.execute("""INSERT OR IGNORE INTO events
            (source,source_id,revision,label,due_at,recorded_at,overdue) VALUES (?,?,?,?,?,?,?)""",
            (source, source_id, revision, label, due_at, recorded_at, int(recorded_at > due_at)))
        return cursor.rowcount

    def start(self) -> None:
        with self._lock:
            self._ensure_open()
            if self._worker is None:
                self._worker = Thread(target=self._run, name="deskpal-scheduler", daemon=True)
                self._worker.start()

    def _run(self) -> None:
        try:
            while True:
                with self._lock:
                    if self._stopping:
                        return
                    self._poll_due_locked()
                    delay = self._interval
                    monotonic = self._monotonic()
                    if self._deadlines:
                        delay = min(delay, max(0.0, min(self._deadlines.values()) - monotonic))
                    self._wake.clear()
                self._wake.wait(delay)
        except Exception as exc:
            with self._lock:
                self._worker_error = exc
                self._wake.set()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._stopping = True
            self._wake.set()
            worker = self._worker
        if worker is current_thread():
            raise RuntimeError("Scheduling service cannot close from its own worker")
        if worker is not None:
            worker.join(timeout=5)
            if worker.is_alive():
                raise RuntimeError("Scheduling worker did not stop; connection remains open")
        with self._lock:
            self._db.close()
            self._closed = True
            error = self._worker_error
        if error is not None:
            raise RuntimeError("Scheduling worker failed before shutdown") from error

    def __enter__(self) -> "SchedulingService":
        self.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc_value: BaseException | None, traceback: TracebackType | None) -> None:
        self.close()
