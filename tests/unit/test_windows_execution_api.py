"""Portable launcher contract tests. These are not Windows isolation evidence."""

from __future__ import annotations

from pathlib import Path
import ctypes
import importlib.util
import json
from collections import Counter
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

from deskpal.execution.protocol import ProtocolViolation, encode_frame
from deskpal.execution.windows import (
    JobLimits, LaunchRejected, LaunchSpec, RunningWorker, WindowsLauncher, WorkerIdentity, WorkerResult,
)
from deskpal.execution.windows import _win32
from deskpal.execution.windows._win32 import Api, NativeWorker
from deskpal.execution.windows import diagnostics


def _spec(tmp_path: Path, **changes: object) -> LaunchSpec:
    values: dict[str, object] = {
        "executable": tmp_path / "fixture.exe", "args": ("{ipc_write_handle}",),
        "cwd": tmp_path, "appcontainer_sid": "S-1-15-2-1-2-3-4-5-6-7",
        "environment": {"SystemRoot": "C:\\Windows", "WINDIR": "C:\\Windows"},
    }
    values.update(changes)
    return LaunchSpec(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("environment", [
    {"API_KEY": "synthetic"}, {"PATH": "synthetic"}, {"PYTHONPATH": "synthetic"},
    {"APPDATA": "synthetic"}, {"SystemRoot": "C:\\Windows", "SYSTEMROOT": "C:\\Windows"},
    {"SYSTEMROOT": "C:\\Windows", "WINDIR": "bad\0value"},
])
def test_environment_is_explicit_and_rejects_privileged_host_values(
    tmp_path: Path, environment: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        _spec(tmp_path, environment=environment)


def test_environment_is_frozen_copy(tmp_path: Path) -> None:
    environment = {"SystemRoot": "C:\\Windows", "WINDIR": "C:\\Windows"}
    spec = _spec(tmp_path, environment=environment)
    environment["API_KEY"] = "synthetic"
    assert dict(spec.environment) == {"SYSTEMROOT": "C:\\Windows", "WINDIR": "C:\\Windows"}


@pytest.mark.parametrize("values", [
    {"active_processes": 2}, {"active_processes": True}, {"cpu_rate": 0},
    {"cpu_rate": 10001}, {"memory_bytes": 1},
])
def test_required_job_limits_cannot_be_disabled(values: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        JobLimits(**values)


def _identity() -> WorkerIdentity:
    return WorkerIdentity(123, "S-1-15-2-1-2-3-4-5-6-7", True, None, 0,
                          1, 0x2308, 256 * 1024 * 1024, 256 * 1024 * 1024,
                          1, 5, 2000, True, (20, 24, 28, 32), 36)


class FakeNative:
    def __init__(self, chunks: list[tuple[bytes, bytes, bytes]], *, done: bool = True) -> None:
        self.chunks = chunks
        self.done = done
        self.closed = False
        self.terminated = False
        self.close_failure = False

    def read_available(self) -> tuple[tuple[bytes, bytes, bytes], tuple[bool, bool, bool]]:
        if self.chunks:
            return self.chunks.pop(0), (False, False, False)
        return (b"", b"", b""), (self.done, self.done, self.done)

    def poll(self) -> int | None:
        return 0 if self.done else None

    def close(self) -> None:
        if self.close_failure:
            raise LaunchRejected("synthetic_reap_failure")
        self.closed = True

    def terminate(self) -> None:
        self.terminated = True


def _worker(native: FakeNative) -> RunningWorker:
    return RunningWorker(cast(NativeWorker, native), _identity())


def test_output_control_is_separate_from_logs_and_drains_after_exit() -> None:
    frame = encode_frame({"probe": True})
    native = FakeNative([(frame[:3], b"out", b"err"), (frame[3:], b"", b"")])
    with _worker(native) as worker:
        result = worker.collect(1)
    assert result.frames == ({"probe": True},)
    assert result.stdout == b"out"
    assert result.stderr == b"err"
    assert native.closed


def test_flood_budget_kills_and_closes_worker() -> None:
    native = FakeNative([(b"", b"x" * 100, b"")], done=False)
    with pytest.raises(LaunchRejected, match="log_budget"):
        _worker(native).collect(1, log_limit_bytes=99)
    assert native.closed


@pytest.mark.parametrize("payload", [b"\xff\xff\xff\xff", b"\x00\x00\x00\x10{}"])
def test_malformed_or_truncated_control_kills_and_closes(payload: bytes) -> None:
    native = FakeNative([(payload, b"", b"")])
    with pytest.raises(ProtocolViolation):
        _worker(native).collect(1)
    assert native.closed


def test_wall_deadline_cleans_up_nonresponsive_worker() -> None:
    native = FakeNative([], done=False)
    with pytest.raises(LaunchRejected, match="deadline"):
        _worker(native).collect(0.001)
    assert native.closed


def test_cleanup_failure_remains_retryable() -> None:
    native = FakeNative([])
    native.close_failure = True
    worker = _worker(native)
    with pytest.raises(LaunchRejected, match="synthetic_reap_failure"):
        worker.close()
    native.close_failure = False
    worker.close()
    assert native.closed


def test_native_cleanup_failure_keeps_job_and_process_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []

    class FakeApi:
        def close(self, handle: int) -> None:
            closed.append(handle)

    def fail(*args: object) -> None:
        raise LaunchRejected("reap_deadline")

    monkeypatch.setattr("deskpal.execution.windows._win32._terminate_and_wait", fail)
    native = NativeWorker(cast(Any, FakeApi()), 1, 2, (3, 4, 5), 6)
    with pytest.raises(LaunchRejected) as failure:
        native.close()
    assert failure.value.cleanup_owner is native
    assert closed == []
    monkeypatch.setattr("deskpal.execution.windows._win32._terminate_and_wait", lambda *args: None)
    native.close()
    assert set(closed) == {1, 2, 3, 4, 5, 6}


class _CreateWorkerApi:
    """Minimal deterministic API double for _create_worker ownership paths."""

    def __init__(self, *, create_error: BaseException | None = None,
                 assign_result: int = 1, resume_result: int = 1,
                 fail_terminate_once: bool = False) -> None:
        self.events: list[tuple[object, ...]] = []
        self.closed: list[int] = []
        self.allocated = [101]  # event canary
        self._next_pipe_handle = 200
        self.process_handle = 701
        self.thread_handle = 702
        self.job_handle = 900
        self.create_error = create_error
        self.assign_result = assign_result
        self.resume_result = resume_result
        self.fail_terminate_once = fail_terminate_once
        self.terminated = False

    def parse_sid(self, _sid: str, output: Any) -> int:
        ctypes.cast(output, ctypes.POINTER(_win32.PTR)).contents.value = 77
        return 1

    def local_free(self, sid: object) -> None:
        self.events.append(("local_free", sid))

    def create_event(self, *_args: object) -> int:
        return self.allocated[0]

    def create_pipe(self, read: Any, write: Any, *_args: object) -> int:
        read_value, write_value = self._next_pipe_handle, self._next_pipe_handle + 1
        self._next_pipe_handle += 2
        ctypes.cast(read, ctypes.POINTER(_win32.HANDLE)).contents.value = read_value
        ctypes.cast(write, ctypes.POINTER(_win32.HANDLE)).contents.value = write_value
        self.allocated.extend((read_value, write_value))
        return 1

    def handle_info(self, *_args: object) -> int:
        return 1

    def close(self, handle: int) -> None:
        self.events.append(("close", handle))
        self.closed.append(handle)

    def init_attrs(self, attrs: Any, _count: int, _flags: int, size_out: Any) -> int:
        if attrs is None:
            ctypes.cast(size_out, ctypes.POINTER(_win32.SIZE)).contents.value = 8
            ctypes.set_last_error(122)
        return 1

    def update_attr(self, *_args: object) -> int:
        return 1

    def create_job(self, *_args: object) -> int:
        self.allocated.append(self.job_handle)
        return self.job_handle

    def set_job(self, *_args: object) -> int:
        return 1

    def create_process(self, *_args: Any) -> int:
        information_pointer = _args[9]
        information = ctypes.cast(
            information_pointer, ctypes.POINTER(_win32.PROCESS_INFORMATION),
        ).contents
        information.hProcess = self.process_handle
        information.hThread = self.thread_handle
        information.dwProcessId = 123
        information.dwThreadId = 124
        self.allocated.extend((self.process_handle, self.thread_handle))
        self.events.append(("create_process_filled", self.process_handle, self.thread_handle))
        if self.create_error is not None:
            raise self.create_error
        return 1

    def assign_job(self, *_args: object) -> int:
        self.events.append(("assign_job",))
        return self.assign_result

    def terminate_process(self, process: int, code: int) -> int:
        self.events.append(("terminate_process", process, code))
        if self.fail_terminate_once:
            self.fail_terminate_once = False
            return 0
        self.terminated = True
        return 1

    def terminate_job(self, job: int, code: int) -> int:
        self.events.append(("terminate_job", job, code))
        self.terminated = True
        return 1

    def wait(self, process: int, timeout: int) -> int:
        self.events.append(("wait", process, timeout))
        return 0 if self.terminated else 258

    def query_job(self, _job: int, _kind: int, output: Any, _size: int, _needed: object) -> int:
        accounting = ctypes.cast(output, ctypes.POINTER(_win32.JOB_ACCOUNTING)).contents
        accounting.ActiveProcesses = 0
        return 1

    def resume(self, _thread: object) -> int:
        self.events.append(("resume",))
        return self.resume_result

    def delete_attrs(self, _attrs: object) -> None:
        self.events.append(("delete_attrs",))


def _launch_with_fake_api(tmp_path: Path, api: _CreateWorkerApi) -> None:
    _win32._create_worker(_spec(tmp_path), _api=cast(Api, api))


def test_create_wrapper_raising_after_filling_process_information_reaps_then_closes(
    tmp_path: Path,
) -> None:
    original = RuntimeError("synthetic wrapper failure after PI fill")
    api = _CreateWorkerApi(create_error=original)

    with pytest.raises(RuntimeError) as failure:
        _launch_with_fake_api(tmp_path, api)

    assert failure.value is original
    create_index = next(i for i, event in enumerate(api.events) if event[0] == "create_process_filled")
    terminate_index = next(i for i, event in enumerate(api.events) if event[0] == "terminate_process")
    final_wait_index = max(i for i, event in enumerate(api.events) if event[0] == "wait")
    preclosed_handle = api.closed[0]
    owned_close_indices = [i for i, event in enumerate(api.events)
                           if event[0] == "close" and event[1] != preclosed_handle]
    assert create_index < terminate_index < final_wait_index < min(owned_close_indices)
    assert Counter(api.closed) == Counter(api.allocated)


def test_create_wrapper_reap_failure_retains_all_handles_for_one_retry(
    tmp_path: Path,
) -> None:
    original = RuntimeError("synthetic wrapper failure after PI fill")
    api = _CreateWorkerApi(create_error=original, fail_terminate_once=True)

    with pytest.raises(LaunchRejected, match="launch_failure_reap_unconfirmed") as failure:
        _launch_with_fake_api(tmp_path, api)

    cleanup_owner = failure.value.cleanup_owner
    assert cleanup_owner is not None
    owner = cast(_win32.PendingLaunchCleanup, cleanup_owner)
    assert owner.process == api.process_handle
    preclosed_handle = api.closed[0]
    assert owner.handles == tuple(handle for handle in api.allocated if handle != preclosed_handle)
    assert failure.value.__cause__ is original
    assert api.closed == [preclosed_handle]  # Only the stdin writer, closed before process creation.

    owner.close()

    assert Counter(api.closed) == Counter(api.allocated)
    assert len(api.closed) == len(set(api.closed))
    assert owner.handles == ()


@pytest.mark.parametrize("failure_point", ["assign", "verify", "resume"])
def test_post_create_failures_close_each_handle_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str,
) -> None:
    api = _CreateWorkerApi(assign_result=0 if failure_point == "assign" else 1,
                           resume_result=0 if failure_point == "resume" else 1)

    if failure_point == "verify":
        def fail_verify(*_args: object) -> WorkerIdentity:
            raise LaunchRejected("synthetic_verify_failure")

        monkeypatch.setattr(_win32, "_verify", fail_verify)
    else:
        monkeypatch.setattr(_win32, "_verify", lambda *_args: _identity())

    expected_stage = {
        "assign": "assign_job",
        "verify": "synthetic_verify_failure",
        "resume": "resume_thread",
    }[failure_point]
    with pytest.raises(LaunchRejected, match=expected_stage):
        _launch_with_fake_api(tmp_path, api)

    assert Counter(api.closed) == Counter(api.allocated)
    assert len(api.closed) == len(set(api.closed))


def test_cannot_collect_twice() -> None:
    native = FakeNative([])
    worker = _worker(native)
    worker.collect(1)
    with pytest.raises(LaunchRejected, match="already_consumed"):
        worker.collect(1)
    worker.close()


def test_unsupported_platform_never_calls_a_fallback(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("deskpal.execution.windows.launcher.sys.platform", "linux")
    with pytest.raises(LaunchRejected, match="unsupported_platform"):
        WindowsLauncher().launch(_spec(tmp_path))


def test_diagnostic_never_calls_native_resume_and_disables_unlisted_inheritance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deskpal.execution.windows._win32 import PROCESS_INFORMATION, STARTUPINFOEX

    native_resume_calls: list[int] = []
    create_calls: list[tuple[object, ...]] = []

    def fake_init(api: Any) -> None:
        api.update_attr = lambda *args: 1

        def create(*args: Any) -> int:
            create_calls.append(args)
            ctypes.cast(args[9], ctypes.POINTER(PROCESS_INFORMATION)).contents.dwProcessId = 123
            return 1

        api.create_process = create
        api.resume = lambda handle: native_resume_calls.append(handle)

    monkeypatch.setattr(Api, "__init__", fake_init)
    api = diagnostics._DiagnosticApi("no_handles", frozenset({0x20002}))
    startup = STARTUPINFOEX()
    startup.StartupInfo.dwFlags = 0x100
    startup.StartupInfo.hStdInput = 20
    information = PROCESS_INFORMATION()
    assert api.create_process(None, None, None, None, True, 4, None, None,
                              ctypes.byref(startup), ctypes.byref(information)) == 1
    assert create_calls[0][4] is False
    assert startup.StartupInfo.dwFlags == 0
    assert startup.StartupInfo.hStdInput is None
    with pytest.raises(LaunchRejected, match="diagnostic_resume_blocked"):
        api.resume(24)
    assert native_resume_calls == []
    assert api.resume_blocked


def test_diagnostic_aborts_matrix_and_preserves_unconfirmed_cleanup_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = FakeNative([])
    called: list[str] = []

    def fake_init(api: Any) -> None:
        api.update_attr = lambda *args: 1
        api.create_process = lambda *args: 0
        api.resume = lambda *args: 1

    def unreaped(*args: object, **kwargs: object) -> object:
        called.append("create")
        raise LaunchRejected("launch_failure_reap_unconfirmed", cleanup_owner=owner)

    monkeypatch.setattr(Api, "__init__", fake_init)
    monkeypatch.setattr(diagnostics, "_create_worker", unreaped)
    with pytest.raises(LaunchRejected) as failure:
        diagnostics.diagnose_suspended_creation(_spec(tmp_path))
    assert failure.value.cleanup_owner is owner
    assert called == ["create"]


def _mock_profile_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
                        delete_fails: bool = False,
                        worker_result: WorkerResult | None = None) -> tuple[ModuleType, Path, Path, list[object]]:
    """All native/profile/environment operations are replaced before invocation."""
    from deskpal.execution import runtime

    script = Path(__file__).resolve().parents[2] / "scripts" / "probe_windows_launch.py"
    module_spec = importlib.util.spec_from_file_location("deskpal_native_probe_test", script)
    assert module_spec is not None and module_spec.loader is not None
    probe = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(probe)
    monkeypatch.setattr(probe, "__file__", str(tmp_path / "scripts/probe_windows_launch.py"))
    source = tmp_path / "data/fixture.exe"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic fixture; never executed")
    report = tmp_path / "data/validation/v06/report.json"
    folder = tmp_path / "owned-profile/AC"
    folder.mkdir(parents=True)
    profile = runtime.ProbeProfile("deskpal.v06.12345678", "S-1-15-2-1-2-3-4-5-6-7", str(folder))
    deleted: list[object] = []

    def delete(item: object) -> None:
        deleted.append(item)
        if delete_fails:
            raise OSError("synthetic profile deletion failure")

    class CompletedWorker:
        def __enter__(self) -> CompletedWorker:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def collect(self, timeout: float) -> WorkerResult:
            return (worker_result if worker_result is not None else
                    WorkerResult(_identity(), 0, ({"probe": "native"},), b"", b"", 0.01))

    monkeypatch.setattr(runtime, "create_probe_profile", lambda name: profile)
    monkeypatch.setattr(runtime, "delete_probe_profile", delete)
    monkeypatch.setattr(probe, "windows_directory", lambda: "C:\\Windows")
    monkeypatch.setattr(probe, "_known_local_appdata", lambda: "C:\\SyntheticLocalData")
    monkeypatch.setattr(probe, "_set_diagnostic_caller_localappdata", lambda value: {})
    monkeypatch.setattr(probe.sys, "getwindowsversion", lambda: (10, 0, 26200), raising=False)
    monkeypatch.setattr(probe, "WindowsLauncher",
                        lambda: SimpleNamespace(launch=lambda spec: CompletedWorker()))
    return probe, source, report, deleted


@pytest.mark.parametrize("delete_fails", [False, True])
def test_native_probe_first_report_failure_still_deletes_owned_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delete_fails: bool,
) -> None:
    probe, source, report, deleted = _mock_profile_probe(tmp_path, monkeypatch, delete_fails=delete_fails)
    original_write = Path.write_text
    writes = 0

    def first_write_fails(path: Path, text: str, *args: Any, **kwargs: Any) -> int:
        nonlocal writes
        if path == report:
            writes += 1
            if writes == 1:
                raise OSError("synthetic first report write failure")
        return original_write(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", first_write_fails)
    with pytest.raises(OSError, match="first report write failure"):
        probe._native_profile_diagnostic(source, report, caller_mode="absent", parameter_matrix=False)
    assert len(deleted) == 1
    body = json.loads(report.read_text(encoding="utf-8"))
    if delete_fails:
        assert body["cleanup"]["status"] == "failed"
    else:
        assert body["cleanup"] == "profile_deleted"


def test_native_probe_deletion_failure_cannot_return_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe, source, report, deleted = _mock_profile_probe(tmp_path, monkeypatch, delete_fails=True)
    assert probe._native_profile_diagnostic(source, report, caller_mode="known-folder",
                                             parameter_matrix=False) == 2
    assert len(deleted) == 1
    body = json.loads(report.read_text(encoding="utf-8"))
    assert body["status"] == "blocked_profile_cleanup_failed"
    assert body["cleanup"]["status"] == "failed"
    assert len(body["runs"]) == 2
    assert all(run["exit_code"] == 0 for run in body["runs"])


def test_native_probe_caller_restore_failure_still_attempts_profile_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe, source, report, deleted = _mock_profile_probe(tmp_path, monkeypatch)
    changes = 0

    def restore_fails(value: str | None) -> dict[str, object]:
        nonlocal changes
        changes += 1
        if changes == 3:
            raise OSError("synthetic caller restore failure")
        return {}

    monkeypatch.setattr(probe, "_set_diagnostic_caller_localappdata", restore_fails)
    assert probe._native_profile_diagnostic(source, report, caller_mode="absent",
                                             parameter_matrix=False) == 2
    assert len(deleted) == 1
    body = json.loads(report.read_text(encoding="utf-8"))
    assert body["cleanup"] == "profile_deleted"
    assert body["caller_restore_error"]["error_type"] == "OSError"


def test_native_probe_dll_initialization_failure_is_not_smoke_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = WorkerResult(_identity(), 0xC0000142, (), b"", b"", 0.01)
    probe, source, report, deleted = _mock_profile_probe(tmp_path, monkeypatch, worker_result=result)
    assert probe._native_profile_diagnostic(source, report, caller_mode="known-folder",
                                             parameter_matrix=False) == 2
    assert len(deleted) == 1
    body = json.loads(report.read_text(encoding="utf-8"))
    assert body["cleanup"] == "profile_deleted"
    assert body["status"] == "startup_rejected"
    assert all(run["startup_status"] == "application_startup_failed" for run in body["runs"])
    assert all(run["startup_failure"] == "nonzero_exit" for run in body["runs"])
    assert all(run["frames"] == [] for run in body["runs"])
