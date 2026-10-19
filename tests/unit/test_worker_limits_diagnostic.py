"""Pure-mock safety checks for the fixed worker-limits diagnostic."""

from __future__ import annotations

import ctypes
from hashlib import sha256
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import diagnose_worker_limits as diagnostic
from deskpal.execution.runtime import ProbeProfile
from deskpal.execution.windows import JobLimits, LaunchRejected, LaunchSpec, WorkerIdentity, WorkerResult
from deskpal.execution.windows import _win32


@pytest.fixture(autouse=True)
def _reset_unconfirmed_cleanup_owners() -> Any:
    diagnostic._UNCONFIRMED_OWNERS.clear()
    yield
    diagnostic._UNCONFIRMED_OWNERS.clear()


def _identity() -> WorkerIdentity:
    return WorkerIdentity(123, "S-1-15-2-1-2-3-4-5-6-7", True, None, 0,
                          1, 0x2308, 256 * 1024 * 1024, 256 * 1024 * 1024,
                          1, 5, 2000, True, (20, 24, 28, 32), 36)


def _spec(tmp_path: Path, *, mode: str = "smoke", limits: JobLimits | None = None) -> LaunchSpec:
    return LaunchSpec(tmp_path / "fixture.exe", ("{ipc_write_handle}", "{excluded_handle}", mode),
                      tmp_path, "S-1-15-2-1-2-3-4-5-6-7",
                      {"SYSTEMROOT": "C:\\Windows", "WINDIR": "C:\\Windows"},
                      limits=limits or JobLimits())


def _valid_frame(*, operation_error: int = 0, allocation_count: int = 0) -> dict[str, object]:
    return {"probe": "native", "excluded_handle_valid": False, "excluded_handle_error": 0,
            "excluded_handle_exception": 0xC0000008, "operation_error": operation_error,
            "allocation_count": allocation_count}


def _result(*, operation_error: int = 0, allocation_count: int = 0) -> WorkerResult:
    return WorkerResult(_identity(), 0, (_valid_frame(operation_error=operation_error,
                                                       allocation_count=allocation_count),),
                        b"", b"", 0.01)


class _ObserverApi:
    def __init__(self, *, active_processes: int = 0, wait_status: int = 0,
                 failed_query_kind: int | None = None) -> None:
        self.duplicates: list[tuple[int, int, int, bool, int]] = []
        self.close_calls: list[int] = []
        self.fail_duplicate_at: int | None = None
        self.fail_close: set[int] = set()
        self.failed_close_once: set[int] = set()
        self.active_processes = active_processes
        self.wait_status = wait_status
        self.failed_query_kind = failed_query_kind
        self.job = 801
        self.process = 802
        self.query_kinds: list[int] = []
        self.handle_count_value = 40

    @property
    def current(self) -> int:
        return 700

    def duplicate(self, source_process: int, source_handle: int, target_process: int,
                  target: Any, access: int, inherit: int, options: int) -> int:
        self.duplicates.append((source_handle, access, inherit, bool(inherit), options))
        if self.fail_duplicate_at == len(self.duplicates):
            ctypes.set_last_error(5)
            return 0
        value = self.job if len(self.duplicates) == 1 else self.process
        ctypes.cast(target, ctypes.POINTER(_win32.HANDLE)).contents.value = value
        return 1

    def close_handle(self, handle: int) -> int:
        self.close_calls.append(handle)
        if handle in self.fail_close or handle in self.failed_close_once:
            self.failed_close_once.discard(handle)
            ctypes.set_last_error(5)
            return 0
        return 1

    def query_job(self, _handle: int, kind: int, output: Any,
                  _size: int, _needed: object) -> int:
        self.query_kinds.append(kind)
        if kind == self.failed_query_kind:
            ctypes.set_last_error(5)
            return 0
        if kind == 1:
            accounting = ctypes.cast(output, ctypes.POINTER(_win32.JOB_ACCOUNTING)).contents
            accounting.ActiveProcesses = self.active_processes
            accounting.TotalProcesses = 1
        elif kind == 9:
            limits = ctypes.cast(output, ctypes.POINTER(_win32.JOB_EXTENDED_LIMIT)).contents
            limits.ProcessMemoryLimit = 256 * 1024 * 1024
            limits.JobMemoryLimit = 256 * 1024 * 1024
        return 1

    def wait(self, _handle: int, _timeout: int) -> int:
        return self.wait_status

    def exit_code(self, _handle: int, output: Any) -> int:
        ctypes.cast(output, ctypes.POINTER(_win32.DWORD)).contents.value = 0
        return 1

    def handle_count(self) -> dict[str, object]:
        return {"available": True, "count": self.handle_count_value}


