"""Pure-mock cleanup ownership checks for the loader diagnostic."""

from __future__ import annotations

import ctypes as C
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from deskpal.execution.windows._win32 import Api
from deskpal.execution.windows.launcher import LaunchRejected

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import diagnose_worker_loader as diagnostic


@dataclass
class _FakeProfile:
    folder: str
    sid: str


@dataclass
class _FakeIdentity:
    pid: int


class _FakePlainOwner:
    def __init__(self, *, fail_close: bool = False) -> None:
        self.fail_close = fail_close
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.fail_close:
            raise OSError("synthetic plain owner close failure")


class _FakeRunningWorker:
    def __init__(self, native: _FakePlainOwner, identity: _FakeIdentity) -> None:
        self.native = native
        self.identity = identity
        self.close_calls = 0

    def collect(self, _timeout_seconds: float) -> object:
        raise RuntimeError("synthetic collect failure")

    def close(self) -> None:
        self.close_calls += 1
        self.native.close()


class _FakeTrace(diagnostic.LoaderTrace):
    def __init__(self, *, fail: bool = False, events: list[str] | None = None) -> None:
        self.release_calls = 0
        self.fail = fail
        self.call_events = events if events is not None else []

    def release_debugger(self) -> None:
        self.release_calls += 1
        self.call_events.append("release")
        if self.fail:
            raise OSError("synthetic debugger release failure")


class _FakeNative:
    def __init__(self, *, failed_closes: int = 0, events: list[str] | None = None) -> None:
        self.close_calls = 0
        self.failed_closes = failed_closes
        self.events = events if events is not None else []

    def close(self) -> None:
        self.close_calls += 1
        self.events.append("close")
        if self.failed_closes:
            self.failed_closes -= 1
            raise OSError("synthetic native close failure")


def test_cleanup_owner_releases_debugger_then_closes_native() -> None:
    events: list[str] = []
    trace = _FakeTrace(events=events)
    native = _FakeNative(events=events)
    owner = diagnostic.LoaderCleanupOwner(trace, native)

    owner.close()

    assert trace.release_calls == 1
    assert native.close_calls == 1
    assert events == ["release", "close"]


def test_failed_debugger_release_preserves_owner_and_does_not_close_native() -> None:
    trace = _FakeTrace(fail=True)
    native = _FakeNative()
    owner = diagnostic.LoaderCleanupOwner(trace, native)

    with pytest.raises(LaunchRejected) as failure:
        owner.close()

    assert failure.value.cleanup_owner is owner
    assert trace.release_calls == 1
    assert native.close_calls == 0


def test_native_close_failure_can_be_retried_with_debugger_release_again() -> None:
    trace = _FakeTrace()
    native = _FakeNative(failed_closes=1)
    owner = diagnostic.LoaderCleanupOwner(trace, native)

    with pytest.raises(LaunchRejected) as failure:
        owner.close()

    assert failure.value.cleanup_owner is owner
    assert trace.release_calls == 1
    assert native.close_calls == 1

    owner.close()

    assert trace.release_calls == 2
    assert native.close_calls == 2


def test_x64_abi_assertion_uses_only_ctypes_layouts() -> None:
    diagnostic._assert_x64_abi()


@pytest.mark.parametrize(
    ("console_mode", "base_flags", "base_expected"),
    [
        ("no_window", 0x08080404, 0x08080406),
        ("detached", 0x0008040C, 0x0008040E),
    ],
)
def test_creation_flags_preserve_launcher_bits(
    console_mode: str, base_flags: int, base_expected: int,
) -> None:
    for extra in (0, 0x20):
        source = base_flags | extra
        expected = base_expected | extra
        actual = diagnostic._diagnostic_creation_flags(source, console_mode)
        assert actual == expected
        changed_console_bits = (source ^ actual) & (0x08000000 | 0x8 | 0x2)
        unchanged_bits = ~(0x08000000 | 0x8 | 0x2)
        assert changed_console_bits == ((source ^ expected) & (0x08000000 | 0x8 | 0x2))
        assert source & unchanged_bits == actual & unchanged_bits
        assert actual & (0x4 | 0x80000 | 0x400 | 0x2) == (0x4 | 0x80000 | 0x400 | 0x2)
        assert actual & 0x1 == 0


