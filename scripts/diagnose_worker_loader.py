"""One synthetic AppContainer/Job loader trace; NOT a product launch mode.

Requires explicit native-experiment approval. Uses ONLY the fixed-hash static-CRT
fixture in a NEW project profile, preserves all normal launcher restrictions,
and adds DEBUG_ONLY_THIS_PROCESS. The diagnostic console mode defaults to the
historical CREATE_NO_WINDOW control; the experimental adapter now defaults to
DETACHED_PROCESS. Explicit console selection replaces only those console bits
and cannot be combined with an environment contrast. No IFEO, loader snaps, installation or global
configuration. No ReadProcessMemory: debug strings are deliberately metadata
only, reducing scope to loader basenames and exception/exit codes.

--console-mode detached --no-debug is a separate one-bit contrast against the
detached debug trace: the same restricted launch runs without a debug connection.
It collects the fixed synthetic smoke frame, exit code and bounded log counts,
not loader events. It cannot be combined with a bootstrap environment contrast.

Win32 ABI and ownership sources (Windows SDK minwinbase.h / winnt.h / debugapi.h):
https://learn.microsoft.com/en-us/windows/win32/api/minwinbase/ns-minwinbase-debug_event
https://learn.microsoft.com/en-us/windows/win32/api/debugapi/nf-debugapi-waitfordebugeventex
https://learn.microsoft.com/en-us/windows/win32/api/debugapi/nf-debugapi-continuedebugevent
https://learn.microsoft.com/en-us/windows/win32/api/debugapi/nf-debugapi-debugactiveprocessstop

Only CREATE_PROCESS/LOAD_DLL hFile handles are caller-closed. Debug process and
thread handles are left to ContinueDebugEvent on their EXIT events, as specified
by Microsoft; these are distinct from the launcher's PROCESS_INFORMATION owner.
On early abort, continue the current event, detach, and then use normal Job reap.
Remaining debug-only handles on a detach path are retained until this one-purpose
helper exits; the report does not claim they received normal EXIT/Continue closure.
"""

from __future__ import annotations

import argparse
import ctypes as C
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import ntpath
import os
from pathlib import Path
import shutil
import sys
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deskpal.execution.protocol import FrameDecoder  # noqa: E402
from deskpal.execution.runtime import create_probe_profile, delete_probe_profile  # noqa: E402
from deskpal.execution.windows._win32 import (  # noqa: E402
    Api, NativeWorker, PROCESS_INFORMATION, _CREATE_LOCK, _create_worker,
)
from deskpal.execution.windows.launcher import (  # noqa: E402
    CleanupOwner, LaunchRejected, LaunchSpec, RunningWorker,
)
from diagnose_worker_startup import FIXTURE_SHA, GUID, native_context, set_caller_localappdata  # noqa: E402

DWORD = C.c_uint32
WORD = C.c_uint16
PTR = C.c_void_p
HANDLE = PTR
BOOL = C.c_int32
DBG_CONTINUE = 0x00010002
DBG_EXCEPTION_NOT_HANDLED = 0x80010001
FIXTURE_V2_SHA: str | None = "08423adb75615e3609e268409be844053bf2e14af1d4d250917006c0ba7ab4e1"


class EXCEPTION_RECORD(C.Structure):
    _fields_ = [("ExceptionCode", DWORD), ("ExceptionFlags", DWORD),
                ("ExceptionRecord", PTR), ("ExceptionAddress", PTR),
                ("NumberParameters", DWORD), ("ExceptionInformation", C.c_size_t * 15)]


class EXCEPTION_DEBUG_INFO(C.Structure):
    _fields_ = [("ExceptionRecord", EXCEPTION_RECORD), ("dwFirstChance", DWORD)]


class CREATE_THREAD_DEBUG_INFO(C.Structure):
    _fields_ = [("hThread", HANDLE), ("lpThreadLocalBase", PTR), ("lpStartAddress", PTR)]


class CREATE_PROCESS_DEBUG_INFO(C.Structure):
    _fields_ = [("hFile", HANDLE), ("hProcess", HANDLE), ("hThread", HANDLE),
                ("lpBaseOfImage", PTR), ("dwDebugInfoFileOffset", DWORD),
                ("nDebugInfoSize", DWORD), ("lpThreadLocalBase", PTR),
                ("lpStartAddress", PTR), ("lpImageName", PTR), ("fUnicode", WORD)]


