"""Pure-mock registry diagnostic checks; never create native profiles/tokens."""

from __future__ import annotations

import ctypes as C
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from typing import Any, Callable, NoReturn, TextIO, cast

import pytest

from deskpal.execution.runtime import ProbeProfile
from deskpal.execution.windows.launcher import LaunchRejected, LaunchSpec

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import diagnose_worker_registry as diagnostic


PACKAGE = "S-1-15-2-1-2-3-4-5-6-7"
USER = "S-1-5-21-1-2-3-1001"


def _metadata(kind: int) -> dict[str, object]:
    return {"package_sid": PACKAGE, "user_sid": USER, "is_appcontainer": 1,
            "capability_count": 0, "integrity_sid": "S-1-16-4096", "token_type": kind,
            "impersonation_level": 2 if kind == 2 else None}


class _HelperApi:
    def __init__(self, *, revert_ok: bool = True, close_ok: bool = True) -> None:
        self.events: list[str] = []
        self.revert_ok, self.close_ok = revert_ok, close_ok
        self.registry_access: list[int] = []
        self.duplicate_access: list[int] = []

    def duplicate_token(self, token: int, access: int, security: object,
                        level: int, kind: int, output: Any) -> int:
        assert (token, security, level, kind) == (11, None, 2, 2)
        self.events.append("duplicate")
        self.duplicate_access.append(access)
        C.cast(output, C.POINTER(C.c_void_p)).contents.value = 12
        return 1

    def impersonate(self, _token: object) -> int:
        self.events.append("impersonate")
        return 1

    def current_thread(self) -> int:
        return -2

    def open_thread_token(self, thread: int, access: int, as_self: bool, output: Any) -> int:
        assert (thread, access, as_self) == (-2, 0x8, True)
        self.events.append("thread_token")
        C.cast(output, C.POINTER(C.c_void_p)).contents.value = 13
        return 1

    def get_registry(self, access: int, output: Any) -> int:
        self.events.append("registry")
        self.registry_access.append(access)
        C.cast(output, C.POINTER(C.c_void_p)).contents.value = 14
        return 0

    def revert(self) -> bool:
        self.events.append("revert")
        return self.revert_ok

    def reg_close(self, _key: object) -> int:
        self.events.append("reg_close")
        return 0 if self.close_ok else 5

    def close(self, _handle: object) -> bool:
        self.events.append("close")
        return self.close_ok


def _prepare_helper(monkeypatch: pytest.MonkeyPatch, api: _HelperApi,
                    *, mismatch: str | None = None) -> list[dict[str, object]]:
    emitted: list[dict[str, object]] = []

    def token_metadata(_api: diagnostic.RegistryApi, token: int) -> dict[str, object]:
        result = _metadata(1 if token == 11 else 2)
        if (mismatch == "primary" and token == 11) or (mismatch == "thread" and token == 13):
            result["package_sid"] = "S-1-15-2-9-9-9-9-9-9-9"
        return result

    def security(_api: diagnostic.RegistryApi, _key: Any, package: str,
                 user: str) -> dict[str, object]:
        assert (package, user) == (PACKAGE, USER)
        api.events.append("security")
        return {"owner_sid": USER, "aces": [], "dacl_sha256": "synthetic"}

    monkeypatch.setattr(diagnostic, "RegistryApi", lambda: api)
    monkeypatch.setattr(diagnostic, "_token_metadata", token_metadata)
    monkeypatch.setattr(diagnostic, "_security_metadata", security)
    monkeypatch.setattr(diagnostic, "_emit_helper", lambda report: emitted.append(dict(report)))
    return emitted


