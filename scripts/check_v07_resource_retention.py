"""Two live Python-allocation snapshots during one frozen native 160s probe.

Only settled acknowledgements for cycles 2 and 19 are observed. This is a
targeted attribution run, not another soak or a performance acceptance run.
It neither explains all OS private bytes nor changes the preserved soak result.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sys
from time import monotonic, perf_counter_ns
import tracemalloc
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "data" / "validation" / "v07"
TARGET = Path(__file__).with_name("check_v07_pet.py").resolve()
FROZEN_SHA256 = "5f23b888c922e4aa4e67fad53581f259f9afb3f6e5c10bc27a36e94e64a9c988"
ACK_QUALNAME = "main.<locals>.control.<locals>.ack"
CAPTURE_CYCLES = (2, 19)
TRACEBACK_DEPTH = 10


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def closure_value(callback: Any, name: str) -> Any:
    """Read one existing closure cell temporarily, without copying the closure."""
    code = callback.__code__
    for key, cell in zip(code.co_freevars, callback.__closure__ or ()):
        if key == name:
            return cell.cell_contents
    raise ValueError("required original acknowledgement cell is missing")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--installed-package-root", required=True, type=Path,
                        help="Exact prepared installed deskpal package directory")
    args = parser.parse_args()
    output, package = args.output_dir.resolve(), args.installed_package_root.resolve()
    if sys.platform != "win32":
        parser.error("This single installed/native attribution run requires Windows")
    if output == EVIDENCE.resolve() or not output.is_relative_to(EVIDENCE.resolve()):
        parser.error("output-dir must be a NEW directory beneath data/validation/v07")
    if not package.is_dir() or not package.is_relative_to(EVIDENCE.resolve()):
        parser.error("installed-package-root must be an explicit prepared package beneath data/validation/v07")
    if digest(TARGET) != FROZEN_SHA256:
        parser.error("target differs from frozen 5F23; do not run")
    if tracemalloc.is_tracing():
        parser.error("tracemalloc is already active; cannot own this diagnostic's trace interval")
    output.mkdir(parents=True, exist_ok=False)
    helper = Path(__file__).resolve()
    # Include the earlier independent helper only as an unchanged source hash.
    preview_helper = helper.with_name("check_v07_preview_trace.py")
    installed_sources = tuple(package / name for name in (
        "gui/pet_controller.py", "gui/pet_window.py", "gui/pet_assets.py", "gui/host.py"))
    tracked = (helper, TARGET, preview_helper, *installed_sources)
    fingerprints_before = {str(path): digest(path) for path in tracked}
    original_argv = [str(TARGET), "--mode", "cycles", "--seconds", "160", "--native", "--installed",
                     "--output-dir", str(output / "probe-output")]
    metadata: dict[str, Any] = {"schema_version": 1, "status": "prepared",
        "helper_argv": sys.argv, "original_probe_argv": original_argv, "executable": sys.executable,
        "installed_package_root": str(package), "fingerprints_before": fingerprints_before,
        "capture_cycles": list(CAPTURE_CYCLES), "traceback_depth": TRACEBACK_DEPTH,
        "callback_qualname": ACK_QUALNAME, "callback_interval_ms": 0,
        "captures": [], "failures": [], "original_main_entered": False,
        "original_exit_code": None, "original_exception_type": None, "setup_exception_type": None,
        "scope": "one original native/installed 160s 20-cycle run, unchanged assertions and return; two post-settled-ack Python heap snapshots only",
        "exit_policy": "process preserves original main return/exception; diagnostic_status and diagnostic_exit_code independently fail on missing snapshots/instrumentation errors",
        "limitations": [
            "Python traced live allocations only: native Qt/image buffers, OS heaps, allocator-reserved pages and all OS private bytes are not fully visible",
            "The first Snapshot and diagnostic records remain alive at the second capture; snapshot/traceback bookkeeping contributes its own overhead, not a product leak",
            "get_tracemalloc_memory measures tracer bookkeeping, not every byte of snapshot storage or total diagnostic overhead",
            "Frozen-probe retained sample/timing arrays and diagnostic allocations are reported separately, never estimated away from OS private bytes",
            "OS resource sequence/age belong to the earlier original settled sample, not to the heap capture instant",
            "Taking snapshots blocks this diagnostic callback and adds allocation/CPU overhead; its timing is not performance acceptance evidence",
            "No object values, task bodies, private configuration or desktop pixels are read; no forced GC or trace clearing is performed",
            "A short Python attribution result cannot prove absence of native leaks or replace the preserved 1800s first failure"],
        "writes": "metadata before Qt; heap dumps, diffs and final metadata only after original main returns/raises"}
    (output / "retention-metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    snapshots: dict[int, tracemalloc.Snapshot] = {}
    scheduled: set[int] = set()
    target_filename = os.path.normcase(str(TARGET))
    tracer_started_ns = perf_counter_ns()
    previous_argv = sys.argv
    timer_class: Any = None
    shot_descriptor: Any = None
    patched = False

    def failure(code: str) -> None:
        if code not in metadata["failures"]:
            metadata["failures"].append(code)

    def capture(callback: Any, cycle: int) -> None:
        """All host/report/Qt references are local to this required capture call."""
        record: dict[str, Any] = {"cycle": cycle, "ack_returned": True,
            "capture_enter_monotonic_seconds": monotonic(), "verified_released_ready": False}
        metadata["captures"].append(record)
        try:
            report = closure_value(callback, "report")
            host = closure_value(callback, "host")
            if type(report) is not dict:
                raise TypeError("original report is not a dictionary")
            settled = next((item for item in reversed(report["cycle_settled"])
                            if item["cycle"] == cycle), None)
            ack = next((item for item in reversed(report["controls"])
                        if item["cycle"] == cycle and item["name"] == "settled"), None)
            if settled is None or ack is None:
                raise ValueError("original settled sample or original completed acknowledgement is missing")
            # Copy only known scalar metadata. Do not retain report, host, window
            # or pixmap references in any capture record or Snapshot side data.
            record["original_settled"] = {key: settled.get(key) for key in (
                "cycle", "elapsed_seconds", "resource_sequence", "resource_available", "resource_fresh",
                "resource_age_seconds", "resource_started_age_seconds", "resource_started_elapsed_seconds",
                "resource_completed_elapsed_seconds", "resource_error", "private_bytes", "handles", "os_threads",
                "cpu_seconds", "cache_bytes", "loader_busy", "retired_windows", "pet_settled", "pet_enabled", "lifecycle")}
            record["original_ack_seconds"] = ack["seconds"]
            pet = host.pet
            live = {"bridge": host.bridge.state, "pet_settled": pet.settled,
                "window_absent": pet.window is None, "loader_busy": pet.loader.busy,
                "cache_bytes": pet.cache_bytes, "retired_windows": len(pet._retired_windows),
                "pet_enabled": host.preferences.pet_enabled, "lifecycle": pet.lifecycle}
            record["live_state_before_snapshot"] = live
            original_ok = (settled.get("resource_fresh") is True and settled.get("resource_error") is None
                and settled.get("pet_settled") is True and settled.get("loader_busy") is False
                and settled.get("cache_bytes") == 0 and settled.get("retired_windows") == 0
                and settled.get("pet_enabled") is False and settled.get("lifecycle") == "Disabled")
            live_ok = (live["bridge"] == "Ready" and live["pet_settled"] and live["window_absent"]
                and not live["loader_busy"] and live["cache_bytes"] == 0 and live["retired_windows"] == 0
                and not live["pet_enabled"] and live["lifecycle"] == "Disabled")
            record["verified_released_ready"] = bool(original_ok and live_ok)
            if not record["verified_released_ready"]:
                failure("capture_state_not_released_ready:" + str(cycle))
            # The old resource age remains labelled as an original-settled
            # observation. Only the elapsed time since the control began is
            # available here; it is not an exact OS sample age at heap capture.
            record["capture_after_control_start_seconds"] = monotonic() - closure_value(callback, "before")
            record["traced_before_bytes"], record["traced_peak_before_bytes"] = tracemalloc.get_traced_memory()
            record["tracer_bookkeeping_before_bytes"] = tracemalloc.get_tracemalloc_memory()
            snapshot_started_ns = perf_counter_ns()
            record["heap_snapshot_started_monotonic_seconds"] = monotonic()
            snapshots[cycle] = tracemalloc.take_snapshot()
            record["heap_snapshot_completed_monotonic_seconds"] = monotonic()
            record["heap_snapshot_seconds"] = (perf_counter_ns() - snapshot_started_ns) / 1_000_000_000
            record["traced_after_bytes"], record["traced_peak_after_bytes"] = tracemalloc.get_traced_memory()
            record["tracer_bookkeeping_after_bytes"] = tracemalloc.get_tracemalloc_memory()
            record["snapshot_trace_count"] = len(snapshots[cycle].traces)
        except Exception as exc:
            record["instrumentation_exception_type"] = type(exc).__name__
            failure("capture_instrumentation_error:" + str(cycle) + ":" + type(exc).__name__)

    def category(filename: str) -> str:
        if filename.startswith("<"):
            return "stdlib_other"
        path = Path(filename).resolve()
        if path == TARGET:
            return "frozen_probe"
        if path == helper:
            return "retention_helper"
        if path.is_relative_to(package):
            return "installed_deskpal"
        return "stdlib_other"

    tracemalloc.start(TRACEBACK_DEPTH)
    try:
        sys.path.insert(0, str(package.parent))
        os.environ["QT_QPA_PLATFORM"] = "windows"
        package_spec = importlib.util.find_spec("deskpal")
        if package_spec is None or package_spec.origin is None or Path(package_spec.origin).resolve().parent != package:
            raise RuntimeError("deskpal did not resolve from the explicit installed target")
        from PySide6.QtCore import QTimer
        timer_class = QTimer
        shot_descriptor = vars(QTimer)["singleShot"]
        original_shot = QTimer.singleShot
        spec = importlib.util.spec_from_file_location("v07_frozen_retention_target", TARGET)
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load explicit frozen target")
        target = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(target)

        def single_shot(*values: Any, **keywords: Any) -> Any:
            if len(values) != 2 or keywords or type(values[0]) is not int or values[0] != 0:
                return original_shot(*values, **keywords)
            callback = values[1]
            code = getattr(callback, "__code__", None)
            if (code is None or code.co_qualname != ACK_QUALNAME
                    or os.path.normcase(code.co_filename) != target_filename):
                return original_shot(*values, **keywords)
            try:
                name, cycle = closure_value(callback, "name"), closure_value(callback, "cycle")
                if name != "settled" or type(cycle) is not int or cycle not in CAPTURE_CYCLES:
                    return original_shot(*values, **keywords)
                if cycle in scheduled:
                    failure("duplicate_target_ack:" + str(cycle))
                    return original_shot(*values, **keywords)
                scheduled.add(cycle)
            except Exception as exc:
                failure("ack_selection_error:" + type(exc).__name__)
                return original_shot(*values, **keywords)

            def observed() -> Any:
                # The original functor is held only for its original timer and
                # this required post-ack read; no Qt object escapes capture().
                result = callback()
                capture(callback, cycle)
                return result

            return original_shot(values[0], observed)

        setattr(QTimer, "singleShot", staticmethod(single_shot))
        patched = True
        sys.argv = original_argv
        metadata["original_main_entered"] = True
        result: int = target.main()
        metadata["original_exit_code"] = result
        return result
    except BaseException as exc:
        label = "original_exception_type" if metadata["original_main_entered"] else "setup_exception_type"
        metadata[label] = type(exc).__name__
        failure(label + ":" + type(exc).__name__)
        raise
    finally:
        sys.argv = previous_argv
        if patched:
            setattr(timer_class, "singleShot", shot_descriptor)
        # The original Qt loop has ended before the following dump/comparison.
        # Stop tracing only after both live snapshots; retained Snapshot data is
        # still available, and post-run analysis cannot contaminate either one.
        metadata["tracing_interval_seconds"] = (perf_counter_ns() - tracer_started_ns) / 1_000_000_000
        tracemalloc.stop()
        metadata["patch_restored"] = patched
        metadata["scheduled_cycles"] = sorted(scheduled)
        metadata["captured_cycles"] = sorted(snapshots)
        for cycle in CAPTURE_CYCLES:
            if cycle not in snapshots:
                failure("missing_heap_snapshot:" + str(cycle))
        try:
            for cycle, snapshot in snapshots.items():
                snapshot.dump(str(output / ("heap-cycle-" + str(cycle) + ".snapshot")))
            if all(cycle in snapshots for cycle in CAPTURE_CYCLES):
                diffs = snapshots[19].compare_to(snapshots[2], "lineno")
                records: list[dict[str, Any]] = []
                totals: dict[str, dict[str, int]] = {name: {"size_diff": 0, "count_diff": 0,
                    "positive_size_diff": 0, "negative_size_diff": 0, "allocation_locations": 0}
                    for name in ("frozen_probe", "retention_helper", "installed_deskpal", "stdlib_other")}
                for stat in diffs:
                    frame = stat.traceback[0]
                    group = category(frame.filename)
                    records.append({"category": group, "filename": frame.filename, "lineno": frame.lineno,
                        "size": stat.size, "size_diff": stat.size_diff,
                        "count": stat.count, "count_diff": stat.count_diff})
                    total = totals[group]
                    total["size_diff"] += stat.size_diff
                    total["count_diff"] += stat.count_diff
                    total["positive_size_diff"] += max(0, stat.size_diff)
                    total["negative_size_diff"] += min(0, stat.size_diff)
                    total["allocation_locations"] += 1
                (output / "allocation-diff.json").write_text(json.dumps({
                    "comparison": "cycle19 minus cycle2 live snapshots", "group_by": "filename+lineno",
                    "categories": totals, "allocation_locations": records,
                    "category_scope": "allocation location only; stdlib_other includes third-party and tracer support; native/untraced allocations excluded",
                    "os_private_adjusted": False}, ensure_ascii=False, indent=2), encoding="utf-8")
                metadata["allocation_categories"] = totals
        except Exception as exc:
            failure("snapshot_export_or_comparison_error:" + type(exc).__name__)
        after = {str(path): digest(path) for path in tracked}
        if after != fingerprints_before:
            failure("source_fingerprint_changed")
        metadata.update(status="returned" if metadata["original_exit_code"] is not None else "raised",
            fingerprints_after=after, fingerprints_unchanged=after == fingerprints_before,
            diagnostic_status="failed" if metadata["failures"] else "captured_for_review",
            diagnostic_exit_code=1 if metadata["failures"] else 0,
            assessment="live Python attribution evidence only; no OS-byte subtraction, no leak-free claim, no change to original soak/probe assertions")
        (output / "retention-metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
