"""Opt-in read-only access observation on one fresh project registry binding.

Only the registered management HKEY is queried, without untagging it or querying
object names. No ACL, registry value, or registry child is written or enumerated.
This observation is not registry denial, cleanup isolation, or stage-zero acceptance.
"""

from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import TextIO, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import diagnose_worker_registry as readonly  # noqa: E402
import diagnose_worker_registry_acl as acl  # noqa: E402
from deskpal.execution.runtime import delete_probe_profile, worker_environment  # noqa: E402
from deskpal.execution.windows._win32 import _CREATE_LOCK, _create_worker  # noqa: E402
from deskpal.execution.windows.launcher import LaunchRejected, LaunchSpec  # noqa: E402

READ_CONTROL = 0x20000
WRITE_DAC = 0x40000
TOKEN_QUERY = 8
ERROR_NO_TOKEN = 1008
PUBLIC_BASIC_BYTES = 56


class PublicObjectBasicInformation(C.Structure):
    # Public winternl.h declaration. Only the first two fields are ever read.
    _fields_ = [("Attributes", C.c_uint32), ("GrantedAccess", C.c_uint32),
                ("HandleCount", C.c_uint32), ("PointerCount", C.c_uint32),
                ("Reserved", C.c_uint32 * 10)]


class AccessApi(acl.WriteApi):
    def __init__(self) -> None:
        super().__init__()
        ntdll = C.WinDLL("ntdll", use_last_error=True)
        self.query_object = readonly._bind(ntdll, "NtQueryObject",
            [W.HANDLE, C.c_int, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32)], C.c_int32)


def _fingerprints() -> dict[str, str]:
    return readonly._fingerprints() | {
        path: sha256((ROOT / path).read_bytes()).hexdigest() for path in (
            "scripts/diagnose_registry_handle_access.py", "scripts/diagnose_worker_registry_acl.py")}


def _token_metadata(api: AccessApi, token: int, result: dict[str, object], expected_type: int) -> None:
    # Host tokens need no package SID and may be ordinary, non-AC tokens.
    result["user_sid"] = readonly._token_sid(api, readonly._token_data(api, token, 1))
    result["token_type"] = C.c_uint32.from_buffer(readonly._token_data(api, token, 8)).value
    result["is_appcontainer"] = C.c_uint32.from_buffer(readonly._token_data(api, token, 29)).value
    if result["token_type"] != expected_type or result["is_appcontainer"] not in (0, 1):
        raise ValueError("handle_access_host_token_shape")
    result["impersonation_level"] = None
    if expected_type == 2:
        level = C.c_uint32.from_buffer(readonly._token_data(api, token, 9)).value
        result["impersonation_level"] = level
        if level not in (0, 1, 2, 3):
            raise ValueError("handle_access_host_impersonation_level")
    result["status"] = "observed"


