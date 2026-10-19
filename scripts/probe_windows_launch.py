"""Synthetic stage-zero launch probe; profile diagnosis is explicitly opt-in.

Build: python scripts/probe_windows_launch.py --build-native-fixture data/v06/native
Run only after runtime/ACL preparation and OS experiment approval, for example:
  --executable ABSOLUTE_EXE --cwd ABSOLUTE_DIRECTORY --sid S-1-15-2-... --mode smoke

The native fixture never reads user files or contacts a network. Its output is
evidence about one launch, not acceptance of the overall execution boundary.
"""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import cast
from uuid import UUID, uuid4

from deskpal.execution.windows import LaunchRejected, LaunchSpec, WindowsLauncher


NATIVE_FIXTURE_SOURCE = r'''
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

static int emit(HANDLE pipe, const char *body) {
    DWORD size = (DWORD)strlen(body), written = 0;
    unsigned char header[4] = {(unsigned char)(size >> 24), (unsigned char)(size >> 16),
                               (unsigned char)(size >> 8), (unsigned char)size};
    if (!WriteFile(pipe, header, 4, &written, NULL) || written != 4) return 91;
    if (!WriteFile(pipe, body, size, &written, NULL) || written != size) return 92;
    return 0;
}

int wmain(int argc, wchar_t **argv) {
    HANDLE pipe, excluded;
    DWORD flags = 0, error, denied = 0, allocation_count = 0;
    BOOL inherited;
    char body[1024];
    if (argc != 4) return 90;
    pipe = (HANDLE)(uintptr_t)_wcstoui64(argv[1], NULL, 10);
    excluded = (HANDLE)(uintptr_t)_wcstoui64(argv[2], NULL, 10);
    SetLastError(0);
    inherited = GetHandleInformation(excluded, &flags);
    error = GetLastError();
    if (wcscmp(argv[3], L"spin") == 0) {
        volatile DWORD counter = 0;
        for (;;) ++counter;
    }
    if (wcscmp(argv[3], L"flood") == 0) {
        char data[4096] = {0};
        DWORD written;
        for (;;) if (!WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), data, sizeof(data),
                                 &written, NULL)) return 93;
    }
    if (wcscmp(argv[3], L"invalid-frame") == 0) {
        DWORD written;
        unsigned char data[4] = {255,255,255,255};
        return WriteFile(pipe, data, 4, &written, NULL) ? 0 : 94;
    }
    if (wcscmp(argv[3], L"memory") == 0) {
        while (allocation_count < 1024) {
            if (!VirtualAlloc(NULL, 16 * 1024 * 1024, MEM_RESERVE | MEM_COMMIT,
                              PAGE_READWRITE)) { denied = GetLastError(); break; }
            ++allocation_count;
        }
    }
    if (wcscmp(argv[3], L"child") == 0) {
        wchar_t application[MAX_PATH], command[MAX_PATH + 4];
        STARTUPINFOW startup = {0};
        PROCESS_INFORMATION child = {0};
        startup.cb = sizeof(startup);
        if (!GetModuleFileNameW(NULL, application, MAX_PATH)) return 95;
        if (swprintf_s(command, MAX_PATH + 4, L"\"%ls\"", application) < 0) return 96;
        if (CreateProcessW(application, command, NULL, NULL, FALSE, CREATE_SUSPENDED,
                           NULL, NULL, &startup, &child)) {
            TerminateProcess(child.hProcess, 97);
            WaitForSingleObject(child.hProcess, 2000);
            CloseHandle(child.hThread); CloseHandle(child.hProcess);
            denied = 0;
        } else denied = GetLastError();
    }
    if (sprintf_s(body, sizeof(body),
        "{\"probe\":\"native\",\"excluded_handle_valid\":%s,\"excluded_handle_error\":%lu,"
        "\"operation_error\":%lu,\"allocation_count\":%lu}",
        inherited ? "true" : "false", error, denied, allocation_count) < 0) return 98;
    return emit(pipe, body);
}
'''


