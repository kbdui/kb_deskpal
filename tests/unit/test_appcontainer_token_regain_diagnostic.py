"""Tripwired V4 parser/lifecycle mocks; never execute the real fixture."""
from __future__ import annotations

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
from deskpal.execution.runtime import ProbeProfile
from deskpal.execution.windows import launcher
from deskpal.execution.windows.launcher import LaunchRejected, WorkerIdentity, WorkerResult

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import diagnose_appcontainer_integrity_smoke as smoke
import diagnose_appcontainer_token_regain as diagnostic

REAL_RESUME = smoke.SmokeApi._read_before_resume
USER, PACKAGE = "S-1-5-21-1-2-3-1001", "S-1-15-2-1-2-3-4-5-6-7"
SOURCES = {"mock-fixed-source": "unchanged"}
IDENTITY = WorkerIdentity(77, PACKAGE, True, None, 0, 1, 0x3204, 268435456,
    268435456, 1, 5, 2000, True, (1, 2, 3, 4), 99)
HOST = {"type": 1, "user": {"sid": USER, "attributes": 0}, "package_sid": PACKAGE,
    "statistics": {"token_id": {"low_dword": 101, "high_long": 0}}, "is_appcontainer": 1,
    "capabilities": [], "integrity": {"sid": "S-1-16-0", "attributes": 96}, "mandatory_policy": 1}