def _host_context(session: acl.Session, report: dict[str, object]) -> str:
    api = cast(AccessApi, session.api)
    process, thread = acl.HandleOwner(api), acl.HandleOwner(api)
    session.handles.extend((process, thread))  # Register before any partial-success output.
    process_result: dict[str, object] = {"status": "unknown", "expected_token_type": 1}
    thread_result: dict[str, object] = {"status": "unknown", "expected_token_type_if_present": 2}
    result: dict[str, object] = {"status": "unknown", "process": process_result, "thread": thread_result,
        "effective_source": "unknown", "expected_process_is_appcontainer": 0,
        "thread_token_presence_is_not_itself_an_error": True,
        "token_handle_close_scope": "Registered Session handles; direct CloseHandle status checked during cleanup."}
    report["host_context"] = result
    try:
        opened = bool(api.open_process_token(api.current_process(), TOKEN_QUERY, C.byref(process.handle)))
        process_result["open_succeeded"] = opened
        if not opened:
            error = C.get_last_error()
            process_result["open_winerror"] = error
            raise OSError(error, "handle_access_host_process_token")
        if not process.handle.value:
            raise ValueError("handle_access_host_process_token_null")
        _token_metadata(api, int(process.handle.value), process_result, 1)
        result["process_matches_non_appcontainer_expectation"] = process_result["is_appcontainer"] == 0
        opened = bool(api.open_thread_token(api.current_thread(), TOKEN_QUERY, True, C.byref(thread.handle)))
        error = 0 if opened else C.get_last_error()  # Read immediately, before any further native call.
        thread_result.update(open_succeeded=opened, open_winerror=error)
        if not opened:
            if error != ERROR_NO_TOKEN or thread.handle.value:
                raise OSError(error, "handle_access_host_thread_token")
            thread_result["status"] = "no_thread_token"
            effective = process_result
            result["effective_source"] = "process"
        else:
            if not thread.handle.value:
                raise ValueError("handle_access_host_thread_token_null")
            _token_metadata(api, int(thread.handle.value), thread_result, 2)
            effective = thread_result
            result["effective_source"] = "thread"
        user = effective["user_sid"]
        if not isinstance(user, str) or not user:
            raise ValueError("handle_access_effective_user_missing")
        result.update(status="observed", effective_user_sid=user)
        return user
    except BaseException as error:
        result.update(error_type=type(error).__name__, error=str(error)[:2048])
        session.mark_unknown("host_context:" + type(error).__name__ + ":" + str(error))
        raise


