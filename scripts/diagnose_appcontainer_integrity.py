"""Opt-in suspended AppContainer primary-token integrity-lowering observation.

Only a fresh profile and its pinned fixture copy are used. The original adapter
verifies its launch first; the replacement resume hook never resumes a thread.
No object-access probe, registry API, DACL API or privilege adjustment is used.
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
import struct
import sys
from typing import Any, TextIO
from uuid import uuid4

import diagnose_write_restricted_token as token
import diagnose_worker_registry as suspended

from deskpal.execution.runtime import create_probe_profile, delete_probe_profile, worker_environment
from deskpal.execution.windows import _win32 as core
from deskpal.execution.windows.launcher import LaunchRejected, LaunchSpec

__all__ = ["LaunchRejected", "LaunchSpec", "core", "suspended", "token"]

ROOT = Path(__file__).resolve().parents[1]
STOP_STAGE = "appcontainer_integrity_diagnostic_resume_blocked"
MAX_ADAPTER_CLOSES, MAX_JOB_CALLS = 24, 8
WORKER_CLASSES = (*token.QUERY_CLASSES, ("integrity", 25), ("mandatory_policy", 27),
                  ("is_appcontainer", 29), ("capabilities", 30), ("package_sid", 31))
UNTRUSTED_SID = bytes((1, 1)) + (16).to_bytes(6, "big") + struct.pack("<I", 0)
_UNCONFIRMED_OWNERS: list[Any] = []


class NativeApi(token.NativeApi):
    """Exact token observation/set-IL surface; no restricted-token creation."""

    def __init__(self) -> None:
        kernel = C.WinDLL("kernel32", use_last_error=True)
        advapi = C.WinDLL("advapi32", use_last_error=True)
        declarations = (
            (kernel, "GetCurrentProcess", [], token.HANDLE),
            (kernel, "GetCurrentThread", [], token.HANDLE),
            (kernel, "CloseHandle", [token.HANDLE], token.BOOL),
            (kernel, "GetWindowsDirectoryW", [C.c_wchar_p, token.DWORD], token.DWORD),
            (advapi, "OpenProcessToken", [token.HANDLE, token.DWORD, C.POINTER(token.HANDLE)], token.BOOL),
            (advapi, "OpenThreadToken", [token.HANDLE, token.DWORD, token.BOOL, C.POINTER(token.HANDLE)], token.BOOL),
            (advapi, "GetTokenInformation", [token.HANDLE, C.c_int, C.c_void_p,
                token.DWORD, C.POINTER(token.DWORD)], token.BOOL),
            (advapi, "SetTokenInformation", [token.HANDLE, C.c_int, C.c_void_p, token.DWORD], token.BOOL),
        )  # type: tuple[tuple[Any, str, list[Any], Any], ...]
        self.functions: dict[str, Any] = {}
        for library, name, arguments, result in declarations:
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result
            self.functions[name] = function


def _additional_parse(kind: int, data: bytes, base: int) -> Any:
    if kind == 25:
        if len(data) < 16 or struct.unpack_from("<Q", data)[0] < base + 16:
            raise ValueError("integrity_label_pointer_in_header")
        value = token._parse(1, data, base)
        if len(value["sid"].split("-")) != 4 or not value["sid"].startswith("S-1-16-"):
            raise ValueError("integrity_label_sid_shape")
        return value
    if kind == 27:
        if len(data) != 4 or struct.unpack("<I", data)[0] & ~3:
            raise ValueError("integrity_mandatory_policy_shape")
        return struct.unpack("<I", data)[0]
    if kind == 30:
        return token._parse(2, data, base)
    if kind == 31:
        if len(data) < C.sizeof(C.c_void_p):
            raise ValueError("integrity_package_pointer_short")
        address = struct.unpack_from("<Q", data)[0]
        if address < base + 8:
            raise ValueError("integrity_package_pointer_in_header")
        return token._sid(data, base, address)
    raise ValueError("integrity_additional_class_not_allowed")


class Session(token.Session):
    def query(self, owner: token.Owner, kind: int, label: str) -> Any:
        if kind not in (25, 27, 30, 31):
            return super().query(owner, kind, label)
        observation: dict[str, Any] = {"subject": owner.label, "class": kind,
            "label": label, "payload_valid": False, "status": "unknown"}
        self.report["token_queries"].append(observation)
        buffer = None
        try:
            needed = token.DWORD()
            probe = self.call("GetTokenInformation", owner.handle, kind, None, 0,
                              C.byref(needed), length=needed)
            observation["size_query"] = probe
            if probe["raw_return"] != 0 or probe["last_error"] != 122 or not 1 <= needed.value <= token.MAX_BUFFER:
                raise ValueError("integrity_query_size_or_budget")
            capacity = int(needed.value)
            buffer = C.create_string_buffer(capacity)
            received = token.DWORD()
            read = self.call("GetTokenInformation", owner.handle, kind, buffer, capacity,
                             C.byref(received), length=received)
            observation["read_query"] = read
            if read["raw_return"] == 0 or not 1 <= received.value <= capacity:
                raise ValueError("integrity_query_read_or_length")
            data = bytes(buffer.raw[:received.value])
            observation["buffer_sha256"] = sha256(data).hexdigest()
            value = _additional_parse(kind, data, C.addressof(buffer))
            observation.update(payload_valid=True, status="observed",
                               evidence_encoding="complete_required_structure_fields")
            return value
        except BaseException as error:
            observation.update(error_type=type(error).__name__, error=str(error)[:512], incomplete=True)
            if buffer is not None:
                raw = bytes(buffer.raw)
                observation.update(buffer_sha256=sha256(raw).hexdigest(), bounded_buffer_prefix_hex=raw[:256].hex(),
                                   allocated_bytes=len(raw), raw_prefix_complete=len(raw) <= 256)
            raise
        finally:
            self.save()

    def worker_snapshot(self, owner: token.Owner, label: str) -> dict[str, Any]:
        value: dict[str, Any] = {}
        self.report[label] = value
        for name, kind in WORKER_CLASSES:
            value[name] = self.query(owner, kind, label + ":" + name)
        self.save()
        return value

    def host_snapshot(self, owner: token.Owner, label: str) -> dict[str, Any]:
        value = self.snapshot(owner, label, source=True)
        value["integrity"] = self.query(owner, 25, label + ":integrity")
        value["mandatory_policy"] = self.query(owner, 27, label + ":mandatory_policy")
        self.save()
        return value


def _non_integrity_groups(observation: dict[str, Any]) -> list[dict[str, Any]]:
    groups = observation["groups"]
    integrity = [entry for entry in groups if entry["attributes"] & 0x20]
    if len(integrity) != 1 or integrity[0] != observation["integrity"]:
        raise ValueError("integrity_group_label_inconsistent")
    return [entry for entry in groups if not entry["attributes"] & 0x20]


def _gates(before: dict[str, Any], after: dict[str, Any], user: str, package: str) -> dict[str, bool]:
    stable = ("user", "type", "is_appcontainer", "capabilities", "package_sid", "privileges", "restricted_sids")
    return {
        "primary_same_user": before["type"] == after["type"] == 1
            and before["user"]["sid"] == after["user"]["sid"] == user,
        "same_ac_cap0_package": before["is_appcontainer"] == after["is_appcontainer"] == 1
            and before["capabilities"] == after["capabilities"] == []
            and before["package_sid"] == after["package_sid"] == package,
        "before_above_untrusted": before["integrity"]["sid"].startswith("S-1-16-")
            and int(before["integrity"]["sid"].split("-")[-1]) > 0,
        "after_exact_untrusted": after["integrity"]["sid"] == "S-1-16-0"
            and after["integrity"]["attributes"] == before["integrity"]["attributes"],
        "policy_unchanged_no_write_up": before["mandatory_policy"] == after["mandatory_policy"]
            and bool(after["mandatory_policy"] & 1),
        "selected_identity_fields_unchanged": all(before[name] == after[name] for name in stable),
        "only_integrity_group_changed": _non_integrity_groups(before) == _non_integrity_groups(after),
        "same_worker_token_id": before["statistics"]["token_id"] == after["statistics"]["token_id"],
    }


class IntegrityApi(suspended.SuspendedRegistryApi):
    """Private adapter: original creation/verification, checked closes, no resume."""

    def __init__(self, session: Session, observe: Any) -> None:
        super().__init__(observe)
        self.session, self.job = session, 0
        self.failed_closes: list[dict[str, Any]] = []
        self.withheld_tokens: list[token.Owner] = []
        self._native_close, self._native_open = self.close, self.open_token
        self._native_assign = self.assign_job
        self.close, self.open_token, self.assign_job = self._checked_close, self._owned_open, self._capture_job

    def _capture_job(self, job: Any, process: Any) -> int:
        self.job = int(job)
        return int(self._native_assign(job, process))

    def _owned_open(self, process: Any, access: Any, output: Any) -> int:
        owner = self.session.owner("adapter_verify_token")
        try:
            return int(self._native_open(process, access, output))
        finally:
            owner.handle.value = C.cast(output, C.POINTER(token.HANDLE)).contents.value

    def _checked_close(self, handle: Any) -> int:
        # Do not raise: the frozen adapter ignores BOOL and must finish its
        # remaining closes. Failed handles are retained, never blindly retried.
        value = int(getattr(handle, "value", handle) or 0)
        event: dict[str, Any] = {"handle": value, "raw_return": None, "last_error": None}
        events = self.session.report["adapter_close_events"]
        result, error = 0, 0
        invalid = not value or C.c_void_p(value).value in token.PSEUDO_HANDLES
        if len(events) >= MAX_ADAPTER_CLOSES:
            event["state"] = "withheld_close_budget"
            self.session.report.setdefault("first_withheld_adapter_close", event)
            self.session.unknown("integrity_adapter_close_budget")
        elif invalid:
            event["state"] = "withheld_invalid_or_pseudo_handle"
            events.append(event)
        else:
            events.append(event)
            event["state"] = "call_entered"
            try:
                C.set_last_error(0)
                result = int(self._native_close(handle) or 0)
                error = C.get_last_error()
                event.update(state="returned", raw_return=result, last_error=error)
            except BaseException as caught:
                event.update(state="wrapper_exception", error_type=type(caught).__name__, error=str(caught)[:512])
        for owner in self.session.owners:
            if owner.label == "adapter_verify_token" and owner.handle.value == value:
                if not result:
                    held = token.Owner(owner.label + ":close_failed")
                    held.handle.value = value
                    self.withheld_tokens.append(held)
                owner.handle.value = None
        if not result:
            if len(self.failed_closes) < MAX_ADAPTER_CLOSES:
                self.failed_closes.append(event)
            self.session.unknown("integrity_adapter_close_failed")
        # Keep GetLastError compatible with the adapter's native-call contract.
        C.set_last_error(error)
        return result

    def _read_before_resume(self, handle: object) -> int:
        if not self.process_handle or self.resume_intercepted:
            raise LaunchRejected("integrity_resume_hook_invalid_state")
        self.resume_intercepted = True
        self.callback(self.process_handle)
        raise LaunchRejected(STOP_STAGE)


def _job_check(api: IntegrityApi, spec: LaunchSpec, process: int, report: dict[str, Any]) -> None:
    events: list[dict[str, Any]] = report["post_lower_job_calls"]

    def call(name: str, *args: Any) -> None:
        if len(events) >= MAX_JOB_CALLS:
            raise ValueError("integrity_job_call_budget")
        event: dict[str, Any] = {"api": name, "state": "call_entered"}
        events.append(event)
        C.set_last_error(0)
        result = int(getattr(api, name)(*args) or 0)
        error = C.get_last_error()
        event.update(state="returned", raw_return=result, last_error=error)
        if not result:
            raise ValueError("integrity_post_lower_job_query_failed:" + name)

    limits, cpu, member, child = core.JOB_EXTENDED_LIMIT(), core.JOB_CPU(), core.BOOL(), core.DWORD()
    call("get_mitigation", process, 13, C.byref(child), C.sizeof(child))
    call("query_job", api.job, 9, C.byref(limits), C.sizeof(limits), None)
    call("query_job", api.job, 15, C.byref(cpu), C.sizeof(cpu), None)
    call("in_job", process, api.job, C.byref(member))
    basic = limits.BasicLimitInformation
    actual = {"child_flags": int(child.value), "member": bool(member.value),
        "limit_flags": int(basic.LimitFlags), "active_process_limit": int(basic.ActiveProcessLimit),
        "process_memory": int(limits.ProcessMemoryLimit), "job_memory": int(limits.JobMemoryLimit),
        "cpu_flags": int(cpu.ControlFlags), "cpu_rate": int(cpu.CpuRate)}
    report["post_lower_job"] = actual
    if (not child.value & 1 or not member.value or basic.LimitFlags != core._LIMIT_FLAGS
            or basic.ActiveProcessLimit != spec.limits.active_processes
            or limits.ProcessMemoryLimit != spec.limits.memory_bytes
            or limits.JobMemoryLimit != spec.limits.memory_bytes
            or cpu.ControlFlags != core._CPU_FLAGS or cpu.CpuRate != spec.limits.cpu_rate):
        raise ValueError("integrity_post_lower_job_mismatch")


def _observe(session: Session, process: int, package: str) -> None:
    token._thread_absent(session, "thread_before")
    source = session.owner("source")
    current = session.call("GetCurrentProcess")["raw_return"]
    opened = session.call("OpenProcessToken", token.HANDLE(current), 8, C.byref(source.handle))
    token._opened(session, source, opened)
    before_host = session.host_snapshot(source, "source_before")
    if before_host["type"] != 1 or before_host["is_appcontainer"] != 0:
        raise ValueError("integrity_host_context_rejected")
    worker = session.owner("worker")
    opened = session.call("OpenProcessToken", token.HANDLE(process), 0x88, C.byref(worker.handle))
    token._opened(session, worker, opened)
    before = session.worker_snapshot(worker, "worker_before")
    # Validate the baseline before the sole mutation. A no-op/relabel upward is
    # not permitted even if a future platform changes the initial AC level.
    if (before["type"] != 1 or before["user"]["sid"] != before_host["user"]["sid"]
            or before["is_appcontainer"] != 1 or before["package_sid"] != package
            or before["capabilities"] or not before["mandatory_policy"] & 1
            or not before["integrity"]["sid"].startswith("S-1-16-")
            or int(before["integrity"]["sid"].split("-")[-1]) <= 0):
        raise ValueError("integrity_worker_baseline_rejected")
    _non_integrity_groups(before)
    # Owned contiguous TOKEN_MANDATORY_LABEL(16) + fixed SID(12).
    payload = C.create_string_buffer(16 + len(UNTRUSTED_SID))
    C.memmove(C.addressof(payload) + 16, UNTRUSTED_SID, len(UNTRUSTED_SID))
    label = token.SidAttributes.from_buffer(payload)
    label.Sid, label.Attributes = C.addressof(payload) + 16, 0x20
    session.report["set_integrity"] = {"state": "prepared", "information_class": 25,
        "sid": "S-1-16-0", "attributes": 0x20, "buffer_bytes": C.sizeof(payload),
        "sid_hex": UNTRUSTED_SID.hex(), "prepared_does_not_prove_call_entered": True}
    session.save()
    if session.report.get("evidence_budget_exceeded"):
        raise ValueError("integrity_prepared_evidence_incomplete")
    event = session.call("SetTokenInformation", worker.handle, 25, payload, C.sizeof(payload))
    session.report["set_integrity"].update(state="returned", native=event)
    after = session.worker_snapshot(worker, "worker_after")
    checks = _gates(before, after, before_host["user"]["sid"], package)
    session.report["worker_gate_checks"] = checks
    session.save()
    if not event["raw_return"] or not all(checks.values()):
        raise ValueError("integrity_set_or_readback_failed")


def _fingerprints() -> dict[str, str]:
    paths = ("scripts/diagnose_appcontainer_integrity.py", "tests/unit/test_appcontainer_integrity_diagnostic.py",
        "scripts/diagnose_write_restricted_token.py", "scripts/diagnose_worker_registry.py",
        "src/deskpal/execution/windows/_win32.py", "src/deskpal/execution/windows/launcher.py",
        "src/deskpal/execution/runtime.py", "tests/fixtures/windows/native_probe_v2.c",
        "data/v06-stage0/native-fixture-v2/fixture.exe")
    return {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}


def _experiment(output: TextIO, report: dict[str, Any]) -> int:
    session: Session | None = None
    adapter: IntegrityApi | None = None
    profile: Any = None
    pending: list[tuple[str, Any]] = []
    observed = False
    try:
        if os.name != "nt":
            raise ValueError("integrity_windows_x64_only")
        token._layout()
        session = Session(NativeApi(), output, report)
        report.update(adapter_close_events=[], post_lower_job_calls=[], worker_cleanup_events=[])
        suspended._reject_reparse(suspended.FIXTURE)
        if sha256(suspended.FIXTURE.read_bytes()).hexdigest() != suspended.FIXTURE_SHA256:
            raise ValueError("integrity_fixture_pin_mismatch")
        profile = create_probe_profile("deskpal.v06." + uuid4().hex)
        report.update(profile=asdict(profile), cleanup="pending")
        session.save()
        folder = Path(profile.folder)
        suspended._reject_reparse(folder)
        temporary, destination = folder / "Temp", folder / "native-fixture.exe"
        suspended._reject_reparse(temporary)
        temporary.mkdir(exist_ok=True)
        suspended._reject_reparse(temporary)
        suspended._reject_reparse(destination)
        with suspended.FIXTURE.open("rb") as source, destination.open("xb") as copied:
            copied.write(source.read())
        if sha256(destination.read_bytes()).hexdigest() != suspended.FIXTURE_SHA256:
            raise ValueError("integrity_copied_fixture_pin_mismatch")
        windows = C.create_unicode_buffer(32768)
        result = session.call("GetWindowsDirectoryW", windows, len(windows))["raw_return"]
        if not 0 < result < len(windows):
            raise ValueError("integrity_windows_directory_failed")
        environment = worker_environment(Path(windows.value), temporary, local_appdata=folder)
        report["requested_child_environment"] = environment
        spec = LaunchSpec(destination, (), folder, profile.sid, environment)

        def observe(process: int) -> None:
            nonlocal observed
            assert session is not None
            if observed:
                raise ValueError("integrity_callback_repeated")
            observed = True
            report["original_adapter_verification_reached_resume_hook"] = True
            if report.get("unknown_reasons"):
                raise ValueError("integrity_adapter_baseline_already_unknown")
            _observe(session, process, profile.sid)
            assert adapter is not None
            _job_check(adapter, spec, process, report)
            report["lowering_and_identity_observed"] = True
            session.save()

        adapter = IntegrityApi(session, observe)
        try:
            with core._CREATE_LOCK:
                native, _identity = core._create_worker(spec, _api=adapter)
            pending.append(("unexpected_native_return", native))
            raise ValueError("integrity_unexpected_launch_return")
        except LaunchRejected as error:
            report.update(stop_stage=error.stage, stage_winerror=error.winerror,
                          cleanup_owner_present=error.cleanup_owner is not None)
            if error.cleanup_owner is not None:
                pending.append(("adapter_cleanup_unconfirmed", error.cleanup_owner))
            if error.stage != STOP_STAGE or error.cleanup_owner is not None or not adapter.resume_intercepted:
                raise
            report["reap_confirmed_by_adapter_contract"] = True
        finally:
            report.update(creation_flags=adapter.creation_flags, resume_intercepted=adapter.resume_intercepted)
    except BaseException as error:
        report.update(error_type=type(error).__name__, error=str(error)[:2048])
        if session is not None:
            session.unknown("integrity_observation:" + type(error).__name__ + ":" + str(error))
    finally:
        for reason, owner in pending:
            cleanup_event: dict[str, Any] = {"reason": reason, "retained_owner": True,
                "automatic_retry": False, "close_attempted": False}
            report["worker_cleanup_events"].append(cleanup_event)
            if reason == "adapter_cleanup_unconfirmed":
                cleanup_event["state"] = "retained_owner_no_automatic_retry"
                _UNCONFIRMED_OWNERS.append(owner)
                continue
            # An unexpected successful return has not had a failed cleanup.
            # Attempt its necessary stop/close once, while keeping this run
            # unknown regardless of whether that exceptional cleanup succeeds.
            cleanup_event.update(state="necessary_close_entered", close_attempted=True)
            try:
                owner.close()
                cleanup_event.update(state="necessary_close_returned", retained_owner=False)
            except BaseException as error:
                cleanup_event.update(state="necessary_close_failed_owner_retained",
                    error_type=type(error).__name__, error=str(error)[:512])
                _UNCONFIRMED_OWNERS.append(owner)
        if session is not None:
            source_owner = next((candidate for candidate in session.owners
                if candidate.label == "source"), None)
            try:
                if source_owner is None or not source_owner.valid() or "source_before" not in report:
                    raise ValueError("integrity_source_before_missing")
                after_host = session.host_snapshot(source_owner, "source_after")
                report["source_token_unchanged"] = report["source_before"] == after_host
                if not report["source_token_unchanged"]:
                    raise ValueError("integrity_source_changed")
            except BaseException as error:
                session.unknown("integrity_source_after:" + type(error).__name__ + ":" + str(error))
            try:
                token._thread_absent(session, "thread_after")
            except BaseException as error:
                session.unknown("integrity_thread_after:" + type(error).__name__ + ":" + str(error))
            session.close()
        try:
            ending = _fingerprints()
            report.update(source_sha256_end=ending, source_unchanged=ending == report.get("source_sha256"))
        except BaseException as error:
            report.update(source_unchanged=False, source_end_error=str(error)[:512])
        accepted = (report.get("lowering_and_identity_observed") is True
            and report.get("reap_confirmed_by_adapter_contract") is True
            and report.get("all_token_handles_closed") is True
            and report.get("source_token_unchanged") is True and report.get("source_unchanged") is True
            and not pending and adapter is not None and not adapter.failed_closes
            and not report.get("unknown_reasons") and not report.get("evidence_budget_exceeded"))
        if profile is not None:
            if accepted:
                try:
                    delete_probe_profile(profile)
                    report["cleanup"] = "profile_deleted"
                except BaseException as error:
                    accepted = False
                    report.update(cleanup="failed_profile_may_remain", cleanup_error=str(error)[:512])
            else:
                report["cleanup"] = "withheld_unknown_observation_reap_close_or_source"
        if adapter is not None and adapter.withheld_tokens:
            _UNCONFIRMED_OWNERS.extend(adapter.withheld_tokens)
        if session is not None:
            _UNCONFIRMED_OWNERS.extend(owner for owner in session.owners if owner.valid())
        report.update(status="suspended_ac_integrity_lowering_observed" if accepted else "unknown",
                      observations_valid=accepted, incomplete=not accepted)
        token._save(output, report)
    return 0 if accepted and not report.get("evidence_budget_exceeded") else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--experiment", action="store_true")
    args = parser.parse_args()
    path = token._new_report(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    token._reject_reparse(path)
    report: dict[str, Any] = {"started_at": datetime.now(timezone.utc).isoformat(),
        "stage0_passed": False, "write_boundary_verified": False,
        "native_resume_called": False, "worker_code_executed": False,
        "registry_access_requested": False, "object_access_probes_requested": False,
        "dacl_mutation_requested": False, "source_privilege_mutation_requested": False,
        "fallback_allowed": False, "cleanup": "not_required", "observations_valid": False,
        "scope": "fresh suspended AC primary-token IL lowering and selected invariant observation only",
        "coverage_gap": "No actual writes, object labels, namespace coverage, startup or inherited-handle isolation tested.",
        "reap_evidence_scope": "Frozen adapter terminate/wait contract; no independent OS handle-close proof.",
        "adapter_close_evidence_scope": "This private wrapper records actual calls only; frozen production adapter still ignores BOOL.",
        "evidence_budget_bytes": token.MAX_REPORT, "token_native_call_budget": token.MAX_CALLS,
        "post_lower_job_call_budget": MAX_JOB_CALLS, "adapter_close_call_budget": MAX_ADAPTER_CLOSES,
        "per_token_query_budget_bytes": token.MAX_BUFFER, "entry_count_budget": token.MAX_ENTRIES}
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
                          error_type=type(error).__name__, error=str(error)[:2048])
            token._save(output, report)
            code = 2
    print(json.dumps({"report": str(path), "diagnostic_exit": code, "stage0_passed": False}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
