"""Bounded V3 commit-memory/IOCP observations; never stage-zero acceptance.

Only a separately built, pinned V3 (32 x 16MiB maximum, freed before output)
is executable. No quota override, arbitrary executable or V2 memory fallback.
The existing private adapter injection intercepts resume AFTER resource ownership
registration and _verify; all original token, Job, CPU and child restrictions
remain enabled. No public launcher API is changed.

An exclusive host IOCP is associated with the suspended worker's Job. SetJob7,
NEW_PROCESS key/PID, IsProcessInJob, QueryJob3 and QueryJob9 form the binding
chain. QueryJob7 is deliberately never used. Microsoft documents that existing
processes are reported on association, and that hard-limit delivery is NOT
guaranteed. Matching message 9 is per-process evidence; only 10 is job-wide.
Absent messages remain unknown regardless of allocation error or peak commit.
https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_associate_completion_port

Cleanup first reaps the worker/Job, then drains a bounded notification queue,
closes host observation handles/port, and finally deletes the fresh profile.
Unknown ownership/reaping/close retains the owner and blocks profile deletion.
This is not a kill-on-last-Job-handle-close experiment.
"""

from __future__ import annotations

import argparse
import ctypes as C
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
from time import monotonic
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deskpal.execution.runtime import create_probe_profile, delete_probe_profile  # noqa: E402
from deskpal.execution.windows import JobLimits, LaunchRejected, LaunchSpec, RunningWorker, WorkerResult  # noqa: E402
from deskpal.execution.windows._win32 import (  # noqa: E402
    Api, BOOL, DWORD, HANDLE, JOB_EXTENDED_LIMIT, SIZE, _bind, _CREATE_LOCK, _create_worker,
)
from diagnose_worker_limits import (  # noqa: E402
    CaseCleanup as BaseCleanup, ObservationHandles, ObserverApi, ReportWriter, _new_report_path, _no_reparse,
)
from diagnose_worker_startup import native_context, set_caller_localappdata  # noqa: E402

FIXTURE_SHA = "613d1d91e6f2115e067e2a16421d477e13f4e3ca3ace3485610c03b570903cb2"
MODES = ("smoke", "bounded-memory")
UNIT = 16 * 1024 * 1024
CEILING = 32 * UNIT
LOG_LIMIT = 64 * 1024
MAX_MESSAGES = 128
_UNCONFIRMED_OWNERS: list[MemoryCleanup] = []


class COMPLETION_ASSOCIATION(C.Structure):
    _fields_ = [("CompletionKey", HANDLE), ("CompletionPort", HANDLE)]


class SINGLE_PROCESS_LIST(C.Structure):
    _fields_ = [("NumberOfAssignedProcesses", DWORD), ("NumberOfProcessIdsInList", DWORD),
                ("ProcessIdList", SIZE * 1)]


class MemoryApi(ObserverApi):
    def __init__(self) -> None:
        super().__init__()
        kernel = C.WinDLL("kernel32", use_last_error=True)
        self.create_port = _bind(kernel, "CreateIoCompletionPort", [HANDLE, HANDLE, SIZE, DWORD], HANDLE)
        self.get_packet = _bind(kernel, "GetQueuedCompletionStatus",
            [HANDLE, C.POINTER(DWORD), C.POINTER(SIZE), C.POINTER(HANDLE), DWORD], BOOL)
        self.set_job = _bind(kernel, "SetInformationJobObject", [HANDLE, C.c_int, HANDLE, DWORD], BOOL)
        self.in_job = _bind(kernel, "IsProcessInJob", [HANDLE, HANDLE, C.POINTER(BOOL)], BOOL)
        self.process_id = _bind(kernel, "GetProcessId", [HANDLE], DWORD)
        self.set_handle_info = _bind(kernel, "SetHandleInformation", [HANDLE, DWORD, DWORD], BOOL)
        self.get_handle_info = _bind(kernel, "GetHandleInformation", [HANDLE, C.POINTER(DWORD)], BOOL)


