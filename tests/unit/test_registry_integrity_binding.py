"""Mock-only checks for the independent read-only MIC binding helper."""
from __future__ import annotations

import ast
import ctypes as C
from ctypes import wintypes as W
import io
import os
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Literal, NoReturn

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import diagnose_registry_integrity_binding as binding

PACKAGE = "S-1-15-2-1-2-3-4-5-6-7"
USER = "S-1-5-21-1-2-3-1001"
NONCE = "a" * 32
PROFILE = "deskpal.v06." + NONCE
SEED = "seed_" + NONCE


def _forbidden(*args: Any, **kwargs: Any) -> NoReturn:
    raise AssertionError("unmocked native/process operation")


@pytest.fixture(autouse=True)
def _no_native(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(C, "WinDLL", _forbidden, raising=False)
    monkeypatch.setattr(binding, "ReadApi", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(os, "set_handle_inheritable", _forbidden)
    monkeypatch.setattr(os, "_exit", _forbidden)


def _sid(value: str) -> bytes:
    numbers = [int(item) for item in value.split("-")[2:]]
    return bytes((1, len(numbers) - 1)) + numbers[0].to_bytes(6, "big") + struct.pack("<" + "I" * (len(numbers) - 1), *numbers[1:])


def _ace(kind: int, sid: str, mask: int = 1, flags: int = 0) -> bytes:
    subject = _sid(sid)
    return struct.pack("<BBHI", kind, flags, 8 + len(subject), mask) + subject


def _acl(entries: list[bytes]) -> bytes:
    return struct.pack("<BBHHH", 2, 0, 8 + sum(map(len, entries)), len(entries), 0) + b"".join(entries)


def _sd(*, entries: list[bytes] | None = None, label: list[bytes] | None = None, owner: str = USER) -> bytes:
    owner_bytes = _sid(owner)
    dacl = _acl(entries if entries is not None else [_ace(0, PACKAGE, 0xF003F, 3), _ace(0, USER, 0xF003F, 3)])
    sacl = _acl(label if label is not None else [_ace(17, "S-1-16-4096")])
    return struct.pack("<BBHIIII", 1, 0, 0x8014, 20, 0, 20 + len(owner_bytes) + len(dacl), 20 + len(owner_bytes)) + owner_bytes + dacl + sacl


class Api:
    def __init__(self) -> None:
        self.closed: list[int] = []
        self.keys: list[int] = []
        self.close_ok = True
        self.error = 0
        self.raw = _sd()
        self.status = 0
        self.security_calls = 0

    def last_error(self) -> int:
        return self.error

    def close(self, handle: Any) -> bool:
        self.closed.append(int(handle.value))
        return self.close_ok

    def reg_close(self, handle: Any) -> int:
        self.keys.append(int(handle.value))
        return 0 if self.close_ok else 5

    def get_security(self, key: Any, information: int, data: Any, size: Any) -> int:
        assert information == 0x15
        self.security_calls += 1
        C.cast(size, C.POINTER(W.DWORD)).contents.value = len(self.raw)
        if not self.status:
            C.memmove(data, self.raw, len(self.raw))
        return self.status


def test_sd_complete_raw_and_canonical_fields() -> None:
    raw = _sd()
    result = binding.parse_sd(raw, PACKAGE, USER)
    assert result["raw_sd_hex"] == raw.hex()
    assert result["sd_bytes"] == len(raw)
    assert result["owner_sid"] == USER
    assert result["label"] == {"type": 17, "flags": 0, "mask": 1, "sid": "S-1-16-4096"}
    assert result["dacl"][0]["sid"] == PACKAGE


def test_known_critical_ace_flag_preserved() -> None:
    raw = _sd(entries=[_ace(0, PACKAGE, 0xF003F, 0x23)])
    parsed = binding.parse_sd(raw, PACKAGE, USER)
    assert parsed["dacl"][0]["flags"] == 0x23
    assert parsed["raw_sd_hex"] == raw.hex()


@pytest.mark.parametrize("raw", [
    _sd(entries=[_ace(2, PACKAGE)]),
    _sd(entries=[_ace(0, PACKAGE), _ace(0, "S-1-1-0")]),
    _sd(entries=[_ace(0, PACKAGE), _ace(1, PACKAGE)]),
    _sd(entries=[]),
    _sd(entries=[_ace(0, PACKAGE)] * 17),
    _sd(label=[]),
    _sd(label=[_ace(17, "S-1-16-4096")] * 2),
    _sd(label=[_ace(17, "S-1-16-0")]),
    _sd(label=[_ace(17, "S-1-16-4096", 0)]),
    _sd(owner="S-1-5-18"),
    b"x" * (binding.MAX_SD + 1),
    b"x" * 19,
])
def test_sd_unknown_or_over_budget_fails_closed(raw: bytes) -> None:
    with pytest.raises(ValueError):
        binding.parse_sd(raw, PACKAGE, USER)


def test_snapshot_one_fixed_call_and_required_size_preserved() -> None:
    api = Api()
    log: list[dict[str, Any]] = []
    api.status = 122
    api.raw = b"x" * (binding.MAX_SD + 1)
    with pytest.raises(ValueError, match="get_security_failed"):
        binding._snapshot(api, log, 25, PACKAGE, USER)
    assert api.security_calls == 1
    assert log == [{"call": "get_security", "state": "returned", "result": 122, "last_error": 0,
                    "required_or_returned_bytes": binding.MAX_SD + 1}]


@pytest.mark.parametrize("key", [False, True])
def test_failed_close_sticky_no_retry(key: bool) -> None:
    api = Api()
    api.close_ok = False
    owner = binding.Handle(api, [], key=key)
    owner.cell.value = 55
    with pytest.raises(ValueError):
        owner.close()
    with pytest.raises(ValueError, match="sticky_close_failure"):
        owner.close()
    assert api.keys + api.closed == [55]


@pytest.mark.parametrize("value", [0xFFFFFFFF, 0xFFFFFFFE, 0x80000000, 0xFFFFFFFFFFFFFFFF, 0xFFFFFFFF80000001])
def test_pseudo_output_not_closed(value: int) -> None:
    api = Api()
    owner = binding.Handle(api, [])
    owner.cell.value = value
    with pytest.raises(ValueError):
        owner.close()
    assert api.closed == []


def test_cleanup_budget_reserved_after_operation_budget() -> None:
    api = Api()
    log: list[dict[str, Any]] = [{"call": "get_token"}] * binding.MAX_CALLS
    owner = binding.Handle(api, log)
    owner.cell.value = 55
    owner.close()
    assert api.closed == [55]


def test_partial_output_owned_even_when_native_bool_fails() -> None:
    api = Api()
    owner = binding.Handle(api, [])

    def fail(output: Any) -> bool:
        C.cast(output, C.POINTER(W.HANDLE)).contents.value = 66
        return False

    dynamic: Any = api
    dynamic.partial = fail
    with pytest.raises(ValueError, match="partial_failed"):
        binding._bool(api, owner.log, "partial", C.byref(owner.cell))
    owner.close()
    assert api.closed == [66]


def test_wrapper_raise_records_call_entry_and_preserves_output() -> None:
    api = Api()
    owner = binding.Handle(api, [])

    def fail(pointer: Any) -> NoReturn:
        C.cast(pointer, C.POINTER(W.HANDLE)).contents.value = 66
        raise RuntimeError("after native side effect")

    dynamic: Any = api
    dynamic.partial = fail
    with pytest.raises(RuntimeError):
        binding._call(api, owner.log, "partial", C.byref(owner.cell))
    assert owner.log == [{"call": "partial", "state": "raised", "error_type": "RuntimeError", "last_error": 0}]
    owner.close()
    assert api.closed == [66]


def test_returned_handle_registered_before_last_error_can_fail() -> None:
    api = Api()
    dynamic: Any = api
    dynamic.open_process = lambda: 88
    owner = binding.Handle(api, [])

    def unavailable() -> NoReturn:
        raise RuntimeError("last error wrapper")

    dynamic.last_error = unavailable
    with pytest.raises(RuntimeError):
        binding._call(api, owner.log, "open_process", _return_owner=owner)
    assert owner.cell.value == 88
    assert owner.log[0]["state"] == "raised"
    dynamic.last_error = lambda: 0
    owner.close()
    assert api.closed == [88]


def test_registration_rejects_wrong_nonce_and_paths() -> None:
    for names in (["seed_" + "b" * 32], [SEED + "\\child"], ["a" * 32], [SEED, "extra"]):
        with pytest.raises(ValueError, match="registered_seed_names"):
            binding.FreshBindingSession(PROFILE, PACKAGE, USER, names, SimpleNamespace())


def test_no_external_pid_or_path_entry_and_no_writes() -> None:
    tree = ast.parse(Path(binding.__file__).read_text(encoding="utf-8"))
    source = ast.unparse(tree)
    for forbidden in ("RegSetValue", "RegCreateKey", "RegSetKeySecurity", "SetSecurityInfo",
                      "RegEnum", "DeleteAppContainerProfile", "diagnose_worker_registry", "WriteApi"):
        assert forbidden not in source
    for argv in (["x", "--pid", "1"], ["x", "--_readonly-binding", "-1", PACKAGE, USER, NONCE]):
        with pytest.raises(ValueError, match="private_helper_arguments"):
            binding._main(argv)


def test_duplicate_json_keys_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate_json_key"):
        binding._decode(b'{"nonce":"a","nonce":"b"}')


def test_helper_start_challenge_precedes_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"START wrong\n")))
    assert binding._helper(55, PACKAGE, USER, NONCE) == 2