class _Owner:
    def __init__(self, error: LaunchRejected | None = None) -> None:
        self.error = error
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.error is not None:
            error, self.error = self.error, None
            raise error


class _Worker(_Owner):
    def __init__(self, api: _ObserverApi, result: WorkerResult | None = None,
                 collect_error: Exception | None = None) -> None:
        super().__init__()
        self._native = SimpleNamespace(job=701, process=702)
        self.identity = _identity()
        self.result = result or _result()
        self.collect_error = collect_error
        self.api = api

    def collect(self, _timeout: float, *, log_limit_bytes: int) -> WorkerResult:
        assert log_limit_bytes == diagnostic.LOG_LIMIT
        if self.collect_error is not None:
            raise self.collect_error
        return self.result


class _Writer:
    def __init__(self, fail_on: int | None = None) -> None:
        self.path = Path("synthetic-report.json")
        self.calls = 0
        self.fail_on = fail_on
        self.saved: list[dict[str, object]] = []

    def save(self, report: dict[str, object]) -> None:
        self.calls += 1
        if self.calls == self.fail_on:
            self.fail_on = None
            raise OSError("synthetic report save failure")
        self.saved.append(dict(report))


def _matrix_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
    writer: _Writer | None = None, delete_error: Exception | None = None,
) -> tuple[_Writer, list[ProbeProfile], list[ProbeProfile], Path, Path]:
    root = tmp_path / "repo"
    (root / "data/validation/v06").mkdir(parents=True)
    source = tmp_path / "fixture.exe"
    source.write_bytes(b"synthetic fixture bytes")
    folder = tmp_path / "profile" / "AC"
    folder.mkdir(parents=True)
    profile = ProbeProfile("deskpal.v06.mock", "S-1-15-2-1-2-3-4-5-6-7", str(folder))
    created: list[ProbeProfile] = []
    deleted: list[ProbeProfile] = []

    def create_profile(_name: str) -> ProbeProfile:
        created.append(profile)
        return profile

    def delete_profile(item: ProbeProfile) -> None:
        deleted.append(item)
        if delete_error is not None:
            raise delete_error

    monkeypatch.setattr(diagnostic, "ROOT", root)
    monkeypatch.setattr(diagnostic, "FIXTURE_SHA", sha256(source.read_bytes()).hexdigest())
    monkeypatch.setattr(diagnostic, "_no_reparse", lambda _path: None)
    monkeypatch.setattr(diagnostic, "create_probe_profile", create_profile)
    monkeypatch.setattr(diagnostic, "delete_probe_profile", delete_profile)
    monkeypatch.setattr(diagnostic, "source_fingerprints", lambda: {"synthetic": "fixed"})
    return writer or _Writer(), created, deleted, source, root


@pytest.mark.parametrize("mode", ["memory", "unknown"])
def test_unknown_and_unexecuted_memory_modes_reject_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    launched: list[LaunchSpec] = []
    monkeypatch.setattr(diagnostic, "WindowsLauncher",
                        lambda: SimpleNamespace(launch=lambda spec: launched.append(spec)))

    with pytest.raises(ValueError, match="limits_unknown_or_mismatched_mode"):
        diagnostic.run_mode(mode, _spec(tmp_path, mode=mode), cast(Any, _ObserverApi()))

    assert launched == []


def test_mode_argument_mismatch_rejects_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[LaunchSpec] = []
    monkeypatch.setattr(diagnostic, "WindowsLauncher",
                        lambda: SimpleNamespace(launch=lambda spec: launched.append(spec)))

    with pytest.raises(ValueError, match="limits_unknown_or_mismatched_mode"):
        diagnostic.run_mode("smoke", _spec(tmp_path, mode="spin"), cast(Any, _ObserverApi()))

    assert launched == []


