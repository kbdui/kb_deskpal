"""Bounded diagnostic repeats of the GUI prefix after collecting the full suite.

This retains full-collection import pressure, per-test progress and thread dumps.
Passing repeats are evidence of non-reproduction, never proof of a fixed cause.
Requires the gui-dev extra. All exercised tests use synthetic local data.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import faulthandler
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PREFIXES = ("tests/gui/test_bridge.py::", "tests/gui/test_desktop.py::", "tests/gui/test_host.py::")


def fingerprint(*, tests: bool = False) -> str:
    digest = sha256()
    paths = sorted((ROOT / ("tests" if tests else "src")).rglob("*.py"))
    for path in paths if tests else paths + [ROOT / "pyproject.toml"]:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def child(result_path: Path, trace_gc: bool, trace_qobjects: bool) -> int:
    # Match `python -m pytest` root imports (including scripts.benchmark_workflow).
    sys.path.insert(0, str(ROOT))
    # Optional observers import before pytest applies pythonpath=['src'].
    sys.path.insert(0, str(ROOT / "src"))
    import pytest

    if trace_gc or trace_qobjects:
        from threading import current_thread

        # pytest fd capture replaces fd 2 during passing tests. Retain the
        # original parent log destination, including interpreter-exit GC. This
        # single non-inheritable descriptor is reclaimed at child process exit.
        trace_fd = os.dup(2)
        os.set_inheritable(trace_fd, False)
        write_trace = os.write
        get_thread = current_thread

    if trace_gc:
        import gc

        def trace_collection(phase: str, info: dict[str, int]) -> None:
            write_trace(trace_fd, (f"\nDIAGNOSTIC GC {phase} generation={info['generation']} "
                                  f"thread={get_thread().name}\n").encode("utf-8"))

        gc.callbacks.append(trace_collection)

    if trace_qobjects:
        from itertools import count
        from threading import get_ident
        from PySide6.QtCore import Qt
        from deskpal.gui.bridge import ApplicationBridge

        sequence = count()
        original_init = ApplicationBridge.__init__
        # Observers retain only primitive metadata, never the observed QObject.
        def observe(obj: Any, kind: str) -> None:
            token, owner = next(sequence), get_ident()
            def record(event: str) -> None:
                write_trace(trace_fd, (f"\nDIAGNOSTIC QOBJECT kind={kind} token={token} event={event} "
                    f"created_ident={owner} actual_ident={get_ident()} thread={get_thread().name}\n").encode("utf-8"))
            def destroyed() -> None:
                record("destroyed")
            record("created")
            obj.destroyed.connect(destroyed, Qt.ConnectionType.DirectConnection)

        def observed_init(bridge: Any, *values: Any, **options: Any) -> None:
            original_init(bridge, *values, **options)
            observe(bridge, "ApplicationBridge")
            parent = bridge.parent()
            if parent is not None and type(parent).__name__ == "DesktopHost":
                observe(parent, "DesktopHost")

        # This ordinary constructor is not a registered Qt slot. The patch is
        # confined to this diagnostic child and changes no destruction policy.
        setattr(ApplicationBridge, "__init__", observed_init)

    class Progress:
        def __init__(self) -> None:
            self.collected = self.selected = self.passed = self.failed = self.errors = self.skipped = 0

        def pytest_collection_modifyitems(self, config: Any, items: list[Any]) -> None:
            self.collected = len(items)
            selected = [item for item in items if item.nodeid.startswith(PREFIXES)]
            deselected = [item for item in items if not item.nodeid.startswith(PREFIXES)]
            config.hook.pytest_deselected(items=deselected)
            items[:] = selected
            self.selected = len(selected)

        def pytest_runtest_logstart(self, nodeid: str, location: Any) -> None:
            print(f"\nDIAGNOSTIC START {nodeid} monotonic={monotonic():.3f}", flush=True)

        def pytest_runtest_logfinish(self, nodeid: str, location: Any) -> None:
            print(f"\nDIAGNOSTIC END {nodeid} monotonic={monotonic():.3f}", flush=True)

        def pytest_runtest_logreport(self, report: Any) -> None:
            if report.when == "call":
                self.passed += int(report.passed)
                self.failed += int(report.failed)
            elif report.failed:
                self.errors += 1
            self.skipped += int(report.skipped)

    progress = Progress()
    faulthandler.enable(all_threads=True)
    faulthandler.dump_traceback_later(20, repeat=True)
    code = int(pytest.main(["-vv", "--setup-show", "--strict-config", "--strict-markers", "--tb=short",
                           "-o", "faulthandler_timeout=20", str(ROOT / "tests")], plugins=[progress]))
    origins: dict[str, str] = {}
    for name, module in tuple(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if (name == "deskpal" or name.startswith("deskpal.")) and isinstance(filename, str):
            origins[name] = str(Path(filename).resolve())
    result_path.write_text(json.dumps({"python": sys.version, "collected": progress.collected,
        "selected": progress.selected, "passed": progress.passed, "failed": progress.failed,
        "errors": progress.errors, "skipped": progress.skipped, "pytest_exit_code": code,
        "deskpal_module_origins": origins,
        "all_deskpal_from_src": bool(origins) and all(Path(path).is_relative_to(ROOT / "src")
            for path in origins.values())}, indent=2), encoding="utf-8")
    print("DIAGNOSTIC PYTEST RETURNED", flush=True)
    # Cover a possible interpreter-exit stall as well as the pytest execution.
    faulthandler.dump_traceback_later(20, repeat=True)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=float, default=90)
    parser.add_argument("--output", type=Path, default=ROOT / "data/validation/v05/gui-prefix-investigation.json")
    parser.add_argument("--trace-gc", action="store_true", help="Observe GC phases/threads without forcing or disabling collection")
    parser.add_argument("--trace-qobjects", action="store_true", help="Observe Bridge/Host destroyed signals without retaining instances")
    parser.add_argument("--child-result", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child_result is not None:
        return child(args.child_result, args.trace_gc, args.trace_qobjects)
    if not args.python.is_file() or not 1 <= args.repetitions <= 100 or not 25 <= args.timeout_seconds <= 600:
        parser.error("require an existing interpreter, 1..100 repetitions and 25..600s per process")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    logs = output.parent / (output.stem + "-" + uuid4().hex[:8])
    logs.mkdir()
    report: dict[str, Any] = {"started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "full-suite collection then GUI bridge/desktop/host prefix; bounded separate processes; no cloud API",
        "source_fingerprint_start": fingerprint(), "script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "test_fingerprint_start": fingerprint(tests=True), "gc_observation": args.trace_gc,
        "qobject_observation": args.trace_qobjects,
        "interpreter": str(args.python.resolve()), "requested_repetitions": args.repetitions,
        "per_process_timeout_seconds": args.timeout_seconds, "runs": [], "status": "running",
        "interpretation": "Passing repetitions do not identify or fix the original untraced stall."}

    def save() -> None:
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Prevent ambient opt-in gates/credentials or Qt options from changing this probe.
    environment = {key: os.environ[key] for key in os.environ
        if not key.upper().startswith(("DESKPAL_", "QT_", "PYTEST_"))
        and key.upper() not in {"DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}}
    environment.update(QT_QPA_PLATFORM="offscreen", PYTEST_QT_API="pyside6", PYTHONUNBUFFERED="1")
    save()
    for index in range(args.repetitions):
        log_path = logs / f"run-{index + 1:03}.log"
        child_path = logs / f"run-{index + 1:03}.json"
        started = monotonic()
        timed_out = False
        with log_path.open("wb") as stream:
            command = [str(args.python.resolve()), str(Path(__file__).resolve()), "--child-result", str(child_path)]
            if args.trace_gc:
                command.append("--trace-gc")
            if args.trace_qobjects:
                command.append("--trace-qobjects")
            process = subprocess.Popen(command, cwd=ROOT, env=environment,
                stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT)
            try:
                code = process.wait(timeout=args.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                code = process.wait(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
        outcome = json.loads(child_path.read_text(encoding="utf-8")) if child_path.exists() else None
        passed = bool(not timed_out and code == 0 and outcome is not None
            and outcome["selected"] > 0 and outcome["passed"] == outcome["selected"]
            and not outcome["failed"] and not outcome["errors"] and not outcome["skipped"]
            and outcome["all_deskpal_from_src"])
        entry = {"iteration": index + 1, "elapsed_seconds": monotonic() - started,
            "exit_code": code, "timed_out": timed_out, "passed": passed,
            "log_path": str(log_path), "result": outcome}
        report["runs"].append(entry)
        save()
        print(json.dumps({key: entry[key] for key in ("iteration", "elapsed_seconds", "exit_code", "timed_out", "passed")}), flush=True)
        if not passed:
            break
    report.update(source_fingerprint_end=fingerprint(), test_fingerprint_end=fingerprint(tests=True), status="completed",
        finished_at=datetime.now(timezone.utc).isoformat())
    report["all_repetitions_passed"] = bool(len(report["runs"]) == args.repetitions
        and all(run["passed"] for run in report["runs"])
        and report["test_fingerprint_start"] == report["test_fingerprint_end"]
        and report["source_fingerprint_start"] == report["source_fingerprint_end"])
    save()
    return 0 if report["all_repetitions_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
