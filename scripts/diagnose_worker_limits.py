"""Fixed synthetic public-launcher limits matrix; NOT stage-zero acceptance.

Run only with explicit native-experiment approval. This helper uses one new
project AppContainer profile, the pinned v2 fixture, the canonical five-key
environment and unmodified WindowsLauncher restrictions. It never accepts an
executable, SID, handle, quota or environment override.

The executable matrix contains smoke/spin/child/flood/invalid-frame only. V2's
memory mode can request 16 GiB and is deliberately UNEXECUTED; this helper has
no switch that enables it. Spin uses one thread and cannot establish saturation
of the 20-percent aggregate CPU cap. Child refusal does not distinguish the
child policy, active-process limit, or another simultaneous failure.

Two noninheritable, reduced-access HOST observer duplicates preserve Job query
and process wait/exit evidence after collect() closes the launcher's handles.
They refer only to the returned worker's existing handles. Their lifetime delays
the last Job handle close, so this is NOT a kill-on-last-handle-close test;
normal adapter cleanup explicitly terminates the Job and confirms reaping.

Official ownership/accounting contracts:
https://learn.microsoft.com/en-us/windows/win32/api/handleapi/nf-handleapi-duplicatehandle
https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information
https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information
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
from typing import TextIO
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deskpal.execution.protocol import ProtocolViolation  # noqa: E402
from deskpal.execution.runtime import create_probe_profile, delete_probe_profile  # noqa: E402
from deskpal.execution.windows import (  # noqa: E402
    JobLimits, LaunchRejected, LaunchSpec, RunningWorker, WindowsLauncher, WorkerResult,
)
from deskpal.execution.windows._win32 import (  # noqa: E402
    BOOL, DWORD, HANDLE, JOB_ACCOUNTING, JOB_EXTENDED_LIMIT, _bind,
)
from deskpal.execution.windows.launcher import CleanupOwner  # noqa: E402
from diagnose_worker_startup import native_context, set_caller_localappdata  # noqa: E402

FIXTURE_SHA = "08423adb75615e3609e268409be844053bf2e14af1d4d250917006c0ba7ab4e1"
MODES = ("smoke", "spin", "child", "flood", "invalid-frame")
LOG_LIMIT = 64 * 1024
_UNCONFIRMED_OWNERS: list[CleanupOwner] = []


def _no_reparse(path: Path) -> None:
    for item in (path.absolute(), *path.absolute().parents):
        if item.is_symlink() or (item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("limits_reparse_ancestor")


def _new_report_path(path: Path) -> Path:
    _no_reparse(path)
    result = path.resolve()
    if (not result.is_relative_to((ROOT / "data/validation/v06").resolve()) or result.exists()
            or result.suffix.lower() != ".json" or any(":" in part for part in result.parts[1:])
            or any(Path(part).is_reserved() for part in result.parts[1:])):
        raise ValueError("limits_report_requires_new_project_validation_file")
    return result


def source_fingerprints() -> dict[str, str]:
    files = (
        "src/deskpal/__init__.py", "src/deskpal/execution/__init__.py",
        "src/deskpal/execution/protocol.py", "src/deskpal/execution/runtime.py",
        "src/deskpal/execution/windows/__init__.py", "src/deskpal/execution/windows/launcher.py",
        "src/deskpal/execution/windows/_win32.py", "scripts/diagnose_worker_startup.py",
        "scripts/diagnose_worker_limits.py", "tests/fixtures/windows/native_probe_v2.c",
    )
    return {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in files}


class ReportWriter:
    """Create exclusively once; every update uses the same open file object.

    Recheck ancestors and file identity before every update. Never reopen a
    pathname with truncation, which could overwrite a replaced older report.
    This does not claim to defend against a hostile concurrent host process.
    """

    def __init__(self, path: Path) -> None:
        self.path = _new_report_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _no_reparse(self.path)
        self.stream: TextIO = self.path.open("x+", encoding="ascii")

    def save(self, report: dict[str, object]) -> None:
        _no_reparse(self.path)
        if not os.path.samestat(os.fstat(self.stream.fileno()), self.path.stat(follow_symlinks=False)):
            raise ValueError("limits_report_identity_changed")
        payload = json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
        if len(payload.encode("ascii")) > 256 * 1024:
            raise ValueError("limits_report_budget")
        self.stream.seek(0)
        self.stream.write(payload)
        self.stream.truncate()
        self.stream.flush()

    def close(self) -> None:
        self.stream.close()


class ObserverApi:
    """Read-only process/Job observations and checked host-handle ownership."""

    def __init__(self) -> None:
        kernel = C.WinDLL("kernel32", use_last_error=True)
        self.current = _bind(kernel, "GetCurrentProcess", [], HANDLE)()
        self.duplicate = _bind(kernel, "DuplicateHandle",
            [HANDLE, HANDLE, HANDLE, C.POINTER(HANDLE), DWORD, BOOL, DWORD], BOOL)
        self.close_handle = _bind(kernel, "CloseHandle", [HANDLE], BOOL)
        self.query_job = _bind(kernel, "QueryInformationJobObject", [HANDLE, C.c_int, C.c_void_p, DWORD, C.c_void_p], BOOL)
        self.wait = _bind(kernel, "WaitForSingleObject", [HANDLE, DWORD], DWORD)
        self.exit_code = _bind(kernel, "GetExitCodeProcess", [HANDLE, C.POINTER(DWORD)], BOOL)
        self.count_handles = _bind(kernel, "GetProcessHandleCount", [HANDLE, C.POINTER(DWORD)], BOOL)

    def handle_count(self) -> dict[str, object]:
        count = DWORD()
        if not self.count_handles(self.current, C.byref(count)):
            return {"available": False, "winerror": C.get_last_error()}
        return {"available": True, "count": int(count.value)}


class ObservationHandles:
    def __init__(self, api: ObserverApi) -> None:
        self.api = api
        self.job: int | None = None
        self.process: int | None = None

    def attach(self, worker: RunningWorker) -> None:
        # Diagnostic access to this returned worker only; never caller handles.
        native = worker._native
        job = HANDLE()
        try:
            if not self.api.duplicate(self.api.current, native.job, self.api.current,
                                      C.byref(job), 0x4, False, 0):  # JOB_OBJECT_QUERY
                raise LaunchRejected("limits_duplicate_job", C.get_last_error())
        finally:
            self.job = int(job.value) if job.value else None
        if not self.job:
            raise LaunchRejected("limits_duplicate_job_null")
        process = HANDLE()
        try:
            if not self.api.duplicate(self.api.current, native.process, self.api.current,
                                      C.byref(process), 0x101000, False, 0):  # SYNC | QUERY_LIMITED
                raise LaunchRejected("limits_duplicate_process", C.get_last_error())
        finally:
            self.process = int(process.value) if process.value else None
        if not self.process:
            raise LaunchRejected("limits_duplicate_process_null")

    def snapshot(self) -> dict[str, object]:
        result: dict[str, object] = {"job_available": self.job is not None,
                                    "process_available": self.process is not None}
        if self.job is not None:
            accounting = JOB_ACCOUNTING()
            if self.api.query_job(self.job, 1, C.byref(accounting), C.sizeof(accounting), None):
                result["accounting"] = {
                    "available": True, "active_processes": int(accounting.ActiveProcesses),
                    "total_processes": int(accounting.TotalProcesses),
                    "limit_terminated_processes": int(accounting.TotalTerminatedProcesses),
                    "user_time_100ns": int(accounting.TotalUserTime),
                    "kernel_time_100ns": int(accounting.TotalKernelTime),
                }
            else:
                result["accounting"] = {"available": False, "winerror": C.get_last_error()}
            limits = JOB_EXTENDED_LIMIT()
            if self.api.query_job(self.job, 9, C.byref(limits), C.sizeof(limits), None):
                result["memory"] = {"available": True,
                    "peak_process_bytes": int(limits.PeakProcessMemoryUsed),
                    "peak_job_bytes": int(limits.PeakJobMemoryUsed),
                    "process_limit_bytes": int(limits.ProcessMemoryLimit),
                    "job_limit_bytes": int(limits.JobMemoryLimit)}
            else:
                result["memory"] = {"available": False, "winerror": C.get_last_error()}
        if self.process is not None:
            status = int(self.api.wait(self.process, 0))
            result["process_wait_status"] = status
            if status == 0xffffffff:
                result["process_wait_winerror"] = C.get_last_error()
            if status == 0:
                exit_code = DWORD()
                if self.api.exit_code(self.process, C.byref(exit_code)):
                    result["exit_code"] = int(exit_code.value)
                else:
                    result["exit_code_winerror"] = C.get_last_error()
        return result

    def close(self) -> None:
        failures: list[int] = []
        for attribute in ("process", "job"):
            handle = getattr(self, attribute)
            if handle is not None:
                if not self.api.close_handle(handle):
                    failures.append(C.get_last_error())
                else:
                    setattr(self, attribute, None)
        if failures:
            raise LaunchRejected("limits_close_observer", failures[0])


class CaseCleanup:
    """Retain worker, error cleanup_owner and observer handles as one owner."""

    def __init__(self, api: ObserverApi) -> None:
        self.owners: list[CleanupOwner] = []
        self.observers = ObservationHandles(api)
        self.after: dict[str, object] | None = None
        self.launch_resolved = False
        self.worker_returned = False
        self.observation_confirmed = False
        self.closed = False

    def add(self, owner: CleanupOwner | None) -> None:
        if owner is not None and all(item is not owner for item in self.owners):
            self.owners.append(owner)

    def close(self) -> None:
        if self.closed:
            return
        try:
            errors: list[BaseException] = []
            # Visit every known owner even if one fails; retain any replacement
            # cleanup_owner supplied by close(), including a distinct owner.
            for owner in tuple(self.owners):
                try:
                    owner.close()
                except BaseException as error:
                    if isinstance(error, LaunchRejected):
                        self.add(error.cleanup_owner)
                    errors.append(error)
            if errors:
                raise errors[0]
            if not self.launch_resolved:
                raise LaunchRejected("limits_launch_ownership_unknown")
            if not self.observation_confirmed:
                self.after = self.observers.snapshot()
                if self.worker_returned:
                    accounting = self.after.get("accounting")
                    if (not isinstance(accounting, dict) or accounting.get("available") is not True
                            or accounting.get("active_processes") != 0):
                        raise LaunchRejected("limits_job_exit_observation_unconfirmed")
                    if (self.after.get("process_available") is not True
                            or self.after.get("process_wait_status") != 0):
                        raise LaunchRejected("limits_process_exit_observation_unconfirmed")
                self.observation_confirmed = True
            self.observers.close()
            self.closed = True
        except BaseException as error:
            raise LaunchRejected("limits_cleanup_unconfirmed", cleanup_owner=self) from error


def _fixed_frame(result: WorkerResult) -> dict[str, object]:
    if len(result.frames) != 1:
        raise LaunchRejected("limits_expected_one_native_frame")
    frame = result.frames[0]
    keys = {"probe", "excluded_handle_valid", "excluded_handle_error", "excluded_handle_exception",
            "operation_error", "allocation_count"}
    if set(frame) != keys or frame["probe"] != "native" or type(frame["excluded_handle_valid"]) is not bool:
        raise LaunchRejected("limits_native_frame_schema")
    for name in ("excluded_handle_error", "excluded_handle_exception", "operation_error", "allocation_count"):
        value = frame[name]
        if type(value) is not int or not 0 <= value <= 0xffffffff:
            raise LaunchRejected("limits_native_frame_schema")
    return frame


def assess_mode(mode: str, result: WorkerResult | None, rejection: str | None,
                violation: str | None) -> dict[str, object]:
    """Behavior observations only; never converts these into quota acceptance."""
    if mode not in MODES:
        raise ValueError("limits_unknown_mode")
    if mode in ("spin", "flood", "invalid-frame"):
        expected = ((mode == "spin" and rejection == "deadline") or
                    (mode == "flood" and rejection == "log_budget") or
                    (mode == "invalid-frame" and violation == "frame_budget"))
        return {"expected_behavior_observed": expected,
                "cpu_hard_cap_trigger_verified": False} if mode == "spin" else {"expected_behavior_observed": expected}
    if result is None:
        return {"expected_behavior_observed": False}
    frame = _fixed_frame(result)
    invalid_handle_denied = frame["excluded_handle_valid"] is False and (
        (frame["excluded_handle_error"] == 6 and frame["excluded_handle_exception"] == 0) or
        (frame["excluded_handle_error"] == 0 and frame["excluded_handle_exception"] == 0xC0000008))
    normal = result.exit_code == 0 and not result.stdout and not result.stderr and invalid_handle_denied
    error, count = frame["operation_error"], frame["allocation_count"]
    observed = normal and error == 0 and count == 0
    evidence: dict[str, object] = {"native_frame": frame, "excluded_handle_denial_observed": invalid_handle_denied}
    if mode == "child":
        observed = normal and error != 0 and count == 0
        evidence.update(child_creation_refusal_observed=error != 0,
                        individual_child_policy_trigger_verified=False,
                        child_failure_attribution="combined_restrictions_or_other_failure_not_separated")
    evidence["expected_behavior_observed"] = observed
    return evidence


def _cleanup_case(cleanup: CaseCleanup, entry: dict[str, object]) -> bool:
    attempts: list[dict[str, object]] = []
    entry["cleanup_attempts"] = attempts
    for _ in range(2):
        try:
            cleanup.close()
        except LaunchRejected as error:
            failed: dict[str, object] = {"returned": False, "stage": error.stage, "winerror": error.winerror}
            cause = error.__cause__
            if isinstance(cause, LaunchRejected):
                failed.update(cause_stage=cause.stage, cause_winerror=cause.winerror)
            elif cause is not None:
                failed["cause_type"] = type(cause).__name__
            attempts.append(failed)
        else:
            attempts.append({"returned": True})
            entry["cleanup_confirmed"] = True
            entry["job_after_cleanup"] = cleanup.after
            return True
    _UNCONFIRMED_OWNERS.append(cleanup)
    entry.update(cleanup_confirmed=False, worker_reap="UNCONFIRMED_owner_retained",
                 job_after_cleanup=cleanup.after)
    return False


def run_mode(mode: str, spec: LaunchSpec, api: ObserverApi) -> tuple[dict[str, object], bool]:
    if mode not in MODES or spec.args != ("{ipc_write_handle}", "{excluded_handle}", mode):
        raise ValueError("limits_unknown_or_mismatched_mode")
    timeout = 1.0 if mode == "spin" else 5.0
    entry: dict[str, object] = {"mode": mode, "timeout_seconds": timeout,
                               "log_limit_bytes": LOG_LIMIT, "expected_behavior_observed": False}
    before = api.handle_count()
    entry["host_handle_count_before"] = before
    cleanup = CaseCleanup(api)
    result: WorkerResult | None = None
    rejection: str | None = None
    violation: str | None = None
    collect_started: float | None = None
    collection_elapsed: float | None = None
    worker_returned = False
    started = monotonic()
    try:
        worker = WindowsLauncher().launch(spec)
        worker_returned = True
        cleanup.add(worker)
        cleanup.worker_returned = True
        cleanup.launch_resolved = True
        entry["identity"] = asdict(worker.identity)
        cleanup.observers.attach(worker)
        entry["job_before_collect"] = cleanup.observers.snapshot()
        collect_started = monotonic()
        result = worker.collect(timeout, log_limit_bytes=LOG_LIMIT)
        entry.update(exit_code=result.exit_code, frame_count=len(result.frames),
                     stdout_bytes=len(result.stdout), stderr_bytes=len(result.stderr))
    except LaunchRejected as error:
        rejection = error.stage
        cleanup.add(error.cleanup_owner)
        # Public launcher contract: a LaunchRejected without cleanup_owner has
        # reaped its process; with one, that retained owner must confirm close.
        cleanup.launch_resolved = True
        entry.update(rejected=error.stage, winerror=error.winerror)
    except ProtocolViolation as error:
        violation = error.code
        entry["protocol_violation"] = error.code
    except Exception as error:
        entry["error_type"] = type(error).__name__
    finally:
        if collect_started is not None:
            collection_elapsed = monotonic() - collect_started
        cleanup_started = monotonic()
        safe = _cleanup_case(cleanup, entry)
        entry["cleanup_elapsed_seconds"] = monotonic() - cleanup_started
        if safe:
            entry["worker_reap"] = ("confirmed_by_adapter_close_with_observer_evidence"
                                     if worker_returned else "not_created_or_reaped_by_launcher_cleanup")
            after = api.handle_count()
            entry["host_handle_count_after"] = after
            first, last = before.get("count"), after.get("count")
            if type(first) is int and type(last) is int:
                entry["host_handle_count_delta"] = last - first
                entry["host_handle_count_unchanged"] = last == first
    entry.update(elapsed_seconds=monotonic() - started, collection_elapsed_seconds=collection_elapsed,
                 bounded_collection=collection_elapsed is not None and collection_elapsed <= timeout + 2.5,
                 handle_count_is_auxiliary_not_handle_table_proof=True)
    try:
        entry.update(assess_mode(mode, result, rejection, violation))
    except LaunchRejected as error:
        entry.update(expected_behavior_observed=False, assessment_rejected=error.stage)
    return entry, safe


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt" or C.sizeof(C.c_void_p) != 8:
        parser.error("Windows x64 only")
    report_path = _new_report_path(args.report)
    fixture = ROOT / "data/v06-stage0/native-fixture-v2/fixture.exe"
    _no_reparse(fixture)
    if sha256(fixture.read_bytes()).hexdigest() != FIXTURE_SHA:
        raise ValueError("limits_fixture_fingerprint_mismatch")
    windows, local, elevated = native_context()
    set_caller_localappdata(local)  # Dedicated helper process only.
    api = ObserverApi()
    version = sys.getwindowsversion()
    runs: list[dict[str, object]] = []
    report: dict[str, object] = {"stage0_passed": False, "resource_enforcement_accepted": False,
        "started_at": datetime.now(timezone.utc).isoformat(), "host_token_elevated": elevated,
        "python_version": sys.version, "python_executable": sys.executable,
        "windows_build": version.build, "fixture_version": 2, "fixture_sha256": FIXTURE_SHA,
        "limits_requested": asdict(JobLimits()), "source_sha256": source_fingerprints(),
        "runs": runs, "cleanup": "not_required", "bounded_behavior_matrix_observed": False,
        "memory_probe": {"execution": "unexecuted", "fixture_allocation_ceiling_bytes": 16 * 1024**3,
                         "reason": "v2_memory_requests_up_to_16GiB_no_safe_independent_fixture_ceiling",
                         "allocation_refusal_observed": None, "job_memory_limit_trigger_verified": False},
        "unresolved": ["CPU hard-cap saturation enforcement not established by spin timeout",
                       "V2 memory mode unexecuted; a separately pinned capped fixture is required",
                       "Child policy and Job active-process restriction not independently triggered",
                       "Kill-on-last-Job-handle-close is not exercised with host observer duplicates",
                       "Host handle count is auxiliary, not proof of every individual handle close"]}
    writer = ReportWriter(report_path)
    try:
        return collect_matrix(writer, report, runs, fixture, windows, api)
    finally:
        writer.close()


def collect_matrix(writer: ReportWriter, report: dict[str, object], runs: list[dict[str, object]],
                   fixture: Path, windows: str, api: ObserverApi) -> int:
    """Own a fresh profile immediately after creation, including save failures."""
    writer.save(report)
    safe_to_delete = True
    code = 0
    profile = create_probe_profile("deskpal.v06." + uuid4().hex)
    try:
        report.update(profile=asdict(profile), cleanup="pending")
        writer.save(report)
        executable = Path(profile.folder) / "native-fixture-v2.exe"
        _no_reparse(executable)
        # Never overwrite an object even inside this freshly created profile.
        with fixture.open("rb") as source, executable.open("xb") as destination:
            shutil.copyfileobj(source, destination)
        if sha256(executable.read_bytes()).hexdigest() != FIXTURE_SHA:
            raise ValueError("limits_copied_fixture_mismatch")
        temporary = Path(profile.folder) / "Temp"
        _no_reparse(temporary)
        temporary.mkdir(exist_ok=True)
        environment = {"SYSTEMROOT": windows, "WINDIR": windows, "TEMP": str(temporary),
                       "TMP": str(temporary), "LOCALAPPDATA": profile.folder}
        report["child_environment_keys"] = sorted(environment)
        for mode in MODES:
            spec = LaunchSpec(executable, ("{ipc_write_handle}", "{excluded_handle}", mode),
                              Path(profile.folder), profile.sid, environment)
            # Any nonreturning call leaves deletion blocked, including an
            # interrupt between worker creation and receipt of its cleanup fact.
            safe_to_delete = False
            entry, safe_to_delete = run_mode(mode, spec, api)
            runs.append(entry)
            writer.save(report)
            if not safe_to_delete:
                code = 2
                break
        observed = len(runs) == len(MODES) and all(
            item.get("expected_behavior_observed") is True and item.get("bounded_collection") is True
            and item.get("cleanup_confirmed") is True for item in runs)
        report["bounded_behavior_matrix_observed"] = observed
        report["status"] = "synthetic_limits_matrix_collected_requires_review"
        if not observed:
            code = 2
    except Exception as error:
        report.update(status="limits_diagnostic_failed", error_type=type(error).__name__)
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
            report["status"] = "source_changed_matrix_not_frozen"
            code = 2
        writer.save(report)
    print(json.dumps({"report": str(writer.path), "diagnostic_exit": code, "cleanup": report["cleanup"]}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