def test_helper_only_uses_minimal_registry_and_impersonation_access(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _HelperApi()
    emitted = _prepare_helper(monkeypatch, api)
    assert diagnostic._helper(11, PACKAGE, USER) == 0
    assert api.duplicate_access == [0xC]  # TOKEN_QUERY | TOKEN_IMPERSONATE.
    assert api.registry_access == [0x20000]  # READ_CONTROL only.
    assert api.events.index("thread_token") < api.events.index("registry")
    assert api.events.index("revert") < api.events.index("reg_close")
    assert emitted[0]["handle_close_errors"] == []
    assert emitted[0]["revert_confirmed"] is True
    assert emitted[0]["registry_values_accessed"] is False
    assert emitted[0]["registry_names_enumerated"] is False
    assert emitted[0]["registry_mutation_requested"] is False


@pytest.mark.parametrize("mismatch", ["primary", "thread"])
def test_mismatched_identity_never_queries_registry(monkeypatch: pytest.MonkeyPatch, mismatch: str) -> None:
    api = _HelperApi()
    emitted = _prepare_helper(monkeypatch, api, mismatch=mismatch)
    assert diagnostic._helper(11, PACKAGE, USER) == 2
    assert api.registry_access == []
    assert emitted[0]["error"] == "registry_subject_token_mismatch"
    assert ("revert" in api.events) is (mismatch == "thread")


class _HelperExit(BaseException):
    pass


def test_revert_failure_exits_without_continuing_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _HelperApi(revert_ok=False)
    emitted = _prepare_helper(monkeypatch, api)

    def exit_process(code: int) -> NoReturn:
        assert code == 77
        raise _HelperExit(code)

    monkeypatch.setattr(os, "_exit", exit_process)
    with pytest.raises(_HelperExit):
        diagnostic._helper(11, PACKAGE, USER)
    assert api.events[-1] == "revert"
    assert "reg_close" not in api.events and "close" not in api.events
    assert emitted[0]["status"] == "fatal_revert_failed_outer_host_must_reap"
    assert emitted[0]["revert_confirmed"] is False


def test_handle_close_failure_is_not_reported_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _HelperApi(close_ok=False)
    emitted = _prepare_helper(monkeypatch, api)
    assert diagnostic._helper(11, PACKAGE, USER) == 2
    assert emitted[0]["status"] == "helper_handle_close_failed"
    assert emitted[0]["handle_close_errors"] == [
        "registry_key", "thread_token", "impersonation_token", "inherited_primary_token"]


@pytest.mark.parametrize("field,value", [
    ("user_sid", "other-user"), ("package_sid", "other-package"),
    ("is_appcontainer", 0), ("capability_count", 1),
    ("integrity_sid", "S-1-16-8192"), ("token_type", 1), ("impersonation_level", 1),
])
def test_every_effective_token_identity_field_is_required(field: str, value: object) -> None:
    metadata = _metadata(2)
    metadata[field] = value
    with pytest.raises(ValueError, match="subject_token_mismatch"):
        diagnostic._verify_token(metadata, PACKAGE, USER, 2)


class _Process:
    def __init__(self, responses: list[tuple[bytes, bytes] | BaseException],
                 *, fail_kill: bool = False, fail_wait: bool = False) -> None:
        self.responses = list(responses)
        self.fail_kill, self.fail_wait = fail_kill, fail_wait
        self.returncode = 0
        self.kill_calls = self.wait_calls = 0
        self.stdin: io.BytesIO | None = None
        self.stdout, self.stderr = io.BytesIO(), io.BytesIO()

    def communicate(self, timeout: int) -> tuple[bytes, bytes]:
        assert timeout in (10, 3)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def kill(self) -> None:
        self.kill_calls += 1
        if self.fail_kill:
            raise OSError("synthetic kill failure")

    def wait(self, timeout: int) -> int:
        assert timeout == 3
        self.wait_calls += 1
        if self.fail_wait:
            raise subprocess.TimeoutExpired("synthetic-helper", timeout)
        return 77


def test_helper_timeout_kills_and_confirms_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostic, "_UNCONFIRMED_OWNERS", [])
    process = _Process([subprocess.TimeoutExpired("helper", 10), (b"", b"")])
    owner = diagnostic.HelperOwner(cast(subprocess.Popen[bytes], process))
    with pytest.raises(RuntimeError, match="timeout_confirmed_reaped"):
        owner.collect()
    assert owner.reaped is True and process.kill_calls == 1
    owner.close()
    assert process.stdout.closed and process.stderr.closed


@pytest.mark.parametrize("kill_fails", [False, True])
def test_unconfirmed_helper_is_retained_for_outer_retry(monkeypatch: pytest.MonkeyPatch, kill_fails: bool) -> None:
    unresolved: list[object] = []
    monkeypatch.setattr(diagnostic, "_UNCONFIRMED_OWNERS", unresolved)
    process = _Process([subprocess.TimeoutExpired("helper", 10),
                        subprocess.TimeoutExpired("helper", 3)],
                       fail_kill=kill_fails, fail_wait=True)
    owner = diagnostic.HelperOwner(cast(subprocess.Popen[bytes], process))
    with pytest.raises((OSError, subprocess.TimeoutExpired)):
        owner.collect()
    assert owner.reaped is False and unresolved == [owner]
    process.fail_kill = process.fail_wait = False
    owner.close()
    assert owner.reaped is True


