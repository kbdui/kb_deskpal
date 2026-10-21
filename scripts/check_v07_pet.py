"""Opt-in V0.7 probe: temporary offline App/QSettings, exclusive new evidence.

Offscreen results do not certify native transparent input, DPI or visual art.
Use --native for a visible Windows run; no desktop capture or input hooks exist.
"""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
from hashlib import sha256
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
from time import monotonic, process_time
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
RAW_TIMING_LIMIT = 100_000
RESOURCE_INTERVAL_SECONDS = 1.0
RESOURCE_MAX_AGE_SECONDS = 2.0


class ResourceSampler:
    """One Qt-free worker, one latest result, no queued sampling requests.

    Every attempted sample is appended by this worker to its exclusive JSONL.
    The GUI never waits for the lock or performs an OS resource query.
    """

    def __init__(self, path: Path, started: float) -> None:
        self.path, self.started = path, started
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._latest: dict[str, Any] | None = None
        self.log_error: str | None = None
        self.thread = threading.Thread(target=self._run, name="v07-resource-sampler", daemon=False)

    def latest(self) -> dict[str, Any] | None:
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return None if self._latest is None else dict(self._latest)
        finally:
            self._lock.release()

    def request_stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        sequence = 0
        while True:
            final = self._stop.is_set()
            before = monotonic()
            sample: dict[str, Any] = {"resource_sequence": sequence,
                "resource_started_elapsed_seconds": before - self.started,
                "resource_final_sample": final, "resource_error": None}
            try:
                sample.update(process_resources())
                sample["cpu_sample_elapsed_seconds"] = sample.pop("cpu_observed_monotonic_seconds") - self.started
            except Exception as exc:
                sample["resource_error"] = type(exc).__name__
            after = monotonic()
            sample.update(resource_completed_elapsed_seconds=after - self.started,
                          resource_duration_seconds=after - before)
            if sequence >= RAW_TIMING_LIMIT - 1 and not final:
                sample["resource_error"] = "raw_timing_limit_reached:resource_worker"
            try:
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(sample, ensure_ascii=False) + "\n")
            except OSError as exc:
                self.log_error = type(exc).__name__
                sample["resource_error"] = "resource_log_write_failed:" + type(exc).__name__
            with self._lock:
                self._latest = sample
            sequence += 1
            if final or sequence >= RAW_TIMING_LIMIT:
                return
            # Fixed load in enabled/disabled phases; overruns never queue work.
            self._stop.wait(max(0.0, RESOURCE_INTERVAL_SECONDS - (monotonic() - before)))


_WINDOWS_RESOURCE_BINDINGS: tuple[Any, Any, Any, Any, Any] | None = None


def _windows_resource_bindings() -> tuple[Any, Any, Any, Any, Any]:
    """Initialize once in the sole sampler; ctypes caches pointer types globally.

    Recreating Structure classes per sample would give POINTER a new type key
    each time and retain those classes. DLLs and function wrappers also stay fixed.
    """
    global _WINDOWS_RESOURCE_BINDINGS
    if _WINDOWS_RESOURCE_BINDINGS is not None:
        return _WINDOWS_RESOURCE_BINDINGS
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]

    class ThreadEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                    ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD)]

    kernel, psapi = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessHandleCount.restype = wintypes.BOOL
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.Thread32First.restype = wintypes.BOOL
    kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.Thread32Next.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    _WINDOWS_RESOURCE_BINDINGS = kernel, psapi, Counters, ThreadEntry, wintypes.DWORD
    return _WINDOWS_RESOURCE_BINDINGS