def forbidden(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise AssertionError("unmocked native/profile/process boundary")


@pytest.fixture(autouse=True)
def no_native(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(C, "WinDLL", forbidden, raising=False)
    for name in ("NativeApi", "SmokeApi", "create_worker", "create_probe_profile", "delete_probe_profile"):
        monkeypatch.setattr(diagnostic, name, forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(diagnostic, "_UNCONFIRMED_OWNERS", [])


def fixture_frame(mode: str, stage: str = "set", error: int = 5) -> dict[str, Any]:
    snapshot = {"valid": 1, "thread_absent": 1, "type": 1, "user": USER, "package": PACKAGE,
        "id": [101, 0], "ac": 1, "cap": 0, "il_known": 1, "il": 0, "il_attrs": 96,
        "policy_known": 1, "policy": 1, "user_attrs": 0}
    info, size = (25, 28) if mode == "raise-il" else (27, 4)
    lengths = {8: 4, 1: 44, 31: 48, 10: 56, 29: 4, 30: 4, 25: 28, 27: 4}

    def capture(phase: int) -> list[list[int]]:
        return [[2, phase, 2, 0, 1008, 0], *[[1, kind, 2, 1, 0, lengths[kind]] for kind in diagnostic.QUERY_ORDER]]

    calls = capture(1) + [[3, 0x88, 2, 0 if stage == "open" else 1, 5 if stage == "open" else 0, int(stage != "open")]]
    if stage != "open":
        calls += [[1, 10, 2, 1, 0, 56], [4, info, 2, 0, error, size]]
    calls += capture(2)
    if stage != "open":
        calls += [[5, 3, 2, 1, 0, 0]]
    return {"probe": "own_token_v4", "mode": mode, "attack_succeeded": 0,
        "resistance_observed": int(error in (5, 1314)), "unknown": int(error not in (5, 1314)),
        "problems": 0 if error in (5, 1314) else 256, "identity_same": 1, "cleanup_ok": 1,
        "open": [0, 5] if stage == "open" else [1, 0], "set": [0, 0, 0] if stage == "open" else [1, 0, error],
        "before": copy.deepcopy(snapshot), "after": copy.deepcopy(snapshot), "refused_handle_count": 0,
        "set_info": info, "set_bytes": size, "call_budget": 48, "call_count_before_ipc": len(calls),
        "maximum_total_calls": len(calls) + 2, "calls": calls, "ipc_write_calls_planned": 2}


def successful_set(frame: dict[str, Any], raw: int = 1) -> None:
    frame.update(attack_succeeded=1, resistance_observed=0, unknown=0)
    frame["set"] = [1, raw, 777]  # Last error on success is retained, not a failure gate.
    next(row for row in frame["calls"] if row[0] == 4)[3:5] = [raw, 777]


def assess(frame: dict[str, Any], mode: str, exit_code: int = 0) -> tuple[bool, dict[str, Any]]:
    report: dict[str, Any] = {"security_boundary_failed": False, "host_runtime_matches_prelaunch": True,
        "host_runtime_after": {"integrity": copy.deepcopy(HOST["integrity"]), "mandatory_policy": HOST["mandatory_policy"]}}
    diagnostic._latch(frame, report)
    result = WorkerResult(IDENTITY, exit_code, (cast(dict[str, object], frame),), b"", b"", 0.1)
    return diagnostic._assess(result, mode, copy.deepcopy(HOST), report), report


@pytest.mark.parametrize("mode", diagnostic.MODES)
@pytest.mark.parametrize("stage,error", [("open", 5), ("set", 5), ("set", 1314)])
def test_two_fixed_modes_accept_only_complete_selected_denials(mode: str, stage: str, error: int) -> None:
    accepted, report = assess(fixture_frame(mode, stage, error), mode)
    assert accepted and report["fixture_body_confirmed"] and report["runtime_context_matches_host"]
    assert not report["security_boundary_failed"]


def test_nonzero_native_bool_and_success_last_error_are_not_misclassified() -> None:
    frame = fixture_frame("raise-il")
    frame["open"] = [2, 777]
    for row in frame["calls"]:
        if row[0] in (1, 3, 5):
            row[3:5] = [2, 777]
    assert assess(frame, "raise-il")[0]


@pytest.mark.parametrize("damage", ["flag_bool", "raw_bool", "extra_key", "query_length", "duplicate_open", "wrong_mode"])
def test_malformed_schema_c_bool_lengths_and_trace_fail_closed(damage: str) -> None:
    frame = fixture_frame("raise-il")
    if damage == "flag_bool":
        frame["identity_same"] = True
    elif damage == "raw_bool":
        frame["set"][1] = False
    elif damage == "extra_key":
        frame["extra"] = 1
    elif damage == "query_length":
        frame["calls"][1][5] = 0
    elif damage == "duplicate_open":
        frame["calls"].insert(10, list(frame["calls"][9]))
        frame["call_count_before_ipc"] += 1
        frame["maximum_total_calls"] += 1
    else:
        frame["mode"] = "disable-no-write-up"
    with pytest.raises(ValueError):
        assess(frame, "raise-il")


@pytest.mark.parametrize("change", ["set", "il", "policy", "malformed_positive"])
def test_positive_boundary_evidence_latches_before_schema_or_exit_checks(change: str) -> None:
    frame = fixture_frame("raise-il")
    if change in ("set", "malformed_positive"):
        successful_set(frame, 2)
    else:
        frame["after"]["il" if change == "il" else "policy"] = 4096 if change == "il" else 0
        frame.update(attack_succeeded=1, resistance_observed=0)
    report: dict[str, Any] = {}
    if change == "malformed_positive":
        del frame["after"]["user"]
    diagnostic._latch(frame, report)
    assert report["security_boundary_failed"]
    result = WorkerResult(IDENTITY, 123, (cast(dict[str, object], frame),), b"later-log", b"", 0.1)
    try:
        assert not diagnostic._assess(result, "raise-il", copy.deepcopy(HOST), report)
    except ValueError:
        assert change == "malformed_positive"
    assert report["security_boundary_failed"]


def test_unsupported_set_and_runtime_identity_mismatch_are_unknown() -> None:
    assert not assess(fixture_frame("disable-no-write-up", error=87), "disable-no-write-up", 2)[0]
    frame = fixture_frame("raise-il")
    frame["after"]["id"] = [102, 0]
    frame.update(identity_same=0, problems=1024, resistance_observed=0, unknown=1)
    accepted, report = assess(frame, "raise-il", 2)
    assert not accepted and not report["runtime_context_matches_host"] and not report["security_boundary_failed"]


class FakeNative:
    def __init__(self, frame: dict[str, Any], output: io.StringIO, case: str) -> None:
        self.frame, self.output, self.case = frame, output, case
        self.close_calls = self.terminate_calls = 0
        self.readers = (201, 202, 203)
        self._eof = [False, False, False]
        body = json.dumps(self.frame).encode("ascii")
        control = struct.pack(">I", len(body)) + body
        if case in ("empty_frame", "collect_empty"):
            control = b""
        elif case == "collect_partial":
            control = control[:20]
        elif case == "latched_tail_eof":
            control += b"\x00"
        elif case == "latched_second_frame":
            control += struct.pack(">I", 2) + b"{}"
        elif case == "latched_bad_length":
            control += struct.pack(">I", 0)
        elif case == "latched_stream_overflow":
            control += b"x" * 5000
        self.payloads = {201: control, 202: b"x" * 4097 if case == "latched_log_budget" else b"", 203: b""}
        self.offsets = dict.fromkeys(self.readers, 0)
        self.read_calls: list[int] = []
        self.api: Any = SimpleNamespace(read=self.read, peek=self.peek)

    def read_available(self) -> Any:
        # Exercise the frozen pipe order, including failures before it returns.
        return diagnostic.core.NativeWorker.read_available(cast(Any, self))

    def peek(self, handle: int, _buffer: Any, _size: int, _read: Any, available: Any, _left: Any) -> int:
        if handle == 202 and self.case == "latched_ipc_then_peek_error":
            C.set_last_error(87)
            return 0
        remaining = len(self.payloads[handle]) - self.offsets[handle]
        if handle == 202 and self.case == "latched_ipc_then_read_error":
            remaining = 1
        available._obj.value = remaining
        C.set_last_error(0 if remaining else 109)
        return int(bool(remaining))

    def read(self, handle: int, buffer: Any, size: int, count: Any, _overlapped: Any) -> int:
        self.read_calls.append(handle)
        if handle == 202 and self.case == "latched_ipc_then_read_error":
            C.set_last_error(87)
            raise OSError("mock other-pipe read exception")
        data = self.payloads[handle][self.offsets[handle]:self.offsets[handle] + size]
        C.memmove(buffer, data, len(data))
        count._obj.value = len(data)
        self.offsets[handle] += len(data)
        C.set_last_error(777 if self.case == "latched_native_read_error" else 321)
        return 0 if self.case == "latched_native_read_error" else 2

    def poll(self) -> int | None:
        if self.case in ("latched_poll_error", "latched_collect_close_failed", "collect_host_il", "collect_host_policy",
                "collect_host_error", "collect_host_il_error_policy", "collect_empty", "collect_partial"):
            raise LaunchRejected("mock_poll_error", 87)
        if self.case == "latched_deadline":
            return None
        return 10 if self.frame["attack_succeeded"] else 2 if self.frame["unknown"] else 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        assert "raw_result" in json.loads(self.output.getvalue())

    def close(self) -> None:
        self.close_calls += 1
        assert self.close_calls == 1
        if self.case in ("close_failed", "launch_failed", "latched_close_failed", "latched_collect_close_failed"):
            raise LaunchRejected("mock_cleanup_failed", cleanup_owner=self)


def lifecycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, case: str = "ok") -> tuple[dict[str, Any], io.StringIO, FakeNative, list[Any], list[Any]]:
    report: dict[str, Any] = {"mode": mode, "source_sha256": SOURCES, "stage0_passed": False,
        "write_boundary_verified": False, "security_boundary_failed": False, "fixture_body_confirmed": False,
        "worker_code_executed": None, "native_resume_called": False}
    output = io.StringIO()
    frame = fixture_frame(mode, error=87 if case == "unknown87" else 5)
    if case.startswith("latched"):
        successful_set(frame)
        if case == "latched_malformed":
            del frame["after"]["user"]
        elif case == "latched_read_failure":
            frame["after"].update(valid=0, user="")
            frame.update(identity_same=0, problems=1026)
            [row for row in frame["calls"] if row[:2] == [1, 1]][-1][3:] = [0, 5, 0]
    native = FakeNative(frame, output, case)
    deleted: list[Any] = []
    resumes: list[Any] = []
    folder = tmp_path / "fresh-profile"
    folder.mkdir()
    profile = ProbeProfile("deskpal.v06." + "a" * 32, PACKAGE, str(folder))
    fixture, source = tmp_path / "fixed.exe", tmp_path / "fixed.c"
    fixture.write_bytes(b"mock approved exe")
    source.write_bytes(b"mock approved source")

    class Session:
        def __init__(self, _api: Any, _output: Any, data: dict[str, Any]) -> None:
            self.report = data
            self.owners: list[Any] = []
            self.host_queries: list[int] = []
            data.update(native_calls=[], token_queries=[], close_results=[], unknown_reasons=[])
            data["mock_host_query_classes"] = self.host_queries

        def save(self) -> None:
            diagnostic.token._save(output, self.report)

        def unknown(self, reason: str) -> None:
            self.report["unknown_reasons"].append(reason)
            self.report["status"] = "unknown"

        def call(self, name: str, *args: Any) -> dict[str, Any]:
            assert name == "GetWindowsDirectoryW"
            args[0].value = "C:/Windows"
            return {"raw_return": len(args[0].value)}

        def query(self, owner: Any, kind: int, label: str) -> Any:
            assert owner.label == "worker" and owner.handle.value == 22 and kind in (25, 27)
            assert kind not in self.host_queries and len(self.host_queries) < 2
            self.host_queries.append(kind)
            saved = json.loads(output.getvalue())
            assert ("raw_result" in saved or "collect_error" in saved) and label.startswith("host_runtime_after:")
            if (case in ("host_error", "latched_host_error", "collect_host_error")
                    or (case == "host_changed_query_error" and kind == 27)
                    or (case == "collect_host_il_error_policy" and kind == 25)):
                raise OSError("mock independent host query failure")
            if kind == 25:
                return {"sid": "S-1-16-4096", "attributes": 96} if case in ("host_il", "host_changed_query_error", "collect_host_il") else copy.deepcopy(HOST["integrity"])
            return 0 if case in ("host_policy", "collect_host_policy", "collect_host_il_error_policy") else 1

    class Adapter:
        _read_before_resume: Any = REAL_RESUME

        def __init__(self, subject: Any, callback: Any) -> None:
            self.session, self.callback = subject, callback
            self.process_handle, self.resume_intercepted, self.resume_calls = 77, False, 0
            self.creation_flags = 0x8040C
            self.failed_closes: list[Any] = []
            self.withheld_tokens: list[Any] = []
            self.resume, self.smoke_resume = self._read_before_resume, self.native_resume
            self.read, self.peek = native.read, native.peek

        def native_resume(self, handle: Any) -> int:
            saved = json.loads(output.getvalue())
            assert handle == 88 and self.resume_calls == 1 and saved["mode"] == mode
            assert saved["resume_call"]["state"] == "prepared" and saved["worker_after"]["integrity"]["sid"] == "S-1-16-0"
            resumes.append(handle)
            return 1

    def observe(subject: Any, process: int, package: str) -> None:
        assert process == 77 and package == PACKAGE
        owner = diagnostic.token.Owner("worker")
        owner.handle.value = 22
        subject.owners.append(owner)
        subject.report.update(worker_after=copy.deepcopy(HOST), worker_gate_checks={"verified": True})

    def create(spec: Any, *, _api: Any) -> Any:
        assert spec.args == ("{ipc_write_handle}", mode) and spec.appcontainer_sid == PACKAGE
        assert set(spec.environment) == {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA"}
        if case == "launch_failed":
            try:
                native.close()
            except LaunchRejected:
                raise LaunchRejected("launch_failure_reap_unconfirmed", cleanup_owner=native)
        assert _api.resume(88) == 1
        native.api = _api
        return native, IDENTITY

    def source_after(subject: Any) -> None:
        subject.report.update(source_token_unchanged=case != "source_changed", all_token_handles_closed=True)
        for owner in subject.owners:
            owner.handle.value = None

    monkeypatch.setattr(diagnostic, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(diagnostic, "Session", Session)
    monkeypatch.setattr(diagnostic, "NativeApi", lambda: object())
    monkeypatch.setattr(diagnostic, "SmokeApi", Adapter)
    monkeypatch.setattr(diagnostic, "observe_integrity", observe)
    monkeypatch.setattr(diagnostic, "check_job", lambda *_args: None)
    monkeypatch.setattr(diagnostic, "source_after", source_after)
    monkeypatch.setattr(diagnostic, "create_worker", create)
    monkeypatch.setattr(diagnostic, "create_probe_profile", lambda _name: profile)
    monkeypatch.setattr(diagnostic, "delete_probe_profile", deleted.append)
    monkeypatch.setattr(diagnostic, "FIXTURE", fixture)
    monkeypatch.setattr(diagnostic, "FIXTURE_SOURCE", source)
    monkeypatch.setattr(diagnostic, "FIXTURE_SHA256", sha256(fixture.read_bytes()).hexdigest())
    monkeypatch.setattr(diagnostic, "FIXTURE_SOURCE_SHA256", sha256(source.read_bytes()).hexdigest())
    monkeypatch.setattr(diagnostic, "_fingerprints", lambda: {"changed": "source"} if case == "hash" else SOURCES)
    last_error = {"value": 0}
    monkeypatch.setattr(C, "set_last_error", lambda error: last_error.update(value=error), raising=False)
    monkeypatch.setattr(C, "get_last_error", lambda: last_error["value"], raising=False)
    return report, output, native, deleted, resumes


@pytest.mark.parametrize("mode", diagnostic.MODES)
def test_lifecycle_reuses_one_resume_rich_frame_limits_and_safe_deletion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str) -> None:
    report, output, native, deleted, resumes = lifecycle(monkeypatch, tmp_path, mode)
    assert diagnostic._experiment(output, report) == 0
    assert resumes == [88] and native.close_calls == native.terminate_calls == 1 and len(deleted) == 1
    assert report["collect"]["frame_limits"]["max_nodes"] == 512 and report["collect"]["frame_limits"]["max_container_items"] == 64
    assert report["status"] == "own_token_regain_resistance_observed" and report["copied_fixture_unchanged"]
    assert report["host_runtime_matches_prelaunch"] and report["c_after_matches_host_runtime"]
    assert report["stage0_passed"] is report["write_boundary_verified"] is False


@pytest.mark.parametrize("case", ["latched", "latched_malformed", "latched_read_failure", "latched_close_failed", "latched_host_error",
    "close_failed", "launch_failed", "source_changed", "hash", "unknown87"])
def test_failure_latches_dominate_later_errors_and_owners_are_never_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str) -> None:
    report, output, native, deleted, _resumes = lifecycle(monkeypatch, tmp_path, "raise-il", case)
    assert diagnostic._experiment(output, report) == 2
    assert not deleted and native.close_calls == 1
    assert report["status"] == ("security_boundary_failure" if case.startswith("latched") else "unknown")
    if case.startswith("latched"):
        assert report["security_boundary_failed"] and "raw_result" in json.loads(output.getvalue())
    if case in ("close_failed", "launch_failed", "latched_close_failed"):
        assert diagnostic._UNCONFIRMED_OWNERS


@pytest.mark.parametrize("case", ["host_il", "host_policy", "host_changed_query_error", "host_error", "empty_frame"])
def test_independent_host_readback_latches_weakening_and_missing_evidence_stays_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str,
) -> None:
    report, output, native, deleted, _resumes = lifecycle(monkeypatch, tmp_path, "raise-il", case)
    assert diagnostic._experiment(output, report) == 2
    weakened = case in ("host_il", "host_policy", "host_changed_query_error")
    assert report["status"] == ("security_boundary_failure" if weakened else "unknown")
    assert report["security_boundary_failed"] is weakened and not deleted and native.close_calls == 1
    assert report["host_readback_attempted"] is True
    if case == "empty_frame":
        assert report["host_runtime_matches_prelaunch"] and report["fixture_body_confirmed"] is False
        assert report["worker_code_executed"] is None and report["raw_result"]["frames"] == []


@pytest.mark.parametrize("case", ["latched_poll_error", "latched_native_read_error", "latched_log_budget", "latched_deadline",
    "latched_tail_eof", "latched_second_frame", "latched_bad_length", "latched_ipc_then_peek_error",
    "latched_ipc_then_read_error", "latched_collect_close_failed", "latched_stream_overflow"])
def test_actual_read_frame_survives_all_later_collection_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str,
) -> None:
    report, output, native, deleted, resumes = lifecycle(monkeypatch, tmp_path, "raise-il", case)
    if case == "latched_deadline":
        ticks = iter((0.0, 6.0))
        monkeypatch.setattr(launcher, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=forbidden))
    assert diagnostic._experiment(output, report) == 2
    assert resumes == [88] and native.close_calls == 1 and not deleted
    assert report["status"] == "security_boundary_failure" and report["security_boundary_failed"]
    assert not report["observations_valid"] and "raw_result" not in report and "collect_error" in report
    assert report["mock_host_query_classes"] == [25, 27] and report["host_readback_attempted"]
    evidence = report["read_evidence"]
    assert evidence["collection_returned"] is False and evidence["frames"] == [native.frame]
    assert json.loads(output.getvalue())["read_evidence"]["frames"] == [native.frame]
    for role, handle in zip(("control", "stdout", "stderr"), native.readers, strict=True):
        channel = evidence["channels"][role]
        limit = 4101 if role == "control" else 4097
        expected = native.payloads[handle][:limit] if handle in native.read_calls else b""
        assert bytes.fromhex(channel["prefix_hex"]) == expected
        assert channel["retained_bytes"] == len(expected) <= limit
        assert channel["truncated"] == (channel["observed_bytes"] > limit)
    assert native.read_calls.count(201) == 1
    if case == "latched_native_read_error":
        assert evidence["events"][0]["raw_return"] == 0 and evidence["events"][0]["last_error"] == 777
    if case == "latched_collect_close_failed":
        assert report["collect_error"]["context_error"] == "mock_poll_error (winerror=87)"
        assert diagnostic._UNCONFIRMED_OWNERS and report["worker_owner_disposition"] == "retained_no_automatic_retry"
    if case in ("latched_second_frame", "latched_bad_length", "latched_tail_eof", "latched_stream_overflow"):
        assert evidence["shadow_protocol_violation"]


