"""Opt-in synthetic workflow capacity, latency, idle, and soak measurements.

No personal configuration, embedding, screen capture, or cloud API is used.
Defaults are short smoke runs; acceptance durations must be requested explicitly.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.metadata
import json
import math
import os
import platform
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deskpal.application.factory import create_application
from deskpal.config import load_config
from deskpal.planner.models import Plan
from deskpal.tools.builtin import EchoTool
from deskpal.tools.models import ToolCall, ToolContext, ToolResult
from deskpal.tasks import TaskStatus
from deskpal.workflow import ExecutionJournal, PersistentTaskManager, WorkflowStore
from deskpal.goals import GoalManager
from deskpal.proactive import ProposalManager


def resources() -> dict[str, Any]:
    result: dict[str, Any] = {"threads": threading.active_count(), "cpu_seconds": time.process_time()}
    if sys.platform != "win32":
        result.update(working_set_bytes=None, peak_working_set_bytes=None, handles=None,
                      method="Windows counters unavailable")
        return result
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]

    kernel, psapi = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    handle = kernel.GetCurrentProcess()
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessHandleCount.restype = wintypes.BOOL
    counters, handles = Counters(), wintypes.DWORD()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
    if not kernel.GetProcessHandleCount(handle, ctypes.byref(handles)):
        raise OSError(ctypes.get_last_error(), "GetProcessHandleCount failed")
    result.update(working_set_bytes=counters.WorkingSetSize, peak_working_set_bytes=counters.PeakWorkingSetSize,
                  private_bytes=counters.PrivateUsage, handles=handles.value,
                  method="GetProcessMemoryInfo/GetProcessHandleCount; working set is process RSS")
    return result


def latencies(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {"samples": len(values), "p95_ms": ordered[max(0, math.ceil(len(values) * .95) - 1)] * 1000 if ordered else None,
            "max_ms": max(values, default=0) * 1000, "sum_seconds": sum(values)}


def metadata(mode: str) -> dict[str, Any]:
    versions: dict[str, str | None] = {}
    for name in ("deskpal", "python-dotenv", "tzdata"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {"benchmark": "deskpal-workflow-v1", "mode": mode, "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(), "python": sys.version,
            "started_at_shanghai": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), "pid": os.getpid(),
            "executable": sys.executable, "platform": platform.platform(), "machine": platform.machine(),
            "logical_cpu_count": os.cpu_count(), "sqlite": sqlite3.sqlite_version, "packages": versions,
            "cpu_denominator": "one logical core = 100%; process_time / monotonic elapsed; not divided by CPU count",
            "scope": "synthetic fresh local databases; offline planner; embedding disabled; no .env or cloud API"}


def save(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


class EchoPlanner:
    def plan(self, request: str, *, memories: Any = (), tools: Any = ()) -> Plan:
        return Plan("synthetic local result", (ToolCall("echo", {"text": "synthetic benchmark"}),))


def run_duration(args: argparse.Namespace, root: Path, report: dict[str, Any]) -> None:
    report["baseline_before_application"] = resources()
    settings = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline", "DESKPAL_EMBEDDING_PROVIDER": "disabled",
        "DESKPAL_DATA_DIR": str(root / "data"), "DESKPAL_WORKSPACE_DIR": str(root / "workspace"),
        "DESKPAL_PROACTIVE_ENABLED": "true"})
    app = create_application(settings, planner=EchoPlanner())
    report["settings"] = {"proactive_enabled": True, "poll_seconds": settings.proactive.poll_interval_seconds,
                           "queue_capacity": settings.workflow.queue_capacity, "embedding": "disabled"}
    iterations, accepted, events = 0, 0, 0
    failed: list[dict[str, Any]] = []
    try:
        goal = app.goals.create("Synthetic soak goal", "User reviews synthetic completed tasks") if args.mode == "soak" else None
        # Warm imports, SQLite connections and background pollers before CPU sampling.
        if args.mode == "soak":
            assert app.run("synthetic warmup").status == TaskStatus.COMPLETED
            assert goal is not None
            future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
            calendar = app.scheduling.create_calendar("Synthetic reused reminder", future, "UTC", reminder_at=future)
            app.goals.bind_source(goal.id, goal.revision, "calendar", calendar.id)
            app.rules.poll()  # Explicit fresh consumer starts before measured events.
            report["soak_scope"] = "one fixed Goal/Calendar/Binding; update reminder occurrence each cycle; actual Core echo plus accepted local proposals"
            report["expected_history_growth"] = "Task/Plan/attempt/audit rows, calendar event occurrences, inbox and Proposal records grow with completed work; active queue, threads and OS handles must remain bounded"
        time.sleep(args.warmup_seconds)
        started, cpu_started = time.monotonic(), time.process_time()
        steady = resources()
        report.update(steady_start=steady, requested_seconds=args.seconds, samples=[])
        report["measurement_started_at"] = datetime.now(timezone.utc).isoformat()
        report["measurement_started_at_shanghai"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        next_tick, next_sample = started, started
        while time.monotonic() - started < args.seconds:
            now = time.monotonic()
            if args.mode == "soak" and now >= next_tick:
                assert goal is not None
                result = app.run("synthetic soak echo")
                if result.status != TaskStatus.COMPLETED:
                    failed.append({"iteration": iterations, "status": result.status.value})
                app.scheduling.update_calendar(calendar.id, reminder_at=datetime.now(timezone.utc).isoformat())
                events += app.scheduling.poll_due()
                app.rules.poll()
                for proposal in app.proposals.list(limit=10):
                    task = app.accept_proposal(proposal.id, proposal.revision, command_id="soak-accept-" + proposal.id)
                    result = app.wait(task.id, timeout=10)
                    if result.status != TaskStatus.COMPLETED:
                        failed.append({"iteration": iterations, "accepted_status": result.status.value})
                    accepted += 1
                iterations += 1
                next_tick += args.interval_seconds
            if now >= next_sample:
                sample = resources()
                sample["elapsed_seconds"] = time.monotonic() - started
                with app.store.transaction() as db:
                    sample["outstanding_queue"] = db.execute("SELECT COUNT(*) FROM submission WHERE status IN ('Queued','Claimed','Blocked')").fetchone()[0]
                    sample["task_count"] = db.execute("SELECT COUNT(*) FROM task").fetchone()[0]
                    sample["proposal_count"] = db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
                    sample["goal_count"] = db.execute("SELECT COUNT(*) FROM goal_objects WHERE kind='goal'").fetchone()[0]
                    sample["binding_count"] = db.execute("SELECT COUNT(*) FROM goal_objects WHERE kind='binding'").fetchone()[0]
                    sample["inbox_count"] = db.execute("SELECT COUNT(*) FROM proactive_inbox").fetchone()[0]
                    sample["pending_inbox"] = db.execute("SELECT COUNT(*) FROM proactive_inbox WHERE status='Pending'").fetchone()[0]
                sample["source_event_tail"] = app.scheduling.event_cursor_state("calendar").tail_id
                sample["rule_error"] = app.diagnostics()["proposal_error"]
                report["samples"].append(sample)
                report.update(elapsed_seconds=time.monotonic() - started, iterations=iterations,
                              accepted_proposals=accepted, manually_polled_due_events=events, failures=failed)
                save(report, args.output)
                print(json.dumps({"mode": args.mode, "seconds": round(report["elapsed_seconds"], 1),
                                  "iterations": iterations, "threads": sample["threads"],
                                  "handles": sample["handles"], "queue": sample["outstanding_queue"]}), flush=True)
                next_sample += args.sample_seconds
            time.sleep(min(.1, max(.001, args.seconds - (time.monotonic() - started))))
        elapsed = time.monotonic() - started
        cpu = time.process_time() - cpu_started
        ending = resources()
        report.update(elapsed_seconds=elapsed, cpu_seconds=cpu, average_one_core_cpu_percent=100 * cpu / elapsed,
                      steady_end=ending, iterations=iterations, accepted_proposals=accepted, failures=failed)
        report["measurement_finished_at"] = datetime.now(timezone.utc).isoformat()
        report["measurement_finished_at_shanghai"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        samples = report["samples"]
        working_sets = [sample["working_set_bytes"] for sample in samples if sample["working_set_bytes"] is not None]
        baseline = report["baseline_before_application"]["working_set_bytes"]
        report["sampled_working_set_increase_bytes"] = max(working_sets + [ending["working_set_bytes"]]) - baseline if working_sets and baseline else None
        report["working_set_increase_bytes"] = max(working_sets + [ending["working_set_bytes"], ending["peak_working_set_bytes"]]) - baseline if working_sets and baseline else None
        report["working_set_baseline_scope"] = "process before ApplicationService construction; includes entire backend overhead, not just workflow"
        required = 300 if args.mode == "idle" else 1800
        report["acceptance_duration_met"] = elapsed >= required
        report["thread_first_last"] = [samples[0]["threads"], ending["threads"]]
        report["handle_first_last"] = [samples[0]["handles"], ending["handles"]]
        report["maximum_outstanding_queue"] = max(sample["outstanding_queue"] for sample in samples)
        report["thread_constant"] = len({sample["threads"] for sample in samples}) == 1
        report["handles_nonincreasing_first_last"] = ending["handles"] <= samples[0]["handles"] if ending["handles"] is not None else None
        report["handle_min_max"] = [min(sample["handles"] for sample in samples), max(sample["handles"] for sample in samples)] if ending["handles"] is not None else None
        report["ongoing_event_submission_cycles"] = iterations > 0 and accepted > 0 and samples[-1]["source_event_tail"] > 0 if args.mode == "soak" else None
        report["passed_resource_targets"] = (report["working_set_increase_bytes"] is not None and
            report["working_set_increase_bytes"] <= 64 * 1024 * 1024 and report["average_one_core_cpu_percent"] <= 2) if args.mode == "idle" else (
                not failed and report["ongoing_event_submission_cycles"] and report["thread_constant"] and report["handles_nonincreasing_first_last"] and
                report["maximum_outstanding_queue"] <= settings.workflow.queue_capacity)
    finally:
        before_close = time.monotonic()
        report["closed"] = app.close(timeout=5)
        report["idle_close_seconds"] = time.monotonic() - before_close
        report["closed_within_5_seconds"] = report["closed"] and report["idle_close_seconds"] <= 5


def _timed_page(function: Any, total: int) -> dict[str, Any]:
    ids: list[str] = []
    maximum = 0
    for offset in range(0, total, 100):
        page = function(limit=100, offset=offset)
        again = function(limit=100, offset=offset)
        assert [item.id for item in page] == [item.id for item in again]
        ids.extend(item.id for item in page)
        maximum = max(maximum, len(page))
    assert len(ids) == total and len(set(ids)) == total
    return {"count": len(ids), "max_page_size": maximum, "stable_repeat_order": True}


def run_capacity(args: argparse.Namespace, root: Path, report: dict[str, Any]) -> None:
    """Build capacity through managers/journal, using actual local EchoTool results.

    Fixture tasks bypass Application/dispatcher/Core and use batch transactions.
    This measures storage capacity; subsequent latency samples use Application.
    """
    started = time.monotonic()
    settings = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline", "DESKPAL_EMBEDDING_PROVIDER": "disabled",
        "DESKPAL_DATA_DIR": str(root / "data"), "DESKPAL_WORKSPACE_DIR": str(root / "workspace")})
    workspace = settings.sandbox.workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    store = WorkflowStore(settings.storage.data_dir / "workflow.sqlite3")
    tasks, journal, goals = PersistentTaskManager(store), ExecutionJournal(store), GoalManager(store)
    proposals = ProposalManager(store, goals, tasks)
    goal_ids: list[str] = []
    report["fixture_scope"] = "Public Goal/Proposal/Task/Journal APIs in batch transactions; 2 actual EchoTool.execute calls per task; synthetic Started/results commit together at batch end; bypass Application/dispatcher/Core for capacity fixture only"
    report["fixture_synthetic_ledger"] = True
    report["fixture_validates_checkpoint_before_effect"] = False
    try:
        for begin in range(0, args.goals, 100):
            with store.transaction():
                for index in range(begin, min(begin + 100, args.goals)):
                    goal_ids.append(goals.create(f"Synthetic capacity Goal {index}", "Explicit human review required").id)
        echo = EchoTool()
        plan = Plan("Synthetic capacity fixture", (ToolCall("echo", {"text": "synthetic first"}, "first"),
                                                    ToolCall("echo", {"text": "synthetic second"}, "second")))
        for begin in range(0, args.tasks, 100):
            with store.transaction():
                for index in range(begin, min(begin + 100, args.tasks)):
                    task = tasks.create(f"synthetic capacity task {index}")
                    tasks.update_status(task.id, TaskStatus.PLANNING)
                    journal.save_plan(task.id, plan, (echo.spec,), str(workspace))
                    tasks.update_status(task.id, TaskStatus.RUNNING)
                    for step_index, call in enumerate(plan.steps):
                        attempt = journal.start_step(task.id, step_index)
                        actual = echo.execute(call.arguments, ToolContext(task.id, workspace))
                        assert actual.success
                        journal.finish_step(attempt.id, ToolResult(True, output=actual.output,
                            tool_name=call.name, call_id=call.call_id))
                    tasks.update_status(task.id, TaskStatus.COMPLETED,
                                        result={"response": "actual local echo fixture completed"})
            if begin % 1000 == 0:
                print(json.dumps({"mode": "capacity", "fixture_tasks": min(begin + 100, args.tasks)}), flush=True)
        due = datetime.now(timezone.utc).isoformat()
        for begin in range(0, args.proposals, 100):
            with store.transaction():
                for index in range(begin, min(begin + 100, args.proposals)):
                    proposal = proposals.create(goal_ids[index % len(goal_ids)], rule_id="synthetic-capacity",
                        root_source_key=f"capacity:{index}", kind="deadline", due_at=due, reason="Synthetic explicit capacity fixture")
                    assert proposal is not None
        with store.transaction() as db:
            report["fixture_counts"] = {"goals": db.execute("SELECT COUNT(*) FROM goal_objects WHERE kind='goal'").fetchone()[0],
                "tasks": db.execute("SELECT COUNT(*) FROM task").fetchone()[0],
                "step_attempts": db.execute("SELECT COUNT(*) FROM attempt").fetchone()[0],
                "proposals": db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]}
        report["capacity_pages"] = {"goals": _timed_page(goals.list, args.goals),
            "tasks": _timed_page(tasks.list_tasks, args.tasks), "proposals": _timed_page(proposals.list, args.proposals)}
        report["fixture_elapsed_seconds"] = time.monotonic() - started
    finally:
        store.close()
    report["capacity_target_met"] = all(report["fixture_counts"][key] >= minimum for key, minimum in
        (("goals", 1000), ("tasks", 10000), ("step_attempts", 20000), ("proposals", 20000)))
    save(report, args.output)
    app = create_application(settings, planner=EchoPlanner())
    durations: dict[str, list[float]] = {"submit": [], "accept": [], "goal_list_100": [], "task_list_100": [], "proposal_list_100": []}
    try:
        accept_candidates = []
        for index in range(args.latency_samples + 5):
            proposal = app.proposals.create(goal_ids[0], rule_id="synthetic-latency", root_source_key=f"latency:{index}",
                kind="deadline", due_at=datetime.now(timezone.utc).isoformat(), reason="Synthetic local latency")
            assert proposal is not None
            accept_candidates.append(proposal)
        for index in range(args.latency_samples + 5):
            mark = time.perf_counter()
            task = app.submit("synthetic warm submit", command_id=f"capacity-latency:{index}")
            elapsed = time.perf_counter() - mark
            assert app.wait(task.id, timeout=30).status == TaskStatus.COMPLETED
            if index >= 5:
                durations["submit"].append(elapsed)
            proposal = accept_candidates[index]
            mark = time.perf_counter()
            task = app.accept_proposal(proposal.id, proposal.revision, command_id=f"capacity-accept:{index}")
            elapsed = time.perf_counter() - mark
            assert app.wait(task.id, timeout=30).status == TaskStatus.COMPLETED
            if index >= 5:
                durations["accept"].append(elapsed)
            for name, query in (("goal_list_100", app.goals.list), ("task_list_100", app.tasks.list_tasks),
                                ("proposal_list_100", app.proposals.list)):
                mark = time.perf_counter()
                page = query(limit=100)
                elapsed = time.perf_counter() - mark
                assert len(page) <= 100
                if index >= 5:
                    durations[name].append(elapsed)
        report["warm_latencies"] = {name: latencies(values) for name, values in durations.items()}
        report["latency_scope"] = "100 measured after 5 warmups; public Application submit/accept transaction only, wait for actual Core execution outside timing; each list asks 100 rows"
        report["latency_target_met"] = args.latency_samples >= 100 and all(value["p95_ms"] <= 200 for value in report["warm_latencies"].values())
        report["sqlite_integrity"] = "ok"
        with app.store.transaction() as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        report["resources_after_capacity"] = resources()
    finally:
        mark = time.monotonic()
        report["closed"] = app.close(timeout=5)
        report["idle_close_seconds"] = time.monotonic() - mark
        report["closed_within_5_seconds"] = report["closed"] and report["idle_close_seconds"] <= 5


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("idle", "soak", "capacity"), default="idle")
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--warmup-seconds", type=float, default=2)
    parser.add_argument("--interval-seconds", type=float, default=1)
    parser.add_argument("--sample-seconds", type=float, default=30)
    parser.add_argument("--goals", type=int, default=1000)
    parser.add_argument("--tasks", type=int, default=10000)
    parser.add_argument("--proposals", type=int, default=20000)
    parser.add_argument("--latency-samples", type=int, default=100)
    parser.add_argument("--output", type=Path, default=ROOT / "data/validation/v04/workflow-benchmark.json")
    args = parser.parse_args()
    if any(not math.isfinite(value) or value <= 0 for value in (args.seconds, args.interval_seconds, args.sample_seconds)) or not math.isfinite(args.warmup_seconds) or args.warmup_seconds < 0:
        parser.error("durations and intervals must be finite and positive")
    if min(args.goals, args.tasks, args.proposals, args.latency_samples) < 1:
        parser.error("capacity counts and latency samples must be positive")
    report = metadata(args.mode)
    try:
        with tempfile.TemporaryDirectory(prefix="deskpal-v04-" + args.mode + "-") as temporary:
            report["synthetic_root"] = temporary
            if args.mode == "capacity":
                run_capacity(args, Path(temporary), report)
            else:
                run_duration(args, Path(temporary), report)
        report["status"] = "complete"
        if args.mode == "capacity":
            report["acceptance_requested"] = args.goals >= 1000 and args.tasks >= 10000 and args.proposals >= 20000 and args.latency_samples >= 100
            report["acceptance_passed"] = report["capacity_target_met"] and report["latency_target_met"] and report["closed_within_5_seconds"]
        else:
            report["acceptance_requested"] = args.seconds >= (300 if args.mode == "idle" else 1800)
            report["acceptance_passed"] = report["acceptance_duration_met"] and report["passed_resource_targets"] and report["closed_within_5_seconds"]
    except BaseException as error:
        report["status"], report["error"] = "failed", type(error).__name__ + ": " + str(error)
        raise
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["finished_at_shanghai"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        save(report, args.output)
    print(str(args.output.resolve()), flush=True)
    return 1 if report["acceptance_requested"] and not report["acceptance_passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
