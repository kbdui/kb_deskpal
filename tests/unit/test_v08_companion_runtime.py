"""Deterministic T01/T04/T05/T06 acceptance probes using temporary local data.

The clock never sleeps. SQL is restricted to temporary control metadata: it
provides independent receipt/budget witnesses and small synthetic count-limit
fixtures, while behavior is asserted through CompanionRuntime's public API.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator

import pytest

from deskpal.companion import CompanionRevisionConflict, CompanionRuntime


@dataclass
class Clock:
    wall: float = 1_800_000_000.0
    monotonic: float = 100.0

    def wall_now(self) -> float:
        return self.wall

    def monotonic_now(self) -> float:
        return self.monotonic

    def advance(self, seconds: float) -> None:
        assert seconds >= 0
        self.wall += seconds
        self.monotonic += seconds


@contextmanager
def runtime_at(
    path: Path, clock: Clock, *, enable: bool = True
) -> Iterator[CompanionRuntime]:
    runtime = CompanionRuntime(
        path,
        timezone="Asia/Shanghai",
        wall_clock=clock.wall_now,
        monotonic_clock=clock.monotonic_now,
    )
    try:
        if enable:
            runtime.command(
                "configure",
                expected_revision=runtime.snapshot()["revision"],
                enabled=True,
                emotions_enabled=True,
            )
        yield runtime
    finally:
        runtime.close()


def values(snapshot: dict[str, Any]) -> tuple[float, float, float]:
    affect = snapshot["affect"]
    return (affect["valence"], affect["arousal"], affect["mood"])


def sql_rows(path: Path, sql: str, parameters: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(path)) as db:
        return db.execute(sql, parameters).fetchall()


def receipt(path: Path, task_id: str) -> tuple[Any, ...]:
    rows = sql_rows(
        path,
        "SELECT eligibility, delta_v, delta_a, delta_m FROM receipts WHERE task_id=?",
        (task_id,),
    )
    assert len(rows) == 1
    return rows[0]


def seed_count_window(path: Path, accepted_times: list[float]) -> None:
    """Isolate the count guard from numeric exhaustion with tiny valid debits.

    These are synthetic control rows, not fabricated request/event bodies.
    The public state remains at a reset baseline. Each receipt and its budget
    debit agree, and the per-dimension sums leave room for a real new outcome.
    """
    with closing(sqlite3.connect(path)) as db:
        for index, accepted in enumerate(accepted_times):
            cursor = db.execute(
                "INSERT INTO receipts(task_id,status,accepted_utc,epoch,eligibility,delta_v,delta_a,delta_m) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"seed-{index}", "completed", accepted, 1, "eligible", 0.001, 0.001, 0.0001),
            )
            db.execute(
                "INSERT INTO budget(sequence,accepted_utc,delta_v,delta_a,delta_m) VALUES (?,?,?,?,?)",
                (cursor.lastrowid, accepted, 0.001, 0.001, 0.0001),
            )
        db.commit()


@pytest.mark.parametrize("separate_connections", [False, True], ids=["one-runtime", "two-clients"])
def test_t01_same_revision_thread_race_has_exactly_one_winner(
    tmp_path: Path, separate_connections: bool
) -> None:
    path = tmp_path / "cas.sqlite3"
    clock = Clock()
    with ExitStack() as stack:
        first = stack.enter_context(runtime_at(path, clock))
        second = stack.enter_context(runtime_at(path, clock, enable=False)) if separate_connections else first
        initial = first.snapshot()
        assert second.snapshot()["revision"] == initial["revision"]
        barrier = threading.Barrier(2)

        def change(runtime: CompanionRuntime, preset: str) -> tuple[str, str]:
            barrier.wait(timeout=5)
            try:
                runtime.command("configure", expected_revision=initial["revision"], preset=preset)
            except CompanionRevisionConflict:
                return ("conflict", preset)
            return ("accepted", preset)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(change, first, "warm"), executor.submit(change, second, "concise")]
            results = [future.result(timeout=10) for future in futures]

        assert sorted(result[0] for result in results) == ["accepted", "conflict"]
        winner = next(preset for result, preset in results if result == "accepted")
        final = first.snapshot()
        assert second.snapshot() == final
        assert final["revision"] == initial["revision"] + 1
        assert final["persona"]["version"] == initial["persona"]["version"] + 1
        assert final["persona"]["preset"] == winner
        assert len(final["persona_versions"]) == len(initial["persona_versions"]) + 1


def test_t01_rollback_creates_a_new_persisted_persona_version(tmp_path: Path) -> None:
    path = tmp_path / "rollback.sqlite3"
    clock = Clock()
    with runtime_at(path, clock) as runtime:
        initial = runtime.snapshot()
        warm = runtime.command("configure", expected_revision=initial["revision"], preset="warm")
        concise = runtime.command("configure", expected_revision=warm["revision"], preset="concise")
        restored = runtime.command(
            "rollback", expected_revision=concise["revision"], persona_version=warm["persona"]["version"]
        )
        assert restored["revision"] == concise["revision"] + 1
        assert restored["persona"]["version"] > concise["persona"]["version"]
        assert restored["persona"]["preset"] == "warm"
        assert restored["persona"]["style"] == warm["persona"]["style"]
        assert set(restored["persona_versions"]) == {
            initial["persona"]["version"], warm["persona"]["version"],
            concise["persona"]["version"], restored["persona"]["version"],
        }
        with pytest.raises(CompanionRevisionConflict):
            runtime.command("rollback", expected_revision=concise["revision"], persona_version=1)
        assert runtime.snapshot() == restored
    with runtime_at(path, clock, enable=False) as reopened:
        assert reopened.snapshot() == restored


@pytest.mark.parametrize(
    ("action", "parameters"),
    [
        ("reset", {"target": "affect"}),
        ("reset", {"target": "persona"}),
        ("reset", {"target": "all"}),
        ("configure", {"preset": "warm"}),
    ],
    ids=["affect-reset", "persona-reset", "all-reset", "persona-configure"],
)
def test_t04_root_task_is_not_rewarded_again_after_reset_or_configure(
    tmp_path: Path, action: str, parameters: dict[str, Any]
) -> None:
    path = tmp_path / "dedup.sqlite3"
    with runtime_at(path, Clock()) as runtime:
        baseline = runtime.snapshot()
        runtime.record_outcome("one-root-task", "Completed", tool_success=True)
        rewarded = runtime.snapshot()
        assert values(rewarded)[0] > values(baseline)[0]
        original_receipt = receipt(path, "one-root-task")
        original_budget = sql_rows(path, "SELECT * FROM budget ORDER BY sequence")
        runtime.record_outcome("one-root-task", "Completed", tool_success=True)
        assert runtime.snapshot() == rewarded

        reset = runtime.command(action, expected_revision=rewarded["revision"], **parameters)
        assert reset["reset_epoch"] > rewarded["reset_epoch"]
        for status in ("Completed", "Failed", "Completed"):
            runtime.record_outcome("one-root-task", status, tool_success=status == "Completed")
        assert runtime.snapshot() == reset
        assert receipt(path, "one-root-task") == original_receipt
        assert sql_rows(path, "SELECT * FROM budget ORDER BY sequence") == original_budget
        assert sql_rows(path, "SELECT COUNT(*) FROM receipts") == [(1,)]


def test_t04_uncertain_waiting_and_cancellation_have_no_effect_or_terminal_claim(tmp_path: Path) -> None:
    path = tmp_path / "nonterminal.sqlite3"
    with runtime_at(path, Clock()) as runtime:
        initial = runtime.snapshot()
        for status in ("Uncertain", "Waiting", "NeedsReview", "Cancelled", "Cancel", "Queued"):
            runtime.record_outcome("eventual-task", status, tool_success=False)
            assert runtime.snapshot() == initial
        assert sql_rows(path, "SELECT COUNT(*) FROM receipts") == [(0,)]
        assert sql_rows(path, "SELECT COUNT(*) FROM budget") == [(0,)]

        runtime.record_outcome("eventual-task", "Completed", tool_success=True)
        completed = runtime.snapshot()
        assert values(completed)[0] > values(initial)[0]
        runtime.record_outcome("not-reliable", "Completed", tool_success=False)
        unreliable = runtime.snapshot()
        assert values(unreliable) == values(completed)
        assert receipt(path, "not-reliable") == ("completion_not_reliably_successful", 0.0, 0.0, 0.0)
        runtime.record_outcome("not-reliable", "Completed", tool_success=True)
        assert runtime.snapshot() == unreliable
        assert sql_rows(path, "SELECT COUNT(*) FROM budget") == [(1,)]


def test_t04_disabled_outcomes_remain_consumed_after_enabling(tmp_path: Path) -> None:
    path = tmp_path / "disabled.sqlite3"
    with runtime_at(path, Clock(), enable=False) as runtime:
        initial = runtime.snapshot()
        assert initial["enabled"] is False
        runtime.record_outcome("disabled-task", "Completed", tool_success=True)
        assert values(runtime.snapshot()) == values(initial)
        enabled = runtime.command(
            "configure", expected_revision=runtime.snapshot()["revision"], enabled=True, emotions_enabled=True
        )
        runtime.record_outcome("disabled-task", "Completed", tool_success=True)
        assert runtime.snapshot() == enabled
        assert receipt(path, "disabled-task") == ("disabled", 0.0, 0.0, 0.0)
        assert sql_rows(path, "SELECT COUNT(*) FROM budget") == [(0,)]
        runtime.record_outcome("new-task", "Completed", tool_success=True)
        assert values(runtime.snapshot())[0] > values(enabled)[0]


@pytest.mark.parametrize(
    ("component", "half_life"),
    [(0, 300), (1, 120), (2, 7200)],
    ids=["valence-300s", "arousal-120s", "mood-7200s"],
)
def test_t05_each_documented_half_life_halves_its_deviation(
    tmp_path: Path, component: int, half_life: int
) -> None:
    clock = Clock()
    with runtime_at(tmp_path / "half-life.sqlite3", clock) as runtime:
        base = values(runtime.snapshot())[component]
        runtime.record_outcome("success", "Completed", tool_success=True)
        initial_deviation = values(runtime.snapshot())[component] - base
        assert initial_deviation > 0
        clock.advance(half_life)
        halfway_deviation = values(runtime.snapshot())[component] - base
        assert halfway_deviation == pytest.approx(initial_deviation / 2, abs=1e-12)
        clock.advance(half_life)
        assert values(runtime.snapshot())[component] - base == pytest.approx(initial_deviation / 4, abs=1e-12)


def test_t05_positive_and_negative_events_spend_absolute_not_net_budgets(tmp_path: Path) -> None:
    path = tmp_path / "absolute.sqlite3"
    with runtime_at(path, Clock()) as runtime:
        baseline = values(runtime.snapshot())
        for index in range(10):
            runtime.record_outcome(f"alternating-{index}", "Completed" if index % 2 == 0 else "Failed", tool_success=index % 2 == 0)
        after_ten = values(runtime.snapshot())
        # +.08 -.08 +.08 -.01 reaches the absolute v cap while its net
        # change is only +.07; ten alternating mood changes net to zero.
        assert after_ten[0] - baseline[0] == pytest.approx(0.07, abs=1e-12)
        assert after_ten[1] - baseline[1] == pytest.approx(0.25, abs=1e-12)
        assert after_ten[2] == pytest.approx(baseline[2], abs=1e-12)
        debit = sql_rows(path, "SELECT SUM(ABS(delta_v)),SUM(ABS(delta_a)),SUM(ABS(delta_m)) FROM budget")[0]
        assert debit == pytest.approx((0.25, 0.25, 0.05), abs=1e-12)
        runtime.record_outcome("eleventh", "Completed", tool_success=True)
        assert values(runtime.snapshot()) == pytest.approx(after_ten, abs=1e-12)
        assert receipt(path, "eleventh")[1:] == pytest.approx((0, 0, 0), abs=1e-12)


def test_t05_minute_budget_releases_at_60_seconds_not_59(tmp_path: Path) -> None:
    path = tmp_path / "minute.sqlite3"
    clock = Clock()
    with runtime_at(path, clock) as runtime:
        for index in range(4):
            runtime.record_outcome(f"initial-{index}", "Completed", tool_success=True)
        assert sql_rows(path, "SELECT SUM(ABS(delta_v)) FROM budget")[0][0] == pytest.approx(0.25)
        clock.advance(59)
        before = values(runtime.snapshot())
        runtime.record_outcome("at-59", "Completed", tool_success=True)
        assert values(runtime.snapshot())[0] == pytest.approx(before[0], abs=1e-12)
        assert receipt(path, "at-59")[1] == pytest.approx(0, abs=1e-12)
        clock.advance(1)
        before_boundary = values(runtime.snapshot())
        runtime.record_outcome("at-60", "Completed", tool_success=True)
        assert values(runtime.snapshot())[0] - before_boundary[0] == pytest.approx(0.08, abs=1e-12)
        assert receipt(path, "at-60")[1] == pytest.approx(0.08, abs=1e-12)


def test_t05_mood_budget_releases_at_3600_seconds_not_3599(tmp_path: Path) -> None:
    path = tmp_path / "hour.sqlite3"
    clock = Clock()
    origin = clock.wall
    with runtime_at(path, clock) as runtime:
        for index in range(10):
            runtime.record_outcome(f"initial-{index}", "Completed", tool_success=True)
        assert sql_rows(path, "SELECT SUM(ABS(delta_m)) FROM budget")[0][0] == pytest.approx(0.05)
        clock.advance(3599)
        before = values(runtime.snapshot())
        runtime.record_outcome("at-3599", "Completed", tool_success=True)
        assert values(runtime.snapshot())[2] == pytest.approx(before[2], abs=1e-12)
        assert receipt(path, "at-3599")[3] == pytest.approx(0, abs=1e-12)
        clock.advance(1)
        before_boundary = values(runtime.snapshot())
        runtime.record_outcome("at-3600", "Completed", tool_success=True)
        assert values(runtime.snapshot())[2] - before_boundary[2] == pytest.approx(0.005, abs=1e-12)
        assert receipt(path, "at-3600")[3] == pytest.approx(0.005, abs=1e-12)
        assert sql_rows(path, "SELECT COUNT(*) FROM budget WHERE accepted_utc=?", (origin,)) == [(0,)]


def test_t05_32_event_minute_count_blocks_even_with_numeric_headroom(tmp_path: Path) -> None:
    path = tmp_path / "minute-count.sqlite3"
    clock = Clock()
    with runtime_at(path, clock):
        pass
    seed_count_window(path, [clock.wall] * 32)
    with runtime_at(path, clock, enable=False) as runtime:
        before = values(runtime.snapshot())
        runtime.record_outcome("minute-overflow", "Completed", tool_success=True)
        assert values(runtime.snapshot()) == before
        assert receipt(path, "minute-overflow") == ("event_window_capacity", 0.0, 0.0, 0.0)
        assert sql_rows(path, "SELECT COUNT(*) FROM budget") == [(32,)]
        clock.advance(60)
        before_release = values(runtime.snapshot())
        runtime.record_outcome("minute-released", "Completed", tool_success=True)
        assert values(runtime.snapshot())[0] - before_release[0] == pytest.approx(0.08, abs=1e-12)
        assert sql_rows(path, "SELECT COUNT(*) FROM budget") == [(33,)]


def test_t05_256_event_hour_count_blocks_below_minute_and_numeric_limits(tmp_path: Path) -> None:
    path = tmp_path / "hour-count.sqlite3"
    clock = Clock()
    with runtime_at(path, clock):
        pass
    # Sixteen per 61-second group: 256 in the hour, only 16 in the minute.
    seed_count_window(path, [clock.wall - 61 * group for group in range(15, -1, -1) for _ in range(16)])
    with runtime_at(path, clock, enable=False) as runtime:
        initial = values(runtime.snapshot())
        runtime.record_outcome("hour-overflow", "Completed", tool_success=True)
        assert values(runtime.snapshot()) == initial
        assert receipt(path, "hour-overflow") == ("event_window_capacity", 0.0, 0.0, 0.0)
        assert sql_rows(path, "SELECT COUNT(*) FROM budget") == [(256,)]
        clock.advance(2684)  # The oldest group is 3,599 seconds old.
        before = values(runtime.snapshot())
        runtime.record_outcome("hour-still-full", "Completed", tool_success=True)
        assert values(runtime.snapshot()) == before
        assert receipt(path, "hour-still-full")[0] == "event_window_capacity"
        clock.advance(1)
        before_release = values(runtime.snapshot())
        runtime.record_outcome("hour-released", "Completed", tool_success=True)
        assert values(runtime.snapshot())[0] - before_release[0] == pytest.approx(0.08, abs=1e-12)
        assert sql_rows(path, "SELECT COUNT(*) FROM budget") == [(241,)]


def test_t05_batch_partition_and_poll_frequency_do_not_change_same_as_of_result(tmp_path: Path) -> None:
    schedule = [
        (0, "Completed"), (0, "Failed"), (0, "Completed"), (0, "Failed"), (0, "Completed"),
        (59, "Completed"), (60, "Failed"), (60, "Completed"),
        (3600, "Completed"), (3600, "Failed"),
    ]
    results: list[dict[str, Any]] = []
    transcripts: list[list[tuple[Any, ...]]] = []
    for name, chunk_size, poll_interval in (("single", 1, 0), ("batch-three", 3, 17), ("batch-eight", 8, 60)):
        path = tmp_path / f"{name}.sqlite3"
        clock = Clock()
        elapsed = 0
        with runtime_at(path, clock) as runtime:
            for offset in range(0, len(schedule), chunk_size):
                for index in range(offset, min(offset + chunk_size, len(schedule))):
                    accepted_at, status = schedule[index]
                    while poll_interval and elapsed + poll_interval < accepted_at:
                        clock.advance(poll_interval)
                        elapsed += poll_interval
                        runtime.snapshot()
                    clock.advance(accepted_at - elapsed)
                    elapsed = accepted_at
                    runtime.record_outcome(f"task-{index}", status, tool_success=status == "Completed")
                if poll_interval:
                    runtime.snapshot()
            stored_anchor = sql_rows(path, "SELECT payload FROM state")
            while poll_interval and elapsed + poll_interval < 4200:
                clock.advance(poll_interval)
                elapsed += poll_interval
                runtime.snapshot()
            clock.advance(4200 - elapsed)
            results.append(runtime.snapshot())
            assert sql_rows(path, "SELECT payload FROM state") == stored_anchor
            transcripts.append(sql_rows(
                path, "SELECT sequence,task_id,status,accepted_utc,eligibility,delta_v,delta_a,delta_m FROM receipts ORDER BY sequence"
            ))
    assert results[0] == results[1] == results[2]
    assert transcripts[0] == transcripts[1] == transcripts[2]
    assert len(transcripts[0]) == len(schedule)


def test_t06_normal_offline_restart_matches_live_decay_without_restoring_cue(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite3"
    original_clock, live_clock = Clock(), Clock()
    with runtime_at(path, original_clock) as original, runtime_at(tmp_path / "live.sqlite3", live_clock) as live:
        initial = original.snapshot()
        original.record_outcome("same-root", "Completed", tool_success=True)
        live.record_outcome("same-root", "Completed", tool_success=True)
        original.command("cue", expected_revision=original.snapshot()["revision"], mode="supportive")
        original.command("preference", expected_revision=original.snapshot()["revision"], scope="session", verbosity="brief")
        saved = original.command("preference", expected_revision=original.snapshot()["revision"], scope="persistent", humor_allowed=False)
        assert saved["cue"] is not None
        original.close()

        live_clock.advance(300)
        restarted_clock = Clock(wall=original_clock.wall + 300, monotonic=500)
        with runtime_at(path, restarted_clock, enable=False) as restarted:
            recovered = restarted.snapshot()
            assert values(recovered) == pytest.approx(values(live.snapshot()), abs=1e-12)
            assert recovered["affect"]["valence"] - initial["affect"]["valence"] == pytest.approx(0.04, abs=1e-12)
            assert recovered["cue"] is None
            assert recovered["affect"]["mode"] == "neutral"
            assert "session" not in recovered["preferences"]
            assert recovered["preferences"]["persistent"] == {"humor_allowed": False}
            assert recovered["revision"] == saved["revision"]
            assert recovered["warnings"] == []
            restarted.record_outcome("same-root", "Completed", tool_success=True)
            assert restarted.snapshot() == recovered
            assert sql_rows(path, "SELECT COUNT(*) FROM receipts") == [(1,)]


def test_t06_ordinary_restart_preserves_exhausted_budget_and_consumed_roots(tmp_path: Path) -> None:
    path = tmp_path / "restart-budget.sqlite3"
    clock = Clock()
    with runtime_at(path, clock) as runtime:
        for index in range(10):
            runtime.record_outcome(f"task-{index}", "Completed", tool_success=True)
    restarted_clock = Clock(wall=clock.wall + 30, monotonic=900)
    with runtime_at(path, restarted_clock, enable=False) as restarted:
        before = restarted.snapshot()
        restarted.record_outcome("task-0", "Completed", tool_success=True)
        assert restarted.snapshot() == before
        restarted.record_outcome("new-after-restart", "Completed", tool_success=True)
        assert values(restarted.snapshot()) == pytest.approx(values(before), abs=1e-12)
        assert receipt(path, "new-after-restart")[1:] == pytest.approx((0, 0, 0), abs=1e-12)
        assert sql_rows(path, "SELECT COUNT(*) FROM receipts") == [(11,)]


@pytest.mark.parametrize("wall_offset", [-120, 30 * 24 * 3600 + 1], ids=["clock-rollback", "abnormal-long-gap"])
def test_t06_clock_anomaly_resets_affect_but_holds_minute_and_hour_budgets(
    tmp_path: Path, wall_offset: int
) -> None:
    path = tmp_path / "clock-anomaly.sqlite3"
    clock = Clock()
    with runtime_at(path, clock) as runtime:
        baseline = runtime.snapshot()
        for index in range(10):
            runtime.record_outcome(f"task-{index}", "Completed", tool_success=True)
        saved = runtime.command("cue", expected_revision=runtime.snapshot()["revision"], mode="supportive")
    restarted_clock = Clock(wall=clock.wall + wall_offset, monotonic=700)
    with runtime_at(path, restarted_clock, enable=False) as restarted:
        recovered = restarted.snapshot()
        assert values(recovered) == pytest.approx(values(baseline), abs=1e-12)
        assert recovered["reset_epoch"] == saved["reset_epoch"] + 1
        assert recovered["revision"] == saved["revision"] + 1
        assert recovered["cue"] is None
        assert recovered["affect"]["mode"] == "neutral"
        assert "companion_clock_anomaly_budget_retained" in recovered["warnings"]
        assert sql_rows(path, "SELECT MIN(accepted_utc),MAX(accepted_utc) FROM budget") == [(restarted_clock.wall, restarted_clock.wall)]
        debit = sql_rows(path, "SELECT SUM(ABS(delta_v)),SUM(ABS(delta_a)),SUM(ABS(delta_m)) FROM budget")[0]
        assert debit == pytest.approx((0.25, 0.25, 0.05), abs=1e-12)
        restarted.record_outcome("task-0", "Completed", tool_success=True)
        assert restarted.snapshot() == recovered

        restarted.record_outcome("fresh-at-zero", "Completed", tool_success=True)
        assert values(restarted.snapshot()) == pytest.approx(values(baseline), abs=1e-12)
        assert receipt(path, "fresh-at-zero")[1:] == pytest.approx((0, 0, 0), abs=1e-12)
        restarted_clock.advance(59)
        restarted.record_outcome("fresh-at-59", "Completed", tool_success=True)
        assert receipt(path, "fresh-at-59")[1:] == pytest.approx((0, 0, 0), abs=1e-12)
        restarted_clock.advance(1)
        before_minute_release = values(restarted.snapshot())
        restarted.record_outcome("fresh-at-60", "Completed", tool_success=True)
        assert values(restarted.snapshot())[0] - before_minute_release[0] == pytest.approx(0.08, abs=1e-12)
        assert receipt(path, "fresh-at-60")[1:] == pytest.approx((0.08, 0.04, 0), abs=1e-12)
        restarted_clock.advance(3539)
        restarted.record_outcome("fresh-at-3599", "Completed", tool_success=True)
        assert receipt(path, "fresh-at-3599")[3] == pytest.approx(0, abs=1e-12)
        restarted_clock.advance(1)
        before_hour_release = values(restarted.snapshot())
        restarted.record_outcome("fresh-at-3600", "Completed", tool_success=True)
        assert values(restarted.snapshot())[2] - before_hour_release[2] == pytest.approx(0.005, abs=1e-12)
        assert receipt(path, "fresh-at-3600")[3] == pytest.approx(0.005, abs=1e-12)