@pytest.mark.parametrize(
    ("flags", "console_mode"),
    [
        (0x08080400, "no_window"),  # no CREATE_SUSPENDED
        (0x00080408, "detached"),  # detached baseline without CREATE_SUSPENDED
        (0x00080404, "detached"),  # no console mode selected
        (0x0808040C, "no_window"),  # CREATE_NO_WINDOW and DETACHED_PROCESS together
        (0x08080414, "no_window"),  # CREATE_NEW_CONSOLE added
        (0x0008041C, "detached"),  # CREATE_NEW_CONSOLE added to detached baseline
        (0x08080404, "unknown"),
    ],
)
def test_creation_flags_reject_unsupported_inputs(flags: int, console_mode: str) -> None:
    with pytest.raises((LaunchRejected, ValueError)):
        diagnostic._diagnostic_creation_flags(flags, console_mode)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--bootstrap-key", "system_drive", "--console-mode", "no_window"],
        ["--bootstrap-key", "system_drive", "--console-mode", "detached"],
        ["--bootstrap-key", "user_profile", "--console-mode", "no_window"],
        ["--bootstrap-key", "user_profile", "--console-mode", "detached"],
        ["--bootstrap-key", "roaming_appdata", "--console-mode", "no_window"],
        ["--bootstrap-key", "roaming_appdata", "--console-mode", "detached"],
        ["--console-mode", "unknown"],
    ],
)
def test_cli_rejects_invalid_contrasts_before_native_checks(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str],
) -> None:
    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("native validation ran before CLI rejection")

    monkeypatch.setattr(diagnostic, "_assert_x64_abi", fail_if_called)
    monkeypatch.setattr(diagnostic, "native_context", fail_if_called)
    monkeypatch.setattr(sys, "argv", ["diagnose_worker_loader.py", "--report", "unused.json", *arguments])

    with pytest.raises(SystemExit) as failure:
        diagnostic.main()
    assert failure.value.code == 2


@pytest.mark.parametrize(
    ("console_mode", "environment", "message"),
    [
        ("unknown", None, "loader_unknown_console_mode"),
        ("detached", {"LOCALAPPDATA": "unused"}, "loader_console_environment_contrasts_are_exclusive"),
    ],
)
def test_debug_api_rejects_mode_before_api_initialization(
    monkeypatch: pytest.MonkeyPatch,
    console_mode: str,
    environment: dict[str, str] | None,
    message: str,
) -> None:
    def fail_if_called(_self: object) -> None:
        raise AssertionError("Win32 API initialization ran before validation")

    monkeypatch.setattr(Api, "__init__", fail_if_called)
    with pytest.raises(ValueError, match=message):
        diagnostic.DebugApi(diagnostic_environment=environment, console_mode=console_mode)


@pytest.mark.parametrize(
    ("console_mode", "expected_flags"),
    [("no_window", 0x08080406), ("detached", 0x0008040E)],
)
def test_debug_create_passes_only_transformed_flags_to_fake_api(
    console_mode: str, expected_flags: int,
) -> None:
    api = object.__new__(diagnostic.DebugApi)
    api.console_mode = console_mode
    api._environment_buffer = None
    captured: list[tuple[object, ...]] = []

    def fake_create(*arguments: object) -> int:
        captured.append(arguments)
        return 0

    api._real_create = fake_create
    arguments: list[object] = [object() for _ in range(10)]
    arguments[5] = 0x08080404

    assert diagnostic.DebugApi._debug_create(api, *arguments) == 0
    assert api.actual_creation_flags == expected_flags
    assert len(captured) == 1
    actual = captured[0]
    for index, argument in enumerate(arguments):
        if index == 5:
            assert actual[index] == expected_flags
        else:
            assert actual[index] is argument