def test_token_each_class_one_fixed_buffer_no_resize() -> None:
    queried: list[int] = []
    api = Api()

    def get_token(token: int, kind: int, data: Any, capacity: int, returned: Any) -> bool:
        assert token == 55 and capacity == binding.MAX_TOKEN
        queried.append(kind)
        if kind in (1, 25, 31):
            sid = _sid({1: USER, 25: "S-1-16-4096", 31: PACKAGE}[kind])
            C.cast(data, C.POINTER(C.c_void_p)).contents.value = C.addressof(data) + 16
            C.memmove(C.addressof(data) + 16, sid, len(sid))
            length = 16 + len(sid)
        elif kind == 10:
            C.memmove(data, struct.pack("<II", 17, 2) + bytes(48), 56)
            length = 56
        else:
            scalar = {8: 1, 29: 1, 30: 0, 27: 1}[kind]
            C.memmove(data, struct.pack("<I", scalar), 4)
            length = 8 if kind == 30 else 4
        C.cast(returned, C.POINTER(W.DWORD)).contents.value = length
        return True

    dynamic: Any = api
    dynamic.get_token = get_token
    result = binding._token(api, [], 55)
    binding._verify_subject(result, PACKAGE, USER, 1)
    assert queried == [1, 8, 25, 29, 30, 31, 27, 10]
    assert result["token_id"] == [17, 2]