@pytest.mark.parametrize("case,weakened", [("collect_host_il", True), ("collect_host_policy", True),
    ("collect_host_error", False), ("collect_host_il_error_policy", True), ("collect_empty", False), ("collect_partial", False)])
def test_collection_failure_still_queries_each_retained_token_class_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str, weakened: bool,
) -> None:
    report, output, native, deleted, _resumes = lifecycle(monkeypatch, tmp_path, "raise-il", case)
    assert diagnostic._experiment(output, report) == 2
    assert report["status"] == ("security_boundary_failure" if weakened else "unknown")
    assert report["security_boundary_failed"] is weakened and not report["observations_valid"]
    assert report["mock_host_query_classes"] == [25, 27] and not deleted and native.close_calls == 1
    assert "mock_poll_error" in report["collect_error"]["error"] and "raw_result" not in report
    evidence = json.loads(output.getvalue())["read_evidence"]
    assert bytes.fromhex(evidence["channels"]["control"]["prefix_hex"]) == native.payloads[201]
    if case in ("collect_empty", "collect_partial"):
        assert evidence["frames"] == [] and report["fixture_body_confirmed"] is False
    if case in ("collect_host_error", "collect_host_il_error_policy"):
        assert report["host_readback_error"] and report["host_readback_errors"]
        assert "mock_poll_error" in report["error"]


