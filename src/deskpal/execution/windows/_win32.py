"""Private Win32 ABI bindings. Loaded only by an explicit Windows launch.

Pointers/handles remain native-width; Win32 DWORD/LONG use fixed widths. Native
API errors are reduced to fixed stages and numeric codes before crossing the
adapter. No profile, filesystem ACL, token capability or network exemption is
provisioned by this module.
"""

from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import subprocess
import threading
import time
from typing import Any

from .launcher import LaunchRejected, LaunchSpec, WorkerIdentity

DWORD = C.c_uint32
SIZE = C.c_size_t
PTR = C.c_void_p
BOOL = C.c_int32
HANDLE = PTR
_CREATE_LOCK = threading.Lock()
_LIMIT_FLAGS = 0x2000 | 0x200 | 0x100 | 0x8  # kill-close, Job/process memory, active count.
_CPU_FLAGS = 0x1 | 0x4  # enabled, hard cap.


class SECURITY_ATTRIBUTES(C.Structure):
    _fields_ = [("nLength", DWORD), ("lpSecurityDescriptor", PTR), ("bInheritHandle", BOOL)]


class STARTUPINFO(C.Structure):
    _fields_ = [
        ("cb", DWORD), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR),
        ("lpTitle", W.LPWSTR), ("dwX", DWORD), ("dwY", DWORD),
        ("dwXSize", DWORD), ("dwYSize", DWORD), ("dwXCountChars", DWORD),
        ("dwYCountChars", DWORD), ("dwFillAttribute", DWORD), ("dwFlags", DWORD),
        ("wShowWindow", C.c_uint16), ("cbReserved2", C.c_uint16),
        ("lpReserved2", PTR), ("hStdInput", HANDLE), ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


class STARTUPINFOEX(C.Structure):
    _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", PTR)]


class PROCESS_INFORMATION(C.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE),
                ("dwProcessId", DWORD), ("dwThreadId", DWORD)]


class SECURITY_CAPABILITIES(C.Structure):
    _fields_ = [("AppContainerSid", PTR), ("Capabilities", PTR),
                ("CapabilityCount", DWORD), ("Reserved", DWORD)]


class JOB_BASIC_LIMIT(C.Structure):
    _fields_ = [("PerProcessUserTimeLimit", C.c_int64), ("PerJobUserTimeLimit", C.c_int64),
                ("LimitFlags", DWORD), ("MinimumWorkingSetSize", SIZE),
                ("MaximumWorkingSetSize", SIZE), ("ActiveProcessLimit", DWORD),
                ("Affinity", SIZE), ("PriorityClass", DWORD), ("SchedulingClass", DWORD)]


