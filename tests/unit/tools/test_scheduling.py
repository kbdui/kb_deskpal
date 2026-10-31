"""Calendar persistence, countdown semantics, recovery and scheduler lifecycle."""

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

from deskpal.scheduling import SchedulingService


class FakeClock:
    def __init__(self) -> None:
        self.wall = datetime(2026, 10, 13, 8, tzinfo=timezone.utc)
        self.elapsed = 100.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.elapsed += seconds


def test_calendar_crud_utc_and_reopen(tmp_path):
    path = tmp_path / "scheduling.sqlite3"
    service = SchedulingService(path, clock=FakeClock())
    saved = service.create_calendar("Meet", "2026-10-14T16:00:00+08:00", "Asia/Shanghai",
                                    end_at="2026-10-14T17:00:00+08:00",
                                    reminder_at="2026-10-14T15:55:00+08:00")
    assert saved.start_at == "2026-10-14T08:00:00.000000+00:00"
    assert saved.timezone == "Asia/Shanghai"
    assert service.list_calendar(start_after="2026-10-14T08:00:00Z",
                                 start_before="2026-10-15T08:00:00Z") == (saved,)
    assert service.list_calendar(start_before="2026-10-14T08:00:00Z") == ()
    changed = service.update_calendar(saved.id, title="Changed", end_at=None)
    assert changed.title == "Changed" and changed.end_at is None
    service.close()
    service = SchedulingService(path, clock=FakeClock())
    assert service.list_calendar() == (changed,)
    assert service.delete_calendar(saved.id)
    assert not service.delete_calendar(saved.id)
    assert service.list_calendar() == ()
    service.close()


@pytest.mark.parametrize("invalid", ["2026-10-13T08:00:00", "2026-10-13", "2026-10-13T08:00:00+99:00",
                                    "2026-02-30T08:00:00Z", "2026-10-13 08:00:00+00:00", 1])
def test_reject_invalid_or_naive_calendar_times(invalid):
    with SchedulingService(":memory:") as service:
        with pytest.raises(ValueError):
            service.create_calendar("Meet", invalid, "UTC")
        assert service.list_calendar() == ()


def test_calendar_timezone_and_time_order_validation():
    service = SchedulingService(":memory:", clock=FakeClock())
    with pytest.raises(ValueError, match="IANA"):
        service.create_calendar("Meet", "2026-10-13T08:00:00Z", "Invalid/Timezone")
    for kwargs in ({"end_at": "2026-10-13T08:00:00Z"}, {"reminder_at": "2026-10-13T09:00:00Z"}):
        with pytest.raises(ValueError):
            service.create_calendar("Meet", "2026-10-13T08:00:00Z", "UTC", **kwargs)
    saved = service.create_calendar("Meet", "2026-10-13T08:00:00Z", "Asia/Shanghai")
    with pytest.raises(ValueError):
        service.update_calendar(saved.id)
    with pytest.raises(ValueError):
        service.update_calendar(saved.id, title=" ")
    with pytest.raises(ValueError):
        service.update_calendar(saved.id, reminder_at="2026-10-13T09:00:00Z")
    assert service.list_calendar() == (saved,)
    service.close()


def test_calendar_revision_dedupe_and_historical_events(tmp_path):
    clock = FakeClock()
    service = SchedulingService(tmp_path / "events.db", clock=clock)
    saved = service.create_calendar("Meet", "2026-10-13T09:00:00Z", "UTC", reminder_at="2026-10-13T08:00:00Z")
    assert service.poll_due() == 1
    first = service.list_events("calendar")[0]
    assert first.id > 0 and not first.overdue
    assert service.poll_due() == 0
    service.update_calendar(saved.id, title="Title only", reminder_at="2026-10-13T08:00:00+00:00")
    assert service.poll_due() == 0
    service.update_calendar(saved.id, reminder_at="2026-10-13T08:01:00Z")
    clock.advance(60)
    assert service.poll_due() == 1
    second = service.list_events("calendar", after_id=first.id)[0]
    assert second.id > first.id and second.label == "Title only"
    assert service.delete_calendar(saved.id)
    assert service.list_events("calendar") == (first, second)
    service.close()
    recovered = SchedulingService(tmp_path / "events.db", clock=clock)
    assert recovered.poll_due() == 0
    assert recovered.list_events("calendar") == (first, second)
    recovered.close()


