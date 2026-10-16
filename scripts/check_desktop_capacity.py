"""Hermetic V0.5 facade capacity, SQL bounds, latency and concurrent-read probe.

The workflow fixture reuses benchmark_workflow's public manager/journal fixture.
Memory capacity rows are a direct SQL seed, explicitly not an ingestion test.
No personal configuration, .env, cloud API or network resource is accessed.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from threading import Lock, enumerate as enumerate_threads
from time import perf_counter
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts import benchmark_workflow as benchmark
from deskpal.application.desktop import DesktopFacade
from deskpal.application.factory import create_application
from deskpal.config import load_config
from deskpal.memory.contracts import MemoryDraft, MemorySource
from deskpal.memory.repository import SQLiteMemoryRepository, _draft_data, _source_data


def seed_memory(root: Path, count: int) -> None:
    """Generate schema-valid synthetic accepted heads in one explicit SQL batch.

    This bypasses normal ingestion validation, learning, embeddings and audit.
    It measures current-head/source paging at capacity, not write correctness.
    """
    path = root / "data" / "memory.sqlite3"
    SQLiteMemoryRepository(path).close()
    now = datetime.now(timezone.utc)
    with sqlite3.connect(path) as db:
        sources, revisions, heads = [], [], []
        for index in range(count):
            memory_id, source_id = f"capacity-memory-{index:05d}", f"capacity-source-{index:05d}"
            content = f"synthetic capacity memory {index:05d}"
            source = MemorySource(content, id=source_id, created_at=now)
            draft = MemoryDraft(content, content, valid_from=now)
            sources.append((source_id, json.dumps(_source_data(source))))
            revisions.append((memory_id, 1, "accepted", source_id, json.dumps(_draft_data(draft)),
                now.isoformat(), now.isoformat(), None, "synthetic SQL capacity seed", 0))
            heads.append((memory_id, 1))
        db.executemany("INSERT INTO memory_sources(id,payload) VALUES(?,?)", sources)
        db.executemany("INSERT INTO memory_revisions VALUES(?,?,?,?,?,?,?,?,?,?)", revisions)
        db.executemany("INSERT INTO memory_heads VALUES(?,?)", heads)
        db.execute("UPDATE memory_meta SET value=value+? WHERE name='epoch'", (count,))
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


class SqlBounds:
    """Retain counters, never expanded SQL text or result bodies."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.limited_selects = 0
        self.maximum_limit = 0
        self.notification_scans = 0
        self.maximum_notification_limit = 0
        self.unbounded_ordered_selects = 0

    def __call__(self, statement: str) -> None:
        upper = statement.upper()
        if not upper.startswith("SELECT"):
            return
        relevant = any(table in upper for table in ("FROM TASK", "FROM MEMORY_REVISIONS",
            "FROM GOAL_OBJECTS", "FROM PROPOSALS"))
        match = re.search(r"\bLIMIT\s+(-?\d+)", upper)
        with self.lock:
            if relevant and "ORDER BY" in upper:
                if match is None:
                    self.unbounded_ordered_selects += 1
                else:
                    self.limited_selects += 1
                    self.maximum_limit = max(self.maximum_limit, int(match.group(1)))
            if "FROM AUDIT_OUTBOX WHERE SEQUENCE>" in upper and "ORDER BY SEQUENCE" in upper:
                assert match is not None
                self.notification_scans += 1
                self.maximum_notification_limit = max(self.maximum_notification_limit, int(match.group(1)))

    def summary(self) -> dict[str, Any]:
        return {"limited_selects": self.limited_selects, "maximum_internal_sql_limit": self.maximum_limit,
            "unbounded_ordered_selects": self.unbounded_ordered_selects,
            "notification_scans": self.notification_scans,
            "maximum_notification_scan_limit": self.maximum_notification_limit,
            "method": "sqlite trace counters; internal list sentinel allows LIMIT 101, DTO <=100; no SQL bodies retained"}