class MemoryObservation(ObservationHandles):
    def __init__(self, api: MemoryApi) -> None:
        super().__init__(api)
        self.memory_api = api
        self.port: int | None = None
        self.pid: int | None = None
        self.key = uuid4().int & ((1 << (8 * C.sizeof(SIZE))) - 1) or 1
        self.binding_verified = False
        self.messages: list[dict[str, int]] = []
        self.drain_finished = False
        self.drain_error: str | None = None
        self.drained = False

    def _duplicate(self, source: int, attribute: str, access: int) -> None:
        target = HANDLE()
        try:
            if not self.api.duplicate(self.api.current, source, self.api.current,
                                      C.byref(target), access, False, 0):
                raise LaunchRejected("memory_duplicate_" + attribute, C.get_last_error())
        finally:
            setattr(self, attribute, int(target.value) if target.value else None)
        if getattr(self, attribute) is None:
            raise LaunchRejected("memory_duplicate_null_" + attribute)

    def bind_suspended(self, job: int, process: int) -> None:
        if self.port is not None or self.job is not None or self.process is not None:
            raise LaunchRejected("memory_binding_reused")
        self._duplicate(job, "job", 0x4)  # query only; never inherited
        self._duplicate(process, "process", 0x101000)  # sync | query limited
        self.pid = int(self.memory_api.process_id(process))
        if not self.pid:
            raise LaunchRejected("memory_process_id", C.get_last_error())
        self.port = int(self.memory_api.create_port(HANDLE(-1), None, 0, 1) or 0) or None
        if self.port is None:
            raise LaunchRejected("memory_create_port", C.get_last_error())
        if not self.memory_api.set_handle_info(self.port, 1, 0):
            raise LaunchRejected("memory_port_clear_inherit", C.get_last_error())
        flags = DWORD()
        if not self.memory_api.get_handle_info(self.port, C.byref(flags)) or flags.value & 1:
            raise LaunchRejected("memory_port_inheritance_unconfirmed", C.get_last_error())
        association = COMPLETION_ASSOCIATION(self.key, self.port)
        if not self.memory_api.set_job(job, 7, C.byref(association), C.sizeof(association)):
            raise LaunchRejected("memory_set_job_port", C.get_last_error())
        # Suspended existing member is reported on association. Missing delivery
        # is unknown and prevents resume, rather than falling back to PID alone.
        deadline = monotonic() + 0.5
        found = False
        for _ in range(32):
            if monotonic() >= deadline:
                break
            packet = self._packet(10)
            if packet is not None:
                if packet["message"] != 6 or packet["pid"] != self.pid:
                    raise LaunchRejected("memory_unexpected_suspended_packet")
                found = True
                break
        if not found:
            raise LaunchRejected("memory_new_process_notification_unknown")
        member = BOOL()
        if not self.memory_api.in_job(process, job, C.byref(member)) or member.value != 1:
            raise LaunchRejected("memory_membership_unconfirmed", C.get_last_error())
        pids = SINGLE_PROCESS_LIST()
        if not self.api.query_job(job, 3, C.byref(pids), C.sizeof(pids), None):
            raise LaunchRejected("memory_job_pid_query", C.get_last_error())
        if (pids.NumberOfAssignedProcesses != 1 or pids.NumberOfProcessIdsInList != 1
                or pids.ProcessIdList[0] != self.pid):
            raise LaunchRejected("memory_job_pid_mismatch")
        limits = JOB_EXTENDED_LIMIT()
        if not self.api.query_job(job, 9, C.byref(limits), C.sizeof(limits), None):
            raise LaunchRejected("memory_job_limits_query", C.get_last_error())
        if (limits.BasicLimitInformation.LimitFlags != 0x2308
                or limits.BasicLimitInformation.ActiveProcessLimit != 1
                or limits.ProcessMemoryLimit != 256 * 1024 * 1024
                or limits.JobMemoryLimit != 256 * 1024 * 1024):
            raise LaunchRejected("memory_job_limits_mismatch")
        self.binding_verified = True

    def _packet(self, wait_ms: int) -> dict[str, int] | None:
        if len(self.messages) >= MAX_MESSAGES:
            raise LaunchRejected("memory_notification_budget")
        message, key, pid = DWORD(), SIZE(), HANDLE()
        ok = self.memory_api.get_packet(self.port, C.byref(message), C.byref(key), C.byref(pid), wait_ms)
        if not ok:
            error = C.get_last_error()
            if error == 258 and pid.value is None:
                return None
            raise LaunchRejected("memory_get_packet", error)
        packet = {"message": int(message.value), "key": int(key.value), "pid": int(pid.value or 0)}
        self.messages.append(packet)
        if key.value != self.key:
            raise LaunchRejected("memory_packet_key_mismatch")
        if message.value not in (3, 4, 6, 7, 8, 9, 10):
            raise LaunchRejected("memory_unexpected_message")
        if ((message.value in (6, 7, 8, 9, 10) and packet["pid"] != self.pid)
                or (message.value in (3, 4) and packet["pid"] != 0)):
            raise LaunchRejected("memory_packet_pid_mismatch")
        return packet

    def close(self) -> None:
        # Called only after BaseCleanup has closed every adapter owner and
        # checked active0/process-signaled using still-open observer handles.
        if not self.drain_finished:
            try:
                if self.port is not None:
                    deadline = monotonic() + 0.25
                    for _ in range(MAX_MESSAGES):
                        if monotonic() >= deadline:
                            raise LaunchRejected("memory_drain_deadline")
                        if self._packet(10) is None:
                            self.drained = True
                            break
                    if not self.drained:
                        raise LaunchRejected("memory_notification_budget")
            except LaunchRejected as error:
                # Notification failure invalidates attribution, but must not
                # prevent known-reaped OS resources from being closed.
                self.drain_error = error.stage
            finally:
                self.drain_finished = True
        failures: list[BaseException] = []
        try:
            super().close()
        except BaseException as error:
            failures.append(error)
        if self.port is not None:
            if self.api.close_handle(self.port):
                self.port = None
            else:
                failures.append(LaunchRejected("memory_close_port", C.get_last_error()))
        if failures:
            raise failures[0]

    def evidence(self) -> dict[str, object]:
        reliable = self.binding_verified and self.drained and self.drain_error is None
        codes = {packet["message"] for packet in self.messages}
        return {"binding_verified": self.binding_verified, "pid": self.pid,
                "messages": list(self.messages), "bounded_drain_empty_observed": self.drained,
                "notification_error": self.drain_error,
                "process_memory_limit_event": "observed" if reliable and 9 in codes else "unknown",
                "job_memory_limit_event": "observed" if reliable and 10 in codes else "unknown",
                "delivery_guaranteed": False, "query_job_7_used": False}


