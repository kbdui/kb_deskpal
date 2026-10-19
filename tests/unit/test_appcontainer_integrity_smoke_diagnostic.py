"""Fixed-smoke mocks; all native/profile/worker entry points are tripwired."""

from __future__ import annotations

import ctypes as C
from hashlib import sha256
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

from deskpal.execution.windows.launcher import WorkerIdentity, WorkerResult
from deskpal.execution.runtime import ProbeProfile as ProbeProfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import diagnose_appcontainer_integrity_smoke as diagnostic
from diagnose_appcontainer_integrity_smoke import SmokeWorker as SmokeWorker

REAL_API = diagnostic.SmokeApi
PACKAGE = "S-1-15-2-1-2-3-4-5-6-7"
SOURCES = {"mock-fixed-source": "unchanged"}
IDENTITY = WorkerIdentity(77, PACKAGE, True, None, 0, 1, 0x3204, 268435456,
    268435456, 1, 5, 2000, True, (1, 2, 3, 4), 99)
FRAME = {"probe": "native", "excluded_handle_valid": False, "excluded_handle_error": 6,
    "excluded_handle_exception": 0, "operation_error": 0, "allocation_count": 0}


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


class FakeNative:
    def __init__(self, case: str, output: io.StringIO) -> None:
        self.case, self.output = case, output
        self.close_calls = self.terminate_calls = 0
        self.adapter: Any = None

    def read_available(self) -> Any:
        if self.case == "invalid_protocol":
            data = bytes((255, 255, 255, 255))
        elif self.case == "empty_frame":
            data = b""
        else:
            frame = dict(FRAME)
            if self.case == "invalid_schema":
                frame["unexpected"] = True
            body = json.dumps(frame).encode("ascii")
            data = struct.pack(">I", len(body)) + body
        return (data, b"", b""), (True, True, True)

    def poll(self) -> int:
        return 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        saved = json.loads(self.output.getvalue())
        assert "raw_result" in saved  # Result persisted before explicit stop.
        if self.case == "terminate_failed":
            raise diagnostic.LaunchRejected("mock_terminate_failed", cleanup_owner=self)

    def close(self) -> None:
        self.close_calls += 1
        assert self.close_calls == 1
        if self.case in ("close_failed", "launch_cleanup_failed"):
            raise diagnostic.LaunchRejected("mock_close_failed", cleanup_owner=self)
        if self.case == "core_close_false":
            self.adapter.failed_closes.append({"raw_return": 0, "last_error": 5})


