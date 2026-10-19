"""Read-only handle diagnostics with native/profile/write tripwires throughout."""

from __future__ import annotations

import ast
import copy
import ctypes as C
from ctypes import wintypes as W
from hashlib import sha256
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest

from deskpal.execution.runtime import ProbeProfile
from deskpal.execution.windows.launcher import LaunchRejected, LaunchSpec

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import diagnose_registry_handle_access as diagnostic
import diagnose_worker_registry as readonly
import diagnose_worker_registry_acl as acl

PACKAGE = "S-1-15-2-1-2-3-4-5-6-7"
USER = "S-1-5-21-1-2-3-1001"
OTHER_USER = "S-1-5-21-1-2-3-1002"
SOURCES = {"synthetic-fixed-input": "unchanged"}


def _forbidden(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("unmocked native/profile/mutation operation forbidden")


@pytest.fixture(autouse=True)
def _no_native(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(C, "WinDLL", _forbidden, raising=False)
    monkeypatch.setattr(diagnostic, "AccessApi", _forbidden)
    monkeypatch.setattr(diagnostic, "_create_worker", _forbidden)
    monkeypatch.setattr(diagnostic, "delete_probe_profile", _forbidden)
    for name in ("WriteApi", "create_probe_profile", "delete_probe_profile", "stage_runtime",
                 "apply_readonly_dacl", "_seed", "_apply", "_restore_for_cleanup", "_bind_from_worker"):
        monkeypatch.setattr(acl, name, _forbidden)
    monkeypatch.setattr(readonly, "SuspendedRegistryApi", _forbidden)
    monkeypatch.setattr(acl, "_SESSIONS", set())
    monkeypatch.setattr(acl, "_BINDINGS", set())
    monkeypatch.setattr(acl, "_UNCONFIRMED", [])


class _Api:
    def __init__(self) -> None:
        self.status = 0
        self.length = 56
        self.grant = 0x60000
        self.attributes = 2
        self.query_calls = 0
        self.closed: list[int] = []
        self.keys_closed: list[int] = []
        self.fail_close: set[int] = set()
        self.process_ok, self.process_handle = True, 11
        self.thread_ok, self.thread_handle = False, 0
        self.last_error = 1008
        self.users = {11: USER, 12: USER}
        self.types = {11: 1, 12: 2}
        self.appcontainer = {11: 0, 12: 0}
        self.impersonation = 2
        self.query_raises = False
        self.token_failure: tuple[int, int] | None = None

    def current_process(self) -> int:
        return -1

    def current_thread(self) -> int:
        return -2

    def open_process_token(self, process: int, access: int, output: Any) -> bool:
        assert (process, access) == (-1, 8)
        C.cast(output, C.POINTER(W.HANDLE)).contents.value = self.process_handle
        return self.process_ok

    def open_thread_token(self, thread: int, access: int, as_self: bool, output: Any) -> bool:
        assert (thread, access, as_self) == (-2, 8, True)
        C.cast(output, C.POINTER(W.HANDLE)).contents.value = self.thread_handle
        return self.thread_ok

    def query_object(self, handle: Any, information_class: int, output: Any, size: int, returned: Any) -> int:
        assert handle.value == 50 and information_class == 0 and size == 56
        self.query_calls += 1
        # Write the documented first two fields; poison the remaining output.
        C.memset(output, 0xA5, size)
        C.cast(output, C.POINTER(C.c_uint32))[0] = self.attributes
        C.cast(output, C.POINTER(C.c_uint32))[1] = self.grant
        C.cast(returned, C.POINTER(C.c_uint32)).contents.value = self.length
        if self.query_raises:
            raise OSError("synthetic NtQueryObject wrapper failure")
        return self.status

    def close(self, handle: Any) -> bool:
        self.closed.append(int(handle.value))
        return handle.value not in self.fail_close

    def reg_close(self, handle: Any) -> int:
        self.keys_closed.append(int(handle.value))
        return 5 if handle.value in self.fail_close else 0

    def windows_directory(self, buffer: Any, _length: int) -> int:
        buffer.value = "C:/Windows"
        return len(buffer.value)

    create_key = set_value = set_security = impersonate = revert = get_token = _forbidden


class _Process:
    def __init__(self) -> None:
        self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()


def _baseline() -> dict[str, object]:
    return {"metadata": {"owner_sid": USER, "dacl_null": False, "dacl_protected": False,
                        "unparsed_ace_count": 0}, "acl_revision": 2,
            "raw_aces_hex": [acl._ace(0, 0x23, 0xF003F, PACKAGE).hex()]}


def _session(monkeypatch: pytest.MonkeyPatch, api: _Api, folder: Path | None = None) -> acl.Session:
    profile = ProbeProfile("deskpal.v06." + "a" * 32, PACKAGE, str(folder or Path("C:/synthetic/new-profile")))

    def create(name: str) -> ProbeProfile:
        assert name.startswith("deskpal.v06.") and len(name.removeprefix("deskpal.v06.")) == 32
        int(name.removeprefix("deskpal.v06."), 16)
        return profile

    monkeypatch.setattr(acl, "create_probe_profile", create)
    return acl.Session(cast(acl.WriteApi, api))


def _binding(monkeypatch: pytest.MonkeyPatch, api: _Api) -> acl.BoundRoot:
    session = _session(monkeypatch, api)
    session.create()
    binding = acl.BoundRoot(session)
    binding.owner.handle.value, binding.user, binding.before, binding.validated = 50, USER, _baseline(), True
    return binding


def _host_tokens(monkeypatch: pytest.MonkeyPatch, api: _Api) -> None:
    monkeypatch.setattr(C, "get_last_error", lambda: api.last_error, raising=False)

    def token_data(_api: object, token: int, kind: int) -> Any:
        assert _api is api and token in (11, 12) and kind in (1, 8, 29, 9)
        if api.token_failure == (token, kind):
            raise OSError("synthetic host token query failure")
        if kind == 1:
            return C.create_string_buffer(api.users[token].encode("ascii"))
        value = api.types[token] if kind == 8 else api.appcontainer[token] if kind == 29 else api.impersonation
        return C.create_string_buffer(int(value).to_bytes(4, "little"))

    monkeypatch.setattr(readonly, "_token_data", token_data)
    monkeypatch.setattr(readonly, "_token_sid", lambda _api, data: bytes(data).rstrip(b"\0").decode("ascii"))


def test_public_layout_and_dynamic_binding_use_documented_fixed_types(monkeypatch: pytest.MonkeyPatch) -> None:
    structure = diagnostic.PublicObjectBasicInformation
    assert C.sizeof(structure) == 56
    assert (structure.Attributes.offset, structure.GrantedAccess.offset, structure.Reserved.offset) == (0, 4, 16)
    captured: list[tuple[object, ...]] = []
    api_class = diagnostic.AccessApi
    # The autouse tripwire replaces AccessApi, so get its declaration without
    # invoking any inherited/native constructor via the original class object.
    assert api_class is _forbidden
    source = Path(diagnostic.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    access = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AccessApi")
    namespace: dict[str, Any] = {"acl": SimpleNamespace(WriteApi=object), "C": C, "W": W,
                                "readonly": SimpleNamespace(_bind=lambda *args: captured.append(args))}
    monkeypatch.setattr(C, "WinDLL", lambda name, **kwargs: (name, kwargs), raising=False)
    exec(compile(ast.Module(body=[access], type_ignores=[]), "<mock AccessApi binding>", "exec"), namespace)
    namespace["AccessApi"]()
    assert len(captured) == 1
    dll, name, arguments, result = captured[0]
    assert dll == ("ntdll", {"use_last_error": True}) and name == "NtQueryObject"
    assert arguments == [W.HANDLE, C.c_int, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32)]
    assert result is C.c_int32


@pytest.mark.parametrize("grant", [0x60000, 0xF003F, 0xFFFFFFFF, 0, 0x20000, 0x40000])
def test_query_reports_actual_grants_and_never_reads_reserved_output(
    monkeypatch: pytest.MonkeyPatch, grant: int,
) -> None:
    api = _Api()
    api.grant = grant
    binding = _binding(monkeypatch, api)

    class Guarded(diagnostic.PublicObjectBasicInformation):
        @property
        def Reserved(self) -> NoReturn:
            raise AssertionError("Reserved must never be read")

        @property
        def HandleCount(self) -> NoReturn:
            raise AssertionError("HandleCount is outside this observation")

        @property
        def PointerCount(self) -> NoReturn:
            raise AssertionError("PointerCount is outside this observation")

    monkeypatch.setattr(diagnostic, "PublicObjectBasicInformation", Guarded)
    report: dict[str, object] = {}
    present = grant & 0x60000 == 0x60000
    if present:
        diagnostic._granted_access(binding, report)
    else:
        with pytest.raises(ValueError, match="required_bits_missing"):
            diagnostic._granted_access(binding, report)
    saved = cast(dict[str, object], report["granted_access"])
    assert api.query_calls == 1 and saved["raw_ntstatus"] == 0 and saved["returned_length"] == 56
    assert saved["attributes"] == 2 and saved["granted_access"] == grant
    assert saved["read_control"] is bool(grant & 0x20000) and saved["write_dac"] is bool(grant & 0x40000)
    assert saved["status"] == "observed" and saved["required_access_gate_passed"] is present
    assert not any(key.lower() in ("reserved", "handlecount", "pointercount") for key in saved)
    assert binding.session.close_resources() is present


@pytest.mark.parametrize("status,length", [(1, 56), (-1073741816, 56), (0x40000000, 56),
    (0, 0), (0, 8), (0, 55), (0, 57), (0, 4096)])
def test_query_unknown_status_and_length_preserve_raw_facts_without_reading_fields(
    monkeypatch: pytest.MonkeyPatch, status: int, length: int,
) -> None:
    api = _Api()
    api.status, api.length = status, length
    binding = _binding(monkeypatch, api)
    report: dict[str, object] = {}
    with pytest.raises(ValueError, match="status_or_length"):
        diagnostic._granted_access(binding, report)
    saved = cast(dict[str, object], report["granted_access"])
    assert saved["raw_ntstatus"] == status and saved["returned_length"] == length
    assert saved["granted_access"] is None and saved["attributes"] is None and saved["status"] == "unknown"
    assert api.query_calls == 1 and not binding.session.close_resources()


@pytest.mark.parametrize("invalid", [None, C.c_void_p(-1).value])
def test_query_rejects_invalid_registered_handle_before_native(monkeypatch: pytest.MonkeyPatch, invalid: int | None) -> None:
    api = _Api()
    binding = _binding(monkeypatch, api)
    binding.owner.handle.value = invalid
    with pytest.raises(ValueError):
        diagnostic._granted_access(binding, {})
    assert api.query_calls == 0 and binding.session.unknown
    binding.owner.handle.value = None  # Synthetic invalid handle must not reach close.
    assert not binding.session.close_resources()


@pytest.mark.parametrize("failure", ["before_change", "after_change", "query", "query_and_after_error"])
def test_sd_bracketing_retains_query_failure_and_independent_after_failure(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    api = _Api()
    binding = _binding(monkeypatch, api)
    api.status = -1073741816 if failure.startswith("query") else 0
    calls = 0

    def snapshot(*_args: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2 and failure == "query_and_after_error":
            raise OSError("independent post-query snapshot failure")
        data = copy.deepcopy(binding.before)
        if (calls == 1 and failure == "before_change") or (calls == 2 and failure == "after_change"):
            data["acl_revision"] = 4
        return data

    monkeypatch.setattr(acl, "_snapshot", snapshot)
    report: dict[str, object] = {}
    with pytest.raises((ValueError, OSError)):
        diagnostic._observe_bound_root(binding, report)
    saved = cast(dict[str, Any], report["security_descriptors"])
    assert saved["all_match"] is (failure == "query")
    assert api.query_calls == (0 if failure == "before_change" else 1)
    if failure.startswith("query"):
        query = cast(dict[str, object], report["granted_access"])
        assert query["raw_ntstatus"] == -1073741816 and query["returned_length"] == 56
        assert query["error"] == "handle_access_ntqueryobject_status_or_length"
    if failure == "query_and_after_error":
        assert saved["after_error"] == "independent post-query snapshot failure"
    assert not binding.session.close_resources()


@pytest.mark.parametrize("thread", [False, True])
def test_host_context_observes_own_effective_token_without_package_query_or_revert(
    monkeypatch: pytest.MonkeyPatch, thread: bool,
) -> None:
    api = _Api()
    api.thread_ok, api.thread_handle = thread, 12 if thread else 0
    _host_tokens(monkeypatch, api)
    session = _session(monkeypatch, api)
    session.create()
    report: dict[str, object] = {}
    assert diagnostic._host_context(session, report) == USER
    saved = cast(dict[str, Any], report["host_context"])
    assert saved["status"] == "observed" and saved["effective_source"] == ("thread" if thread else "process")
    assert saved["process"]["is_appcontainer"] == 0 and saved["process"]["token_type"] == 1
    assert saved["thread"]["status"] == ("observed" if thread else "no_thread_token")
    if thread:
        assert saved["thread"]["impersonation_level"] == 2 and saved["thread"]["token_type"] == 2
    assert session.close_resources() and api.closed == ([12, 11] if thread else [11])
    state = session.cleanup_state()
    assert all(record["closed"] is True for record in cast(list[dict[str, object]], state["resources"]))


@pytest.mark.parametrize("failure", ["process_partial", "process_null", "thread_error", "thread_partial_no_token",
    "thread_null", "token_query", "thread_type", "thread_level", "close"])
def test_host_context_unknown_and_partial_handles_remain_owned_and_status_closed(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    api = _Api()
    if failure == "process_partial":
        api.process_ok, api.last_error = False, 5
    elif failure == "process_null":
        api.process_handle = 0
    elif failure == "thread_error":
        api.last_error = 5
    elif failure == "thread_partial_no_token":
        api.thread_handle = 12
    elif failure == "thread_null":
        api.thread_ok = True
    elif failure == "token_query":
        api.token_failure = (11, 29)
    elif failure in ("thread_type", "thread_level"):
        api.thread_ok, api.thread_handle = True, 12
        api.types[12], api.impersonation = (1, 2) if failure == "thread_type" else (2, 4)
    else:
        api.fail_close.add(11)
    _host_tokens(monkeypatch, api)
    session = _session(monkeypatch, api)
    session.create()
    report: dict[str, object] = {}
    if failure == "close":
        diagnostic._host_context(session, report)
    else:
        with pytest.raises((OSError, ValueError)):
            diagnostic._host_context(session, report)
    assert not session.close_resources()
    assert api.closed == ([12, 11] if failure in ("thread_partial_no_token", "thread_type", "thread_level") else [] if failure == "process_null" else [11])
    assert session.unknown
    assert session.resource_close_unconfirmed is (failure == "close")


@pytest.mark.parametrize("failure", ["none", "thread_present", "wrong_effective_user", "missing_access",
    "ntstatus", "query_wrapper", "before_sd", "after_sd", "bad_stop", "pending_owner", "no_callback",
    "unreaped_helper", "binding", "token_close", "key_close", "delete", "sources", "copied_fixture", "temp_reparse"])
def test_fresh_observation_success_cleanup_and_unknown_preservation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str,
) -> None:
    api = _Api()
    _host_tokens(monkeypatch, api)
    if failure in ("thread_present", "wrong_effective_user"):
        api.thread_ok, api.thread_handle = True, 12
        if failure == "wrong_effective_user":
            api.users[12] = OTHER_USER
    if failure == "missing_access":
        api.grant = 0x20000
    if failure == "ntstatus":
        api.status = -1073741816
    api.query_raises = failure == "query_wrapper"
    if failure in ("token_close", "key_close"):
        api.fail_close.add(11 if failure == "token_close" else 50)
    folder = tmp_path / "synthetic-new-profile"
    folder.mkdir()
    session = _session(monkeypatch, api, folder)
    monkeypatch.setattr(diagnostic, "AccessApi", lambda: api)

    def selected_session(selected: object) -> acl.Session:
        assert selected is api
        return session

    monkeypatch.setattr(acl, "Session", selected_session)
    monkeypatch.setattr(diagnostic, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(diagnostic, "_fingerprints", lambda: {"changed": "input"} if failure == "sources" else dict(SOURCES))
    fixture = tmp_path / "pinned-fixture.exe"
    fixture.write_bytes(b"synthetic fixture; never executed")
    monkeypatch.setattr(readonly, "FIXTURE", fixture)
    monkeypatch.setattr(readonly, "FIXTURE_SHA256", sha256(fixture.read_bytes()).hexdigest())

    def reject(path: Path) -> None:
        if failure == "temp_reparse" and path.name == "Temp" and path.exists():
            raise ValueError("synthetic reparse discovered after mkdir")

    monkeypatch.setattr(readonly, "_reject_reparse", reject)
    helper = acl.BindingHelperOwner(cast(subprocess.Popen[bytes], _Process()), cast(acl.WriteApi, api))
    helper.reaped = failure != "unreaped_helper"
    if failure == "unreaped_helper":
        monkeypatch.setattr(helper, "close", lambda: None)  # Reap remains unconfirmed.
    baseline = _baseline()

    def bind(selected: acl.Session, process: int, environment: dict[str, str]) -> dict[str, object]:
        assert selected is session and process == 91
        assert {key.upper() for key in environment} == {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA"}
        if failure == "binding":
            raise ValueError("synthetic binding rejection")
        root = acl.BoundRoot(session)
        root.owner.handle.value, root.user, root.before, root.validated = 50, USER, copy.deepcopy(baseline), True
        session.helpers.append(helper)
        return {"synthetic_verified_binding": True}

    monkeypatch.setattr(acl, "_bind_from_worker", bind)
    monkeypatch.setattr(readonly, "SuspendedRegistryApi", lambda callback: SimpleNamespace(
        callback=callback, resume_intercepted=False, creation_flags=None))
    launches: list[LaunchSpec] = []
    pending = SimpleNamespace(close=lambda: None)

    def create(spec: LaunchSpec, *, _api: Any) -> NoReturn:
        launches.append(spec)
        assert spec.executable == folder / "native-fixture.exe" and spec.args == () and spec.appcontainer_sid == PACKAGE
        assert spec.executable.read_bytes() == fixture.read_bytes()
        _api.creation_flags = 0x80404
        if failure != "no_callback":
            _api.resume_intercepted = True
            _api.callback(91)
        raise LaunchRejected("synthetic_bad_stage" if failure == "bad_stop" else readonly.STOP_STAGE,
                             cleanup_owner=pending if failure == "pending_owner" else None)

    monkeypatch.setattr(diagnostic, "_create_worker", create)
    if failure == "copied_fixture":
        real_open = Path.open

        class CorruptWriter:
            def __init__(self, stream: Any) -> None:
                self.stream = stream

            def __enter__(self) -> CorruptWriter:
                return self

            def __exit__(self, *_args: object) -> None:
                self.stream.close()

            def write(self, payload: bytes) -> None:
                self.stream.write(payload + b"corruption")

        def corrupt_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            stream = real_open(path, mode, *args, **kwargs)
            return CorruptWriter(stream) if path == folder / "native-fixture.exe" and mode == "xb" else stream

        monkeypatch.setattr(Path, "open", corrupt_open)
    snapshots = 0

    def snapshot(selected: object, handle: Any, package: str, user: str) -> dict[str, object]:
        nonlocal snapshots
        assert selected is api and handle.value == 50 and (package, user) == (PACKAGE, USER)
        snapshots += 1
        data = copy.deepcopy(baseline)
        if (failure == "before_sd" and snapshots == 1) or (failure == "after_sd" and snapshots == 2):
            data["acl_revision"] = 4
        return data

    monkeypatch.setattr(acl, "_snapshot", snapshot)
    deleted: list[ProbeProfile] = []

    def delete(profile: ProbeProfile) -> None:
        assert profile is session.profile and session.closed and not session.unknown
        assert api.keys_closed == [50] and api.closed == ([12, 11] if api.thread_ok else [11])
        deleted.append(profile)
        if failure == "delete":
            raise OSError("synthetic profile deletion failure")

    monkeypatch.setattr(diagnostic, "delete_probe_profile", delete)
    report: dict[str, object] = {"stage0_passed": False, "source_sha256": dict(SOURCES)}
    output = io.StringIO()
    success = failure in ("none", "thread_present")
    assert diagnostic._experiment(output, report) == (0 if success else 2)
    saved = json.loads(output.getvalue())
    assert saved == report and saved["stage0_passed"] is False
    assert len(deleted) == (1 if success or failure == "delete" else 0)
    assert len(launches) == (0 if failure in ("copied_fixture", "temp_reparse") else 1)
    if success:
        assert saved["status"] == "read_only_handle_access_observed" and saved["cleanup"] == "profile_deleted"
        assert saved["granted_access"]["granted_access"] == 0x60000
        assert saved["security_descriptors"]["all_match"] is True
        assert saved["native_resume_called"] is False and saved["worker_code_executed"] is False
        assert saved["suspended_worker"]["creation_flags"] == 0x80404
        assert saved["suspended_worker"]["reap_confirmed_by_adapter_contract"] is True
    elif failure == "delete":
        assert saved["cleanup"] == "failed_profile_may_remain" and folder.exists()
    else:
        assert saved["cleanup"] == "withheld_unknown_helper_worker_key_or_binding" and folder.exists()
        assert saved["cleanup_state"]["unknown"] is True
    assert saved["cleanup_state"]["individual_adapter_handle_closes_verified"] is False
    assert saved["cleanup_state"]["popen_own_process_handle_verified"] is False
    if failure in ("wrong_effective_user", "before_sd", "bad_stop", "pending_owner", "no_callback",
                   "unreaped_helper", "binding", "copied_fixture", "temp_reparse"):
        assert api.query_calls == 0
    else:
        assert api.query_calls == 1
    if failure == "missing_access":
        assert saved["granted_access"]["granted_access"] == 0x20000 and snapshots == 2
    if failure == "query_wrapper":
        assert saved["granted_access"]["raw_ntstatus"] is None
        assert saved["granted_access"]["returned_length"] == 56 and snapshots == 2
    if failure == "wrong_effective_user":
        assert saved["host_context"]["effective_source"] == "thread"
        assert saved["host_context"]["matches_binding_user"] is False


def test_without_optin_only_exclusively_writes_unexecuted_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report = tmp_path / "new.json"
    monkeypatch.setattr(readonly, "_new_report", lambda _path: report)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", str(report)])
    assert diagnostic.main() == 0
    saved = json.loads(report.read_text(encoding="ascii"))
    assert saved["status"] == "not_executed_explicit_opt_in_required" and saved["stage0_passed"] is False
    assert saved["sd_mutation_requested"] is False and saved["native_resume_called"] is False
    with pytest.raises(FileExistsError):
        diagnostic.main()


@pytest.mark.parametrize("argument", ["--sid", "--hkey", "--pid", "--profile", "--registry-path", "--command", "--python"])
def test_cli_cannot_target_old_profile_handle_or_arbitrary_command(
    monkeypatch: pytest.MonkeyPatch, argument: str,
) -> None:
    monkeypatch.setattr(readonly, "_new_report", _forbidden)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", "new.json", "--experiment", argument, "arbitrary"])
    with pytest.raises(SystemExit) as error:
        diagnostic.main()
    assert error.value.code == 2


def test_cli_optin_records_full_source_inputs_and_end_hash_even_on_constructor_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    report = tmp_path / "new.json"
    monkeypatch.setattr(readonly, "_new_report", lambda _path: report)
    monkeypatch.setattr(diagnostic, "_fingerprints", lambda: dict(SOURCES))
    monkeypatch.setattr(diagnostic, "os", SimpleNamespace(name="nt"))

    def unavailable() -> NoReturn:
        raise OSError("synthetic unavailable NtQueryObject")

    monkeypatch.setattr(diagnostic, "AccessApi", unavailable)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", str(report), "--experiment"])
    assert diagnostic.main() == 2
    saved = json.loads(report.read_text(encoding="ascii"))
    assert saved["source_sha256"] == SOURCES and saved["source_sha256_end"] == SOURCES
    assert saved["source_unchanged"] is True and saved["cleanup"] == "not_required"
    assert saved["status"] == "handle_access_observation_blocked" and saved["stage0_passed"] is False


def test_fingerprint_scope_contains_all_fixed_sources_and_pinned_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    inherited = {path: "old" for path in ("scripts/diagnose_worker_registry.py", "src/deskpal/execution/runtime.py",
        "src/deskpal/execution/windows/_win32.py", "src/deskpal/execution/windows/launcher.py",
        "src/deskpal/execution/protocol.py", "tests/fixtures/windows/native_probe_v2.c",
        "data/v06-stage0/native-fixture-v2/fixture.exe")}
    monkeypatch.setattr(readonly, "_fingerprints", lambda: dict(inherited))
    inputs = diagnostic._fingerprints()
    assert inputs.keys() == inherited.keys() | {"scripts/diagnose_registry_handle_access.py", "scripts/diagnose_worker_registry_acl.py"}
    assert all(inputs[path] == "old" for path in inherited)
