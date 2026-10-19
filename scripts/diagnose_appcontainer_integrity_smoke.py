"""Opt-in fixed native-v2 smoke after lowering one new AC worker to Untrusted.

This observes startup and the fixture body only. It does not probe object writes,
token regain, registry access, or namespace isolation. No mode/EXE is selectable.
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
import sys
from typing import Any, TextIO
from uuid import uuid4

import diagnose_appcontainer_integrity as mic
import diagnose_worker_limits as limits

from deskpal.execution.protocol import FrameLimits, ProtocolViolation
from deskpal.execution.windows.launcher import LaunchRejected, LaunchSpec, RunningWorker
from deskpal.execution.runtime import (
    create_probe_profile as create_probe_profile,
    delete_probe_profile as delete_probe_profile,
    worker_environment as worker_environment,
)

__all__ = ["LaunchRejected", "SmokeWorker", "mic"]

ROOT, token, core = mic.ROOT, mic.token, mic.core
Session, NativeApi = mic.Session, mic.NativeApi
observe_integrity, check_job, thread_absent = mic._observe, mic._job_check, token._thread_absent
create_worker = core._create_worker
new_report = token._new_report
FIXTURE, FIXTURE_SHA256 = mic.suspended.FIXTURE, "08423adb75615e3609e268409be844053bf2e14af1d4d250917006c0ba7ab4e1"
FIXTURE_SOURCE = ROOT / "tests/fixtures/windows/native_probe_v2.c"
FIXTURE_SOURCE_SHA256 = "226c38d7e9798735ac144b1ef0a209a3b92d8805e06e075b49646fb7aecdb5cb"
ARGS = ("{ipc_write_handle}", "{excluded_handle}", "smoke")
FRAME_LIMITS = FrameLimits(max_frame_bytes=4096, max_stream_bytes=4100, max_depth=8,
    max_string_bytes=4096, max_container_items=32, max_nodes=128, max_frames=1)
_UNCONFIRMED_OWNERS: list[Any] = []


class SmokeApi(mic.IntegrityApi):
    def __init__(self, session: mic.Session, callback: Any) -> None:
        super().__init__(session, callback)
        kernel = C.WinDLL("kernel32", use_last_error=True)
        self.smoke_resume = mic.suspended._bind(kernel, "ResumeThread", [core.HANDLE], core.DWORD)
        self.resume_calls = 0

    def _read_before_resume(self, handle: object) -> int:
        if not self.process_handle or self.resume_intercepted or self.resume_calls:
            raise LaunchRejected("integrity_smoke_resume_state")
        self.resume_intercepted = True
        if self.session.report.get("unknown_reasons"):
            raise LaunchRejected("integrity_smoke_baseline_unknown")
        self.callback(self.process_handle)
        report = self.session.report
        if (report.get("lowering_and_identity_observed") is not True
                or not report.get("worker_gate_checks") or not all(report["worker_gate_checks"].values())
                or report.get("unknown_reasons") or report.get("evidence_budget_exceeded")):
            raise LaunchRejected("integrity_smoke_lowering_gate")
        event: dict[str, Any] = {"api": "ResumeThread", "state": "prepared", "attempted": False,
            "raw_return": None, "last_error": None, "call_budget": 1,
            "prepared_record_does_not_establish_call_started": True}
        report["resume_call"] = event
        self.session.save()
        if report.get("unknown_reasons") or report.get("evidence_budget_exceeded"):
            raise LaunchRejected("integrity_smoke_resume_evidence_incomplete")
        self.resume_calls = 1
        event.update(state="call_entered", attempted=True)
        report.update(native_resume_called=True, worker_code_executed=None)
        try:
            C.set_last_error(0)
            previous = int(self.smoke_resume(handle))
            last_error = C.get_last_error()
            event.update(state="returned", raw_return=previous, last_error=last_error)
        except BaseException as error:
            event.update(state="wrapper_exception", error_type=type(error).__name__, error=str(error)[:512])
            raise
        finally:
            self.session.save()
        if previous != 1:
            raise LaunchRejected("integrity_smoke_resume_count", last_error)
        report["resume_count_verified"] = True
        return previous


class SmokeWorker(RunningWorker):
    """Record first cleanup attempts, including collect()'s implicit close."""

    def __init__(self, native: Any, identity: Any, report: dict[str, Any]) -> None:
        super().__init__(native, identity)
        self.report = report
        self.close_attempted = self.close_returned = self.cleanup_failed = False
        self.terminate_attempted = False

    def terminate(self) -> None:
        if self.cleanup_failed or self.terminate_attempted:
            raise LaunchRejected("integrity_smoke_terminate_retry_withheld")
        self.terminate_attempted = True
        self.report["terminate"] = {"state": "call_entered"}
        try:
            super().terminate()
            self.report["terminate"]["state"] = "returned"
        except BaseException:
            self.cleanup_failed = True
            self.report["terminate"]["state"] = "failed_owner_retained_no_retry"
            raise

    def close(self) -> None:
        if self.cleanup_failed or self.close_attempted:
            raise LaunchRejected("integrity_smoke_close_retry_withheld")
        self.close_attempted = True
        self.report["worker_close"] = {"state": "call_entered"}
        try:
            super().close()
            self.close_returned = True
            self.report["worker_close"]["state"] = "returned"
        except BaseException:
            self.cleanup_failed = True
            self.report["worker_close"]["state"] = "failed_owner_retained_no_retry"
            raise