def test_thread_absence_rejects_partial_failure_and_owns_output() -> None:
    api = Api()
    api.error = 1008

    def open_thread(thread: int, access: int, as_self: bool, output: Any) -> bool:
        assert (thread, access, as_self) == (-2, 8, True)
        C.cast(output, C.POINTER(W.HANDLE)).contents.value = 77
        return False

    dynamic: Any = api
    dynamic.current_thread = lambda: -2
    dynamic.open_thread_token = open_thread
    owners: list[binding.Handle] = []
    with pytest.raises(ValueError, match="thread_token_not_absent"):
        binding._absent_thread(api, [], owners)
    owners[0].close()
    assert api.closed == [77]


def test_binding_failure_consumes_attempt_without_spawning() -> None:
    session = binding.FreshBindingSession(PROFILE, PACKAGE, USER, [SEED], SimpleNamespace())
    with pytest.raises(ValueError, match="helper_environment_keys"):
        session.bind({"PATH": "unexpected"})
    assert session.failed
    with pytest.raises(ValueError, match="already_consumed"):
        session.bind({})


def test_seed_rejects_unregistered_path_without_native() -> None:
    session = binding.FreshBindingSession(PROFILE, PACKAGE, USER, [SEED], SimpleNamespace(), api=Api())
    root = binding.BoundRoot(session, session._owner(key=True), {})
    session.binding = root
    with pytest.raises(ValueError, match="unregistered_seed"):
        root.read_seed(SEED + "\\child", phase="after_low")
    assert session.log == []