class IO_COUNTERS(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOB_EXTENDED_LIMIT(C.Structure):
    _fields_ = [("BasicLimitInformation", JOB_BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", SIZE), ("JobMemoryLimit", SIZE),
                ("PeakProcessMemoryUsed", SIZE), ("PeakJobMemoryUsed", SIZE)]


class JOB_CPU(C.Structure):
    _fields_ = [("ControlFlags", DWORD), ("CpuRate", DWORD)]


class JOB_ACCOUNTING(C.Structure):
    _fields_ = [("TotalUserTime", C.c_int64), ("TotalKernelTime", C.c_int64),
                ("ThisPeriodTotalUserTime", C.c_int64), ("ThisPeriodTotalKernelTime", C.c_int64),
                ("TotalPageFaultCount", DWORD), ("TotalProcesses", DWORD),
                ("ActiveProcesses", DWORD), ("TotalTerminatedProcesses", DWORD)]


def _bind(dll: Any, name: str, args: list[Any], result: Any) -> Any:
    function = getattr(dll, name)
    function.argtypes = args
    function.restype = result
    return function


class Api:
    def __init__(self) -> None:
        k = C.WinDLL("kernel32", use_last_error=True)
        a = C.WinDLL("advapi32", use_last_error=True)
        self.close = _bind(k, "CloseHandle", [HANDLE], BOOL)
        self.local_free = _bind(k, "LocalFree", [PTR], PTR)
        self.create_pipe = _bind(k, "CreatePipe", [C.POINTER(HANDLE), C.POINTER(HANDLE),
                                                   C.POINTER(SECURITY_ATTRIBUTES), DWORD], BOOL)
        self.create_event = _bind(k, "CreateEventW", [C.POINTER(SECURITY_ATTRIBUTES), BOOL,
                                                     BOOL, W.LPCWSTR], HANDLE)
        self.handle_info = _bind(k, "SetHandleInformation", [HANDLE, DWORD, DWORD], BOOL)
        self.init_attrs = _bind(k, "InitializeProcThreadAttributeList",
                               [PTR, DWORD, DWORD, C.POINTER(SIZE)], BOOL)
        self.update_attr = _bind(k, "UpdateProcThreadAttribute",
                                [PTR, DWORD, SIZE, PTR, SIZE, PTR, PTR], BOOL)
        self.delete_attrs = _bind(k, "DeleteProcThreadAttributeList", [PTR], None)
        self.create_process = _bind(k, "CreateProcessW", [W.LPCWSTR, W.LPWSTR, PTR, PTR,
                                   BOOL, DWORD, PTR, W.LPCWSTR, C.POINTER(STARTUPINFOEX),
                                   C.POINTER(PROCESS_INFORMATION)], BOOL)
        self.create_job = _bind(k, "CreateJobObjectW", [PTR, W.LPCWSTR], HANDLE)
        self.set_job = _bind(k, "SetInformationJobObject", [HANDLE, C.c_int, PTR, DWORD], BOOL)
        self.query_job = _bind(k, "QueryInformationJobObject", [HANDLE, C.c_int, PTR, DWORD, PTR], BOOL)
        self.assign_job = _bind(k, "AssignProcessToJobObject", [HANDLE, HANDLE], BOOL)
        self.in_job = _bind(k, "IsProcessInJob", [HANDLE, HANDLE, C.POINTER(BOOL)], BOOL)
        self.resume = _bind(k, "ResumeThread", [HANDLE], DWORD)
        self.terminate_process = _bind(k, "TerminateProcess", [HANDLE, DWORD], BOOL)
        self.terminate_job = _bind(k, "TerminateJobObject", [HANDLE, DWORD], BOOL)
        self.wait = _bind(k, "WaitForSingleObject", [HANDLE, DWORD], DWORD)
        self.exit_code = _bind(k, "GetExitCodeProcess", [HANDLE, C.POINTER(DWORD)], BOOL)
        self.peek = _bind(k, "PeekNamedPipe", [HANDLE, PTR, DWORD, PTR, C.POINTER(DWORD), PTR], BOOL)
        self.read = _bind(k, "ReadFile", [HANDLE, PTR, DWORD, C.POINTER(DWORD), PTR], BOOL)
        self.open_token = _bind(a, "OpenProcessToken", [HANDLE, DWORD, C.POINTER(HANDLE)], BOOL)
        self.get_token = _bind(a, "GetTokenInformation", [HANDLE, C.c_int, PTR, DWORD,
                                                        C.POINTER(DWORD)], BOOL)
        self.parse_sid = _bind(a, "ConvertStringSidToSidW", [W.LPCWSTR, C.POINTER(PTR)], BOOL)
        self.equal_sid = _bind(a, "EqualSid", [PTR, PTR], BOOL)
        self.get_mitigation = _bind(k, "GetProcessMitigationPolicy",
                                   [HANDLE, C.c_int, PTR, SIZE], BOOL)


def _check(ok: object, stage: str) -> None:
    if not ok:
        raise LaunchRejected(stage, C.get_last_error())


def _token_value(api: Api, token: int, kind: int) -> Any:
    size = DWORD()
    api.get_token(token, kind, None, 0, C.byref(size))
    if C.get_last_error() != 122 or not 0 < size.value <= 65536:
        raise LaunchRejected("token_size", C.get_last_error())
    buffer = C.create_string_buffer(size.value)
    _check(api.get_token(token, kind, buffer, size, C.byref(size)), "query_token")
    return buffer


def _token_dword(api: Api, token: int, kind: int) -> int:
    buffer = _token_value(api, token, kind)
    if C.sizeof(buffer) < C.sizeof(DWORD):
        raise LaunchRejected("token_invalid_size")
    return int(DWORD.from_buffer(buffer).value)


class NativeWorker:
    def __init__(self, api: Api, process: int, job: int, readers: tuple[int, int, int],
                 canary: int) -> None:
        self.api = api
        self.process = process
        self.job = job
        self.readers = readers
        self.canary = canary
        self._closed = False
        self._eof = [False, False, False]

    def poll(self) -> int | None:
        wait = self.api.wait(self.process, 0)
        if wait == 258:
            return None
        _check(wait == 0, "poll_process")
        code = DWORD()
        _check(self.api.exit_code(self.process, C.byref(code)), "process_exit_code")
        return int(code.value)

    def read_available(self) -> tuple[tuple[bytes, bytes, bytes], tuple[bool, bool, bool]]:
        chunks: list[bytes] = []
        for index, handle in enumerate(self.readers):
            available = DWORD()
            if self._eof[index]:
                chunks.append(b"")
                continue
            if not self.api.peek(handle, None, 0, None, C.byref(available), None):
                error = C.get_last_error()
                if error in (109, 232):
                    self._eof[index] = True
                    chunks.append(b"")
                    continue
                raise LaunchRejected("peek_pipe", error)
            size = min(available.value, 16384)
            if not size:
                chunks.append(b"")
                continue
            data = C.create_string_buffer(size)
            read = DWORD()
            _check(self.api.read(handle, data, size, C.byref(read), None), "read_pipe")
            chunks.append(data.raw[:read.value])
        return ((chunks[0], chunks[1], chunks[2]),
                (self._eof[0], self._eof[1], self._eof[2]))

    def terminate(self) -> None:
        if self._closed:
            return
        try:
            _terminate_and_wait(self.api, self.process, self.job, True)
        except LaunchRejected as error:
            error.cleanup_owner = self
            raise

    def close(self) -> None:
        if self._closed:
            return
        self.terminate()
        for handle in (*self.readers, self.canary, self.process, self.job):
            self.api.close(handle)
        self._closed = True


def _terminate_and_wait(api: Api, process: int, job: int, assigned: bool) -> None:
    if assigned:
        _check(api.terminate_job(job, 0xE0000001), "terminate_job")
    elif api.wait(process, 0) != 0:
        _check(api.terminate_process(process, 0xE0000001), "terminate_process")
    deadline = time.monotonic() + 2.0
    while True:
        empty = True
        if assigned:
            accounting = JOB_ACCOUNTING()
            _check(api.query_job(job, 1, C.byref(accounting),
                                 C.sizeof(accounting), None), "query_job_active")
            empty = accounting.ActiveProcesses == 0
        status = api.wait(process, 0)
        _check(status in (0, 258), "wait_reap")
        if empty and status == 0:
            return
        if time.monotonic() >= deadline:
            raise LaunchRejected("reap_deadline")
        time.sleep(0.005)


class PendingLaunchCleanup:
    """Retain failed-launch handles until a later close confirms reaping."""

    def __init__(self, api: Api, process: int, job: int, assigned: bool,
                 handles: tuple[int, ...]) -> None:
        self.api, self.process, self.job, self.assigned = api, process, job, assigned
        self.handles = handles

    def close(self) -> None:
        if not self.handles:
            return
        _terminate_and_wait(self.api, self.process, self.job, self.assigned)
        for handle in reversed(self.handles):
            self.api.close(handle)
        self.handles = ()


def create_worker(spec: LaunchSpec) -> tuple[NativeWorker, WorkerIdentity]:
    # Temporary inheritable ends are protected against concurrent launches using
    # this adapter. Other host spawn sites must also avoid blanket inheritance.
    with _CREATE_LOCK:
        return _create_worker(spec)


def _create_worker(spec: LaunchSpec, *, _api: Api | None = None) -> tuple[NativeWorker, WorkerIdentity]:
    # The private injection exists only for the suspended-only diagnostic module.
    # WindowsLauncher never supplies an alternate API or omits restrictions.
    api = Api() if _api is None else _api
    handles: list[int] = []
    sid = PTR()
    attrs: Any = None
    attrs_initialized = False
    process = PROCESS_INFORMATION()
    job = 0
    assigned = False
    try:
        _check(api.parse_sid(spec.appcontainer_sid, C.byref(sid)), "parse_package_sid")
        # Require the canonical package SID shape, not a capability/group SID.
        # Conversion above also rejects malformed numeric fields.
        if len(spec.appcontainer_sid.split("-")) != 11:
            raise LaunchRejected("package_sid_shape")
        readers: list[int] = []
        writers: list[int] = []
        sa = SECURITY_ATTRIBUTES(C.sizeof(SECURITY_ATTRIBUTES), None, True)
        canary = int(api.create_event(C.byref(sa), True, False, None) or 0)
        _check(canary, "create_inheritance_canary")
        handles.append(canary)
        for _ in range(4):
            read, write = HANDLE(), HANDLE()
            _check(api.create_pipe(C.byref(read), C.byref(write), C.byref(sa), 65536), "create_pipe")
            read_value, write_value = int(read.value or 0), int(write.value or 0)
            handles.extend((read_value, write_value))
            readers.append(read_value)
            writers.append(write_value)
        # control/stdout/stderr readers belong exclusively to the host. stdin
        # writer is closed before creation, so the child can only observe EOF.
        for handle in readers[:3]:
            _check(api.handle_info(handle, 1, 0), "clear_parent_inherit")
        api.close(writers[3])
        handles.remove(writers[3])
        whitelist = (writers[0], writers[1], writers[2], readers[3])
        count = 4 if spec.low_privilege else 3
        size = SIZE()
        api.init_attrs(None, count, 0, C.byref(size))
        if C.get_last_error() != 122 or not size.value:
            raise LaunchRejected("attribute_list_size", C.get_last_error())
        attrs = C.create_string_buffer(size.value)
        _check(api.init_attrs(attrs, count, 0, C.byref(size)), "initialize_attributes")
        attrs_initialized = True
        capabilities = SECURITY_CAPABILITIES(sid, None, 0, 0)
        inherited = (HANDLE * len(whitelist))(*whitelist)
        child_policy = DWORD(1)
        lpac_policy = DWORD(1)
        values = [(0x20009, capabilities), (0x20002, inherited), (0x2000E, child_policy)]
        if spec.low_privilege:
            values.append((0x2000F, lpac_policy))
        for attribute, value in values:
            _check(api.update_attr(attrs, 0, attribute, C.byref(value), C.sizeof(value), None, None),
                   "set_launch_attribute")
        job = int(api.create_job(None, None) or 0)
        _check(job, "create_job")
        handles.append(job)
        limits = JOB_EXTENDED_LIMIT()
        limits.BasicLimitInformation.LimitFlags = _LIMIT_FLAGS
        limits.BasicLimitInformation.ActiveProcessLimit = spec.limits.active_processes
        limits.ProcessMemoryLimit = spec.limits.memory_bytes
        limits.JobMemoryLimit = spec.limits.memory_bytes
        cpu = JOB_CPU(_CPU_FLAGS, spec.limits.cpu_rate)
        _check(api.set_job(job, 9, C.byref(limits), C.sizeof(limits)), "set_job_limits")
        _check(api.set_job(job, 15, C.byref(cpu), C.sizeof(cpu)), "set_cpu_limit")
        startup = STARTUPINFOEX()
        startup.StartupInfo.cb = C.sizeof(startup)
        startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = readers[3]
        startup.StartupInfo.hStdOutput = writers[1]
        startup.StartupInfo.hStdError = writers[2]
        startup.lpAttributeList = C.cast(attrs, PTR)
        args = [arg.replace("{ipc_write_handle}", str(writers[0])).replace(
            "{excluded_handle}", str(canary)) for arg in spec.args]
        command = C.create_unicode_buffer(subprocess.list2cmdline([str(spec.executable), *args]))
        environment = C.create_unicode_buffer("\0".join(
            f"{key}={value}" for key, value in sorted(spec.environment.items())) + "\0\0")
        flags = 0x4 | 0x80000 | 0x400 | 0x8  # suspended, extended, Unicode env, detached console.
        try:
            _check(api.create_process(str(spec.executable), command, None, None, True, flags,
                                     environment, str(spec.cwd), C.byref(startup), C.byref(process)),
                   "create_suspended_process")
        finally:
            # Diagnostic wrappers can raise after Windows has filled PI. Keep
            # those handles owned even when control never returns from create.
            # Register here, before later code can close/transfer either one.
            for created_handle in (process.hProcess, process.hThread):
                if created_handle and int(created_handle) not in handles:
                    handles.append(int(created_handle))
        _check(api.assign_job(job, process.hProcess), "assign_job")
        assigned = True
        identity = _verify(api, spec, process, job, sid, whitelist, canary)
        # Close host copies of child ends before resume, preserving EOF detection.
        for handle in whitelist:
            api.close(handle)
            handles.remove(handle)
        previous_count = api.resume(process.hThread)
        if previous_count != 1:
            raise LaunchRejected("resume_thread", C.get_last_error())
        api.close(process.hThread)
        handles.remove(int(process.hThread))
        native = NativeWorker(api, int(process.hProcess), job,
                              (readers[0], readers[1], readers[2]), canary)
        handles.clear()  # ownership transferred to NativeWorker.
        return native, identity
    except BaseException as original:
        if process.hProcess:
            owner = PendingLaunchCleanup(api, int(process.hProcess), job, assigned, tuple(handles))
            handles.clear()
            try:
                owner.close()
            except BaseException:
                raise LaunchRejected("launch_failure_reap_unconfirmed", cleanup_owner=owner) from original
        raise
    finally:
        for handle in reversed(handles):
            api.close(handle)
        if attrs_initialized:
            api.delete_attrs(attrs)
        if sid:
            api.local_free(sid)


def _verify(api: Api, spec: LaunchSpec, process: PROCESS_INFORMATION, job: int,
            expected_sid: PTR, whitelist: tuple[int, ...], canary: int) -> WorkerIdentity:
    token = HANDLE()
    _check(api.open_token(process.hProcess, 0x8, C.byref(token)), "open_worker_token")
    try:
        token_handle = int(token.value or 0)
        is_ac = _token_dword(api, token_handle, 29)
        capabilities = _token_dword(api, token_handle, 30)  # TOKEN_GROUPS.GroupCount.
        sid_buffer = _token_value(api, token_handle, 31)
        actual_sid = PTR.from_buffer(sid_buffer)
        # The LPAC query is not supported by every Windows build. A requested
        # LPAC must be positively verified or launch fails. Ordinary AC does not
        # depend on that optional query and reports its LPAC distinction unknown.
        lpac = _token_dword(api, token_handle, 46) if spec.low_privilege else None
        if is_ac != 1 or capabilities != 0 or not api.equal_sid(actual_sid, expected_sid):
            raise LaunchRejected("worker_token_mismatch")
        if spec.low_privilege and lpac != 1:
            raise LaunchRejected("worker_restriction_mismatch")
    finally:
        api.close(token)
    limits = JOB_EXTENDED_LIMIT()
    cpu = JOB_CPU()
    member = BOOL()
    child_flags = DWORD()
    _check(api.get_mitigation(process.hProcess, 13, C.byref(child_flags), C.sizeof(child_flags)),
           "query_child_process_policy")
    if not child_flags.value & 1:
        raise LaunchRejected("child_process_policy_mismatch")
    _check(api.query_job(job, 9, C.byref(limits), C.sizeof(limits), None), "query_job_limits")
    _check(api.query_job(job, 15, C.byref(cpu), C.sizeof(cpu), None), "query_cpu_limit")
    _check(api.in_job(process.hProcess, job, C.byref(member)), "query_job_membership")
    basic = limits.BasicLimitInformation
    if (basic.LimitFlags != _LIMIT_FLAGS or basic.ActiveProcessLimit != spec.limits.active_processes
            or limits.ProcessMemoryLimit != spec.limits.memory_bytes
            or limits.JobMemoryLimit != spec.limits.memory_bytes or not member.value
            or cpu.ControlFlags != _CPU_FLAGS or cpu.CpuRate != spec.limits.cpu_rate):
        raise LaunchRejected("job_limit_mismatch")
    return WorkerIdentity(int(process.dwProcessId), spec.appcontainer_sid, True,
                          bool(lpac) if lpac is not None else None,
                          capabilities, child_flags.value, basic.LimitFlags, int(limits.JobMemoryLimit),
                          int(limits.ProcessMemoryLimit), basic.ActiveProcessLimit,
                          cpu.ControlFlags, cpu.CpuRate, True, whitelist, canary)