def read_observer(monkeypatch: pytest.MonkeyPatch, original: Any) -> tuple[Any, dict[str, Any], io.StringIO]:
    report: dict[str, Any] = {"unknown_reasons": [], "security_boundary_failed": False}
    output = io.StringIO()
    last_error = {"value": 0}
    monkeypatch.setattr(C, "set_last_error", lambda value: last_error.update(value=value), raising=False)
    monkeypatch.setattr(C, "get_last_error", lambda: last_error["value"], raising=False)
    session = SimpleNamespace(report=report, save=lambda: diagnostic.token._save(output, report),
        unknown=lambda reason: report["unknown_reasons"].append(reason))
    native = SimpleNamespace(readers=(201, 202, 203), api=SimpleNamespace(read=original))
    diagnostic._ReadEvidence(cast(Any, session), native)
    return native.api.read, report, output


@pytest.mark.parametrize("failure", [False, True])
def test_read_observer_preserves_one_call_raw_bool_last_error_and_partial_output(
    monkeypatch: pytest.MonkeyPatch, failure: bool,
) -> None:
    calls: list[Any] = []
    frame = fixture_frame("raise-il")
    successful_set(frame)
    body = json.dumps(frame).encode("ascii")
    payload = struct.pack(">I", len(body)) + body

    def read(*args: Any) -> int:
        calls.append(args)
        C.memmove(args[1], payload, len(payload))
        args[3]._obj.value = len(payload)
        C.set_last_error(1234)
        if failure:
            raise OSError("mock read wrapper failure after bounded output")
        return 2

    wrapped, report, output = read_observer(monkeypatch, read)
    buffer, count = C.create_string_buffer(len(payload)), diagnostic.core.DWORD()
    args = (201, buffer, len(payload), C.byref(count), None)
    if failure:
        with pytest.raises(OSError, match="mock read wrapper failure"):
            wrapped(*args)
    else:
        assert wrapped(*args) == 2
    assert len(calls) == 1 and calls[0] == args and C.get_last_error() == 1234
    assert buffer.raw == payload and count.value == len(payload)
    assert report["read_evidence"]["events"][0]["raw_return"] == (None if failure else 2)
    assert report["read_evidence"]["events"][0]["last_error"] == 1234
    assert report["security_boundary_failed"] and report["read_evidence"]["frames"] == [frame]
    assert bytes.fromhex(json.loads(output.getvalue())["read_evidence"]["channels"]["control"]["prefix_hex"]) == payload