@pytest.mark.parametrize(
    ("source_flags", "expected_flags"),
    [(0x08080404, 0x0008040C), (0x0008040C, 0x0008040C)],
)
def test_no_debug_api_transforms_flags_and_preserves_arguments(
    source_flags: int, expected_flags: int,
) -> None:
    api = object.__new__(diagnostic.NoDebugApi)
    api.actual_creation_flags = None
    api.pid = None
    captured: list[tuple[object, ...]] = []

    def fake_create(*arguments: object) -> int:
        captured.append(arguments)
        return 0

    api._real_create = fake_create
    arguments: list[object] = [object() for _ in range(10)]
    arguments[5] = source_flags

    assert diagnostic.NoDebugApi._plain_create(api, *arguments) == 0
    assert api.actual_creation_flags == expected_flags
    assert len(captured) == 1
    actual = captured[0]
    for index, argument in enumerate(arguments):
        if index == 5:
            assert actual[index] == expected_flags
        else:
            assert actual[index] is argument


@pytest.mark.parametrize("source_flags", [0x08080405, 0x08080406])
def test_no_debug_api_rejects_debug_bits_before_fake_create(source_flags: int) -> None:
    api = object.__new__(diagnostic.NoDebugApi)
    api.actual_creation_flags = None
    api.pid = None
    calls = 0

    def fake_create(*_arguments: object) -> int:
        nonlocal calls
        calls += 1
        return 0

    api._real_create = fake_create
    arguments: list[object] = [object() for _ in range(10)]
    arguments[5] = source_flags

    with pytest.raises(LaunchRejected, match="loader_no_debug_requires_plain_baseline"):
        diagnostic.NoDebugApi._plain_create(api, *arguments)
    assert calls == 0
    assert api.actual_creation_flags is None


@pytest.mark.parametrize(
    ("raw_flags", "raise_exception", "permanently_enabled", "reserved"),
    [(3, True, True, 0), (4, False, False, 4)],
)
def test_no_debug_resume_records_strict_handle_policy_before_resume(
    raw_flags: int,
    raise_exception: bool,
    permanently_enabled: bool,
    reserved: int,
) -> None:
    api = object.__new__(diagnostic.NoDebugApi)
    api._process_handle = 123
    api.strict_handle_policy = None
    events: list[str] = []
    query_arguments: list[tuple[int, int, int]] = []
    resumed: list[object] = []

    def fake_query(process: int, policy: int, output: Any, size: int) -> int:
        events.append("query")
        query_arguments.append((process, policy, size))
        C.cast(output, C.POINTER(diagnostic.DWORD)).contents.value = raw_flags
        return 1

    thread = object()

    def fake_resume(handle: object) -> int:
        events.append("resume")
        resumed.append(handle)
        return 1

    api.get_mitigation = fake_query
    api._real_resume = fake_resume

    assert diagnostic.NoDebugApi._resume_with_snapshot(api, thread) == 1
    assert query_arguments == [(123, 3, 4)]
    assert events == ["query", "resume"]
    assert resumed == [thread]
    assert api.strict_handle_policy == {
        "available": True,
        "policy": 3,
        "phase": "suspended_before_resume",
        "raw_flags": raw_flags,
        "raise_exception_on_invalid_handle_reference": raise_exception,
        "handle_exceptions_permanently_enabled": permanently_enabled,
        "reserved_flags": reserved,
    }


def test_no_debug_resume_records_query_failure_before_exactly_one_resume() -> None:
    api = object.__new__(diagnostic.NoDebugApi)
    api._process_handle = 123
    api.strict_handle_policy = None
    events: list[str] = []
    query_arguments: list[tuple[int, int, int]] = []
    resumed: list[object] = []

    def fake_query(process: int, policy: int, _output: Any, size: int) -> int:
        events.append("query")
        query_arguments.append((process, policy, size))
        C.set_last_error(87)
        return 0

    thread = object()

    def fake_resume(handle: object) -> int:
        events.append("resume")
        resumed.append(handle)
        C.set_last_error(0)
        return 1

    api.get_mitigation = fake_query
    api._real_resume = fake_resume

    assert diagnostic.NoDebugApi._resume_with_snapshot(api, thread) == 1
    assert query_arguments == [(123, 3, 4)]
    assert events == ["query", "resume"]
    assert resumed == [thread]
    assert len(resumed) == 1
    assert C.get_last_error() == 0
    policy = api.strict_handle_policy
    assert policy is not None
    assert policy == {
        "available": False,
        "policy": 3,
        "phase": "suspended_before_resume",
        "winerror": 87,
    }
    assert not ({"raw_flags", "raise_exception_on_invalid_handle_reference",
                 "handle_exceptions_permanently_enabled", "reserved_flags"}
                & policy.keys())