def test_calendar_delete_and_clear_reminder_prevent_events():
    clock = FakeClock()
    service = SchedulingService(":memory:", clock=clock)
    entries = [service.create_calendar(str(n), "2026-10-13T09:00:00Z", "UTC",
                                       reminder_at="2026-10-13T08:01:00Z") for n in range(2)]
    service.delete_calendar(entries[0].id)
    service.update_calendar(entries[1].id, reminder_at=None)
    clock.advance(1000)
    assert service.poll_due() == 0
    service.close()


def test_timer_monotonic_ignores_wall_clock_jumps():
    clock = FakeClock()
    service = SchedulingService(":memory:", clock=clock)
    timer = service.create_timer(30, label="Tea")
    clock.wall += timedelta(days=3)
    assert service.poll_due() == 0
    clock.wall -= timedelta(days=5)
    clock.elapsed += 30
    assert service.poll_due() == 1
    assert service.list_timers(status="fired")[0].id == timer.id
    assert service.list_events("timer")[0].source_id == timer.id
    assert service.poll_due() == 0
    service.close()


def test_restart_recovers_future_overdue_calendar_and_timers_once(tmp_path):
    path = tmp_path / "recovery.db"
    clock = FakeClock()
    service = SchedulingService(path, clock=clock)
    overdue = service.create_timer(10)
    future = service.create_timer(100)
    service.create_calendar("Old reminder", "2026-10-13T09:00:00Z", "UTC", reminder_at="2026-10-13T08:00:10Z")
    service.close()
    clock.advance(50)
    service = SchedulingService(path, clock=clock)
    assert service.poll_due() == 2
    event = service.list_events("timer")[0]
    assert event.source_id == overdue.id and event.overdue
    calendar_event = service.list_events("calendar")[0]
    assert calendar_event.id != event.id and calendar_event.overdue
    assert service.list_timers(status="pending")[0].id == future.id
    clock.advance(50)
    assert service.poll_due() == 1
    assert len(service.list_events("timer")) == 2
    service.close()
    service = SchedulingService(path, clock=clock)
    assert service.poll_due() == 0
    assert len(service.list_events("timer")) == 2
    service.close()


def test_cancel_and_repeat_peek_do_not_generate_events():
    clock = FakeClock()
    service = SchedulingService(":memory:", clock=clock)
    cancelled = service.create_timer(1)
    assert service.cancel_timer(cancelled.id)
    assert not service.cancel_timer(cancelled.id)
    assert not service.cancel_timer("missing")
    fired = service.create_timer(1)
    clock.advance(1)
    # Read-only interfaces do not change due state.
    assert service.list_events("timer") == ()
    assert service.list_timers(status="pending")[0].id == fired.id
    assert service.poll_due() == 1
    assert not service.cancel_timer(fired.id)
    peek = service.list_events("timer")
    assert service.list_events("timer") == peek
    assert service.list_events("timer", after_id=peek[0].id) == ()
    service.close()


@pytest.mark.parametrize("duration", [0, -1, True, "1", float("nan"), float("inf"), 2678401, 10**1000])
def test_invalid_timer_duration(duration):
    service = SchedulingService(":memory:")
    with pytest.raises(ValueError):
        service.create_timer(duration)
    assert service.list_timers() == ()
    service.close()


@pytest.mark.parametrize("method,kwargs", [
    ("list_timers", {"limit": True}), ("list_calendar", {"limit": 0}),
    ("list_events", {"source": "timer", "after_id": True}),
    ("list_events", {"source": "timer", "after_id": 2**64}),
    ("list_events", {"source": "unknown"}), ("list_timers", {"status": "unknown"}),
])
def test_service_query_boundaries(method, kwargs):
    service = SchedulingService(":memory:")
    with pytest.raises(ValueError):
        getattr(service, method)(**kwargs)
    service.close()


def test_concurrent_timer_submission_sqlite_connection_safe():
    clock = FakeClock()
    service = SchedulingService(":memory:", clock=clock)
    with ThreadPoolExecutor(max_workers=6) as executor:
        created = tuple(executor.map(lambda n: service.create_timer(1, label=str(n)), range(30)))
    assert len({item.id for item in created}) == 30
    clock.advance(1)
    assert service.poll_due() == 30
    assert len(service.list_events("timer")) == 30
    service.close()


