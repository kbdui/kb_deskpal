"""Pure mock checks: no real worker, IOCP, profile, ACL or native calls."""

from __future__ import annotations

import ctypes as C
from hashlib import sha256
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import diagnose_worker_memory as diagnostic
from deskpal.execution.runtime import ProbeProfile
from deskpal.execution.windows import JobLimits, LaunchRejected, LaunchSpec, WorkerIdentity, WorkerResult
from deskpal.execution.windows import _win32


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> Any:
    def no_native(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("pure_mock_test_attempted_native_or_profile_operation")
    monkeypatch.setattr(C, "WinDLL", no_native, raising=False)
    monkeypatch.setattr(diagnostic, "create_probe_profile", no_native)
    monkeypatch.setattr(diagnostic, "delete_probe_profile", no_native)
    diagnostic._UNCONFIRMED_OWNERS.clear()
    yield
    diagnostic._UNCONFIRMED_OWNERS.clear()


class FakeApi:
    def __init__(self) -> None:
        self.current = 700
        self.key = 0
        self.pid = 123
        self.active = 0
        self.wait_status = 0
        self.flags = 0x2308
        self.memory_bytes = 256 * 1024 * 1024
        self.pid_count = 1
        self.member = 1
        self.fail = ""
        self.close_fail: set[int] = set()
        self.once_close_fail: set[int] = set()
        self.duplicates: list[tuple[int, int, bool, int]] = []
        self.close_calls: list[int] = []
        self.queries: list[int] = []
        self.set_kinds: list[int] = []
        self.packet_calls: list[int] = []
        self.queue: list[tuple[int, int | None, int] | str] = [(6, None, self.pid)]
        self.events: list[str] = []

    def duplicate(self, _current: int, source: int, _target_process: int, target: Any,
                  access: int, inherit: bool, options: int) -> int:
        self.duplicates.append((source, access, inherit, options))
        if self.fail == "duplicate" + str(len(self.duplicates)):
            C.set_last_error(5)
            return 0
        C.cast(target, C.POINTER(_win32.HANDLE)).contents.value = 800 + len(self.duplicates)
        return 1

    def close_handle(self, handle: int) -> int:
        self.close_calls.append(handle)
        self.events.append("close:" + str(handle))
        if handle in self.close_fail or handle in self.once_close_fail:
            self.once_close_fail.discard(handle)
            C.set_last_error(5)
            return 0
        return 1

    def query_job(self, _job: int, kind: int, output: Any, _size: int, _needed: Any) -> int:
        self.queries.append(kind)
        if self.fail == "query" + str(kind):
            C.set_last_error(5)
            return 0
        if kind == 1:
            C.cast(output, C.POINTER(_win32.JOB_ACCOUNTING)).contents.ActiveProcesses = self.active
        elif kind == 3:
            pids = C.cast(output, C.POINTER(diagnostic.SINGLE_PROCESS_LIST)).contents
            pids.NumberOfAssignedProcesses = self.pid_count
            pids.NumberOfProcessIdsInList = self.pid_count
            pids.ProcessIdList[0] = self.pid + (1 if self.fail == "wrongpid" else 0)
        elif kind == 9:
            limits = C.cast(output, C.POINTER(_win32.JOB_EXTENDED_LIMIT)).contents
            limits.BasicLimitInformation.LimitFlags = self.flags
            limits.BasicLimitInformation.ActiveProcessLimit = 1
            limits.ProcessMemoryLimit = self.memory_bytes
            limits.JobMemoryLimit = self.memory_bytes
        return 1

    def process_id(self, _process: int) -> int:
        return 0 if self.fail == "pid" else self.pid

    def create_port(self, _file: Any, port: Any, key: int, concurrency: int) -> int:
        assert port is None and key == 0 and concurrency == 1
        return 0 if self.fail == "port" else 803

    def set_handle_info(self, handle: int, mask: int, flags: int) -> int:
        assert (handle, mask, flags) == (803, 1, 0)
        return int(self.fail != "clearinherit")

    def get_handle_info(self, _handle: int, output: Any) -> int:
        C.cast(output, C.POINTER(_win32.DWORD)).contents.value = int(self.fail == "inherited")
        return int(self.fail != "getinherit")

    def set_job(self, _job: int, kind: int, output: Any, size: int) -> int:
        assert size == C.sizeof(diagnostic.COMPLETION_ASSOCIATION)
        association = C.cast(output, C.POINTER(diagnostic.COMPLETION_ASSOCIATION)).contents
        self.key = int(association.CompletionKey or 0)
        assert association.CompletionPort == 803
        self.set_kinds.append(kind)
        return int(self.fail != "associate")

    def get_packet(self, _port: Any, message: Any, key: Any, pid: Any, wait: int) -> int:
        self.events.append("packet")
        self.packet_calls.append(wait)
        packet = self.queue.pop(0) if self.queue else "timeout"
        if packet in ("timeout", "error"):
            C.set_last_error(258 if packet == "timeout" else 5)
            return 0
        assert isinstance(packet, tuple)
        code, packet_key, packet_pid = packet
        C.cast(message, C.POINTER(_win32.DWORD)).contents.value = code
        C.cast(key, C.POINTER(_win32.SIZE)).contents.value = self.key if packet_key is None else packet_key
        C.cast(pid, C.POINTER(_win32.HANDLE)).contents.value = packet_pid
        return 1

    def in_job(self, _process: int, _job: int, result: Any) -> int:
        C.cast(result, C.POINTER(_win32.BOOL)).contents.value = self.member
        return int(self.fail != "membership")

    def wait(self, _process: int, _timeout: int) -> int:
        return self.wait_status

    def exit_code(self, _process: int, code: Any) -> int:
        C.cast(code, C.POINTER(_win32.DWORD)).contents.value = 0
        return 1


def observation(api: FakeApi | None = None) -> tuple[FakeApi, diagnostic.MemoryObservation]:
    selected = api or FakeApi()
    return selected, diagnostic.MemoryObservation(cast(Any, selected))


def identity() -> WorkerIdentity:
    return WorkerIdentity(123, "S-1-15-2-1-2-3-4-5-6-7", True, None, 0, 1, 0x2308,
                          256 * 1024 * 1024, 256 * 1024 * 1024, 1, 5, 2000, True, (20, 24, 28, 32), 36)


def frame(mode: str = "bounded-memory", count: int = 15, error: int = 8) -> dict[str, object]:
    return {"probe": "native_memory_v3", "fixture_version": 3, "mode": mode,
            "excluded_handle_valid": False, "excluded_handle_error": 0,
            "excluded_handle_exception": 0xc0000008, "allocation_attempted": mode == "bounded-memory",
            "allocation_count": count, "allocation_bytes": count * diagnostic.UNIT,
            "allocation_unit_bytes": diagnostic.UNIT, "allocation_ceiling_bytes": diagnostic.CEILING,
            "allocation_error": error, "allocation_ceiling_reached": count == 32,
            "freed_count": count, "freed_bytes": count * diagnostic.UNIT,
            "free_error": 0, "all_allocations_freed": True}


def result(value: dict[str, object] | None = None) -> WorkerResult:
    return WorkerResult(identity(), 0, (frame() if value is None else value,), b"", b"", 0.01)


def spec(tmp_path: Path, mode: str = "bounded-memory", *, limits: JobLimits | None = None) -> LaunchSpec:
    return LaunchSpec(tmp_path / "fixture.exe", ("{ipc_write_handle}", "{excluded_handle}", mode),
                      tmp_path, identity().appcontainer_sid, {"SYSTEMROOT": "C:\\Windows", "WINDIR": "C:\\Windows"},
                      limits=limits or JobLimits())


def test_binding_checks_host_port_set7_new_process_membership_pid_list_limits_without_query7() -> None:
    api, observer = observation()
    observer.bind_suspended(701, 702)
    assert observer.binding_verified
    assert api.duplicates == [(701, 4, False, 0), (702, 0x101000, False, 0)]
    assert api.set_kinds == [7]
    assert api.queries == [3, 9]
    assert observer.messages == [{"message": 6, "key": observer.key, "pid": 123}]


@pytest.mark.parametrize("failure", ["duplicate1", "duplicate2", "pid", "port", "clearinherit",
                                    "getinherit", "inherited", "associate", "membership", "query3", "query9", "wrongpid"])
def test_binding_fails_closed_and_retains_each_acquired_handle(failure: str) -> None:
    api, observer = observation()
    api.fail = failure
    with pytest.raises(LaunchRejected):
        observer.bind_suspended(701, 702)
    assert observer.binding_verified is False
    # Adapter must reap first; this test has no real process to reclaim.
    observer.close()
    expected = ([801] if failure == "duplicate2" else [] if failure == "duplicate1" else [802, 801])
    if failure not in ("duplicate1", "duplicate2", "pid", "port"):
        expected.append(803)
    assert api.close_calls == expected


@pytest.mark.parametrize("kind", ["member", "multi", "flags", "memory", "nonew", "wrongkey", "wrongpacketpid", "earlyevent"])
def test_binding_rejects_unconfirmed_or_mismatched_evidence(kind: str) -> None:
    api, observer = observation()
    if kind == "member":
        api.member = 0
    elif kind == "multi":
        api.pid_count = 2
    elif kind == "flags":
        api.flags = 0x2300
    elif kind == "memory":
        api.memory_bytes = diagnostic.CEILING
    elif kind == "nonew":
        api.queue.clear()
    elif kind == "wrongkey":
        api.queue = [(6, 1, 123)]
    elif kind == "wrongpacketpid":
        api.queue = [(6, None, 124)]
    else:
        api.queue = [(9, None, 123)]
    with pytest.raises(LaunchRejected):
        observer.bind_suspended(701, 702)
    assert not observer.binding_verified
    assert len(api.packet_calls) <= 32
    assert 7 not in api.queries


@pytest.mark.parametrize("code", [None, 9, 10])
def test_absent_notifications_remain_unknown_and_9_never_means_job_wide(code: int | None) -> None:
    api, observer = observation()
    observer.bind_suspended(701, 702)
    if code is not None:
        api.queue.append((code, None, 123))
    observer.close()
    evidence = observer.evidence()
    assert evidence["process_memory_limit_event"] == ("observed" if code == 9 else "unknown")
    assert evidence["job_memory_limit_event"] == ("observed" if code == 10 else "unknown")
    assert evidence["delivery_guaranteed"] is False


@pytest.mark.parametrize("packets", ["flood", "wrongkey", "wrongpid", "apierror", "unknownmessage"])
def test_failed_drain_invalidates_attribution_but_closes_reaped_resources(packets: str) -> None:
    api, observer = observation()
    observer.bind_suspended(701, 702)
    if packets == "flood":
        api.queue = [(9, None, 123)] * 140
    elif packets == "wrongkey":
        api.queue = [(9, 1, 123)]
    elif packets == "wrongpid":
        api.queue = [(10, None, 124)]
    elif packets == "unknownmessage":
        api.queue = [(999, None, 0)]
    else:
        api.queue = ["error"]
    observer.close()
    assert observer.drain_error is not None
    assert observer.evidence()["process_memory_limit_event"] == "unknown"
    assert observer.evidence()["job_memory_limit_event"] == "unknown"
    assert len(observer.messages) <= diagnostic.MAX_MESSAGES
    assert api.close_calls == [802, 801, 803]


def test_cleanup_reaps_before_drain_and_close_and_retries_only_failed_close() -> None:
    api = FakeApi()
    cleanup = diagnostic.MemoryCleanup(cast(Any, api))
    cleanup.observers.bind_suspended(701, 702)
    api.events.clear()
    cleanup.add(SimpleNamespace(close=lambda: api.events.append("reap")))
    cleanup.launch_resolved = True
    api.once_close_fail.add(803)
    with pytest.raises(LaunchRejected):
        cleanup.close()
    assert api.events[0] == "reap"
    assert api.events.index("packet") < api.events.index("close:802") < api.events.index("close:803")
    cleanup.close()
    assert cleanup.closed
    assert api.close_calls == [802, 801, 803, 803]


@pytest.mark.parametrize("failure", ["reap", "unknownlaunch", "active", "wait", "query1"])
def test_unknown_reap_retains_port_and_observers_without_draining(failure: str) -> None:
    api = FakeApi()
    cleanup = diagnostic.MemoryCleanup(cast(Any, api))
    cleanup.observers.bind_suspended(701, 702)
    cleanup.launch_resolved = failure != "unknownlaunch"
    if failure == "reap":
        def reject() -> None:
            raise LaunchRejected("synthetic_reap_unknown")
        cleanup.add(SimpleNamespace(close=reject))
    elif failure == "active":
        api.active = 1
    elif failure == "wait":
        api.wait_status = 258
    elif failure == "query1":
        api.fail = "query1"
    previous_packets = len(api.packet_calls)
    with pytest.raises(LaunchRejected) as caught:
        cleanup.close()
    assert caught.value.cleanup_owner is cleanup
    assert api.close_calls == []
    assert len(api.packet_calls) == previous_packets


def test_resume_hook_binds_before_native_resume_and_refuses_repeat(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    def resume(_thread: object) -> int:
        events.append("resume")
        return 1
    def fake_init(self: Any) -> None:
        self.assign_job = lambda _job, _process: 1
        self.resume = resume
    monkeypatch.setattr(_win32.Api, "__init__", fake_init)
    observer = SimpleNamespace(bind_suspended=lambda _job, _process: events.append("bind"))
    api = diagnostic.MemoryLaunchApi(cast(Any, observer))
    with pytest.raises(LaunchRejected):
        api.resume(3)
    assert api.assign_job(1, 2) == 1
    assert api.resume(3) == 1
    assert events == ["bind", "resume"]
    with pytest.raises(LaunchRejected):
        api.resume(3)
    assert events == ["bind", "resume"]


@pytest.mark.parametrize("field,value", [("fixture_version", 2), ("allocation_count", 33),
                                         ("allocation_count", True), ("allocation_bytes", 1),
                                         ("all_allocations_freed", False), ("free_error", 5),
                                         ("freed_count", 14), ("allocation_error", 0),
                                         ("allocation_ceiling_bytes", 16 * 1024**3)])
def test_frame_schema_and_independent_ceiling_rejects_invalid_claims(field: str, value: object) -> None:
    payload = frame()
    payload[field] = value
    with pytest.raises(LaunchRejected):
        diagnostic.fixed_frame(result(payload), "bounded-memory")


def test_smoke_and_ceiling_frames_are_consistent_but_do_not_establish_limit_attribution() -> None:
    smoke = frame("smoke", 0, 0)
    assert diagnostic.fixed_frame(result(smoke), "smoke") == smoke
    maximum = frame(count=32, error=0)
    assert diagnostic.fixed_frame(result(maximum), "bounded-memory") == maximum
    allocation_refused = frame(error=8)
    assert diagnostic.fixed_frame(result(allocation_refused), "bounded-memory") == allocation_refused


@pytest.mark.parametrize("change", ["unknown", "missing", "wrongmode", "wrongprobe", "multiframe"])
def test_fixed_frame_rejects_unknown_missing_or_misidentified_payloads(change: str) -> None:
    payload = frame()
    if change == "unknown":
        payload["unknown"] = 1
    elif change == "missing":
        payload.pop("free_error")
    elif change == "wrongmode":
        payload["mode"] = "memory"
    elif change == "wrongprobe":
        payload["probe"] = "native"
    candidate = result(payload)
    if change == "multiframe":
        candidate = WorkerResult(identity(), 0, (payload, payload), b"", b"", 0.01)
    with pytest.raises(LaunchRejected):
        diagnostic.fixed_frame(candidate, "bounded-memory")


@pytest.mark.parametrize("mode,limits", [("memory", JobLimits()), ("spin", JobLimits()),
                                         ("bounded-memory", JobLimits(memory_bytes=512 * 1024 * 1024)),
                                         ("bounded-memory", JobLimits(cpu_rate=1000))])
def test_invalid_modes_or_modified_limits_never_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     mode: str, limits: JobLimits) -> None:
    launched: list[object] = []
    monkeypatch.setattr(diagnostic, "launch_observed", lambda *args: launched.append(args))
    with pytest.raises(ValueError):
        diagnostic.run_mode(mode, spec(tmp_path, mode, limits=limits), cast(Any, FakeApi()))
    assert launched == []


def test_pin_missing_or_mismatched_fails_before_profile_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = tmp_path / "fixture.exe"
    fixture.write_bytes(b"V2 is not a fallback")
    created: list[object] = []
    monkeypatch.setattr(diagnostic, "create_probe_profile", lambda *args: created.append(args))
    with pytest.raises(ValueError, match="memory_v3_fingerprint_mismatch"):
        diagnostic.collect_matrix(cast(Any, None), {}, fixture, "C:\\Windows", cast(Any, FakeApi()))
    monkeypatch.setattr(diagnostic, "FIXTURE_SHA", "")
    with pytest.raises(ValueError, match="memory_v3_pin_unavailable"):
        diagnostic.collect_matrix(cast(Any, None), {}, fixture, "C:\\Windows", cast(Any, FakeApi()))
    assert created == []


def matrix_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[ProbeProfile], Any]:
    fixture = tmp_path / "fixture.exe"
    fixture.write_bytes(b"synthetic V3")
    folder = tmp_path / "profile"
    folder.mkdir()
    profile = ProbeProfile("deskpal.v06.synthetic", identity().appcontainer_sid, str(folder))
    deleted: list[ProbeProfile] = []
    monkeypatch.setattr(diagnostic, "FIXTURE_SHA", sha256(fixture.read_bytes()).hexdigest())
    monkeypatch.setattr(diagnostic, "create_probe_profile", lambda _name: profile)
    monkeypatch.setattr(diagnostic, "delete_probe_profile", deleted.append)
    monkeypatch.setattr(diagnostic, "source_fingerprints", lambda: {"synthetic": "frozen"})
    writer = SimpleNamespace(save=lambda _report: None)
    return fixture, deleted, writer