def process_resources() -> dict[str, Any]:
    result: dict[str, Any] = {"python_threads": threading.active_count(), "cpu_seconds": process_time(),
                              "cpu_observed_monotonic_seconds": monotonic()}
    if sys.platform != "win32":
        return {**result, "threads": None, "os_threads": None, "os_thread_snapshot_seconds": None,
                "private_bytes": None, "rss_bytes": None, "handles": None}
    kernel, psapi, Counters, ThreadEntry, DWord = _windows_resource_bindings()
    handle = kernel.GetCurrentProcess()
    counters, handles = Counters(), DWord()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        raise OSError("GetProcessMemoryInfo failed")
    if not kernel.GetProcessHandleCount(handle, ctypes.byref(handles)):
        raise OSError("GetProcessHandleCount failed")
    thread_snapshot_started = monotonic()
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    os_threads = 0
    try:
        entry = ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Thread32First(snapshot, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        while True:
            # No thread IDs, foreign process counts, paths or commands are retained.
            if entry.th32OwnerProcessID == os.getpid():
                os_threads += 1
            entry.dwSize = ctypes.sizeof(entry)
            if not kernel.Thread32Next(snapshot, ctypes.byref(entry)):
                if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                    raise ctypes.WinError(ctypes.get_last_error())
                break
    finally:
        kernel.CloseHandle(snapshot)
    return {**result, "private_bytes": counters.PrivateUsage, "rss_bytes": counters.WorkingSetSize,
            "peak_rss_bytes": counters.PeakWorkingSetSize, "handles": handles.value,
            "threads": os_threads, "os_threads": os_threads,
            "os_thread_snapshot_seconds": monotonic() - thread_snapshot_started}


def distribution(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {"samples": len(values), "p95_ms": ordered[max(0, math.ceil(len(values) * .95) - 1)] * 1000
            if ordered else None, "max_ms": max(values, default=0) * 1000}


def fingerprints(package_root: Path) -> dict[str, str | None]:
    # Only current implementation sources and the five fixed assets. No data,
    # defaults, environment files, private profiles or old evidence are scanned.
    paths = list(package_root.rglob("*.py"))
    paths += [package_root / "gui" / "assets" / "pet" / (name + ".png")
              for name in ("neutral", "thinking", "focused", "pleased", "alert")]
    result = {str(path.relative_to(package_root)): sha256(path.read_bytes()).hexdigest() if path.is_file() else None
              for path in sorted(paths)}
    result["__probe__/check_v07_pet.py"] = sha256(Path(__file__).read_bytes()).hexdigest()
    return result


def qt_display_snapshot(app: Any, **windows: Any) -> dict[str, Any]:
    """Only Qt-owned metadata; no screen pixels or global input inspection."""
    screens = [{"name": screen.name(), "geometry_logical": list(screen.geometry().getRect()),
                "available_geometry_logical": list(screen.availableGeometry().getRect()),
                "dpr": screen.devicePixelRatio(), "logical_dpi": screen.logicalDotsPerInch(),
                "logical_dpi_x": screen.logicalDotsPerInchX(),
                "logical_dpi_y": screen.logicalDotsPerInchY()}
               for screen in app.screens()]
    window_states: dict[str, Any] = {}
    for name, window in windows.items():
        if window is None:
            window_states[name] = None
            continue
        screen = window.screen()
        region = window.mask()
        window_states[name] = {"geometry_logical": list(window.geometry().getRect()),
            "frame_geometry_logical": list(window.frameGeometry().getRect()),
            "screen": screen.name() if screen is not None else None,
            "dpr": window.devicePixelRatioF(), "flags": int(window.windowFlags()),
            "visible": window.isVisible(), "active": window.isActiveWindow(),
            "mask_rect_count": region.rectCount(), "mask_bounds_logical": list(region.boundingRect().getRect()),
            "mask_empty": region.isEmpty()}
    active = app.activeWindow()
    focus = app.focusWidget()
    return {"screens": screens, "windows": window_states,
            "active_window": active.objectName() or active.windowTitle() if active is not None else None,
            "focus_widget": focus.objectName() or type(focus).__name__ if focus is not None else None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("smoke", "cycles", "soak"), default="smoke")
    parser.add_argument("--seconds", type=float)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--installed", action="store_true", help="Use installed deskpal without adding source sys.path")
    parser.add_argument("--disabled", action="store_true", help="Same host/control schedule, pet disabled for comparison")
    parser.add_argument("--slow-core-seconds", type=float, default=0)
    parser.add_argument("--slow-loader-seconds", type=float, default=0)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    defaults = {"smoke": 12.0, "cycles": 180.0, "soak": 1800.0}
    seconds = defaults[args.mode] if args.seconds is None else args.seconds
    if (not math.isfinite(seconds) or seconds < 8 or args.mode == "cycles" and seconds < 160
            or args.mode == "soak" and seconds < 1800):
        parser.error("smoke requires >=8s, 20 cycles >=160s, soak >=1800s")
    if any(not math.isfinite(value) or not 0 <= value <= 60
           for value in (args.slow_core_seconds, args.slow_loader_seconds)):
        parser.error("slow-path delay must be between 0 and 60 seconds")
    if args.slow_core_seconds and args.slow_loader_seconds:
        parser.error("Core and image slow paths must be independent evidence runs")
    if args.native and sys.platform != "win32":
        parser.error("--native requires Windows")
    if args.mode == "soak" and args.disabled:
        parser.error("soak contains its own disabled/enabled comparison; omit --disabled")
    evidence_root = ROOT / "data" / "validation" / "v07"
    output = (args.output_dir or evidence_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
              + "-pet-" + args.mode + "-" + uuid4().hex[:8])).resolve()
    if not output.is_relative_to(evidence_root.resolve()):
        parser.error("output-dir must be a new directory beneath data/validation/v07")
    output.mkdir(parents=True, exist_ok=False)
    if not args.installed:
        sys.path.insert(0, str(ROOT / "src"))
    os.environ["QT_QPA_PLATFORM"] = "windows" if args.native else "offscreen"
    from PySide6.QtCore import QEvent, QObject, QSettings, QTimer, qVersion
    from PySide6.QtWidgets import QApplication
    import deskpal.gui.pet_controller as controller_module
    from deskpal.application.factory import create_application
    from deskpal.config import AppConfig, SandboxConfig, StorageConfig
    from deskpal.gui.host import DesktopHost
    from deskpal.gui.pet_assets import load_package_images
    from deskpal.gui.preferences import GuiPreferences
    from deskpal.planner.models import Plan
    from deskpal.tools.models import ToolCall

    package_root = Path(controller_module.__file__).resolve().parents[1]
    initial_fingerprints = fingerprints(package_root)
    report: dict[str, Any] = {"status": "running", "mode": args.mode, "native": args.native,
        "disabled": args.disabled, "python": sys.version, "executable": sys.executable,
        "source_package": str(package_root), "argv": sys.argv, "fingerprints_start": initial_fingerprints,
        "slow_core_seconds": args.slow_core_seconds, "slow_loader_seconds": args.slow_loader_seconds,
        "cpu_denominator": "process CPU seconds / wall seconds; 100% = one logical core",
        "thread_denominator": "own-PID OS/Python counts include the same single resource sampler in enabled/disabled phases; final sample also precedes sampler exit",
        "resource_instrumentation": {"revision": "worker-v1", "before": "full synchronous OS/Toolhelp sampling in GUI tick",
            "after": "one Qt-free worker; single latest slot; nonblocking GUI consumption",
            "resource_sampling_in_gui": False, "heartbeat_timer_or_budget_changed": False,
            "interval_seconds": RESOURCE_INTERVAL_SECONDS, "max_age_seconds": RESOURCE_MAX_AGE_SECONDS,
            "raw_file": "resource-worker.jsonl", "cadence": "fixed start cadence; no queued requests; overruns skip waiting",
            "final_join_timeout_seconds": 10, "sampling_changes_thread_count_by": 1},
        "scope": "temporary offline backend and preferences; no default config/.env/cloud/private data",
        "packages": {name: importlib.metadata.version(name) for name in ("PySide6-Essentials", "shiboken6")},
        "samples": [], "controls": [], "cycle_settled": [], "failures": [], "test_only_previews": [],
        "phases": [], "warmup_attempted": 0, "warmup_completed": 0, "warmup_target": 3,
        "idle_windows": {}, "warmup_baseline": None, "bridge_states": [],
        "stabilization": {"seconds": 5, "warmup_timeout_seconds": 120,
            "enabled_settling_timeout_seconds": 60,
            "criterion": "expected lifecycle/cache/window, loader idle, no retired windows; OS threads and handles unchanged across distinct fresh worker samples spanning >=5s; samples must start after phase entry",
            "passes": [],
            "private_bytes": "raw only; allocator variation is not silently declared stable"},
        "manual_native_input": "not performed by this script; requires separate native observation",
        "started_at": datetime.now(timezone.utc).isoformat()}

    def save(reason: str = "checkpoint", *, final: bool = False) -> None:
        # GUI checkpoints have bounded payload size independent of sample count.
        # All samples stay in samples.jsonl and in the final complete report;
        # worker timings are independently durable in resource-worker.jsonl.
        if final:
            report["is_final_report"] = True
            payload = report
        else:
            payload = {"is_final_report": False, "checkpoint_reason": reason,
                "checkpoint_at": datetime.now(timezone.utc).isoformat(),
                "status": report["status"], "mode": report["mode"], "native": report["native"],
                "started_at": report["started_at"], "executable": report["executable"],
                "source_package": report["source_package"],
                "probe_sha256": initial_fingerprints["__probe__/check_v07_pet.py"],
                "phase": report["phases"][-1] if report["phases"] else None,
                "bridge_state": report["bridge_states"][-1] if report["bridge_states"] else None,
                "sample_count": len(report["samples"]), "control_count": len(report["controls"]),
                "preview_count": len(report["test_only_previews"]),
                "cycle_settled_count": len(report["cycle_settled"]),
                "latest_sample": report["samples"][-1] if report["samples"] else None,
                "failures": report["failures"],
                "raw_files": ["samples.jsonl", "resource-worker.jsonl"],
                "scope": "live checkpoint only; complete arrays/fingerprints/raw timings are saved after Qt exits"}
        (output / "report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    save("prepared")
    app = QApplication([sys.argv[0]])
    app.setQuitOnLastWindowClosed(False)
    report["qt_runtime"] = {"version": qVersion(), "qpa_platform": app.platformName(),
        "qpa_requested": "windows" if args.native else "offscreen"}
    temp = tempfile.TemporaryDirectory(prefix="deskpal-v07-")
    temp_root = Path(temp.name)
    prefs = GuiPreferences(QSettings(str(temp_root / "preferences.ini"), QSettings.Format.IniFormat))
    config = AppConfig(storage=StorageConfig(temp_root / "data"),
                       sandbox=SandboxConfig(workspace_dir=temp_root / "workspace"))

    class Planner:
        def plan(self, request: str, *, memories: Any = (), tools: Any = ()) -> Plan:
            if args.slow_core_seconds:
                threading.Event().wait(args.slow_core_seconds)
            return Plan("synthetic probe", (ToolCall("echo", {"text": "synthetic v07"}),))

    host = DesktopHost(lambda: create_application(config, planner=Planner()), preferences=prefs,
        notification_settings=QSettings(str(temp_root / "notifications.ini"), QSettings.Format.IniFormat),
        quit_on_closed=False)
    if args.slow_loader_seconds:
        def slow_load(generation: int, cancel: threading.Event) -> Any:
            threading.Event().wait(args.slow_loader_seconds)
            return load_package_images(generation, cancel)
        host.pet.loader._load = slow_load
    host.start(visible=args.native)
    report["display_start"] = qt_display_snapshot(app, main=host.window, pet=host.pet.window)
    started = last_heartbeat = monotonic()
    sampler = ResourceSampler(output / "resource-worker.jsonl", started)
    next_sample = 0.0
    heartbeat_intervals: list[float] = []
    heartbeat_raw: list[tuple[float, float]] = []
    tick_body_raw: list[tuple[float, float]] = []
    resource_timing_raw: list[dict[str, Any]] = []
    timing_dropped = {"heartbeat": 0, "tick_body": 0, "resources": 0}
    receipts: list[float] = []
    requests = 0
    real_send = host.bridge.send

    def counted_send(operation: str, parameters: dict[str, Any] | None = None, *, generation: int = 0) -> str:
        nonlocal requests
        if operation == "presentation":
            requests += 1
        return real_send(operation, parameters, generation=generation)

    host.bridge.send = counted_send  # type: ignore[method-assign]
    ready_at: float | None = None
    closing_at: float | None = None
    action_index = 0
    cycle_count = 1 if args.mode == "smoke" else 20
    actions: list[tuple[float, str, int]] = []
    phase = "startup"
    phase_started = started
    stable_since: float | None = None
    stable_signature: tuple[Any, ...] | None = None
    stable_sequence: int | None = None
    cycles_started: float | None = None
    warmup_started: float | None = None
    after_closed = False
    checkpoint_failure_count = 0

    def retain_timing(name: str, values: list[Any], value: Any) -> bool:
        if len(values) < RAW_TIMING_LIMIT:
            values.append(value)
            return True
        timing_dropped[name] += 1
        failure = "raw_timing_limit_reached:" + name
        if failure not in report["failures"]:
            report["failures"].append(failure)
        return False

    observed_resource_sequence: int | None = None
    resource_cache: dict[str, Any] | None = None

    def measured_resources(purpose: str) -> dict[str, Any]:
        nonlocal observed_resource_sequence, resource_cache
        result = sampler.latest()
        if result is not None:
            resource_cache = result
        elif resource_cache is not None:
            result = dict(resource_cache)
        now = monotonic() - started
        if result is None:
            return {"resource_available": False, "resource_fresh": False, "resource_age_seconds": None,
                    "resource_sequence": None, "resource_consumer": purpose}
        age = now - result["resource_completed_elapsed_seconds"]
        started_age = now - result["resource_started_elapsed_seconds"]
        result.update(resource_available=True, resource_age_seconds=age, resource_consumer=purpose,
                      resource_started_age_seconds=started_age,
                      resource_fresh=result["resource_error"] is None and 0 <= started_age <= RESOURCE_MAX_AGE_SECONDS)
        if result["resource_sequence"] != observed_resource_sequence:
            retain_timing("resources", resource_timing_raw, {**result, "gui_observed_elapsed_seconds": now})
            observed_resource_sequence = result["resource_sequence"]
        if result["resource_error"] is not None:
            failure = "resource_sampler_error:" + result["resource_error"]
            if failure not in report["failures"]:
                report["failures"].append(failure)
        return result

    def resource_sample() -> dict[str, Any]:
        return {**measured_resources("resource_sample:" + phase), "elapsed_seconds": monotonic() - started,
            "pet_queries": requests, "cache_bytes": host.pet.cache_bytes,
            "loader_busy": host.pet.loader.busy, "lifecycle": host.pet.lifecycle,
            "retired_windows": len(host.pet._retired_windows), "pet_settled": host.pet.settled,
            "pet_enabled": prefs.pet_enabled, "status": host.pet.status,
            "expression": host.pet.expression, "phase": phase}

    def bridge_state(state: str) -> None:
        report["bridge_states"].append({"state": state, "elapsed_seconds": monotonic() - started})

    host.bridge.state_changed.connect(bridge_state)
    bridge_state(host.bridge.state)

    def begin_phase(name: str) -> None:
        nonlocal phase, phase_started, stable_since, stable_signature, stable_sequence
        phase, phase_started = name, monotonic()
        stable_since = None
        stable_signature = None
        stable_sequence = None
        report["phases"].append({"name": name, "elapsed_seconds": phase_started - started})
        save("phase_changed")

    def begin_cycles() -> None:
        nonlocal cycles_started
        begin_phase("cycles")
        cycles_started = monotonic()
        available = seconds - (cycles_started - started)
        if available < (cycle_count - 1) * 7.5 + 6.5:
            report["failures"].append("insufficient_time_for_uncompressed_cycle_schedule")
        spacing = max(7.5, (available - 7.5) / cycle_count)
        for cycle in range(cycle_count):
            base = cycle * spacing
            actions.extend((base + offset, name, cycle) for offset, name in
                           ((0, "enable"), (1.5, "hide"), (2.5, "show"), (3.5, "options"),
                            (4.5, "disable"), (6.5, "settled")))
        if args.native and args.mode == "smoke" and seconds > 10:
            actions.append((7.5, "enable", cycle_count))

    def stable_for_five_seconds(enabled: bool, now: float) -> bool:
        nonlocal stable_since, stable_signature, stable_sequence
        if enabled:
            healthy = (host.pet.settled and host.pet.window is not None
                       and host.pet.cache_bytes > 0 and host.pet.lifecycle == "Visible")
        else:
            healthy = (host.pet.settled and host.pet.window is None
                       and host.pet.cache_bytes == 0 and host.pet.lifecycle == "Disabled")
        if not healthy:
            stable_since, stable_signature, stable_sequence = None, None, None
            return False
        resources = measured_resources("stabilization:" + phase)
        if (not resources["resource_fresh"]
                or resources["resource_started_elapsed_seconds"] < phase_started - started):
            stable_since, stable_signature, stable_sequence = None, None, None
            return False
        if resources["resource_sequence"] == stable_sequence:
            return False  # A cached result cannot lengthen the measured stable span.
        stable_sequence = resources["resource_sequence"]
        observed_at = resources["resource_started_elapsed_seconds"]
        signature = (resources["os_threads"], resources["handles"], host.pet.cache_bytes,
                     len(host.pet._retired_windows))
        if signature != stable_signature:
            stable_since, stable_signature = observed_at, signature
        stable = stable_since is not None and observed_at - stable_since >= 5
        if stable and stable_since is not None:
            report["stabilization"]["passes"].append({"phase": phase,
                "first_sample_started_elapsed_seconds": stable_since,
                "last_sample_started_elapsed_seconds": observed_at,
                "last_resource_sequence": stable_sequence, "span_seconds": observed_at - stable_since,
                "signature": signature})
        return stable

    def finish_idle(name: str) -> bool:
        window = report["idle_windows"][name]
        end = resource_sample()
        if not end["resource_fresh"]:
            return False
        wall = end["cpu_sample_elapsed_seconds"] - window["start"]["cpu_sample_elapsed_seconds"]
        if wall < 300:
            return False
        window["end"] = end
        cpu = end["cpu_seconds"] - window["start"]["cpu_seconds"]
        window.update(wall_seconds=wall, process_cpu_seconds=cpu, cpu_percent_one_core=100 * cpu / wall)
        return True

    def soak_step(now: float) -> None:
        nonlocal warmup_started
        if warmup_started is not None and phase in {"warmup_enabled", "warmup_disabled", "disabled_baseline"}:
            if now - warmup_started > 120:
                report["failures"].append("warmup_or_disabled_baseline_timeout_120_seconds")
                begin_phase("failed")
                return
        if phase == "warmup_enabled" and stable_for_five_seconds(True, now):
            prefs.set_pet_enabled(False)
            begin_phase("warmup_disabled")
        elif phase == "warmup_disabled" and stable_for_five_seconds(False, now):
            report["warmup_completed"] += 1
            report.setdefault("warmup_settled", []).append(resource_sample())
            if report["warmup_completed"] < report["warmup_target"]:
                report["warmup_attempted"] += 1
                prefs.set_pet_enabled(True)
                begin_phase("warmup_enabled")
            else:
                begin_phase("disabled_baseline")
        elif phase == "disabled_baseline" and stable_for_five_seconds(False, now):
            report["warmup_baseline"] = resource_sample()
            begin_phase("disabled_idle")
            report["idle_windows"]["disabled"] = {"start": resource_sample()}
        elif phase == "disabled_idle":
            if prefs.pet_enabled or not host.pet.settled or host.pet.cache_bytes:
                report["failures"].append("disabled_idle_state_changed")
                begin_phase("failed")
            elif finish_idle("disabled"):
                prefs.set_pet_visible(True)
                prefs.set_pet_enabled(True)
                begin_phase("enabled_settling")
        elif phase == "enabled_settling":
            if stable_for_five_seconds(True, now):
                begin_phase("enabled_idle")
                report["idle_windows"]["enabled"] = {"start": resource_sample()}
            elif now - phase_started > 60:
                report["failures"].append("enabled_idle_stabilization_timeout_60_seconds")
                begin_phase("failed")
        elif phase == "enabled_idle":
            if not prefs.pet_enabled or not host.pet.settled or host.pet.cache_bytes == 0:
                report["failures"].append("enabled_idle_state_changed")
                begin_phase("failed")
            elif finish_idle("enabled"):
                disabled = report["idle_windows"]["disabled"]["cpu_percent_one_core"]
                enabled = report["idle_windows"]["enabled"]["cpu_percent_one_core"]
                report["idle_cpu_comparison"] = {"disabled_cpu_percent_one_core": disabled,
                    "enabled_cpu_percent_one_core": enabled, "increment_percentage_points": enabled - disabled,
                    "limit_percentage_points": 1.0,
                    "load": "same idle host/probe timers and sampling; no synthetic submit in either window",
                    "assessment": "raw comparison; no automatic acceptance"}
                prefs.set_pet_enabled(False)
                begin_cycles()

    class PreviewObserver(QObject):
        """Probe-only one-shot paint observation; never changes task/controller state."""

        def __init__(self) -> None:
            super().__init__(host)
            self.pending: tuple[Any, Any, dict[str, Any]] | None = None

        def eventFilter(self, watched: QObject, event: QEvent) -> bool:
            pending = self.pending
            if pending is not None and watched is pending[0] and event.type() == QEvent.Type.Paint:
                self.pending = None
                window, pixmap, record = pending
                record["paint_event_elapsed_seconds"] = monotonic() - started

                def painted() -> None:
                    same_window = host.pet.window is window
                    record.update(paint_completed_observed=True,
                        presented_cached_id=(record["requested_id"] if same_window
                            and window._pixmap is pixmap and not host.pet._render_broken else None),
                        authority_status_after=host.pet.status,
                        controller_expression_after=host.pet.expression)
                    if same_window:
                        window.removeEventFilter(self)

                QTimer.singleShot(0, painted)
            return False

    preview_observer = PreviewObserver()

    def preview(cycle: int) -> None:
        names = ("neutral", "thinking", "focused", "pleased", "alert")
        name = names[cycle % len(names)]
        window, pixmap = host.pet.window, host.pet._cache.get(name)
        record: dict[str, Any] = {"cycle": cycle, "requested_id": name,
            "elapsed_seconds": monotonic() - started, "test_only": True,
            "authority_status_before": host.pet.status, "controller_expression_before": host.pet.expression,
            "cached_ids": sorted(host.pet._cache), "paint_completed_observed": False,
            "presented_cached_id": None,
            "scope": "direct fixed cached-pixmap preview; no authority snapshot, Core grant or controller-expression mutation"}
        report["test_only_previews"].append(record)
        if window is None or pixmap is None or not window.isVisible():
            record["error"] = "preview_window_or_fixed_asset_unavailable"
            report["failures"].append("test_only_preview_unavailable:" + str(cycle))
            return
        record["pixmap_physical_size"] = [pixmap.width(), pixmap.height()]
        record["display_before"] = qt_display_snapshot(app, pet=window)
        preview_observer.pending = (window, pixmap, record)
        window.installEventFilter(preview_observer)
        window.present(pixmap, window._status + " · 诊断预览", "probe-only:" + name)

        def restore_authority() -> None:
            if host.pet.window is window:
                window.removeEventFilter(preview_observer)
                host.pet._render()
            if preview_observer.pending is not None and preview_observer.pending[2] is record:
                preview_observer.pending = None
            if record["presented_cached_id"] != name:
                report["failures"].append("test_only_preview_paint_unobserved:" + str(cycle))
            record["restored_elapsed_seconds"] = monotonic() - started

        QTimer.singleShot(250, restore_authority)

    def control(name: str, cycle: int) -> None:
        before = monotonic()
        if name == "enable":
            prefs.set_pet_visible(True)
            prefs.set_pet_enabled(not args.disabled)
            if host.bridge.state == "Ready":
                host.bridge.send("submit", {"request": "synthetic v07", "use_policy": "local_only",
                                           "command_id": "v07-probe-" + str(cycle)})
        elif name == "hide":
            prefs.set_pet_visible(False)
        elif name == "show":
            prefs.set_pet_visible(True)
        elif name == "options":
            prefs.set_pet_on_top(not prefs.pet_on_top)
            prefs.set_pet_click_through(True)
            prefs.set_pet_click_through(False)
            if args.mode in {"cycles", "soak"} and not args.disabled:
                preview(cycle)
        elif name == "disable":
            prefs.set_pet_enabled(False)
        elif name == "settled":
            report["cycle_settled"].append({"cycle": cycle, **resource_sample(),
                "display": qt_display_snapshot(app, main=host.window, pet=host.pet.window)})
            if not host.pet.settled or host.pet.cache_bytes or prefs.pet_enabled:
                report["failures"].append("cycle_not_settled:" + str(cycle))
        # A queued acknowledgement measures return to the GUI event loop, including
        # synchronous control cost, rather than recording only setter duration.
        def ack() -> None:
            latency = monotonic() - before
            receipts.append(latency)
            report["controls"].append({"name": name, "cycle": cycle, "seconds": latency})
            report["controls"][-1]["display"] = qt_display_snapshot(app, main=host.window, pet=host.pet.window)
        QTimer.singleShot(0, ack)

    def tick() -> None:
        nonlocal last_heartbeat, next_sample, ready_at, action_index, closing_at, after_closed
        nonlocal warmup_started, checkpoint_failure_count
        now = monotonic()
        interval = now - last_heartbeat
        if retain_timing("heartbeat", heartbeat_raw, (now - started, interval)):
            heartbeat_intervals.append(interval)
        last_heartbeat = now
        elapsed = now - started
        if host.bridge.state == "Ready" and ready_at is None:
            ready_at = now
            report["ready_seconds"] = elapsed
            if args.mode == "soak":
                warmup_started = now
                report["warmup_attempted"] += 1
                prefs.set_pet_visible(True)
                prefs.set_pet_enabled(True)
                begin_phase("warmup_enabled")
            else:
                begin_cycles()
        if ready_at is not None and closing_at is None:
            if args.mode == "soak":
                soak_step(now)
            active_elapsed = now - cycles_started if cycles_started is not None else 0
            while cycles_started is not None and action_index < len(actions) and active_elapsed >= actions[action_index][0]:
                _, name, cycle = actions[action_index]
                control(name, cycle)
                action_index += 1
        if elapsed >= next_sample:
            sample = resource_sample()
            report["samples"].append(sample)
            # Constant-size append avoids increasingly large report rewrites biasing
            # the second CPU window. The final report still contains every sample.
            with (output / "samples.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(sample, ensure_ascii=False) + "\n")
            next_sample += 1
        if closing_at is None and elapsed >= seconds:
            closing_at = monotonic()
            report["shutdown_requested_seconds"] = closing_at - started
            host.bridge.shutdown()
        if host._finalized and not after_closed:
            after_closed = True
            finished = monotonic()
            report["shutdown_seconds"] = finished - closing_at if closing_at is not None else None
            report["shutdown_completed_seconds"] = finished - started
            report["resource_sampler_shutdown"] = {"requested_elapsed_seconds": monotonic() - started,
                "request_origin": "host_finalized", "join_in_gui": False}
            sampler.request_stop()
            timer.stop()
            app.quit()
        if closing_at is not None and now - closing_at > 90:
            # Preserve owner and event loop. A blocked worker must not be destroyed
            # merely to make a benchmark exit or produce a false normal result.
            if "shutdown_exceeded_90_seconds" not in report["failures"]:
                report["failures"].append("shutdown_exceeded_90_seconds")
        if len(report["failures"]) != checkpoint_failure_count:
            save("new_failure")
            checkpoint_failure_count = len(report["failures"])
        retain_timing("tick_body", tick_body_raw, (now - started, monotonic() - now))

    timer = QTimer(host)
    timer.setInterval(50)
    report["heartbeat_timer"] = {"interval_ms": timer.interval(), "timer_type": timer.timerType().name,
        "type_was_overridden": False}
    timer.timeout.connect(tick)
    sampler.thread.start()
    timer.start()
    try:
        exit_code = app.exec()
    finally:
        shutdown = report.setdefault("resource_sampler_shutdown", {
            "requested_elapsed_seconds": monotonic() - started,
            "request_origin": "Qt_loop_ended", "join_in_gui": False})
        sampler.request_stop()
        join_started = monotonic()
        # Qt's event loop has ended. This is the only join, explicitly bounded;
        # on timeout the live non-daemon owner remains retained and exit fails.
        sampler.thread.join(timeout=10)
        shutdown.update(join_started_elapsed_seconds=join_started - started,
            join_seconds=monotonic() - join_started, stopped=not sampler.thread.is_alive(),
            timeout_seconds=10, completed_elapsed_seconds=monotonic() - started)
    final_resources = measured_resources("final_resources")
    sampler_closed = (shutdown["stopped"] and final_resources.get("resource_final_sample") is True
        and final_resources.get("resource_error") is None
        and final_resources.get("resource_started_elapsed_seconds", -1) >= shutdown["requested_elapsed_seconds"])
    shutdown["final_sample_after_stop_request"] = sampler_closed
    if not sampler_closed:
        report["failures"].append("resource_sampler_shutdown_or_final_sample_failed")
    if sampler.log_error is not None:
        report["failures"].append("resource_sampler_log_failed:" + sampler.log_error)
    report.update(status="completed" if after_closed else "interrupted", qt_exit_code=exit_code,
        elapsed_seconds=monotonic() - started, final_resources=final_resources,
        heartbeat=distribution(heartbeat_intervals), control_ack=distribution(receipts),
        fingerprints_end=fingerprints(package_root), pet_diagnostics=host.pet.diagnostics)
    report["timing_raw"] = {"limit_per_series": RAW_TIMING_LIMIT, "dropped": timing_dropped,
        "heartbeat_columns": ["elapsed_seconds", "interval_seconds"], "heartbeat": heartbeat_raw,
        "tick_body_columns": ["start_elapsed_seconds", "duration_seconds"], "tick_body": tick_body_raw,
        "resources": resource_timing_raw,
        "resource_worker_file": "resource-worker.jsonl",
        "interpretation": "Heartbeat/tick body are GUI timings; resources are asynchronously observed worker samples. Every worker attempt and Toolhelp duration is in resource-worker.jsonl. No scheduling cause inferred."}
    states = [item["state"] for item in report["bridge_states"]]
    report["normalClosing"] = (closing_at is not None and "Closing" in states and "Closed" in states
        and after_closed and host._finalized and host.bridge.state == "Closed" and host.pet.settled
        and sampler_closed and exit_code == 0)
    report["closing_final_state"] = {"bridge": host.bridge.state, "host_finalized": host._finalized,
        "pet_settled": host.pet.settled, "loader_busy": host.pet.loader.busy,
        "cache_bytes": host.pet.cache_bytes, "retired_windows": len(host.pet._retired_windows)}
    report["display_end"] = qt_display_snapshot(app, main=host.window, pet=host.pet.window)
    if report["fingerprints_end"] != initial_fingerprints:
        report["failures"].append("implementation_changed_during_probe")
    if ready_at is None:
        report["failures"].append("backend_never_ready")
    if len(report["cycle_settled"]) != cycle_count:
        report["failures"].append("cycle_schedule_incomplete")
    if args.mode == "soak" and (report["warmup_baseline"] is None
            or "end" not in report["idle_windows"].get("disabled", {})
            or "end" not in report["idle_windows"].get("enabled", {})):
        report["failures"].append("matched_hot_idle_comparison_incomplete")
    if args.mode in {"cycles", "soak"} and not args.disabled:
        presented = {item["presented_cached_id"] for item in report["test_only_previews"]
                     if item["paint_completed_observed"]}
        report["test_only_preview_coverage"] = {"expected": ["neutral", "thinking", "focused", "pleased", "alert"],
            "paint_observed_cached_ids": sorted(name for name in presented if name is not None),
            "authority_status_coverage_claimed": False}
        if presented != {"neutral", "thinking", "focused", "pleased", "alert"}:
            report["failures"].append("test_only_five_fixed_asset_paint_coverage_incomplete")
    report["assessment"] = "raw evidence only; budget/visual/native acceptance requires review"
    save(final=True)
    if host.pet.settled and host.bridge.state == "Closed":
        temp.cleanup()
    print(str(output / "report.json"), flush=True)
    return 0 if after_closed and not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