def forbidden_unbounded(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("Facade invoked an unbounded compatibility listing")


def check_pages(facade: DesktopFacade, operation: str, expected: int, expected_epoch: int) -> dict[str, Any]:
    print(json.dumps({"phase": "desktop-pages", "operation": operation, "expected": expected}), flush=True)
    parameters: dict[str, Any] = {"limit": 100}
    if operation == "proposals":
        parameters["status"] = None
    seen: set[str] = set()
    offset, pages, maximum = 0, 0, 0
    while True:
        page = facade.execute(operation, {**parameters, "offset": offset})
        again = facade.execute(operation, {**parameters, "offset": offset})
        items = page["items"]
        assert [item["id"] for item in items] == [item["id"] for item in again["items"]]
        assert len(items) <= 100 and page["offset"] == offset
        if operation == "tasks":
            assert all(not (set(item) & {"request", "result", "plan", "attempts", "error"}) for item in items)
        if operation == "memories":
            assert page["memory_epoch"] == expected_epoch
        for item in items:
            assert item["id"] not in seen
            seen.add(item["id"])
        pages += 1
        maximum = max(maximum, len(items))
        if not page["has_more"]:
            break
        offset += 100
    assert len(seen) == expected
    return {"count": len(seen), "pages": pages, "max_dto_items": maximum,
        "stable_repeat_order": True, "duplicate_ids": 0}


def measure(facade: DesktopFacade, samples: int) -> dict[str, Any]:
    measured: dict[str, Any] = {}
    for operation in ("tasks", "memories", "goals", "proposals"):
        values = []
        for index in range(samples + 5):
            args: dict[str, Any] = {"offset": (index % 10) * 100, "limit": 100}
            if operation == "proposals":
                args["status"] = None
            mark = perf_counter()
            result = facade.execute(operation, args)
            duration = perf_counter() - mark
            assert len(result["items"]) <= 100
            if index >= 5:
                values.append(duration)
        measured[operation] = benchmark.latencies(values)
    return measured


def concurrent_reads(facade: DesktopFacade, samples: int) -> dict[str, Any]:
    def read(index: int) -> None:
        operation = ("tasks", "memories", "goals", "proposals")[index % 4]
        args: dict[str, Any] = {"offset": (index % 10) * 100, "limit": 100}
        if operation == "proposals":
            args["status"] = None
        value = facade.execute(operation, args)
        assert 0 < len(value["items"]) <= 100
        assert value["session_id"] == facade.session_id

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="capacity-reader") as pool:
        list(pool.map(read, range(samples)))
    workers = sum(thread.name == "deskpal-workflow" for thread in enumerate_threads())
    assert workers == 1
    return {"requests": samples, "parallel_reader_threads": 4, "failures": 0,
        "core_execution_workers": workers, "scope": "parallel facade reads; command/epoch races covered by integration tests"}