def test_unselected_experiment_has_no_native_or_profile_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("native API touched without opt-in")

    monkeypatch.setattr(diagnostic, "RegistryApi", forbidden)
    monkeypatch.setattr(diagnostic, "create_probe_profile", forbidden)
    output = io.StringIO()
    report: dict[str, object] = {"stage0_passed": False}
    assert diagnostic._experiment(output, report, False) == 0
    assert json.loads(output.getvalue())["status"] == "not_executed_explicit_opt_in_required"


def test_outer_inherits_only_query_duplicate_token_and_explicit_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name != "nt":
        pytest.skip("Windows subprocess startup constants; no native calls are made")
    accesses: list[int] = []
    inheritance: list[tuple[int, bool]] = []
    process = _Process([(b'{"status":"synthetic"}', b"")])

    class _OuterApi:
        def open_process_token(self, _process: object, access: int, output: Any) -> int:
            accesses.append(access)
            C.cast(output, C.POINTER(C.c_void_p)).contents.value = 11 if len(accesses) == 1 else 21
            return 1

        def close(self, _token: object) -> bool:
            return True

    class _Startup:
        def __init__(self) -> None:
            self.lpAttributeList: dict[str, list[int]] = {}

    environment = {"SystemRoot": "C:/Windows", "WINDIR": "C:/Windows", "TEMP": "C:/Temp",
                   "TMP": "C:/Temp", "LOCALAPPDATA": "C:/Synthetic"}

    def spawn(arguments: list[str], **options: Any) -> _Process:
        assert arguments[1:4] == ["-I", "-S", "-B"]
        assert arguments[-4:] == ["--_registry-helper", "11", PACKAGE, USER]
        assert options["close_fds"] is True
        assert options["startupinfo"].lpAttributeList == {"handle_list": [11]}
        assert options["env"] is environment
        return process

    def inheritable(handle: int, enabled: bool) -> None:
        inheritance.append((handle, enabled))

    monkeypatch.setattr(diagnostic, "RegistryApi", _OuterApi)
    monkeypatch.setattr(diagnostic, "_token_data", lambda *_args: b"unused")
    monkeypatch.setattr(diagnostic, "_token_sid", lambda *_args: USER)
    monkeypatch.setattr(diagnostic, "_token_metadata", lambda *_args: _metadata(1))
    monkeypatch.setattr(subprocess, "STARTUPINFO", _Startup)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(os, "set_handle_inheritable", inheritable)
    owners: list[diagnostic.HelperOwner] = []
    result = diagnostic._run_helper(91, PACKAGE, environment, owners)
    assert accesses == [0xA, 0x8]
    assert inheritance == [(11, True), (11, False)]
    assert result["helper_reap_confirmed"] is True and result["outer_token_close_errors"] == []
    assert len(owners) == 1
    owners[0].close()


class _ContextApi:
    def __init__(self, windows: Path) -> None:
        self.windows = str(windows)

    def windows_directory(self, output: Any, _size: int) -> int:
        output.value = self.windows
        return len(self.windows)


class _Suspended(diagnostic.SuspendedRegistryApi):
    def __init__(self, callback: Callable[[int], None]) -> None:
        # Deliberately do not construct Api or bind any WinDLL.
        self.callback, self.process_handle = callback, 91
        self.creation_flags, self.resume_intercepted = 0x8040C, False


