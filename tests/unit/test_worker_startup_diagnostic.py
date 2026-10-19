"""Pure-mock failure-path checks for the worker startup diagnostic."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import shutil
import sys
from typing import NoReturn

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import diagnose_worker_startup as diagnostic
from deskpal.execution.runtime import ProbeProfile
from deskpal.execution.windows import LaunchRejected, LaunchSpec


FIXTURE_BYTES = b"fixture"


class _MockLauncher:
    def __init__(self, error: LaunchRejected) -> None:
        self._error = error

    def launch(self, spec: LaunchSpec) -> NoReturn:
        del spec
        raise self._error


class _UnconfirmedOwner:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        raise OSError("synthetic owner close failure")


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    delete_error: OSError | None = None,
    launch_error: LaunchRejected | None = None,
) -> tuple[Path, Path, list[ProbeProfile], list[ProbeProfile], list[str | None]]:
    repo = tmp_path / "repo"
    source = repo / "data" / "v06-stage0" / "native-fixture-v1" / "fixture.exe"
    source.parent.mkdir(parents=True)
    source.write_bytes(FIXTURE_BYTES)
    report_path = repo / "data" / "validation" / "v06" / "report.json"
    profile_folder = tmp_path / "profile"
    created: list[ProbeProfile] = []
    deleted: list[ProbeProfile] = []
    localappdata_calls: list[str | None] = []

    def create_profile(name: str) -> ProbeProfile:
        profile_folder.mkdir()
        profile = ProbeProfile(name, "S-1-15-2-12345", str(profile_folder))
        created.append(profile)
        return profile

    def delete_profile(profile: ProbeProfile) -> None:
        deleted.append(profile)
        if delete_error is not None:
            raise delete_error
        shutil.rmtree(profile.folder)

    error = launch_error if launch_error is not None else LaunchRejected("synthetic launch rejection")
    monkeypatch.setattr(diagnostic, "ROOT", repo)
    monkeypatch.setattr(diagnostic, "FIXTURE_SHA", sha256(FIXTURE_BYTES).hexdigest())
    monkeypatch.setattr(diagnostic, "native_context", lambda: ("C:/Windows", "C:/Users/mock/AppData/Local", False))
    monkeypatch.setattr(diagnostic, "set_caller_localappdata", localappdata_calls.append)
    monkeypatch.setattr(diagnostic, "create_probe_profile", create_profile)
    monkeypatch.setattr(diagnostic, "delete_probe_profile", delete_profile)
    monkeypatch.setattr(diagnostic, "WindowsLauncher", lambda: _MockLauncher(error))
    monkeypatch.setattr(sys, "argv", ["diagnose_worker_startup.py", "--report", str(report_path)])
    return report_path, profile_folder, created, deleted, localappdata_calls


def test_first_report_write_failure_still_deletes_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report_path, profile_folder, created, deleted, _ = _prepare(monkeypatch, tmp_path)
    original_write_text = Path.write_text
    failed = False

    def fail_first_report_write(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        nonlocal failed
        if path == report_path and not failed:
            failed = True
            raise OSError("synthetic first report write failure")
        return original_write_text(path, data, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "write_text", fail_first_report_write)

    result = diagnostic.main()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result != 0
    assert failed
    assert len(created) == 1
    assert deleted == created
    assert not profile_folder.exists()
    assert report["status"] == "diagnostic_failed"
    assert report["error_type"] == "OSError"
    assert report["cleanup"] == "profile_deleted"


def test_profile_delete_failure_is_reported_and_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report_path, profile_folder, created, deleted, _ = _prepare(
        monkeypatch, tmp_path, delete_error=OSError("synthetic profile delete failure")
    )

    result = diagnostic.main()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result != 0
    assert len(created) == 1
    assert deleted == created
    assert profile_folder.exists()
    assert report["cleanup"] == "failed"
    assert report["cleanup_error"] == "synthetic profile delete failure"


def test_unconfirmed_launch_owner_withholds_profile_deletion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = _UnconfirmedOwner()
    report_path, profile_folder, created, deleted, _ = _prepare(
        monkeypatch,
        tmp_path,
        launch_error=LaunchRejected("synthetic launch failure", cleanup_owner=owner),
    )

    result = diagnostic.main()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result != 0
    assert len(created) == 1
    assert deleted == []
    assert profile_folder.exists()
    assert owner.close_calls == 1
    assert report["cleanup"] == "withheld_unconfirmed_worker_exit"
    assert report["worker_cleanup_retry"] == "UNCONFIRMED"
    assert report["worker_cleanup_error"] == "synthetic owner close failure"