def _readable_root() -> tuple[binding.FreshBindingSession, binding.BoundRoot, Api]:
    api = Api()
    session = binding.FreshBindingSession(PROFILE, PACKAGE, USER, [SEED], SimpleNamespace(), api=api)
    owner = session._owner(key=True)
    owner.cell.value = 55
    root = binding.BoundRoot(session, owner, {})
    session.binding = root

    def open_key(handle: Any, name: str, options: int, access: int, output: Any) -> int:
        assert (handle.value, name, options, access) == (55, SEED, 0, binding.READ_CONTROL | 1)
        C.cast(output, C.POINTER(W.HANDLE)).contents.value = 77
        return 0

    def query(handle: Any, name: str, reserved: Any, kind: Any, data: Any, size: Any) -> int:
        assert (handle.value, name, reserved) == (77, "synthetic", None)
        C.cast(kind, C.POINTER(W.DWORD)).contents.value = 3
        C.cast(size, C.POINTER(W.DWORD)).contents.value = 8
        C.memmove(data, b"12345678", 8)
        return 0

    dynamic: Any = api
    dynamic.open_key, dynamic.query_value = open_key, query
    return session, root, api


def test_seed_two_ordered_once_only_phases() -> None:
    session, root, api = _readable_root()
    low = root.read_seed(SEED, phase="after_low")
    assert low["value_hex"] == b"12345678".hex()
    assert low["root_snapshot"]["raw_sd_hex"] == api.raw.hex()
    untrusted = root.read_seed(SEED, phase="after_untrusted")
    assert untrusted["snapshot"] == low["snapshot"]
    with pytest.raises(ValueError):
        root.read_seed(SEED, phase="after_untrusted")
    assert api.keys == [77, 77]


def test_seed_skipped_phase_is_sticky_without_native() -> None:
    session, root, _ = _readable_root()
    with pytest.raises(ValueError):
        root.read_seed(SEED, phase="after_untrusted")
    with pytest.raises(ValueError):
        root.read_seed(SEED, phase="after_low")
    assert session.failed and session.log == []


def test_seed_repeated_first_phase_blocks_second_phase() -> None:
    session, root, api = _readable_root()
    root.read_seed(SEED, phase="after_low")
    with pytest.raises(ValueError):
        root.read_seed(SEED, phase="after_low")
    with pytest.raises(ValueError):
        root.read_seed(SEED, phase="after_untrusted")
    assert session.failed and api.keys == [77]


def test_seed_first_phase_failure_blocks_all_future_phases() -> None:
    session, root, api = _readable_root()
    api.status = 5
    with pytest.raises(ValueError):
        root.read_seed(SEED, phase="after_low")
    assert session.failed
    api.status = 0
    phases: tuple[Literal["after_low", "after_untrusted"], ...] = ("after_low", "after_untrusted")
    for phase in phases:
        with pytest.raises(ValueError):
            root.read_seed(SEED, phase=phase)
    assert api.security_calls == 1


def test_query_error_preserves_raw_type_size_and_buffer() -> None:
    session, root, api = _readable_root()

    def query(handle: Any, name: str, reserved: Any, kind: Any, data: Any, size: Any) -> int:
        C.cast(kind, C.POINTER(W.DWORD)).contents.value = 3
        C.cast(size, C.POINTER(W.DWORD)).contents.value = 99
        C.memmove(data, b"partial!", 8)
        return 234

    dynamic: Any = api
    dynamic.query_value = query
    with pytest.raises(ValueError, match="query_value_failed"):
        root.read_seed(SEED, phase="after_low")
    event = next(item for item in session.log if item["call"] == "query_value")
    assert event["result"] == 234 and event["raw_type"] == 3
    assert event["required_or_returned_bytes"] == 99
    assert event["bounded_buffer_hex8"] == b"partial!".hex()
    assert event["valid_binary8"] is False


def test_seed_result_retained_when_final_close_fails() -> None:
    session, root, api = _readable_root()
    api.close_ok = False
    with pytest.raises(ValueError, match="reg_close_failed"):
        root.read_seed(SEED, phase="after_low")
    assert session.failed
    assert session.seed_results["after_low"]["value_hex"] == b"12345678".hex()
    assert session.seed_results["after_low"]["seed_close_confirmed"] is False
    assert session.seed_results["after_low"]["snapshot"]["raw_sd_hex"] == api.raw.hex()


def test_pipe_stdout_retained_when_stderr_peek_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    child: Any = SimpleNamespace(stdout=object(), stderr=object())
    owner = binding.HelperOwner(child, Api(), [])

    def chunk(stream: Any) -> bytes:
        if stream is child.stdout:
            return b"raw partial stdout"
        raise OSError("stderr peek failed")

    monkeypatch.setattr(owner, "_chunk", chunk)
    with pytest.raises(OSError):
        owner.pump(ready=True)
    assert owner.stdout == b"raw partial stdout"