def test_report_failure_after_profile_creation_still_deletes_known_idle_profile(tmp_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    fixture, deleted, writer = matrix_setup(tmp_path, monkeypatch)
    calls: list[int] = []
    def save(_report: object) -> None:
        calls.append(1)
        if len(calls) == 2:
            raise OSError("synthetic write failure")
    writer.save = save
    report: dict[str, object] = {"source_sha256": {"synthetic": "frozen"}}
    assert diagnostic.collect_matrix(writer, report, fixture, "C:\\Windows", cast(Any, FakeApi())) == 2
    assert len(deleted) == 1
    assert report["cleanup"] == "profile_deleted"


def test_interrupted_run_withholds_profile_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture, deleted, writer = matrix_setup(tmp_path, monkeypatch)
    def interrupted(*_args: object) -> tuple[dict[str, object], bool]:
        raise RuntimeError("synthetic run result unknown")
    monkeypatch.setattr(diagnostic, "run_mode", interrupted)
    report: dict[str, object] = {"source_sha256": {"synthetic": "frozen"}}
    assert diagnostic.collect_matrix(writer, report, fixture, "C:\\Windows", cast(Any, FakeApi())) == 2
    assert deleted == []
    assert report["cleanup"] == "withheld_unconfirmed_owner"


def test_failed_smoke_stops_before_memory_even_after_successful_reap(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    fixture, deleted, writer = matrix_setup(tmp_path, monkeypatch)
    modes: list[str] = []
    def failed(mode: str, *_args: object) -> tuple[dict[str, object], bool]:
        modes.append(mode)
        return {"expected_behavior_observed": False, "bounded_collection": True, "cleanup_confirmed": True}, True
    monkeypatch.setattr(diagnostic, "run_mode", failed)
    report: dict[str, object] = {"source_sha256": {"synthetic": "frozen"}}
    assert diagnostic.collect_matrix(writer, report, fixture, "C:\\Windows", cast(Any, FakeApi())) == 2
    assert modes == ["smoke"]
    assert len(deleted) == 1


@pytest.mark.parametrize("failure", ["deadline", "protocol", "unknownlaunch", "cleanup"])
def test_run_mode_failure_paths_preserve_reap_facts_and_unknown_owners(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch,
                                                                    failure: str) -> None:
    api = FakeApi()
    def launch(_spec: LaunchSpec, cleanup: diagnostic.MemoryCleanup) -> Any:
        if failure == "unknownlaunch":
            raise RuntimeError("synthetic launch result unknown")
        cleanup.observers.bind_suspended(701, 702)
        def collect(_timeout: float, *, log_limit_bytes: int) -> WorkerResult:
            assert log_limit_bytes == diagnostic.LOG_LIMIT
            if failure == "deadline":
                raise LaunchRejected("deadline")
            if failure == "protocol":
                raise ValueError("synthetic protocol error")
            return result()
        def close() -> None:
            api.events.append("reap")
            if failure == "cleanup":
                raise LaunchRejected("synthetic_reap_unconfirmed")
        return SimpleNamespace(identity=identity(), collect=collect, close=close)
    monkeypatch.setattr(diagnostic, "launch_observed", launch)
    entry, safe = diagnostic.run_mode("bounded-memory", spec(tmp_path), cast(Any, api))
    assert safe is (failure in ("deadline", "protocol"))
    assert bool(diagnostic._UNCONFIRMED_OWNERS) is (not safe)
    assert entry["stage0_passed"] is False
    assert entry["resource_enforcement_accepted"] is False
    if not safe:
        assert api.close_calls == []
    else:
        assert api.close_calls == [802, 801, 803]
        assert api.events.index("reap") < api.events.index("close:802")


def test_collected_report_always_refuses_stage0_or_enforcement_acceptance(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    fixture, deleted, writer = matrix_setup(tmp_path, monkeypatch)
    def safe(*_args: object) -> tuple[dict[str, object], bool]:
        return {"expected_behavior_observed": True, "bounded_collection": True, "cleanup_confirmed": True}, True
    monkeypatch.setattr(diagnostic, "run_mode", safe)
    report: dict[str, object] = {"source_sha256": {"synthetic": "frozen"}}
    assert diagnostic.collect_matrix(writer, report, fixture, "C:\\Windows", cast(Any, FakeApi())) == 0
    assert len(deleted) == 1
    assert report["stage0_passed"] is False
    assert report["resource_enforcement_accepted"] is False
