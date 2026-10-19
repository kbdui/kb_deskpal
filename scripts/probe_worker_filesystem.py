"""Synthetic stage-0 runtime/profile write probe (explicit experiment opt-in).

--experiment creates a fresh Deskpal AppContainer profile and changes DACLs only
on that new profile and the new project-owned runtime. Run that option only via
the approved native-security experiment command. No real secrets are read.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deskpal.execution.runtime import (  # noqa: E402
    apply_readonly_dacl, create_probe_profile, delete_probe_profile,
    profile_package_root, stage_runtime, verify_manifest, worker_environment,
)


WORKER_SOURCE = r'''
import ctypes
from ctypes import wintypes
import json
import os
import struct
import sys
import winreg

def file_probe(directory, label, marker):
    path = os.path.join(directory, marker + ".canary")
    out = {"label": label, "directory": directory}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
    except OSError as exc:
        out.update(created=False, winerror=getattr(exc, "winerror", None), errno=exc.errno)
    else:
        try:
            out.update(created=True, bytes_written=os.write(fd, b"SYNTH06!"))
        finally:
            os.close(fd)
        try:
            os.unlink(path)
            out["cleaned"] = True
        except OSError as exc:
            out.update(cleaned=False, cleanup_winerror=getattr(exc, "winerror", None))
    # Access-check only. Do not change security descriptors or owner.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    for name, access in (("write_dac", 0x40000), ("write_owner", 0x80000)):
        handle = kernel.CreateFileW(directory, access, 7, None, 3, 0x02000000, None)
        allowed = handle != ctypes.c_void_p(-1).value
        out[name] = allowed
        out[name + "_winerror"] = 0 if allowed else ctypes.get_last_error()
        if allowed:
            kernel.CloseHandle(handle)
    return out

def registry_probe(marker):
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    userenv.GetAppContainerRegistryLocation.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HKEY)]
    userenv.GetAppContainerRegistryLocation.restype = ctypes.c_long
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.RegCloseKey.argtypes = [wintypes.HKEY]
    advapi.RegCloseKey.restype = ctypes.c_long
    out = {}
    for label, mask in (("set_value", 0x2), ("create_sub_key", 0x4),
                        ("write_dac", 0x40000), ("write_owner", 0x80000)):
        key = wintypes.HKEY()
        hr = userenv.GetAppContainerRegistryLocation(mask, ctypes.byref(key))
        out[label + "_hresult"] = hr & 0xffffffff
        if hr != 0:
            continue
        try:
            if label == "create_sub_key":
                try:
                    child = winreg.CreateKeyEx(key.value, marker, 0, winreg.KEY_SET_VALUE)
                    try:
                        winreg.SetValueEx(child, "synthetic", 0, winreg.REG_BINARY, b"SYNTH06!")
                        out["created"] = True
                    finally:
                        child.Close()
                    try:
                        winreg.DeleteKey(key.value, marker)
                        out["cleaned"] = True
                    except OSError as exc:
                        out.update(cleaned=False, cleanup_winerror=getattr(exc, "winerror", None))
                except OSError as exc:
                    out.update(created=False, winerror=getattr(exc, "winerror", None))
        finally:
            advapi.RegCloseKey(key)
    return out

case = json.loads(sys.argv[1])
results = [] if case.get("startup_only") else [file_probe(path, label, case["marker"]) for label, path in case["directories"]]
report = {"kind": "filesystem_probe", "python": list(sys.version_info[:3]),
    "prefix": sys.prefix, "path": sys.path, "isolated": sys.flags.isolated,
    "no_site": sys.flags.no_site, "dont_write_bytecode": sys.dont_write_bytecode,
    "site_loaded": "site" in sys.modules,
    "environment_keys": sorted(os.environ),
    "environment_temp": {key: os.environ.get(key) for key in ("TEMP", "TMP", "LOCALAPPDATA")},
    "files": results, "registry": {} if case.get("startup_only") else registry_probe(case["marker"])}
if len(sys.argv) > 3:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetHandleInformation.restype = wintypes.BOOL
    flags = wintypes.DWORD()
    try:
        ok = kernel.GetHandleInformation(int(sys.argv[3]), ctypes.byref(flags))
    except OSError as exc:
        # Strict handle checks may surface a native exception through ctypes.
        # Preserve the observation without treating arbitrary OSError as denial.
        report["handle_inheritance_canary"] = {"opened": None, "raised": True,
            "status": "unresolved_exception", "error_type": type(exc).__name__,
            "error": str(exc)[:4096], "errno": exc.errno,
            "error_args_repr": repr(exc.args)[:4096],
            "winerror": getattr(exc, "winerror", None),
            "last_error": ctypes.get_last_error(),
            "proof_scope": "numeric_handle_only_requires_independent_review"}
    else:
        report["handle_inheritance_canary"] = {"opened": bool(ok), "raised": False,
            "status": "observed_api_return", "winerror": 0 if ok else ctypes.get_last_error(),
            "proof_scope": "numeric_handle_only_requires_independent_review"}
payload = json.dumps(report, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
if len(sys.argv) > 2:
    import msvcrt
    fd = msvcrt.open_osfhandle(int(sys.argv[2]), os.O_WRONLY | os.O_BINARY)
    try:
        frame = struct.pack("!I", len(payload)) + payload
        while frame:
            count = os.write(fd, frame)
            if count <= 0:
                raise RuntimeError("pipe closed")
            frame = frame[count:]
    finally:
        os.close(fd)
else:
    print(payload.decode("utf-8"))
'''


def _windows_directory() -> Path:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetWindowsDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.GetWindowsDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetWindowsDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise OSError("Windows directory unavailable")
    return Path(buffer.value)


def _owner_sid(windows: Path) -> str:
    result = subprocess.run([str(windows / "System32" / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
                            capture_output=True, check=True, timeout=10)
    matches = re.findall(rb"S-1-5-21-(?:[0-9]+-){3}[0-9]+", result.stdout)
    if len(matches) != 1:
        raise ValueError("Probe requires an explicit local/domain user SID")
    return str(matches[0], "ascii")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True, dest="python_executable")
    parser.add_argument("--root", type=Path, required=True, help="NEW directory under data/validation/v06")
    parser.add_argument("--experiment", action="store_true", help="Explicit native profile/ACL experiment opt-in")
    parser.add_argument("--native-fixture", type=Path,
                        help="Explicit V2 static-CRT fixture path; staged and hashed for native/Python comparison")
    parser.add_argument("--startup-only", action="store_true", help="Only startup and inheritance; no file/registry canary writes")
    parser.add_argument("--appcontainer-kind", choices=("ordinary", "lpac", "both"), default="both",
                        help="Explicit experiment scope; excluded kinds are untested, never a fallback")
    args = parser.parse_args()
    experiment_root = args.root.resolve()
    allowed = (ROOT / "data" / "validation" / "v06").resolve()
    if not experiment_root.is_relative_to(allowed) or experiment_root == allowed or experiment_root.exists():
        parser.error("root must be a NEW descendant of this project's data/validation/v06")
    experiment_root.mkdir(parents=True, exist_ok=False)
    report_path = experiment_root / "filesystem-report.json"
    report: dict[str, object] = {"started_at": datetime.now(timezone.utc).isoformat(),
        "stage0_passed": False, "scope": "synthetic eight-byte canaries; no real credentials/configuration",
        "experiment_requested": args.experiment, "runs": [], "cleanup": "not_required",
        "startup_only": args.startup_only, "appcontainer_kind_requested": args.appcontainer_kind,
        "appcontainer_kinds_selected": [kind for kind in ("ordinary", "lpac")
                                       if args.appcontainer_kind in (kind, "both")],
        "appcontainer_kinds_unselected": [kind for kind in ("ordinary", "lpac")
                                         if args.appcontainer_kind not in (kind, "both")],
        "fallback_allowed": False,
        "native_fixture_source": str(args.native_fixture.resolve()) if args.native_fixture is not None else None,
        "handle_canary_policy": "numeric_only; ctypes_exceptions_unresolved_not_denial_proof"}

    def save() -> None:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    try:
        manifest = stage_runtime(args.python_executable, experiment_root / "runtime", worker_source=WORKER_SOURCE,
                                 native_fixture=args.native_fixture)
        verify_manifest(manifest)
        report["manifest"] = asdict(manifest)
        if not args.experiment:
            report["status"] = "staged_only_no_execution_or_acl_proof"
            save()
            return 0
        from deskpal.execution.windows import LaunchSpec, WindowsLauncher

        windows = _windows_directory()
        owner = _owner_sid(windows)
        profile = create_probe_profile("deskpal.v06." + uuid4().hex)
        profile_safe_to_delete = True
        try:
            # Ownership protection begins immediately after successful create,
            # before reporting/serialization can fail.
            report["profile"] = asdict(profile)
            report["cleanup"] = "pending"
            save()
            folder = Path(profile.folder)
            package_root = profile_package_root(profile)
            report["profile_package_root"] = str(package_root)
            # Only create known synthetic paths INSIDE this newly created profile.
            profile_children = (package_root, folder, folder / "Temp",
                                package_root / "LocalState", package_root / "LocalCache",
                                package_root / "RoamingState", package_root / "TempState",
                                folder / "INetCache", folder / "INetCookies")
            for directory in profile_children:
                directory.mkdir(parents=True, exist_ok=True)
            runtime = Path(manifest.root)
            directories = [("runtime", str(runtime))] + [
                ("profile_" + str(index), str(path)) for index, path in enumerate(profile_children)]
            case = json.dumps({"marker": "DeskpalV06Probe_" + uuid4().hex, "directories": directories,
                               "startup_only": args.startup_only},
                              ensure_ascii=True, separators=(",", ":"))
            # folder is the native-query-bound path of this fresh profile.
            environment = worker_environment(windows, folder / "Temp", local_appdata=folder)
            report["canonical_environment_keys"] = sorted(environment)
            worker_args = ("-I", "-S", "-B", "-X", "utf8", str(runtime / "worker.py"), case)
            runs: list[dict[str, object]] = []
            report["runs"] = runs
            # Explicit unsandboxed positive control ONLY. Never a launch fallback.
            control = subprocess.run([manifest.executable, *worker_args], cwd=runtime,
                env=environment, capture_output=True, timeout=15)
            control_body: object = json.loads(control.stdout) if control.returncode == 0 else None
            runs.append({"mode": "explicit_host_positive_control", "exit_code": control.returncode,
                         "report": control_body, "stderr": control.stderr.decode("utf-8", "replace")[:4096]})
            save()
            if control.returncode:
                report["status"] = "blocked_staged_runtime_startup"
                return 2
            # Runtime has no package-write rights in either comparison.
            apply_readonly_dacl(manifest, owner_sid=owner, package_sid=profile.sid)
            launcher = WindowsLauncher()
            for profile_readonly in ((False,) if args.startup_only else (False, True)):
                if profile_readonly:
                    apply_readonly_dacl(profile, owner_sid=owner, package_sid=profile.sid)
                for low_privilege in (False, True):
                    kind = "lpac" if low_privilege else "ordinary"
                    if args.appcontainer_kind not in (kind, "both"):
                        continue
                    programs: list[tuple[str, Path, tuple[str, ...]]] = [
                        ("python", Path(manifest.executable), worker_args + ("{ipc_write_handle}", "{excluded_handle}"))]
                    if args.native_fixture is not None:
                        programs.insert(0, ("native", runtime / "native-fixture.exe",
                                            ("{ipc_write_handle}", "{excluded_handle}", "smoke")))
                    for program, executable, invocation_args in programs:
                        label = (("readonly_profile" if profile_readonly else "default_profile") +
                                 ("_lpac" if low_privilege else "_ac") +
                                 "_explicit_localappdata5_" + program)
                        try:
                            spec = LaunchSpec(executable, invocation_args, runtime, profile.sid,
                                              dict(environment), low_privilege=low_privilege)
                            with launcher.launch(spec) as worker:
                                result = worker.collect(15)
                            runs.append({"mode": label,
                                "application_success": result.exit_code == 0 and len(result.frames) == 1,
                                "result": asdict(result) | {
                                "stdout": result.stdout.decode("utf-8", "replace"),
                                "stderr": result.stderr.decode("utf-8", "replace")}})
                        except Exception as exc:
                            # Report a refused experiment as blocked; never ordinary fallback.
                            failed: dict[str, object] = {"mode": label, "error_type": type(exc).__name__, "error": str(exc)[:4096]}
                            runs.append(failed)
                            cleanup_owner = getattr(exc, "cleanup_owner", None)
                            if cleanup_owner is not None:
                                try:
                                    cleanup_owner.close()
                                    failed["cleanup_retry"] = "confirmed_reaped"
                                except Exception as cleanup_error:
                                    profile_safe_to_delete = False
                                    failed["cleanup_retry"] = "UNCONFIRMED"
                                    failed["cleanup_error"] = str(cleanup_error)[:4096]
                                    report["status"] = "blocked_worker_reap_unconfirmed_no_profile_reuse_or_delete"
                                    return 2
                        save()
            launch_errors = any("error_type" in run or ("result" in run and not run.get("application_success"))
                                for run in runs)
            report["status"] = ("blocked_launch_requires_investigation" if launch_errors else
                                "experiment_complete_requires_namespace_coverage_review")
            report["stage0_passed"] = False
            # Finite canary success/failure cannot enumerate every writable OS resource.
            return 2 if launch_errors else 0
        finally:
            if profile_safe_to_delete:
                try:
                    delete_probe_profile(profile)
                    report["cleanup"] = "profile_deleted"
                except Exception as exc:
                    report["cleanup"] = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)[:4096]}
                    save()
                    raise RuntimeError("Dedicated profile cleanup failed; profile may remain") from exc
            else:
                report["cleanup"] = "withheld_unconfirmed_worker_exit"
            save()
    except Exception as exc:
        report["status"] = "blocked"
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)[:4096]
        save()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