class EXIT_DEBUG_INFO(C.Structure):
    _fields_ = [("dwExitCode", DWORD)]


class LOAD_DLL_DEBUG_INFO(C.Structure):
    _fields_ = [("hFile", HANDLE), ("lpBaseOfDll", PTR), ("dwDebugInfoFileOffset", DWORD),
                ("nDebugInfoSize", DWORD), ("lpImageName", PTR), ("fUnicode", WORD)]


class UNLOAD_DLL_DEBUG_INFO(C.Structure):
    _fields_ = [("lpBaseOfDll", PTR)]


class OUTPUT_DEBUG_STRING_INFO(C.Structure):
    _fields_ = [("lpDebugStringData", PTR), ("fUnicode", WORD), ("nDebugStringLength", WORD)]


class RIP_INFO(C.Structure):
    _fields_ = [("dwError", DWORD), ("dwType", DWORD)]


class DEBUG_UNION(C.Union):
    _fields_ = [("Exception", EXCEPTION_DEBUG_INFO), ("CreateThread", CREATE_THREAD_DEBUG_INFO),
                ("CreateProcessInfo", CREATE_PROCESS_DEBUG_INFO), ("ExitThread", EXIT_DEBUG_INFO),
                ("ExitProcess", EXIT_DEBUG_INFO), ("LoadDll", LOAD_DLL_DEBUG_INFO),
                ("UnloadDll", UNLOAD_DLL_DEBUG_INFO), ("DebugString", OUTPUT_DEBUG_STRING_INFO),
                ("RipInfo", RIP_INFO)]


class DEBUG_EVENT(C.Structure):
    _fields_ = [("dwDebugEventCode", DWORD), ("dwProcessId", DWORD),
                ("dwThreadId", DWORD), ("u", DEBUG_UNION)]


def _assert_x64_abi() -> None:
    if C.sizeof(PTR) != 8 or C.sizeof(EXCEPTION_RECORD) != 152 or C.sizeof(DEBUG_UNION) != 160:
        raise RuntimeError("debug_abi_not_supported_x64")
    if C.sizeof(DEBUG_EVENT) != 176 or DEBUG_EVENT.u.offset != 16 or C.sizeof(CREATE_PROCESS_DEBUG_INFO) != 72:
        raise RuntimeError("debug_event_abi_layout_mismatch")


def _bind(library: Any, name: str, args: list[Any], result: Any) -> Any:
    function = getattr(library, name)
    function.argtypes, function.restype = args, result
    return function


def _diagnostic_creation_flags(flags: int, console_mode: str) -> int:
    """Pure flag transformation; preserve every non-console launcher bit."""
    if console_mode not in ("no_window", "detached"):
        raise ValueError("loader_unknown_console_mode")
    if not flags & 0x4:
        raise LaunchRejected("loader_requires_suspended_create")
    console_bits = flags & (0x08000000 | 0x8)
    if console_bits not in (0x08000000, 0x8) or flags & 0x10:
        raise LaunchRejected("loader_requires_single_console_baseline")
    flags |= 0x2  # DEBUG_ONLY_THIS_PROCESS, not DEBUG_PROCESS.
    flags = (flags & ~(0x08000000 | 0x8)) | (0x8 if console_mode == "detached" else 0x08000000)
    return flags