def test_inheritance_reset_failure_reaps_registered_helper_without_start(monkeypatch: pytest.MonkeyPatch) -> None:
    api = Api()
    dynamic: Any = api
    dynamic.current_process = lambda: -1

    def open_token(process: int, access: int, pointer: Any) -> bool:
        C.cast(pointer, C.POINTER(W.HANDLE)).contents.value = 11 if process == 99 else 22
        return True

    dynamic.open_process_token = open_token
    calls: list[str] = []
    child: Any = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO(),
        _handle=SimpleNamespace(Close=lambda: calls.append("process_close")),
        kill=lambda: calls.append("kill"), wait=lambda timeout: calls.append("wait"))

    def inherit(handle: int, enabled: bool) -> None:
        calls.append("inherit" if enabled else "reset")
        if not enabled:
            raise OSError("reset failed")

    def metadata(_api: Any, _log: Any, token: Any) -> dict[str, Any]:
        return {"user_sid": USER, "package_sid": PACKAGE if token.value == 11 else None,
                "token_type": 1, "integrity_sid": "S-1-16-4096" if token.value == 11 else "S-1-16-8192",
                "is_appcontainer": int(token.value == 11), "capability_count": 0,
                "mandatory_policy": 1 if token.value == 11 else 3, "token_id": [token.value, 0]}

    monkeypatch.setattr(binding, "_token", metadata)
    monkeypatch.setattr(binding, "_absent_thread", lambda *args: None)
    monkeypatch.setattr(os, "set_handle_inheritable", inherit)
    monkeypatch.setattr(subprocess, "STARTUPINFO", SimpleNamespace, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: child)
    worker = SimpleNamespace(require_suspended_verified=lambda: 99)
    session = binding.FreshBindingSession(PROFILE, PACKAGE, USER, [SEED], worker, api=api)
    environment = {key: "C:\\fixed" for key in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP")}
    with pytest.raises(OSError, match="reset failed"):
        session.bind(environment)
    assert session.failed and session.helpers[0].reaped
    assert calls == ["inherit", "reset", "kill", "wait", "process_close"]
    assert session.helpers[0].job.cell.value is None
    assert api.closed == [22, 11]


@pytest.mark.parametrize("revert_fails", [False, True])
def test_helper_same_root_export_nonce_and_close_once(monkeypatch: pytest.MonkeyPatch, revert_fails: bool) -> None:
    api = Api()
    dynamic: Any = api
    dynamic.current_process = lambda: -1
    dynamic.current_thread = lambda: -2
    active = False

    def output(pointer: Any, value: int) -> None:
        C.cast(pointer, C.POINTER(W.HANDLE)).contents.value = value

    def open_primary(process: int, access: int, pointer: Any) -> bool:
        assert (process, access) == (-1, 8)
        output(pointer, 11)
        return True

    def duplicate(token: Any, access: int, security: Any, level: int, kind: int, pointer: Any) -> bool:
        assert (token.value, access, security, level, kind) == (55, 0xC, None, 2, 2)
        output(pointer, 22)
        return True

    def impersonate(token: Any) -> bool:
        nonlocal active
        assert token.value == 22
        active = True
        return True

    def revert() -> bool:
        nonlocal active
        assert active
        if revert_fails:
            return False
        active = False
        return True

    def thread(process: int, access: int, as_self: bool, pointer: Any) -> bool:
        assert (process, access, as_self) == (-2, 8, True)
        output(pointer, 33 if active else 0)
        api.error = 0 if active else 1008
        return active

    def location(access: int, pointer: Any) -> int:
        assert active and access == binding.READ_CONTROL
        output(pointer, 43)
        return 0

    def same_root(root: Any, name: Any, options: int, access: int, pointer: Any) -> int:
        assert active and (root.value, name, options, access) == (43, None, 0, binding.READ_CONTROL)
        output(pointer, 44)
        return 0

    def metadata(_api: Any, _log: Any, token: Any) -> dict[str, Any]:
        if token.value == 11:
            return {"user_sid": USER, "package_sid": None, "token_type": 1,
                    "integrity_sid": "S-1-16-8192", "is_appcontainer": 0, "capability_count": 0,
                    "mandatory_policy": 3, "token_id": [101, 0]}
        assert token.value in (55, 33)
        assert active == (token.value == 33)
        result: dict[str, Any] = {"user_sid": USER, "package_sid": PACKAGE, "token_type": 1 if token.value == 55 else 2,
                                  "integrity_sid": "S-1-16-4096", "is_appcontainer": 1, "capability_count": 0,
                                  "mandatory_policy": 1, "token_id": [102 if token.value == 55 else 103, 0]}
        if token.value == 33:
            result["impersonation_level"] = 2
        return result

    dynamic.open_process_token = open_primary
    dynamic.duplicate_token = duplicate
    dynamic.impersonate = impersonate
    dynamic.revert = revert
    dynamic.open_thread_token = thread
    dynamic.get_registry = location
    dynamic.open_key = same_root
    stdout = io.BytesIO()
    monkeypatch.setattr(binding, "ReadApi", lambda: api)
    monkeypatch.setattr(binding, "_token", metadata)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(
        ("START " + NONCE + "\nACK " + NONCE + "\n").encode("ascii"))))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=stdout))
    class ControlledExit(BaseException):
        pass

    def exit77(code: int) -> NoReturn:
        assert code == 77
        raise ControlledExit()

    monkeypatch.setattr(os, "_exit", exit77)
    if revert_fails:
        with pytest.raises(ControlledExit):
            binding._helper(55, PACKAGE, USER, NONCE)
        failed_frames = [binding._decode(line) for line in stdout.getvalue().splitlines()]
        assert [frame["status"] for frame in failed_frames] == ["fatal_revert_failure"]
        assert failed_frames[0]["calls"][-1]["call"] == "revert"
        assert failed_frames[0]["calls"][-1]["result"] == 0
        assert sum(event["call"] == "revert" for event in failed_frames[0]["calls"]) == 1
        assert api.closed == [] and api.keys == []
        return
    assert binding._helper(55, PACKAGE, USER, NONCE) == 0
    frames = [binding._decode(line) for line in stdout.getvalue().splitlines()]
    assert [frame["status"] for frame in frames] == ["ready", "complete"]
    assert frames[0]["exported_key"] == 44
    assert frames[0]["snapshot"]["raw_sd_hex"] == api.raw.hex()
    assert frames[1]["close_errors"] == []
    assert frames[1]["revert_confirmed"] is True
    assert api.keys == [44, 43]
    assert api.closed == [33, 22, 11, 55]
    assert not active