def test_observer_duplicates_use_reduced_noninheritable_access_and_close_first_on_second_failure() -> None:
    api = _ObserverApi()
    api.fail_duplicate_at = 2
    observer = diagnostic.ObservationHandles(cast(Any, api))
    worker = cast(Any, SimpleNamespace(_native=SimpleNamespace(job=701, process=702)))

    with pytest.raises(LaunchRejected, match="limits_duplicate_process"):
        observer.attach(worker)

    assert api.duplicates == [(701, 0x4, 0, False, 0), (702, 0x101000, 0, False, 0)]
    assert observer.job == api.job
    assert observer.process is None
    observer.close()
    assert api.close_calls == [api.job]


def test_case_cleanup_adds_distinct_cleanup_owner_and_retry_converges() -> None:
    api = _ObserverApi()
    distinct = _Owner()
    original = _Owner(LaunchRejected("synthetic_close", cleanup_owner=distinct))
    cleanup = diagnostic.CaseCleanup(cast(Any, api))
    cleanup.add(cast(Any, original))
    cleanup.launch_resolved = True

    with pytest.raises(LaunchRejected, match="limits_cleanup_unconfirmed") as failure:
        cleanup.close()

    assert failure.value.cleanup_owner is cleanup
    assert cleanup.owners == [original, distinct]
    assert original.close_calls == 1
    assert distinct.close_calls == 0
    cleanup.close()
    assert original.close_calls == 2
    assert distinct.close_calls == 1
    assert cleanup.closed
    assert cleanup.observation_confirmed


@pytest.mark.parametrize(
    ("launch_resolved", "active_processes", "wait_status", "failed_query_kind"),
    [
        (False, 0, 0, None),
        (True, 0, 0, 1),
        (True, 1, 0, None),
        (True, 0, 258, None),
    ],
    ids=["unknown-launch", "job-query-failure", "active-process-remains", "process-still-running"],
)
def test_unconfirmed_launch_or_reap_keeps_owner_and_observer_handles(
    launch_resolved: bool, active_processes: int, wait_status: int,
    failed_query_kind: int | None,
) -> None:
    api = _ObserverApi(active_processes=active_processes, wait_status=wait_status,
                       failed_query_kind=failed_query_kind)
    cleanup = diagnostic.CaseCleanup(cast(Any, api))
    cleanup.launch_resolved = launch_resolved
    cleanup.worker_returned = launch_resolved
    cleanup.observers.job = api.job
    cleanup.observers.process = api.process
    diagnostic._UNCONFIRMED_OWNERS.clear()
    entry: dict[str, object] = {}

    assert not diagnostic._cleanup_case(cleanup, entry)
    assert entry["cleanup_confirmed"] is False
    assert entry["worker_reap"] == "UNCONFIRMED_owner_retained"
    assert diagnostic._UNCONFIRMED_OWNERS == [cleanup]
    assert api.close_calls == []


def test_observer_close_attempts_both_and_does_not_repeat_successful_close() -> None:
    api = _ObserverApi()
    api.failed_close_once.add(api.process)
    observer = diagnostic.ObservationHandles(cast(Any, api))
    observer.process = api.process
    observer.job = api.job

    with pytest.raises(LaunchRejected, match="limits_close_observer"):
        observer.close()

    assert api.close_calls == [api.process, api.job]
    assert observer.process == api.process
    assert observer.job is None
    observer.close()
    assert api.close_calls == [api.process, api.job, api.process]
    assert observer.process is None


def test_report_writer_refuses_existing_ads_reserved_reparse_and_oversized_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    report_root = root / "data/validation/v06"
    report_root.mkdir(parents=True)
    monkeypatch.setattr(diagnostic, "ROOT", root)

    old_report = report_root / "old.json"
    old_report.write_text('{"old":true}', encoding="ascii")
    with pytest.raises(ValueError, match="limits_report_requires_new_project_validation_file"):
        diagnostic.ReportWriter(old_report)
    assert old_report.read_text(encoding="ascii") == '{"old":true}'

    with pytest.raises(ValueError, match="limits_report_requires_new_project_validation_file"):
        diagnostic.ReportWriter(report_root / "report.json:stream")
    with pytest.raises(ValueError, match="limits_report_requires_new_project_validation_file"):
        diagnostic.ReportWriter(report_root / "CON.json")

    real_no_reparse = diagnostic._no_reparse

    def reject_reparse(path: Path) -> None:
        if path.name == "reparse.json":
            raise ValueError("limits_reparse_ancestor")
        real_no_reparse(path)

    monkeypatch.setattr(diagnostic, "_no_reparse", reject_reparse)
    with pytest.raises(ValueError, match="limits_reparse_ancestor"):
        diagnostic.ReportWriter(report_root / "reparse.json")

    writer = diagnostic.ReportWriter(report_root / "large.json")
    try:
        with pytest.raises(ValueError, match="limits_report_budget"):
            writer.save({"payload": "x" * (256 * 1024)})
    finally:
        writer.close()