class DebugApi(Api):
    """Add debug/optional console contrast; retain every security attribute."""

    def __init__(self, *, diagnostic_environment: dict[str, str] | None = None,
                 console_mode: str = "no_window") -> None:
        # Reject unsupported/combined contrasts before loading any Win32 API.
        if console_mode not in ("no_window", "detached"):
            raise ValueError("loader_unknown_console_mode")
        if console_mode == "detached" and diagnostic_environment is not None:
            raise ValueError("loader_console_environment_contrasts_are_exclusive")
        super().__init__()
        self.console_mode = console_mode
        self.actual_creation_flags: int | None = None
        self._environment_buffer: Any = None
        if diagnostic_environment is not None:
            # Diagnostic-only replacement, assembled from base5 plus ONE
            # API-derived metadata key. Never pass through os.environ.
            allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA",
                       "SYSTEMDRIVE", "USERPROFILE", "APPDATA"}
            keys = set(diagnostic_environment)
            required = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA"}
            if (not required <= keys or not keys <= allowed or len(keys) != 6 or
                    any(not value or "\0" in value for value in diagnostic_environment.values())):
                raise ValueError("loader_requires_base5_plus_single_metadata_key")
            data = "\0".join(f"{key}={value}" for key, value in sorted(diagnostic_environment.items())) + "\0\0"
            if len(data) > 16000:
                raise ValueError("loader_diagnostic_environment_budget")
            self._environment_buffer = C.create_unicode_buffer(data)
        self._real_create = self.create_process
        self.create_process = self._debug_create
        self.pid: int | None = None
        kernel = C.WinDLL("kernel32", use_last_error=True)
        self.wait_debug = _bind(kernel, "WaitForDebugEventEx", [C.POINTER(DEBUG_EVENT), DWORD], BOOL)
        self.continue_debug = _bind(kernel, "ContinueDebugEvent", [DWORD, DWORD, DWORD], BOOL)
        self.detach_debug = _bind(kernel, "DebugActiveProcessStop", [DWORD], BOOL)
        self.kill_debuggee_on_exit = _bind(kernel, "DebugSetProcessKillOnExit", [BOOL], BOOL)
        self.final_path = _bind(kernel, "GetFinalPathNameByHandleW", [HANDLE, C.c_wchar_p, DWORD, DWORD], DWORD)

    def _debug_create(self, *arguments: Any) -> int:
        args = list(arguments)
        args[5] = _diagnostic_creation_flags(int(args[5]), self.console_mode)
        if self._environment_buffer is not None:
            if not int(args[5]) & 0x400:
                raise LaunchRejected("loader_requires_unicode_environment")
            args[6] = self._environment_buffer
        self.actual_creation_flags = int(args[5])
        result = int(self._real_create(*args))
        if result:
            info = C.cast(args[9], C.POINTER(PROCESS_INFORMATION)).contents
            self.pid = int(info.dwProcessId)
            # Official contract requires an established debug connection first.
            # The process is still suspended here; the default is already kill.
            if not self.kill_debuggee_on_exit(True):
                raise LaunchRejected("loader_set_kill_on_debugger_exit", C.get_last_error())
        return result


class NoDebugApi(Api):
    """Detached synthetic contrast only; never establish a debug connection."""

    def __init__(self) -> None:
        super().__init__()
        self.actual_creation_flags: int | None = None
        self.pid: int | None = None
        self._process_handle: int | None = None  # Borrowed from _create_worker's owner.
        self.strict_handle_policy: dict[str, object] | None = None
        self._real_create = self.create_process
        self.create_process = self._plain_create
        self._real_resume = self.resume
        self.resume = self._resume_with_snapshot

    def _plain_create(self, *arguments: Any) -> int:
        args = list(arguments)
        original_flags = int(args[5])
        if original_flags & 0x3:
            raise LaunchRejected("loader_no_debug_requires_plain_baseline")
        # Same detached transformation, with DEBUG_ONLY_THIS_PROCESS removed.
        args[5] = _diagnostic_creation_flags(original_flags, "detached") & ~0x2
        self.actual_creation_flags = int(args[5])
        result = int(self._real_create(*args))
        if result:
            info = C.cast(args[9], C.POINTER(PROCESS_INFORMATION)).contents
            self.pid = int(info.dwProcessId)
            self._process_handle = int(info.hProcess or 0)
        return result


    def _resume_with_snapshot(self, thread_handle: Any) -> int:
        """Read policy only after launcher ownership/verification, while suspended.

        PROCESS_MITIGATION_STRICT_HANDLE_CHECK_POLICY is one DWORD union:
        https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-process_mitigation_strict_handle_check_policy
        No policy is changed; a failed query remains unavailable, never flags=0.
        """
        if not self._process_handle:
            raise LaunchRejected("loader_no_debug_missing_process_handle")
        policy: dict[str, object] = {"available": False, "policy": 3,
                                    "phase": "suspended_before_resume"}
        self.strict_handle_policy = policy
        flags = DWORD()
        if self.get_mitigation(self._process_handle, 3, C.byref(flags), C.sizeof(flags)):
            policy.update(available=True, raw_flags=int(flags.value),
                          raise_exception_on_invalid_handle_reference=bool(flags.value & 1),
                          handle_exceptions_permanently_enabled=bool(flags.value & 2),
                          reserved_flags=int(flags.value & ~3))
        else:
            policy["winerror"] = C.get_last_error()
        return int(self._real_resume(thread_handle))