def _fingerprints() -> dict[str, str]:
    paths = ("scripts/diagnose_appcontainer_integrity_smoke.py",
             "tests/unit/test_appcontainer_integrity_smoke_diagnostic.py")
    return mic._fingerprints() | limits.source_fingerprints() | {
        path: sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}


def _source_after(session: mic.Session) -> None:
    report = session.report
    try:
        source = next((owner for owner in session.owners if owner.label == "source"), None)
        if source is None or not source.valid() or "source_before" not in report:
            raise ValueError("integrity_smoke_source_before_missing")
        report["source_token_unchanged"] = report["source_before"] == session.host_snapshot(source, "source_after")
        if not report["source_token_unchanged"]:
            raise ValueError("integrity_smoke_source_changed")
    except BaseException as error:
        session.unknown("source_after:" + type(error).__name__ + ":" + str(error))
    try:
        thread_absent(session, "thread_after")
    except BaseException as error:
        session.unknown("thread_after:" + type(error).__name__ + ":" + str(error))
    session.close()


def _experiment(output: TextIO, report: dict[str, Any]) -> int:
    session: mic.Session | None = None
    adapter: SmokeApi | None = None
    worker: SmokeWorker | None = None
    native: Any = None
    profile: Any = None
    retained: list[Any] = []
    destination: Path | None = None
    try:
        if os.name != "nt":
            raise ValueError("integrity_smoke_windows_x64_only")
        token._layout()
        session = Session(NativeApi(), output, report)
        report.update(adapter_close_events=[], post_lower_job_calls=[])
        for path, expected in ((FIXTURE, FIXTURE_SHA256), (FIXTURE_SOURCE, FIXTURE_SOURCE_SHA256)):
            token._reject_reparse(path)
            if sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("integrity_smoke_fixed_fixture_pin")
        profile = create_probe_profile("deskpal.v06." + uuid4().hex)
        report.update(profile=asdict(profile), cleanup="pending")
        session.save()
        folder = Path(profile.folder)
        token._reject_reparse(folder)
        temporary, destination = folder / "Temp", folder / "native-fixture.exe"
        token._reject_reparse(temporary)
        temporary.mkdir(exist_ok=True)
        token._reject_reparse(temporary)
        token._reject_reparse(destination)
        with FIXTURE.open("rb") as source, destination.open("xb") as copied:
            copied.write(source.read())
        if sha256(destination.read_bytes()).hexdigest() != FIXTURE_SHA256:
            raise ValueError("integrity_smoke_copied_fixture_pin")
        windows = C.create_unicode_buffer(32768)
        length = session.call("GetWindowsDirectoryW", windows, len(windows))["raw_return"]
        if not 0 < length < len(windows):
            raise ValueError("integrity_smoke_windows_directory")
        environment = worker_environment(Path(windows.value), temporary, local_appdata=folder)
        report["requested_child_environment"] = environment
        spec = LaunchSpec(destination, ARGS, folder, profile.sid, environment)

        def observe(process: int) -> None:
            assert session is not None and adapter is not None
            report["original_adapter_verification_reached_resume_hook"] = True
            observe_integrity(session, process, profile.sid)
            check_job(adapter, spec, process, report)
            report["lowering_and_identity_observed"] = True

        adapter = SmokeApi(session, observe)
        try:
            with core._CREATE_LOCK:
                native, identity = create_worker(spec, _api=adapter)
        except LaunchRejected as error:
            report.update(launch_stage=error.stage, launch_winerror=error.winerror,
                          launch_cleanup_owner_present=error.cleanup_owner is not None)
            if error.cleanup_owner is not None:
                retained.append(error.cleanup_owner)
                report["launch_owner_disposition"] = "retained_no_automatic_retry"
            raise
        worker = SmokeWorker(native, identity, report)
        if adapter.failed_closes or report.get("unknown_reasons"):
            raise ValueError("integrity_smoke_post_launch_unknown")
        report["collect"] = {"state": "call_entered", "timeout_seconds": 5.0,
            "frame_limits": asdict(FRAME_LIMITS), "log_limit_bytes": 4096}
        session.save()
        result = worker.collect(5.0, frame_limits=FRAME_LIMITS, log_limit_bytes=4096)
        report["raw_result"] = {"identity": asdict(result.identity), "exit_code": result.exit_code,
            "frames": list(result.frames), "stdout_hex": result.stdout.hex(), "stderr_hex": result.stderr.hex(),
            "elapsed_seconds": result.elapsed_seconds}
        report["collect"]["state"] = "returned_raw_result_saved_before_termination"
        session.save()
        assessment = limits.assess_mode("smoke", result, None, None)
        report["smoke_assessment"] = assessment
        report["fixture_body_confirmed"] = assessment.get("expected_behavior_observed") is True
        if not report["fixture_body_confirmed"]:
            raise ValueError("integrity_smoke_body_unconfirmed")
        report["worker_code_executed"] = True
        session.save()
        worker.terminate()
        worker.close()
        report["worker_cleanup_confirmed"] = True
    except BaseException as error:
        report.update(error_type=type(error).__name__, error=str(error)[:1024])
        if isinstance(error, ProtocolViolation):
            report["protocol_violation"] = error.code
        context = error.__context__
        if isinstance(context, ProtocolViolation):
            report["protocol_violation"] = context.code
        if isinstance(error, LaunchRejected):
            report.update(rejected_stage=error.stage, rejected_winerror=error.winerror)
        if session is not None:
            session.unknown("smoke:" + type(error).__name__ + ":" + str(error))
    finally:
        # collect() may already have closed, or failed its close. Never retry it.
        if worker is not None:
            if not worker.close_attempted and not worker.cleanup_failed:
                try:
                    worker.close()
                except BaseException as error:
                    report["necessary_cleanup_error"] = type(error).__name__
            if worker.cleanup_failed:
                retained.append(worker)
                report["worker_owner_disposition"] = "retained_no_automatic_retry"
        elif native is not None:
            report["unexpected_native_cleanup"] = {"state": "one_necessary_close_entered"}
            try:
                native.close()
                report["unexpected_native_cleanup"]["state"] = "returned"
            except BaseException as error:
                retained.append(native)
                report["unexpected_native_cleanup"].update(state="failed_owner_retained_no_retry", error_type=type(error).__name__)
        if session is not None:
            _source_after(session)
        if adapter is not None:
            report.update(creation_flags=adapter.creation_flags, resume_intercepted=adapter.resume_intercepted,
                          actual_resume_calls=adapter.resume_calls)
            retained.extend(adapter.withheld_tokens)
            if adapter.failed_closes and native is not None:
                retained.append(native)
        if session is not None:
            retained.extend(owner for owner in session.owners if owner.valid())
        _UNCONFIRMED_OWNERS.extend(retained)
        try:
            ending = _fingerprints()
            report.update(source_sha256_end=ending, source_unchanged=ending == report.get("source_sha256"))
        except BaseException as error:
            report.update(source_unchanged=False, source_end_error_type=type(error).__name__)
        accepted = (report.get("fixture_body_confirmed") is True and report.get("worker_cleanup_confirmed") is True
            and report.get("resume_count_verified") is True and report.get("actual_resume_calls") == 1
            and report.get("all_token_handles_closed") is True and report.get("source_token_unchanged") is True
            and report.get("source_unchanged") is True and not retained and adapter is not None and not adapter.failed_closes
            and not report.get("unknown_reasons") and not report.get("evidence_budget_exceeded"))
        # Inspect the copied executable again only for an otherwise successful,
        # reaped case. Failed profiles are retained without reopening objects.
        if accepted and destination is not None:
            try:
                token._reject_reparse(destination)
                report["copied_fixture_unchanged"] = sha256(destination.read_bytes()).hexdigest() == FIXTURE_SHA256
                accepted = report["copied_fixture_unchanged"] is True
            except BaseException as error:
                accepted = False
                report["copied_fixture_end_error_type"] = type(error).__name__
        if profile is not None:
            if accepted:
                try:
                    delete_probe_profile(profile)
                    report["cleanup"] = "profile_deleted"
                except BaseException as error:
                    accepted = False
                    report.update(cleanup="failed_profile_may_remain", cleanup_error_type=type(error).__name__)
            else:
                report["cleanup"] = "withheld_unknown_smoke_source_close_or_reap"
        report.update(status="untrusted_ac_native_smoke_observed" if accepted else "unknown",
                      observations_valid=accepted, incomplete=not accepted)
        token._save(output, report)
    return 0 if accepted and not report.get("evidence_budget_exceeded") else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--experiment", action="store_true")
    args = parser.parse_args()
    path = new_report(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    token._reject_reparse(path)
    report: dict[str, Any] = {"started_at": datetime.now(timezone.utc).isoformat(), "stage0_passed": False,
        "write_boundary_verified": False, "fixture_body_confirmed": False, "worker_code_executed": None,
        "native_resume_called": False, "cleanup": "not_required", "fallback_allowed": False,
        "registry_access_requested": False, "object_write_probe_requested": False,
        "token_regain_probe_requested": False, "fixture_sha256": FIXTURE_SHA256,
        "fixture_source_sha256": FIXTURE_SOURCE_SHA256, "fixed_args": ARGS,
        "token_native_call_budget": token.MAX_CALLS, "resume_call_budget": 1,
        "evidence_budget_bytes": token.MAX_REPORT, "per_token_query_budget_bytes": token.MAX_BUFFER,
        "entry_count_budget": token.MAX_ENTRIES,
        "adapter_close_call_budget": mic.MAX_ADAPTER_CLOSES, "post_lower_job_call_budget": mic.MAX_JOB_CALLS,
        "scope": "One fresh AC worker lowered before one ResumeThread; fixed native-v2 smoke only.",
        "coverage_gap": "No registry/file write denial, object LABEL, token-regain or namespace acceptance.",
        "close_evidence_scope": "This diagnostic wrapper records close BOOLs; production adapter still ignores them."}
    with path.open("x", encoding="ascii", newline="\n") as output:
        if not args.experiment:
            report["status"] = "not_executed_explicit_opt_in_required"
            token._save(output, report)
            return 0
        try:
            report["source_sha256"] = _fingerprints()
            token._save(output, report)
            code = _experiment(output, report)
        except BaseException as error:
            report.update(status="unknown", incomplete=True, observations_valid=False,
                          error_type=type(error).__name__, error=str(error)[:1024])
            token._save(output, report)
            code = 2
    print(json.dumps({"report": str(path), "diagnostic_exit": code, "stage0_passed": False}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