class MemoryCleanup(BaseCleanup):
    def __init__(self, api: MemoryApi) -> None:
        super().__init__(api)
        self.observers: MemoryObservation = MemoryObservation(api)

    def close(self) -> None:
        # A resume-hook failure can have observer duplicates despite no returned
        # worker. Require reaping evidence whenever BOTH observers were captured.
        self.worker_returned = self.worker_returned or (
            self.observers.job is not None and self.observers.process is not None)
        super().close()


class MemoryLaunchApi(Api):
    def __init__(self, observe: MemoryObservation) -> None:
        super().__init__()
        self.observe = observe
        self.assigned_job = 0
        self.assigned_process = 0
        self.resume_intercepted = False
        self._real_assign = self.assign_job
        self._real_resume = self.resume
        self.assign_job = self._assign
        self.resume = self._resume

    def _assign(self, job: Any, process: Any) -> int:
        result = int(self._real_assign(job, process))
        if result:
            self.assigned_job, self.assigned_process = int(job), int(process)
        return result

    def _resume(self, thread: Any) -> int:
        if not self.assigned_job or not self.assigned_process or self.resume_intercepted:
            raise LaunchRejected("memory_resume_hook_invalid_state")
        self.resume_intercepted = True
        self.observe.bind_suspended(self.assigned_job, self.assigned_process)
        return int(self._real_resume(thread))


def launch_observed(spec: LaunchSpec, cleanup: MemoryCleanup) -> RunningWorker:
    api = MemoryLaunchApi(cleanup.observers)
    try:
        with _CREATE_LOCK:
            native, identity = _create_worker(spec, _api=api)
    except LaunchRejected:
        raise
    except BaseException as error:
        # _create_worker either reaped, or raised LaunchRejected with its owner.
        raise LaunchRejected("memory_private_launch_failed") from error
    cleanup.add(native)
    return RunningWorker(native, identity)