def build_native_fixture(destination: Path, *, compiler: Path, sdk_root: Path,
                         sdk_version: str, fixture_version: int = 1) -> dict[str, object]:
    """Build a static-CRT synthetic fixture in a new workspace directory only."""
    if type(fixture_version) is not int or fixture_version not in (1, 2, 3):
        raise ValueError("Unknown native fixture version")
    original = destination.absolute()
    if any(parent.is_symlink() or (parent.exists() and
           getattr(parent.lstat(), "st_file_attributes", 0) & 0x400) for parent in (original, *original.parents)):
        raise ValueError("Native fixture ancestors cannot be reparse points")
    root = destination.resolve()
    workspace = Path(__file__).resolve().parents[1]
    if not root.is_relative_to(workspace) or root.exists():
        raise ValueError("Native fixture requires a NEW directory inside this workspace")
    repository_source: Path | None = None
    repository_source_bytes: bytes | None = None
    if fixture_version in (2, 3):
        repository_source = workspace / ("tests/fixtures/windows/native_probe_v2.c" if fixture_version == 2
                                         else "tests/fixtures/windows/native_memory_probe_v3.c")
        if any(parent.is_symlink() or (parent.exists() and
               getattr(parent.lstat(), "st_file_attributes", 0) & 0x400)
               for parent in (repository_source, *repository_source.parents)):
            raise ValueError("Native fixture source ancestors cannot be reparse points")
        repository_source_bytes = repository_source.read_bytes()
    version3_inputs: dict[str, str] = {}
    if fixture_version == 3:
        # Explicit provenance samples, not a claim of complete toolchain closure.
        selected = (compiler, sdk_root / "Include" / sdk_version / "um/windows.h",
                    sdk_root / "Lib" / sdk_version / "um/x64/kernel32.lib")
        for item in selected:
            if any(parent.is_symlink() or (parent.exists() and
                   getattr(parent.lstat(), "st_file_attributes", 0) & 0x400)
                   for parent in (item.absolute(), *item.absolute().parents)):
                raise ValueError("V3 compiler/SDK inputs cannot traverse reparse points")
            version3_inputs[str(item)] = hashlib.sha256(item.read_bytes()).hexdigest()
    root.mkdir(parents=True)
    source = root / "fixture.c"
    executable = root / "fixture.exe"
    if repository_source_bytes is None:
        source.write_text(NATIVE_FIXTURE_SOURCE, encoding="utf-8")
    else:
        source.write_bytes(repository_source_bytes)
    msvc = compiler.parents[3]
    includes = [msvc / "include", *(sdk_root / "Include" / sdk_version / name
                                    for name in ("um", "shared", "ucrt"))]
    libraries = [msvc / "lib/x64", *(sdk_root / "Lib" / sdk_version / name / "x64"
                                    for name in ("um", "ucrt"))]
    command = [str(compiler), "/nologo", "/W4", "/WX", "/O2", "/MT",
               f"/Fe{executable}", f"/Fo{root / 'fixture.obj'}", f"/Fd{root / 'fixture.pdb'}",
               *(f"/I{path}" for path in includes), str(source), "/link",
               *(f"/LIBPATH:{path}" for path in libraries), "kernel32.lib"]
    # This environment belongs to the trusted compiler; it is never passed to a worker.
    compile_env = {"PATH": str(compiler.parent), "SystemRoot": os.environ["SystemRoot"],
                   "TEMP": str(root), "TMP": str(root)}
    result = subprocess.run(command, cwd=root, env=compile_env, capture_output=True,
                            timeout=60, check=False)
    build_log = root / "build.log"
    build_log.write_bytes(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"Native fixture build failed; exit={result.returncode}; see {root / 'build.log'}")
    if fixture_version == 3:
        if any(hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest
               for name, digest in version3_inputs.items()):
            raise RuntimeError("V3 compiler/SDK input changed during build; no frozen receipt")
        if (repository_source is None or source.read_bytes() != repository_source_bytes
                or repository_source.read_bytes() != repository_source_bytes):
            raise RuntimeError("V3 source changed during build; no frozen receipt")
    executable_sha = hashlib.sha256(executable.read_bytes()).hexdigest()
    receipt: dict[str, object] = {"fixture_version": fixture_version,
        "executable": str(executable), "sha256": executable_sha, "executable_sha256": executable_sha,
        "source": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "repository_source": str(repository_source) if repository_source is not None else None,
        "repository_source_sha256": (hashlib.sha256(repository_source_bytes).hexdigest()
                                     if repository_source_bytes is not None else None),
        "compiler": str(compiler), "sdk_root": str(sdk_root), "sdk_version": sdk_version,
        "build_log": str(build_log)}
    if fixture_version == 3:
        receipt.update(build_command=command, compiler_sha256=version3_inputs[str(compiler)],
                       selected_toolchain_inputs_sha256=version3_inputs,
                       toolchain_hash_scope="compiler and selected SDK header/import library; not dependency closure",
                       repository_source_unchanged=True, build_source_unchanged=True,
                       allocation_ceiling_bytes=512 * 1024 * 1024,
                       supported_modes=["smoke", "bounded-memory"])
    (root / "build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def windows_directory() -> str:
    function = ctypes.WinDLL("kernel32", use_last_error=True).GetWindowsDirectoryW
    function.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    length = function(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise RuntimeError("GetWindowsDirectoryW failed")
    return buffer.value


def _known_local_appdata() -> str:
    """Resolve a directory location only; never enumerate/read user contents."""
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    initialize = ole.CoInitializeEx
    initialize.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    initialize.restype = ctypes.c_int32
    uninitialize = ole.CoUninitialize
    uninitialize.argtypes = []
    uninitialize.restype = None
    free = ole.CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    query = shell.SHGetKnownFolderPath
    query.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                      ctypes.POINTER(ctypes.c_void_p)]
    query.restype = ctypes.c_int32
    initialized = initialize(None, 0)
    if initialized < 0:
        raise RuntimeError(f"Diagnostic COM initialization failed HRESULT={initialized & 0xffffffff:08x}")
    pointer = ctypes.c_void_p()
    identifier = (ctypes.c_ubyte * 16).from_buffer_copy(
        UUID("F1B32785-6FBA-4FCF-9D55-7B8E7F157091").bytes_le)
    try:
        result = query(identifier, 0, None, ctypes.byref(pointer))
        if result < 0 or not pointer.value:
            raise RuntimeError(f"Known folder query failed HRESULT={result & 0xffffffff:08x}")
        return ctypes.wstring_at(pointer.value)
    finally:
        if pointer.value:
            free(pointer)
        uninitialize()


def _set_diagnostic_caller_localappdata(value: str | None) -> dict[str, object]:
    """Only for this standalone probe process, never the shared application."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    change = kernel.SetEnvironmentVariableW
    change.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    change.restype = ctypes.c_int32
    query = kernel.GetEnvironmentVariableW
    query.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    query.restype = ctypes.c_uint32
    if value is None:
        os.environ.pop("LOCALAPPDATA", None)
    else:
        os.environ["LOCALAPPDATA"] = value
    if not change("LOCALAPPDATA", value):
        raise RuntimeError(f"Caller environment setup failed winerror={ctypes.get_last_error()}")
    ctypes.set_last_error(0)
    length = int(query("LOCALAPPDATA", None, 0))
    return {"python_present": "LOCALAPPDATA" in os.environ,
            "native_present": bool(length), "native_required_chars": length,
            "native_winerror": ctypes.get_last_error()}


def _native_profile_diagnostic(source: Path, report_path: Path, *, caller_mode: str,
                               parameter_matrix: bool) -> int:
    """Standalone CLI-only synthetic experiment; requires OS approval to run.

    The profile intentionally retains its default permissions as an explicit
    startup control. It is not an accepted disk-write boundary or production
    runtime. Only the compiled fixture is copied; no existing user data is read.
    """
    from deskpal.execution.runtime import create_probe_profile, delete_probe_profile

    workspace = Path(__file__).resolve().parents[1]
    allowed = workspace / "data"
    original_paths = (source.absolute(), report_path.absolute())
    for original in original_paths:
        if any(path.is_symlink() or (path.exists() and
               getattr(path.stat(), "st_file_attributes", 0) & 0x400)
               for path in (original, *original.parents)):
            raise ValueError("Diagnostic inputs/output must not traverse reparse points")
    source, report_path = source.resolve(), report_path.resolve()
    if (not source.is_relative_to(allowed) or source.suffix.lower() != ".exe"
            or not source.is_file() or not report_path.is_relative_to(allowed / "validation/v06")
            or report_path.exists()):
        raise ValueError("Diagnostic requires a plain workspace fixture and NEW validation report")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    windows = windows_directory()
    known_folder = _known_local_appdata()
    report: dict[str, object] = {
        "started_at": datetime.now(timezone.utc).isoformat(), "stage0_passed": False,
        "purpose": "native startup diagnostic; default writable profile is an explicit control",
        "caller_localappdata_mode": caller_mode, "python": sys.version,
        "os_version": list(sys.getwindowsversion()), "fixture_source": str(source),
        "fixture_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "runs": [],
        "cleanup": "not_required", "scope": "No user files/keys/cloud; no extra ACL grants",
    }

    def save() -> None:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Provision/cleanup are outside the caller-env launch contrast. The setting
    # exists only in this short-lived diagnostic process and is never persisted.
    _set_diagnostic_caller_localappdata(known_folder)
    safe_to_delete = True
    failed = False
    exit_code = 2
    profile = create_probe_profile("deskpal.v06." + uuid4().hex)
    try:
        # Once creation succeeds, even DTO conversion or the first report write
        # must pass through the profile cleanup guard.
        report["profile"] = asdict(profile)
        report["cleanup"] = "pending"
        save()
        folder = Path(profile.folder)
        destination = folder / "native-fixture.exe"
        if destination.exists():
            raise RuntimeError("NEW profile unexpectedly contains fixture")
        shutil.copyfile(source, destination)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != report["fixture_sha256"]:
            raise RuntimeError("Copied fixture fingerprint mismatch")
        temporary = folder / "Temp"
        temporary.mkdir(exist_ok=True)
        report["caller_environment_observation"] = _set_diagnostic_caller_localappdata(
            None if caller_mode == "absent" else known_folder)
        runs: list[dict[str, object]] = []
        report["runs"] = runs
        environment = {"SYSTEMROOT": windows, "WINDIR": windows,
                       "TEMP": str(temporary), "TMP": str(temporary)}
        for profile_hint in (False, True):
            child_environment = dict(environment)
            if profile_hint:
                child_environment["LOCALAPPDATA"] = profile.folder
            spec = LaunchSpec(destination, ("{ipc_write_handle}", "{excluded_handle}", "smoke"),
                              folder, profile.sid, child_environment)
            entry: dict[str, object] = {"child_environment_keys": sorted(child_environment)}
            runs.append(entry)
            try:
                with WindowsLauncher().launch(spec) as worker:
                    result = worker.collect(5)
                entry.update({"identity": asdict(result.identity), "exit_code": result.exit_code,
                              "frames": result.frames, "stdout_bytes": len(result.stdout),
                              "stderr_bytes": len(result.stderr), "elapsed_seconds": result.elapsed_seconds})
                smoke_completed = result.exit_code == 0 and any(
                    frame.get("probe") == "native" for frame in result.frames)
                entry["startup_status"] = ("native_smoke_completed" if smoke_completed else
                                           "application_startup_failed")
                if not smoke_completed:
                    failed = True
                    entry["startup_failure"] = ("nonzero_exit" if result.exit_code != 0 else
                                                "missing_native_frame")
            except Exception as error:
                failed = True
                entry.update({"error_type": type(error).__name__, "error": str(error)})
                cleanup_owner = getattr(error, "cleanup_owner", None)
                if cleanup_owner is not None:
                    safe_to_delete = False
                    try:
                        cleanup_owner.close()
                        safe_to_delete = True
                        entry["cleanup_retry"] = "confirmed_reaped"
                    except Exception:
                        entry["cleanup_retry"] = "UNCONFIRMED"
                        report["status"] = "blocked_unconfirmed_worker_exit"
                        return 2
            save()
        if parameter_matrix:
            from deskpal.execution.windows.diagnostics import diagnose_suspended_creation

            spec = LaunchSpec(destination, ("{ipc_write_handle}", "{excluded_handle}", "smoke"),
                              folder, profile.sid, environment)
            try:
                report["suspended_parameter_matrix"] = diagnose_suspended_creation(spec)
            except LaunchRejected as error:
                if error.cleanup_owner is not None:
                    safe_to_delete = False
                    report["status"] = "blocked_unconfirmed_diagnostic_exit"
                    # Retain the live owner on the raised exception. The command
                    # must stop; profile deletion/reuse is explicitly withheld.
                    raise
                raise
        report["status"] = "startup_rejected" if failed else "startup_completed_requires_security_review"
        exit_code = 2 if failed else 0
    finally:
        if safe_to_delete:
            try:
                _set_diagnostic_caller_localappdata(known_folder)
            except Exception as error:
                # A caller-env restoration error must not skip deletion of the
                # already-owned synthetic profile.
                exit_code = 2
                report["caller_restore_error"] = {"error_type": type(error).__name__,
                                                   "error": str(error)}
            try:
                delete_probe_profile(profile)
                report["cleanup"] = "profile_deleted"
            except Exception as error:
                exit_code = 2
                report["status"] = "blocked_profile_cleanup_failed"
                report["cleanup"] = {"status": "failed", "error_type": type(error).__name__,
                                     "error": str(error)}
        else:
            exit_code = 2
            report["cleanup"] = "withheld_unconfirmed_worker_exit"
        save()
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-native-fixture", type=Path)
    parser.add_argument("--fixture-version", type=int, choices=(1, 2, 3), default=1,
                        help="Native fixture build source version; defaults to the preserved v1 source")
    parser.add_argument("--native-profile-diagnostic", type=Path, metavar="FIXTURE_EXE")
    parser.add_argument("--diagnostic-report", type=Path)
    parser.add_argument("--caller-localappdata", choices=("absent", "known-folder"))
    parser.add_argument("--suspended-parameter-matrix", action="store_true")
    parser.add_argument("--compiler", type=Path, default=Path(
        "E:/no_game/vsGenerateTool/BuildTools/VC/Tools/MSVC/14.44.35207/bin/Hostx64/x64/cl.exe"))
    parser.add_argument("--sdk-root", type=Path, default=Path("C:/Program Files (x86)/Windows Kits/10"))
    parser.add_argument("--sdk-version", default="10.0.26100.0")
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--sid")
    parser.add_argument("--low-privilege", action="store_true")
    parser.add_argument("--mode", choices=("smoke", "spin", "memory", "child", "flood", "invalid-frame"),
                        default="smoke")
    parser.add_argument("--timeout", type=float, default=5.0)
    options = parser.parse_args()
    if options.fixture_version == 3 and (options.build_native_fixture is None or options.native_profile_diagnostic is not None):
        parser.error("Fixture version 3 is build-only here; use a separately reviewed bounded-memory diagnostic")
    if options.native_profile_diagnostic:
        if options.diagnostic_report is None or options.caller_localappdata is None:
            parser.error("Native profile diagnostic needs --diagnostic-report and --caller-localappdata")
        return _native_profile_diagnostic(options.native_profile_diagnostic, options.diagnostic_report,
                                          caller_mode=options.caller_localappdata,
                                          parameter_matrix=options.suspended_parameter_matrix)
    if options.build_native_fixture:
        print(json.dumps(build_native_fixture(options.build_native_fixture, compiler=options.compiler,
                                              sdk_root=options.sdk_root, sdk_version=options.sdk_version,
                                              fixture_version=options.fixture_version),
                         sort_keys=True))
        return 0
    if options.executable is None or options.cwd is None or options.sid is None:
        parser.error("--executable, --cwd and --sid are required for a launch")
    windows = windows_directory()
    spec = LaunchSpec(options.executable, ("{ipc_write_handle}", "{excluded_handle}", options.mode),
                      options.cwd, cast(str, options.sid), {"SYSTEMROOT": windows, "WINDIR": windows},
                      low_privilege=options.low_privilege)
    try:
        with WindowsLauncher().launch(spec) as worker:
            result = worker.collect(options.timeout)
            print(json.dumps({"identity": asdict(result.identity), "exit_code": result.exit_code,
                              "frames": result.frames, "stdout_bytes": len(result.stdout),
                              "stderr_bytes": len(result.stderr), "elapsed_seconds": result.elapsed_seconds},
                             sort_keys=True))
    except LaunchRejected as error:
        print(json.dumps({"rejected": error.stage, "winerror": error.winerror,
                          "cleanup_unconfirmed": error.cleanup_owner is not None}, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