def test_collect_matrix_deletes_profile_when_first_post_creation_save_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer, created, deleted, fixture, _root = _matrix_inputs(
        tmp_path, monkeypatch, writer=_Writer(fail_on=2),
    )
    report: dict[str, object] = {"source_sha256": {"synthetic": "fixed"}}

    code = diagnostic.collect_matrix(cast(Any, writer), report, [], fixture, "C:\\Windows",
                                     cast(Any, _ObserverApi()))

    assert code == 2
    assert len(created) == 1
    assert deleted == created
    assert report["status"] == "limits_diagnostic_failed"
    assert report["cleanup"] == "profile_deleted"
    assert writer.calls == 3  # initial save, failed post-create save, final cleanup save


def test_collect_matrix_withholds_delete_when_run_mode_does_not_return_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer, created, deleted, fixture, _root = _matrix_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(diagnostic, "MODES", ("smoke",))

    def interrupted(*_args: object) -> tuple[dict[str, object], bool]:
        raise RuntimeError("synthetic interrupt before safety result")

    monkeypatch.setattr(diagnostic, "run_mode", interrupted)
    report: dict[str, object] = {"source_sha256": {"synthetic": "fixed"}}
    code = diagnostic.collect_matrix(cast(Any, writer), report, [], fixture, "C:\\Windows",
                                     cast(Any, _ObserverApi()))

    assert code == 2
    assert len(created) == 1
    assert deleted == []
    assert report["cleanup"] == "withheld_unconfirmed_owner"


def test_unknown_launch_does_not_confirm_cleanup_or_allow_profile_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _ObserverApi()
    diagnostic._UNCONFIRMED_OWNERS.clear()

    def unknown_launch(_spec: LaunchSpec) -> object:
        raise RuntimeError("synthetic launch result unavailable")

    monkeypatch.setattr(diagnostic, "WindowsLauncher",
                        lambda: SimpleNamespace(launch=unknown_launch))
    entry, safe = diagnostic.run_mode("smoke", _spec(tmp_path), cast(Any, api))

    assert safe is False
    assert entry["worker_reap"] == "UNCONFIRMED_owner_retained"
    assert diagnostic._UNCONFIRMED_OWNERS


def test_collect_matrix_reports_delete_failure_as_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer, created, deleted, fixture, _root = _matrix_inputs(
        tmp_path, monkeypatch, delete_error=OSError("synthetic delete failure"),
    )
    monkeypatch.setattr(diagnostic, "MODES", ("smoke",))

    def safe_mode(_mode: str, *_args: object) -> tuple[dict[str, object], bool]:
        return {"expected_behavior_observed": True, "bounded_collection": True,
                "cleanup_confirmed": True}, True

    monkeypatch.setattr(diagnostic, "run_mode", safe_mode)
    report: dict[str, object] = {"source_sha256": {"synthetic": "fixed"}}
    code = diagnostic.collect_matrix(cast(Any, writer), report, [], fixture, "C:\\Windows",
                                     cast(Any, _ObserverApi()))

    assert code == 2
    assert len(created) == 1
    assert deleted == created
    assert report["cleanup"] == "failed_profile_may_remain"
    assert report["cleanup_error_type"] == "OSError"


def test_assessment_does_not_claim_cpu_or_child_limit_attribution() -> None:
    spin = diagnostic.assess_mode("spin", None, "deadline", None)
    assert spin == {"expected_behavior_observed": True, "cpu_hard_cap_trigger_verified": False}

    child = diagnostic.assess_mode("child", _result(operation_error=5), None, None)
    assert child["expected_behavior_observed"] is True
    assert child["child_creation_refusal_observed"] is True
    assert child["individual_child_policy_trigger_verified"] is False
    assert child["child_failure_attribution"] == "combined_restrictions_or_other_failure_not_separated"