def _fixed_native_smoke_frames(frames: tuple[dict[str, object], ...]) -> list[dict[str, object]]:
    """Record only the known synthetic schema, never arbitrary worker strings."""
    if len(frames) > 1:
        raise LaunchRejected("loader_native_smoke_frame_count")
    keys = {"probe", "excluded_handle_valid", "excluded_handle_error",
            "operation_error", "allocation_count"}
    for frame in frames:
        if (not keys <= set(frame) <= keys | {"excluded_handle_exception"} or frame["probe"] != "native" or
                type(frame["excluded_handle_valid"]) is not bool):
            raise LaunchRejected("loader_native_smoke_frame_schema")
        for key in ("excluded_handle_error", "operation_error", "allocation_count"):
            value = frame[key]
            if type(value) is not int or not 0 <= value <= 0xffffffff:
                raise LaunchRejected("loader_native_smoke_frame_schema")
        if "excluded_handle_exception" in frame:
            exception = frame["excluded_handle_exception"]
            if type(exception) is not int or exception not in (0, 0xC0000008):
                raise LaunchRejected("loader_native_smoke_frame_schema")
    return list(frames)


class LoaderTrace:
    """Single creating/debugging thread, at most 128 events / 5s capture."""

    def __init__(self, api: DebugApi, report: dict[str, object]) -> None:
        self.api, self.report = api, report
        self.pending: tuple[int, int, int] | None = None
        self.pending_is_exit = False
        self.file_handles: set[int] = set()
        self.attached = False
        self.detached = False
        self.exit_seen = False
        self.initial_breakpoint_handled = False
        self.event_count = 0
        self.event_bytes = 0
        self.events: list[dict[str, object]] = []
        report["debug_events"] = self.events
        self.decoder = FrameDecoder()
        self.frame_count = 0
        self.log_bytes = [0, 0]

    def _append(self, event: dict[str, object]) -> None:
        size = len(json.dumps(event, ensure_ascii=True, allow_nan=False).encode("ascii"))
        if self.event_bytes + size > 128 * 1024:
            raise LaunchRejected("loader_event_report_budget")
        self.event_bytes += size
        self.events.append(event)

    def _basename(self, handle: int) -> str | None:
        # Metadata of the event's own DLL/image handle, never read its contents.
        buffer = C.create_unicode_buffer(1024)
        length = self.api.final_path(handle, buffer, len(buffer), 0)
        if not 0 < length < len(buffer):
            return None
        name = ntpath.basename(buffer.value)
        encoded = name.encode("utf-8", "replace")
        return name if len(encoded) <= 1024 else None

    def _close_event_files(self) -> None:
        for handle in tuple(self.file_handles):
            if not self.api.close(handle):
                raise LaunchRejected("loader_close_event_file", C.get_last_error())
            self.file_handles.remove(handle)

    def _continue_pending(self) -> None:
        self._close_event_files()
        if self.pending is None:
            return
        pid, tid, status = self.pending
        if not self.api.continue_debug(pid, tid, status):
            raise LaunchRejected("loader_continue_event", C.get_last_error())
        self.pending = None
        if self.pending_is_exit:
            self.exit_seen, self.attached = True, False
        self.pending_is_exit = False

    def _receive(self, milliseconds: int) -> bool:
        event = DEBUG_EVENT()
        if not self.api.wait_debug(C.byref(event), milliseconds):
            error = C.get_last_error()
            if error in (121, 258):
                return False
            raise LaunchRejected("loader_wait_event", error)
        self.event_count += 1
        code = int(event.dwDebugEventCode)
        pid, tid = int(event.dwProcessId), int(event.dwThreadId)
        status = DBG_EXCEPTION_NOT_HANDLED if code == 1 else DBG_CONTINUE
        self.pending = (pid, tid, status)
        self.pending_is_exit = code == 5
        # Own event hFile immediately, before any validation/reporting may fail.
        file_handle = (int(event.u.CreateProcessInfo.hFile or 0) if code == 3 else
                       int(event.u.LoadDll.hFile or 0) if code == 6 else 0)
        if file_handle:
            self.file_handles.add(file_handle)
        if pid != self.api.pid:
            raise LaunchRejected("loader_unexpected_debuggee_pid")
        item: dict[str, object] = {"event": code, "sequence": self.event_count}
        if code in (3, 6):
            item["module_basename"] = self._basename(file_handle) if file_handle else None
        if code == 1:
            exception = int(event.u.Exception.ExceptionRecord.ExceptionCode)
            first = bool(event.u.Exception.dwFirstChance)
            item.update(exception_code=exception, first_chance=first)
            if exception == 0x80000003 and first and not self.initial_breakpoint_handled:
                self.initial_breakpoint_handled = True
                self.pending = (pid, tid, DBG_CONTINUE)
                item["handled_as_initial_breakpoint"] = True
        elif code in (4, 5):
            item["exit_code"] = int(event.u.ExitProcess.dwExitCode if code == 5 else event.u.ExitThread.dwExitCode)
        elif code == 8:
            item.update(debug_string_capture="omitted_no_remote_memory_read",
                        reported_characters=int(event.u.DebugString.nDebugStringLength),
                        unicode=bool(event.u.DebugString.fUnicode))
        elif code == 9:
            item.update(rip_error=int(event.u.RipInfo.dwError), rip_type=int(event.u.RipInfo.dwType))
        self._append(item)
        self._continue_pending()
        return True

    def capture(self, owner: NativeWorker) -> None:
        self.attached = True
        deadline = monotonic() + 5.0
        while not self.exit_seen:
            if self.event_count >= 128:
                raise LaunchRejected("loader_event_count_budget")
            if monotonic() >= deadline:
                raise LaunchRejected("loader_capture_deadline")
            self._receive(20)
            chunks, _ = owner.read_available()
            self.frame_count += len(self.decoder.feed(chunks[0]))
            for index in range(2):
                self.log_bytes[index] += len(chunks[index + 1])
                if self.log_bytes[index] > 64 * 1024:
                    raise LaunchRejected("loader_log_budget")
        self.report.update(debug_exit_event_seen=True, frame_count=self.frame_count,
                           stdout_bytes=self.log_bytes[0], stderr_bytes=self.log_bytes[1])

    def release_debugger(self) -> None:
        """Release a held event before detach/reap; never wait on a paused exit."""
        self._continue_pending()
        if self.api.pid is not None and not self.exit_seen and not self.detached:
            # Covers launch validation failing after process creation but before
            # capture() received its first CREATE_PROCESS event.
            if not self.api.detach_debug(self.api.pid):
                raise LaunchRejected("loader_detach_debuggee", C.get_last_error())
            self.detached = True
            self.report["debug_detached_early"] = True
            self.report["debug_process_thread_handles"] = "held_until_helper_exit_after_detach"
        elif self.exit_seen:
            self.report["debug_process_thread_handles"] = "system_closed_on_exit_continue"
        elif self.api.pid is None:
            self.report["debug_process_thread_handles"] = "process_not_created"
        self.attached = False