def _mock_host(monkeypatch: pytest.MonkeyPatch, failure: str = "") -> tuple[binding.FreshBindingSession, dict[str, Any], Api]:
    api = Api()
    state: dict[str, Any] = {"actions": [], "duplicated": [], "opens": [], "spawned": []}
    dynamic: Any = api
    dynamic.current_process = lambda: -1

    def open_token(process: int, access: int, pointer: Any) -> bool:
        assert (process, access) in ((99, 0xA), (-1, 8))
        C.cast(pointer, C.POINTER(W.HANDLE)).contents.value = 11 if process == 99 else 22
        return True

    def open_process(access: int, inherited: bool, pid: int) -> int:
        state["opens"].append((access, inherited, pid))
        return 33

    def duplicate(source: Any, exported: int, target: int, output: Any, access: int, inherited: bool, options: int) -> bool:
        state["duplicated"].append((source.value, exported, target, access, inherited, options))
        C.cast(output, C.POINTER(W.HANDLE)).contents.value = 55
        return failure != "partial_duplicate"

    def assign(job: Any, process: int) -> bool:
        assert (job.value, process) == (66, 90)
        state["actions"].append("assign")
        return failure != "assign_job"

    dynamic.open_process_token, dynamic.open_process = open_token, open_process
    dynamic.duplicate_handle = duplicate
    dynamic.create_job = lambda *args: 66
    dynamic.set_job = lambda *args: True
    dynamic.assign_job = assign

    def metadata(_api: Any, _log: Any, token: Any) -> dict[str, Any]:
        host = token.value == 22
        return {"user_sid": USER, "package_sid": None if host else PACKAGE,
                "token_type": 1, "integrity_sid": "S-1-16-8192" if host else "S-1-16-4096",
                "is_appcontainer": 0 if host else 1, "capability_count": 0,
                "mandatory_policy": 3 if host else 1, "token_id": [100 if host else 200, 0]}

    class ProcessHandle(int):
        def Close(self) -> None:
            state["actions"].append("process_close")

    child: Any = SimpleNamespace(pid=333, stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO(),
        _handle=ProcessHandle(90), returncode=None)
    child.kill = lambda: state["actions"].append("kill")

    def wait(timeout: int) -> int:
        state["actions"].append("wait")
        child.returncode = 0
        return 0

    child.wait = wait

    def spawn(command: list[str], **kwargs: Any) -> Any:
        assert kwargs["close_fds"] is True
        assert kwargs["startupinfo"].lpAttributeList == {"handle_list": [11]}
        assert command[-1] == NONCE
        state["spawned"].append(child.pid)
        return child

    def ready_report() -> dict[str, Any]:
        effective = metadata(api, [], SimpleNamespace(value=11))
        effective.update(token_type=2, impersonation_level=2, token_id=[201, 0])
        return {"stage0_passed": False, "status": "ready", "nonce": NONCE,
                "revert_confirmed": True, "exported_key": 44,
                "helper_primary": metadata(api, [], SimpleNamespace(value=22)),
                "inherited_worker_primary": metadata(api, [], SimpleNamespace(value=11)),
                "effective_thread": effective, "snapshot": binding.parse_sd(api.raw, PACKAGE, USER)}

    def pump(owner: binding.HelperOwner, *, ready: bool) -> dict[str, Any]:
        report = ready_report()
        if ready:
            if failure == "ready_nonce":
                report["nonce"] = "b" * 32
            return report
        owner.reaped = True
        child.returncode = 3 if failure == "final_exit" else 0
        report.update(status="failed" if failure == "final_status" else "complete", close_errors=[])
        if failure == "final_nonce":
            report["nonce"] = "b" * 32
        return report

    def send(owner: binding.HelperOwner, prefix: str, nonce: str) -> None:
        assert nonce == NONCE
        state["actions"].append(prefix)

    monkeypatch.setattr(binding, "_token", metadata)
    monkeypatch.setattr(binding, "_absent_thread", lambda *args: None)
    monkeypatch.setattr(binding, "uuid4", lambda: SimpleNamespace(hex=NONCE))
    monkeypatch.setattr(os, "set_handle_inheritable", lambda *args: None)
    monkeypatch.setattr(subprocess, "STARTUPINFO", SimpleNamespace, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(binding.HelperOwner, "pump", pump)
    monkeypatch.setattr(binding.HelperOwner, "send", send)
    worker = SimpleNamespace(require_suspended_verified=lambda: 99)
    session = binding.FreshBindingSession(PROFILE, PACKAGE, USER, [SEED], worker, api=api)
    return session, state, api


def _environment() -> dict[str, str]:
    return {key: "C:\\fixed" for key in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP")}


def test_host_binding_success_own_helper_rc_noninherit_ack_and_reap(monkeypatch: pytest.MonkeyPatch) -> None:
    session, state, api = _mock_host(monkeypatch)
    report = session.bind(_environment())
    assert report["helper_reaped"] is True and report["stage0_passed"] is False
    assert state["spawned"] == [333] and state["opens"] == [(0x40, False, 333)]
    assert state["duplicated"] == [(33, 44, -1, binding.READ_CONTROL, False, 0)]
    assert state["actions"] == ["assign", "START", "ACK", "process_close"]
    assert session.binding is not None and session.binding.owner.cell.value == 55
    assert 55 not in api.keys
    session.close()
    assert api.keys == [55]


@pytest.mark.parametrize("failure", ["ready_nonce", "partial_duplicate", "final_nonce", "final_status", "final_exit", "assign_job"])
def test_host_binding_fail_closed_cleanup(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    session, state, api = _mock_host(monkeypatch, failure)
    with pytest.raises(ValueError):
        session.bind(_environment())
    assert session.failed and session.binding is None
    assert session.helpers[0].reaped
    assert state["actions"].count("process_close") == 1
    if failure in ("ready_nonce", "assign_job"):
        assert state["duplicated"] == [] and "ACK" not in state["actions"]
    if failure == "assign_job":
        assert "START" not in state["actions"]
    if failure == "partial_duplicate":
        assert api.keys == [55] and "ACK" not in state["actions"]
    if failure.startswith("final_"):
        assert api.keys == [55] and "ACK" in state["actions"]
    session.close()
    assert api.keys.count(55) <= 1
