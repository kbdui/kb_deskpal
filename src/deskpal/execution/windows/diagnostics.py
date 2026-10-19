"""Experimental, suspended-only CreateProcess parameter diagnosis.

Use only in a single-purpose synthetic probe process with a newly provisioned
project-owned AppContainer/runtime. This is not a launch mode or a fallback.
Every ResumeThread call is replaced by an exception before reaching Win32; even
an explicitly non-AppContainer control is killed while still suspended. The
normal launcher remains unchanged and always requires all restrictions.

Profile/ACL provisioning and any native experiment approval remain the caller's
responsibility. This module never changes caller or worker environment values.
"""

from __future__ import annotations

import ctypes as C
from typing import Any

from ._win32 import (
    Api, PROCESS_INFORMATION, STARTUPINFOEX, _CREATE_LOCK, _create_worker,
)
from .launcher import LaunchRejected, LaunchSpec


_MATRIX: tuple[tuple[str, frozenset[int]], ...] = (
    ("all_restrictions", frozenset()),
    ("without_child_policy", frozenset({0x2000E})),
    ("without_inherited_handles", frozenset({0x20002})),
    ("appcontainer_only_no_handles", frozenset({0x20002, 0x2000E})),
    ("explicit_non_appcontainer_suspended_control", frozenset({0x20009})),
    ("explicit_no_attributes_suspended_control", frozenset({0x20009, 0x20002, 0x2000E})),
)


class _DiagnosticApi(Api):
    def __init__(self, mode: str, omitted: frozenset[int]) -> None:
        super().__init__()
        self.mode = mode
        self.omitted = omitted
        self.created = False
        self.pid: int | None = None
        self.creation_error: int | None = None
        self.resume_blocked = False
        self.applied_attributes: list[int] = []
        self._real_update = self.update_attr
        self._real_create = self.create_process
        self.update_attr = self._update_attribute
        self.create_process = self._create_suspended
        # Never retain or expose a callable native ResumeThread in this object.
        self.resume = self._block_resume

    def _update_attribute(self, attributes: Any, flags: Any, attribute: int,
                          value: Any, size: Any, previous: Any, returned: Any) -> int:
        if attribute in self.omitted:
            return 1
        self.applied_attributes.append(attribute)
        return int(self._real_update(attributes, flags, attribute, value, size, previous, returned))

    def _create_suspended(self, *arguments: Any) -> int:
        args = list(arguments)
        if not int(args[5]) & 0x4:
            raise LaunchRejected("diagnostic_requires_create_suspended")
        if 0x20002 in self.omitted:
            # Removing the explicit list must also disable all inheritance.
            # Otherwise a diagnostic child could acquire unrelated host handles.
            args[4] = False
            startup = C.cast(args[8], C.POINTER(STARTUPINFOEX)).contents
            startup.StartupInfo.dwFlags = 0
            startup.StartupInfo.hStdInput = None
            startup.StartupInfo.hStdOutput = None
            startup.StartupInfo.hStdError = None
        result = int(self._real_create(*args))
        self.creation_error = None if result else C.get_last_error()
        self.created = bool(result)
        if result:
            information = C.cast(args[9], C.POINTER(PROCESS_INFORMATION)).contents
            self.pid = int(information.dwProcessId)
        return result

    def _block_resume(self, handle: object) -> int:
        self.resume_blocked = True
        raise LaunchRejected("diagnostic_resume_blocked_before_native_call")


def diagnose_suspended_creation(spec: LaunchSpec) -> dict[str, object]:
    """Return a fixed six-case parameter matrix; no child executes user code.

    A creation that succeeds still has its token/Job queried by the normal code
    where applicable, and is then terminated and reaped. A cleanup uncertainty
    is immediately raised with its live cleanup_owner, aborting the matrix; the
    caller must retain it and block profile deletion/reuse. Successful cases are
    evidence about CreateProcess acceptance, never isolation/tool execution.
    """
    if spec.low_privilege:
        raise ValueError("Diagnose ordinary AppContainer parameters before LPAC")
    cases: list[dict[str, object]] = []
    with _CREATE_LOCK:
        for mode, omitted in _MATRIX:
            api = _DiagnosticApi(mode, omitted)
            try:
                native, _ = _create_worker(spec, _api=api)
            except LaunchRejected as error:
                if error.cleanup_owner is not None:
                    # Do not serialize or discard the only native handle owner.
                    raise
                cases.append({
                    "mode": mode, "created_suspended": api.created,
                    "creation_winerror": api.creation_error, "pid": api.pid,
                    "stopped_stage": error.stage, "stage_winerror": error.winerror,
                    "resume_blocked": api.resume_blocked, "native_resume_called": False,
                    "reap_confirmed": api.created,
                    "applied_attributes": [hex(value) for value in api.applied_attributes],
                })
            else:
                # Defensive invariant: the diagnostic resume stub must make the
                # ordinary launch return impossible. Retain normal cleanup even
                # if a future refactor accidentally changes that control flow.
                native.close()
                raise LaunchRejected("diagnostic_unexpected_launch_return")
    return {"experimental": True, "worker_code_executed": False,
            "child_environment_keys": sorted(spec.environment), "cases": cases}
