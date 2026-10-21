"""Opt-in synthetic large-history presentation/index/lock measurements.

Run only with the explicit project interpreter. This imports WorkflowStore and
PersistentTaskManager, never Core, a factory, configuration, profiles or cloud
APIs. Every database is newly created under tempfile.mkdtemp and is retained for
first-failure inspection; the script accepts no input database path.

Example:
    python scripts/check_v07_projection.py --output data/validation/v07/NEW/projection.json

The two default sizes are 10,000 and 100,000 historical tasks/submissions. Index
retrofit time is measured separately from steady-state calls. The concurrent
phase deliberately saturates queries to measure shared transaction/lock delays;
it does not simulate the product's one-second polling cadence or call Core.
No timing threshold is silently inferred. Raw samples and EXPLAIN details are
evidence for review, not an automatic performance-acceptance certificate.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
from threading import Barrier, Event
from time import perf_counter
import traceback
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deskpal.workflow.store import WorkflowStore
from deskpal.workflow.tasks import PersistentTaskManager, _PRESENTATION_SQL


INDEX_NAMES = ("task_presentation_status", "task_presentation_latest", "submission_presentation_status")
SENTINEL = "synthetic-current-task"
ORIGINAL_ORDER_SQL = (
    "SELECT t.id,t.status,t.revision FROM task AS t LEFT JOIN submission AS s ON s.task_id=t.id "
    "ORDER BY CASE WHEN t.status IN ('Planning','Running') THEN 0 "
    "WHEN s.status='Claimed' AND t.status IN ('Created','Waiting') THEN 0 "
    "WHEN s.status='Queued' THEN 1 "
    "WHEN t.status IN ('Created','Waiting','NeedsReview','Uncertain') THEN 2 ELSE 3 END, "
    "CASE WHEN s.status='Queued' THEN s.sequence END,t.updated_at DESC,t.id DESC LIMIT 1")


def latency(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {"count": len(values), "raw_ms": values,
            "p50_ms": ordered[math.ceil(len(values) * .5) - 1] if values else None,
            "p95_ms": ordered[math.ceil(len(values) * .95) - 1] if values else None,
            "max_ms": max(values) if values else None}


def seed_history(store: WorkflowStore, count: int) -> dict[str, Any]:
    """Only the newly owned synthetic database is modified or stripped of indexes."""
    started = perf_counter()
    base = datetime(2000, 1, 1, tzinfo=timezone.utc)
    with store.transaction() as conn:
        # Reproduce an existing schema 1 database predating the access paths.
        for name in INDEX_NAMES:
            conn.execute(f"DROP INDEX {name}")
        conn.executemany(
            "INSERT INTO task(id,request,status,created_at,updated_at) VALUES(?,?,?,?,?)",
            ((f"history-{index:09d}", "synthetic historical body",
              ("Completed", "Failed", "Cancelled")[index % 3],
              (base + timedelta(seconds=index)).isoformat(),
              (base + timedelta(seconds=index)).isoformat()) for index in range(count)))
        conn.executemany("INSERT INTO submission(task_id,status) VALUES(?,'Finished')",
                         ((f"history-{index:09d}",) for index in range(count)))
        store.record_event("synthetic_projection_seed")
    return {"task_count": count, "submission_count": count,
            "seed_seconds_without_presentation_indexes": perf_counter() - started}


def configure_scenario(store: WorkflowStore, status: str | None,
                       submission_status: str | None) -> None:
    with store.transaction() as conn:
        conn.execute("DELETE FROM submission WHERE task_id=?", (SENTINEL,))
        conn.execute("DELETE FROM task WHERE id=?", (SENTINEL,))
        if status is not None:
            timestamp = "2026-01-01T00:00:00+00:00"
            conn.execute("INSERT INTO task(id,request,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                         (SENTINEL, "synthetic current body", status, timestamp, timestamp))
            if submission_status is not None:
                conn.execute("INSERT INTO submission(task_id,status) VALUES(?,?)",
                             (SENTINEL, submission_status))
        store.record_event("synthetic_projection_scenario")


def measure_scenario(store: WorkflowStore, tasks: PersistentTaskManager, name: str,
                     status: str | None, submission_status: str | None,
                     samples: int) -> dict[str, Any]:
    configure_scenario(store, status, submission_status)
    # Untimed independent original-order oracle also guards against false Idle.
    with store.transaction() as conn:
        expected: dict[str, Any] = dict(conn.execute(ORIGINAL_ORDER_SQL).fetchone())
    durations: list[float] = []
    snapshots: list[dict[str, Any] | None] = []
    errors: list[str | None] = []
    for _ in range(samples):
        started = perf_counter()
        try:
            snapshots.append(tasks.presentation_snapshot())
            errors.append(None)
        except ValueError as exc:
            snapshots.append(None)
            errors.append(type(exc).__name__)
        durations.append((perf_counter() - started) * 1000)
    expected_error = status == "UnsupportedSyntheticStatus"
    semantics = all(error == "ValueError" for error in errors) if expected_error else all(
        error is None and snapshot is not None
        and (snapshot["task_id"], snapshot["status"], snapshot["revision"]) == (
            expected["id"], expected["status"], expected["revision"])
        for error, snapshot in zip(errors, snapshots))
    progress_calls = 0

    def progress() -> int:
        nonlocal progress_calls
        progress_calls += 1
        return 0

    # Probe-exclusive stage: keep observer setup under the existing reentrant
    # store lock, but let the public read own its transaction. An expected
    # ValueError must roll back that read, not poison an enclosing transaction.
    # Progress handlers remain absent from the timed samples above.
    with store._lock:
        conn = store._conn
        conn.set_progress_handler(progress, 1000)
        try:
            try:
                tasks.presentation_snapshot()
            except ValueError:
                if not expected_error:
                    raise
        finally:
            conn.set_progress_handler(None, 0)
    return {"scenario": name, "synthetic_status": status, "submission_status": submission_status,
            "semantics_valid": semantics, "expected_error": "ValueError" if expected_error else None,
            "original_order_expected": expected,
            "first_snapshot": snapshots[0], "last_snapshot": snapshots[-1], "raw_error_classes": errors,
            "query_latency": latency(durations), "separate_instrumented_read": {
                "scope": "public snapshot with its own transaction; no enclosing probe transaction",
                "vm_progress_interval": 1000, "progress_callback_count": progress_calls,
                "vm_steps_lower_bound": progress_calls * 1000,
                "vm_steps_exclusive_upper_bound": (progress_calls + 1) * 1000}}


def concurrent_response(store: WorkflowStore, tasks: PersistentTaskManager,
                        samples: int) -> dict[str, Any]:
    configure_scenario(store, "Running", "Claimed")
    start = Barrier(2)
    querying = Event()
    stop = Event()

    def read_loop() -> dict[str, Any]:
        durations: list[float] = []
        valid = True
        start.wait(timeout=10)
        for _ in range(samples * 2):
            if stop.is_set():
                break
            querying.set()
            began = perf_counter()
            try:
                snapshot = tasks.presentation_snapshot()
                valid = valid and snapshot["task_id"] == SENTINEL and snapshot["status"] == "Running"
            finally:
                querying.clear()
            durations.append((perf_counter() - began) * 1000)
        return {"latency": latency(durations), "semantics_valid": valid}

    def cancel_loop() -> dict[str, Any]:
        durations: list[float] = []
        overlapped: list[bool] = []
        valid = True
        start.wait(timeout=10)
        for _ in range(samples):
            if stop.is_set():
                break
            overlapped.append(querying.is_set())
            began = perf_counter()
            # Real persistent cancellation method on a synthetic Running task;
            # no dispatcher, Core, tool, network or external effect is involved.
            task = tasks.request_cancel(SENTINEL)
            durations.append((perf_counter() - began) * 1000)
            valid = valid and task.cancel_requested and task.status.value == "Running"
        return {"latency": latency(durations), "query_in_progress_before_control": overlapped,
                "overlap_observed_count": sum(overlapped), "semantics_valid": valid}

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="v07-synthetic-projection") as pool:
        reading = pool.submit(read_loop)
        controlling = pool.submit(cancel_loop)
        try:
            read_result = reading.result(timeout=30)
            control_result = controlling.result(timeout=30)
        finally:
            stop.set()
    return {"load": "bounded saturated reads, not one-second product polling",
            "query": read_result, "persistent_cancel_control": control_result,
            "semantics_valid": read_result["semantics_valid"] and control_result["semantics_valid"]}


def run_size(root: Path, count: int, samples: int) -> dict[str, Any]:
    path = root / f"history-{count}" / "workflow.sqlite3"
    result: dict[str, Any] = {"history_count": count, "synthetic_database": str(path)}
    store = WorkflowStore(path)
    try:
        result["seed"] = seed_history(store, count)
    finally:
        store.close()
    started = perf_counter()
    store = WorkflowStore(path)
    result["existing_schema_one_index_startup_ms"] = (perf_counter() - started) * 1000
    try:
        tasks = PersistentTaskManager(store)
        with store.transaction() as conn:
            result["indexes"] = [dict(row) for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='index' ORDER BY name")]
            result["schema_version"] = conn.execute("SELECT version FROM schema_version").fetchone()[0]
            plan = [dict(row) for row in conn.execute("EXPLAIN QUERY PLAN " + _PRESENTATION_SQL)]
            result["explain_query_plan"] = plan
            result["explain_mentions_indexes"] = {
                name: any(name in row["detail"] for row in plan) for name in INDEX_NAMES}
            result["original_order_explain"] = [dict(row) for row in conn.execute(
                "EXPLAIN QUERY PLAN " + ORIGINAL_ORDER_SQL)]
        baseline: list[float] = []
        for _ in range(3):
            began = perf_counter()
            with store.transaction() as conn:
                conn.execute(ORIGINAL_ORDER_SQL).fetchone()
            baseline.append((perf_counter() - began) * 1000)
        result["original_full_history_order_latency"] = latency(baseline)
        scenarios: list[tuple[str, str | None, str | None]] = [
            ("terminal_history", None, None),
            ("claimed_created", "Created", "Claimed"),
            ("active_planning", "Planning", "Claimed"),
            ("active_running", "Running", "Claimed"),
            ("queued_created", "Created", "Queued"),
            ("waiting_review", "Waiting", "Blocked"),
            ("unknown_latest_is_rejected", "UnsupportedSyntheticStatus", None),
            ("unknown_queued_is_rejected", "UnsupportedSyntheticStatus", "Queued"),
        ]
        result["scenarios"] = [measure_scenario(store, tasks, *scenario, samples) for scenario in scenarios]
        configure_scenario(store, None, None)
        queue_ids = [f"synthetic-queue-{index:03d}" for index in range(store.queue_capacity)]
        with store.transaction() as conn:
            timestamp = "2026-01-01T00:00:00+00:00"
            conn.executemany(
                "INSERT INTO task(id,request,status,created_at,updated_at) VALUES(?,?,'Created',?,?)",
                ((task_id, "synthetic queued body", timestamp, timestamp) for task_id in queue_ids))
            conn.executemany("INSERT INTO submission(task_id,status) VALUES(?,'Queued')",
                             ((task_id,) for task_id in queue_ids))
        result["scenarios"].append(measure_scenario(
            store, tasks, "full_bounded_queue", None, None, samples))
        with store.transaction() as conn:
            conn.executemany("DELETE FROM submission WHERE task_id=?", ((task_id,) for task_id in queue_ids))
            conn.executemany("DELETE FROM task WHERE id=?", ((task_id,) for task_id in queue_ids))
        result["concurrent_response"] = concurrent_response(store, tasks, samples)
        configure_scenario(store, None, None)
        with store.transaction() as conn:
            conn.execute("UPDATE task SET status='NeedsReview'")
        result["scenarios"].append(measure_scenario(
            store, tasks, "large_review_history_without_terminal", None, None, samples))
    finally:
        store.close()
    started = perf_counter()
    reopened = WorkflowStore(path)
    result["indexed_schema_one_reopen_ms"] = (perf_counter() - started) * 1000
    reopened.close()
    result["semantics_valid"] = (all(scenario["semantics_valid"] for scenario in result["scenarios"])
                                 and result["concurrent_response"]["semantics_valid"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sizes", nargs="+", type=int, default=[10000, 100000])
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True, help="new JSON evidence file; never overwrite")
    arguments = parser.parse_args()
    if (not arguments.sizes or len(arguments.sizes) > 2
            or any(size not in {10000, 100000} for size in arguments.sizes)
            or len(set(arguments.sizes)) != len(arguments.sizes) or not 1 <= arguments.samples <= 100):
        parser.error("sizes must be unique 10000/100000; samples must be 1..100")
    output = arguments.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the evidence destination before running; a repeat needs a new path.
    with output.open("x", encoding="utf-8") as stream:
        root = Path(tempfile.mkdtemp(prefix="deskpal-v07-projection-"))
        report: dict[str, Any] = {"probe": "v07-presentation-indexes-v1", "started_at": datetime.now(timezone.utc).isoformat(),
            "python_executable": sys.executable, "python_version": sys.version,
            "sqlite_version": sqlite3.sqlite_version, "platform": platform.platform(),
            "arguments": {"sizes": arguments.sizes, "samples": arguments.samples},
            "synthetic_root_retained": str(root), "output": str(output),
            "scope": "fresh synthetic workflow databases only; no Core, factory, configuration or cloud",
            "fingerprints": {str(path.relative_to(ROOT)): sha256(path.read_bytes()).hexdigest() for path in (
                Path(__file__).resolve(), ROOT / "src/deskpal/workflow/store.py", ROOT / "src/deskpal/workflow/tasks.py")},
            "results": [], "performance_accepted": None,
            "interpretation": "LIMIT limits returned candidates, not arbitrary-database scan work; inspect plan, VM bounds, actual latencies and queue invariants."}
        exit_code = 0
        try:
            for count in arguments.sizes:
                report["results"].append(run_size(root, count, arguments.samples))
            report["semantics_valid"] = all(result["semantics_valid"] for result in report["results"])
            if not report["semantics_valid"]:
                exit_code = 1
        except Exception as exc:
            exit_code = 1
            report["failure"] = {"type": type(exc).__name__, "traceback": traceback.format_exc()}
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["exit_code"] = exit_code
        serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write(serialized + "\n")
        stream.flush()
    print(serialized)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
