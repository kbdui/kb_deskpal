"""Pure mocks for the opt-in ACL diagnostic; no native ACL/profile calls."""

from __future__ import annotations

import ast
import copy
import ctypes as C
from ctypes import wintypes as W
from dataclasses import asdict
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest

from deskpal.execution.runtime import ProbeProfile, RuntimeManifest
from deskpal.execution.windows.launcher import LaunchRejected, LaunchSpec

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import diagnose_worker_registry_acl as diagnostic
import diagnose_worker_registry as readonly

PACKAGE = "S-1-15-2-1-2-3-4-5-6-7"
USER = "S-1-5-21-1-2-3-1001"
NONCE = "a" * 32
PROFILE = ProbeProfile("deskpal.v06." + NONCE, PACKAGE, "C:/synthetic/AC")


def _forbidden(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("native/profile/runtime operation forbidden in pure mocks")


@pytest.fixture(autouse=True)
def _no_native(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(C, "WinDLL", _forbidden, raising=False)
    for name in ("WriteApi", "create_probe_profile", "delete_probe_profile", "stage_runtime",
                 "apply_readonly_dacl", "_create_worker", "WindowsLauncher"):
        monkeypatch.setattr(diagnostic, name, _forbidden)
    monkeypatch.setattr(diagnostic, "_SESSIONS", set())
    monkeypatch.setattr(diagnostic, "_BINDINGS", set())
    monkeypatch.setattr(diagnostic, "_UNCONFIRMED", [])


def _identity(kind: int = 1) -> dict[str, object]:
    return {"user_sid": USER, "package_sid": PACKAGE, "integrity_sid": "S-1-16-4096",
            "is_appcontainer": 1, "capability_count": 0, "token_type": kind,
            "impersonation_level": 2 if kind == 2 else None}


def _snapshot(*, inherited: bool = False) -> dict[str, object]:
    entries = [(0, 2, 0xF003F, "S-1-5-18"), (0, 2, 0xF003F, USER),
               (0, 0x23, 0xF003F, PACKAGE)]
    if inherited:
        entries.append((0, 0x12, 0x20019, "S-1-5-32-545"))
    return {"metadata": {"owner_sid": USER, "dacl_null": False, "dacl_protected": False,
        "unparsed_ace_count": 0, "aces": [dict(type=t, flags=f, mask=m, sid=s) for t, f, m, s in entries]},
        "acl_revision": 2, "raw_aces_hex": [diagnostic._ace(*entry).hex() for entry in entries]}


def _worker_sd_bytes(entries: list[bytes] | None = None, *, control: int = 0x9004) -> bytes:
    owner = diagnostic._sid_bytes(USER)
    raw = entries if entries is not None else [diagnostic._ace(0, 0x23, 0xF003F, PACKAGE)]
    acl = struct.pack("<BBHHH", 2, 0, 8 + sum(map(len, raw)), len(raw), 0) + b"".join(raw)
    return struct.pack("<BBHLLLL", 1, 0, control, 20, 0, 0, 20 + len(owner)) + owner + acl


def _sd_observation(stage: str, call_index: int) -> dict[str, object]:
    return {"stage": stage, "sd_call_index": call_index, "status": "observed",
            "snapshot": diagnostic._parse_worker_sd(_worker_sd_bytes())}


def _worker_ast_nodes() -> list[ast.stmt]:
    return ast.parse(diagnostic.WORKER_SOURCE).body


def _worker_function(name: str, namespace: dict[str, Any]) -> Any:
    node = next(node for node in _worker_ast_nodes() if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<mock-worker-function>", "exec"), namespace)
    return namespace[name]


def test_worker_sd_parser_and_embedded_copy_are_native_free() -> None:
    sd = _worker_sd_bytes()
    parsed = diagnostic._parse_worker_sd(sd)
    assert parsed["owner_sid"] == USER and parsed["control"] == 0x9004
    assert parsed["sd_revision"] == 1 and parsed["acl_revision"] == 2
    assert parsed["dacl_protected"] is True and parsed["dacl_null"] is False
    assert parsed["raw_aces_hex"] == [diagnostic._ace(0, 0x23, 0xF003F, PACKAGE).hex()]
    embedded = _worker_function("_parse_worker_sd", {"struct": struct})
    assert embedded(sd) == parsed


@pytest.mark.parametrize("damage", ["short", "oversize", "revision", "relative", "owner_offset",
    "owner_count", "owner_revision", "acl_offset", "acl_revision", "acl_size", "ace_count",
    "ace_header", "ace_short", "ace_unaligned", "ace_overrun", "ace_bytes"])
def test_worker_sd_parser_rejects_malformed_or_unbounded_descriptors(damage: str) -> None:
    data = bytearray(_worker_sd_bytes())
    dacl = struct.unpack_from("<L", data, 16)[0]
    if damage == "short":
        data = data[:19]
    elif damage == "oversize":
        data.extend(b"\0" * 65536)
    elif damage == "revision":
        data[0] = 2
    elif damage == "relative":
        struct.pack_into("<H", data, 2, 4)
    elif damage == "owner_offset":
        struct.pack_into("<L", data, 4, len(data) - 7)
    elif damage == "owner_count":
        data[21] = 16
    elif damage == "owner_revision":
        data[20] = 2
    elif damage == "acl_offset":
        struct.pack_into("<L", data, 16, len(data) - 7)
    elif damage == "acl_revision":
        data[dacl] = 3
    elif damage == "acl_size":
        struct.pack_into("<H", data, dacl + 2, len(data))
    elif damage == "ace_count":
        struct.pack_into("<H", data, dacl + 4, 33)
    elif damage == "ace_header":
        struct.pack_into("<H", data, dacl + 4, 2)
    elif damage in ("ace_short", "ace_unaligned", "ace_overrun"):
        struct.pack_into("<H", data, dacl + 10, {"ace_short": 3, "ace_unaligned": 5, "ace_overrun": 4096}[damage])
    else:
        data = bytearray(_worker_sd_bytes([struct.pack("<BBH", 0, 0, 100) + b"\0" * 96] * 21))
    with pytest.raises(ValueError, match="worker_sd_"):
        diagnostic._parse_worker_sd(bytes(data))


def test_worker_sd_parser_accepts_exact_ace_budgets_and_null_acl() -> None:
    raw = struct.pack("<BBH", 0, 0, 64) + b"\0" * 60
    parsed = diagnostic._parse_worker_sd(_worker_sd_bytes([raw] * 32))
    assert parsed["raw_aces_hex"] == [raw.hex()] * 32
    for control in (0x8000, 0x8004):
        data = bytearray(_worker_sd_bytes(control=control))
        struct.pack_into("<L", data, 16, 0)
        parsed = diagnostic._parse_worker_sd(bytes(data))
        assert parsed["dacl_null"] is True and parsed["raw_aces_hex"] == []
        assert parsed["acl_revision"] is None


@pytest.mark.parametrize("failure", ["none", "query", "query_partial", "size", "parse", "free", "free_raise"])
def test_worker_sd_query_frees_partial_output_and_marks_failed_evidence_unknown(failure: str) -> None:
    data = _worker_sd_bytes() if failure != "parse" else b"x" * 20
    descriptor = C.create_string_buffer(data)
    frees: list[int] = []
    queries: list[int] = []

    def query(root: Any, kind: int, information: int, _owner: Any, _group: Any,
              _dacl: Any, _sacl: Any, output: Any) -> int:
        assert kind == 4 and information == 5
        queries.append(int(root.value))
        if failure != "query":
            C.cast(output, C.POINTER(C.c_void_p)).contents.value = C.addressof(descriptor)
        return 5 if failure in ("query", "query_partial") else 0

    def free(pointer: Any) -> int:
        frees.append(int(pointer.value))
        if failure == "free_raise":
            raise OSError("synthetic release failure")
        return int(pointer.value) if failure == "free" else 0

    observe = _worker_function("observe_sd", {"C": C, "getsecurity": query,
        "sdlength": lambda _pointer: 65537 if failure == "size" else len(data),
        "free": free, "_parse_worker_sd": diagnostic._parse_worker_sd})
    result = observe(W.HKEY(77), "before_permission_calls", 1)
    assert queries == [77] and frees == ([] if failure == "query" else [C.addressof(descriptor)])
    assert result["stage"] == "before_permission_calls" and result["sd_call_index"] == 1
    assert result["status"] == ("observed" if failure == "none" else "unknown")
    if failure.startswith("free"):
        assert "release_error" in result and "snapshot" in result
    elif failure != "none":
        assert "error" in result and "snapshot" not in result


class _Api:
    def __init__(self) -> None:
        self.closed: list[int] = []
        self.close_fail: set[int] = set()
        self.set_calls = 0
        self.disposition = 1
        self.duplicate_ok = True
        self.duplicate_raise = False

    def reg_close(self, handle: Any) -> int:
        self.closed.append(int(handle.value))
        return 5 if handle.value in self.close_fail else 0

    def close(self, handle: Any) -> bool:
        return self.reg_close(handle) == 0

    def create_key(self, *_args: Any) -> int:
        C.cast(_args[-2], C.POINTER(W.HKEY)).contents.value = 70
        C.cast(_args[-1], C.POINTER(W.DWORD)).contents.value = self.disposition
        return 0

    def set_value(self, *_args: Any) -> int:
        assert _args[-2:] == (diagnostic.CANARY, 8)
        self.set_calls += 1
        return 0

    def set_security(self, *_args: Any) -> int:
        self.set_calls += 1
        return 0

    def windows_directory(self, buffer: Any, _length: int) -> int:
        buffer.value = "C:/Windows"
        return len(buffer.value)

    def current_process(self) -> int:
        return -1

    def open_process_token(self, process: int, _access: int, output: Any) -> bool:
        C.cast(output, C.POINTER(W.HANDLE)).contents.value = 11 if process != -1 else 12
        return True

    def open_process(self, access: int, inherit: bool, pid: int) -> int:
        assert (access, inherit, pid) == (0x40, False, 900)
        return 13

    def duplicate_handle(self, source: Any, exported: int, target: int, output: Any,
                         access: int, inherit: bool, options: int) -> bool:
        assert (source.value, exported, target, access, inherit, options) == (13, 99, -1, 0x60000, False, 0)
        C.cast(output, C.POINTER(W.HANDLE)).contents.value = 71
        if self.duplicate_raise:
            raise RuntimeError("after native duplicate success")
        return self.duplicate_ok


def _session(monkeypatch: pytest.MonkeyPatch, api: _Api, *, deny_target: str = "package",
             restore_for_cleanup: bool = False) -> diagnostic.Session:
    monkeypatch.setattr(diagnostic, "create_probe_profile", lambda _name: PROFILE)
    session = diagnostic.Session(cast(diagnostic.WriteApi, api), deny_target=deny_target,
                                 restore_for_cleanup=restore_for_cleanup)
    assert session.create() is PROFILE
    return session


def _binding(monkeypatch: pytest.MonkeyPatch, api: _Api, *, deny_target: str = "package",
             restore_for_cleanup: bool = False) -> diagnostic.BoundRoot:
    root = diagnostic.BoundRoot(_session(monkeypatch, api, deny_target=deny_target,
                                       restore_for_cleanup=restore_for_cleanup))
    root.owner.handle.value, root.user, root.before, root.validated = 50, USER, _snapshot(), True
    return root


def test_plan_keeps_critical_bytes_and_canonical_position() -> None:
    snapshot = _snapshot(inherited=True)
    untouched = copy.deepcopy(snapshot)
    acl, raw = diagnostic._plan(snapshot, PACKAGE, USER)
    original = cast(list[str], snapshot["raw_aces_hex"])
    inherited = bytes.fromhex(original[-1])
    converted = inherited[:1] + bytes((inherited[1] & ~0x10,)) + inherited[2:]
    assert raw == [diagnostic._ace(1, 2, 0xD0026, PACKAGE).hex(), *original[:-1],
                   diagnostic._ace(0, 2, 0x20000, "S-1-3-4").hex(), converted.hex()]
    assert bytes.fromhex(original[2])[1] == 0x23 and raw[3] == original[2]
    assert snapshot == untouched
    assert len(acl) == int.from_bytes(acl[2:4], "little")
    assert int.from_bytes(acl[4:6], "little") == len(raw)


@pytest.mark.parametrize("inherited", [False, True])
def test_user_deny_plan_changes_only_new_deny_trustee(inherited: bool) -> None:
    snapshot = _snapshot(inherited=inherited)
    untouched = copy.deepcopy(snapshot)
    package_acl, package_plan = diagnostic._plan(snapshot, PACKAGE, USER)
    user_acl, user_plan = diagnostic._plan(snapshot, PACKAGE, USER, deny_target="user")
    assert package_plan[0] == diagnostic._ace(1, 2, 0xD0026, PACKAGE).hex()
    assert user_plan[0] == diagnostic._ace(1, 2, 0xD0026, USER).hex()
    assert user_plan[1:] == package_plan[1:] and snapshot == untouched
    # The only header difference is the size required by the different SID.
    assert user_acl[:2] == package_acl[:2] and user_acl[4:8] == package_acl[4:8]
    assert len(user_acl) == int.from_bytes(user_acl[2:4], "little")
    assert user_acl[8:] == b"".join(bytes.fromhex(item) for item in user_plan)
    audit = diagnostic._input_plan_audit(snapshot, user_acl, user_plan, PACKAGE,
                                         user=USER, deny_target="user")
    assert (audit["deny_target"], audit["denied_sid"]) == ("user", USER)
    assert audit["critical_aces_preserved_in_plan"] is True
    assert audit["inherited_conversion_original_indices"] == ([3] if inherited else [])


@pytest.mark.parametrize("plan_target,audit_target", [("package", "user"), ("user", "package")])
def test_input_plan_audit_rejects_other_selected_trustee(plan_target: str, audit_target: str) -> None:
    snapshot = _snapshot(inherited=True)
    acl, planned = diagnostic._plan(snapshot, PACKAGE, USER, deny_target=plan_target)
    with pytest.raises(ValueError, match="acl_plan_added_aces_changed"):
        diagnostic._input_plan_audit(snapshot, acl, planned, PACKAGE, user=USER, deny_target=audit_target)


@pytest.mark.parametrize("deny_target", ["package", "user"])
def test_apply_rejects_plan_for_other_trustee_before_set(
    monkeypatch: pytest.MonkeyPatch, deny_target: str,
) -> None:
    api = _Api()
    binding = _binding(monkeypatch, api, deny_target=deny_target)
    other_target = "package" if deny_target == "user" else "user"
    acl, planned = diagnostic._plan(binding.before, PACKAGE, USER, deny_target=other_target)

    def wrong_plan(snapshot: dict[str, object], package: str, user: str, *, deny_target: str) -> tuple[bytes, list[str]]:
        assert snapshot == binding.before and (package, user) == (PACKAGE, USER)
        assert deny_target == binding.session.deny_target
        return acl, planned

    monkeypatch.setattr(diagnostic, "_snapshot", lambda *_args: copy.deepcopy(binding.before))
    monkeypatch.setattr(diagnostic, "_plan", wrong_plan)
    with pytest.raises(ValueError, match="acl_plan_added_aces_changed"):
        diagnostic._apply(binding)
    assert api.set_calls == 0 and binding.dacl_audit["set_attempted"] is False
    assert binding.dacl_audit["failed_at"] == "input_planning"
    assert not binding.session.close_resources()


@pytest.mark.parametrize("subject", ["S-1-5-18", "S-1-5-19", "S-1-5-20", "S-1-5-018"])
def test_user_deny_refuses_service_subject_before_query_or_set(
    monkeypatch: pytest.MonkeyPatch, subject: str,
) -> None:
    snapshot = _snapshot()
    acl, planned = diagnostic._plan(snapshot, PACKAGE, USER)
    with pytest.raises(ValueError, match="acl_service_user_deny_refused"):
        diagnostic._plan(snapshot, PACKAGE, subject, deny_target="user")
    with pytest.raises(ValueError, match="acl_service_user_deny_refused"):
        diagnostic._input_plan_audit(snapshot, acl, planned, PACKAGE, user=subject, deny_target="user")
    api = _Api()
    binding = _binding(monkeypatch, api, deny_target="user")
    binding.user = subject
    monkeypatch.setattr(diagnostic, "_snapshot", _forbidden)
    with pytest.raises(ValueError, match="acl_service_user_deny_refused"):
        diagnostic._apply(binding)
    assert api.set_calls == 0 and binding.dacl_audit["set_attempted"] is False
    assert binding.session.cleanup_state()["evidence_unknown"] is True
    assert not binding.session.close_resources()


@pytest.mark.parametrize("deny_target", ["", "arbitrary", USER])
def test_invalid_deny_target_is_rejected_before_native_or_set(
    monkeypatch: pytest.MonkeyPatch, deny_target: str,
) -> None:
    snapshot = _snapshot()
    acl, planned = diagnostic._plan(snapshot, PACKAGE, USER)
    with pytest.raises(ValueError, match="acl_deny_target_invalid"):
        diagnostic.Session(cast(diagnostic.WriteApi, _Api()), deny_target=deny_target)
    with pytest.raises(ValueError, match="acl_deny_target_invalid"):
        diagnostic._plan(snapshot, PACKAGE, USER, deny_target=deny_target)
    with pytest.raises(ValueError, match="acl_deny_target_invalid"):
        diagnostic._input_plan_audit(snapshot, acl, planned, PACKAGE, user=USER, deny_target=deny_target)
    with pytest.raises(ValueError, match="acl_deny_target_invalid"):
        diagnostic._experiment(io.StringIO(), {}, Path("C:/synthetic/python.exe"),
                               Path("C:/synthetic/runtime"), deny_target=deny_target)
    api = _Api()
    binding = _binding(monkeypatch, api)
    binding.session.deny_target = deny_target
    monkeypatch.setattr(diagnostic, "_snapshot", _forbidden)
    with pytest.raises(ValueError, match="acl_deny_target_invalid"):
        diagnostic._apply(binding)
    assert api.set_calls == 0 and binding.dacl_audit["set_attempted"] is False
    assert not binding.session.close_resources()


@pytest.mark.parametrize("flags", [0x10, 0x11, 0x12, 0x13, 0x18, 0x1B])
def test_input_plan_audits_only_inherited_bit_conversion(flags: int) -> None:
    snapshot = _snapshot(inherited=True)
    metadata = cast(dict[str, Any], snapshot["metadata"])
    metadata["aces"][-1]["flags"] = flags
    original = cast(list[str], snapshot["raw_aces_hex"])
    original[-1] = diagnostic._ace(0, flags, 0x20019, "S-1-5-32-545").hex()
    acl, planned = diagnostic._plan(snapshot, PACKAGE, USER)
    audit = diagnostic._input_plan_audit(snapshot, acl, planned, PACKAGE)
    assert audit["inherited_conversion_original_indices"] == [3]
    assert audit["inherited_conversion_planned_indices"] == [5]
    assert audit["original_aces_preserved_in_plan"] is False
    assert audit["critical_aces_preserved_in_plan"] is True
    assert audit["only_inherited_bit_cleared_in_plan"] is True
    records = cast(list[dict[str, object]], audit["original_ace_plan"])
    assert len(records) == len(original)
    for index, record in enumerate(records):
        assert record["original_index"] == index and record["original_ace_hex"] == original[index]
        assert record["planned_ace_hex"] == planned[int(str(record["planned_index"]))]
        assert record["inherited_bit_cleared"] is (index == 3)
        before, after = bytes.fromhex(original[index]), bytes.fromhex(str(record["planned_ace_hex"]))
        assert before[:1] == after[:1] and before[2:] == after[2:]
        assert after[1] == before[1] & ~0x10
    assert records[2]["critical"] is True and records[2]["original_ace_hex"] == records[2]["planned_ace_hex"]
    assert "critical_aces_preserved_bytewise" not in audit  # Planning is not readback.


@pytest.mark.parametrize("shape", ["inherited_deny", "critical_inherited", "critical_package_inherited"])
def test_apply_refuses_unapproved_conversion_before_set(monkeypatch: pytest.MonkeyPatch, shape: str) -> None:
    api = _Api()
    binding = _binding(monkeypatch, api)
    snapshot = _snapshot(inherited=True)
    metadata = cast(dict[str, Any], snapshot["metadata"])
    raw = cast(list[str], snapshot["raw_aces_hex"])
    index = 2 if shape == "critical_package_inherited" else 3
    entry = metadata["aces"][index]
    if shape == "inherited_deny":
        entry["type"] = 1
    else:
        entry["flags"] |= 0x30
    raw[index] = diagnostic._ace(entry["type"], entry["flags"], entry["mask"], entry["sid"]).hex()
    binding.before = snapshot
    monkeypatch.setattr(diagnostic, "_snapshot", lambda *_args: copy.deepcopy(snapshot))
    message = {"inherited_deny": "converted_order_not_canonical",
        "critical_inherited": "critical_inherited_conversion_refused",
        "critical_package_inherited": "critical_package_allow_unexpected"}[shape]
    with pytest.raises(ValueError, match=message):
        diagnostic._apply(binding)
    assert api.set_calls == 0
    assert binding.dacl_audit["failed_at"] == "input_planning"
    assert binding.dacl_audit["set_attempted"] is False
    assert "critical_aces_preserved_bytewise" not in binding.dacl_audit
    assert binding.session.cleanup_state()["evidence_unknown"] is True
    assert not binding.session.close_resources()


def test_plan_preserves_other_explicit_critical_ace_bytewise() -> None:
    snapshot = _snapshot(inherited=True)
    metadata = cast(dict[str, Any], snapshot["metadata"])
    metadata["aces"][-1]["flags"] = 0x22
    original = cast(list[str], snapshot["raw_aces_hex"])
    original[-1] = diagnostic._ace(0, 0x22, 0x20019, "S-1-5-32-545").hex()
    acl, planned = diagnostic._plan(snapshot, PACKAGE, USER)
    audit = diagnostic._input_plan_audit(snapshot, acl, planned, PACKAGE)
    assert planned[1:-1] == original
    assert audit["inherited_conversion_original_indices"] == []
    assert audit["original_aces_preserved_in_plan"] is True
    assert audit["critical_aces_preserved_in_plan"] is True
    records = cast(list[dict[str, object]], audit["original_ace_plan"])
    assert [item["original_index"] for item in records if item["critical"]] == [2, 3]


@pytest.mark.parametrize("mutation", ["package_flags", "raw_mismatch", "deny_after_allow", "explicit_after_inherited", "revision"])
def test_plan_refuses_ambiguous_or_noncanonical_baseline(mutation: str) -> None:
    snapshot = _snapshot(inherited=True)
    metadata = cast(dict[str, Any], snapshot["metadata"])
    raw = cast(list[str], snapshot["raw_aces_hex"])
    if mutation == "package_flags":
        metadata["aces"][2]["flags"] = 3
    elif mutation == "raw_mismatch":
        raw[0] = diagnostic._ace(0, 2, 0x20000, "S-1-5-18").hex()
    elif mutation in ("deny_after_allow", "explicit_after_inherited"):
        kind = 1 if mutation == "deny_after_allow" else 0
        entry = dict(type=kind, flags=2, mask=2, sid=USER)
        index = 3 if mutation == "deny_after_allow" else 4
        metadata["aces"].insert(index, entry)
        raw.insert(index, diagnostic._ace(kind, 2, 2, USER).hex())
    else:
        snapshot["acl_revision"] = 3
    with pytest.raises(ValueError):
        diagnostic._plan(snapshot, PACKAGE, USER)


@pytest.mark.parametrize("subject", ["S-1-3-4", "S-1-15-2-9-9-9"])
def test_baseline_rejects_other_package_and_preexisting_owner_rights(subject: str) -> None:
    snapshot = _snapshot()
    metadata = cast(dict[str, Any], snapshot["metadata"])
    metadata["aces"].append(dict(type=0, flags=2, mask=0x20000, sid=subject))
    with pytest.raises(ValueError, match="other_package_or_existing_owner_rights"):
        diagnostic._baseline(snapshot, PACKAGE, USER)


@pytest.mark.parametrize("payload", [b'{"x":1,"x":2}', b'{"x":NaN}', b'[]', b'{}\n{}', b'x' * (128 * 1024 + 1)],
                         ids=["duplicate", "nonfinite", "array", "multiple", "over-budget"])
def test_helper_messages_reject_duplicate_nonfinite_shape_frames_and_budget(payload: bytes) -> None:
    with pytest.raises(ValueError):
        diagnostic._decode_message(payload)


def test_session_cannot_adopt_profile_or_restart_after_close(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _Api()
    adopted = diagnostic.Session(cast(diagnostic.WriteApi, api))
    adopted.profile = PROFILE
    with pytest.raises(ValueError, match="own_fresh_session"):
        diagnostic.BoundRoot(adopted)
    session = _session(monkeypatch, api)
    assert session.close_resources()
    with pytest.raises(ValueError):
        session.create()
    with pytest.raises(ValueError):
        diagnostic.BoundRoot(session)


@pytest.mark.parametrize("change", ["profile", "root", "unknown", "closed"])
def test_binding_checks_current_session_identity(monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    binding = _binding(monkeypatch, _Api())
    if change == "profile":
        binding.session.profile = ProbeProfile(PROFILE.name, PACKAGE, PROFILE.folder)
    elif change == "root":
        binding.session.root = None
    elif change == "unknown":
        binding.session.unknown = True
    else:
        binding.session.close_resources()
    with pytest.raises(ValueError):
        binding.require()


def test_failed_close_retains_owner_and_continues_other_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _Api()
    binding = _binding(monkeypatch, api)
    failed, other = diagnostic.HandleOwner(cast(diagnostic.WriteApi, api)), diagnostic.HandleOwner(cast(diagnostic.WriteApi, api))
    failed.handle.value, other.handle.value = 20, 21
    binding.session.handles.extend([other, failed])
    api.close_fail.add(20)
    assert not binding.session.close_resources()
    assert api.closed == [20, 21, 50]
    assert failed.handle.value == 20 and failed in diagnostic._UNCONFIRMED
    api.close_fail.clear()
    assert not binding.session.close_resources()  # Unknown stays sticky after retry.
    state = binding.session.cleanup_state()
    assert state["unknown"] is True and state["evidence_unknown"] is False


def test_generic_owner_return_does_not_verify_individual_os_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session(monkeypatch, _Api())
    calls: list[str] = []
    session.owners.append(SimpleNamespace(close=lambda: calls.append("close")))
    assert session.close_resources()
    state = session.cleanup_state()
    assert calls == ["close"]
    assert state["unknown"] is False and state["evidence_unknown"] is False
    assert state["resource_close_unconfirmed"] is False
    assert state["individual_adapter_handle_closes_verified"] is False
    assert state["popen_own_process_handle_verified"] is False
    record = cast(list[dict[str, object]], state["resources"])[0]
    assert record["close_call_completed"] is True
    assert record["close_scope"] == "owner_contract_only"
    assert record["closed"] is None and record["reaped"] is None


@pytest.mark.parametrize("evidence_first", [False, True])
def test_unknown_classification_survives_reason_and_resource_budgets(
    monkeypatch: pytest.MonkeyPatch, evidence_first: bool,
) -> None:
    session = _session(monkeypatch, _Api())
    monkeypatch.setattr(diagnostic, "MAX_UNKNOWN_REASONS", 2)
    monkeypatch.setattr(diagnostic, "MAX_CLEANUP_RECORDS", 1)
    if evidence_first:
        session.mark_unknown("failed readback")

    def fail_close() -> NoReturn:
        raise OSError("synthetic close failure")

    session.owners.extend(SimpleNamespace(close=fail_close) for _index in range(3))
    assert not session.close_resources()
    if not evidence_first:
        assert session.cleanup_state()["evidence_unknown"] is False
        session.mark_unknown("failed readback")
    state = session.cleanup_state()
    assert state["unknown"] is True and state["evidence_unknown"] is True
    assert state["resource_close_unconfirmed"] is True
    assert state["unknown_reasons_truncated"] is True
    assert len(cast(list[object], state["unknown_reasons"])) == 2
    assert state["resources_truncated"] is True
    assert len(cast(list[object], state["resources"])) == 1
    if not evidence_first:
        assert all(item["category"] == "resource_unconfirmed"
                   for item in cast(list[dict[str, str]], state["unknown_reasons"]))
    # Repeated cleanup cannot clear the evidence classification or retention.
    session.owners.clear()
    assert not session.close_resources()
    assert session.cleanup_state()["evidence_unknown"] is True


def test_existing_seed_never_writes_and_is_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _Api()
    api.disposition = 2
    binding = _binding(monkeypatch, api)
    with pytest.raises(ValueError, match="existing_refused"):
        diagnostic._seed(binding, {"set": "DeskpalV06_" + NONCE + "_set"})
    assert api.set_calls == 0 and binding.session.unknown
    assert not binding.session.close_resources()
    assert 70 in api.closed


@pytest.mark.parametrize("deny_target", ["package", "user"])
@pytest.mark.parametrize("failure", ["changed_before", "readback_raise", "readback_bytes", "readback_owner", "readback_protection", "readback_revision", "readback_null"])
def test_apply_requires_exact_readback_and_retains_unknown_session(
    monkeypatch: pytest.MonkeyPatch, failure: str, deny_target: str,
) -> None:
    api = _Api()
    binding = _binding(monkeypatch, api, deny_target=deny_target)
    before = copy.deepcopy(binding.before)
    after = copy.deepcopy(before)
    _, after["raw_aces_hex"] = diagnostic._plan(before, PACKAGE, USER, deny_target=deny_target)
    metadata = cast(dict[str, Any], after["metadata"])
    metadata["dacl_protected"] = True
    if failure == "changed_before":
        before["acl_revision"] = 4
    elif failure == "readback_bytes":
        after["raw_aces_hex"] = binding.before["raw_aces_hex"]
    elif failure == "readback_owner":
        metadata["owner_sid"] = "S-1-5-18"
    elif failure == "readback_protection":
        metadata["dacl_protected"] = False
    elif failure == "readback_revision":
        after["acl_revision"] = 4
    elif failure == "readback_null":
        metadata["dacl_null"] = True
    snapshots = iter([before, after])

    def snapshot(*_args: object) -> dict[str, object]:
        value = next(snapshots)
        if value is after and failure == "readback_raise":
            raise OSError("unconfirmed readback")
        return value

    monkeypatch.setattr(diagnostic, "_snapshot", snapshot)
    with pytest.raises((ValueError, OSError)):
        diagnostic._apply(binding)
    assert binding.session.unknown
    assert api.set_calls == (0 if failure == "changed_before" else 1)
    audit = binding.dacl_audit
    assert audit["deny_target"] == deny_target and audit["denied_sid"] == (USER if deny_target == "user" else PACKAGE)
    assert audit["state"] == "failed" and audit["readback_gate_passed"] is False
    assert audit["before"] == before and audit["binding_baseline"] == binding.before
    if failure != "changed_before":
        _, expected = diagnostic._plan(binding.before, PACKAGE, USER, deny_target=deny_target)
        assert audit["expected_raw_aces_hex"] == expected
        assert audit["expected_readback"] == {"owner_sid": USER, "dacl_protected": True,
            "dacl_null": False, "acl_revision": 2, "raw_aces_hex": expected}
        assert audit["set_returned"] is True and audit["set_security_status"] == 0
        if failure == "readback_raise":
            assert audit["failed_at"] == "readback_query"
            assert audit["actual_snapshot_obtained"] is False and "after" not in audit
        else:
            assert audit["failed_at"] == "readback_obtained"
            assert audit["after"] == after and audit["actual_snapshot_obtained"] is True
            checks = cast(dict[str, bool], audit["gate_checks"])
            assert sum(not result for result in checks.values()) == 1
    assert not binding.session.close_resources()


@pytest.mark.parametrize("failure", ["before_query", "set_raise", "set_status"])
def test_apply_audits_failed_query_and_set_without_claiming_readback(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    api = _Api()
    binding = _binding(monkeypatch, api)
    queries: list[str] = []

    def snapshot(*_args: object) -> dict[str, object]:
        queries.append("query")
        if failure == "before_query":
            raise OSError("synthetic query failure")
        return copy.deepcopy(binding.before)

    def set_security(*_args: object) -> int:
        api.set_calls += 1
        if failure == "set_raise":
            raise OSError("set may have taken effect before wrapper failed")
        return 5

    monkeypatch.setattr(diagnostic, "_snapshot", snapshot)
    monkeypatch.setattr(api, "set_security", set_security)
    with pytest.raises(OSError):
        diagnostic._apply(binding)
    audit = binding.dacl_audit
    assert audit["state"] == "failed" and audit["error_type"] == "OSError"
    assert audit["actual_snapshot_obtained"] is False and "after" not in audit
    assert "original_aces_preserved_bytewise" not in audit
    assert "critical_aces_preserved_bytewise" not in audit
    assert audit["readback_gate_passed"] is False and queries == ["query"]
    assert audit["before_snapshot_obtained"] is (failure != "before_query")
    assert audit["set_attempted"] is (failure != "before_query")
    assert audit["set_returned"] is (failure == "set_status")
    assert api.set_calls == (0 if failure == "before_query" else 1)
    if failure == "before_query":
        assert audit["failed_at"] == "before_query" and "expected_readback" not in audit
    else:
        assert "expected_readback" in audit and "planned_acl_hex" in audit
        assert audit["failed_at"] == ("set_call" if failure == "set_raise" else "set_returned")
    assert audit.get("set_security_status") == (5 if failure == "set_status" else None)
    assert binding.session.cleanup_state()["evidence_unknown"] is True
    assert not binding.session.close_resources()
    state = binding.session.cleanup_state()
    assert state["evidence_unknown"] is True and state["resource_close_unconfirmed"] is False
    assert cast(list[dict[str, object]], state["resources"])[0]["closed"] is True


@pytest.mark.parametrize("deny_target", ["package", "user"])
@pytest.mark.parametrize("mutation", [
    "none", "none_no_conversion",
    *(f"byte-{index}" for index in range(24) if index != 1),
    *(f"flag-{bit}" for bit in (1, 2, 4, 8, 16, 32, 64, 128)),
    "critical", "deny", "owner_rights", "order", "extra", "query_failure",
])
def test_converted_input_still_requires_unmodified_exact_readback(
    monkeypatch: pytest.MonkeyPatch, mutation: str, deny_target: str,
) -> None:
    api = _Api()
    binding = _binding(monkeypatch, api, deny_target=deny_target)
    inherited = mutation != "none_no_conversion"
    binding.before = _snapshot(inherited=inherited)
    _acl, planned = diagnostic._plan(binding.before, PACKAGE, USER, deny_target=deny_target)
    after = copy.deepcopy(binding.before)
    actual = list(planned)
    cast(dict[str, object], after["metadata"])["dacl_protected"] = True
    if mutation.startswith(("byte-", "flag-")):
        changed = bytearray.fromhex(actual[-1])
        if mutation.startswith("byte-"):
            changed[int(mutation.split("-")[1])] ^= 1
        else:
            changed[1] ^= int(mutation.split("-")[1])
        actual[-1] = changed.hex()
    elif mutation in ("critical", "deny", "owner_rights"):
        index = {"critical": 3, "deny": 0, "owner_rights": 4}[mutation]
        changed = bytearray.fromhex(actual[index])
        changed[4] ^= 1
        actual[index] = changed.hex()
    elif mutation == "order":
        actual[1], actual[2] = actual[2], actual[1]
    elif mutation == "extra":
        actual.append(actual[-1])
    after["raw_aces_hex"] = actual
    queries = 0

    def snapshot(*_args: object) -> dict[str, object]:
        nonlocal queries
        queries += 1
        if queries == 2 and mutation == "query_failure":
            raise OSError("synthetic converted-plan readback failure")
        return copy.deepcopy(binding.before if queries == 1 else after)

    monkeypatch.setattr(diagnostic, "_snapshot", snapshot)
    success = mutation in ("none", "none_no_conversion")
    if success:
        diagnostic._apply(binding)
    else:
        with pytest.raises((ValueError, OSError)):
            diagnostic._apply(binding)
    audit = binding.dacl_audit
    assert audit["deny_target"] == deny_target and audit["denied_sid"] == (USER if deny_target == "user" else PACKAGE)
    assert api.set_calls == 1 and audit["expected_raw_aces_hex"] == planned
    assert audit["critical_aces_preserved_in_plan"] is True
    assert audit["original_aces_preserved_in_plan"] is (not inherited)
    assert audit["inherited_conversion_original_indices"] == ([3] if inherited else [])
    assert audit["readback_gate_passed"] is success
    if success:
        assert audit["state"] == "verified" and not binding.session.unknown
        assert audit["original_aces_preserved_bytewise"] is (not inherited)
        assert audit["critical_aces_preserved_bytewise"] is True
        assert audit["only_inherited_bit_cleared_bytewise"] is True
        assert binding.session.close_resources()
    else:
        assert "original_aces_preserved_bytewise" not in audit
        assert "critical_aces_preserved_bytewise" not in audit
        assert binding.session.cleanup_state()["evidence_unknown"] is True
        assert not binding.session.close_resources()
        if mutation == "query_failure":
            assert audit["actual_snapshot_obtained"] is False and "after" not in audit
        else:
            assert audit["after"] == after
            assert cast(dict[str, bool], audit["gate_checks"])["raw_aces_match"] is False


@pytest.mark.parametrize("change", ["other_flag", "mask", "sid", "critical", "deny", "buffer"])
def test_input_plan_audit_rejects_changes_beyond_inherited_bit_before_set(
    monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    api = _Api()
    binding = _binding(monkeypatch, api)
    binding.before = _snapshot(inherited=True)
    acl, planned = diagnostic._plan(binding.before, PACKAGE, USER)
    if change == "buffer":
        acl = acl[:-1] + bytes((acl[-1] ^ 1,))
    else:
        index = 3 if change == "critical" else 0 if change == "deny" else 5
        changed = bytearray.fromhex(planned[index])
        offset = {"other_flag": 1, "mask": 4, "sid": -1, "critical": 1, "deny": 4}[change]
        changed[offset] ^= 1
        planned[index] = changed.hex()
        acl = acl[:8] + b"".join(bytes.fromhex(item) for item in planned)
    def altered_plan(snapshot: dict[str, object], package: str, user: str, *,
                     deny_target: str = "package") -> tuple[bytes, list[str]]:
        assert snapshot == binding.before and (package, user, deny_target) == (PACKAGE, USER, "package")
        return acl, planned

    monkeypatch.setattr(diagnostic, "_plan", altered_plan)
    monkeypatch.setattr(diagnostic, "_snapshot", lambda *_args: copy.deepcopy(binding.before))
    with pytest.raises(ValueError, match="acl_plan_"):
        diagnostic._apply(binding)
    assert api.set_calls == 0 and binding.dacl_audit["set_attempted"] is False
    assert binding.dacl_audit["failed_at"] == "input_planning"
    assert binding.session.cleanup_state()["evidence_unknown"] is True
    assert not binding.session.close_resources()


class _HelperApi(_Api):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []
        self.revert_ok = True
        self.fail_stage: str | None = None

    def duplicate_token(self, _token: int, access: int, _security: object, level: int, kind: int, output: Any) -> bool:
        assert (access, level, kind) == (0xC, 2, 2)
        C.cast(output, C.POINTER(W.HANDLE)).contents.value = 14
        if self.fail_stage == "duplicate":
            raise OSError("partial duplicate")
        return True

    def impersonate(self, _token: Any) -> bool:
        self.events.append("impersonate")
        if self.fail_stage == "impersonate":
            raise OSError("partial impersonate")
        return True

    def current_thread(self) -> int:
        return -2

    def open_thread_token(self, _thread: int, _access: int, _as_self: bool, output: Any) -> bool:
        C.cast(output, C.POINTER(W.HANDLE)).contents.value = 15
        return True

    def get_registry(self, access: int, output: Any) -> int:
        assert access == 0x60000
        C.cast(output, C.POINTER(W.HKEY)).contents.value = 16
        return 0

    def open_key(self, _location: Any, name: object, _options: int, access: int, output: Any) -> int:
        assert name is None and access == 0x60000
        C.cast(output, C.POINTER(W.HKEY)).contents.value = 17
        if self.fail_stage == "export":
            raise OSError("partial export")
        return 0

    def revert(self) -> bool:
        self.events.append("revert")
        return self.revert_ok

    def reg_close(self, handle: Any) -> int:
        self.events.append("close")
        if self.fail_stage == "close" and handle.value == 17:
            self.closed.append(17)
            raise OSError("close failed")
        return super().reg_close(handle)


def _prepare_helper(monkeypatch: pytest.MonkeyPatch, api: _HelperApi) -> list[dict[str, object]]:
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(diagnostic, "WriteApi", lambda: api)
    monkeypatch.setattr(readonly, "_token_metadata", lambda _api, token: _identity(1 if token == 11 else 2))
    monkeypatch.setattr(diagnostic, "_snapshot", lambda *_args: _snapshot())
    monkeypatch.setattr(readonly, "_emit_helper", lambda value: emitted.append(dict(value)))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(("ACK " + NONCE + "\n").encode())))
    return emitted


def test_helper_ready_only_after_revert_and_final_after_all_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _HelperApi()
    emitted = _prepare_helper(monkeypatch, api)
    assert diagnostic._binding_helper(11, PACKAGE, USER, NONCE) == 0
    assert [value["status"] for value in emitted] == ["binding_ready", "binding_helper_complete"]
    assert emitted[0]["revert_confirmed"] is True
    assert emitted[1]["close_errors"] == []
    assert api.events.index("revert") < api.events.index("close")
    assert api.closed == [17, 16, 15, 14, 11]


@pytest.mark.parametrize("stage,expected", [("duplicate", [14, 11]), ("impersonate", [14, 11]),
    ("export", [17, 16, 15, 14, 11]), ("close", [17, 16, 15, 14, 11])])
def test_helper_partial_success_closes_every_owned_slot(monkeypatch: pytest.MonkeyPatch, stage: str, expected: list[int]) -> None:
    api = _HelperApi()
    api.fail_stage = stage
    emitted = _prepare_helper(monkeypatch, api)
    assert diagnostic._binding_helper(11, PACKAGE, USER, NONCE) == 2
    assert api.closed == expected
    assert ("revert" in api.events) is (stage != "duplicate")
    if stage == "close":
        assert emitted[-1]["status"] == "binding_helper_close_failed"
        assert emitted[-1]["close_errors"] == ["exported_key"]


class _Exit(BaseException):
    pass


def test_revert_failure_exits_without_any_further_key_or_token_work(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _HelperApi()
    api.revert_ok = False
    emitted = _prepare_helper(monkeypatch, api)

    def exit_process(code: int) -> NoReturn:
        assert code == 77
        raise _Exit()

    monkeypatch.setattr(os, "_exit", exit_process)
    with pytest.raises(_Exit):
        diagnostic._binding_helper(11, PACKAGE, USER, NONCE)
    assert api.events[-1] == "revert" and api.closed == []
    assert len(emitted) == 1 and emitted[0]["status"] == "fatal_revert_failed"


class _Process:
    pid = 900
    returncode = 0

    def __init__(self, ready: dict[str, object]) -> None:
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.final = ready | {"status": "binding_helper_complete", "close_errors": []}

    def communicate(self, timeout: int) -> tuple[bytes, bytes]:
        assert timeout == 10
        return json.dumps(self.final).encode(), b""

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: int) -> int:
        assert timeout == 3
        return self.returncode

    def kill(self) -> None:
        self.returncode = 77


def test_helper_cleanup_reports_reap_and_streams_without_claiming_popen_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session(monkeypatch, _Api())
    process = _Process({})
    owner = diagnostic.BindingHelperOwner(cast(subprocess.Popen[bytes], process), session.api)
    session.helpers.append(owner)
    assert session.close_resources()
    state = session.cleanup_state()
    record = cast(list[dict[str, object]], state["resources"])[0]
    assert record["close_call_completed"] is True and record["reaped"] is True
    assert record["streams_closed"] == {"stdin": True, "stdout": True, "stderr": True}
    assert record["close_scope"] == "helper_reap_and_streams_only" and record["closed"] is None
    assert state["popen_own_process_handle_verified"] is False


def _prepare_bind(monkeypatch: pytest.MonkeyPatch, api: _Api) -> tuple[diagnostic.Session, _Process]:
    session = _session(monkeypatch, api)
    ready = {"status": "binding_ready", "nonce": NONCE, "revert_confirmed": True,
             "primary_token": _identity(), "effective_thread_token": _identity(2),
             "exported_key": 99, "snapshot": _snapshot()}
    process = _Process(ready)
    monkeypatch.setattr(diagnostic, "uuid4", lambda: SimpleNamespace(hex=NONCE))
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(os, "set_handle_inheritable", lambda *_args: None)
    monkeypatch.setattr(readonly, "_token_data", lambda *_args: None)
    monkeypatch.setattr(readonly, "_token_sid", lambda *_args: USER)
    monkeypatch.setattr(readonly, "_token_metadata", lambda *_args: _identity())
    monkeypatch.setattr(diagnostic, "_snapshot", lambda *_args: _snapshot())
    monkeypatch.setattr(diagnostic, "_ready", lambda *_args: ready)
    delivered = False

    def chunk(_api: object, stream: object) -> bytes:
        nonlocal delivered
        if stream is process.stdout and not delivered:
            delivered = True
            return json.dumps(process.final).encode()
        return b""

    monkeypatch.setattr(diagnostic, "_pipe_chunk", chunk)
    return session, process


@pytest.mark.parametrize("failure", ["duplicate_false", "duplicate_raise", "nonce", "revert", "close"])
def test_outer_retains_partial_duplicate_and_rejects_unconfirmed_final(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    api = _Api()
    session, process = _prepare_bind(monkeypatch, api)
    if failure == "duplicate_false":
        api.duplicate_ok = False
    elif failure == "duplicate_raise":
        api.duplicate_raise = True
    elif failure == "nonce":
        process.final["nonce"] = "wrong"
    elif failure == "revert":
        process.final["revert_confirmed"] = False
    else:
        process.final["close_errors"] = ["exported_key"]
    with pytest.raises((ValueError, RuntimeError, OSError)):
        diagnostic._bind_from_worker(session, 500, {})
    assert session.unknown and session.root is not None
    assert session.root.owner.handle.value == 71
    # Avoid any real process work during close in this pure mock.
    session.helpers[0].reaped = True
    assert not session.close_resources()
    assert 71 in api.closed


def _frame(phase: str) -> dict[str, object]:
    checks: dict[str, object] = {}
    for name, mask in (("set", 2), ("create", 4), ("delete", 0x10000), ("wd", 0x40000), ("wo", 0x80000)):
        checks["root_" + name] = {"api": "GetAppContainerRegistryLocation", "access": mask,
                                   "status": 0 if phase == "default" else 0x80070005}
    for name, mask in (("set", 2), ("wd", 0x40000), ("wo", 0x80000)):
        checks["seeded_" + name] = {"api": "RegOpenKeyExW_seeded", "access": mask,
                                     "status": 0 if phase == "default" else 5, "set_status": 0}
    checks["seeded_delete"] = {"api": "RegDeleteKeyW_seeded", "status": 0 if phase == "default" else 5}
    checks["readonly_parent_create"] = {"api": "RegCreateKeyExW_read_control_parent",
        "status": 0 if phase == "default" else 5, "disposition": 1 if phase == "default" else 0, "set_status": 0}
    return {"kind": "registry_acl_worker", "phase": phase, "identity": _identity(), "close_errors": [],
            "root_read_control_hresult": 0, "root_read_control_precedes_permission_calls": True,
            "root_sd_before": _sd_observation("before_permission_calls", 1),
            "root_sd_after": _sd_observation("after_permission_calls", 2), "checks": checks}


@pytest.mark.parametrize("phase", ["default", "readonly"])
def test_worker_requires_all_real_api_positive_or_denial_observations(phase: str) -> None:
    diagnostic._worker_gate(_frame(phase), PROFILE, USER, phase)


@pytest.mark.parametrize("label", ["root_set", "root_create", "root_delete", "root_wd", "root_wo",
    "seeded_set", "seeded_wd", "seeded_wo", "seeded_delete", "readonly_parent_create"])
def test_user_deny_worker_still_requires_each_real_denial(
    monkeypatch: pytest.MonkeyPatch, label: str,
) -> None:
    binding = _binding(monkeypatch, _Api(), deny_target="user")
    frame = _frame("readonly")
    cast(dict[str, Any], frame["checks"])[label]["status"] = 0
    result = SimpleNamespace(exit_code=0, frames=(frame,), stdout=b"", stderr=b"")

    class Worker:
        def __enter__(self) -> Worker:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def collect(self, timeout: int) -> Any:
            assert timeout == 15
            return result

    monkeypatch.setattr(diagnostic, "verify_manifest", lambda _manifest: None)
    monkeypatch.setattr(diagnostic, "WindowsLauncher", lambda: SimpleNamespace(launch=lambda _spec: Worker()))
    monkeypatch.setattr(diagnostic, "asdict", lambda _result: {"frames": [frame]})
    monkeypatch.setattr(diagnostic, "_snapshot", lambda *_args: _snapshot())
    manifest = RuntimeManifest("C:/synthetic/runtime", "C:/synthetic/runtime/python.exe", "test", "C:/synthetic/python.exe", ())
    report: dict[str, object] = {}
    with pytest.raises(ValueError, match="acl_worker_positive_or_denial_gate_failed:" + label):
        diagnostic._worker(binding.session, manifest, {"SystemRoot": "C:/Windows", "WINDIR": "C:/Windows"},
                           {"phase": "readonly"}, report)
    assert cast(dict[str, Any], report["readonly_worker"])["frames"] == [frame]
    assert binding.session.cleanup_state()["evidence_unknown"] is True
    assert not binding.session.close_resources()


@pytest.mark.parametrize("failure", ["timeout", "unknown_error", "wrong_api", "wrong_mask", "missing", "close", "existing"])
def test_worker_gate_cannot_turn_incomplete_evidence_into_denial(failure: str) -> None:
    frame = _frame("readonly")
    checks = cast(dict[str, Any], frame["checks"])
    if failure == "timeout":
        checks["root_set"]["status"] = 258
    elif failure == "unknown_error":
        checks["seeded_set"]["status"] = 87
    elif failure == "wrong_api":
        checks["seeded_set"]["api"] = "cached"
    elif failure == "wrong_mask":
        checks["seeded_wd"]["access"] = 8
    elif failure == "missing":
        del checks["seeded_wo"]
    elif failure == "close":
        frame["close_errors"] = ["key"]
    else:
        frame["existing_object_refused"] = True
    with pytest.raises(ValueError):
        diagnostic._worker_gate(frame, PROFILE, USER, "readonly")


@pytest.mark.parametrize("failure", ["before_missing", "after_unknown", "stage", "call_index",
    "order", "ace_budget", "release", "control"])
def test_worker_gate_rejects_incomplete_sd_evidence(failure: str) -> None:
    frame = _frame("readonly")
    after = cast(dict[str, Any], frame["root_sd_after"])
    if failure == "before_missing":
        del frame["root_sd_before"]
    elif failure in ("after_unknown", "release"):
        after.update(status="unknown", release_error="synthetic failure")
    elif failure == "stage":
        after["stage"] = "before_permission_calls"
    elif failure == "call_index":
        after["sd_call_index"] = 1
    elif failure == "order":
        frame["root_read_control_precedes_permission_calls"] = False
    elif failure == "ace_budget":
        after["snapshot"]["raw_aces_hex"] *= 33
    else:
        after["snapshot"]["dacl_protected"] = False
    with pytest.raises(ValueError, match="acl_worker_sd_"):
        diagnostic._worker_gate(frame, PROFILE, USER, "readonly")


@pytest.mark.parametrize("operation_failure", [False, True])
def test_worker_operations_snapshot_same_rc_key_before_root_calls_and_in_finally(operation_failure: bool) -> None:
    nodes = _worker_ast_nodes()
    start = next(index for index, node in enumerate(nodes) if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "root" for target in node.targets))
    end = next(index for index, node in enumerate(nodes) if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == "payload" for target in node.targets))
    events: list[tuple[str, int]] = []
    out: dict[str, Any] = {"checks": {}, "close_errors": []}

    def getroot(access: int, output: Any) -> int:
        events.append(("getroot", access))
        C.cast(output, C.POINTER(W.HKEY)).contents.value = 100 if access == 0x20000 else 200 + access
        return 0

    def observe(root: Any, stage: str, call_index: int) -> dict[str, object]:
        events.append((stage, int(root.value)))
        return _sd_observation(stage, call_index)

    def create(root: Any, *_args: Any) -> int:
        events.append(("create", int(root.value)))
        C.cast(_args[-2], C.POINTER(W.HKEY)).contents.value = 300
        C.cast(_args[-1], C.POINTER(W.DWORD)).contents.value = 1
        return 0

    def setvalue(*_args: Any) -> int:
        if operation_failure:
            raise OSError("synthetic canary operation failure")
        return 0

    def openkey(root: Any, *_args: Any) -> int:
        events.append(("open", int(root.value)))
        C.cast(_args[-1], C.POINTER(W.HKEY)).contents.value = 301
        return 0

    def delete(root: Any, _name: str) -> int:
        events.append(("delete", int(root.value)))
        return 0

    namespace: dict[str, Any] = {"W": W, "C": C, "out": out, "getroot": getroot,
        "observe_sd": observe, "create": create, "setvalue": setvalue, "openkey": openkey,
        "delete": delete, "closekey": lambda key: events.append(("close", int(key.value or 0))),
        "case": {"new_child": "synthetic", "seeded": {key: "synthetic" for key in ("set", "wd", "wo", "delete")}}}
    code = compile(ast.Module(body=nodes[start:end], type_ignores=[]), "<mock-worker-operations>", "exec")
    if operation_failure:
        with pytest.raises(OSError, match="synthetic canary"):
            exec(code, namespace)
    else:
        exec(code, namespace)
        assert len(out["checks"]) == 10
        assert out["checks"]["readonly_parent_create"]["disposition"] == 1
    assert events[:3] == [("getroot", 0x20000), ("before_permission_calls", 100), ("getroot", 2)]
    assert events[-2:] == [("after_permission_calls", 100), ("close", 100)]
    assert out["root_sd_before"]["sd_call_index"] == 1 and out["root_sd_after"]["sd_call_index"] == 2
    assert all(handle == 100 for name, handle in events if name in ("create", "open", "delete"))


def test_worker_frame_budget_remains_16k_with_sd_observations() -> None:
    nodes = _worker_ast_nodes()
    payload_node = next(node for node in nodes if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "payload" for target in node.targets))
    budget_node = nodes[nodes.index(payload_node) + 1]
    code = compile(ast.Module(body=[payload_node, budget_node], type_ignores=[]), "<mock-worker-budget>", "exec")
    raw = struct.pack("<BBH", 0, 0, 64) + b"\0" * 60
    frame = _frame("readonly")
    for key in ("root_sd_before", "root_sd_after"):
        cast(dict[str, Any], frame[key])["snapshot"] = diagnostic._parse_worker_sd(_worker_sd_bytes([raw] * 32))
    namespace: dict[str, Any] = {"json": json, "out": frame}
    exec(code, namespace)
    assert len(namespace["payload"]) <= 16384
    frame["oversized"] = "x" * 16384
    with pytest.raises(ValueError, match="worker output budget"):
        exec(code, namespace)


def test_worker_collision_keeps_profile_after_confirmed_worker_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    binding = _binding(monkeypatch, _Api())
    frame = _frame("default")
    frame["existing_object_refused"] = True
    result = SimpleNamespace(exit_code=0, frames=(frame,), stdout=b"", stderr=b"")

    class Worker:
        def __enter__(self) -> Worker:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def collect(self, _timeout: int) -> Any:
            return result

    monkeypatch.setattr(diagnostic, "verify_manifest", lambda _manifest: None)
    monkeypatch.setattr(diagnostic, "WindowsLauncher", lambda: SimpleNamespace(launch=lambda _spec: Worker()))
    monkeypatch.setattr(diagnostic, "asdict", lambda _result: {})
    monkeypatch.setattr(diagnostic, "_snapshot", lambda *_args: _snapshot())
    manifest = RuntimeManifest("C:/synthetic/runtime", "C:/synthetic/runtime/python.exe", "test", "C:/synthetic/python.exe", ())
    with pytest.raises(ValueError, match="observation_incomplete"):
        diagnostic._worker(binding.session, manifest, {"SystemRoot": "C:/Windows", "WINDIR": "C:/Windows"}, {"phase": "default"}, {})
    assert binding.session.unknown
    assert not binding.session.close_resources()


@pytest.mark.parametrize("failure", ["none", "host_query", "worker_gate"])
def test_host_post_worker_snapshot_saved_before_gate_even_on_failure(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    binding = _binding(monkeypatch, _Api())
    frame = _frame("readonly")
    if failure == "worker_gate":
        cast(dict[str, Any], frame["checks"])["root_set"]["status"] = 0
    result = SimpleNamespace(exit_code=0, frames=(frame,), stdout=b"", stderr=b"")
    events: list[str] = []
    report: dict[str, object] = {"dacl": {"after": {"apply_snapshot": "retained"}}}

    class Worker:
        def __enter__(self) -> Worker:
            return self

        def __exit__(self, *_args: object) -> None:
            events.append("context_exit")

        def collect(self, _timeout: int) -> Any:
            events.append("collect")
            return result

    def snapshot(api: object, handle: Any, package: str, user: str) -> dict[str, object]:
        assert events == ["collect", "context_exit"] and "readonly_worker" in report
        assert api is binding.session.api and handle is binding.owner.handle
        assert (package, user) == (PACKAGE, USER)
        events.append("host_snapshot")
        if failure == "host_query":
            raise OSError("synthetic host snapshot failure")
        return _snapshot(inherited=True)

    gate = diagnostic._worker_gate

    def check(*args: Any) -> None:
        assert events[-1] == "host_snapshot"
        events.append("gate")
        gate(*args)

    monkeypatch.setattr(diagnostic, "verify_manifest", lambda _manifest: None)
    monkeypatch.setattr(diagnostic, "WindowsLauncher", lambda: SimpleNamespace(launch=lambda _spec: Worker()))
    monkeypatch.setattr(diagnostic, "asdict", lambda _result: {"frames": [frame]})
    monkeypatch.setattr(diagnostic, "_snapshot", snapshot)
    monkeypatch.setattr(diagnostic, "_worker_gate", check)
    manifest = RuntimeManifest("C:/synthetic/runtime", "C:/synthetic/runtime/python.exe", "test", "C:/synthetic/python.exe", ())
    environment = {"SystemRoot": "C:/Windows", "WINDIR": "C:/Windows"}
    if failure == "none":
        diagnostic._worker(binding.session, manifest, environment, {"phase": "readonly"}, report)
    else:
        with pytest.raises((ValueError, OSError)):
            diagnostic._worker(binding.session, manifest, environment, {"phase": "readonly"}, report)
        assert binding.session.cleanup_state()["evidence_unknown"] is True
    saved = json.loads(json.dumps(report))
    assert saved["dacl"] == {"after": {"apply_snapshot": "retained"}}
    assert saved["readonly_worker"]["frames"] == [frame]
    observed = saved["readonly_worker"]["host_bound_root_after_worker"]
    assert observed["status"] == ("unknown" if failure == "host_query" else "observed")
    if failure == "host_query":
        assert observed["error_type"] == "OSError" and events[-1] == "host_snapshot"
    else:
        assert observed["snapshot"] == _snapshot(inherited=True) and events[-1] == "gate"
    assert binding.session.close_resources() is (failure == "none")


def test_worker_cleanup_owner_is_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    binding = _binding(monkeypatch, _Api())
    owner = SimpleNamespace(close=lambda: None)

    def launch(_spec: object) -> NoReturn:
        raise LaunchRejected("unknown_reap", cleanup_owner=owner)

    monkeypatch.setattr(diagnostic, "verify_manifest", lambda _manifest: None)
    monkeypatch.setattr(diagnostic, "WindowsLauncher", lambda: SimpleNamespace(launch=launch))
    manifest = RuntimeManifest("C:/synthetic/runtime", "C:/synthetic/runtime/python.exe", "test", "C:/synthetic/python.exe", ())
    with pytest.raises(LaunchRejected):
        diagnostic._worker(binding.session, manifest, {"SystemRoot": "C:/Windows", "WINDIR": "C:/Windows"}, {"phase": "readonly"}, {})
    assert binding.session.owners == [owner] and binding.session.unknown


def test_unknown_cleanup_withholds_profile_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _Api()
    session = _session(monkeypatch, api)
    session.unknown = True
    monkeypatch.setattr(diagnostic, "WriteApi", lambda: api)
    def selected_session(selected_api: object, *, deny_target: str = "package",
                         restore_for_cleanup: bool = False) -> diagnostic.Session:
        assert selected_api is api and deny_target == "package" and restore_for_cleanup is False
        return session

    monkeypatch.setattr(diagnostic, "Session", selected_session)
    monkeypatch.setattr(session, "create", lambda: PROFILE)

    def stop(_path: Path) -> NoReturn:
        raise ValueError("stop before any filesystem/native work")

    monkeypatch.setattr(readonly, "_reject_reparse", stop)
    report: dict[str, object] = {}
    assert diagnostic._experiment(io.StringIO(), report, Path("C:/synthetic/python.exe"), Path("C:/synthetic/runtime")) == 2
    assert report["cleanup"] == "withheld_unknown_helper_worker_key_or_binding"


@pytest.mark.parametrize("deny_target", ["package", "user"])
@pytest.mark.parametrize("failure,close_failure", [
    ("readback_mismatch", False), ("readback_query", False), ("set_raise", False),
    ("set_status", False), ("readback_mismatch", True),
])
def test_experiment_finally_saves_failed_dacl_audit_and_withholds_negative_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str, close_failure: bool, deny_target: str,
) -> None:
    # Only disposable synthetic files are copied. Every native/profile/runtime
    # boundary is replaced; the autouse guard still forbids unmocked calls.
    api = _Api()
    profile_dir = tmp_path / "synthetic-profile"
    profile_dir.mkdir()
    profile = ProbeProfile(PROFILE.name, PACKAGE, str(profile_dir))
    monkeypatch.setattr(diagnostic, "create_probe_profile", lambda _name: profile)
    session = diagnostic.Session(cast(diagnostic.WriteApi, api), deny_target=deny_target)

    def selected_session(selected_api: object, *, deny_target: str = "package",
                         restore_for_cleanup: bool = False) -> diagnostic.Session:
        assert selected_api is api and deny_target == session.deny_target and restore_for_cleanup is False
        return session

    monkeypatch.setattr(diagnostic, "Session", selected_session)
    monkeypatch.setattr(diagnostic, "WriteApi", lambda: api)
    monkeypatch.setattr(diagnostic, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(readonly, "_reject_reparse", lambda _path: None)
    fixture = tmp_path / "synthetic-fixture.exe"
    fixture.write_bytes(b"mock fixture; never executed")
    monkeypatch.setattr(readonly, "FIXTURE", fixture)
    monkeypatch.setattr(readonly, "FIXTURE_SHA256", sha256(fixture.read_bytes()).hexdigest())
    monkeypatch.setattr(readonly, "SuspendedRegistryApi", lambda _observe: object())

    def suspended_launch(_spec: object, *, _api: object) -> NoReturn:
        binding = diagnostic.BoundRoot(session)
        binding.owner.handle.value, binding.user = 50, USER
        binding.before, binding.validated = _snapshot(), True
        raise LaunchRejected(readonly.STOP_STAGE)

    monkeypatch.setattr(diagnostic, "_create_worker", suspended_launch)
    runtime = tmp_path / "synthetic-runtime"
    manifest = RuntimeManifest(str(runtime), str(runtime / "python.exe"), "test", str(fixture), ())
    monkeypatch.setattr(diagnostic, "stage_runtime", lambda *_args, **_kwargs: manifest)
    monkeypatch.setattr(diagnostic, "verify_manifest", lambda _manifest: None)
    monkeypatch.setattr(diagnostic, "apply_readonly_dacl", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(diagnostic, "_seed", lambda *_args: [])
    phases: list[str] = []

    def worker(_session: object, _manifest: object, _environment: object,
               case: dict[str, object], report: dict[str, object]) -> None:
        phases.append(str(case["phase"]))
        assert case["phase"] == "default", "negative worker must remain gated"
        report["default_worker"] = {"synthetic_positive": True}

    monkeypatch.setattr(diagnostic, "_worker", worker)
    queries = 0

    def snapshot(*_args: object) -> dict[str, object]:
        nonlocal queries
        queries += 1
        if queries == 2 and failure == "readback_query":
            raise OSError("synthetic readback query failure")
        result = _snapshot()
        if queries == 2:
            cast(dict[str, object], result["metadata"])["dacl_protected"] = True
        return result

    def set_security(*_args: object) -> int:
        api.set_calls += 1
        if failure == "set_raise":
            raise OSError("synthetic set wrapper failure")
        return 5 if failure == "set_status" else 0

    monkeypatch.setattr(diagnostic, "_snapshot", snapshot)
    monkeypatch.setattr(api, "set_security", set_security)
    if close_failure:
        api.close_fail.add(50)
    report: dict[str, object] = {}
    output = io.StringIO()
    assert diagnostic._experiment(output, report, fixture, runtime, deny_target=deny_target) == 2
    saved = json.loads(output.getvalue())
    assert saved == report and phases == ["default"]
    assert saved["status"] == "acl_experiment_blocked"
    assert saved["cleanup"] == "withheld_unknown_helper_worker_key_or_binding"
    assert saved["deny_target"] == deny_target and saved["denied_sid"] == (USER if deny_target == "user" else PACKAGE)
    assert "readonly_worker" not in saved
    audit = saved["dacl"]
    _, expected = diagnostic._plan(_snapshot(), PACKAGE, USER, deny_target=deny_target)
    assert audit["state"] == "failed" and audit["readback_gate_passed"] is False
    assert audit["before"] == _snapshot() and audit["expected_raw_aces_hex"] == expected
    assert audit["expected_readback"] == {"owner_sid": USER, "dacl_protected": True,
        "dacl_null": False, "acl_revision": 2, "raw_aces_hex": expected}
    assert audit["set_attempted"] is True
    assert audit["set_returned"] is (failure != "set_raise")
    if failure == "readback_mismatch":
        assert audit["after"]["raw_aces_hex"] != expected
        assert audit["actual_snapshot_obtained"] is True
        assert audit["gate_checks"] == {"owner_matches": True, "dacl_protected": True,
            "dacl_nonnull": True, "acl_revision_matches": True, "raw_aces_match": False}
    else:
        assert audit["actual_snapshot_obtained"] is False and "after" not in audit
    assert saved["cleanup_state"]["evidence_unknown"] is True
    assert saved["cleanup_state"]["resource_close_unconfirmed"] is close_failure
    assert saved["cleanup_state"]["resources"][0]["closed"] is (not close_failure)
    assert api.closed == [50] and api.set_calls == 1


def test_user_deny_profile_delete_failure_is_reported_without_acl_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    api = _Api()
    profile_dir = tmp_path / "synthetic-profile"
    profile_dir.mkdir()
    profile = ProbeProfile(PROFILE.name, PACKAGE, str(profile_dir))
    monkeypatch.setattr(diagnostic, "create_probe_profile", lambda _name: profile)
    session = diagnostic.Session(cast(diagnostic.WriteApi, api), deny_target="user")

    def selected_session(selected_api: object, *, deny_target: str = "package",
                         restore_for_cleanup: bool = False) -> diagnostic.Session:
        assert selected_api is api and deny_target == "user" and restore_for_cleanup is False
        return session

    monkeypatch.setattr(diagnostic, "Session", selected_session)
    monkeypatch.setattr(diagnostic, "WriteApi", lambda: api)
    monkeypatch.setattr(diagnostic, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(readonly, "_reject_reparse", lambda _path: None)
    fixture = tmp_path / "synthetic-fixture.exe"
    fixture.write_bytes(b"mock fixture; never executed")
    monkeypatch.setattr(readonly, "FIXTURE", fixture)
    monkeypatch.setattr(readonly, "FIXTURE_SHA256", sha256(fixture.read_bytes()).hexdigest())
    monkeypatch.setattr(readonly, "SuspendedRegistryApi", lambda _observe: object())

    def suspended_launch(_spec: object, *, _api: object) -> NoReturn:
        binding = diagnostic.BoundRoot(session)
        binding.owner.handle.value, binding.user = 50, USER
        binding.before, binding.validated = _snapshot(inherited=True), True
        raise LaunchRejected(readonly.STOP_STAGE)

    monkeypatch.setattr(diagnostic, "_create_worker", suspended_launch)
    runtime = tmp_path / "synthetic-runtime"
    manifest = RuntimeManifest(str(runtime), str(runtime / "python.exe"), "test", str(fixture), ())
    monkeypatch.setattr(diagnostic, "stage_runtime", lambda *_args, **_kwargs: manifest)
    monkeypatch.setattr(diagnostic, "verify_manifest", lambda _manifest: None)
    monkeypatch.setattr(diagnostic, "apply_readonly_dacl", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(diagnostic, "_seed", lambda *_args: [])
    phases: list[str] = []

    def worker(selected: diagnostic.Session, _manifest: object, _environment: object,
               case: dict[str, object], _report: dict[str, object]) -> None:
        assert selected is session and selected.deny_target == "user" and selected.root is not None
        phase = str(case["phase"])
        assert selected.root.dacl_audit["readback_gate_passed"] is (phase == "readonly")
        phases.append(phase)

    monkeypatch.setattr(diagnostic, "_worker", worker)
    before = _snapshot(inherited=True)
    expected_acl, planned = diagnostic._plan(before, PACKAGE, USER, deny_target="user")
    after = copy.deepcopy(before)
    after["raw_aces_hex"] = planned
    cast(dict[str, object], after["metadata"])["dacl_protected"] = True
    snapshots = iter((before, after))
    monkeypatch.setattr(diagnostic, "_snapshot", lambda *_args: next(snapshots))

    def set_security(*args: Any) -> int:
        root = session.root
        assert root is not None and args[0] is root.owner.handle and args[1:5] == (4, 0x80000004, None, None)
        assert C.string_at(args[5], len(expected_acl)) == expected_acl and args[6] is None
        api.set_calls += 1
        return 0

    monkeypatch.setattr(api, "set_security", set_security)
    deleted: list[ProbeProfile] = []

    def delete(selected: ProbeProfile) -> NoReturn:
        assert selected is profile and session.closed and api.closed == [50]
        deleted.append(selected)
        raise OSError("synthetic profile delete denied")

    monkeypatch.setattr(diagnostic, "delete_probe_profile", delete)
    report: dict[str, object] = {"stage0_passed": False}
    output = io.StringIO()
    assert diagnostic._experiment(output, report, fixture, runtime, deny_target="user") == 2
    saved = json.loads(output.getvalue())
    assert saved == report and phases == ["default", "readonly"]
    assert deleted == [profile] and api.set_calls == 1 and profile_dir.exists()
    assert saved["deny_target"] == "user" and saved["denied_sid"] == USER
    assert saved["dacl"]["deny_target"] == "user" and saved["dacl"]["denied_sid"] == USER
    assert saved["dacl"]["before"] == before and saved["dacl"]["after"] == after
    assert saved["dacl"]["expected_raw_aces_hex"] == planned
    assert saved["dacl"]["readback_gate_passed"] is True
    assert saved["cleanup"] == "failed_profile_may_remain"
    assert saved["cleanup_error"] == "synthetic profile delete denied" and saved["stage0_passed"] is False


def test_without_opt_in_never_constructs_native_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    monkeypatch.setattr(readonly, "_new_report", lambda _path: report)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", str(report), "--python", "C:/synthetic/python.exe"])
    assert diagnostic.main() == 0
    result = json.loads(report.read_text(encoding="ascii"))
    assert result["status"] == "not_executed_explicit_opt_in_required"
    assert result["stage0_passed"] is False and result["binding_verified"] is False
    assert result["deny_target"] == "package" and result["denied_sid"] is None


def test_user_selection_without_opt_in_is_reported_without_native(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    report = tmp_path / "user-report.json"
    monkeypatch.setattr(readonly, "_new_report", lambda _path: report)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", str(report), "--python", "C:/synthetic/python.exe",
                                    "--deny-target", "user"])
    assert diagnostic.main() == 0
    saved = json.loads(report.read_text(encoding="ascii"))
    assert saved["deny_target"] == "user" and saved["denied_sid"] is None
    assert saved["status"] == "not_executed_explicit_opt_in_required"
    assert saved["stage0_passed"] is False and saved["binding_verified"] is False


@pytest.mark.parametrize("selection", [None, "package", "user"])
@pytest.mark.parametrize("failure", ["experiment", "runtime_exists"])
def test_main_forwards_deny_selection_and_retains_it_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, selection: str | None, failure: str,
) -> None:
    report = tmp_path / "report.json"
    runtime = tmp_path / "report-runtime"
    if failure == "runtime_exists":
        runtime.mkdir()
    selected = selection or "package"
    monkeypatch.setattr(readonly, "_new_report", lambda _path: report)
    monkeypatch.setattr(readonly, "_fingerprints", lambda: {"synthetic": "unchanged"})
    calls: list[str] = []

    def experiment(output: Any, data: dict[str, object], python: Path, runtime_path: Path, *,
                   deny_target: str, restore_for_cleanup: bool = False) -> NoReturn:
        assert python == Path("C:/synthetic/python.exe") and runtime_path == runtime
        assert data["deny_target"] == deny_target == selected and data["denied_sid"] is None
        assert restore_for_cleanup is False
        assert cast(dict[str, object], data["cleanup_dacl_restore"])["enabled"] is False
        assert output.name == str(report)
        calls.append(deny_target)
        raise ValueError("synthetic experiment failure before native")

    monkeypatch.setattr(diagnostic, "_experiment", experiment)
    args = ["diagnostic", "--report", str(report), "--python", "C:/synthetic/python.exe", "--experiment"]
    if selection is not None:
        args.extend(("--deny-target", selection))
    monkeypatch.setattr(sys, "argv", args)
    assert diagnostic.main() == 2
    saved = json.loads(report.read_text(encoding="ascii"))
    assert calls == ([selected] if failure == "experiment" else [])
    assert saved["deny_target"] == selected and saved["denied_sid"] is None
    assert saved["status"] == "acl_experiment_blocked" and saved["stage0_passed"] is False
    assert saved["source_unchanged"] is True


@pytest.mark.parametrize("selection", [USER, "S-1-5-18", "C:/arbitrary/key", "0xF003F", "both"])
def test_cli_rejects_arbitrary_deny_selection_before_report_or_native(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, selection: str,
) -> None:
    report = tmp_path / "invalid.json"
    monkeypatch.setattr(readonly, "_new_report", _forbidden)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", str(report), "--python", "C:/synthetic/python.exe",
                                    "--experiment", "--deny-target", selection])
    with pytest.raises(SystemExit) as error:
        diagnostic.main()
    assert error.value.code == 2 and not report.exists()


@pytest.mark.parametrize("phase,stream_name", [(True, "stdout"), (True, "stderr"), (False, "stdout"), (False, "stderr")])
def test_helper_pump_bounds_both_streams_before_and_after_ack(monkeypatch: pytest.MonkeyPatch, phase: bool, stream_name: str) -> None:
    process = _Process({})
    owner = diagnostic.BindingHelperOwner(cast(subprocess.Popen[bytes], process), cast(diagnostic.WriteApi, _Api()))
    limit = diagnostic.MAX_MESSAGE if stream_name == "stdout" else 4096
    reads = 0

    def chunk(_api: object, stream: object) -> bytes:
        nonlocal reads
        if stream is getattr(process, stream_name):
            reads += 1
            return b"x" * 4096
        return b""

    monkeypatch.setattr(diagnostic, "_pipe_chunk", chunk)
    with pytest.raises(ValueError, match="output_budget"):
        owner.pump(ready=phase)
    assert reads <= limit // 4096 + 1
    owner.close()
    assert owner.reaped and process.stdout.closed and process.stderr.closed


def test_helper_exit_before_ready_is_reaped_and_never_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _Process({})
    owner = diagnostic.BindingHelperOwner(cast(subprocess.Popen[bytes], process), cast(diagnostic.WriteApi, _Api()))
    monkeypatch.setattr(diagnostic, "_pipe_chunk", lambda *_args: b"")
    with pytest.raises(ValueError, match="exited_before_binding"):
        diagnostic._ready(owner.api, owner)
    assert owner.reaped


def test_helper_ready_rejects_second_early_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _Process({})
    owner = diagnostic.BindingHelperOwner(cast(subprocess.Popen[bytes], process), cast(diagnostic.WriteApi, _Api()))
    monkeypatch.setattr(diagnostic, "_pipe_chunk", lambda _api, stream: b"{}\n{}\n" if stream is process.stdout else b"")
    with pytest.raises(ValueError, match="early_second_message"):
        diagnostic._ready(owner.api, owner)


def test_helper_timeout_retains_unconfirmed_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _Process({})
    owner = diagnostic.BindingHelperOwner(cast(subprocess.Popen[bytes], process), cast(diagnostic.WriteApi, _Api()))

    def timeout(*_args: object, **_kwargs: object) -> NoReturn:
        raise TimeoutError("bounded pump timeout")

    def unknown_wait(_timeout: int = 3, **_kwargs: object) -> NoReturn:
        raise subprocess.TimeoutExpired("synthetic", 3)

    monkeypatch.setattr(owner, "pump", timeout)
    monkeypatch.setattr(process, "wait", unknown_wait)
    with pytest.raises(TimeoutError):
        owner.collect()
    assert not owner.reaped and diagnostic._UNCONFIRMED == [owner]


def test_helper_import_works_with_isolated_python_and_no_site() -> None:
    path = Path(diagnostic.__file__).resolve()
    source = ("import runpy; ns=runpy.run_path(" + repr(str(path)) +
              ",run_name='acl_import_contract'); "
              "assert ns['MANAGEMENT_ACCESS']==0x60000; print('isolated-import-only')")
    result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", source],
                            cwd=path.parent.parent, capture_output=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert result.stdout == b"isolated-import-only\n" or result.stdout == b"isolated-import-only\r\n"


def _restore_binding(monkeypatch: pytest.MonkeyPatch, api: _Api, *, protected: bool = False) -> diagnostic.BoundRoot:
    binding = _binding(monkeypatch, api, deny_target="user", restore_for_cleanup=True)
    binding.before = _snapshot(inherited=True)
    cast(dict[str, object], binding.before["metadata"])["dacl_protected"] = protected
    applied = copy.deepcopy(binding.before)
    _, applied["raw_aces_hex"] = diagnostic._plan(binding.before, PACKAGE, USER, deny_target="user")
    cast(dict[str, object], applied["metadata"])["dacl_protected"] = True
    binding.dacl_audit.update(state="verified", readback_gate_passed=True, after=applied)
    binding.session.verified_reaped_phases.update(("default", "readonly"))
    helper = diagnostic.BindingHelperOwner(cast(subprocess.Popen[bytes], _Process({})),
                                            cast(diagnostic.WriteApi, api))
    helper.reaped = True
    binding.session.helpers.append(helper)
    return binding


@pytest.mark.parametrize("deny_target,selection", [("package", True), ("user", 1), ("user", "yes")])
def test_invalid_cleanup_restore_combination_is_rejected_before_native(
    deny_target: str, selection: object,
) -> None:
    restore = cast(bool, selection)  # Exercise runtime rejection, not just annotations.
    with pytest.raises(ValueError, match="acl_cleanup_restore_requires_user_deny"):
        diagnostic.Session(cast(diagnostic.WriteApi, _Api()), deny_target=deny_target, restore_for_cleanup=restore)
    with pytest.raises(ValueError, match="acl_cleanup_restore_requires_user_deny"):
        diagnostic._experiment(io.StringIO(), {}, Path("C:/synthetic/python.exe"), Path("C:/synthetic/runtime"),
                               deny_target=deny_target, restore_for_cleanup=restore)


@pytest.mark.parametrize("guard", ["enabled", "user_deny", "fresh_live_binding",
    "default_worker_verified_reaped", "readonly_worker_verified_reaped", "helpers_present",
    "all_helpers_reaped", "no_unknown", "no_pending_cleanup_owner", "apply_exact_gate_passed",
    "not_previously_invoked"])
def test_cleanup_restore_requires_every_guard_before_snapshot_or_set(
    monkeypatch: pytest.MonkeyPatch, guard: str,
) -> None:
    api = _Api()
    binding = _restore_binding(monkeypatch, api)
    session = binding.session
    if guard == "enabled":
        session.restore_for_cleanup = False
    elif guard == "user_deny":
        session.deny_target = "package"
    elif guard == "fresh_live_binding":
        binding.validated = False
    elif guard in ("default_worker_verified_reaped", "readonly_worker_verified_reaped"):
        session.verified_reaped_phases.remove(guard.split("_")[0])
    elif guard == "helpers_present":
        session.helpers.clear()
    elif guard == "all_helpers_reaped":
        session.helpers[0].reaped = False
    elif guard == "no_unknown":
        session.mark_unknown("synthetic unknown before restore")
    elif guard == "no_pending_cleanup_owner":
        session.owners.append(SimpleNamespace(close=lambda: None))
    elif guard == "apply_exact_gate_passed":
        binding.dacl_audit["readback_gate_passed"] = False
    else:
        session.cleanup_restore_invoked = True
    monkeypatch.setattr(diagnostic, "_snapshot", _forbidden)
    with pytest.raises(ValueError, match="acl_cleanup_restore_guards_failed"):
        diagnostic._restore_for_cleanup(binding)
    audit = session.cleanup_dacl_restore
    assert cast(dict[str, bool], audit["guards"])[guard] is False
    assert audit["state"] == "failed" and audit["failed_at"] == "guard_checks"
    assert audit["set_attempted"] is False and api.set_calls == 0
    assert session.unknown and not session.close_resources()


@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("failure", ["none", "before_query", "before_changed", "set_status", "set_raise",
    "readback_query", "order", "critical", "inherited_flag", "deny_remains", "owner_rights_remains",
    "owner", "protection", "null", "revision", "metadata"])
def test_cleanup_restore_preserves_original_bytes_protection_and_requires_full_exact_readback(
    monkeypatch: pytest.MonkeyPatch, protected: bool, failure: str,
) -> None:
    api = _Api()
    binding = _restore_binding(monkeypatch, api, protected=protected)
    original = copy.deepcopy(binding.before)
    original_apply = copy.deepcopy(binding.dacl_audit)
    current = cast(dict[str, object], copy.deepcopy(binding.dacl_audit["after"]))
    actual = copy.deepcopy(original)
    raw = cast(list[str], actual["raw_aces_hex"])
    metadata = cast(dict[str, object], actual["metadata"])
    if failure == "order":
        raw[0], raw[1] = raw[1], raw[0]
    elif failure in ("critical", "inherited_flag"):
        index = 2 if failure == "critical" else 3
        changed = bytearray.fromhex(raw[index])
        changed[1] ^= 0x20 if failure == "critical" else 0x10
        raw[index] = changed.hex()
    elif failure == "deny_remains":
        raw.insert(0, diagnostic._ace(1, 2, diagnostic.DENY_MASK, USER).hex())
    elif failure == "owner_rights_remains":
        raw.append(diagnostic._ace(0, 2, 0x20000, "S-1-3-4").hex())
    elif failure == "owner":
        metadata["owner_sid"] = "S-1-5-18"
    elif failure == "protection":
        metadata["dacl_protected"] = not protected
    elif failure == "null":
        metadata["dacl_null"] = True
    elif failure == "revision":
        actual["acl_revision"] = 4
    elif failure == "metadata":
        metadata["unexpected_normalization"] = True
    if failure == "before_changed":
        cast(dict[str, object], current["metadata"])["owner_sid"] = "S-1-5-18"
    queries = 0

    def snapshot(api_arg: object, handle: object, package: str, user: str) -> dict[str, object]:
        nonlocal queries
        assert api_arg is api and handle is binding.owner.handle and (package, user) == (PACKAGE, USER)
        queries += 1
        if failure == ("before_query" if queries == 1 else "readback_query"):
            raise OSError("synthetic restore snapshot failure")
        return copy.deepcopy(current if queries == 1 else actual)

    def set_security(*args: Any) -> int:
        api.set_calls += 1
        assert args[0] is binding.owner.handle
        assert args[1:5] == (4, 0x80000004 if protected else 0x20000004, None, None)
        planned = bytes.fromhex(str(binding.session.cleanup_dacl_restore["planned_acl_hex"]))
        assert C.string_at(args[5], len(planned)) == planned and args[6] is None
        assert planned[8:] == b"".join(bytes.fromhex(item) for item in cast(list[str], original["raw_aces_hex"]))
        assert planned[4:6] == struct.pack("<H", len(cast(list[str], original["raw_aces_hex"])))
        if failure == "set_raise":
            raise OSError("synthetic restore wrapper failure")
        return 5 if failure == "set_status" else 0

    monkeypatch.setattr(diagnostic, "_snapshot", snapshot)
    monkeypatch.setattr(api, "set_security", set_security)
    if failure == "none":
        diagnostic._restore_for_cleanup(binding)
    else:
        with pytest.raises((ValueError, OSError)):
            diagnostic._restore_for_cleanup(binding)
    audit = binding.session.cleanup_dacl_restore
    assert binding.before == original and binding.dacl_audit == original_apply
    assert audit["expected"] == original and audit["expected_before_restore"] == original_apply["after"]
    assert audit["readback_gate_passed"] is (failure == "none")
    assert audit["set_attempted"] is (failure not in ("before_query", "before_changed"))
    assert api.set_calls == (0 if failure in ("before_query", "before_changed") else 1)
    assert audit["set_returned"] is (failure not in ("before_query", "before_changed", "set_raise"))
    if failure in ("before_query", "before_changed", "set_status", "set_raise", "readback_query"):
        assert audit["failed_at"] == {"before_query": "before_query", "before_changed": "before_obtained",
            "set_status": "set_returned", "set_raise": "set_call", "readback_query": "readback_query"}[failure]
    if failure == "set_status":
        assert audit["set_security_status"] == 5
    assert audit["individual_adapter_handle_closes_verified"] is False
    if failure not in ("before_query", "before_changed", "set_status", "set_raise", "readback_query"):
        assert audit["actual"] == actual and audit["actual_snapshot_obtained"] is True
        assert audit["gate_checks"] == {"snapshot_matches_original": failure == "none"}
    else:
        assert audit["actual_snapshot_obtained"] is False and "actual" not in audit
    assert binding.session.close_resources() is (failure == "none")


def test_cleanup_restore_cannot_be_called_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _Api()
    binding = _restore_binding(monkeypatch, api)
    snapshots = iter((binding.dacl_audit["after"], binding.before))
    monkeypatch.setattr(diagnostic, "_snapshot", lambda *_args: next(snapshots))
    diagnostic._restore_for_cleanup(binding)
    assert api.set_calls == 1
    monkeypatch.setattr(diagnostic, "_snapshot", _forbidden)
    with pytest.raises(ValueError, match="acl_cleanup_restore_guards_failed"):
        diagnostic._restore_for_cleanup(binding)
    assert api.set_calls == 1 and binding.session.cleanup_dacl_restore["readback_gate_passed"] is False
    assert not binding.session.close_resources()


@pytest.mark.parametrize("post_state", ["protected", "baseline", "snapshot_error"])
def test_cleanup_restore_failed_set_records_one_readonly_post_observation(
    monkeypatch: pytest.MonkeyPatch, post_state: str,
) -> None:
    api = _Api()
    binding = _restore_binding(monkeypatch, api)
    original = copy.deepcopy(binding.before)
    apply_audit = copy.deepcopy(binding.dacl_audit)
    applied = copy.deepcopy(cast(dict[str, object], binding.dacl_audit["after"]))
    queries = 0

    def snapshot(api_arg: object, handle: object, package: str, user: str) -> dict[str, object]:
        nonlocal queries
        queries += 1
        assert api_arg is api and handle is binding.owner.handle
        assert (package, user) == (PACKAGE, USER)
        if queries == 1:
            return copy.deepcopy(applied)
        assert queries == 2
        if post_state == "snapshot_error":
            raise OSError("synthetic post-failed-set observation failure")
        return copy.deepcopy(applied if post_state == "protected" else original)

    def set_security(*_args: Any) -> int:
        api.set_calls += 1
        return 5

    monkeypatch.setattr(diagnostic, "_snapshot", snapshot)
    monkeypatch.setattr(api, "set_security", set_security)
    with pytest.raises(OSError, match="acl_cleanup_restore_dacl"):
        diagnostic._restore_for_cleanup(binding)

    audit = binding.session.cleanup_dacl_restore
    assert queries == 2 and api.set_calls == 1
    assert binding.before == original and binding.dacl_audit == apply_audit
    assert audit["expected"] == original and audit["expected_before_restore"] == apply_audit["after"]
    assert audit["set_returned"] is True and audit["set_security_status"] == 5
    assert audit["readback_gate_passed"] is False
    assert audit["actual_snapshot_obtained"] is False and "actual" not in audit
    assert audit["state"] == "failed" and audit["failed_at"] == "set_returned"
    assert audit["error_type"] == "OSError" and audit["error"] == "[Errno 5] acl_cleanup_restore_dacl"
    observation = audit["post_failed_set_observation"]
    if post_state == "snapshot_error":
        assert observation == {
            "status": "unknown",
            "error_type": "OSError",
            "error": "synthetic post-failed-set observation failure",
        }
    else:
        assert observation == {
            "status": "observed",
            "snapshot": applied if post_state == "protected" else original,
        }
    assert binding.session.unknown is True
    assert binding.session.close_resources() is False


def test_cleanup_restore_success_does_not_add_failed_set_observation_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _Api()
    binding = _restore_binding(monkeypatch, api)
    snapshots = iter((cast(dict[str, object], binding.dacl_audit["after"]), binding.before))
    queries = 0

    def snapshot(*_args: object) -> dict[str, object]:
        nonlocal queries
        queries += 1
        return copy.deepcopy(next(snapshots))

    def set_security(*_args: Any) -> int:
        api.set_calls += 1
        return 0

    monkeypatch.setattr(diagnostic, "_snapshot", snapshot)
    monkeypatch.setattr(api, "set_security", set_security)
    audit = diagnostic._restore_for_cleanup(binding)
    assert queries == 2 and api.set_calls == 1
    assert audit["readback_gate_passed"] is True
    assert audit["post_failed_set_observation"] is None


@pytest.mark.parametrize("phase", ["default", "readonly"])
@pytest.mark.parametrize("failure", ["none", "terminate", "context_exit", "gate", "host_snapshot"])
def test_restore_worker_reaps_inside_context_and_preserves_collected_evidence_on_failure(
    monkeypatch: pytest.MonkeyPatch, phase: str, failure: str,
) -> None:
    api = _Api()
    binding = _binding(monkeypatch, api, deny_target="user", restore_for_cleanup=True)
    frame = _frame(phase)
    if failure == "gate":
        cast(dict[str, Any], frame["checks"])["root_set"]["status"] = 5 if phase == "default" else 0
    result = SimpleNamespace(exit_code=0, frames=(frame,), stdout=b"saved stdout", stderr=b"saved stderr")
    events: list[str] = []
    pending = SimpleNamespace(close=lambda: events.append("pending_close"))

    class Worker:
        def __enter__(self) -> Worker:
            events.append("enter")
            return self

        def __exit__(self, *_args: object) -> None:
            events.append("exit")
            if failure == "context_exit":
                raise LaunchRejected("synthetic_close_failure", cleanup_owner=pending)

        def collect(self, timeout: int) -> Any:
            assert timeout == 15
            events.append("collect")
            return result

        def terminate(self) -> None:
            assert events == ["enter", "collect"]
            events.append("terminate")
            if failure == "terminate":
                raise LaunchRejected("synthetic_terminate_failure", cleanup_owner=pending)

    monkeypatch.setattr(diagnostic, "verify_manifest", lambda _manifest: None)
    monkeypatch.setattr(diagnostic, "WindowsLauncher", lambda: SimpleNamespace(launch=lambda _spec: Worker()))
    monkeypatch.setattr(diagnostic, "asdict", lambda _result: {"frames": [frame]})

    def snapshot(*_args: object) -> dict[str, object]:
        assert events[-1] == "exit"
        events.append("snapshot")
        if failure == "host_snapshot":
            raise OSError("synthetic post-worker snapshot failure")
        return _snapshot()

    real_gate = diagnostic._worker_gate

    def gate(*args: Any) -> None:
        assert events[-1] == "snapshot" and not binding.session.verified_reaped_phases
        events.append("gate")
        real_gate(*args)

    monkeypatch.setattr(diagnostic, "_snapshot", snapshot)
    monkeypatch.setattr(diagnostic, "_worker_gate", gate)
    manifest = RuntimeManifest("C:/synthetic/runtime", "C:/synthetic/runtime/python.exe", "test", "C:/synthetic/python.exe", ())
    report: dict[str, object] = {}
    case: dict[str, object] = {"phase": phase}
    args = (binding.session, manifest, {"SystemRoot": "C:/Windows", "WINDIR": "C:/Windows"}, case, report)
    if failure == "none":
        diagnostic._worker(*args)
    else:
        with pytest.raises((LaunchRejected, ValueError, OSError)):
            diagnostic._worker(*args)
    saved = json.loads(json.dumps(report))[phase + "_worker"]
    assert saved["frames"] == [frame] and saved["stdout"] == "saved stdout" and saved["stderr"] == "saved stderr"
    host = saved["host_terminate_and_wait"]
    assert host["terminate_attempted"] is True
    assert host["terminate_and_wait_completed"] is (failure != "terminate")
    assert host["context_exit_completed"] is (failure not in ("terminate", "context_exit"))
    assert host["strict_worker_gate_passed"] is (failure == "none")
    assert host["individual_adapter_handle_closes_verified"] is False
    assert host["popen_own_process_handle_verified"] is False
    assert "Trusted host RunningWorker.terminate contract" in host["basis"]
    assert binding.session.verified_reaped_phases == ({phase} if failure == "none" else set())
    if failure in ("terminate", "context_exit"):
        assert events == ["enter", "collect", "terminate", "exit"]
        assert "host_bound_root_after_worker" not in saved and binding.session.owners == [pending]
        assert host["failed_at"] == ("terminate_call" if failure == "terminate" else "context_exit")
        assert host["error_type"] == "LaunchRejected"
    else:
        assert events == ["enter", "collect", "terminate", "exit", "snapshot"] + ([] if failure == "host_snapshot" else ["gate"])
    assert binding.session.close_resources() is (failure == "none")


@pytest.mark.parametrize("selection", [None, "package"])
def test_cli_cleanup_restore_requires_user_before_report_or_native(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, selection: str | None,
) -> None:
    report = tmp_path / "invalid-combination.json"
    monkeypatch.setattr(readonly, "_new_report", _forbidden)
    args = ["diagnostic", "--report", str(report), "--python", "C:/synthetic/python.exe", "--restore-for-cleanup"]
    if selection is not None:
        args.extend(("--deny-target", selection))
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(SystemExit) as error:
        diagnostic.main()
    assert error.value.code == 2 and not report.exists()


@pytest.mark.parametrize("execute", [False, True])
def test_cli_cleanup_restore_explicit_user_selection_and_forwarding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, execute: bool,
) -> None:
    report = tmp_path / "restore-report.json"
    monkeypatch.setattr(readonly, "_new_report", lambda _path: report)
    monkeypatch.setattr(readonly, "_fingerprints", lambda: {"synthetic": "unchanged"})
    calls: list[bool] = []

    def experiment(output: Any, data: dict[str, object], python: Path, runtime: Path, *,
                   deny_target: str, restore_for_cleanup: bool) -> NoReturn:
        assert output.name == str(report) and python == Path("C:/synthetic/python.exe")
        assert runtime == tmp_path / "restore-report-runtime"
        assert deny_target == "user" and restore_for_cleanup is True
        assert cast(dict[str, object], data["cleanup_dacl_restore"])["enabled"] is True
        calls.append(restore_for_cleanup)
        raise ValueError("synthetic before-native stop")

    monkeypatch.setattr(diagnostic, "_experiment", experiment)
    args = ["diagnostic", "--report", str(report), "--python", "C:/synthetic/python.exe",
            "--deny-target", "user", "--restore-for-cleanup"]
    if execute:
        args.append("--experiment")
    monkeypatch.setattr(sys, "argv", args)
    assert diagnostic.main() == (2 if execute else 0)
    saved = json.loads(report.read_text(encoding="ascii"))
    assert calls == ([True] if execute else [])
    assert saved["cleanup_dacl_restore"]["enabled"] is True
    assert saved["cleanup_dacl_restore"]["set_attempted"] is False
    assert saved["deny_target"] == "user" and saved["stage0_passed"] is False


@pytest.mark.parametrize("failure", ["none", "default_gate", "readonly_gate", "terminate", "context_exit",
    "restore_before", "restore_set_status", "restore_set_raise", "restore_query", "restore_exact", "close", "delete"])
def test_cleanup_restore_experiment_only_restores_after_strict_reap_and_preserves_every_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str,
) -> None:
    api = _Api()
    profile_dir = tmp_path / "synthetic-profile"
    profile_dir.mkdir()
    profile = ProbeProfile(PROFILE.name, PACKAGE, str(profile_dir))
    monkeypatch.setattr(diagnostic, "create_probe_profile", lambda _name: profile)
    session = diagnostic.Session(cast(diagnostic.WriteApi, api), deny_target="user", restore_for_cleanup=True)

    def selected_session(selected_api: object, *, deny_target: str = "package",
                         restore_for_cleanup: bool = False) -> diagnostic.Session:
        assert selected_api is api and deny_target == "user" and restore_for_cleanup is True
        return session

    monkeypatch.setattr(diagnostic, "Session", selected_session)
    monkeypatch.setattr(diagnostic, "WriteApi", lambda: api)
    monkeypatch.setattr(diagnostic, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(readonly, "_reject_reparse", lambda _path: None)
    fixture = tmp_path / "synthetic-fixture.exe"
    fixture.write_bytes(b"mock fixture; never executed")
    monkeypatch.setattr(readonly, "FIXTURE", fixture)
    monkeypatch.setattr(readonly, "FIXTURE_SHA256", sha256(fixture.read_bytes()).hexdigest())
    monkeypatch.setattr(readonly, "SuspendedRegistryApi", lambda _observe: object())
    original = _snapshot(inherited=True)
    applied = copy.deepcopy(original)
    apply_acl, applied["raw_aces_hex"] = diagnostic._plan(original, PACKAGE, USER, deny_target="user")
    cast(dict[str, object], applied["metadata"])["dacl_protected"] = True
    helper = diagnostic.BindingHelperOwner(cast(subprocess.Popen[bytes], _Process({})), cast(diagnostic.WriteApi, api))
    helper.reaped = True

    def suspended_launch(_spec: object, *, _api: object) -> NoReturn:
        binding = diagnostic.BoundRoot(session)
        binding.owner.handle.value, binding.user = 50, USER
        binding.before, binding.validated = copy.deepcopy(original), True
        session.helpers.append(helper)
        raise LaunchRejected(readonly.STOP_STAGE)

    monkeypatch.setattr(diagnostic, "_create_worker", suspended_launch)
    runtime = tmp_path / "synthetic-runtime"
    manifest = RuntimeManifest(str(runtime), str(runtime / "python.exe"), "test", str(fixture), ())
    monkeypatch.setattr(diagnostic, "stage_runtime", lambda *_args, **_kwargs: manifest)
    monkeypatch.setattr(diagnostic, "verify_manifest", lambda _manifest: None)
    monkeypatch.setattr(diagnostic, "apply_readonly_dacl", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(diagnostic, "_seed", lambda *_args: [])
    events: list[str] = []

    class Worker:
        def __init__(self, phase: str) -> None:
            self.phase = phase

        def __enter__(self) -> Worker:
            events.append("enter:" + self.phase)
            return self

        def __exit__(self, *_args: object) -> None:
            events.append("exit:" + self.phase)
            if self.phase == "readonly" and failure == "context_exit":
                raise LaunchRejected("synthetic_close_failure", cleanup_owner=SimpleNamespace(close=lambda: None))

        def collect(self, timeout: int) -> Any:
            assert timeout == 15
            events.append("collect:" + self.phase)
            frame = _frame(self.phase)
            if failure == self.phase + "_gate":
                cast(dict[str, Any], frame["checks"])["root_set"]["status"] = 5 if self.phase == "default" else 0
            return SimpleNamespace(exit_code=0, frames=(frame,), stdout=b"", stderr=b"")

        def terminate(self) -> None:
            assert events[-1] == "collect:" + self.phase
            events.append("terminate:" + self.phase)
            if self.phase == "readonly" and failure == "terminate":
                raise LaunchRejected("synthetic_terminate_failure", cleanup_owner=SimpleNamespace(close=lambda: None))

    def launch(spec: LaunchSpec) -> Worker:
        assert spec.appcontainer_sid == PACKAGE
        return Worker(str(json.loads(spec.args[-2])["phase"]))

    monkeypatch.setattr(diagnostic, "WindowsLauncher", lambda: SimpleNamespace(launch=launch))
    real_asdict = asdict

    def serialized(value: Any) -> dict[str, Any]:
        if isinstance(value, SimpleNamespace):
            return {"exit_code": value.exit_code, "frames": list(value.frames)}
        return real_asdict(value)

    monkeypatch.setattr(diagnostic, "asdict", serialized)
    real_gate = diagnostic._worker_gate

    def gate(frame: dict[str, object], selected_profile: ProbeProfile, user: str, phase: str) -> None:
        assert "exit:" + phase in events and "terminate:" + phase in events
        real_gate(frame, selected_profile, user, phase)
        events.append("gate:" + phase)

    monkeypatch.setattr(diagnostic, "_worker_gate", gate)

    def snapshot(*_args: object) -> dict[str, object]:
        current = copy.deepcopy(original if api.set_calls in (0, 2) else applied)
        if session.verified_reaped_phases == {"default", "readonly"} and api.set_calls == 1:
            events.append("snapshot:restore_before")
            if failure == "restore_before":
                current["acl_revision"] = 4
        if api.set_calls == 2:
            events.append("snapshot:restore_actual")
            if failure == "restore_query":
                raise OSError("synthetic restore readback query failure")
            if failure == "restore_exact":
                cast(dict[str, object], current["metadata"])["dacl_protected"] = True
        return current

    monkeypatch.setattr(diagnostic, "_snapshot", snapshot)

    def set_security(*args: Any) -> int:
        api.set_calls += 1
        root = session.root
        assert root is not None and args[0] is root.owner.handle
        if api.set_calls == 1:
            assert args[1:5] == (4, 0x80000004, None, None)
            assert C.string_at(args[5], len(apply_acl)) == apply_acl
            events.append("set:apply")
            return 0
        assert api.set_calls == 2 and session.verified_reaped_phases == {"default", "readonly"}
        assert helper.reaped and not session.unknown and not session.owners
        assert events[-1] == "snapshot:restore_before" and "gate:readonly" in events
        assert args[1:5] == (4, 0x20000004, None, None)
        expected, _information = diagnostic._original_dacl(root)
        assert C.string_at(args[5], len(expected)) == expected
        events.append("set:restore")
        if failure == "restore_set_raise":
            raise OSError("synthetic restore Set failure")
        return 5 if failure == "restore_set_status" else 0

    monkeypatch.setattr(api, "set_security", set_security)
    real_close = api.reg_close

    def close(handle: Any) -> int:
        events.append("close:root")
        return real_close(handle)

    monkeypatch.setattr(api, "reg_close", close)
    if failure == "close":
        api.close_fail.add(50)
    deleted: list[ProbeProfile] = []

    def delete(selected: ProbeProfile) -> None:
        assert selected is profile and session.closed and api.closed == [50]
        assert session.cleanup_dacl_restore["readback_gate_passed"] is True and not session.unknown
        assert events[-1] == "close:root"
        events.append("delete")
        deleted.append(selected)
        if failure == "delete":
            raise OSError("synthetic delete failure after restore")

    monkeypatch.setattr(diagnostic, "delete_probe_profile", delete)
    report: dict[str, object] = {"stage0_passed": False}
    output = io.StringIO()
    assert diagnostic._experiment(output, report, fixture, runtime, deny_target="user", restore_for_cleanup=True) == (0 if failure == "none" else 2)
    saved = json.loads(output.getvalue())
    assert saved == report and saved["stage0_passed"] is False
    assert deleted == ([profile] if failure in ("none", "delete") else [])
    expected_sets = 0 if failure == "default_gate" else 1 if failure in (
        "readonly_gate", "terminate", "context_exit", "restore_before") else 2
    assert api.set_calls == expected_sets and api.closed == [50]
    restore_audit = saved["cleanup_dacl_restore"]
    assert restore_audit["enabled"] is True
    if expected_sets:
        assert saved["dacl"]["after"] == applied and saved["dacl"]["readback_gate_passed"] is True
    if failure in ("none", "close", "delete"):
        assert restore_audit["readback_gate_passed"] is True and restore_audit["actual"] == original
        assert restore_audit["expected"] == original and saved["dacl"]["after"] == applied
        assert events.index("gate:readonly") < events.index("set:restore") < events.index("close:root")
        assert saved["cleanup_state"]["individual_adapter_handle_closes_verified"] is False
        assert saved["cleanup_state"]["popen_own_process_handle_verified"] is False
    else:
        assert saved["cleanup"] == "withheld_unknown_helper_worker_key_or_binding"
        assert saved["cleanup_state"]["evidence_unknown"] is True and profile_dir.exists()
        assert restore_audit["readback_gate_passed"] is False
    if failure == "none":
        assert saved["cleanup"] == "profile_deleted"
        assert saved["status"] == "selected_registry_canaries_denied_namespace_coverage_incomplete"
    elif failure == "delete":
        assert saved["cleanup"] == "failed_profile_may_remain" and saved["cleanup_error"] == "synthetic delete failure after restore"
    elif failure == "close":
        assert saved["cleanup"] == "withheld_unknown_helper_worker_key_or_binding"
        assert saved["cleanup_state"]["resource_close_unconfirmed"] is True
    if failure in ("default_gate", "readonly_gate", "terminate", "context_exit"):
        assert "set:restore" not in events and restore_audit["state"] == "withheld"
        phase = "default" if failure == "default_gate" else "readonly"
        assert saved[phase + "_worker"]["frames"]
        assert saved[phase + "_worker"]["host_terminate_and_wait"]["strict_worker_gate_passed"] is False
