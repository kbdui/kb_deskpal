"""Opt-in, suspended-only AppContainer registry SD location diagnostic.

Only the outer host creates/deletes two NEW profiles and owns worker Jobs.
Helpers inherit one query/duplicate token handle, never a process/Job handle.
They impersonate, request READ_CONTROL, and read token/security metadata only.
No registry values, key names, enumeration, writes or SD changes are requested.
This is an experimental A/B/A observation, not a registry binding guarantee.
Run actual experiments only through the approved native experiment command.
"""

from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import Any, Callable, TextIO
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deskpal.execution.runtime import (  # noqa: E402
    ProbeProfile, create_probe_profile, delete_probe_profile, worker_environment,
)
from deskpal.execution.windows._win32 import Api, _CREATE_LOCK, _create_worker  # noqa: E402
from deskpal.execution.windows.launcher import CleanupOwner, LaunchRejected, LaunchSpec  # noqa: E402

FIXTURE_SHA256 = "08423adb75615e3609e268409be844053bf2e14af1d4d250917006c0ba7ab4e1"
FIXTURE = ROOT / "data/v06-stage0/native-fixture-v2/fixture.exe"
STOP_STAGE = "registry_readonly_diagnostic_resume_blocked"
MAX_SD_BYTES = 65536
MAX_HELPER_BYTES = 128 * 1024
_UNCONFIRMED_OWNERS: list[object] = []


def _bind(dll: Any, name: str, arguments: list[Any], result: Any) -> Any:
    function = getattr(dll, name)
    function.argtypes, function.restype = arguments, result
    return function


def _require(ok: object, stage: str) -> None:
    if not ok:
        raise OSError(C.get_last_error(), stage)