def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str = "ok") -> tuple[dict[str, Any], io.StringIO, FakeNative, list[Any], list[Any]]:
    output = io.StringIO()
    report: dict[str, Any] = {"source_sha256": SOURCES, "stage0_passed": False,
        "write_boundary_verified": False, "fixture_body_confirmed": False, "worker_code_executed": None,
        "native_resume_called": False}
    native = FakeNative(case, output)
    deleted: list[Any] = []
    resume_calls: list[Any] = []
    fixture, source = tmp_path / "fixed.exe", tmp_path / "fixed.c"
    fixture.write_bytes(b"mock pinned executable")
    source.write_bytes(b"mock pinned source")
    folder = tmp_path / "fresh-profile"
    folder.mkdir()
    profile = ProbeProfile("deskpal.v06." + "a" * 32, PACKAGE, str(folder))

    class Session:
        def __init__(self, _api: Any, _output: Any, data: dict[str, Any]) -> None:
            self.report = data
            self.owners: list[Any] = []
            data.update(native_calls=[], token_queries=[], close_results=[], unknown_reasons=[])

        def save(self) -> None:
            if case == "prepare_save" and self.report.get("resume_call", {}).get("state") == "prepared":
                raise ValueError("prepared save failed")
            diagnostic.token._save(output, self.report)

        def call(self, name: str, *args: Any) -> dict[str, Any]:
            assert name == "GetWindowsDirectoryW"
            args[0].value = "C:/Windows"
            return {"raw_return": len(args[0].value)}

        def unknown(self, reason: str) -> None:
            self.report["unknown_reasons"].append(reason)

        def host_snapshot(self, _owner: Any, label: str) -> Any:
            value = {"modified_id": 2 if case == "source_changed" else 1}
            self.report[label] = value
            return value

        def close(self) -> bool:
            self.report["all_token_handles_closed"] = case != "token_close"
            if case != "token_close":
                for owner in self.owners:
                    owner.handle.value = None
            return case != "token_close"

    def observe(subject: Any, process: int, package: str) -> None:
        assert process == 77 and package == PACKAGE
        owner = diagnostic.token.Owner("source")
        owner.handle.value = 11
        subject.owners.append(owner)
        subject.report.update(source_before={"modified_id": 1}, worker_before={"integrity": "S-1-16-4096"},
            worker_after={"integrity": "S-1-16-0"}, worker_gate_checks={"exact_il": case != "il_gate"})

    class Adapter:
        _read_before_resume: Any = REAL_API._read_before_resume

        def __init__(self, subject: Any, callback: Any) -> None:
            self.session, self.callback = subject, callback
            self.process_handle, self.resume_intercepted, self.resume_calls = 77, False, 0
            self.creation_flags = 0x8040C
            self.failed_closes: list[Any] = []
            self.withheld_tokens: list[Any] = []
            self.resume = self._read_before_resume
            self.smoke_resume = self.native_resume

        def native_resume(self, handle: Any) -> int:
            assert handle == 88 and self.resume_calls == 1
            saved = json.loads(output.getvalue())
            assert saved["resume_call"]["state"] == "prepared" and not saved["resume_call"]["attempted"]
            assert saved["worker_after"]["integrity"] == "S-1-16-0"
            assert saved["lowering_and_identity_observed"] is True
            resume_calls.append(handle)
            return 0xFFFFFFFF if case == "resume_error" else 1

    def create(spec: Any, *, _api: Any) -> Any:
        assert spec.args == diagnostic.ARGS
        assert set(spec.environment) == {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA"}
        native.adapter = _api
        try:
            if case == "launch_cleanup_failed":
                raise ValueError("initial launch failed")
            assert _api.resume(88) == 1
        except BaseException as error:
            try:
                native.close()  # Mimic the original adapter's first cleanup.
            except BaseException:
                raise diagnostic.LaunchRejected("launch_failure_reap_unconfirmed", cleanup_owner=native) from error
            raise
        return native, IDENTITY

    class CheckedWorker(SmokeWorker):
        def collect(self, timeout_seconds: float = 30.0, *, frame_limits: Any = None,
                    log_limit_bytes: int = 0) -> WorkerResult:
            assert timeout_seconds == 5.0 and frame_limits is diagnostic.FRAME_LIMITS and log_limit_bytes == 4096
            assert frame_limits.max_frame_bytes == 4096 and frame_limits.max_stream_bytes == 4100
            return super().collect(timeout_seconds, frame_limits=frame_limits, log_limit_bytes=log_limit_bytes)

    monkeypatch.setattr(diagnostic, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(diagnostic, "Session", Session)
    monkeypatch.setattr(diagnostic, "NativeApi", lambda: object())
    monkeypatch.setattr(diagnostic, "SmokeApi", Adapter)
    monkeypatch.setattr(diagnostic, "SmokeWorker", CheckedWorker)
    monkeypatch.setattr(diagnostic, "observe_integrity", observe)
    monkeypatch.setattr(diagnostic, "check_job", lambda _api, _spec, _process, data: data.update(post_lower_job={"verified": True}))
    monkeypatch.setattr(diagnostic, "thread_absent", lambda subject, label: subject.report.update({label: {"status": "absent"}}))
    monkeypatch.setattr(diagnostic, "create_worker", create)
    monkeypatch.setattr(diagnostic, "create_probe_profile", lambda _name: profile)
    monkeypatch.setattr(diagnostic, "delete_probe_profile", deleted.append)
    monkeypatch.setattr(diagnostic, "FIXTURE", fixture)
    monkeypatch.setattr(diagnostic, "FIXTURE_SOURCE", source)
    monkeypatch.setattr(diagnostic, "FIXTURE_SHA256", sha256(fixture.read_bytes()).hexdigest())
    monkeypatch.setattr(diagnostic, "FIXTURE_SOURCE_SHA256", sha256(source.read_bytes()).hexdigest())
    monkeypatch.setattr(diagnostic, "_fingerprints", lambda: {"changed": "source"} if case == "hash" else SOURCES)
    monkeypatch.setattr(C, "set_last_error", lambda _error: None, raising=False)
    monkeypatch.setattr(C, "get_last_error", lambda: 5 if case == "resume_error" else 0, raising=False)
    return report, output, native, deleted, resume_calls


def test_fixed_smoke_resumes_once_after_persisted_il_gate_and_saves_before_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report, output, native, deleted, resume = setup(monkeypatch, tmp_path)
    assert diagnostic._experiment(output, report) == 0
    saved = json.loads(output.getvalue())
    assert resume == [88] and saved["actual_resume_calls"] == 1
    assert saved["fixture_body_confirmed"] and saved["worker_code_executed"] is True
    assert native.terminate_calls == native.close_calls == 1 and len(deleted) == 1
    assert saved["copied_fixture_unchanged"] and saved["source_token_unchanged"]
    assert saved["stage0_passed"] is saved["write_boundary_verified"] is False


@pytest.mark.parametrize("case", ["il_gate", "prepare_save", "resume_error"])
def test_resume_gate_save_or_native_error_never_retries_or_deletes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str) -> None:
    report, output, native, deleted, resume = setup(monkeypatch, tmp_path, case)
    assert diagnostic._experiment(output, report) == 2
    assert len(resume) == int(case == "resume_error") and native.close_calls == 1 and not deleted
    if case == "resume_error":
        assert report["resume_call"]["raw_return"] == 0xFFFFFFFF and report["resume_call"]["last_error"] == 5
    assert report["fixture_body_confirmed"] is False and report["worker_code_executed"] is None


@pytest.mark.parametrize("case", ["invalid_protocol", "invalid_schema", "empty_frame"])
def test_invalid_or_absent_frame_stays_unknown_and_preserves_available_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str) -> None:
    report, output, native, deleted, _resume = setup(monkeypatch, tmp_path, case)
    assert diagnostic._experiment(output, report) == 2
    assert native.close_calls == 1 and native.terminate_calls == 0 and not deleted
    assert report["status"] == "unknown" and report["worker_code_executed"] is None
    if case == "invalid_protocol":
        assert report["protocol_violation"] == "frame_budget"
    else:
        assert "raw_result" in report
        assert report["raw_result"]["frames"] == ([dict(FRAME, unexpected=True)] if case == "invalid_schema" else [])


