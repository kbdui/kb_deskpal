"""Fixed V3 inner-Job 128MiB commit observation; never stage-zero acceptance.

The unchanged adapter creates/verifies the canonical outer Job before its private
resume hook adds an anonymous inner Job. No quota/executable/identity override.
Official contracts reviewed for this diagnostic:
https://learn.microsoft.com/en-us/windows/win32/procthread/nested-jobs
https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject
https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_associate_completion_port
https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-terminatejobobject
Inner events propagate upward: this private INNER port is never outer-256MiB
evidence. Hard-limit message delivery is not guaranteed. Missing remains unknown.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import ctypes as C
from ctypes import wintypes as W
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from time import monotonic
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deskpal.execution.protocol import FrameLimits  # noqa: E402
from deskpal.execution.runtime import create_probe_profile, delete_probe_profile, worker_environment  # noqa: E402
from deskpal.execution.windows.launcher import (  # noqa: E402
    CleanupOwner, JobLimits, LaunchRejected, LaunchSpec, RunningWorker,
)
from deskpal.execution.windows._win32 import (  # noqa: E402
    BOOL, DWORD, HANDLE, JOB_ACCOUNTING, JOB_CPU, JOB_EXTENDED_LIMIT, NativeWorker,
    _bind, _CREATE_LOCK, _create_worker,
)
from diagnose_worker_memory import (  # noqa: E402
    CEILING as CEILING, COMPLETION_ASSOCIATION, FIXTURE_SHA, MemoryApi, MemoryLaunchApi as MemoryLaunchApi,
    MemoryObservation, MODES, SINGLE_PROCESS_LIST, fixed_frame, verify_fixture,
)
from diagnose_worker_limits import ReportWriter, _no_reparse  # noqa: E402

FIXTURE = ROOT / "data/v06-stage0/native-memory-fixture-v3/fixture.exe"
INNER_BYTES = 128 * 1024 * 1024
OUTER_BYTES = 256 * 1024 * 1024
INNER_FLAGS = 0x2208  # kill-close | job memory | active count; NO process memory/breakaway.
FRAME_LIMITS = FrameLimits(max_frame_bytes=4096, max_stream_bytes=4100, max_depth=4,
    max_string_bytes=128, max_container_items=64, max_nodes=128, max_frames=1)
_UNCONFIRMED: list[NestedOwner] = []


class NestedApi(MemoryApi):
    def __init__(self) -> None:
        super().__init__()
        kernel = C.WinDLL("kernel32", use_last_error=True)
        self.create_job = _bind(kernel, "CreateJobObjectW", [HANDLE, W.LPCWSTR], HANDLE)
        self.assign_job = _bind(kernel, "AssignProcessToJobObject", [HANDLE, HANDLE], BOOL)
        self.windows_directory = _bind(kernel, "GetWindowsDirectoryW", [W.LPWSTR, W.UINT], W.UINT)


class NestedOwner(MemoryObservation):
    """One owner for partial setup, adapter cleanup and both Job observations."""

    def __init__(self, api: NestedApi, entry: dict[str, object], persist: Callable[[], None]) -> None:
        super().__init__(api)
        self.nested_api = api
        self.inner: int | None = None
        self.adapter: RunningWorker | NativeWorker | None = None
        self.pending: list[CleanupOwner] = []
        self.launch_resolved = False
        self.failed_launch_reaped = False
        self.setup_started = False
        self.outer_terminate_completed = False
        self.reap_verified = False
        self.close_started = False
        self.closed = False
        self.failed = False
        self.persist = persist
        self.events: list[dict[str, object]] = []
        self.close_attempted: set[str] = set()
        self.closed_resources: list[dict[str, object]] = []
        self.audit: dict[str, object] = {"events": self.events, "messages": self.messages,
            "direct_handle_closes": self.closed_resources, "binding_verified": False,
            "reap_verified": False, "delivery_guaranteed": False, "query_job_7_used": False,
            "inner_requested": {"flags": INNER_FLAGS, "active": 1, "process_memory": 0, "job_memory": INNER_BYTES},
            "outer_requested": asdict(JobLimits()), "individual_adapter_handle_closes_verified": False,
            "popen_own_process_handle_verified": False, "kill_on_last_handle_close_tested": False}
        entry["observation"] = self.audit

    def step(self, stage: str, **facts: object) -> None:
        self.audit["state"] = stage
        if len(self.events) < 128:
            self.events.append({"stage": stage, **facts})
        else:
            self.failed = True
            self.audit["event_budget_exceeded"] = True
            if not self.close_started:
                raise LaunchRejected("nested_event_budget")
        try:
            self.persist()
        except Exception as error:
            self.failed = True
            self.audit.update(unknown=True, persistence_error_type=type(error).__name__, persistence_error=str(error)[:256])
            if not self.close_started:
                raise

    def call(self, stage: str, method: Any, *args: Any) -> int:
        self.step(stage + "_prepared", prepared_does_not_prove_call_started=True)
        result = int(method(*args))
        error = 0 if result else C.get_last_error()
        self.step(stage + "_returned", result=result, winerror=error)
        if not result:
            raise LaunchRejected(stage, error)
        return result

    def noninheritable(self, handle: int, label: str) -> None:
        self.call(label + "_clear_inherit", self.nested_api.set_handle_info, handle, 1, 0)
        flags = DWORD()
        self.call(label + "_query_inherit", self.nested_api.get_handle_info, handle, C.byref(flags))
        if flags.value & 1:
            raise LaunchRejected("nested_inheritable_" + label)

    def limits(self, job: int, label: str, inner: bool) -> None:
        limits = JOB_EXTENDED_LIMIT()
        self.call(label + "_class9", self.nested_api.query_job, job, 9, C.byref(limits), C.sizeof(limits), None)
        basic = limits.BasicLimitInformation
        actual = {"flags": int(basic.LimitFlags), "active": int(basic.ActiveProcessLimit),
            "process_memory": int(limits.ProcessMemoryLimit), "job_memory": int(limits.JobMemoryLimit),
            "peak_process_bytes": int(limits.PeakProcessMemoryUsed), "peak_job_bytes": int(limits.PeakJobMemoryUsed)}
        self.audit[label + "_limits"] = actual
        expected = (INNER_FLAGS, 1, 0, INNER_BYTES) if inner else (0x2308, 1, OUTER_BYTES, OUTER_BYTES)
        if tuple(actual[key] for key in ("flags", "active", "process_memory", "job_memory")) != expected:
            raise LaunchRejected("nested_limits_mismatch_" + label)

    def membership(self, process: int, job: int, label: str) -> None:
        member, ui = BOOL(), DWORD()
        self.call(label + "_membership", self.nested_api.in_job, process, job, C.byref(member))
        pids = SINGLE_PROCESS_LIST()
        self.call(label + "_class3", self.nested_api.query_job, job, 3, C.byref(pids), C.sizeof(pids), None)
        self.call(label + "_ui", self.nested_api.query_job, job, 4, C.byref(ui), C.sizeof(ui), None)
        self.audit[label + "_membership"] = {"member": int(member.value), "assigned": int(pids.NumberOfAssignedProcesses),
            "listed": int(pids.NumberOfProcessIdsInList), "pid": int(pids.ProcessIdList[0]), "ui_flags": int(ui.value)}
        if (member.value != 1 or pids.NumberOfAssignedProcesses != 1 or pids.NumberOfProcessIdsInList != 1
                or pids.ProcessIdList[0] != self.pid or ui.value != 0):
            raise LaunchRejected("nested_membership_pid_or_ui_" + label)

    def _packet(self, wait_ms: int) -> dict[str, int] | None:
        packet = super()._packet(wait_ms)
        if packet is not None and packet["message"] == 6 and sum(item["message"] == 6 for item in self.messages) != 1:
            raise LaunchRejected("nested_duplicate_new_process_packet")
        return packet

    def bind_suspended(self, job: int, process: int) -> None:
        if self.setup_started:
            raise LaunchRejected("nested_setup_reused")
        self.setup_started = True
        try:
            # The unchanged _create_worker invokes this via MemoryLaunchApi only
            # after PI/Job registration, outer assignment and its full _verify.
            for source, attribute, rights in ((job, "job", 4), (process, "process", 0x101000)):
                self.step("duplicate_" + attribute + "_prepared")
                self._duplicate(source, attribute, rights)  # Its finally registers partial output.
                self.noninheritable(int(getattr(self, attribute)), attribute)
            self.pid = int(self.nested_api.process_id(process))
            if not self.pid:
                raise LaunchRejected("nested_process_id", C.get_last_error())
            self.audit.update(pid=self.pid, completion_key=self.key)
            self.step("create_inner_prepared")
            self.inner = int(self.nested_api.create_job(None, None) or 0) or None
            if self.inner is None:
                raise LaunchRejected("nested_create_inner", C.get_last_error())
            self.noninheritable(self.inner, "inner")
            accounting = JOB_ACCOUNTING()
            self.call("inner_empty", self.nested_api.query_job, self.inner, 1, C.byref(accounting), C.sizeof(accounting), None)
            self.audit["inner_preassign_active"] = int(accounting.ActiveProcesses)
            if accounting.ActiveProcesses != 0:
                raise LaunchRejected("nested_inner_not_empty")
            limits = JOB_EXTENDED_LIMIT()
            limits.BasicLimitInformation.LimitFlags = INNER_FLAGS
            limits.BasicLimitInformation.ActiveProcessLimit = 1
            limits.JobMemoryLimit = INNER_BYTES
            self.call("inner_set_class9", self.nested_api.set_job, self.inner, 9, C.byref(limits), C.sizeof(limits))
            self.limits(self.inner, "inner_preassign", True)
            self.step("create_port_prepared")
            self.port = int(self.nested_api.create_port(HANDLE(-1), None, 0, 1) or 0) or None
            if self.port is None:
                raise LaunchRejected("nested_create_port", C.get_last_error())
            self.noninheritable(self.port, "port")
            association = COMPLETION_ASSOCIATION(self.key, self.port)
            self.call("inner_associate_class7", self.nested_api.set_job, self.inner, 7, C.byref(association), C.sizeof(association))
            self.audit["inner_assignment_attempted"] = True
            self.call("inner_assign", self.nested_api.assign_job, self.inner, process)
            self.audit["inner_assignment_returned_success"] = True
            deadline, found = monotonic() + 0.5, False
            for _ in range(32):
                if monotonic() >= deadline:
                    break
                packet = self._packet(10)
                if packet is not None:
                    if packet["message"] != 6:
                        raise LaunchRejected("nested_pre_resume_packet")
                    found = True
                    break
            if not found:
                raise LaunchRejected("nested_new_process_notification_unknown")
            for handle, label, is_inner in ((job, "outer", False), (self.inner, "inner", True)):
                self.membership(process, handle, label)
                self.limits(handle, label, is_inner)
            cpu = JOB_CPU()
            self.call("outer_cpu_class15", self.nested_api.query_job, job, 15, C.byref(cpu), C.sizeof(cpu), None)
            self.audit["outer_cpu"] = {"flags": int(cpu.ControlFlags), "rate": int(cpu.CpuRate)}
            if cpu.ControlFlags != 5 or cpu.CpuRate != 2000:
                raise LaunchRejected("nested_outer_cpu_changed")
            self.binding_verified = True
            self.audit["binding_verified"] = True
            self.step("nested_binding_verified_before_resume")
        except BaseException as error:
            self.failed = True
            self.audit.update(setup_error_type=type(error).__name__, setup_error=str(error)[:512])
            raise

    def observe_reap(self) -> None:
        results: dict[str, object] = {}
        self.audit["reap_observations"] = results
        for handle, label in ((self.job, "outer"), (self.inner, "inner")):
            try:
                if handle is None:
                    raise LaunchRejected("nested_missing_reap_handle_" + label)
                accounting = JOB_ACCOUNTING()
                self.call(label + "_reap_class1", self.nested_api.query_job,
                          handle, 1, C.byref(accounting), C.sizeof(accounting), None)
                results[label + "_active"] = int(accounting.ActiveProcesses)
            except Exception as error:
                results[label + "_error"] = str(error)[:256]
        if self.process is not None:
            try:
                wait_status = int(self.nested_api.wait(self.process, 0))
                results.update(process_wait_status=wait_status,
                               process_wait_winerror=C.get_last_error() if wait_status == 0xFFFFFFFF else 0)
            except Exception as error:
                results["process_wait_error"] = str(error)[:256]
        if results.get("outer_active") != 0 or results.get("inner_active") != 0 or results.get("process_wait_status") != 0:
            raise LaunchRejected("nested_independent_reap_unknown")
        self.reap_verified = True
        self.audit["reap_verified"] = True
        self.step("both_jobs_empty_and_process_signaled")

    def close(self) -> None:
        if self.close_started:
            if self.closed and not self.failed:
                return
            raise LaunchRejected("nested_prior_cleanup_unknown_no_retry", cleanup_owner=self)
        self.close_started = True
        try:
            if self.pending or not self.launch_resolved:
                raise LaunchRejected("nested_launch_owner_unknown")
            if self.adapter is not None:
                self.step("outer_terminate_prepared")
                self.adapter.terminate()
                self.outer_terminate_completed = True
                self.step("outer_terminate_contract_completed")
            elif not self.failed_launch_reaped:
                raise LaunchRejected("nested_outer_reap_contract_unknown")
            # No observer is discarded until all three independent conditions hold.
            if self.setup_started:
                self.observe_reap()
            if self.adapter is not None:
                self.step("adapter_close_prepared")
                self.adapter.close()
                self.step("adapter_close_contract_returned")
            if self.port is not None:
                try:
                    deadline = monotonic() + 0.3
                    for _ in range(128):
                        if monotonic() >= deadline:
                            raise LaunchRejected("nested_drain_deadline")
                        if self._packet(10) is None:
                            self.drained = True
                            break
                    if not self.drained:
                        raise LaunchRejected("nested_drain_budget")
                except Exception as error:
                    self.failed = True
                    self.drain_error = str(error)[:256]
                finally:
                    self.drain_finished = True
                    self.step("inner_port_drain_finished", drained=self.drained, error=self.drain_error)
            for attribute in ("process", "job", "inner", "port"):
                handle = getattr(self, attribute)
                if handle is None:
                    continue
                self.close_attempted.add(attribute)
                record: dict[str, object] = {"resource": attribute, "returned": False}
                self.closed_resources.append(record)
                self.step("direct_close_prepared", resource=attribute, prepared_does_not_prove_call_started=True)
                try:
                    result = int(self.nested_api.close_handle(handle))
                    record.update(returned=True, raw_status=result, winerror=0 if result else C.get_last_error())
                    if not result:
                        self.failed = True
                    else:
                        setattr(self, attribute, None)
                except Exception as error:
                    self.failed = True
                    record.update(error_type=type(error).__name__, error=str(error)[:256])
                self.step("direct_close_recorded", resource=attribute)
            self.closed = all(getattr(self, name) is None for name in ("process", "job", "inner", "port"))
            if self.failed or not self.closed:
                raise LaunchRejected("nested_evidence_or_close_unknown")
        except BaseException as error:
            self.failed = True
            pending = getattr(error, "cleanup_owner", None)
            if pending is not None and pending is not self and all(owner is not pending for owner in self.pending):
                self.pending.append(pending)
            self.audit.update(cleanup_error_type=type(error).__name__, cleanup_error=str(error)[:512])
            raise LaunchRejected("nested_cleanup_unknown", cleanup_owner=self) from error
        finally:
            self.audit.update(outer_terminate_contract_completed=self.outer_terminate_completed,
                direct_resources_closed=self.closed, bounded_drain_empty_observed=self.drained,
                notification_error=self.drain_error, unknown=self.failed,
                inner_job_memory_limit_event="observed" if self.binding_verified and self.drained
                    and self.drain_error is None and any(item["message"] == 10 for item in self.messages) else "unknown")
            self.step("cleanup_finished")
        if self.failed:
            raise LaunchRejected("nested_cleanup_persistence_unknown", cleanup_owner=self)


def _launch(spec: LaunchSpec, owner: NestedOwner) -> RunningWorker:
    try:
        with _CREATE_LOCK:
            native, identity = _create_worker(spec, _api=MemoryLaunchApi(owner))
        owner.adapter = native  # Register before constructing the wrapper or saving.
        worker = RunningWorker(native, identity)
        owner.adapter = worker
        owner.launch_resolved = True
        return worker
    except BaseException as error:
        owner.launch_resolved = True
        pending = getattr(error, "cleanup_owner", None)
        if pending is not None:
            owner.pending.append(pending)
        else:
            owner.failed_launch_reaped = True  # Original _create_worker cleanup contract only.
        raise


def run_case(spec: LaunchSpec, api: NestedApi, entry: dict[str, object], persist: Callable[[], None]) -> bool:
    mode = spec.args[-1] if spec.args else ""
    if mode not in MODES or spec.args != ("{ipc_write_handle}", "{excluded_handle}", mode) or spec.limits != JobLimits() or spec.low_privilege:
        raise ValueError("nested_fixed_spec_required")
    owner = NestedOwner(api, entry, persist)
    gate = False
    entry.update(mode=mode, stage0_passed=False, resource_enforcement_accepted=False)
    try:
        worker = _launch(spec, owner)
        result = worker.collect(5.0, frame_limits=FRAME_LIMITS, log_limit_bytes=4096)
        data = asdict(result)
        data["stdout"], data["stderr"] = result.stdout.decode("utf-8", "replace"), result.stderr.decode("utf-8", "replace")
        entry["collected"] = data
        persist()  # Collected facts survive terminate/close failures.
        frame = fixed_frame(result, mode)
        excluded = frame["excluded_handle_valid"] is False and (
            (frame["excluded_handle_error"] == 6 and frame["excluded_handle_exception"] == 0)
            or (frame["excluded_handle_error"] == 0 and frame["excluded_handle_exception"] == 0xC0000008))
        if result.exit_code != 0 or result.stdout or result.stderr or not excluded:
            raise LaunchRejected("nested_fixture_result_gate")
        if mode == "bounded-memory" and (frame["allocation_error"] == 0 or frame["allocation_ceiling_reached"] is not False):
            raise LaunchRejected("nested_allocation_refusal_unknown")
        gate = True
    except BaseException as error:
        owner.failed = True
        entry.update(error_type=type(error).__name__, error=str(error)[:512])
        if not isinstance(error, Exception):
            raise
    finally:
        try:
            owner.close()
        except Exception:
            if all(item is not owner for item in _UNCONFIRMED):
                _UNCONFIRMED.append(owner)
    event = owner.audit.get("inner_job_memory_limit_event")
    observed = gate and owner.binding_verified and owner.closed and not owner.failed and (
        event == "observed" if mode == "bounded-memory" else event == "unknown")
    if not observed and all(item is not owner for item in _UNCONFIRMED):
        _UNCONFIRMED.append(owner)
    entry.update(expected_behavior_observed=observed, cleanup_confirmed=owner.closed and not owner.failed,
                 case_evidence_unknown=not observed,
                 inner_128m_job_event_observed=event == "observed", outer_256m_job_enforcement_accepted=False)
    persist()
    return observed


def fingerprints() -> dict[str, str]:
    names = ("scripts/diagnose_worker_job_memory.py", "scripts/diagnose_worker_memory.py",
        "scripts/diagnose_worker_limits.py", "scripts/diagnose_worker_startup.py",
        "src/deskpal/execution/windows/_win32.py", "src/deskpal/execution/windows/launcher.py",
        "src/deskpal/execution/runtime.py", "src/deskpal/execution/protocol.py",
        "src/deskpal/execution/__init__.py", "src/deskpal/execution/windows/__init__.py", "src/deskpal/__init__.py",
        "tests/fixtures/windows/native_memory_probe_v3.c", "data/v06-stage0/native-memory-fixture-v3/fixture.exe")
    return {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in names}


def experiment(writer: ReportWriter, report: dict[str, object]) -> int:
    if os.name != "nt" or C.sizeof(C.c_void_p) != 8:
        raise ValueError("nested_windows_x64_only")
    verify_fixture(FIXTURE)
    api = NestedApi()
    profile = create_probe_profile("deskpal.v06." + uuid4().hex)
    runs: list[dict[str, object]] = []
    report.update(profile=asdict(profile), runs=runs, cleanup="pending")
    complete = False
    code = 2
    executable: Path | None = None
    try:
        writer.save(report)
        executable = Path(profile.folder) / "native-memory-fixture-v3.exe"
        temporary = Path(profile.folder) / "Temp"
        _no_reparse(executable)
        with FIXTURE.open("rb") as source, executable.open("xb") as target:
            target.write(source.read())
        verify_fixture(executable)
        report["copied_fixture_sha256_start"] = sha256(executable.read_bytes()).hexdigest()
        writer.save(report)
        _no_reparse(temporary)
        temporary.mkdir(exist_ok=True)
        _no_reparse(temporary)
        directory = C.create_unicode_buffer(32768)
        length = int(api.windows_directory(directory, len(directory)))
        if not 0 < length < len(directory):
            raise ValueError("nested_windows_directory_unknown")
        environment = worker_environment(Path(directory.value), temporary, local_appdata=Path(profile.folder))
        for mode in MODES:
            verify_fixture(executable)
            entry: dict[str, object] = {}
            runs.append(entry)
            spec = LaunchSpec(executable, ("{ipc_write_handle}", "{excluded_handle}", mode), Path(profile.folder), profile.sid, environment)
            if not run_case(spec, api, entry, lambda: writer.save(report)):
                break
        complete = len(runs) == 2 and all(entry.get("expected_behavior_observed") is True for entry in runs)
        report["status"] = "inner_128m_job_observation_requires_review" if complete else "nested_job_observation_unknown"
    except BaseException as error:
        report.update(status="nested_job_observation_unknown", error_type=type(error).__name__, error=str(error)[:512])
        if not isinstance(error, Exception):
            raise
    finally:
        stable = False
        try:
            ending = fingerprints()
            report.update(source_sha256_end=ending, source_unchanged=ending == report.get("source_sha256"))
            if executable is not None:
                _no_reparse(executable)
            report["copied_fixture_sha256_end"] = sha256(executable.read_bytes()).hexdigest() if executable is not None and executable.exists() else None
            stable = report["source_unchanged"] is True and report["copied_fixture_sha256_end"] == FIXTURE_SHA
        except Exception as error:
            report.update(source_unchanged=False, source_end_error_type=type(error).__name__)
        if complete and stable and not _UNCONFIRMED:
            try:
                delete_probe_profile(profile)
                report["cleanup"] = "profile_deleted"
                code = 0
            except Exception as error:
                report.update(cleanup="failed_profile_may_remain", cleanup_error_type=type(error).__name__)
        else:
            report["cleanup"] = "withheld_failed_unknown_or_unstable_case"
        writer.save(report)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--experiment", action="store_true")
    args = parser.parse_args()
    report: dict[str, object] = {"stage0_passed": False, "resource_enforcement_accepted": False,
        "fixture_version": 3, "fixture_sha256": FIXTURE_SHA, "fixture_allocation_ceiling_bytes": CEILING,
        "cleanup": "not_required", "outer_256m_job_enforcement_accepted": False,
        "individual_adapter_handle_closes_verified": False, "popen_own_process_handle_verified": False,
        "scope": "One fresh ordinary AC, canonical outer limits, private inner Job128MiB and fixed V3 only."}
    writer = ReportWriter(args.report)
    try:
        if not args.experiment:
            report["status"] = "not_executed_explicit_opt_in_required"
            writer.save(report)
            return 0
        code = 2
        try:
            report["source_sha256"] = fingerprints()
            code = experiment(writer, report)
        except Exception as error:
            report.update(status="nested_job_observation_unknown", error_type=type(error).__name__, error=str(error)[:512])
        if "source_sha256_end" not in report:
            try:
                ending = fingerprints()
                report.update(source_sha256_end=ending, source_unchanged=ending == report.get("source_sha256"))
            except Exception as error:
                report.update(source_unchanged=False, source_end_error_type=type(error).__name__)
        if report.get("source_unchanged") is not True:
            code = 2
        writer.save(report)
    finally:
        writer.close()
    print(json.dumps({"report": str(args.report), "diagnostic_exit": code, "stage0_passed": False}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