def fixed_frame(result: WorkerResult, mode: str) -> dict[str, object]:
    if len(result.frames) != 1:
        raise LaunchRejected("memory_expected_one_frame")
    frame = result.frames[0]
    bools = {"excluded_handle_valid", "allocation_attempted", "allocation_ceiling_reached", "all_allocations_freed"}
    ints = {"fixture_version", "excluded_handle_error", "excluded_handle_exception", "allocation_count",
            "allocation_bytes", "allocation_unit_bytes", "allocation_ceiling_bytes", "allocation_error",
            "freed_count", "freed_bytes", "free_error"}
    if (set(frame) != bools | ints | {"probe", "mode"}
            or frame["probe"] != "native_memory_v3" or frame["mode"] != mode
            or any(type(frame[name]) is not bool for name in bools)):
        raise LaunchRejected("memory_frame_schema")
    for name in ints:
        value = frame[name]
        if type(value) is not int or not 0 <= value <= 0xffffffff:
            raise LaunchRejected("memory_frame_schema")
    count = frame["allocation_count"]
    if (frame["fixture_version"] != 3 or type(count) is not int or count > 32
            or frame["allocation_unit_bytes"] != UNIT or frame["allocation_ceiling_bytes"] != CEILING
            or frame["allocation_bytes"] != count * UNIT
            or frame["freed_count"] != count or frame["freed_bytes"] != count * UNIT
            or frame["free_error"] != 0 or frame["all_allocations_freed"] is not True
            or frame["allocation_ceiling_reached"] != (count == 32)
            or frame["allocation_attempted"] != (mode == "bounded-memory")
            or (mode == "smoke" and (count != 0 or frame["allocation_error"] != 0))
            or (mode == "bounded-memory" and (frame["allocation_error"] == 0) != (count == 32))):
        raise LaunchRejected("memory_frame_inconsistent")
    return frame


def run_mode(mode: str, spec: LaunchSpec, api: MemoryApi) -> tuple[dict[str, object], bool]:
    if (mode not in MODES or spec.args != ("{ipc_write_handle}", "{excluded_handle}", mode)
            or spec.limits != JobLimits() or spec.low_privilege):
        raise ValueError("memory_fixed_mode_and_limits_required")
    cleanup = MemoryCleanup(api)
    entry: dict[str, object] = {"mode": mode, "timeout_seconds": 5.0,
                               "expected_behavior_observed": False, "stage0_passed": False}
    started = monotonic()
    collected: float | None = None
    result: WorkerResult | None = None
    try:
        worker = launch_observed(spec, cleanup)
        cleanup.add(worker)
        cleanup.launch_resolved = True
        cleanup.worker_returned = True
        entry["identity"] = asdict(worker.identity)
        collection_start = monotonic()
        try:
            result = worker.collect(5.0, log_limit_bytes=LOG_LIMIT)
        finally:
            collected = monotonic() - collection_start
        frame = fixed_frame(result, mode)
        denied = frame["excluded_handle_valid"] is False and (
            (frame["excluded_handle_error"] == 6 and frame["excluded_handle_exception"] == 0) or
            (frame["excluded_handle_error"] == 0 and frame["excluded_handle_exception"] == 0xc0000008))
        entry.update(native_frame=frame, exit_code=result.exit_code,
                     allocation_refusal_observed=frame["allocation_error"] != 0,
                     expected_behavior_observed=result.exit_code == 0 and not result.stdout and not result.stderr and denied)
    except LaunchRejected as error:
        cleanup.add(error.cleanup_owner)
        cleanup.launch_resolved = True
        entry.update(rejected=error.stage, winerror=error.winerror)
    except Exception as error:
        entry["error_type"] = type(error).__name__
    finally:
        safe = False
        attempts: list[str] = []
        for _ in range(2):
            try:
                cleanup.close()
            except LaunchRejected as error:
                attempts.append(error.stage)
            else:
                safe = True
                break
        if not safe:
            _UNCONFIRMED_OWNERS.append(cleanup)
        entry.update(cleanup_confirmed=safe, cleanup_errors=attempts,
                     job_after_cleanup=cleanup.after, notifications=cleanup.observers.evidence(),
                     worker_reap="process_and_job_reaped" if safe else "UNCONFIRMED_owner_retained",
                     individual_adapter_handle_closes_verified=False,
                     handle_leak_or_long_run_acceptance=False)
    entry.update(elapsed_seconds=monotonic() - started, collection_elapsed_seconds=collected,
                 bounded_collection=collected is not None and collected <= 7.5,
                 resource_enforcement_accepted=False)
    if not cleanup.observers.binding_verified or cleanup.observers.drain_error is not None:
        entry["expected_behavior_observed"] = False
    return entry, safe


def source_fingerprints() -> dict[str, str]:
    paths = ("src/deskpal/execution/windows/_win32.py", "src/deskpal/execution/windows/launcher.py",
             "src/deskpal/execution/runtime.py", "src/deskpal/execution/protocol.py",
             "scripts/diagnose_worker_startup.py", "scripts/diagnose_worker_limits.py",
             "scripts/diagnose_worker_memory.py", "scripts/probe_windows_launch.py",
             "tests/fixtures/windows/native_memory_probe_v3.c")
    return {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}


def verify_fixture(fixture: Path) -> None:
    if len(FIXTURE_SHA) != 64:
        raise ValueError("memory_v3_pin_unavailable")
    _no_reparse(fixture)
    if sha256(fixture.read_bytes()).hexdigest() != FIXTURE_SHA:
        raise ValueError("memory_v3_fingerprint_mismatch")