@pytest.mark.parametrize("process_handle", [None, 0])
def test_no_debug_resume_rejects_missing_process_handle_without_native_calls(
    process_handle: int | None,
) -> None:
    api = object.__new__(diagnostic.NoDebugApi)
    api._process_handle = process_handle
    api.strict_handle_policy = None
    calls: list[str] = []

    def fake_query(*_arguments: object) -> int:
        calls.append("query")
        return 1

    def fake_resume(_handle: object) -> int:
        calls.append("resume")
        return 1

    api.get_mitigation = fake_query
    api._real_resume = fake_resume

    with pytest.raises(LaunchRejected, match="loader_no_debug_missing_process_handle"):
        diagnostic.NoDebugApi._resume_with_snapshot(api, object())
    assert calls == []
    assert api.strict_handle_policy is None


@pytest.mark.parametrize(
    "arguments",
    [
        ["--no-debug"],
        ["--no-debug", "--console-mode", "no_window"],
        ["--no-debug", "--bootstrap-key", "system_drive"],
        ["--no-debug", "--bootstrap-key", "user_profile"],
        ["--no-debug", "--bootstrap-key", "roaming_appdata"],
        ["--no-debug", "--bootstrap-key", "system_drive", "--console-mode", "detached"],
        ["--no-debug", "--bootstrap-key", "user_profile", "--console-mode", "detached"],
        ["--no-debug", "--bootstrap-key", "roaming_appdata", "--console-mode", "detached"],
    ],
)
def test_no_debug_cli_rejects_unsafe_combinations_before_native_checks(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str],
) -> None:
    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("native validation ran before --no-debug rejection")

    monkeypatch.setattr(diagnostic, "_assert_x64_abi", fail_if_called)
    monkeypatch.setattr(diagnostic, "native_context", fail_if_called)
    monkeypatch.setattr(sys, "argv", ["diagnose_worker_loader.py", "--report", "unused.json", *arguments])

    with pytest.raises(SystemExit) as failure:
        diagnostic.main()
    assert failure.value.code == 2


def _valid_native_smoke_frame() -> dict[str, object]:
    return {
        "probe": "native",
        "excluded_handle_valid": True,
        "excluded_handle_error": 0,
        "operation_error": 0,
        "allocation_count": 1,
    }


def test_fixed_native_smoke_frames_accepts_empty_or_one_fixed_frame() -> None:
    assert diagnostic._fixed_native_smoke_frames(()) == []
    frame = _valid_native_smoke_frame()
    assert diagnostic._fixed_native_smoke_frames((frame,)) == [frame]


@pytest.mark.parametrize(
    "frames",
    [
        ({**_valid_native_smoke_frame(), "extra": "ignored"},),
        (_valid_native_smoke_frame(), _valid_native_smoke_frame()),
        ({**_valid_native_smoke_frame(), "probe": "worker supplied text"},),
        ({**_valid_native_smoke_frame(), "excluded_handle_error": True},),
        ({**_valid_native_smoke_frame(), "operation_error": -1},),
        ({**_valid_native_smoke_frame(), "allocation_count": 0x100000000},),
    ],
)
def test_fixed_native_smoke_frames_rejects_unbounded_or_invalid_values(
    frames: tuple[dict[str, object], ...],
) -> None:
    with pytest.raises(LaunchRejected, match="loader_native_smoke_frame"):
        diagnostic._fixed_native_smoke_frames(frames)


