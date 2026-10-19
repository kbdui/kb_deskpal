"""Fail-closed AppContainer/Job launcher for synthetic stage-zero probes.

This is an experimental adapter, not a tool executor. The caller is responsible
for preparing and independently verifying an immutable runtime and every
reachable writable namespace. No ACL/profile changes are made here. stdout and
stderr are logs, never protocol. The output-only control pipe accepts framed JSON
from exactly the process created by this instance, but business/session/attempt
authentication is deliberately left to a future supervisor.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
from pathlib import Path
import sys
import time
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from deskpal.execution.protocol import FrameDecoder, FrameLimits

if TYPE_CHECKING:
    from ._win32 import NativeWorker


class CleanupOwner(Protocol):
    def close(self) -> None: ...


class LaunchRejected(RuntimeError):
    """A launch/transport failed; never carries worker-controlled text."""

    def __init__(self, stage: str, winerror: int | None = None, *,
                 cleanup_owner: CleanupOwner | None = None) -> None:
        self.stage = stage
        self.winerror = winerror
        # If reaping could not be confirmed, the caller must retain this owner,
        # retry close(), and keep profile/runtime cleanup and reuse blocked.
        self.cleanup_owner = cleanup_owner
        super().__init__(stage if winerror is None else f"{stage} (winerror={winerror})")


@dataclass(frozen=True)
class JobLimits:
    memory_bytes: int = 256 * 1024 * 1024
    cpu_rate: int = 2000  # 20 percent in the Windows Job 1/100-percent unit.
    active_processes: int = 1

    def __post_init__(self) -> None:
        if type(self.memory_bytes) is not int or not 16 * 1024 * 1024 <= self.memory_bytes <= 2**40:
            raise ValueError("Invalid Job memory limit")
        if type(self.cpu_rate) is not int or not 1 <= self.cpu_rate <= 10000:
            raise ValueError("Invalid Job CPU hard cap")
        if type(self.active_processes) is not int or self.active_processes != 1:
            raise ValueError("Stage-zero production policy allows exactly one process")


@dataclass(frozen=True)
class LaunchSpec:
    executable: Path
    args: tuple[str, ...]
    cwd: Path
    appcontainer_sid: str
    environment: Mapping[str, str]
    limits: JobLimits = field(default_factory=JobLimits)
    low_privilege: bool = False

    def __post_init__(self) -> None:
        if not self.executable.is_absolute() or not self.cwd.is_absolute():
            raise ValueError("Executable and cwd must be absolute")
        if not self.appcontainer_sid.startswith("S-1-15-2-") or "\0" in self.appcontainer_sid:
            raise ValueError("An explicit AppContainer package SID is required")
        if type(self.low_privilege) is not bool:
            raise ValueError("low_privilege must be bool")
        if type(self.args) is not tuple or any(type(arg) is not str or "\0" in arg for arg in self.args):
            raise ValueError("Arguments must be a tuple of NUL-free strings")
        if any("\0" in str(path) for path in (self.executable, self.cwd)):
            raise ValueError("NUL in launch path")
        # LOCALAPPDATA is an explicit stage-zero diagnostic option: standalone
        # probes may supply their NEW AppContainer AC folder or a KnownFolder
        # base resolved through the native API. Never inherit the host variable;
        # supplying directory metadata does not grant filesystem rights.
        allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA"}
        copied: dict[str, str] = {}
        for key, value in self.environment.items():
            if type(key) is not str or type(value) is not str or key.upper() not in allowed:
                raise ValueError("Environment key is outside the fixed whitelist")
            if key.upper() in copied or "\0" in value or not value:
                raise ValueError("Invalid or duplicate environment key")
            copied[key.upper()] = value
        if not {"SYSTEMROOT", "WINDIR"} <= copied.keys():
            raise ValueError("Explicit Windows directory environment is required")
        if len(str(self.executable)) + sum(len(arg) + 3 for arg in self.args) > 30000:
            raise ValueError("Command line budget exceeded")
        if sum(len(key) + len(value) + 2 for key, value in copied.items()) > 16000:
            raise ValueError("Environment budget exceeded")
        object.__setattr__(self, "environment", MappingProxyType(copied))


@dataclass(frozen=True)
class WorkerIdentity:
    pid: int
    appcontainer_sid: str
    is_appcontainer: bool
    low_privilege: bool | None
    capability_count: int
    child_process_policy_flags: int
    job_limit_flags: int
    job_memory_bytes: int
    job_process_memory_bytes: int
    job_active_process_limit: int
    job_cpu_flags: int
    job_cpu_rate: int
    in_job: bool
    inherited_handle_allowlist: tuple[int, ...]
    excluded_handle_canary: int


@dataclass(frozen=True)
class WorkerResult:
    identity: WorkerIdentity
    exit_code: int
    frames: tuple[dict[str, object], ...]
    stdout: bytes
    stderr: bytes
    elapsed_seconds: float


class RunningWorker:
    """Single-owner worker. close() kills and waits, including exception paths.

    collect() runs on a supervisor/background thread, never the GUI thread.
    A transport violation or timeout kills the Job and raises. It does not infer
    business success, cancellation, or side-effect facts from an exit code.
    """

    def __init__(self, native: NativeWorker, identity: WorkerIdentity) -> None:
        self._native = native
        self.identity = identity
        self._collected = False
        self._closed = False

    def __enter__(self) -> RunningWorker:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def collect(self, timeout_seconds: float = 30.0, *,
                frame_limits: FrameLimits = FrameLimits(),
                log_limit_bytes: int = 64 * 1024) -> WorkerResult:
        if self._closed or self._collected:
            raise LaunchRejected("worker_already_consumed")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300:
            raise ValueError("Timeout must be finite and within 300 seconds")
        if type(log_limit_bytes) is not int or not 0 <= log_limit_bytes <= 1024 * 1024:
            raise ValueError("Invalid log budget")
        self._collected = True
        decoder = FrameDecoder(frame_limits)
        frames: list[dict[str, object]] = []
        logs = [bytearray(), bytearray()]
        started = time.monotonic()
        try:
            while True:
                # Each pass consumes at most 3 * 16KiB, so a flood cannot prevent
                # deadline/process checks or monopolize the supervisor forever.
                chunks, eof = self._native.read_available()
                frames.extend(decoder.feed(chunks[0]))
                for index in range(2):
                    data = chunks[index + 1]
                    if len(logs[index]) + len(data) > log_limit_bytes:
                        raise LaunchRejected("log_budget")
                    logs[index].extend(data)
                exit_code = self._native.poll()
                if exit_code is not None and all(eof):
                    decoder.finish()
                    return WorkerResult(self.identity, exit_code, tuple(frames),
                                        bytes(logs[0]), bytes(logs[1]), time.monotonic() - started)
                if time.monotonic() - started >= timeout_seconds:
                    raise LaunchRejected("deadline")
                time.sleep(0.005)
        except BaseException:
            self.close()
            raise

    def terminate(self) -> None:
        """Terminate the entire Job and confirm no active process remains."""
        if not self._closed:
            self._native.terminate()

    def close(self) -> None:
        if not self._closed:
            self._native.close()
            self._closed = True


class WindowsLauncher:
    def launch(self, spec: LaunchSpec) -> RunningWorker:
        """Create suspended, apply/query restrictions, then resume exactly once.

        ``{ipc_write_handle}`` in an argument is replaced with a decimal Win32
        handle. The control stream is output-only in this stage-zero adapter;
        stdin is an EOF pipe. No arbitrary inherited handles are accepted.
        ``{excluded_handle}`` is a synthetic inheritable event omitted from the
        allowlist, for negative inheritance probes. The allowlist is configured
        intent, not an enumeration of the child's actual handle table.
        """
        if sys.platform != "win32":
            raise LaunchRejected("unsupported_platform")
        from ._win32 import create_worker

        native, identity = create_worker(spec)
        return RunningWorker(native, identity)