def test_read_event_budget_still_keeps_and_latches_first_complete_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    frame = fixture_frame("raise-il")
    successful_set(frame)
    body = json.dumps(frame).encode("ascii")
    payload = struct.pack(">I", len(body)) + body

    def read(_handle: Any, buffer: Any, _size: Any, count: Any, _overlapped: Any) -> int:
        nonlocal calls
        calls += 1
        data = payload if calls == diagnostic.MAX_READ_EVENTS + 1 else b""
        C.memmove(buffer, data, len(data))
        count._obj.value = len(data)
        C.set_last_error(19)
        return 2

    wrapped, report, output = read_observer(monkeypatch, read)
    for _ in range(diagnostic.MAX_READ_EVENTS + 2):
        wrapped(201, C.create_string_buffer(len(payload)), len(payload), C.byref(diagnostic.core.DWORD()), None)
    evidence = report["read_evidence"]
    assert calls == diagnostic.MAX_READ_EVENTS + 2 and len(evidence["events"]) == diagnostic.MAX_READ_EVENTS
    assert evidence["events_withheld"] == 2 and evidence["first_withheld_event"]["state"] == "event_budget"
    assert report["unknown_reasons"] and report["security_boundary_failed"] and evidence["frames"] == [frame]
    assert len(output.getvalue().encode("ascii")) <= diagnostic.token.MAX_REPORT