def collect_matrix(writer: ReportWriter, report: dict[str, object], fixture: Path,
                   windows: str, api: MemoryApi) -> int:
    verify_fixture(fixture)  # fail before profile creation, including direct callers
    writer.save(report)
    safe_to_delete = True
    code = 0
    profile = create_probe_profile("deskpal.v06." + uuid4().hex)
    runs: list[dict[str, object]] = []
    report["runs"] = runs
    try:
        report.update(profile=asdict(profile), cleanup="pending")
        writer.save(report)
        executable = Path(profile.folder) / "native-memory-fixture-v3.exe"
        _no_reparse(executable)
        with fixture.open("rb") as source, executable.open("xb") as destination:
            shutil.copyfileobj(source, destination)
        verify_fixture(executable)
        temporary = Path(profile.folder) / "Temp"
        _no_reparse(temporary)
        temporary.mkdir(exist_ok=True)
        environment = {"SYSTEMROOT": windows, "WINDIR": windows, "TEMP": str(temporary),
                       "TMP": str(temporary), "LOCALAPPDATA": profile.folder}
        report["child_environment_keys"] = sorted(environment)
        for mode in MODES:
            spec = LaunchSpec(executable, ("{ipc_write_handle}", "{excluded_handle}", mode),
                              Path(profile.folder), profile.sid, environment)
            safe_to_delete = False
            entry, safe_to_delete = run_mode(mode, spec, api)
            runs.append(entry)
            writer.save(report)
            if (not safe_to_delete or entry.get("expected_behavior_observed") is not True
                    or entry.get("bounded_collection") is not True):
                break
        observed = len(runs) == 2 and all(
            item.get("expected_behavior_observed") is True and item.get("bounded_collection") is True
            and item.get("cleanup_confirmed") is True for item in runs)
        report.update(bounded_fixture_behavior_observed=observed,
                      status="bounded_memory_diagnostic_collected_requires_review")
        if not observed:
            code = 2
    except Exception as error:
        report.update(status="memory_diagnostic_failed", error_type=type(error).__name__)
        code = 2
    finally:
        if safe_to_delete and not _UNCONFIRMED_OWNERS:
            try:
                delete_probe_profile(profile)
                report["cleanup"] = "profile_deleted"
            except Exception as error:
                report.update(cleanup="failed_profile_may_remain", cleanup_error_type=type(error).__name__)
                code = 2
        else:
            report["cleanup"] = "withheld_unconfirmed_owner"
            code = 2
        report["source_sha256_end"] = source_fingerprints()
        report["source_unchanged"] = report["source_sha256_end"] == report["source_sha256"]
        if not report["source_unchanged"]:
            report["status"] = "source_changed_diagnostic_not_frozen"
            code = 2
        report.update(stage0_passed=False, resource_enforcement_accepted=False)
        writer.save(report)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt" or C.sizeof(C.c_void_p) != 8:
        parser.error("Windows x64 only")
    path = _new_report_path(args.report)
    fixture = ROOT / "data/v06-stage0/native-memory-fixture-v3/fixture.exe"
    verify_fixture(fixture)
    windows, local, elevated = native_context()
    set_caller_localappdata(local)
    api = MemoryApi()
    report: dict[str, object] = {
        "started_at": datetime.now(timezone.utc).isoformat(), "stage0_passed": False,
        "resource_enforcement_accepted": False, "host_token_elevated": elevated,
        "python_version": sys.version, "python_executable": sys.executable,
        "windows_build": sys.getwindowsversion().build, "fixture_version": 3,
        "fixture_sha256": FIXTURE_SHA, "fixture_allocation_ceiling_bytes": CEILING,
        "limits_requested": asdict(JobLimits()), "source_sha256": source_fingerprints(),
        "unresolved": ["Missing hard-limit messages remain unknown; delivery is not guaranteed",
                       "Equal process and job limits do not independently exercise job-wide enforcement",
                       "Commit requests do not establish physical memory saturation",
                       "Observer Job handle means kill-on-last-handle-close is not tested",
                       "Persistent writable namespace bounds and remaining stage-zero gates remain unaccepted"]}
    writer = ReportWriter(path)
    try:
        code = collect_matrix(writer, report, fixture, windows, api)
    finally:
        writer.close()
    print(json.dumps({"report": str(path), "diagnostic_exit": code, "cleanup": report.get("cleanup")}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
