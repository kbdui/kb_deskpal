"""Focused nested-Job mocks; no real profile, worker, IOCP, or native calls."""

from __future__ import annotations

import ctypes as C
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import diagnose_worker_job_memory as diagnostic
import diagnose_worker_limits as limits
from deskpal.execution.runtime import ProbeProfile
from deskpal.execution.windows._win32 import Api, BOOL, DWORD, HANDLE, JOB_ACCOUNTING, JOB_CPU, JOB_EXTENDED_LIMIT, SIZE
from deskpal.execution.windows.launcher import JobLimits, LaunchRejected, LaunchSpec, WorkerIdentity, WorkerResult
from diagnose_worker_memory import COMPLETION_ASSOCIATION, SINGLE_PROCESS_LIST

SOURCES = {"synthetic-input": "unchanged"}


def _forbidden(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("native/profile/worker operation forbidden outside explicit fake")


@pytest.fixture(autouse=True)
def _no_native(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(C, "WinDLL", _forbidden, raising=False)
    for name in ("NestedApi", "create_probe_profile", "delete_probe_profile", "_create_worker"):
        monkeypatch.setattr(diagnostic, name, _forbidden)
    monkeypatch.setattr(diagnostic, "_UNCONFIRMED", [])


class _Api:
    def __init__(self, failure: str = "") -> None:
        self.failure = failure
        self.current = 700
        self.pid = 123
        self.key = 0
        self.error = 258
        self.reaped = False
        self.assigned = False
        self.configured = False
        self.associated = False
        self.duplicates = 0
        self.queue: list[tuple[int, int | None, int]] = []
        self.closed: list[int] = []
        self.events: list[str] = []

    def duplicate(self, current: int, source: int, target_process: int, output: Any,
                  rights: int, inherit: bool, options: int) -> int:
        self.duplicates += 1
        assert (current, target_process, inherit, options) == (700, 700, False, 0)
        assert (source, rights) == ((101, 4) if self.duplicates == 1 else (102, 0x101000))
        C.cast(output, C.POINTER(HANDLE)).contents.value = 200 + self.duplicates
        if self.failure == "partial_duplicate":
            raise OSError("synthetic exception after partial duplicate output")
        return 1

    def set_handle_info(self, handle: int, mask: int, value: int) -> int:
        assert handle in (201, 202, 303, 404) and (mask, value) == (1, 0)
        return 1

    def get_handle_info(self, handle: int, output: Any) -> int:
        C.cast(output, C.POINTER(DWORD)).contents.value = 1 if self.failure == "inherit" and handle == 303 else 0
        return 1

    def process_id(self, process: int) -> int:
        assert process == 102
        return self.pid

    def create_job(self, attributes: object, name: object) -> int:
        assert attributes is name is None
        self.events.append("create_inner")
        return 0 if self.failure == "create_inner" else 303

    def create_port(self, handle: Any, existing: object, key: int, threads: int) -> int:
        assert handle.value == HANDLE(-1).value and (existing, key, threads) == (None, 0, 1)
        self.events.append("create_port")
        return 0 if self.failure == "create_port" else 404

    def set_job(self, job: int, kind: int, data: Any, _size: int) -> int:
        assert job == 303 and kind in (7, 9)
        self.events.append("set" + str(kind))
        if kind == 9:
            limits_value = C.cast(data, C.POINTER(JOB_EXTENDED_LIMIT)).contents
            assert limits_value.BasicLimitInformation.LimitFlags == 0x2208
            assert limits_value.BasicLimitInformation.ActiveProcessLimit == 1
            assert limits_value.ProcessMemoryLimit == 0 and limits_value.JobMemoryLimit == 128 * 1024 * 1024
            self.configured = True
        else:
            association = C.cast(data, C.POINTER(COMPLETION_ASSOCIATION)).contents
            assert association.CompletionPort == 404 and association.CompletionKey
            self.key = int(association.CompletionKey)
            self.associated = True
        if self.failure == "set" + str(kind):
            self.error = 5
            return 0
        return 1

    def assign_job(self, job: int, process: int) -> int:
        assert (job, process) == (303, 102) and self.configured and self.associated
        self.events.append("assign_inner")
        self.assigned = True  # A failing wrapper cannot be taken as proof of no assignment.
        if self.failure == "assign":
            self.error = 5
            return 0
        if self.failure != "missing6":
            self.queue.append((6, 1 if self.failure == "key6" else None,
                               self.pid + (1 if self.failure == "pid6" else 0)))
        return 1

    def query_job(self, job: int, kind: int, output: Any, _size: int, _returned: object) -> int:
        assert kind != 7
        inner = job == 303
        label = "inner" if inner else "outer"
        self.events.append(f"query{kind}:" + label)
        if self.failure == label + "_query" and kind == 9:
            self.error = 5
            return 0
        if kind == 1:
            active = 0 if self.reaped or inner and not self.assigned else 1
            if self.reaped and self.failure == "reap_" + label:
                active = 1
            C.cast(output, C.POINTER(JOB_ACCOUNTING)).contents.ActiveProcesses = active
        elif kind == 3:
            pids = C.cast(output, C.POINTER(SINGLE_PROCESS_LIST)).contents
            pids.NumberOfAssignedProcesses = 2 if self.failure == label + "_count" else 1
            pids.NumberOfProcessIdsInList = 1
            pids.ProcessIdList[0] = self.pid + (1 if self.failure == label + "_pid" else 0)
        elif kind == 4:
            C.cast(output, C.POINTER(DWORD)).contents.value = 1 if self.failure == label + "_ui" else 0
        elif kind == 9:
            extended = C.cast(output, C.POINTER(JOB_EXTENDED_LIMIT)).contents
            extended.BasicLimitInformation.LimitFlags = 0x2208 if inner else 0x2308
            extended.BasicLimitInformation.ActiveProcessLimit = 1
            extended.ProcessMemoryLimit = 0 if inner else diagnostic.OUTER_BYTES
            extended.JobMemoryLimit = diagnostic.INNER_BYTES if inner else diagnostic.OUTER_BYTES
            if self.failure == label + "_flags":
                extended.BasicLimitInformation.LimitFlags ^= 0x100
            if self.failure == label + "_active":
                extended.BasicLimitInformation.ActiveProcessLimit = 2
            if self.failure == label + "_process":
                extended.ProcessMemoryLimit += 1
            if self.failure == label + "_memory":
                extended.JobMemoryLimit += 1
        elif kind == 15:
            cpu = C.cast(output, C.POINTER(JOB_CPU)).contents
            cpu.ControlFlags, cpu.CpuRate = 5, 2000
            if self.failure == "cpu":
                cpu.CpuRate = 5000
        else:
            raise AssertionError("unexpected Job query")
        return 1

    def in_job(self, process: int, job: int, output: Any) -> int:
        assert process == 102
        label = "inner" if job == 303 else "outer"
        C.cast(output, C.POINTER(BOOL)).contents.value = 0 if self.failure == label + "_member" else 1
        return 1

    def get_packet(self, port: int, message: Any, key: Any, pid: Any, wait: int) -> int:
        assert port == 404 and wait == 10
        self.events.append("packet")
        if self.failure == "drain" and self.reaped:
            self.error = 5
            return 0
        if not self.queue:
            self.error = 258
            return 0
        code, packet_key, packet_pid = self.queue.pop(0)
        C.cast(message, C.POINTER(DWORD)).contents.value = code
        C.cast(key, C.POINTER(SIZE)).contents.value = self.key if packet_key is None else packet_key
        C.cast(pid, C.POINTER(HANDLE)).contents.value = packet_pid
        return 1

    def wait(self, process: int, timeout: int) -> int:
        assert (process, timeout) == (202, 0)
        self.events.append("wait")
        return 0 if self.reaped and self.failure != "reap_process" else 258

    def close_handle(self, handle: int) -> int:
        self.events.append("close:" + str(handle))
        self.closed.append(handle)
        if self.failure == "close" and handle == 303:
            self.error = 5
            return 0
        return 1

    def windows_directory(self, buffer: Any, _size: int) -> int:
        buffer.value = "C:/Windows"
        return len(buffer.value)


class _Adapter:
    def __init__(self, api: _Api) -> None:
        self.api = api

    def terminate(self) -> None:
        self.api.events.append("outer_terminate")
        if self.api.failure == "terminate":
            raise LaunchRejected("synthetic_terminate", cleanup_owner=self)
        self.api.reaped = True

    def close(self) -> None:
        self.api.events.append("adapter_close")
        if self.api.failure == "adapter_close":
            raise LaunchRejected("synthetic_adapter_close", cleanup_owner=self)


def _owner(monkeypatch: pytest.MonkeyPatch, failure: str = "") -> tuple[_Api, diagnostic.NestedOwner, dict[str, object]]:
    api = _Api(failure)
    monkeypatch.setattr(C, "get_last_error", lambda: api.error, raising=False)
    entry: dict[str, object] = {}
    def persist() -> None:
        json.dumps(entry)

    owner = diagnostic.NestedOwner(cast(Any, api), entry, persist)
    return api, owner, entry


def test_setup_is_inner_only_noninheritable_and_fully_verified_before_real_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    api, owner, _entry = _owner(monkeypatch)
    resumed: list[int] = []

    def original_api(instance: Any) -> None:
        instance.assign_job = lambda _job, _process: 1

        def resume(thread: int) -> int:
            assert owner.binding_verified and api.events.index("set7") < api.events.index("assign_inner")
            assert api.events.index("set9") < api.events.index("assign_inner")
            resumed.append(thread)
            return 1

        instance.resume = resume

    monkeypatch.setattr(Api, "__init__", original_api)
    facade = diagnostic.MemoryLaunchApi(owner)
    assert facade.assign_job(101, 102) == 1
    assert facade.resume(777) == 1 and resumed == [777]
    assert owner.audit["outer_cpu"] == {"flags": 5, "rate": 2000}
    assert cast(dict[str, object], owner.audit["inner_limits"])["process_memory"] == 0
    owner.launch_resolved, owner.adapter = True, cast(Any, _Adapter(api))
    api.queue.append((10, None, api.pid))
    owner.close()
    assert owner.reap_verified and owner.closed and owner.audit["inner_job_memory_limit_event"] == "observed"
    assert api.events.index("wait") < api.events.index("adapter_close") < api.events.index("close:202")
    assert api.closed == [202, 201, 303, 404]


@pytest.mark.parametrize("failure", ["partial_duplicate", "create_inner", "create_port", "inherit", "set9", "set7", "assign", "missing6", "key6", "pid6",
    "outer_query", "inner_query", "outer_member", "inner_member", "outer_count", "inner_count",
    "outer_pid", "inner_pid", "outer_ui", "inner_ui", "outer_flags", "inner_flags", "outer_active", "inner_active",
    "outer_process", "inner_process", "outer_memory", "inner_memory", "cpu"])
def test_all_setup_failures_block_resume_and_preserve_owned_partial_resources(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    api, owner, _entry = _owner(monkeypatch, failure)
    with pytest.raises((LaunchRejected, OSError)):
        owner.bind_suspended(101, 102)
    assert not owner.binding_verified and owner.failed
    assert api.closed == []
    if failure == "partial_duplicate":
        assert owner.job == 201 and owner.process is None and api.duplicates == 1
    assert len(owner.messages) <= 128


@pytest.mark.parametrize("failure", ["reap_outer", "reap_inner", "reap_process", "terminate", "adapter_close", "drain", "close"])
def test_cleanup_three_reap_conditions_and_direct_close_or_drain_failure_never_pass(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    api, owner, entry = _owner(monkeypatch)
    owner.bind_suspended(101, 102)
    owner.launch_resolved, owner.adapter = True, cast(Any, _Adapter(api))
    api.failure = failure
    api.queue.append((10, None, api.pid))
    with pytest.raises(LaunchRejected):
        owner.close()
    assert owner.failed and cast(dict[str, Any], entry["observation"])["unknown"] is True
    if failure in ("reap_outer", "reap_inner", "reap_process", "terminate", "adapter_close"):
        assert api.closed == []
    else:
        assert api.closed == [202, 201, 303, 404]
    calls = list(api.closed)
    with pytest.raises(LaunchRejected):
        owner.close()
    assert api.closed == calls  # No blind repeated closes, including a failed CloseHandle.


@pytest.mark.parametrize("resolved,pending", [(False, False), (True, True)])
def test_unknown_launch_ownership_is_retained_without_discarding_observers(
    monkeypatch: pytest.MonkeyPatch, resolved: bool, pending: bool,
) -> None:
    api, owner, _entry = _owner(monkeypatch)
    owner.bind_suspended(101, 102)
    owner.launch_resolved = resolved
    owner.adapter = cast(Any, _Adapter(api))
    if pending:
        owner.pending.append(cast(Any, _Adapter(api)))
    before = list(api.events)
    with pytest.raises(LaunchRejected) as error:
        owner.close()
    assert error.value.cleanup_owner is owner
    assert api.events == before and owner.inner == 303 and owner.port == 404


def test_persist_failure_before_set_blocks_native_mutation_and_keeps_partial_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    api, owner, _entry = _owner(monkeypatch)

    def persist() -> None:
        if owner.audit["state"] == "inner_set_class9_prepared":
            raise OSError("synthetic durable report failure")

    owner.persist = persist
    with pytest.raises(OSError):
        owner.bind_suspended(101, 102)
    assert "set9" not in api.events and "assign_inner" not in api.events
    assert (owner.job, owner.process, owner.inner) == (201, 202, 303)
    assert owner.failed and owner.audit["unknown"] is True and api.closed == []


def test_last_cleanup_persist_failure_cannot_report_success(monkeypatch: pytest.MonkeyPatch) -> None:
    api, owner, _entry = _owner(monkeypatch)
    owner.bind_suspended(101, 102)
    owner.launch_resolved, owner.adapter = True, cast(Any, _Adapter(api))

    def persist() -> None:
        if owner.audit["state"] == "cleanup_finished":
            raise OSError("synthetic final save failure")

    owner.persist = persist
    with pytest.raises(LaunchRejected) as error:
        owner.close()
    assert error.value.cleanup_owner is owner and owner.closed and owner.failed
    assert owner.audit["unknown"] is True
    assert api.closed == [202, 201, 303, 404]


def test_failed_native_launch_keeps_adapter_cleanup_owner_in_composite_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    api, owner, _entry = _owner(monkeypatch)
    pending = _Adapter(api)
    monkeypatch.setattr(diagnostic, "MemoryLaunchApi", lambda selected: object() if selected is owner else _forbidden())

    def create(_spec: LaunchSpec, *, _api: object) -> NoReturn:
        raise LaunchRejected("synthetic_reap_unknown", cleanup_owner=pending)

    monkeypatch.setattr(diagnostic, "_create_worker", create)
    spec = LaunchSpec(tmp_path / "fixture.exe", ("{ipc_write_handle}", "{excluded_handle}", "smoke"),
                      tmp_path, _result("smoke").identity.appcontainer_sid,
                      {"SystemRoot": "C:/Windows", "WINDIR": "C:/Windows"})
    with pytest.raises(LaunchRejected):
        diagnostic._launch(spec, owner)
    assert owner.pending == [pending] and owner.launch_resolved and not owner.failed_launch_reaped
    with pytest.raises(LaunchRejected) as error:
        owner.close()
    assert error.value.cleanup_owner is owner and api.events == []


@pytest.mark.parametrize("packet", [None, 9, 10, "key", "pid", "duplicate6"])
def test_only_current_inner_key_pid_message10_is_jobwide_evidence(monkeypatch: pytest.MonkeyPatch, packet: object) -> None:
    api, owner, _entry = _owner(monkeypatch)
    owner.bind_suspended(101, 102)
    owner.launch_resolved, owner.adapter = True, cast(Any, _Adapter(api))
    if type(packet) is int:
        api.queue.append((packet, None, api.pid))
    elif packet in ("key", "pid", "duplicate6"):
        api.queue.append((6 if packet == "duplicate6" else 10, 1 if packet == "key" else None,
                          api.pid + (1 if packet == "pid" else 0)))
    if packet in ("key", "pid", "duplicate6"):
        with pytest.raises(LaunchRejected):
            owner.close()
    else:
        owner.close()
    assert owner.audit["inner_job_memory_limit_event"] == ("observed" if packet == 10 else "unknown")


def _result(mode: str) -> WorkerResult:
    count = 0 if mode == "smoke" else 7
    identity = WorkerIdentity(123, "S-1-15-2-1-2-3-4-5-6-7", True, None, 0, 1, 0x2308,
        diagnostic.OUTER_BYTES, diagnostic.OUTER_BYTES, 1, 5, 2000, True, (20, 24, 28, 32), 36)
    frame: dict[str, object] = {"probe": "native_memory_v3", "fixture_version": 3, "mode": mode,
        "excluded_handle_valid": False, "excluded_handle_error": 6, "excluded_handle_exception": 0,
        "allocation_attempted": mode == "bounded-memory", "allocation_count": count,
        "allocation_bytes": count * 16 * 1024 * 1024, "allocation_unit_bytes": 16 * 1024 * 1024,
        "allocation_ceiling_bytes": diagnostic.CEILING, "allocation_error": 0 if mode == "smoke" else 1455,
        "allocation_ceiling_reached": False, "freed_count": count, "freed_bytes": count * 16 * 1024 * 1024,
        "free_error": 0, "all_allocations_freed": True}
    return WorkerResult(identity, 0, (frame,), b"", b"", 0.01)


@pytest.mark.parametrize("event,terminate_failure", [(10, False), (9, False), (None, False), (10, True)])
def test_case_saves_collect_before_termination_and_missing10_remains_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, event: int | None, terminate_failure: bool,
) -> None:
    api = _Api()
    monkeypatch.setattr(C, "get_last_error", lambda: api.error, raising=False)
    entry: dict[str, object] = {}
    saves: list[dict[str, Any]] = []
    mode = "bounded-memory"

    class Worker(_Adapter):
        def collect(self, timeout: float, **kwargs: object) -> WorkerResult:
            assert timeout == 5.0 and kwargs == {"frame_limits": diagnostic.FRAME_LIMITS, "log_limit_bytes": 4096}
            if event is not None:
                api.queue.append((event, None, api.pid))
            return _result(mode)

        def terminate(self) -> None:
            assert "collected" in saves[-1]
            if terminate_failure:
                api.failure = "terminate"
            super().terminate()

    def launch(_spec: LaunchSpec, owner: diagnostic.NestedOwner) -> Any:
        owner.bind_suspended(101, 102)
        owner.launch_resolved, owner.adapter = True, cast(Any, Worker(api))
        return owner.adapter

    monkeypatch.setattr(diagnostic, "_launch", launch)
    spec = LaunchSpec(tmp_path / "fixture.exe", ("{ipc_write_handle}", "{excluded_handle}", mode),
                      tmp_path, _result(mode).identity.appcontainer_sid, {"SystemRoot": "C:/Windows", "WINDIR": "C:/Windows"})
    passed = diagnostic.run_case(spec, cast(Any, api), entry, lambda: saves.append(json.loads(json.dumps(entry))))
    assert passed is (event == 10 and not terminate_failure)
    assert "collected" in entry and entry["outer_256m_job_enforcement_accepted"] is False
    assert entry["stage0_passed"] is False
    assert bool(diagnostic._UNCONFIRMED) is (not passed)


@pytest.mark.parametrize("failure", ["none", "first_case", "sources", "copy", "delete"])
def test_fresh_profile_is_deleted_only_after_both_cases_and_stable_source_exe_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str,
) -> None:
    fixture = tmp_path / "fixed-v3.exe"
    fixture.write_bytes(b"synthetic V3; never executable")
    pin = sha256(fixture.read_bytes()).hexdigest()
    folder = tmp_path / "new-profile"
    folder.mkdir()
    profile = ProbeProfile("deskpal.v06." + "a" * 32, "S-1-15-2-1-2-3-4-5-6-7", str(folder))
    api = _Api()
    monkeypatch.setattr(diagnostic, "FIXTURE", fixture)
    monkeypatch.setattr(diagnostic, "FIXTURE_SHA", pin)
    monkeypatch.setattr(diagnostic, "NestedApi", lambda: api)
    monkeypatch.setattr(diagnostic, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(diagnostic, "fingerprints", lambda: {"changed": "input"} if failure == "sources" else dict(SOURCES))
    monkeypatch.setattr(diagnostic, "create_probe_profile", lambda name: profile if name.startswith("deskpal.v06.") else _forbidden())
    checks: list[Path] = []

    def verify(path: Path) -> None:
        checks.append(path)
        assert path.read_bytes() == fixture.read_bytes()
        if failure == "copy" and path != fixture:
            raise ValueError("synthetic copied fixture pin failure")

    monkeypatch.setattr(diagnostic, "verify_fixture", verify)
    cases: list[str] = []

    def run_case(spec: LaunchSpec, _api: object, entry: dict[str, object], persist: Any) -> bool:
        assert spec.limits == JobLimits() and not spec.low_privilege
        assert {key.upper() for key in spec.environment} == {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA"}
        cases.append(spec.args[-1])
        entry["expected_behavior_observed"] = failure != "first_case"
        persist()
        return failure != "first_case"

    monkeypatch.setattr(diagnostic, "run_case", run_case)
    deleted: list[ProbeProfile] = []

    def delete(selected: ProbeProfile) -> None:
        assert selected is profile and cases == ["smoke", "bounded-memory"]
        deleted.append(selected)
        if failure == "delete":
            raise OSError("synthetic profile delete failure")

    monkeypatch.setattr(diagnostic, "delete_probe_profile", delete)
    report: dict[str, object] = {"source_sha256": dict(SOURCES), "stage0_passed": False}
    writer = SimpleNamespace(save=lambda data: json.dumps(data))
    assert diagnostic.experiment(cast(Any, writer), report) == (0 if failure == "none" else 2)
    assert len(deleted) == (1 if failure in ("none", "delete") else 0)
    assert cases == ([] if failure == "copy" else ["smoke"] if failure == "first_case" else ["smoke", "bounded-memory"])
    assert checks[0] == fixture and report["stage0_passed"] is False


def test_cli_without_optin_is_exclusive_and_never_constructs_native(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report = tmp_path / "new.json"
    monkeypatch.setattr(limits, "_new_report_path", lambda _path: report)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", str(report)])
    assert diagnostic.main() == 0
    saved = json.loads(report.read_text(encoding="ascii"))
    assert saved["status"] == "not_executed_explicit_opt_in_required" and saved["stage0_passed"] is False
    with pytest.raises(FileExistsError):
        diagnostic.main()


def test_bad_fixed_fixture_is_rejected_before_any_native_or_profile_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    writer = SimpleNamespace(save=_forbidden)
    wrong = tmp_path / "synthetic-bad-v3.exe"
    wrong.write_bytes(b"synthetic bytes, never executable")
    monkeypatch.setattr(diagnostic, "FIXTURE", wrong)
    monkeypatch.setattr(diagnostic, "os", SimpleNamespace(name="nt"))
    with pytest.raises(ValueError, match="memory_v3_fingerprint_mismatch"):
        diagnostic.experiment(cast(Any, writer), {})
    assert diagnostic._UNCONFIRMED == []


def test_cli_optin_forwards_exclusive_writer_and_stable_source_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    report = tmp_path / "optin.json"
    monkeypatch.setattr(limits, "_new_report_path", lambda _path: report)
    monkeypatch.setattr(diagnostic, "fingerprints", lambda: dict(SOURCES))
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", str(report), "--experiment"])
    called: list[bool] = []

    def experiment(writer: limits.ReportWriter, data: dict[str, object]) -> int:
        assert writer.path == report and data["source_sha256"] == SOURCES
        called.append(True)
        data.update(status="synthetic_mock_only", cleanup="not_created")
        writer.save(data)
        return 2

    monkeypatch.setattr(diagnostic, "experiment", experiment)
    assert diagnostic.main() == 2 and called == [True]
    saved = json.loads(report.read_text(encoding="ascii"))
    assert saved["source_sha256_end"] == SOURCES and saved["source_unchanged"] is True
    assert saved["stage0_passed"] is False and saved["individual_adapter_handle_closes_verified"] is False


@pytest.mark.parametrize("option", ["--executable", "--sid", "--quota", "--handle", "--mode", "--low-privilege"])
def test_cli_rejects_all_nonfixed_experiment_inputs(monkeypatch: pytest.MonkeyPatch, option: str) -> None:
    monkeypatch.setattr(diagnostic, "ReportWriter", _forbidden)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", "new.json", "--experiment", option, "arbitrary"])
    with pytest.raises(SystemExit) as error:
        diagnostic.main()
    assert error.value.code == 2