class RegistryApi:
    """Fixed read-only API surface; no registry mutation/enumeration bindings."""

    def __init__(self) -> None:
        kernel = C.WinDLL("kernel32", use_last_error=True)
        advapi = C.WinDLL("advapi32", use_last_error=True)
        userenv = C.WinDLL("userenv", use_last_error=True)
        self.close = _bind(kernel, "CloseHandle", [W.HANDLE], W.BOOL)
        self.local_free = _bind(kernel, "LocalFree", [C.c_void_p], C.c_void_p)
        self.current_thread = _bind(kernel, "GetCurrentThread", [], W.HANDLE)
        self.windows_directory = _bind(kernel, "GetWindowsDirectoryW", [W.LPWSTR, W.UINT], W.UINT)
        self.open_process_token = _bind(advapi, "OpenProcessToken",
            [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL)
        self.open_thread_token = _bind(advapi, "OpenThreadToken",
            [W.HANDLE, W.DWORD, W.BOOL, C.POINTER(W.HANDLE)], W.BOOL)
        self.get_token = _bind(advapi, "GetTokenInformation",
            [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL)
        self.duplicate_token = _bind(advapi, "DuplicateTokenEx",
            [W.HANDLE, W.DWORD, C.c_void_p, C.c_int, C.c_int, C.POINTER(W.HANDLE)], W.BOOL)
        self.impersonate = _bind(advapi, "ImpersonateLoggedOnUser", [W.HANDLE], W.BOOL)
        self.revert = _bind(advapi, "RevertToSelf", [], W.BOOL)
        self.sid_string = _bind(advapi, "ConvertSidToStringSidW",
            [C.c_void_p, C.POINTER(C.c_void_p)], W.BOOL)
        self.get_registry = _bind(userenv, "GetAppContainerRegistryLocation",
            [W.DWORD, C.POINTER(W.HKEY)], C.c_int32)
        self.reg_close = _bind(advapi, "RegCloseKey", [W.HKEY], C.c_int32)
        self.get_security = _bind(advapi, "GetSecurityInfo",
            [W.HANDLE, C.c_int, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p,
             C.POINTER(C.c_void_p), C.c_void_p, C.POINTER(C.c_void_p)], W.DWORD)
        self.sd_length = _bind(advapi, "GetSecurityDescriptorLength", [C.c_void_p], W.DWORD)
        self.sd_control = _bind(advapi, "GetSecurityDescriptorControl",
            [C.c_void_p, C.POINTER(C.c_uint16), C.POINTER(W.DWORD)], W.BOOL)
        self.acl_info = _bind(advapi, "GetAclInformation",
            [C.c_void_p, C.c_void_p, W.DWORD, C.c_int], W.BOOL)
        self.get_ace = _bind(advapi, "GetAce", [C.c_void_p, W.DWORD, C.POINTER(C.c_void_p)], W.BOOL)


def _sid(api: RegistryApi, address: int | None, start: int, end: int) -> str:
    if not address:
        raise ValueError("missing_sid")
    if not start <= address <= end - 8:
        raise ValueError("sid_header_outside_buffer")
    revision = C.c_ubyte.from_address(address).value
    subauthorities = C.c_ubyte.from_address(address + 1).value
    if revision != 1 or subauthorities > 15 or address + 8 + 4 * subauthorities > end:
        raise ValueError("sid_length_outside_buffer")
    text = C.c_void_p()
    _require(api.sid_string(address, C.byref(text)), "sid_to_string")
    try:
        value = C.wstring_at(text)
        if len(value) > 256:
            raise ValueError("sid_string_budget")
        return value
    finally:
        if text.value:
            api.local_free(text)


def _token_data(api: RegistryApi, token: int, kind: int) -> Any:
    size = W.DWORD()
    first = api.get_token(token, kind, None, 0, C.byref(size))
    if first or C.get_last_error() != 122 or not 0 < size.value <= MAX_SD_BYTES:
        raise ValueError("token_size_invalid")
    data = C.create_string_buffer(size.value)
    _require(api.get_token(token, kind, data, size.value, C.byref(size)), "token_query")
    minimum = C.sizeof(C.c_void_p) if kind in (1, 25, 31) else 4
    if not minimum <= size.value <= len(data):
        raise ValueError("token_result_size_invalid")
    return data


def _token_sid(api: RegistryApi, data: Any) -> str:
    start = C.addressof(data)
    return _sid(api, C.c_void_p.from_buffer(data).value, start, start + len(data))


def _token_metadata(api: RegistryApi, token: int) -> dict[str, object]:
    user = _token_data(api, token, 1)
    package = _token_data(api, token, 31)
    integrity = _token_data(api, token, 25)
    kind = W.DWORD.from_buffer(_token_data(api, token, 8)).value
    return {
        "user_sid": _token_sid(api, user),
        "package_sid": _token_sid(api, package),
        "integrity_sid": _token_sid(api, integrity),
        "is_appcontainer": W.DWORD.from_buffer(_token_data(api, token, 29)).value,
        "capability_count": W.DWORD.from_buffer(_token_data(api, token, 30)).value,
        "token_type": kind,
        "impersonation_level": W.DWORD.from_buffer(_token_data(api, token, 9)).value if kind == 2 else None,
    }


def _verify_token(metadata: dict[str, object], package_sid: str, user_sid: str,
                  token_type: int) -> None:
    if (metadata["package_sid"] != package_sid or metadata["user_sid"] != user_sid
            or metadata["is_appcontainer"] != 1 or metadata["capability_count"] != 0
            or metadata["integrity_sid"] != "S-1-16-4096" or metadata["token_type"] != token_type
            or (token_type == 2 and metadata["impersonation_level"] != 2)):
        raise ValueError("registry_subject_token_mismatch")


class AclSize(C.Structure):
    _fields_ = [("ace_count", W.DWORD), ("bytes_in_use", W.DWORD), ("bytes_free", W.DWORD)]


def _security_metadata(api: RegistryApi, key: Any, package_sid: str,
                       user_sid: str) -> dict[str, object]:
    owner, dacl, descriptor = C.c_void_p(), C.c_void_p(), C.c_void_p()
    status = api.get_security(key, 4, 0x5, C.byref(owner), None, C.byref(dacl), None,
                              C.byref(descriptor))  # SE_REGISTRY_KEY; OWNER | DACL.
    if status:
        raise OSError(int(status), "registry_security_query")
    try:
        if not descriptor.value:
            raise ValueError("registry_sd_missing")
        length = int(api.sd_length(descriptor))
        if not 20 <= length <= MAX_SD_BYTES:
            raise ValueError("registry_sd_budget")
        start, end = descriptor.value, descriptor.value + length
        control, revision = C.c_uint16(), W.DWORD()
        _require(api.sd_control(descriptor, C.byref(control), C.byref(revision)), "sd_control")
        if not control.value & 0x8000:
            raise ValueError("registry_sd_not_self_relative")
        owner_sid = _sid(api, owner.value, start, end)
        aces: list[dict[str, object]] = []
        dacl_hash: str | None = None
        if dacl.value:
            if not start <= dacl.value <= end - 8:
                raise ValueError("registry_acl_outside_sd")
            acl_bytes = C.c_uint16.from_address(dacl.value + 2).value
            if acl_bytes < 8 or dacl.value + acl_bytes > end:
                raise ValueError("registry_acl_length_outside_sd")
            size = AclSize()
            _require(api.acl_info(dacl, C.byref(size), C.sizeof(size), 2), "acl_size")
            if not 8 <= size.bytes_in_use <= acl_bytes or size.ace_count > 512:
                raise ValueError("registry_acl_budget")
            dacl_hash = sha256(C.string_at(dacl, size.bytes_in_use)).hexdigest()
            for index in range(size.ace_count):
                ace = C.c_void_p()
                _require(api.get_ace(dacl, index, C.byref(ace)), "get_ace")
                address = int(ace.value or 0)
                if not dacl.value <= address <= dacl.value + size.bytes_in_use - 4:
                    raise ValueError("ace_address_invalid")
                ace_type, flags = C.c_ubyte.from_address(address).value, C.c_ubyte.from_address(address + 1).value
                ace_size = C.c_uint16.from_address(address + 2).value
                if ace_size < 4 or address + ace_size > dacl.value + size.bytes_in_use:
                    raise ValueError("ace_size_invalid")
                entry: dict[str, object] = {"type": ace_type, "flags": flags, "bytes": ace_size}
                # Parse only ordinary allow/deny ACEs. Unknown ACE forms stay
                # explicit coverage gaps, rather than guessing their SID layout.
                if ace_type in (0, 1) and ace_size >= 16:
                    entry.update(mask=C.c_uint32.from_address(address + 4).value,
                                 sid=_sid(api, address + 8, address, address + ace_size), sid_parsed=True)
                else:
                    entry.update(sid_parsed=False, sha256=sha256(C.string_at(address, ace_size)).hexdigest())
                aces.append(entry)
        return {"owner_sid": owner_sid, "owner_matches_subject_user": owner_sid == user_sid,
                "sd_bytes": length, "control": control.value, "revision": revision.value,
                "dacl_present": bool(control.value & 0x4), "dacl_null": not bool(dacl.value),
                "dacl_protected": bool(control.value & 0x1000), "dacl_sha256": dacl_hash,
                "aces": aces, "exact_package_sid_aces": [ace for ace in aces if ace.get("sid") == package_sid],
                "unparsed_ace_count": sum(ace.get("sid_parsed") is not True for ace in aces)}
    finally:
        if descriptor.value:
            api.local_free(descriptor)


def _emit_helper(report: dict[str, object]) -> None:
    payload = json.dumps(report, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    if len(payload) + 1 > MAX_HELPER_BYTES:
        payload = b'{"status":"helper_output_budget","stage0_passed":false}'
    sys.stdout.buffer.write(payload + b"\n")
    sys.stdout.buffer.flush()


def _helper(token: int, package_sid: str, user_sid: str) -> int:
    api = RegistryApi()
    duplicate, thread, key = W.HANDLE(), W.HANDLE(), W.HKEY()
    impersonating = False
    report: dict[str, object] = {"stage0_passed": False, "status": "helper_failed",
        "requested_registry_access": 0x20000, "registry_values_accessed": False,
        "registry_names_enumerated": False, "registry_mutation_requested": False}
    code = 2
    try:
        primary = _token_metadata(api, token)
        report["primary_token"] = primary
        _verify_token(primary, package_sid, user_sid, 1)
        _require(api.duplicate_token(token, 0xC, None, 2, 2, C.byref(duplicate)), "duplicate_impersonation_token")
        _require(api.impersonate(duplicate), "impersonate")
        impersonating = True
        _require(api.open_thread_token(api.current_thread(), 0x8, True, C.byref(thread)), "open_effective_thread_token")
        effective = _token_metadata(api, int(thread.value or 0))
        report["effective_thread_token"] = effective
        _verify_token(effective, package_sid, user_sid, 2)
        hr = int(api.get_registry(0x20000, C.byref(key)))
        report["registry_location_hresult"] = hr & 0xFFFFFFFF
        if hr != 0 or not key.value:
            raise ValueError("registry_location_not_obtained")
        report["registry_security"] = _security_metadata(api, key, package_sid, user_sid)
        report["status"] = "readonly_metadata_collected_binding_unverified"
        code = 0
    except Exception as error:
        report.update(error_type=type(error).__name__, error=str(error)[:2048])
    finally:
        if impersonating:
            try:
                reverted = bool(api.revert())
            except BaseException:
                reverted = False
            if not reverted:
                report.update(status="fatal_revert_failed_outer_host_must_reap", revert_confirmed=False)
                try:
                    _emit_helper(report)
                finally:
                    # No profile/Job belongs to this helper. Exit closes its
                    # duplicate handles; the outer host remains cleanup owner.
                    os._exit(77)
            report["revert_confirmed"] = True
        else:
            report["revert_confirmed"] = "not_impersonated"
        close_errors: list[str] = []
        if key.value and api.reg_close(key) != 0:
            close_errors.append("registry_key")
        for label, handle in (("thread_token", thread), ("impersonation_token", duplicate)):
            if handle.value and not api.close(handle):
                close_errors.append(label)
        if not api.close(token):
            close_errors.append("inherited_primary_token")
        report["handle_close_errors"] = close_errors
        if close_errors:
            report["status"] = "helper_handle_close_failed"
            code = 2
    _emit_helper(report)
    return code


class HelperOwner:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.reaped = False

    def collect(self) -> tuple[bytes, bytes]:
        try:
            output = self.process.communicate(timeout=10)
            self.reaped = True
            return output
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate(timeout=3)
            self.reaped = True
            raise RuntimeError("registry_helper_timeout_confirmed_reaped")
        finally:
            if not self.reaped:
                try:
                    self.process.kill()
                except OSError:
                    pass
                try:
                    self.process.wait(timeout=3)
                    self.reaped = True
                except (OSError, subprocess.TimeoutExpired):
                    _UNCONFIRMED_OWNERS.append(self)

    def close(self) -> None:
        if not self.reaped:
            try:
                self.process.kill()
            except OSError:
                pass
            self.process.wait(timeout=3)
            self.reaped = True
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


def _run_helper(process: int, expected_sid: str, environment: dict[str, str],
                owners: list[HelperOwner]) -> dict[str, object]:
    api = RegistryApi()
    token, host_token = W.HANDLE(), W.HANDLE()
    close_errors: list[str] = []
    _require(api.open_process_token(process, 0xA, C.byref(token)), "open_subject_query_duplicate")
    try:
        _require(api.open_process_token(W.HANDLE(-1), 0x8, C.byref(host_token)), "open_host_query")
        host_data = _token_data(api, int(host_token.value or 0), 1)
        user_sid = _token_sid(api, host_data)
        subject = _token_metadata(api, int(token.value or 0))
        _verify_token(subject, expected_sid, user_sid, 1)
        os.set_handle_inheritable(int(token.value or 0), True)
        try:
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {"handle_list": [int(token.value or 0)]}
            child = subprocess.Popen([sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()),
                "--_registry-helper", str(token.value), expected_sid, user_sid],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                close_fds=True, startupinfo=startup, env=environment, cwd=ROOT,
                creationflags=subprocess.CREATE_NO_WINDOW)
            owner = HelperOwner(child)
            owners.append(owner)  # Register before output processing can fail.
        finally:
            os.set_handle_inheritable(int(token.value or 0), False)
        stdout, stderr = owner.collect()
        if len(stdout) > MAX_HELPER_BYTES or len(stderr) > 4096:
            raise ValueError("registry_helper_output_budget")
        result: object = json.loads(stdout)
        if not isinstance(result, dict):
            raise ValueError("registry_helper_output_shape")
        return {"outer_verified_subject": subject, "helper_exit_code": child.returncode,
                "helper_reap_confirmed": owner.reaped, "helper_report": result,
                "helper_stderr": stderr.decode("utf-8", "replace"),
                "outer_token_close_errors": close_errors,
                "helper_output_scope": "Trusted fixed helper emits at most 128KiB; communicate is not an untrusted worker stream defense."}
    finally:
        if host_token.value and not api.close(host_token):
            close_errors.append("host_query_token")
        if not api.close(token):
            close_errors.append("subject_query_duplicate_token")


class SuspendedRegistryApi(Api):
    """Resume is replaced, never delegated; outer _create_worker owns cleanup."""

    def __init__(self, callback: Callable[[int], None]) -> None:
        super().__init__()
        self.callback = callback
        self.process_handle = 0
        self.creation_flags: int | None = None
        self.resume_intercepted = False
        self._real_create = self.create_process
        self.create_process = self._capture_create
        self._real_open_token = self.open_token
        self.open_token = self._capture_open_token
        self.resume = self._read_before_resume

    def _capture_create(self, *arguments: Any) -> int:
        if not int(arguments[5]) & 0x4:
            raise ValueError("registry_diagnostic_requires_suspended")
        self.creation_flags = int(arguments[5])
        # Do not inspect PI or run Python post-processing after native creation:
        # the adapter must first register all successful-create handles.
        return int(self._real_create(*arguments))

    def _capture_open_token(self, process: Any, access: Any, token: Any) -> int:
        # _verify calls this only after process/thread/Job ownership registration.
        self.process_handle = int(process)
        return int(self._real_open_token(process, access, token))

    def _read_before_resume(self, handle: object) -> int:
        if not self.process_handle or self.resume_intercepted:
            raise LaunchRejected("registry_resume_hook_invalid_state")
        self.resume_intercepted = True
        self.callback(self.process_handle)
        raise LaunchRejected(STOP_STAGE)


def _reject_reparse(path: Path) -> None:
    for item in (path, *path.parents):
        try:
            metadata = item.lstat()
        except FileNotFoundError:
            continue
        if item.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError("registry_diagnostic_reparse_path")


def _new_report(path: Path) -> Path:
    _reject_reparse(path.absolute())
    target = path.resolve()
    allowed = (ROOT / "data/validation/v06").resolve()
    if (not target.is_relative_to(allowed) or target == allowed or target.exists()
            or target.suffix.lower() != ".json" or target.is_reserved()
            or any(":" in part for part in target.parts[1:])):
        raise ValueError("report_must_be_new_project_validation_file")
    return target


def _fingerprints() -> dict[str, str]:
    paths = ("scripts/diagnose_worker_registry.py", "src/deskpal/execution/runtime.py",
             "src/deskpal/execution/windows/_win32.py", "src/deskpal/execution/windows/launcher.py",
             "src/deskpal/execution/protocol.py", "src/deskpal/execution/__init__.py",
             "src/deskpal/execution/windows/__init__.py", "src/deskpal/__init__.py",
             "tests/fixtures/windows/native_probe_v2.c", "data/v06-stage0/native-fixture-v2/fixture.exe")
    return {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}


def _save(output: TextIO, report: dict[str, object]) -> None:
    payload = json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
    if len(payload.encode("ascii")) > 512 * 1024:
        raise ValueError("registry_report_budget")
    # The new report's exclusive creation handle stays owned by main throughout.
    # Never reopen its path, follow a substituted link, or overwrite an old run.
    output.seek(0)
    output.write(payload)
    output.truncate()
    output.flush()


def _comparison(runs: list[dict[str, object]]) -> dict[str, object]:
    observations: list[dict[str, object]] = []
    package_sids = {str(run["package_sid"]) for run in runs}
    for run in runs:
        helper = run.get("helper_report")
        if isinstance(helper, dict) and isinstance(helper.get("registry_security"), dict):
            security = helper["registry_security"]
            aces = security.get("aces", [])
            package_aces = [ace for ace in aces if isinstance(ace, dict)
                            and ace.get("sid") in package_sids] if isinstance(aces, list) else []
            observations.append({"label": run["label"], "package_sid": run["package_sid"],
                "owner_sid": security.get("owner_sid"), "dacl_sha256": security.get("dacl_sha256"),
                "control": security.get("control"), "all_test_profile_package_sid_aces": package_aces,
                "exact_package_sid_aces": security.get("exact_package_sid_aces"),
                "unparsed_ace_count": security.get("unparsed_ace_count")})
    a_hash = observations[0].get("dacl_sha256") if len(observations) == 3 else None
    b_hash = observations[1].get("dacl_sha256") if len(observations) == 3 else None
    same_a = (len(observations) == 3 and isinstance(a_hash, str)
              and a_hash == observations[2].get("dacl_sha256"))
    return {"requested_sequence": ["A", "B", "A"], "observations": observations,
            "a_dacl_hash_stable": same_a, "binding_verified": False,
            "b_dacl_differs_from_a": a_hash != b_hash
            if isinstance(a_hash, str) and isinstance(b_hash, str) else None,
            "distinct_test_package_sids": len(package_sids),
            "interpretation": "Review exact ACEs and verified tokens; hashes alone do not identify a root. No ACL changes authorized."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--experiment", action="store_true", help="Explicit native read-only registry experiment opt-in")
    args = parser.parse_args()
    path = _new_report(args.report)
    report: dict[str, object] = {"started_at": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(), "python_executable": sys.executable,
        "platform": platform.platform(),
        "stage0_passed": False, "binding_verified": False, "experiment_requested": args.experiment,
        "worker_code_executed": False, "native_resume_called": False, "cleanup": "not_required",
        "scope": "ordinary AC only; two fresh profiles; A/B/A suspended token and registry SD metadata",
        "registry_values_accessed": False, "registry_names_enumerated": False,
        "registry_mutation_requested": False, "fallback_allowed": False,
        "requested_child_environment_only": True,
        "coverage_gap": "Actual worker environment remapping/nested cache paths are unobserved while suspended; canonical input is unchanged.",
        "fixture_sha256": FIXTURE_SHA256, "profiles": [], "runs": []}
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_reparse(path)
    with path.open("x", encoding="ascii", newline="\n") as output:
        try:
            code = _experiment(output, report, args.experiment)
        except Exception as error:
            report.update(status="registry_diagnostic_blocked", error_type=type(error).__name__,
                          error=str(error)[:2048])
            _save(output, report)
            code = 2
    print(json.dumps({"report": str(path), "diagnostic_exit": code, "stage0_passed": False}))
    return code


def _experiment(output: TextIO, report: dict[str, object], experiment: bool) -> int:
    if not experiment:
        report["status"] = "not_executed_explicit_opt_in_required"
        _save(output, report)
        return 0
    if os.name != "nt" or C.sizeof(C.c_void_p) != 8:
        raise ValueError("Windows x64 only")
    _reject_reparse(FIXTURE)
    if sha256(FIXTURE.read_bytes()).hexdigest() != FIXTURE_SHA256:
        raise ValueError("registry_fixture_v2_fingerprint_mismatch")
    api = RegistryApi()
    windows = C.create_unicode_buffer(32768)
    length = int(api.windows_directory(windows, len(windows)))
    if not 0 < length < len(windows):
        raise ValueError("windows_directory_query_failed")
    report["source_sha256"] = _fingerprints()
    profiles: list[ProbeProfile] = []
    helper_owners: list[HelperOwner] = []
    pending: list[CleanupOwner] = []
    runs: list[dict[str, object]] = []
    code = 2
    try:
        for _ in ("A", "B"):
            profile = create_probe_profile("deskpal.v06." + uuid4().hex)
            profiles.append(profile)  # Outer owns before serialization/reporting.
            report["cleanup"] = "pending"
            report["profiles"] = [asdict(item) for item in profiles]
            _save(output, report)
            if len({item.sid for item in profiles}) != len(profiles):
                raise ValueError("registry_fresh_profile_sids_not_distinct")
            _reject_reparse(Path(profile.folder))
            destination = Path(profile.folder) / "native-fixture.exe"
            _reject_reparse(destination)
            with FIXTURE.open("rb") as source, destination.open("xb") as copied:
                shutil.copyfileobj(source, copied)
            if sha256(destination.read_bytes()).hexdigest() != FIXTURE_SHA256:
                raise ValueError("registry_copied_fixture_mismatch")
            temporary = Path(profile.folder) / "Temp"
            _reject_reparse(temporary)
            temporary.mkdir(exist_ok=True)
            _reject_reparse(temporary)
        report["runs"] = runs
        for label, profile in (("A", profiles[0]), ("B", profiles[1]), ("A", profiles[0])):
            folder = Path(profile.folder)
            environment = worker_environment(Path(windows.value), folder / "Temp", local_appdata=folder)
            entry: dict[str, object] = {"label": label, "package_sid": profile.sid,
                "requested_child_environment": environment, "worker_reap": "not_observed"}
            runs.append(entry)

            def observe(process: int) -> None:
                entry.update(_run_helper(process, profile.sid, environment, helper_owners))

            suspended = SuspendedRegistryApi(observe)
            try:
                spec = LaunchSpec(folder / "native-fixture.exe", (), folder, profile.sid, environment)
                with _CREATE_LOCK:
                    native, _ = _create_worker(spec, _api=suspended)
                # Resume stub must always raise. Retain any unexpected owner.
                pending.append(native)
                raise ValueError("registry_unexpected_launch_return")
            except LaunchRejected as error:
                if error.cleanup_owner is not None:
                    pending.append(error.cleanup_owner)
                entry.update(stopped_stage=error.stage, stage_winerror=error.winerror)
                if error.cleanup_owner is None:
                    entry["worker_reap"] = "adapter_cleanup_completed_no_pending_owner"
                else:
                    entry["worker_reap"] = "UNCONFIRMED"
                helper_report = entry.get("helper_report")
                helper_complete = (isinstance(helper_report, dict)
                    and helper_report.get("status") == "readonly_metadata_collected_binding_unverified"
                    and helper_report.get("revert_confirmed") is True
                    and isinstance(helper_report.get("registry_security"), dict))
                if (error.stage != STOP_STAGE or error.cleanup_owner is not None
                        or entry.get("helper_exit_code") != 0 or not helper_complete
                        or not suspended.resume_intercepted or entry.get("outer_token_close_errors")):
                    raise RuntimeError("registry_readonly_observation_failed") from error
            except BaseException:
                # _create_worker translates an unconfirmed reap to LaunchRejected
                # with a cleanup owner, handled above. This original exception
                # can arise before creation or after successful adapter cleanup.
                entry["worker_reap"] = "adapter_error_without_pending_owner"
                raise
            finally:
                entry.update(actual_creation_flags=suspended.creation_flags,
                             resume_intercepted=suspended.resume_intercepted, native_resume_called=False)
            _save(output, report)
        report["comparison"] = _comparison(runs)
        report["status"] = "readonly_aba_metadata_collected_binding_requires_review"
        code = 0
    except Exception as error:
        report.update(status="registry_diagnostic_blocked", error_type=type(error).__name__, error=str(error)[:2048])
    finally:
        for helper_owner in helper_owners:
            try:
                helper_owner.close()
            except BaseException:
                _UNCONFIRMED_OWNERS.append(helper_owner)
        workers_confirmed = True
        for worker_owner in pending:
            try:
                worker_owner.close()
            except BaseException:
                workers_confirmed = False
                _UNCONFIRMED_OWNERS.append(worker_owner)
        helpers_confirmed = all(helper_owner.reaped for helper_owner in helper_owners)
        report.update(all_helpers_reaped=helpers_confirmed, all_pending_workers_reaped=workers_confirmed)
        cleanup: list[dict[str, object]] = []
        if helpers_confirmed and workers_confirmed:
            for profile in reversed(profiles):
                try:
                    delete_probe_profile(profile)
                    cleanup.append({"name": profile.name, "status": "profile_deleted"})
                except Exception as error:
                    cleanup.append({"name": profile.name, "status": "failed_profile_may_remain", "error": str(error)[:2048]})
                    code = 2
            report["cleanup"] = cleanup
        else:
            report["cleanup"] = "withheld_unconfirmed_helper_or_worker_exit"
            code = 2
        report["source_sha256_end"] = _fingerprints()
        report["source_unchanged"] = report["source_sha256"] == report["source_sha256_end"]
        if not report["source_unchanged"]:
            report["status"] = "source_changed_observation_not_frozen"
            code = 2
        _save(output, report)
    return code


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--_registry-helper":
        raise SystemExit(_helper(int(sys.argv[2]), sys.argv[3], sys.argv[4]))
    raise SystemExit(main())