def test_fixture_source_v1_uses_fixed_project_artifact_and_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_root = Path("R:/synthetic-project")
    monkeypatch.setattr(diagnostic, "ROOT", fake_root)
    monkeypatch.setattr(diagnostic, "FIXTURE_SHA", "v1-fixed-hash")

    source, fixture_hash = diagnostic._fixture_source(1)

    assert source == fake_root / "data/v06-stage0/native-fixture-v1/fixture.exe"
    assert fixture_hash == "v1-fixed-hash"


def test_fixture_source_v2_rejects_when_hash_is_not_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostic, "FIXTURE_V2_SHA", None)

    with pytest.raises(ValueError, match="loader_fixture_v2_fingerprint_not_reviewed"):
        diagnostic._fixture_source(2)


@pytest.mark.parametrize("version", [0, 3, True, False])
def test_fixture_source_rejects_invalid_versions(version: int) -> None:
    with pytest.raises(ValueError, match="loader_unknown_fixture_version"):
        diagnostic._fixture_source(version)


def test_fixture_source_v2_uses_fixed_project_artifact_and_pinned_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root = Path("R:/synthetic-project")
    fixture_hash = "08423adb75615e3609e268409be844053bf2e14af1d4d250917006c0ba7ab4e1"
    monkeypatch.setattr(diagnostic, "ROOT", fake_root)
    monkeypatch.setattr(diagnostic, "FIXTURE_V2_SHA", fixture_hash)

    source, actual_hash = diagnostic._fixture_source(2)

    assert source == fake_root / "data/v06-stage0/native-fixture-v2/fixture.exe"
    assert actual_hash == fixture_hash


def test_fixture_version_two_cli_rejects_unpinned_hash_before_native_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("native validation ran before V2 pin rejection")

    monkeypatch.setattr(diagnostic, "FIXTURE_V2_SHA", None)
    monkeypatch.setattr(diagnostic, "_assert_x64_abi", fail_if_called)
    monkeypatch.setattr(diagnostic, "native_context", fail_if_called)
    monkeypatch.setattr(sys, "argv", [
        "diagnose_worker_loader.py", "--report", "unused.json", "--fixture-version", "2",
    ])

    with pytest.raises(ValueError, match="loader_fixture_v2_fingerprint_not_reviewed"):
        diagnostic.main()


@pytest.mark.parametrize("exception_code", [0, 0xC0000008])
def test_fixed_native_smoke_frames_accepts_v2_exception_code(exception_code: int) -> None:
    frame = {**_valid_native_smoke_frame(), "excluded_handle_exception": exception_code}

    assert diagnostic._fixed_native_smoke_frames((frame,)) == [frame]


@pytest.mark.parametrize(
    "frame",
    [
        {**_valid_native_smoke_frame(), "excluded_handle_exception": 0xC0000005},
        {**_valid_native_smoke_frame(), "excluded_handle_exception": True},
        {**_valid_native_smoke_frame(), "excluded_handle_exception": 0, "extra": "rejected"},
    ],
)
def test_fixed_native_smoke_frames_rejects_invalid_v2_exception_field(
    frame: dict[str, object],
) -> None:
    with pytest.raises(LaunchRejected, match="loader_native_smoke_frame"):
        diagnostic._fixed_native_smoke_frames((frame,))