def run(args: argparse.Namespace, root: Path, report: dict[str, Any]) -> None:
    fixture_args = argparse.Namespace(goals=1000, tasks=10000, proposals=20000,
        latency_samples=args.samples, output=args.output)
    benchmark.run_capacity(fixture_args, root, report)
    report["memory_fixture_scope"] = ("10000 schema-valid synthetic accepted heads, unique sources, empty profile keys; "
        "direct SQL batch in newly initialized SQLiteMemoryRepository; bypasses ingestion/learning/embedding/audit; capacity fixture only")
    seed_memory(root, 10000)
    settings = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline", "DESKPAL_EMBEDDING_PROVIDER": "disabled",
        "DESKPAL_DATA_DIR": str(root / "data"), "DESKPAL_WORKSPACE_DIR": str(root / "workspace")})
    app = create_application(settings, planner=benchmark.EchoPlanner())
    trace = SqlBounds()
    facade = DesktopFacade(app)
    runtime = app.core.memory_runtime
    assert runtime is not None
    # A forbidden full-list call fails the entire probe rather than concealing
    # all-materialize-and-slice behind the facade's correct output shape.
    setattr(runtime, "list_items", forbidden_unbounded)
    setattr(app.tasks, "list_tasks", forbidden_unbounded)
    try:
        with app.store.transaction() as db:
            db.set_trace_callback(trace)
            counts = {"goals": int(db.execute("SELECT COUNT(*) FROM goal_objects WHERE kind='goal'").fetchone()[0]),
                "tasks": int(db.execute("SELECT COUNT(*) FROM task").fetchone()[0]),
                "proposals": int(db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]),
                "attempts": int(db.execute("SELECT COUNT(*) FROM attempt").fetchone()[0])}
        # Separate observer connection is unnecessary: inspect only the exact
        # synthetic repository created by this application for SQLite tracing.
        repository = getattr(runtime, "repository")
        assert isinstance(repository, SQLiteMemoryRepository)
        repository._db.set_trace_callback(trace)
        counts["memories"] = int(repository._db.execute("SELECT COUNT(*) FROM memory_heads").fetchone()[0])
        memory_epoch = runtime.epoch
        report["desktop_pages"] = {}
        for operation, count in (("goals", counts["goals"]), ("tasks", counts["tasks"]),
                                 ("proposals", counts["proposals"]), ("memories", counts["memories"])):
            report["desktop_pages"][operation] = check_pages(facade, operation, count, memory_epoch)
            benchmark.save(report, args.output)
        baseline = facade.execute("notification_feed", {})
        assert baseline["baseline"] and not baseline["items"]
        with app.store.transaction():
            for _ in range(205):
                app.store.record_event("synthetic_capacity_noise")
        after, until, scans = baseline["next_sequence"], None, 0
        while True:
            page = facade.execute("notification_feed", {"after_sequence": after, "until_sequence": until, "limit": 100})
            assert len(page["items"]) <= 100 and not page["baseline"]
            assert page["next_sequence"] > after
            after, until = page["next_sequence"], page["until_sequence"]
            scans += 1
            if not page["has_more"]:
                break
        assert scans == 3 and after == until
        report["notification_incremental_probe"] = {"noise_rows": 205, "pages": scans,
            "empty_baseline": True, "unrelated_rows_advance_cursor": True, "fixed_upper_bound": True}
        report["facade_latencies"] = measure(facade, args.samples)
        report["concurrent_reads"] = concurrent_reads(facade, args.samples)
        report["sql_bounds"] = trace.summary()
        assert trace.unbounded_ordered_selects == 0
        assert 0 < trace.maximum_limit <= 101
        assert trace.maximum_notification_limit <= 100
        report["observed_counts_after_workflow_latency"] = counts
        report["memory_epoch"] = memory_epoch
        report["facade_latency_target_met"] = all(item["p95_ms"] <= 300 for item in report["facade_latencies"].values())
        report["resources_after_desktop_probe"] = benchmark.resources()
    finally:
        # Restore compatibility methods before application cleanup can use them.
        delattr(runtime, "list_items")
        delattr(app.tasks, "list_tasks")
        started = perf_counter()
        report["desktop_closed"] = app.close(5)
        report["desktop_close_seconds"] = perf_counter() - started
    report["acceptance_passed"] = bool(report["capacity_target_met"] and report["latency_target_met"]
        and report["facade_latency_target_met"] and report["desktop_closed"] and report["desktop_close_seconds"] <= 5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--output", type=Path, default=ROOT / "data/validation/v05/desktop-capacity-py312.json")
    args = parser.parse_args()
    if args.samples < 100:
        parser.error("at least 100 measured local requests are required")
    report = benchmark.metadata("desktop-capacity")
    report["benchmark"] = "deskpal-desktop-capacity-v05"
    fingerprint = sha256()
    for path in sorted((ROOT / "src").rglob("*.py")):
        fingerprint.update(path.relative_to(ROOT).as_posix().encode())
        fingerprint.update(path.read_bytes())
    report["source_tree_sha256"] = fingerprint.hexdigest()
    report["probe_script_sha256"] = sha256(Path(__file__).read_bytes()).hexdigest()
    started = perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="deskpal-v05-capacity-") as temporary:
            report["synthetic_root"] = temporary
            run(args, Path(temporary), report)
        report["status"] = "complete"
    except BaseException as error:
        report.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        report["elapsed_seconds"] = perf_counter() - started
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        benchmark.save(report, args.output)
    print(json.dumps({"output": str(args.output.resolve()), "acceptance_passed": report["acceptance_passed"],
        "elapsed_seconds": report["elapsed_seconds"]}), flush=True)
    return 0 if report["acceptance_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
