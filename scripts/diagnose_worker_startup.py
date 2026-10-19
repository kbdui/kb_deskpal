"""Explicit synthetic caller/child environment contrast; NOT a sandbox backend.

Run with OS experiment permission. Only a fresh Deskpal profile is changed, and
the known static-CRT fixture is the only executable copied into it. This short-
lived process changes its own environment, never user/system environment values.
Default writable profile permissions are an intentional startup control only.
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
import shutil
import sys
from time import monotonic
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deskpal.execution.runtime import create_probe_profile, delete_probe_profile  # noqa: E402
from deskpal.execution.windows import LaunchRejected, LaunchSpec, WindowsLauncher  # noqa: E402
from deskpal.execution.windows.launcher import CleanupOwner  # noqa: E402

FIXTURE_SHA = "4d92fe675dd1476eeb04ae7f2b948516aa695229fb827af6f5267f09161f836a"


class GUID(C.Structure):
    _fields_ = [("data1", C.c_uint32), ("data2", C.c_uint16),
               ("data3", C.c_uint16), ("data4", C.c_ubyte * 8)]


def native_context() -> tuple[str, str, bool]:
    kernel = C.WinDLL("kernel32", use_last_error=True)
    shell = C.WinDLL("shell32", use_last_error=True)
    ole = C.WinDLL("ole32", use_last_error=True)
    advapi = C.WinDLL("advapi32", use_last_error=True)
    kernel.GetWindowsDirectoryW.argtypes = [W.LPWSTR, W.UINT]
    kernel.GetWindowsDirectoryW.restype = W.UINT
    shell.SHGetKnownFolderPath.argtypes = [C.POINTER(GUID), W.DWORD, W.HANDLE, C.POINTER(C.c_void_p)]
    shell.SHGetKnownFolderPath.restype = C.c_long
    ole.CoTaskMemFree.argtypes = [C.c_void_p]
    ole.CoTaskMemFree.restype = None
    advapi.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)]
    advapi.OpenProcessToken.restype = W.BOOL
    advapi.GetTokenInformation.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
    advapi.GetTokenInformation.restype = W.BOOL
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.CloseHandle.restype = W.BOOL
    buffer = C.create_unicode_buffer(32768)
    if not 0 < kernel.GetWindowsDirectoryW(buffer, len(buffer)) < len(buffer):
        raise RuntimeError("windows_directory_query_failed")
    guid = GUID.from_buffer_copy(UUID("f1b32785-6fba-4fcf-9d55-7b8e7f157091").bytes_le)
    folder = C.c_void_p()
    hr = shell.SHGetKnownFolderPath(C.byref(guid), 0, None, C.byref(folder))
    try:
        if hr != 0 or not folder.value:
            raise RuntimeError("known_folder_query_failed")
        local = C.wstring_at(folder)
    finally:
        if folder.value:
            ole.CoTaskMemFree(folder)
    token, elevated, returned = W.HANDLE(), W.DWORD(), W.DWORD()
    if not advapi.OpenProcessToken(W.HANDLE(-1), 8, C.byref(token)):
        raise RuntimeError("host_token_query_failed")
    try:
        if not advapi.GetTokenInformation(token, 20, C.byref(elevated), C.sizeof(elevated), C.byref(returned)):
            raise RuntimeError("host_elevation_query_failed")
    finally:
        kernel.CloseHandle(token)
    return buffer.value, local, bool(elevated.value)


def set_caller_localappdata(value: str | None) -> None:
    function = C.WinDLL("kernel32", use_last_error=True).SetEnvironmentVariableW
    function.argtypes = [W.LPCWSTR, W.LPCWSTR]
    function.restype = W.BOOL
    if value is None:
        os.environ.pop("LOCALAPPDATA", None)
    else:
        os.environ["LOCALAPPDATA"] = value
    if not function("LOCALAPPDATA", value):
        raise RuntimeError("caller_environment_setup_failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--compare-child-base", action="store_true",
                        help="Also explicitly test the known-folder base as the child bootstrap value")
    args = parser.parse_args()
    report_path = args.report.resolve()
    allowed = (ROOT / "data/validation/v06").resolve()
    if (not allowed.is_relative_to(ROOT.resolve()) or not report_path.is_relative_to(allowed)
            or report_path.exists()):
        parser.error("report must be a NEW project validation file")
    source = ROOT / "data/v06-stage0/native-fixture-v1/fixture.exe"
    for original in (source, args.report.absolute()):
        if any(path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
               for path in (original, *original.parents)):
            raise RuntimeError("diagnostic_path_contains_reparse_point")
    if sha256(source.read_bytes()).hexdigest() != FIXTURE_SHA:
        raise RuntimeError("fixture_fingerprint_mismatch")
    windows, local, elevated = native_context()
    report: dict[str, object] = {"started_at": datetime.now(timezone.utc).isoformat(),
        "stage0_passed": False, "host_token_elevated": elevated, "fixture_sha256": FIXTURE_SHA,
        "diagnostic_script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "fresh default-writable profile startup contrast only; no private files/cloud",
        "cleanup": "not_required", "runs": []}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    set_caller_localappdata(local)
    profile = create_probe_profile("deskpal.v06." + uuid4().hex)
    safe_to_delete = True
    result_code = 0
    pending_owner: CleanupOwner | None = None
    try:
        report["profile"] = asdict(profile)
        report["cleanup"] = "pending"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        executable = Path(profile.folder) / "native-fixture.exe"
        shutil.copyfile(source, executable)
        if sha256(executable.read_bytes()).hexdigest() != FIXTURE_SHA:
            raise RuntimeError("copied_fixture_fingerprint_mismatch")
        temporary = Path(profile.folder) / "Temp"
        temporary.mkdir(exist_ok=True)
        runs: list[dict[str, object]] = []
        report["runs"] = runs
        cases = [(False, "absent"), (True, "absent"), (False, "profile_ac"), (True, "profile_ac")]
        if args.compare_child_base:
            cases.extend(((False, "known_folder_base"), (True, "known_folder_base")))
        cases.append((False, "absent"))
        for caller_present, child_source in cases:
            set_caller_localappdata(local if caller_present else None)
            environment = {"SYSTEMROOT": windows, "WINDIR": windows,
                           "TEMP": str(temporary), "TMP": str(temporary)}
            if child_source != "absent":
                environment["LOCALAPPDATA"] = local if child_source == "known_folder_base" else profile.folder
            entry: dict[str, object] = {"caller_localappdata_present": caller_present,
                                       "child_localappdata_present": child_source != "absent",
                                       "child_localappdata_source": child_source}
            runs.append(entry)
            started = monotonic()
            try:
                spec = LaunchSpec(executable, ("{ipc_write_handle}", "{excluded_handle}", "smoke"),
                                  Path(profile.folder), profile.sid, environment)
                with WindowsLauncher().launch(spec) as worker:
                    result = worker.collect(5)
                entry.update(identity=asdict(result.identity), exit_code=result.exit_code,
                             frames=result.frames, stdout_bytes=len(result.stdout), stderr_bytes=len(result.stderr))
            except LaunchRejected as error:
                entry.update(rejected=error.stage, winerror=error.winerror)
                if error.cleanup_owner is not None:
                    pending_owner = error.cleanup_owner
                    safe_to_delete = False
                    report["cleanup"] = "withheld_unconfirmed_worker_exit"
                    result_code = 2
                    break
            finally:
                entry["elapsed_seconds"] = monotonic() - started
        if result_code == 0:
            report["status"] = "contrast_completed_requires_review"
    except Exception as error:
        report.update(error_type=type(error).__name__, error=str(error), status="diagnostic_failed")
        result_code = 2
    finally:
        if pending_owner is not None:
            try:
                pending_owner.close()
                safe_to_delete = True
                report["worker_cleanup_retry"] = "confirmed_reaped"
            except Exception as error:
                report["worker_cleanup_retry"] = "UNCONFIRMED"
                report["worker_cleanup_error"] = str(error)
                result_code = 2
        if safe_to_delete:
            try:
                set_caller_localappdata(local)
                delete_probe_profile(profile)
                report["cleanup"] = "profile_deleted"
            except Exception as error:
                report.update(cleanup="failed", cleanup_error=str(error))
                result_code = 2
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), "cleanup": report["cleanup"], "diagnostic_exit": result_code}))
    return result_code


if __name__ == "__main__":
    raise SystemExit(main())
