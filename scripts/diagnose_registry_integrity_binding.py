"""Private, read-only same-HKEY export for the fresh V0.6 MIC diagnostic.

No profiles are created/deleted here. The driver supplies a registered worker
owner whose require_suspended_verified() rechecks its native launch evidence.
Importing this module loads no DLL and performs no experiment.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
from time import monotonic, sleep
from typing import Any, Literal, Mapping, Protocol, Sequence
from uuid import uuid4

READ_CONTROL = 0x20000
MAX_SD = 8192
MAX_TOKEN = 65536
MAX_STDOUT = 65536
MAX_STDERR = 4096
MAX_CALLS = 192
MAX_CLOSE_CALLS = 32
FIXED_VALUE = "synthetic"
_SEED = re.compile(r"seed_[0-9a-f]{32}\Z", re.ASCII)
_NONCE = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
_PROFILE = re.compile(r"deskpal\.v06\.[0-9a-f]{32}\Z", re.ASCII)
_UNCONFIRMED: list[object] = []


class VerifiedWorkerOwner(Protocol):
    def require_suspended_verified(self) -> int: ...


class ReadApi:
    """No registry write, enumeration, security mutation or privilege API."""

    def __init__(self) -> None:
        kernel = C.WinDLL("kernel32", use_last_error=True)
        advapi = C.WinDLL("advapi32", use_last_error=True)
        userenv = C.WinDLL("userenv", use_last_error=True)
        bindings: tuple[tuple[Any, str, str, list[Any], Any], ...] = (
            (kernel, "close", "CloseHandle", [W.HANDLE], W.BOOL),
            (kernel, "current_process", "GetCurrentProcess", [], W.HANDLE),
            (kernel, "current_thread", "GetCurrentThread", [], W.HANDLE),
            (kernel, "open_process", "OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            (kernel, "duplicate_handle", "DuplicateHandle", [W.HANDLE, W.HANDLE, W.HANDLE, C.POINTER(W.HANDLE), W.DWORD, W.BOOL, W.DWORD], W.BOOL),
            (kernel, "peek", "PeekNamedPipe", [W.HANDLE, C.c_void_p, W.DWORD, C.c_void_p, C.POINTER(W.DWORD), C.c_void_p], W.BOOL),
            (kernel, "create_job", "CreateJobObjectW", [C.c_void_p, W.LPCWSTR], W.HANDLE),
            (kernel, "set_job", "SetInformationJobObject", [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
            (kernel, "assign_job", "AssignProcessToJobObject", [W.HANDLE, W.HANDLE], W.BOOL),
            (advapi, "open_process_token", "OpenProcessToken", [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL),
            (advapi, "open_thread_token", "OpenThreadToken", [W.HANDLE, W.DWORD, W.BOOL, C.POINTER(W.HANDLE)], W.BOOL),
            (advapi, "get_token", "GetTokenInformation", [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
            (advapi, "duplicate_token", "DuplicateTokenEx", [W.HANDLE, W.DWORD, C.c_void_p, C.c_int, C.c_int, C.POINTER(W.HANDLE)], W.BOOL),
            (advapi, "impersonate", "ImpersonateLoggedOnUser", [W.HANDLE], W.BOOL),
            (advapi, "revert", "RevertToSelf", [], W.BOOL),
            (advapi, "open_key", "RegOpenKeyExW", [W.HKEY, W.LPCWSTR, W.DWORD, W.DWORD, C.POINTER(W.HKEY)], C.c_int32),
            (advapi, "reg_close", "RegCloseKey", [W.HKEY], C.c_int32),
            (advapi, "get_security", "RegGetKeySecurity", [W.HKEY, W.DWORD, C.c_void_p, C.POINTER(W.DWORD)], C.c_int32),
            (advapi, "query_value", "RegQueryValueExW", [W.HKEY, W.LPCWSTR, C.c_void_p, C.POINTER(W.DWORD), C.c_void_p, C.POINTER(W.DWORD)], C.c_int32),
            (userenv, "get_registry", "GetAppContainerRegistryLocation", [W.DWORD, C.POINTER(W.HKEY)], C.c_int32),
        )
        for dll, alias, name, arguments, result in bindings:
            function = getattr(dll, name)
            function.argtypes, function.restype = arguments, result
            setattr(self, alias, function)
        self.last_error = C.get_last_error


def _call(api: Any, log: list[dict[str, Any]], name: str, *args: Any,
          _return_owner: Handle | None = None) -> int:
    closing = name in ("close", "reg_close")
    count = sum((item["call"] in ("close", "reg_close")) == closing for item in log)
    if count >= (MAX_CLOSE_CALLS if closing else MAX_CALLS):
        raise ValueError("native_call_budget")
    event: dict[str, Any] = {"call": name, "state": "prepared"}
    log.append(event)
    event["state"] = "call_entered"
    try:
        result = getattr(api, name)(*args)
        if _return_owner is not None:
            _return_owner.cell.value = result  # Before logging/formatting can fail.
        error = api.last_error()  # Before any other native call/formatting.
    except BaseException as failure:
        event.update(state="raised", error_type=type(failure).__name__)
        try:
            event["last_error"] = api.last_error()
        except BaseException:
            event["last_error_unavailable"] = True
        raise
    value = int(result or 0)
    event.update(state="returned", result=value, last_error=error)
    if name in ("close", "reg_close"):
        log[-1]["handle"] = args[0].value
    return value


def _bool(api: Any, log: list[dict[str, Any]], name: str, *args: Any) -> int:
    value = _call(api, log, name, *args)
    if not value:
        raise ValueError(name + "_failed")
    return value


def _status(api: Any, log: list[dict[str, Any]], name: str, *args: Any) -> None:
    if _call(api, log, name, *args) != 0:
        raise ValueError(name + "_failed")


def _handle(value: Any) -> bool:
    return (type(value) is int and 0 < value < 2**(8 * C.sizeof(C.c_void_p))
            and value not in (0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFE)
            and not 0x80000000 <= value <= 0x800000FF
            and not 0xFFFFFFFF80000000 <= value <= 0xFFFFFFFF800000FF)


class Handle:
    """Output cell is registered before native use; close is attempted once."""

    def __init__(self, api: Any, log: list[dict[str, Any]], *, key: bool = False) -> None:
        self.api, self.log, self.key = api, log, key
        self.cell = W.HANDLE()
        self.attempted = False
        self.failed = False

    def require(self) -> Any:
        if self.attempted or not _handle(self.cell.value):
            raise ValueError("invalid_or_closed_handle")
        return self.cell

    def close(self) -> None:
        if self.attempted:
            if self.failed:
                raise ValueError("sticky_close_failure")
            return
        self.attempted = True
        if not self.cell.value:
            return
        if not _handle(self.cell.value):
            self.failed = True
            raise ValueError("pseudo_or_invalid_output_not_closed")
        try:
            if self.key:
                _status(self.api, self.log, "reg_close", self.cell)
            else:
                _bool(self.api, self.log, "close", self.cell)
        except BaseException:
            self.failed = True
            raise


def _sid(raw: bytes, offset: int, end: int) -> tuple[str, int]:
    if not 0 <= offset <= end - 8 or raw[offset] != 1:
        raise ValueError("sid_header")
    count = raw[offset + 1]
    size = 8 + 4 * count
    if count > 15 or offset + size > end:
        raise ValueError("sid_budget")
    authority = int.from_bytes(raw[offset + 2:offset + 8], "big")
    sub = struct.unpack_from("<" + "I" * count, raw, offset + 8)
    return "S-1-" + str(authority) + "".join("-" + str(x) for x in sub), size


def _canonical_sid(text: str) -> str:
    if not isinstance(text, str) or len(text) > 184 or not re.fullmatch(r"S-1-[0-9]+(?:-[0-9]+){1,15}", text, re.ASCII):
        raise ValueError("sid_text")
    parts = [int(x) for x in text.split("-")[2:]]
    if parts[0] >= 2**48 or any(x >= 2**32 for x in parts[1:]):
        raise ValueError("sid_range")
    if text != "S-1-" + "-".join(str(x) for x in parts):
        raise ValueError("sid_noncanonical")
    return text


def parse_sd(raw: bytes, package: str, user: str) -> dict[str, Any]:
    """Bounded canonical parser; raw SD is preserved whole, never truncated."""
    if not isinstance(raw, bytes) or not 20 <= len(raw) <= MAX_SD:
        raise ValueError("sd_budget")
    revision, reserved, control, owner, group, sacl, dacl = struct.unpack_from("<BBHIIII", raw)
    if revision != 1 or reserved or not control & 0x8000 or not control & 4 or not control & 16:
        raise ValueError("sd_shape")
    if any(offset and (offset < 20 or offset % 4 or offset >= len(raw)) for offset in (owner, group, sacl, dacl)):
        raise ValueError("sd_offset")
    if not owner or not sacl or not dacl:
        raise ValueError("sd_missing_owner_dacl_label")
    owner_sid, owner_size = _sid(raw, owner, len(raw))
    regions = [(owner, owner + owner_size)]
    if owner_sid != user:
        raise ValueError("unexpected_owner")
    if group:
        _, group_size = _sid(raw, group, len(raw))
        if (group, group + group_size) != regions[0]:
            regions.append((group, group + group_size))

    def acl(offset: int, label: bool) -> list[dict[str, Any]]:
        if offset > len(raw) - 8:
            raise ValueError("acl_header")
        rev, zero, size, count, zero2 = struct.unpack_from("<BBHHH", raw, offset)
        if rev not in (2, 4) or zero or zero2 or not 8 <= size <= len(raw) - offset or count > (1 if label else 16):
            raise ValueError("acl_budget")
        regions.append((offset, offset + size))
        cursor = offset + 8
        entries: list[dict[str, Any]] = []
        for _ in range(count):
            if cursor > offset + size - 8:
                raise ValueError("ace_header")
            kind, flags, length, mask = struct.unpack_from("<BBHI", raw, cursor)
            if length < 16 or length % 4 or cursor + length > offset + size or kind not in ((17,) if label else (0, 1)):
                raise ValueError("unknown_or_invalid_ace")
            sid, sid_size = _sid(raw, cursor + 8, cursor + length)
            if length != 8 + sid_size or flags & ~(0x1F if label else 0x3F):
                raise ValueError("ace_layout")
            if label:
                if sid != "S-1-16-4096" or mask != 1 or flags & ~0x13:
                    raise ValueError("label_not_low_no_write_up")
            elif sid not in (package, user, "S-1-5-18", "S-1-5-32-544"):
                raise ValueError("unknown_dacl_subject")
            entries.append({"type": kind, "flags": flags, "mask": mask, "sid": sid})
            cursor += length
        if any(raw[cursor:offset + size]):
            raise ValueError("acl_nonzero_slack")
        if label and count != 1:
            raise ValueError("label_missing")
        return entries

    entries, labels = acl(dacl, False), acl(sacl, True)
    ordered = sorted(regions)
    if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
        raise ValueError("overlapping_sd_regions")
    if sum(entry["sid"] == package for entry in entries) != 1:
        raise ValueError("package_ace_not_unique")
    return {"raw_sd_hex": raw.hex(), "sd_bytes": len(raw), "owner_sid": owner_sid,
            "control": control, "dacl": entries, "label": labels[0]}


def _snapshot(api: Any, log: list[dict[str, Any]], key: Any, package: str, user: str) -> dict[str, Any]:
    data, size = C.create_string_buffer(MAX_SD), W.DWORD(MAX_SD)
    status = _call(api, log, "get_security", key, 0x15, data, C.byref(size))
    log[-1]["required_or_returned_bytes"] = size.value
    if status:
        raise ValueError("get_security_failed")
    if not 20 <= size.value <= MAX_SD:
        raise ValueError("sd_result_budget")
    raw = data.raw[:size.value]
    log[-1]["raw_sd_hex"] = raw.hex()  # Preserve bounded evidence even on parser rejection.
    return parse_sd(raw, package, user)


def _token(api: Any, log: list[dict[str, Any]], token: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for name, kind in (("user_sid", 1), ("token_type", 8), ("integrity_sid", 25),
                       ("is_appcontainer", 29), ("capability_count", 30), ("package_sid", 31),
                       ("mandatory_policy", 27), ("token_id", 10)):
        data, size = C.create_string_buffer(MAX_TOKEN), W.DWORD()
        _bool(api, log, "get_token", token, kind, data, MAX_TOKEN, C.byref(size))
        if not 4 <= size.value <= MAX_TOKEN:
            raise ValueError("token_result_budget")
        if kind in (1, 25, 31):
            minimum = 8 if kind == 31 else 16
            if size.value < minimum:
                raise ValueError("token_pointer_header")
            pointer = C.c_void_p.from_buffer(data).value
            if not pointer and kind == 31:
                fields[name] = None
            else:
                offset = int(pointer or 0) - C.addressof(data)
                if offset < minimum:
                    raise ValueError("token_sid_overlaps_header")
                fields[name] = _sid(data.raw[:size.value], offset, size.value)[0]
        elif kind == 10:
            if size.value != 56:
                raise ValueError("token_statistics_size")
            fields[name] = list(struct.unpack_from("<II", data.raw))
        else:
            if (kind == 30 and size.value < 8) or (kind != 30 and size.value != 4):
                raise ValueError("token_scalar_or_groups_header")
            fields[name] = struct.unpack_from("<I", data.raw)[0]
    if fields["token_type"] == 2:
        data, size = C.create_string_buffer(MAX_TOKEN), W.DWORD()
        _bool(api, log, "get_token", token, 9, data, MAX_TOKEN, C.byref(size))
        if size.value != 4:
            raise ValueError("impersonation_level_size")
        fields["impersonation_level"] = struct.unpack_from("<I", data.raw)[0]
    return fields


def _verify_subject(metadata: Mapping[str, Any], package: str, user: str, kind: int) -> None:
    if (metadata.get("user_sid") != user or metadata.get("package_sid") != package
            or metadata.get("token_type") != kind or metadata.get("integrity_sid") != "S-1-16-4096"
            or metadata.get("is_appcontainer") != 1 or metadata.get("capability_count") != 0
            or metadata.get("mandatory_policy") != 1
            or (kind == 2 and metadata.get("impersonation_level") != 2)):
        raise ValueError("subject_token_mismatch")


def _absent_thread(api: Any, log: list[dict[str, Any]], owners: list[Handle]) -> None:
    thread = Handle(api, log)
    owners.append(thread)
    result = _call(api, log, "open_thread_token", api.current_thread(), 8, True, C.byref(thread.cell))
    if result or log[-1]["last_error"] != 1008 or thread.cell.value:
        raise ValueError("thread_token_not_absent")


def _verify_host(metadata: Mapping[str, Any], user: str) -> None:
    if (metadata.get("user_sid") != user or metadata.get("token_type") != 1
            or metadata.get("integrity_sid") != "S-1-16-8192" or metadata.get("is_appcontainer") != 0
            or metadata.get("capability_count") != 0 or metadata.get("package_sid") is not None):
        raise ValueError("helper_or_host_primary_not_medium")
    if metadata.get("mandatory_policy") != 3:
        raise ValueError("host_mandatory_policy_not_three")


def _message(value: Mapping[str, Any]) -> bytes:
    payload = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii") + b"\n"
    if len(payload) > MAX_STDOUT:
        raise ValueError("message_budget")
    return payload


def _decode(data: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result
    value = json.loads(data, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("message_shape")
    return dict(value)


def _helper(token: int, package: str, user: str, nonce: str) -> int:
    # No native call until the parent has registered us and assigned our Job.
    if sys.stdin.buffer.readline(80) != ("START " + nonce + "\n").encode("ascii"):
        return 2
    api: Any = ReadApi()
    log: list[dict[str, Any]] = []
    owners: list[Handle] = []
    inherited = Handle(api, log)
    inherited.cell.value = token
    owners.append(inherited)
    report: dict[str, Any] = {"stage0_passed": False, "nonce": nonce, "status": "failed"}
    impersonating, revert_attempted, code, emitted = False, False, 2, 0

    def emit() -> None:
        nonlocal emitted
        payload = _message(report)
        emitted += len(payload)
        if emitted > MAX_STDOUT:
            raise ValueError("helper_total_stdout_budget")
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()

    try:
        host = Handle(api, log)
        owners.append(host)
        _bool(api, log, "open_process_token", api.current_process(), 8, C.byref(host.cell))
        report["helper_primary"] = _token(api, log, host.require())
        _verify_host(report["helper_primary"], user)
        _absent_thread(api, log, owners)
        report["inherited_worker_primary"] = _token(api, log, inherited.require())
        _verify_subject(report["inherited_worker_primary"], package, user, 1)
        duplicate, thread = Handle(api, log), Handle(api, log)
        location, exported = Handle(api, log, key=True), Handle(api, log, key=True)
        owners.extend((duplicate, thread, location, exported))
        _bool(api, log, "duplicate_token", inherited.require(), 0xC, None, 2, 2, C.byref(duplicate.cell))
        impersonating = True  # Revert even if native/wrapper reports failure.
        _bool(api, log, "impersonate", duplicate.require())
        _bool(api, log, "open_thread_token", api.current_thread(), 8, True, C.byref(thread.cell))
        report["effective_thread"] = _token(api, log, thread.require())
        _verify_subject(report["effective_thread"], package, user, 2)
        _status(api, log, "get_registry", READ_CONTROL, C.byref(location.cell))
        _status(api, log, "open_key", location.require(), None, 0, READ_CONTROL, C.byref(exported.cell))
        report["snapshot"] = _snapshot(api, log, exported.require(), package, user)
        revert_attempted = True
        _bool(api, log, "revert")
        impersonating = False
        _absent_thread(api, log, owners)
        report.update(status="ready", exported_key=exported.cell.value, revert_confirmed=True)
        emit()
        if sys.stdin.buffer.readline(80) != ("ACK " + nonce + "\n").encode("ascii"):
            raise ValueError("nonce_ack_missing")
        report["status"], code = "complete", 0
    except BaseException as error:
        report.update(status="failed", error=type(error).__name__ + ":" + str(error)[:256])
    finally:
        if impersonating:
            try:
                if revert_attempted:
                    raise ValueError("revert_already_failed_no_retry")
                revert_attempted = True
                _bool(api, log, "revert")
                impersonating = False
                _absent_thread(api, log, owners)
            except BaseException:
                report.update(status="fatal_revert_failure", calls=log)
                try:
                    emit()
                finally:
                    os._exit(77)
        errors = []
        for owner in reversed(owners):
            try:
                owner.close()
            except BaseException:
                errors.append("key" if owner.key else "handle")
        if errors:
            report["status"], code = "close_failed", 2
        report.update(close_errors=errors, calls=log)
    emit()
    return code


class _BasicLimits(C.Structure):
    _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64), ("flags", W.DWORD),
                ("min_ws", C.c_size_t), ("max_ws", C.c_size_t), ("active", W.DWORD),
                ("affinity", C.c_size_t), ("priority", W.DWORD), ("scheduling", W.DWORD)]


class _JobLimits(C.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", C.c_uint64 * 6), ("process_memory", C.c_size_t),
                ("job_memory", C.c_size_t), ("peak_process", C.c_size_t), ("peak_job", C.c_size_t)]


class HelperOwner:
    def __init__(self, child: subprocess.Popen[bytes], api: Any, log: list[dict[str, Any]]) -> None:
        self.child, self.api, self.log = child, api, log
        self.job = Handle(api, log)
        self.stdout_bytes = 0
        self.stderr = bytearray()
        self.stdout = bytearray()
        self.reaped = False
        self.close_attempted = False
        self.close_failed = False
        self.pipe_calls: list[dict[str, Any]] = []
        self.cleanup_events: list[dict[str, Any]] = []

    def assign_job(self) -> None:
        _call(self.api, self.log, "create_job", None, None, _return_owner=self.job)
        self.job.require()
        limits = _JobLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
        _bool(self.api, self.log, "set_job", self.job.require(), 9, C.byref(limits), C.sizeof(limits))
        _bool(self.api, self.log, "assign_job", self.job.require(), int(getattr(self.child, "_handle")))

    def _chunk(self, stream: Any) -> bytes:
        import msvcrt
        available = W.DWORD()
        if len(self.pipe_calls) >= 8192:
            raise ValueError("pipe_call_budget")
        event: dict[str, Any] = {"state": "call_entered"}
        self.pipe_calls.append(event)
        try:
            value = self.api.peek(msvcrt.get_osfhandle(stream.fileno()), None, 0, None, C.byref(available), None)
            error = self.api.last_error()
        except BaseException as failure:
            event.update(state="raised", error_type=type(failure).__name__)
            event["last_error"] = self.api.last_error()
            raise
        event.update(state="returned", result=int(value or 0), last_error=error, available=available.value)
        if not value:
            if error == 109:
                return b""
            raise ValueError("pipe_peek_failed")
        return os.read(stream.fileno(), min(available.value, 4096)) if available.value else b""

    def pump(self, *, ready: bool) -> dict[str, Any]:
        deadline, data = monotonic() + 10, bytearray()
        while monotonic() < deadline:
            chunk = self._chunk(self.child.stdout)
            self.stdout_bytes += len(chunk)
            self.stdout.extend(chunk)  # Retain evidence before stderr can fail.
            data.extend(chunk)
            if self.stdout_bytes > MAX_STDOUT:
                raise ValueError("helper_output_budget")
            errors = self._chunk(self.child.stderr)
            self.stderr.extend(errors)
            if self.stdout_bytes > MAX_STDOUT or len(self.stderr) > MAX_STDERR:
                raise ValueError("helper_output_budget")
            if ready and b"\n" in data:
                line, trailing = bytes(data).split(b"\n", 1)
                if trailing:
                    raise ValueError("unexpected_second_ready_frame")
                return _decode(line)
            if self.child.poll() is not None and not chunk and not errors:
                self.child.wait(timeout=3)
                self.reaped = True
                if ready:
                    raise ValueError("helper_exited_before_ready")
                return _decode(bytes(data))
            if not chunk and not errors:
                sleep(0.005)
        raise TimeoutError("helper_output_timeout")

    def send(self, prefix: str, nonce: str) -> None:
        if self.child.stdin is None:
            raise ValueError("helper_stdin_missing")
        self.child.stdin.write((prefix + " " + nonce + "\n").encode("ascii"))
        self.child.stdin.flush()

    def close(self) -> None:
        if self.close_attempted:
            if self.close_failed:
                raise ValueError("sticky_helper_close_failure")
            return
        self.close_attempted = True
        errors = []
        if not self.reaped:
            try:
                self.child.kill()  # Once only; no terminate/kill retry.
                self.cleanup_events.append({"operation": "kill", "returned": True})
            except OSError as error:
                self.cleanup_events.append({"operation": "kill", "returned": False,
                                            "winerror": getattr(error, "winerror", None)})
            try:
                self.child.wait(timeout=3)
                self.reaped = True
                self.cleanup_events.append({"operation": "wait", "reaped": True})
            except BaseException:
                errors.append("reap")
                _UNCONFIRMED.append(self)
        for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
            try:
                if stream is not None:
                    stream.close()
            except BaseException:
                errors.append("stream_close")
        try:
            self.job.close()
        except BaseException:
            errors.append("job_close")
        # Popen owns this process handle; its Close method marks it consumed
        # before the Windows close, preventing a destructor retry on failure.
        try:
            getattr(self.child, "_handle").Close()
            self.cleanup_events.append({"operation": "popen_process_close", "returned": True})
        except BaseException:
            errors.append("popen_process_close")
        if errors:
            self.close_failed = True
            raise ValueError("helper_cleanup:" + ",".join(errors))


class FreshBindingSession:
    """One attempt, one registered fresh worker; no caller PID/HKEY/path input.

    The native worker owner is the driver trust boundary, not a boolean receipt.
    Its require_suspended_verified() must recheck registered launch evidence and
    must never resume the worker. The driver MUST call bind from its beforeResume
    callback while holding core._CREATE_LOCK across the entire inheritance
    window; a module-local lock cannot protect other host spawning code.
    Binding/reset failure is sticky; the inherited token reset is never retried.
    """

    def __init__(self, profile_name: str, package_sid: str, user_sid: str,
                 seed_names: Sequence[str], worker_owner: VerifiedWorkerOwner, *, api: Any = None) -> None:
        if not _PROFILE.fullmatch(profile_name):
            raise ValueError("fresh_profile_name")
        self.package = _canonical_sid(package_sid)
        self.user = _canonical_sid(user_sid)
        if not self.package.startswith("S-1-15-2-") or self.user.startswith("S-1-15-"):
            raise ValueError("subject_sid_family")
        self.names = frozenset(seed_names)
        if self.names != {"seed_" + profile_name.rsplit(".", 1)[1]}:
            raise ValueError("registered_seed_names")
        self.profile_name, self.worker = profile_name, worker_owner
        self.api = api
        self.log: list[dict[str, Any]] = []
        self.handles: list[Handle] = []
        self.helpers: list[HelperOwner] = []
        self.attempted = self.failed = False
        self.binding: BoundRoot | None = None
        self.seed_phase = 0
        self.seed_results: dict[str, dict[str, Any]] = {}

    def _owner(self, *, key: bool = False) -> Handle:
        owner = Handle(self.api, self.log, key=key)
        self.handles.append(owner)
        return owner

    def bind(self, environment: Mapping[str, str]) -> dict[str, Any]:
        if self.attempted:
            raise ValueError("binding_attempt_already_consumed")
        self.attempted = True
        try:
            return self._bind(environment)
        except BaseException:
            self.failed = True
            self.close()
            raise

    def _bind(self, environment: Mapping[str, str]) -> dict[str, Any]:
        # Only fixed launcher keys are accepted; the driver constructs values
        # from its known Windows directory. Never copy the ambient environment.
        if set(environment) != {"SystemRoot", "WINDIR", "PATH", "TEMP", "TMP"}:
            raise ValueError("helper_environment_keys")
        if any(not isinstance(value, str) or not value or len(value) > 512 or "\0" in value for value in environment.values()):
            raise ValueError("helper_environment_value")
        self.api = self.api or ReadApi()
        api = self.api
        process = self.worker.require_suspended_verified()
        if not _handle(process):
            raise ValueError("registered_worker_handle")
        token, host, source = self._owner(), self._owner(), self._owner()
        _bool(api, self.log, "open_process_token", process, 0xA, C.byref(token.cell))
        _bool(api, self.log, "open_process_token", api.current_process(), 8, C.byref(host.cell))
        host_metadata = _token(api, self.log, host.require())
        _verify_host(host_metadata, self.user)
        _absent_thread(api, self.log, self.handles)
        subject = _token(api, self.log, token.require())
        _verify_subject(subject, self.package, self.user, 1)
        # Recheck immediately before spawning; the driver retains worker owner.
        if self.worker.require_suspended_verified() != process:
            raise ValueError("worker_owner_changed")
        nonce = uuid4().hex
        os.set_handle_inheritable(token.require().value, True)
        try:
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {"handle_list": [token.cell.value]}
            child = subprocess.Popen([sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()),
                "--_readonly-binding", str(token.cell.value), self.package, self.user, nonce],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                close_fds=True, startupinfo=startup, env=dict(environment), cwd=Path(__file__).resolve().parent,
                creationflags=subprocess.CREATE_NO_WINDOW)
            helper = HelperOwner(child, api, self.log)
            self.helpers.append(helper)  # Before Job/START/pipe processing.
        finally:
            os.set_handle_inheritable(int(token.cell.value or 0), False)
        helper.assign_job()
        helper.send("START", nonce)
        ready = helper.pump(ready=True)
        if ready.get("status") != "ready" or ready.get("nonce") != nonce or ready.get("revert_confirmed") is not True:
            raise ValueError("helper_ready_mismatch")
        _verify_host(ready.get("helper_primary", {}), self.user)
        _verify_subject(ready.get("inherited_worker_primary", {}), self.package, self.user, 1)
        if ready["inherited_worker_primary"].get("token_id") != subject.get("token_id"):
            raise ValueError("inherited_worker_token_id_mismatch")
        _verify_subject(ready.get("effective_thread", {}), self.package, self.user, 2)
        exported = ready.get("exported_key")
        if not _handle(exported):
            raise ValueError("exported_key_shape")
        _call(api, self.log, "open_process", 0x40, False, child.pid, _return_owner=source)
        root = self._owner(key=True)
        _bool(api, self.log, "duplicate_handle", source.require(), exported, api.current_process(),
              C.byref(root.cell), READ_CONTROL, False, 0)
        snapshot = _snapshot(api, self.log, root.require(), self.package, self.user)
        if snapshot != ready.get("snapshot"):
            raise ValueError("export_snapshot_changed")
        # Object identity is the direct DuplicateHandle lineage above; SD
        # equality is only a separate consistency check, never identity proof.
        helper.send("ACK", nonce)
        final = helper.pump(ready=False)
        if (not helper.reaped or child.returncode != 0 or helper.stderr
                or final.get("status") != "complete" or final.get("close_errors") != []
                or any(final.get(key) != ready.get(key) for key in ready if key != "status")):
            raise ValueError("helper_final_or_reap_unconfirmed")
        helper.close()
        for owner in (token, host, source):
            owner.close()
        self.binding = BoundRoot(self, root, snapshot)
        return {"stage0_passed": False, "outer_subject": subject, "helper_ready": ready,
                "helper_final": final, "helper_reaped": True, "helper_pipe_calls": helper.pipe_calls,
                "binding_identity": "direct_duplicate_of_this_spawned_helpers_exported_hkey"}

    def close(self) -> None:
        errors = []
        resources: list[HelperOwner | Handle] = [*reversed(self.helpers), *reversed(self.handles)]
        for owner in resources:
            try:
                owner.close()
            except BaseException:
                errors.append("owned_resource_close")
        if errors:
            self.failed = True
            _UNCONFIRMED.append(self)
            raise ValueError("session_cleanup_failed")


class BoundRoot:
    def __init__(self, session: FreshBindingSession, owner: Handle, snapshot: dict[str, Any]) -> None:
        self.session, self.owner, self.snapshot = session, owner, snapshot

    def read_seed(self, name: str, *, phase: Literal["after_low", "after_untrusted"]) -> dict[str, Any]:
        session = self.session
        expected = {0: "after_low", 1: "after_untrusted"}.get(session.seed_phase)
        if session.failed or phase != expected or session.binding is not self or name not in session.names:
            session.failed = True
            raise ValueError("unregistered_seed_or_invalid_binding")
        # No path, enumeration, caller value name, create, or write operation.
        if not isinstance(name, str) or not _SEED.fullmatch(name):
            raise ValueError("seed_name")
        session.seed_phase = -1  # Consume before any native call, including root SD.
        seed = session._owner(key=True)
        try:
            root_snapshot = _snapshot(session.api, session.log, self.owner.require(), session.package, session.user)
            _status(session.api, session.log, "open_key", self.owner.require(), name, 0,
                    READ_CONTROL | 1, C.byref(seed.cell))
            kind, size, data = W.DWORD(), W.DWORD(8), C.create_string_buffer(8)
            query_index = len(session.log)
            try:
                status = _call(session.api, session.log, "query_value", seed.require(), FIXED_VALUE,
                               None, C.byref(kind), data, C.byref(size))
            finally:
                if len(session.log) > query_index:
                    entry = session.log[query_index]
                    entry.update(raw_type=kind.value, required_or_returned_bytes=size.value,
                                 bounded_buffer_hex8=data.raw.hex(),
                                 valid_binary8=(entry.get("state") == "returned" and entry.get("result") == 0
                                                and kind.value == 3 and size.value == 8))
            if status != 0:
                raise ValueError("query_value_failed")
            if kind.value != 3 or size.value != 8:
                raise ValueError("seed_value_not_binary8")
            snapshot = _snapshot(session.api, session.log, seed.require(), session.package, session.user)
            session.seed_phase = 1 if phase == "after_low" else 2
            result = {"phase": phase, "name": name, "value_name": FIXED_VALUE,
                      "value_hex": data.raw.hex(), "snapshot": snapshot, "root_snapshot": root_snapshot,
                      "seed_close_confirmed": False}
            session.seed_results[phase] = result  # Preserve before close can fail.
            return result
        except BaseException:
            session.failed = True
            raise
        finally:
            try:
                seed.close()
                if phase in session.seed_results:
                    session.seed_results[phase]["seed_close_confirmed"] = True
            except BaseException:
                session.failed = True
                raise


def _main(argv: Sequence[str]) -> int:
    # Private self-spawn entry, no arbitrary PID/HKEY/path or experiment CLI.
    if (len(argv) != 6 or argv[1] != "--_readonly-binding" or not argv[2].isascii()
            or not argv[2].isdecimal() or not _handle(int(argv[2])) or not _NONCE.fullmatch(argv[5])):
        raise ValueError("private_helper_arguments")
    package, user = _canonical_sid(argv[3]), _canonical_sid(argv[4])
    if not package.startswith("S-1-15-2-") or user.startswith("S-1-15-"):
        raise ValueError("private_helper_sid_family")
    return _helper(int(argv[2]), package, user, argv[5])


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