def _patch_no_debug_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    create_worker: Any,
) -> tuple[Path, _FakeProfile, list[_FakeProfile], Any]:
    fake_root = tmp_path / "fake-repo"
    fake_source = fake_root / "data" / "v06-stage0" / "native-fixture-v1" / "fixture.exe"
    fake_source.parent.mkdir(parents=True)
    fake_fixture = b"synthetic fixture bytes for a pure mock"
    fake_source.write_bytes(fake_fixture)
    fake_profile = _FakeProfile(str(tmp_path / "fake-profile"), "S-1-15-2-1")
    Path(fake_profile.folder).mkdir()
    report_path = tmp_path / "report.json"
    deleted: list[_FakeProfile] = []
    fake_api = SimpleNamespace(actual_creation_flags=None, pid=None, strict_handle_policy=None)

    monkeypatch.setattr(diagnostic, "ROOT", fake_root)
    monkeypatch.setattr(diagnostic, "FIXTURE_SHA", sha256(fake_fixture).hexdigest())
    monkeypatch.setattr(diagnostic, "_assert_x64_abi", lambda: None)
    monkeypatch.setattr(diagnostic, "_new_report_path", lambda _path: report_path)
    monkeypatch.setattr(diagnostic, "native_context", lambda: ("C:\\Windows", "C:\\Users\\fake\\AppData\\Local", False))
    monkeypatch.setattr(diagnostic, "set_caller_localappdata", lambda _local: None)
    monkeypatch.setattr(diagnostic, "desktop_context", lambda: {"fake": True})
    monkeypatch.setattr(diagnostic, "create_probe_profile", lambda _name: fake_profile)
    monkeypatch.setattr(diagnostic, "delete_probe_profile", lambda profile: deleted.append(profile))
    monkeypatch.setattr(diagnostic, "NoDebugApi", lambda: fake_api)
    monkeypatch.setattr(diagnostic, "_create_worker", create_worker)
    monkeypatch.setattr(diagnostic, "RunningWorker", _FakeRunningWorker)
    monkeypatch.setattr(diagnostic, "source_fingerprints", lambda: {"fake.py": "same"})
    monkeypatch.setattr(
        sys, "getwindowsversion",
        lambda: SimpleNamespace(major=10, minor=0, build=1, platform=2, service_pack=""),
    )
    monkeypatch.setattr(
        sys, "argv",
        ["diagnose_worker_loader.py", "--report", str(report_path), "--no-debug", "--console-mode", "detached"],
    )
    monkeypatch.setattr(diagnostic, "_UNCONFIRMED_OWNERS", [])
    return report_path, fake_profile, deleted, fake_api


def test_no_debug_main_deletes_profile_after_pending_owner_closes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    owner = _FakePlainOwner()

    def fail_create(_spec: object, *, _api: object) -> object:
        raise LaunchRejected("synthetic_create_rejected", cleanup_owner=owner)

    report_path, profile, deleted, _api = _patch_no_debug_main(monkeypatch, tmp_path, fail_create)

    assert diagnostic.main() == 2
    assert owner.close_calls == 1
    assert deleted == [profile]
    assert json.loads(report_path.read_text(encoding="ascii"))["cleanup"] == "profile_deleted"


def test_no_debug_main_retains_pending_owner_and_withholds_profile_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    owner = _FakePlainOwner(fail_close=True)

    def fail_create(_spec: object, *, _api: object) -> object:
        raise LaunchRejected("synthetic_create_rejected", cleanup_owner=owner)

    report_path, _profile, deleted, _api = _patch_no_debug_main(monkeypatch, tmp_path, fail_create)

    assert diagnostic.main() == 2
    assert owner.close_calls == 1
    assert diagnostic._UNCONFIRMED_OWNERS == [owner]
    assert deleted == []
    assert json.loads(report_path.read_text(encoding="ascii"))["cleanup"] == "withheld_unconfirmed_worker_or_debugger"


def test_no_debug_main_closes_registered_worker_after_collect_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    native = _FakePlainOwner()
    identity = _FakeIdentity(pid=321)

    def return_worker(_spec: object, *, _api: object) -> tuple[_FakePlainOwner, _FakeIdentity]:
        return native, identity

    report_path, profile, deleted, _api = _patch_no_debug_main(monkeypatch, tmp_path, return_worker)
    fake_worker: list[_FakeRunningWorker] = []

    def make_worker(worker_native: _FakePlainOwner, worker_identity: _FakeIdentity) -> _FakeRunningWorker:
        worker = _FakeRunningWorker(worker_native, worker_identity)
        fake_worker.append(worker)
        return worker

    monkeypatch.setattr(diagnostic, "RunningWorker", make_worker)

    assert diagnostic.main() == 2
    assert len(fake_worker) == 1
    assert fake_worker[0].close_calls == 1
    assert native.close_calls == 1
    assert deleted == [profile]
    report = json.loads(report_path.read_text(encoding="ascii"))
    assert report["error_type"] == "RuntimeError"
    assert report["cleanup"] == "profile_deleted"