@pytest.mark.parametrize("role,handle", [("stdout", 202), ("stderr", 203)])
def test_log_read_prefix_is_bounded_and_never_becomes_control_evidence(
    monkeypatch: pytest.MonkeyPatch, role: str, handle: int,
) -> None:
    calls = 0
    frame = fixture_frame("raise-il")
    successful_set(frame)
    body = json.dumps(frame).encode("ascii")
    payload = (struct.pack(">I", len(body)) + body).ljust(8192, b"x")

    def read(_handle: Any, buffer: Any, _size: Any, count: Any, _overlapped: Any) -> int:
        nonlocal calls
        calls += 1
        C.memmove(buffer, payload, len(payload))
        count._obj.value = len(payload)
        C.set_last_error(81)
        return 2

    wrapped, report, _output = read_observer(monkeypatch, read)
    buffer, count = C.create_string_buffer(len(payload)), diagnostic.core.DWORD()
    assert wrapped(handle, buffer, len(payload), C.byref(count), None) == 2
    channel = report["read_evidence"]["channels"][role]
    assert calls == 1 and C.get_last_error() == 81 and buffer.raw == payload and count.value == 8192
    assert channel["observed_bytes"] == 8192 and channel["retained_bytes"] == 4097 and channel["truncated"]
    assert bytes.fromhex(channel["prefix_hex"]) == payload[:4097]
    assert not report["security_boundary_failed"] and report["read_evidence"]["frames"] == []