class LoaderCleanupOwner:
    """Keep debug attachment and original Job owner together if cleanup fails."""

    def __init__(self, trace: LoaderTrace, native: CleanupOwner | None) -> None:
        self.trace, self.native = trace, native

    def close(self) -> None:
        try:
            self.trace.release_debugger()
            if self.native is not None:
                self.native.close()
        except BaseException as error:
            raise LaunchRejected("loader_cleanup_unconfirmed", cleanup_owner=self) from error


_UNCONFIRMED_OWNERS: list[CleanupOwner] = []


def _new_report_path(value: Path) -> Path:
    original = value.absolute()
    for item in (original, *original.parents):
        if item.is_symlink() or (item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("loader_report_reparse_ancestor")
    result = value.resolve()
    if not result.is_relative_to((ROOT / "data/validation/v06").resolve()) or result.exists():
        raise ValueError("loader_report_requires_new_project_validation_file")
    return result


def desktop_context() -> dict[str, object]:
    """Name metadata of THIS helper's current objects; no ACL/state changes.

    GetProcessWindowStation/GetThreadDesktop return borrowed system handles;
    Microsoft explicitly says not to close them. UOI_NAME is 2, size in bytes.
    """
    user = C.WinDLL("user32", use_last_error=True)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    station = _bind(user, "GetProcessWindowStation", [], HANDLE)
    thread_id = _bind(kernel, "GetCurrentThreadId", [], DWORD)
    desktop = _bind(user, "GetThreadDesktop", [DWORD], HANDLE)
    info = _bind(user, "GetUserObjectInformationW", [HANDLE, C.c_int, PTR, DWORD, C.POINTER(DWORD)], BOOL)
    result: dict[str, object] = {}
    for name, handle in (("window_station", station()), ("desktop", desktop(thread_id()))):
        if not handle:
            result[name] = {"available": False, "winerror": C.get_last_error()}
            continue
        needed = DWORD()
        info(handle, 2, None, 0, C.byref(needed))
        if not 0 < needed.value <= 1024:
            result[name] = {"available": False, "winerror": C.get_last_error(), "size_budget": 1024}
            continue
        buffer = C.create_string_buffer(needed.value)
        if not info(handle, 2, buffer, len(buffer), C.byref(needed)):
            result[name] = {"available": False, "winerror": C.get_last_error()}
            continue
        value = C.wstring_at(buffer, len(buffer) // C.sizeof(C.c_wchar)).rstrip("\0")
        if len(value.encode("utf-8", "replace")) > 1024:
            result[name] = {"available": False, "size_budget": 1024}
        else:
            result[name] = {"available": True, "name": value}
    return result


def bootstrap_metadata(choice: str | None, windows: str) -> tuple[dict[str, str], dict[str, object]]:
    """Derive one diagnostic path value; never read files or inherit host env.

    GUIDs follow Microsoft KNOWNFOLDERID (Knownfolders.h):
    https://learn.microsoft.com/en-us/windows/win32/shell/knownfolderid
    SHGetKnownFolderPath flags=0 queries metadata, never KF_FLAG_CREATE.
    """
    if choice is None:
        return {}, {"choice": None, "environment_key": None, "source": "base5_unchanged"}
    if choice == "system_drive":
        value = ntpath.splitdrive(windows)[0]
        if len(value) != 2 or value[1] != ":" or not value[0].isascii() or not value[0].isalpha():
            raise ValueError("loader_windows_directory_has_no_local_drive")
        return {"SYSTEMDRIVE": value}, {"choice": choice, "environment_key": "SYSTEMDRIVE",
            "source": "GetWindowsDirectoryW_volume_component", "value_characters": len(value)}
    folders = {
        "user_profile": ("USERPROFILE", "5e6c858f-0e22-4760-9afe-ea3317b67173", "FOLDERID_Profile"),
        "roaming_appdata": ("APPDATA", "3eb685db-65f9-4cf6-a03a-e3ef65729f3d", "FOLDERID_RoamingAppData"),
    }
    if choice not in folders:
        raise ValueError("loader_unknown_bootstrap_key")
    key, identifier, name = folders[choice]
    shell = C.WinDLL("shell32", use_last_error=True)
    ole = C.WinDLL("ole32", use_last_error=True)
    query = _bind(shell, "SHGetKnownFolderPath", [C.POINTER(GUID), DWORD, HANDLE, C.POINTER(PTR)], C.c_long)
    free = _bind(ole, "CoTaskMemFree", [PTR], None)
    guid = GUID.from_buffer_copy(UUID(identifier).bytes_le)
    folder = PTR()
    hr = query(C.byref(guid), 0, None, C.byref(folder))
    try:
        if hr != 0 or not folder.value:
            raise RuntimeError(f"loader_known_folder_query_failed_{hr & 0xffffffff:08x}")
        value = C.wstring_at(folder)
        if not value or "\0" in value or len(value) > 8192:
            raise ValueError("loader_known_folder_metadata_budget")
        return {key: value}, {"choice": choice, "environment_key": key,
            "source": "SHGetKnownFolderPath(" + name + ")_metadata_only", "value_characters": len(value)}
    finally:
        if folder.value:
            free(folder)


def source_fingerprints() -> dict[str, str]:
    """Explicit code dependencies only: no configuration, database or glob."""
    files = (
        "src/deskpal/__init__.py", "src/deskpal/execution/__init__.py",
        "src/deskpal/execution/protocol.py", "src/deskpal/execution/runtime.py",
        "src/deskpal/execution/windows/__init__.py", "src/deskpal/execution/windows/_win32.py",
        "src/deskpal/execution/windows/launcher.py", "scripts/diagnose_worker_startup.py",
        "scripts/diagnose_worker_loader.py", "scripts/probe_windows_launch.py",
        "tests/fixtures/windows/native_probe_v2.c",
    )
    return {relative: sha256((ROOT / relative).read_bytes()).hexdigest() for relative in files}


def _fixture_source(version: int) -> tuple[Path, str]:
    """Only fixed project artifacts and reviewed hashes; no caller path/hash."""
    if type(version) is not int or version not in (1, 2):
        raise ValueError("loader_unknown_fixture_version")
    if version == 1:
        return ROOT / "data/v06-stage0/native-fixture-v1/fixture.exe", FIXTURE_SHA
    if FIXTURE_V2_SHA is None:
        raise ValueError("loader_fixture_v2_fingerprint_not_reviewed")
    if len(FIXTURE_V2_SHA) != 64 or any(char not in "0123456789abcdef" for char in FIXTURE_V2_SHA):
        raise ValueError("loader_fixture_v2_fingerprint_invalid")
    return ROOT / "data/v06-stage0/native-fixture-v2/fixture.exe", FIXTURE_V2_SHA


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--fixture-version", type=int, choices=(1, 2), default=1,
                        help="Fixed project fixture only; v2 requires a reviewed pinned executable hash")
    contrast = parser.add_mutually_exclusive_group()
    contrast.add_argument("--bootstrap-key", choices=("system_drive", "user_profile", "roaming_appdata"),
                          help="ONE API-derived bootstrap metadata key; diagnostic only, no product whitelist changes")
    contrast.add_argument("--console-mode", choices=("no_window", "detached"), default=None,
                          help="Diagnostic console flag only; detached replaces CREATE_NO_WINDOW with DETACHED_PROCESS")
    parser.add_argument("--no-debug", action="store_true",
                        help="Only with detached and no bootstrap key: omit the debug connection, collect fixed native smoke output")
    args = parser.parse_args()
    console_mode = args.console_mode or "no_window"
    if args.no_debug and (console_mode != "detached" or args.bootstrap_key is not None):
        parser.error("--no-debug requires --console-mode detached and no --bootstrap-key")
    if os.name != "nt":
        parser.error("Windows x64 only")
    source, fixture_sha = _fixture_source(args.fixture_version)
    _assert_x64_abi()
    report_path = _new_report_path(args.report)
    for item in (source, *source.parents):
        if item.is_symlink() or (item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("loader_fixture_reparse_ancestor")
    if sha256(source.read_bytes()).hexdigest() != fixture_sha:
        raise ValueError("loader_fixture_fingerprint_mismatch")
    windows, local, elevated = native_context()
    extra_environment, extra_metadata = bootstrap_metadata(args.bootstrap_key, windows)
    set_caller_localappdata(local)  # This single-purpose process only.
    version = sys.getwindowsversion()
    report: dict[str, object] = {"stage0_passed": False,
        "started_at": datetime.now(timezone.utc).isoformat(), "host_token_elevated": elevated,
        "fixture_version": args.fixture_version, "fixture_sha256": fixture_sha,
        "script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "debug_flag": None if args.no_debug else "DEBUG_ONLY_THIS_PROCESS",
        "debug_enabled": not args.no_debug, "console_mode": console_mode,
        "actual_creation_flags": None, "capture_limit_seconds": 5,
        "max_debug_events": 0 if args.no_debug else 128,
        "remote_memory_read": False, "cleanup": "not_required"}
    report.update(python_version=sys.version, python_executable=sys.executable,
                  windows_version={"major": version.major, "minor": version.minor,
                                   "build": version.build, "platform": version.platform,
                                   "service_pack": version.service_pack},
                  source_sha256=source_fingerprints(), bootstrap_metadata=extra_metadata)
    report["host_desktop_context"] = desktop_context()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    profile = create_probe_profile("deskpal.v06." + uuid4().hex)
    cleanup: LoaderCleanupOwner | None = None
    safe_to_delete = True
    code = 0
    try:
        report["profile"] = asdict(profile)
        report["cleanup"] = "pending"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        executable = Path(profile.folder) / "native-fixture.exe"
        shutil.copyfile(source, executable)
        if sha256(executable.read_bytes()).hexdigest() != fixture_sha:
            raise ValueError("loader_copied_fixture_mismatch")
        temporary = Path(profile.folder) / "Temp"
        temporary.mkdir(exist_ok=True)
        environment = {"SYSTEMROOT": windows, "WINDIR": windows, "TEMP": str(temporary),
                       "TMP": str(temporary), "LOCALAPPDATA": profile.folder}
        spec = LaunchSpec(executable, ("{ipc_write_handle}", "{excluded_handle}", "smoke"),
                          Path(profile.folder), profile.sid, environment)
        actual_environment = environment | extra_environment
        report["actual_child_environment_keys"] = sorted(actual_environment)
        if args.no_debug:
            plain_api = NoDebugApi()
            plain_owner: CleanupOwner | None = None
            report.update(debug_events_available=False,
                          debug_process_thread_handles="not_applicable_no_debug_connection")
            try:
                with _CREATE_LOCK:
                    native, identity = _create_worker(spec, _api=plain_api)
                plain_owner = native  # Own immediately, even if wrapper setup fails.
                worker = RunningWorker(native, identity)
                plain_owner = worker
                report["identity"] = asdict(identity)
                result = worker.collect(5)
                report.update(exit_code=result.exit_code, frame_count=len(result.frames),
                              stdout_bytes=len(result.stdout), stderr_bytes=len(result.stderr),
                              elapsed_seconds=result.elapsed_seconds)
                report["native_frames"] = _fixed_native_smoke_frames(result.frames)
                report["status"] = "synthetic_no_debug_capture_collected_requires_review"
            except LaunchRejected as error:
                if plain_owner is None:
                    plain_owner = error.cleanup_owner
                report.update(status="loader_diagnostic_failed", rejected=error.stage, winerror=error.winerror)
                code = 2
            finally:
                report["actual_creation_flags"] = plain_api.actual_creation_flags
                report["strict_handle_policy"] = plain_api.strict_handle_policy
                try:
                    if plain_owner is not None:
                        plain_owner.close()
                    report["worker_reap"] = "confirmed" if plain_api.pid is not None else "not_created"
                except BaseException:
                    if plain_owner is not None:
                        _UNCONFIRMED_OWNERS.append(plain_owner)
                    safe_to_delete = False
                    report["worker_reap"] = "UNCONFIRMED_owner_retained"
                    code = 2
        else:
            api = DebugApi(diagnostic_environment=actual_environment if extra_environment else None,
                           console_mode=console_mode)
            trace = LoaderTrace(api, report)
            cleanup = LoaderCleanupOwner(trace, None)
            try:
                with _CREATE_LOCK:
                    native, identity = _create_worker(spec, _api=api)
                cleanup.native = native
                report["identity"] = asdict(identity)
                trace.capture(native)
                report["status"] = "synthetic_loader_trace_collected_requires_review"
            except LaunchRejected as error:
                if cleanup.native is None:
                    cleanup.native = error.cleanup_owner
                report.update(status="loader_diagnostic_failed", rejected=error.stage, winerror=error.winerror)
                code = 2
            finally:
                report["actual_creation_flags"] = api.actual_creation_flags
                # Even if _create_worker failed before returning its native owner,
                # its cleanup_owner remains composed with this debug attachment.
                if cleanup is not None:
                    try:
                        cleanup.close()
                        report["worker_reap"] = "confirmed" if api.pid is not None else "not_created"
                    except LaunchRejected:
                        _UNCONFIRMED_OWNERS.append(cleanup)
                        safe_to_delete = False
                        report["worker_reap"] = "UNCONFIRMED_owner_retained"
                        code = 2
    except Exception as error:
        report.update(status="loader_diagnostic_failed", error_type=type(error).__name__, error=str(error)[:1024])
        code = 2
    finally:
        if safe_to_delete:
            try:
                delete_probe_profile(profile)
                report["cleanup"] = "profile_deleted"
            except Exception as error:
                report.update(cleanup="failed_profile_may_remain", cleanup_error=str(error)[:1024])
                code = 2
        else:
            report["cleanup"] = "withheld_unconfirmed_worker_or_debugger"
        report["source_sha256_end"] = source_fingerprints()
        report["source_unchanged"] = report["source_sha256_end"] == report["source_sha256"]
        if not report["source_unchanged"]:
            report["status"] = "source_changed_trace_not_frozen"
            code = 2
        text = json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
        if len(text.encode("ascii")) > 256 * 1024:
            raise RuntimeError("loader_total_report_budget_exceeded")
        report_path.write_text(text, encoding="ascii")
    print(json.dumps({"report": str(report_path), "diagnostic_exit": code, "cleanup": report["cleanup"]}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