def _granted_access(binding: acl.BoundRoot, report: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {"status": "unknown", "state": "preflight", "information_class": 0,
        "structure_bytes": C.sizeof(PublicObjectBasicInformation), "required_structure_bytes": PUBLIC_BASIC_BYTES,
        "returned_length": None, "raw_ntstatus": None, "granted_access": None,
        "attributes": None, "read_control": None, "write_dac": None, "required_access_gate_passed": False,
        "requested_management_access": acl.MANAGEMENT_ACCESS,
        "request_mask_is_not_granted_access_evidence": True}
    report["granted_access"] = result
    length = C.c_uint32()
    try:
        binding.require()
        if (C.sizeof(PublicObjectBasicInformation) != PUBLIC_BASIC_BYTES
                or PublicObjectBasicInformation.Attributes.offset != 0
                or PublicObjectBasicInformation.GrantedAccess.offset != 4):
            raise ValueError("handle_access_public_layout_changed")
        if binding.owner.handle.value in (None, 0, C.c_void_p(-1).value):
            raise ValueError("handle_access_invalid_registered_handle")
        information = PublicObjectBasicInformation()
        result["state"] = "query_call"
        status = int(cast(AccessApi, binding.session.api).query_object(binding.owner.handle, 0,
            C.byref(information), PUBLIC_BASIC_BYTES, C.byref(length)))
        result.update(raw_ntstatus=status, ntstatus_hex=f"0x{status & 0xFFFFFFFF:08x}", returned_length=length.value)
        if status != 0 or length.value != PUBLIC_BASIC_BYTES:
            raise ValueError("handle_access_ntqueryobject_status_or_length")
        granted = int(information.GrantedAccess)
        rc, wd = bool(granted & READ_CONTROL), bool(granted & WRITE_DAC)
        result.update(status="observed", state="observed", attributes=int(information.Attributes),
                      granted_access=granted, granted_access_hex=f"0x{granted:08x}",
                      read_control=rc, write_dac=wd, required_access_gate_passed=rc and wd)
        if not (rc and wd):
            raise ValueError("handle_access_required_bits_missing")
        return result
    except BaseException as error:
        result.update(state="failed", returned_length=length.value,
                      error_type=type(error).__name__, error=str(error)[:2048])
        binding.session.mark_unknown("granted_access:" + type(error).__name__ + ":" + str(error))
        raise


def _observe_bound_root(binding: acl.BoundRoot, report: dict[str, object]) -> None:
    snapshots: dict[str, object] = {"binding_baseline": binding.before, "all_match": False}
    report["security_descriptors"] = snapshots
    try:
        binding.require()
        before = acl._snapshot(binding.session.api, binding.owner.handle, binding.profile.sid, binding.user)
        snapshots["before_query"] = before
        if before != binding.before:
            raise ValueError("handle_access_sd_changed_before_query")
        try:
            _granted_access(binding, report)
        finally:
            # Preserve a post-query observation even when NtQueryObject failed.
            try:
                after = acl._snapshot(binding.session.api, binding.owner.handle, binding.profile.sid, binding.user)
                snapshots["after_query"] = after
                snapshots["all_match"] = before == after == binding.before
                if snapshots["all_match"] is not True:
                    raise ValueError("handle_access_sd_changed_after_query")
            except BaseException as error:
                snapshots.update(after_error_type=type(error).__name__, after_error=str(error)[:2048])
                raise
    except BaseException as error:
        snapshots.update(error_type=type(error).__name__, error=str(error)[:2048])
        binding.session.mark_unknown("access_sd_observation:" + type(error).__name__ + ":" + str(error))
        raise


def _source_end(report: dict[str, object]) -> bool:
    ending = _fingerprints()
    report["source_sha256_end"] = ending
    unchanged = report.get("source_sha256") == ending
    report["source_unchanged"] = unchanged
    return unchanged


def _experiment(output: TextIO, report: dict[str, object]) -> int:
    if os.name != "nt" or C.sizeof(C.c_void_p) != 8:
        raise ValueError("handle_access_windows_x64_only")
    api = AccessApi()
    session = acl.Session(api)
    code = 2
    try:
        profile = session.create()
        report.update(profile=asdict(profile), cleanup="pending")
        readonly._save(output, report)
        readonly._reject_reparse(Path(profile.folder))
        temporary = Path(profile.folder) / "Temp"
        readonly._reject_reparse(temporary)
        temporary.mkdir(exist_ok=True)
        readonly._reject_reparse(temporary)
        windows = C.create_unicode_buffer(32768)
        length = int(api.windows_directory(windows, len(windows)))
        if not 0 < length < len(windows):
            raise ValueError("handle_access_windows_directory_failed")
        environment = worker_environment(Path(windows.value), temporary, local_appdata=Path(profile.folder))
        readonly._reject_reparse(readonly.FIXTURE)
        if sha256(readonly.FIXTURE.read_bytes()).hexdigest() != readonly.FIXTURE_SHA256:
            raise ValueError("handle_access_fixture_pin_mismatch")
        destination = Path(profile.folder) / "native-fixture.exe"
        readonly._reject_reparse(destination)
        with readonly.FIXTURE.open("rb") as source, destination.open("xb") as target:
            target.write(source.read())
        if sha256(destination.read_bytes()).hexdigest() != readonly.FIXTURE_SHA256:
            raise ValueError("handle_access_copied_fixture_pin_mismatch")
        binding_report: dict[str, object] = {}
        report["binding"] = binding_report
        observed = False

        def observe(process: int) -> None:
            nonlocal observed
            if observed:
                raise ValueError("handle_access_binding_callback_repeated")
            observed = True
            binding_report.update(acl._bind_from_worker(session, process, environment))

        suspended = readonly.SuspendedRegistryApi(observe)
        stopped: dict[str, object] = {"reap_confirmed_by_adapter_contract": False,
            "evidence_scope": "STOP_STAGE and no cleanup_owner under the fixed adapter contract; not independent OS handle-close verification."}
        report["suspended_worker"] = stopped
        report.update(native_resume_called=None, worker_code_executed=None)
        try:
            with _CREATE_LOCK:
                native, _identity = _create_worker(LaunchSpec(destination, (), destination.parent,
                    profile.sid, environment), _api=suspended)
            session.owners.append(native)
            raise ValueError("handle_access_unexpected_resume_return")
        except LaunchRejected as error:
            stopped.update(stop_stage=error.stage, cleanup_owner_present=error.cleanup_owner is not None)
            if error.cleanup_owner is not None:
                session.owners.append(error.cleanup_owner)
            if (error.stage != readonly.STOP_STAGE or error.cleanup_owner is not None
                    or not observed or not suspended.resume_intercepted):
                raise
            stopped["reap_confirmed_by_adapter_contract"] = True
            report.update(native_resume_called=False, worker_code_executed=False)
        finally:
            stopped.update(creation_flags=suspended.creation_flags, resume_intercepted=suspended.resume_intercepted)
        binding = session.root
        if binding is None or not session.helpers or not all(owner.reaped for owner in session.helpers) or session.owners:
            raise ValueError("handle_access_binding_or_reap_missing")
        binding.require()
        effective_user = _host_context(session, report)
        context = cast(dict[str, object], report["host_context"])
        context["matches_binding_user"] = effective_user == binding.user
        if effective_user != binding.user:
            raise ValueError("handle_access_effective_user_binding_mismatch")
        _observe_bound_root(binding, report)
        report["status"] = "read_only_handle_access_observed"
        code = 0
    except BaseException as error:
        session.mark_unknown("handle_access_experiment:" + type(error).__name__ + ":" + str(error))
        report.update(status="handle_access_observation_blocked", error_type=type(error).__name__, error=str(error)[:2048])
        if not isinstance(error, Exception):
            raise
    finally:
        try:
            if not _source_end(report):
                session.mark_unknown("handle_access_source_changed")
        except BaseException as error:
            session.mark_unknown("handle_access_source_end_unknown")
            report.update(source_unchanged=False, source_end_error_type=type(error).__name__, source_end_error=str(error)[:2048])
        safe = session.close_resources()
        report["cleanup_state"] = session.cleanup_state()
        if safe and session.profile is not None:
            try:
                delete_probe_profile(session.profile)
                report["cleanup"] = "profile_deleted"
            except Exception as error:
                report.update(cleanup="failed_profile_may_remain", cleanup_error=str(error)[:2048])
                code = 2
        elif session.profile is not None:
            report["cleanup"] = "withheld_unknown_helper_worker_key_or_binding"
            code = 2
        readonly._save(output, report)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--experiment", action="store_true")
    args = parser.parse_args()
    path = readonly._new_report(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    readonly._reject_reparse(path)
    report: dict[str, object] = {"stage0_passed": False, "cleanup": "not_required",
        "native_resume_called": False, "worker_code_executed": False,
        "scope": "read-only actual granted-access and own host-token observation after fresh profile and binding initialization",
        "sd_mutation_requested": False, "registry_values_read": False, "registry_names_enumerated": False,
        "fallback_allowed": False, "individual_adapter_handle_closes_verified": False,
        "popen_own_process_handle_verified": False,
        "observation_limits": "Only this fresh registered handle at query time; no inference about retained runs' past permissions.",
        "coverage_gap": "No registry-denial, cleanup-isolation, or namespace-coverage acceptance."}
    with path.open("x", encoding="ascii") as output:
        if not args.experiment:
            report["status"] = "not_executed_explicit_opt_in_required"
            readonly._save(output, report)
            return 0
        code = 2
        try:
            report["source_sha256"] = _fingerprints()
            code = _experiment(output, report)
        except Exception as error:
            report.update(status="handle_access_observation_blocked", error_type=type(error).__name__, error=str(error)[:2048])
        if "source_sha256_end" not in report:
            try:
                _source_end(report)
            except Exception as error:
                report.update(source_unchanged=False, source_end_error_type=type(error).__name__, source_end_error=str(error)[:2048])
        if report.get("source_unchanged") is not True:
            code = 2
        readonly._save(output, report)
    print(json.dumps({"report": str(path), "diagnostic_exit": code, "stage0_passed": False}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