@pytest.mark.parametrize("damage", ["request_size", "returned_count", "count_pointer", "unregistered_reader"])
def test_read_observer_rejects_unknown_abi_without_pointer_dereference_or_retry(
    monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    calls = 0

    def read(_handle: Any, _buffer: Any, size: int, count: Any, _overlapped: Any) -> int:
        nonlocal calls
        calls += 1
        count._obj.value = size + 1 if damage == "returned_count" else 0
        C.set_last_error(456)
        return 2

    wrapped, report, _output = read_observer(monkeypatch, read)
    size = 16385 if damage == "request_size" else 16
    count = diagnostic.core.DWORD()
    pointer = C.byref(count, 1) if damage == "count_pointer" else C.byref(count)
    assert wrapped(999 if damage == "unregistered_reader" else 201, C.create_string_buffer(size), size, pointer, None) == 2
    assert calls == 1 and C.get_last_error() == 456 and report["unknown_reasons"]
    assert report["read_evidence"]["channels"]["control"]["prefix_hex"] == ""


def test_unbuilt_sentinel_fails_before_api_or_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostic, "FIXTURE_SHA256", "UNBUILT")
    monkeypatch.setattr(diagnostic, "_fingerprints", lambda: SOURCES)
    report = {"mode": "raise-il", "source_sha256": SOURCES}
    assert diagnostic._experiment(io.StringIO(), report) == 2
    assert report["status"] == "unknown" and "unbuilt" in report["error"] and "profile" not in report


def test_no_optin_and_fixed_mode_are_persisted_without_native(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    monkeypatch.setattr(diagnostic, "new_report", lambda _path: path)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", str(path), "--mode", "disable-no-write-up"])
    assert diagnostic.main() == 0
    saved = json.loads(path.read_text(encoding="ascii"))
    assert saved["mode"] == "disable-no-write-up" and saved["status"] == "not_executed_explicit_opt_in_required"
    assert not saved["security_boundary_failed"] and not saved["native_resume_called"]


@pytest.mark.parametrize("extra", [["--mode", "arbitrary"], ["--exe", "arbitrary"], []])
def test_cli_rejects_arbitrary_target_or_missing_mode_before_selection(monkeypatch: pytest.MonkeyPatch, extra: list[str]) -> None:
    monkeypatch.setattr(diagnostic, "new_report", forbidden)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", "unused.json", "--experiment", *extra])
    with pytest.raises(SystemExit) as error:
        diagnostic.main()
    assert error.value.code == 2