def _prepare_experiment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[
    io.StringIO, dict[str, object], list[ProbeProfile], list[ProbeProfile]
]:
    if os.name != "nt":
        pytest.skip("Windows-only script guard; all API operations below are mocked")
    fixture = tmp_path / "fixture.exe"
    fixture.write_bytes(b"synthetic fixture")
    created: list[ProbeProfile] = []
    deleted: list[ProbeProfile] = []

    def create(name: str) -> ProbeProfile:
        folder = tmp_path / ("profile" + str(len(created)))
        folder.mkdir()
        profile = ProbeProfile(name, PACKAGE + str(len(created)), str(folder))
        created.append(profile)
        return profile

    def launch(spec: LaunchSpec, *, _api: diagnostic.SuspendedRegistryApi) -> NoReturn:
        assert set(spec.environment) == {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA"}
        _api._read_before_resume(92)
        raise AssertionError("resume hook unexpectedly returned")

    def helper(_process: int, package: str, environment: dict[str, str],
               _owners: list[diagnostic.HelperOwner]) -> dict[str, object]:
        assert set(environment) == {"SystemRoot", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA"}
        return {"helper_exit_code": 0, "helper_report": {
            "status": "readonly_metadata_collected_binding_unverified", "revert_confirmed": True,
            "registry_security": {"owner_sid": USER, "dacl_sha256": package,
                                  "aces": [{"sid": package}], "exact_package_sid_aces": [{"sid": package}],
                                  "unparsed_ace_count": 0}}}

    monkeypatch.setattr(diagnostic, "FIXTURE", fixture)
    monkeypatch.setattr(diagnostic, "FIXTURE_SHA256", sha256(fixture.read_bytes()).hexdigest())
    monkeypatch.setattr(diagnostic, "RegistryApi", lambda: _ContextApi(tmp_path / "Windows"))
    monkeypatch.setattr(diagnostic, "_fingerprints", lambda: {"synthetic": "unchanged"})
    monkeypatch.setattr(diagnostic, "create_probe_profile", create)
    monkeypatch.setattr(diagnostic, "delete_probe_profile", deleted.append)
    monkeypatch.setattr(diagnostic, "SuspendedRegistryApi", _Suspended)
    monkeypatch.setattr(diagnostic, "_create_worker", launch)
    monkeypatch.setattr(diagnostic, "_run_helper", helper)
    monkeypatch.setattr(diagnostic, "_UNCONFIRMED_OWNERS", [])
    return io.StringIO(), {"stage0_passed": False, "binding_verified": False}, created, deleted


def test_aba_uses_fresh_profiles_and_never_declares_binding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    output, report, created, deleted = _prepare_experiment(monkeypatch, tmp_path)
    assert diagnostic._experiment(output, report, True) == 0
    saved = json.loads(output.getvalue())
    assert len(created) == 2 and deleted == list(reversed(created))
    assert [run["label"] for run in saved["runs"]] == ["A", "B", "A"]
    assert all(run["native_resume_called"] is False for run in saved["runs"])
    assert saved["comparison"]["a_dacl_hash_stable"] is True
    assert saved["comparison"]["b_dacl_differs_from_a"] is True
    assert saved["comparison"]["binding_verified"] is False
    assert saved["binding_verified"] is False and saved["stage0_passed"] is False


def test_first_post_create_save_failure_still_deletes_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    output, report, created, deleted = _prepare_experiment(monkeypatch, tmp_path)
    save = diagnostic._save
    calls = 0

    def fail_first(stream: TextIO, data: dict[str, object]) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic first save failure")
        save(stream, data)

    monkeypatch.setattr(diagnostic, "_save", fail_first)
    assert diagnostic._experiment(output, report, True) == 2
    assert len(created) == 1 and deleted == created
    assert json.loads(output.getvalue())["error"] == "synthetic first save failure"


def test_helper_exit_zero_without_complete_observation_is_blocked(monkeypatch: pytest.MonkeyPatch,
                                                                tmp_path: Path) -> None:
    output, report, created, deleted = _prepare_experiment(monkeypatch, tmp_path)
    monkeypatch.setattr(diagnostic, "_run_helper", lambda *_args: {
        "helper_exit_code": 0, "helper_report": {"status": "helper_output_budget"}})
    assert diagnostic._experiment(output, report, True) == 2
    assert len(created) == 2 and deleted == list(reversed(created))
    assert json.loads(output.getvalue())["status"] == "registry_diagnostic_blocked"


def test_fixture_copy_refuses_existing_destination(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    output, report, created, deleted = _prepare_experiment(monkeypatch, tmp_path)
    # _prepare_experiment installed a pure fake here; keep that exact callable.
    create = cast(Callable[[str], ProbeProfile], getattr(diagnostic, "create_probe_profile"))

    def create_with_existing_fixture(name: str) -> ProbeProfile:
        profile = create(name)
        (Path(profile.folder) / "native-fixture.exe").write_bytes(b"must remain unchanged")
        return profile

    monkeypatch.setattr(diagnostic, "create_probe_profile", create_with_existing_fixture)
    assert diagnostic._experiment(output, report, True) == 2
    assert len(created) == 1 and deleted == created
    assert (Path(created[0].folder) / "native-fixture.exe").read_bytes() == b"must remain unchanged"
    assert json.loads(output.getvalue())["error_type"] == "FileExistsError"


def test_profile_delete_failure_is_nonzero_and_explicit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    output, report, created, _deleted = _prepare_experiment(monkeypatch, tmp_path)

    def fail_delete(_profile: ProbeProfile) -> NoReturn:
        raise OSError("synthetic delete failure")

    monkeypatch.setattr(diagnostic, "delete_probe_profile", fail_delete)
    assert diagnostic._experiment(output, report, True) == 2
    saved = json.loads(output.getvalue())
    assert len(created) == 2
    assert len(saved["cleanup"]) == 2
    assert all(item["status"] == "failed_profile_may_remain" for item in saved["cleanup"])


class _Unconfirmed:
    def __init__(self) -> None:
        self.calls = 0

    def close(self) -> NoReturn:
        self.calls += 1
        raise OSError("synthetic unconfirmed exit")


@pytest.mark.parametrize("unknown", ["helper", "worker"])
def test_unconfirmed_exit_prevents_profile_deletion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                  unknown: str) -> None:
    output, report, created, deleted = _prepare_experiment(monkeypatch, tmp_path)
    owner = _Unconfirmed()

    if unknown == "worker":
        def fail_launch(_spec: LaunchSpec, *, _api: diagnostic.SuspendedRegistryApi) -> NoReturn:
            raise LaunchRejected("launch_failure_reap_unconfirmed", cleanup_owner=owner)
        monkeypatch.setattr(diagnostic, "_create_worker", fail_launch)
    else:
        process = _Process([], fail_kill=True, fail_wait=True)
        helper_owner = diagnostic.HelperOwner(cast(subprocess.Popen[bytes], process))

        def fail_helper(_process: int, _package: str, _environment: dict[str, str],
                        owners: list[diagnostic.HelperOwner]) -> NoReturn:
            owners.append(helper_owner)
            raise RuntimeError("synthetic helper timeout")
        monkeypatch.setattr(diagnostic, "_run_helper", fail_helper)

    assert diagnostic._experiment(output, report, True) == 2
    assert len(created) == 2 and deleted == []
    assert json.loads(output.getvalue())["cleanup"] == "withheld_unconfirmed_helper_or_worker_exit"


def test_new_report_rejects_existing_and_outside_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(diagnostic, "ROOT", tmp_path)
    valid = tmp_path / "data/validation/v06/new.json"
    assert diagnostic._new_report(valid) == valid
    valid.parent.mkdir(parents=True)
    valid.write_text("existing", encoding="ascii")
    for path in (valid, tmp_path / "outside.json", valid.parent / "bad.txt", valid.parent / "file:stream.json"):
        with pytest.raises(ValueError):
            diagnostic._new_report(path)
    assert valid.read_text(encoding="ascii") == "existing"


def test_cli_creates_new_report_and_refuses_overwrite_without_native_calls(monkeypatch: pytest.MonkeyPatch,
                                                                         tmp_path: Path) -> None:
    path = tmp_path / "data/validation/v06/new.json"

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("no native operation is allowed")

    monkeypatch.setattr(diagnostic, "ROOT", tmp_path)
    monkeypatch.setattr(diagnostic, "RegistryApi", forbidden)
    monkeypatch.setattr(diagnostic, "create_probe_profile", forbidden)
    monkeypatch.setattr(platform, "platform", lambda: "synthetic-platform")
    monkeypatch.setattr(sys, "argv", ["registry.py", "--report", str(path)])
    assert diagnostic.main() == 0
    before = path.read_bytes()
    assert json.loads(before)["experiment_requested"] is False
    with pytest.raises(ValueError, match="new_project_validation"):
        diagnostic.main()
    assert path.read_bytes() == before


def test_report_reparse_ancestor_rejected_without_creating_links(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(diagnostic, "ROOT", tmp_path)
    parent = tmp_path / "data"
    parent.mkdir()
    original = Path.lstat

    class _ReparseMetadata:
        st_file_attributes = 0x400
        st_mode = 0o040755

    def lstat(path: Path) -> Any:
        return _ReparseMetadata() if path == parent else original(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="reparse"):
        diagnostic._new_report(parent / "validation/v06/new.json")


def test_sid_cannot_escape_ace_buffer_before_conversion() -> None:
    data = C.create_string_buffer(16)
    start = C.addressof(data)
    C.c_ubyte.from_address(start + 8).value = 1
    C.c_ubyte.from_address(start + 9).value = 15
    with pytest.raises(ValueError, match="sid_length_outside_buffer"):
        diagnostic._sid(cast(diagnostic.RegistryApi, object()), start + 8, start, start + len(data))


def test_sd_query_requests_only_owner_and_dacl() -> None:
    class _SecurityFailure:
        def get_security(self, _key: object, kind: int, information: int,
                         _owner: object, group: object, _dacl: object, sacl: object,
                         _descriptor: object) -> int:
            assert (kind, information, group, sacl) == (4, 0x5, None, None)
            return 5

    with pytest.raises(OSError, match="registry_security_query"):
        diagnostic._security_metadata(cast(diagnostic.RegistryApi, _SecurityFailure()), 14, PACKAGE, USER)