@pytest.mark.parametrize("case", ["close_failed", "terminate_failed", "launch_cleanup_failed", "core_close_false", "source_changed", "hash", "token_close"])
def test_failure_owners_are_not_retried_and_unknown_profiles_are_retained(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str) -> None:
    report, output, native, deleted, _resume = setup(monkeypatch, tmp_path, case)
    assert diagnostic._experiment(output, report) == 2
    assert not deleted and report["status"] == "unknown" and native.close_calls <= 1
    if case in ("close_failed", "terminate_failed", "launch_cleanup_failed", "core_close_false", "token_close"):
        assert diagnostic._UNCONFIRMED_OWNERS
    if case == "terminate_failed":
        assert native.close_calls == 0
    if case == "launch_cleanup_failed":
        assert native.close_calls == 1 and report["launch_owner_disposition"] == "retained_no_automatic_retry"


def test_no_optin_never_touches_fixture_profile_or_native(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    monkeypatch.setattr(diagnostic, "new_report", lambda _path: path)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", str(path)])
    assert diagnostic.main() == 0
    saved = json.loads(path.read_text(encoding="ascii"))
    assert saved["status"] == "not_executed_explicit_opt_in_required" and not saved["native_resume_called"]
    assert saved["fixture_body_confirmed"] is False and saved["fixed_args"] == list(diagnostic.ARGS)


@pytest.mark.parametrize("argument", ["--exe", "--mode", "--command"])
def test_cli_rejects_arbitrary_executable_or_mode_before_target_selection(monkeypatch: pytest.MonkeyPatch, argument: str) -> None:
    monkeypatch.setattr(diagnostic, "new_report", forbidden)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", "unused.json", "--experiment", argument, "arbitrary"])
    with pytest.raises(SystemExit) as error:
        diagnostic.main()
    assert error.value.code == 2