def test_close_wakes_long_wait_and_is_idempotent():
    service = SchedulingService(":memory:", poll_interval=60)
    service.create_timer(2678400)
    service.start()
    service.start()
    started = time.monotonic()
    service.close()
    assert time.monotonic() - started < 1
    service.close()
    with pytest.raises(RuntimeError, match="closed"):
        service.start()
    with pytest.raises(RuntimeError, match="closed"):
        service.list_timers()


class FailingAudit:
    def __init__(self, fail_on: str) -> None:
        self.fail_on = fail_on
        self.failed = Event()

    def record(self, event, task_id=None, data=None):
        if event == self.fail_on:
            self.failed.set()
            raise OSError("Audit disk unavailable")


def test_mutation_audit_failure_prevents_write():
    audit = FailingAudit("timer_create_requested")
    service = SchedulingService(":memory:", execution_log=audit)
    with pytest.raises(OSError):
        service.create_timer(30)
    assert service.list_timers() == ()
    audit.fail_on = "calendar_create_requested"
    with pytest.raises(OSError):
        service.create_calendar("Meet", "2026-10-13T09:00:00Z", "UTC")
    assert service.list_calendar() == ()
    service.close()


def test_due_audit_failure_rolls_back_event_and_fired_state():
    clock = FakeClock()
    audit = FailingAudit("schedule_due_requested")
    service = SchedulingService(":memory:", clock=clock, execution_log=audit)
    service.create_timer(1)
    clock.advance(1)
    with pytest.raises(OSError):
        service.poll_due()
    assert service.list_events("timer") == ()
    assert service.list_timers()[0].status == "pending"
    audit.fail_on = "nothing"
    assert service.poll_due() == 1
    service.close()


def test_due_batch_audit_failure_rolls_back_prior_events():
    clock = FakeClock()

    class SecondDueFails:
        calls = 0
        failing = True

        def record(self, event, task_id=None, data=None):
            if event == "schedule_due_requested":
                self.calls += 1
                if self.failing and self.calls == 2:
                    raise OSError("Second event audit failed")

    audit = SecondDueFails()
    service = SchedulingService(":memory:", clock=clock, execution_log=audit)
    service.create_timer(1)
    service.create_timer(1)
    clock.advance(1)
    with pytest.raises(OSError):
        service.poll_due()
    assert service.list_events("timer") == ()
    assert len(service.list_timers(status="pending")) == 2
    audit.failing = False
    assert service.poll_due() == 2
    service.close()


def test_sqlite_write_failure_is_visible_without_timer(tmp_path):
    path = tmp_path / "write_failure.db"
    service = SchedulingService(path, clock=FakeClock())
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TRIGGER reject_timer BEFORE INSERT ON timers
            BEGIN SELECT RAISE(ABORT, 'Simulated storage failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        service.create_timer(1)
    assert service.list_timers() == ()
    assert service.poll_due() == 0
    service.close()


@pytest.mark.parametrize("poll_interval", [True, 0, -1, float("nan"), float("inf"), 61, 10**1000])
def test_poll_interval_boundaries(poll_interval):
    with pytest.raises(ValueError):
        SchedulingService(":memory:", poll_interval=poll_interval)


def test_worker_audit_failure_visible_and_pending_recoverable(tmp_path):
    path = tmp_path / "failure.db"
    clock = FakeClock()
    audit = FailingAudit("schedule_due_requested")
    service = SchedulingService(path, clock=clock, execution_log=audit)
    service.create_timer(1)
    clock.advance(1)
    service.start()
    assert audit.failed.wait(2)
    # Joining the worker synchronizes its recorded failure without polling.
    with pytest.raises(RuntimeError, match="failed before shutdown"):
        service.close()
    service.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT status FROM timers").fetchone()[0] == "pending"
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    recovered = SchedulingService(path, clock=clock)
    assert recovered.poll_due() == 1
    recovered.close()


def test_background_worker_records_calendar_due_event():
    recorded = Event()

    class Audit:
        def record(self, event, task_id=None, data=None):
            if event == "schedule_due_requested":
                recorded.set()

    service = SchedulingService(":memory:", clock=FakeClock(), execution_log=Audit())
    service.create_calendar("Meet", "2026-10-13T09:00:00Z", "UTC", reminder_at="2026-10-13T08:00:00Z")
    service.start()
    assert recorded.wait(2)
    assert len(service.list_events("calendar")) == 1
    service.close()
