"""Mock-only integrity diagnostic coverage; native/process/profile tripwires."""

from __future__ import annotations

import ast
import copy
import ctypes as C
from hashlib import sha256
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import diagnose_appcontainer_integrity as diagnostic
from deskpal.execution.runtime import ProbeProfile as ProbeProfile

TOKEN = diagnostic.token
REAL_ADAPTER = diagnostic.IntegrityApi
USER = "S-1-5-21-1-2-3-1001"
PACKAGE = "S-1-15-2-1-2-3-4-5-6-7"
SOURCES = {"mock-only-frozen-source": "unchanged"}


def forbidden(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise AssertionError("unmocked native/process/profile operation")


@pytest.fixture(autouse=True)
def no_native(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(C, "WinDLL", forbidden, raising=False)
    monkeypatch.setattr(diagnostic, "NativeApi", forbidden)
    monkeypatch.setattr(diagnostic, "IntegrityApi", forbidden)
    monkeypatch.setattr(diagnostic, "create_probe_profile", forbidden)
    monkeypatch.setattr(diagnostic, "delete_probe_profile", forbidden)
    monkeypatch.setattr(diagnostic.core, "_create_worker", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(diagnostic, "_UNCONFIRMED_OWNERS", [])


def sid_bytes(value: str) -> bytes:
    parts = value.split("-")
    values = [int(part) for part in parts[3:]]
    return bytes((1, len(values))) + int(parts[2]).to_bytes(6, "big") + struct.pack("<" + "I" * len(values), *values)


class Native:
    def __init__(self) -> None:
        self.lowered, self.set_ok, self.set_error = False, 1, 0
        self.source_changed = False
        self.close_fail: set[int] = set()
        self.worker_open_ok, self.worker_open_output = 1, 22
        self.after_override: dict[str, Any] = {}
        self.closed: list[int] = []
        self.calls: list[tuple[str, Any]] = []

    def value(self, worker: bool, kind: int) -> Any:
        level = "S-1-16-0" if worker and self.lowered else "S-1-16-4096" if worker else "S-1-16-8192"
        value = {
            1: {"sid": USER, "attributes": 0},
            2: [{"sid": level, "attributes": 0x60}, {"sid": "S-1-1-0", "attributes": 7}],
            3: [{"luid": {"low_dword": 23, "high_long": 0}, "attributes": 3}],
            8: 1, 10: (102 if worker else 101,
                       203 if worker and self.lowered else 202 if worker else 999 if self.source_changed else 201),
            11: [], 25: {"sid": level, "attributes": 0x60}, 27: 3,
            29: int(worker), 30: [], 31: PACKAGE,
        }[kind]
        if worker and self.lowered and str(kind) in self.after_override:
            return self.after_override[str(kind)]
        return value

    def payload(self, worker: bool, kind: int, base: int) -> bytes:
        value = self.value(worker, kind)
        if kind in (1, 25):
            return struct.pack("<QI4x", base + 16, value["attributes"]) + sid_bytes(value["sid"])
        if kind in (2, 11, 30):
            if not value:
                return struct.pack("<I", 0)
            end, entries, storage = 8 + 16 * len(value), b"", b""
            for entry in value:
                entries += struct.pack("<QI4x", base + end + len(storage), entry["attributes"])
                storage += sid_bytes(entry["sid"])
            return struct.pack("<I4x", len(value)) + entries + storage
        if kind == 3:
            return struct.pack("<I", len(value)) + b"".join(struct.pack("<IiI", entry["luid"]["low_dword"],
                entry["luid"]["high_long"], entry["attributes"]) for entry in value)
        if kind in (8, 27, 29):
            return struct.pack("<I", value)
        if kind == 31:
            return struct.pack("<Q", base + 8) + sid_bytes(value)
        if kind == 10:
            data = TOKEN.Statistics()
            data.TokenId.LowPart, data.ModifiedId.LowPart = value
            return bytes(data)
        raise AssertionError(kind)

    def call(self, name: str, *args: Any) -> tuple[int, int]:
        self.calls.append((name, args))
        if name in ("GetCurrentProcess", "GetCurrentThread"):
            return (-1 if name == "GetCurrentProcess" else -2), 0
        if name == "OpenThreadToken":
            assert args[1:3] == (8, True)
            return 0, 1008
        if name == "OpenProcessToken":
            worker = args[0].value == 77
            assert args[1] == (0x88 if worker else 8)
            C.cast(args[2], C.POINTER(TOKEN.HANDLE)).contents.value = self.worker_open_output if worker else 11
            return (self.worker_open_ok, 0 if self.worker_open_ok else 5) if worker else (1, 0)
        if name == "GetTokenInformation":
            handle, kind, buffer, capacity, returned = args
            data = self.payload(handle.value == 22, kind, 0 if buffer is None else C.addressof(buffer))
            C.cast(returned, C.POINTER(TOKEN.DWORD)).contents.value = len(data)
            if buffer is None:
                return 0, 122
            assert len(data) <= capacity
            C.memmove(buffer, data, len(data))
            return 1, 0
        if name == "SetTokenInformation":
            handle, kind, buffer, length = args
            assert handle.value == 22 and kind == 25 and length == 28
            data = bytes(buffer.raw)
            assert TOKEN._parse(1, data, C.addressof(buffer)) == {"sid": "S-1-16-0", "attributes": 0x20}
            assert data[16:] == diagnostic.UNTRUSTED_SID
            self.lowered = bool(self.set_ok)
            return self.set_ok, self.set_error
        if name == "CloseHandle":
            value = int(args[0].value)
            self.closed.append(value)
            return (0, 5) if value in self.close_fail else (1, 0)
        if name == "GetWindowsDirectoryW":
            args[0].value = "C:/Windows"
            return len(args[0].value), 0
        raise AssertionError(name)


def session(api: Native) -> tuple[diagnostic.Session, dict[str, Any], io.StringIO]:
    report: dict[str, Any] = {"adapter_close_events": [], "post_lower_job_calls": []}
    output = io.StringIO()
    return diagnostic.Session(cast(Any, api), output, report), report, output


def test_actual_bounded_before_after_reads_and_fixed_set_payload() -> None:
    api = Native()
    subject, report, _output = session(api)
    diagnostic._observe(subject, 77, PACKAGE)
    assert all(report["worker_gate_checks"].values())
    assert report["worker_before"]["integrity"]["sid"] == "S-1-16-4096"
    assert report["worker_after"]["integrity"]["sid"] == "S-1-16-0"
    assert report["worker_before"]["statistics"] != report["worker_after"]["statistics"]
    assert report["set_integrity"]["native"]["raw_return"] == 1
    assert sum(name == "SetTokenInformation" for name, _args in api.calls) == 1
    assert subject.close()
    assert sorted(api.closed) == [11, 22]


@pytest.mark.parametrize("kind,value", [
    ("1", {"sid": "S-1-5-21-1-2-3-1002", "attributes": 0}),
    ("8", 2), ("29", 0), ("31", "S-1-15-2-7-6-5-4-3-2-1"),
    ("27", 1), ("27", 0),
    ("30", [{"sid": "S-1-1-0", "attributes": 7}]),
    ("11", [{"sid": "S-1-5-21-9-9-9-9", "attributes": 7}]),
    ("3", []), ("25", {"sid": "S-1-16-4096", "attributes": 0x60}),
])
def test_identity_or_policy_transition_is_rejected(kind: str, value: Any) -> None:
    api = Native()
    api.after_override[kind] = value
    subject, report, _output = session(api)
    with pytest.raises(ValueError):
        diagnostic._observe(subject, 77, PACKAGE)
    assert "worker_after" in report
    subject.close()


def test_set_failure_preserves_raw_error_and_after_observation() -> None:
    api = Native()
    api.set_ok, api.set_error = 0, 1314
    subject, report, _output = session(api)
    with pytest.raises(ValueError, match="set_or_readback"):
        diagnostic._observe(subject, 77, PACKAGE)
    assert report["set_integrity"]["native"]["last_error"] == 1314
    assert report["worker_after"]["integrity"]["sid"] == "S-1-16-4096"
    subject.close()


def test_partial_worker_token_is_owned_and_checked_close_failure_retained() -> None:
    api = Native()
    api.worker_open_ok, api.worker_open_output = 0, 22
    api.close_fail = {22}
    subject, report, _output = session(api)
    with pytest.raises(ValueError, match="open_or_create"):
        diagnostic._observe(subject, 77, PACKAGE)
    assert not subject.close()
    assert api.closed.count(22) == 1 and 11 in api.closed
    assert not report["all_token_handles_closed"] and report["unknown_reasons"]
    assert any(owner.handle.value == 22 for owner in subject.owners)


@pytest.mark.parametrize("kind,data", [(25, bytes(15)), (27, bytes(3)),
    (27, struct.pack("<I", 4)), (31, bytes(7)), (31, struct.pack("<Q", 999))])
def test_additional_parser_rejects_malformed_owned_buffer(kind: int, data: bytes) -> None:
    with pytest.raises(ValueError):
        diagnostic._additional_parse(kind, data, 100)


def test_close_wrapper_records_failure_and_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    subject, report, _output = session(Native())
    adapter = object.__new__(REAL_ADAPTER)
    adapter.session, adapter.failed_closes, adapter.withheld_tokens = subject, [], []
    calls: list[Any] = []

    def successful_close(handle: Any) -> int:
        calls.append(handle)
        return 0

    adapter._native_close = successful_close
    monkeypatch.setattr(C, "set_last_error", lambda _error: None, raising=False)
    monkeypatch.setattr(C, "get_last_error", lambda: 5, raising=False)
    owner = subject.owner("adapter_verify_token")
    owner.handle.value = 33
    assert adapter._checked_close(33) == 0
    assert calls == [33] and report["adapter_close_events"][0]["last_error"] == 5
    assert owner.handle.value is None and adapter.withheld_tokens[0].handle.value == 33
    subject.close()
    assert calls == [33]


def test_adapter_partial_open_registered_and_resume_always_stops() -> None:
    subject, _report, _output = session(Native())
    adapter = object.__new__(REAL_ADAPTER)
    adapter.session = subject

    def partial(_process: Any, _access: Any, output: Any) -> int:
        C.cast(output, C.POINTER(TOKEN.HANDLE)).contents.value = 44
        raise RuntimeError("partial output wrapper failure")

    adapter._native_open = partial
    output = TOKEN.HANDLE()
    with pytest.raises(RuntimeError, match="partial output"):
        adapter._owned_open(77, 8, C.byref(output))
    assert subject.owners[-1].handle.value == 44
    subject.close()
    adapter.process_handle, adapter.resume_intercepted = 77, False
    seen: list[int] = []
    adapter.callback = seen.append
    with pytest.raises(diagnostic.LaunchRejected) as stopped:
        adapter._read_before_resume(88)
    assert stopped.value.stage == diagnostic.STOP_STAGE and seen == [77]
    with pytest.raises(diagnostic.LaunchRejected):
        adapter._read_before_resume(88)
    assert seen == [77]


@pytest.mark.parametrize("handle", [None, 0, -1, -2, -3, -4, -5, -6, C.c_void_p(-1).value])
def test_adapter_invalid_and_pseudo_closes_are_withheld(monkeypatch: pytest.MonkeyPatch, handle: Any) -> None:
    subject, report, _output = session(Native())
    adapter = object.__new__(REAL_ADAPTER)
    adapter.session, adapter.failed_closes, adapter.withheld_tokens = subject, [], []
    adapter._native_close = forbidden
    monkeypatch.setattr(C, "set_last_error", lambda _error: None, raising=False)
    assert adapter._checked_close(handle) == 0
    assert report["adapter_close_events"][0]["state"] == "withheld_invalid_or_pseudo_handle"
    assert report["unknown_reasons"] and adapter.failed_closes


def test_post_lower_job_check_uses_bounded_independent_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    core = diagnostic.core
    spec = diagnostic.LaunchSpec(
        Path("C:/mock/fixture.exe"), (), Path("C:/mock"), PACKAGE,
        {"SYSTEMROOT": "C:/Windows", "WINDIR": "C:/Windows"},
    )
    wrong = False

    def mitigation(_process: Any, _kind: Any, output: Any, _length: Any) -> int:
        C.cast(output, C.POINTER(core.DWORD)).contents.value = 0 if wrong else 1
        return 1

    def query(_job: Any, kind: Any, output: Any, _length: Any, _returned: Any) -> int:
        if kind == 9:
            limits = C.cast(output, C.POINTER(core.JOB_EXTENDED_LIMIT)).contents
            limits.BasicLimitInformation.LimitFlags = core._LIMIT_FLAGS
            limits.BasicLimitInformation.ActiveProcessLimit = spec.limits.active_processes
            limits.ProcessMemoryLimit = limits.JobMemoryLimit = spec.limits.memory_bytes
        else:
            cpu = C.cast(output, C.POINTER(core.JOB_CPU)).contents
            cpu.ControlFlags, cpu.CpuRate = core._CPU_FLAGS, spec.limits.cpu_rate
        return 1

    def member(_process: Any, _job: Any, output: Any) -> int:
        C.cast(output, C.POINTER(core.BOOL)).contents.value = 1
        return 1

    monkeypatch.setattr(C, "set_last_error", lambda _error: None, raising=False)
    monkeypatch.setattr(C, "get_last_error", lambda: 0, raising=False)
    api = SimpleNamespace(job=55, get_mitigation=mitigation, query_job=query, in_job=member)
    report: dict[str, Any] = {"post_lower_job_calls": []}
    diagnostic._job_check(cast(Any, api), spec, 77, report)
    assert len(report["post_lower_job_calls"]) == 4 and report["post_lower_job"]["member"]
    wrong = True
    with pytest.raises(ValueError, match="job_mismatch"):
        diagnostic._job_check(cast(Any, api), spec, 77, report)
    with pytest.raises(ValueError, match="call_budget"):
        diagnostic._job_check(cast(Any, api), spec, 77, report)


def experiment_mocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, api: Native) -> list[Any]:
    folder = tmp_path / "new-profile"
    folder.mkdir()
    fixture = tmp_path / "pinned-fixture.exe"
    fixture.write_bytes(b"mock fixture never executed")
    profile = ProbeProfile("deskpal.v06." + "a" * 32, PACKAGE, str(folder))
    deleted: list[Any] = []
    monkeypatch.setattr(diagnostic, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(diagnostic, "NativeApi", lambda: api)
    monkeypatch.setattr(diagnostic, "create_probe_profile", lambda _name: profile)
    monkeypatch.setattr(diagnostic, "delete_probe_profile", deleted.append)
    monkeypatch.setattr(diagnostic, "_fingerprints", lambda: SOURCES)
    monkeypatch.setattr(diagnostic.suspended, "FIXTURE", fixture)
    monkeypatch.setattr(diagnostic.suspended, "FIXTURE_SHA256", sha256(fixture.read_bytes()).hexdigest())
    monkeypatch.setattr(diagnostic, "_job_check", lambda _api, _spec, _process, report: report.update(post_lower_job={"mock": True}))

    class Adapter:
        def __init__(self, subject: Any, observe: Any) -> None:
            self.callback, self.session = observe, subject
            self.resume_intercepted, self.creation_flags = False, 0x8040C
            self.failed_closes: list[Any] = []
            self.withheld_tokens: list[Any] = []

    def launch(spec: Any, *, _api: Any) -> Any:
        assert spec.appcontainer_sid == PACKAGE and set(spec.environment) == {
            "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA"}
        _api.resume_intercepted = True
        _api.callback(77)
        raise diagnostic.LaunchRejected(diagnostic.STOP_STAGE)

    monkeypatch.setattr(diagnostic, "IntegrityApi", Adapter)
    monkeypatch.setattr(diagnostic.core, "_create_worker", launch)
    return deleted


def test_success_is_only_suspended_configuration_observation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    api = Native()
    deleted = experiment_mocks(monkeypatch, tmp_path, api)
    report: dict[str, Any] = {
        "source_sha256": SOURCES, "stage0_passed": False, "write_boundary_verified": False,
    }
    output = io.StringIO()
    assert diagnostic._experiment(output, report) == 0
    assert report["status"] == "suspended_ac_integrity_lowering_observed"
    assert report["source_token_unchanged"] and report["cleanup"] == "profile_deleted"
    assert len(deleted) == 1 and len(report["native_calls"]) <= 96
    assert not report["stage0_passed"] and not report["write_boundary_verified"]


@pytest.mark.parametrize("failure", ["set", "close", "source", "hash"])
def test_unknown_retains_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str) -> None:
    api = Native()
    deleted = experiment_mocks(monkeypatch, tmp_path, api)
    if failure == "set":
        api.set_ok, api.set_error = 0, 5
    elif failure == "close":
        api.close_fail = {22}
    elif failure == "source":
        original = api.call

        def changed(name: str, *args: Any) -> tuple[int, int]:
            result = original(name, *args)
            if name == "SetTokenInformation":
                api.source_changed = True
            return result

        monkeypatch.setattr(api, "call", changed)
    else:
        monkeypatch.setattr(diagnostic, "_fingerprints", lambda: {"mock-only-frozen-source": "changed"})
    report: dict[str, Any] = {"source_sha256": SOURCES}
    assert diagnostic._experiment(io.StringIO(), report) == 2
    assert report["status"] == "unknown" and not deleted
    assert report["cleanup"].startswith("withheld_")
    assert sum(name == "SetTokenInformation" for name, _args in api.calls) == 1


def test_failed_adapter_cleanup_owner_retained_without_automatic_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    api = Native()
    deleted = experiment_mocks(monkeypatch, tmp_path, api)

    class FailedOwner:
        calls = 0

        def close(self) -> None:
            self.calls += 1
            raise OSError(5, "first adapter cleanup failed")

    owner = FailedOwner()

    def launch(_spec: Any, *, _api: Any) -> Any:
        _api.resume_intercepted = True
        _api.callback(77)
        try:
            owner.close()  # The original adapter's first failed cleanup.
        except OSError:
            pass
        raise diagnostic.LaunchRejected("launch_failure_reap_unconfirmed", 5, cleanup_owner=owner)

    monkeypatch.setattr(diagnostic.core, "_create_worker", launch)
    report: dict[str, Any] = {"source_sha256": SOURCES}
    assert diagnostic._experiment(io.StringIO(), report) == 2
    assert owner.calls == 1 and owner in diagnostic._UNCONFIRMED_OWNERS and not deleted
    assert report["stop_stage"] == "launch_failure_reap_unconfirmed"
    assert report["stage_winerror"] == 5 and report["cleanup_owner_present"] is True
    assert report["worker_cleanup_events"] == [{"reason": "adapter_cleanup_unconfirmed",
        "retained_owner": True, "automatic_retry": False, "close_attempted": False,
        "state": "retained_owner_no_automatic_retry"}]
    assert report["status"] == "unknown" and report["cleanup"].startswith("withheld_")


def test_no_opt_in_calls_no_native_or_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    monkeypatch.setattr(TOKEN, "_new_report", lambda _path: report)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", str(report)])
    assert diagnostic.main() == 0
    data = json.loads(report.read_text(encoding="ascii"))
    assert data["status"] == "not_executed_explicit_opt_in_required"
    assert data["native_resume_called"] is data["worker_code_executed"] is False


def test_native_allowlist_and_resume_hook_are_statically_bounded() -> None:
    tree = ast.parse(Path(diagnostic.__file__).read_text(encoding="utf-8"))
    calls = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)}
    assert not {"resume", "ResumeThread", "AdjustTokenPrivileges", "impersonate", "set_security"} & calls
    source = Path(diagnostic.__file__).read_text(encoding="utf-8")
    assert '"SetTokenInformation"' in source and '"CreateRestrictedToken"' not in source
    assert "raise LaunchRejected(STOP_STAGE)" in source
    hook = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_read_before_resume")
    assert isinstance(hook.body[-1], ast.Raise)


def test_non_integrity_group_change_rejected() -> None:
    api = Native()
    subject, report, _output = session(api)
    diagnostic._observe(subject, 77, PACKAGE)
    after = copy.deepcopy(report["worker_after"])
    after["groups"][1]["attributes"] = 16
    assert not diagnostic._gates(report["worker_before"], after, USER, PACKAGE)["only_integrity_group_changed"]
    subject.close()
