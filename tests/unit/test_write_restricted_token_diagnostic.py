"""Mock-only precursor diagnostic coverage; actual native calls are tripwired."""

from __future__ import annotations

import ast
import ctypes as C
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
from typing import Any, NoReturn, cast
from uuid import UUID

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import diagnose_write_restricted_token as diagnostic

USER = "S-1-5-21-1-2-3-1001"
SOURCES = {"standalone-script-and-test": "fixed"}
ALLOWED = {"GetCurrentProcess", "GetCurrentThread", "CloseHandle", "OpenProcessToken",
           "OpenThreadToken", "GetTokenInformation", "IsTokenRestricted",
           "LookupPrivilegeValueW", "CreateRestrictedToken"}


def forbidden(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise AssertionError("native/process/profile/impersonation/mutation tripwire")


@pytest.fixture(autouse=True)
def no_native(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(C, "WinDLL", forbidden, raising=False)
    monkeypatch.setattr(diagnostic, "NativeApi", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)


def sid_bytes(sid: str) -> bytes:
    parts = sid.split("-")
    values = [int(value) for value in parts[3:]]
    return bytes((1, len(values))) + int(parts[2]).to_bytes(6, "big") + struct.pack("<" + "I" * len(values), *values)


class Api:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed: list[int] = []
        self.create_calls = 0
        self.synthetic = ""
        self.source_user = USER
        self.source_restricted: list[str] = []
        self.source_ac = 0
        self.thread_ok, self.thread_error, self.thread_handle = 0, 1008, 0
        self.process_ok, self.process_handle = 1, 11
        self.create_ok, self.derived_handle = 1, 22
        self.derived_type = 1
        self.derived_user = USER
        self.derived_sids: list[str] | None = None
        self.derived_is_restricted = 1
        self.other_privilege_attributes = 0
        self.changed_source = False
        self.close_fail: set[int] = set()
        self.read_failure: tuple[int, int] | None = None
        self.bad_read_length: tuple[int, int] | None = None
        self.probe_size: int | None = None
        self.group_count_override: int | None = None

    def payload(self, token: int, kind: int, base: int) -> bytes:
        derived = token == 22
        if kind == 1:
            user = self.derived_user if derived else self.source_user
            attrs = int(self.changed_source and self.create_calls > 0 and not derived)
            return struct.pack("<QI4x", base + 16, attrs) + sid_bytes(user)
        if kind in (2, 11):
            if kind == 2:
                sids = ["S-1-5-32-545"]
            elif derived:
                sids = self.derived_sids if self.derived_sids is not None else [self.synthetic]
            else:
                sids = self.source_restricted
            count = len(sids)
            if kind == 2 and self.group_count_override is not None:
                return struct.pack("<I", self.group_count_override)
            if count == 0:
                return struct.pack("<I", 0)
            end = 8 + count * 16
            entries, storage = b"", b""
            for sid in sids:
                entries += struct.pack("<QI4x", base + end + len(storage), 7)
                storage += sid_bytes(sid)
            return struct.pack("<I4x", count) + entries + storage
        if kind == 3:
            other = self.other_privilege_attributes if derived else 2
            return struct.pack("<I", 2) + struct.pack("<IiI", 23, 0, 2) + struct.pack("<IiI", 99, -1, other)
        if kind == 8:
            return struct.pack("<I", self.derived_type if derived else 1)
        if kind == 29:
            return struct.pack("<I", self.source_ac)
        if kind == 10:
            value = diagnostic.Statistics()
            value.TokenId.LowPart = 102 if derived else 101
            value.ModifiedId.LowPart = 202 if derived else 201
            return bytes(value)
        raise AssertionError("unexpected token class")

    def call(self, name: str, *args: Any) -> tuple[int, int]:
        assert name in ALLOWED
        self.calls.append(name)
        if name in ("GetCurrentProcess", "GetCurrentThread"):
            return (-1 if name == "GetCurrentProcess" else -2), 0
        if name == "OpenThreadToken":
            assert args[1:3] == (8, True)
            C.cast(args[3], C.POINTER(C.c_void_p)).contents.value = self.thread_handle
            return self.thread_ok, self.thread_error
        if name == "OpenProcessToken":
            assert args[1] == 10
            C.cast(args[2], C.POINTER(C.c_void_p)).contents.value = self.process_handle
            return self.process_ok, 0 if self.process_ok else 5
        if name == "LookupPrivilegeValueW":
            assert args[:2] == (None, "SeChangeNotifyPrivilege")
            C.cast(args[2], C.POINTER(diagnostic.Luid)).contents.LowPart = 23
            return 1, 0
        if name == "IsTokenRestricted":
            return (self.derived_is_restricted if args[0].value == 22 else int(bool(self.source_restricted))), 0
        if name == "CreateRestrictedToken":
            assert args[0].value == 11
            assert args[1:7] == (9, 0, None, 0, None, 1)
            restricting = C.cast(args[7], C.POINTER(diagnostic.SidAttributes)).contents
            assert restricting.Attributes == 0
            raw = C.string_at(restricting.Sid, 28)
            self.synthetic = diagnostic._sid(raw, 0, 0)
            C.cast(args[8], C.POINTER(C.c_void_p)).contents.value = self.derived_handle
            self.create_calls += 1
            return self.create_ok, 0 if self.create_ok else 5
        if name == "CloseHandle":
            value = int(args[0].value)
            self.closed.append(value)
            return (0, 6) if value in self.close_fail else (1, 0)
        assert name == "GetTokenInformation"
        token, kind, output, capacity, returned = args
        assert kind in (1, 2, 3, 8, 11, 10, 29)
        base = C.addressof(output) if output is not None else 0
        payload = self.payload(int(token.value), kind, base)
        pointer = C.cast(returned, C.POINTER(C.c_uint32))
        if output is None:
            assert capacity == 0
            pointer.contents.value = len(payload) if self.probe_size is None else self.probe_size
            return 0, 122
        if self.read_failure == (int(token.value), kind):
            pointer.contents.value = 0
            return 0, 5
        assert len(payload) <= capacity
        C.memmove(output, payload, len(payload))
        pointer.contents.value = capacity + 1 if self.bad_read_length == (int(token.value), kind) else len(payload)
        return 1, 0


def run(monkeypatch: pytest.MonkeyPatch, api: Api) -> tuple[int, dict[str, Any], str]:
    monkeypatch.setattr(diagnostic, "NativeApi", lambda: cast(diagnostic.NativeApi, api))
    monkeypatch.setattr(diagnostic, "_fingerprints", lambda: SOURCES.copy())
    monkeypatch.setattr(diagnostic, "_layout", lambda: None)
    monkeypatch.setattr(diagnostic, "os", type("Platform", (), {"name": "nt"})())
    report: dict[str, Any] = {"stage0_passed": False, "source_sha256": SOURCES.copy()}
    output = io.StringIO()
    code = diagnostic._experiment(output, report)
    return code, report, output.getvalue()


def test_precursor_preserves_source_and_saves_before_create(monkeypatch: pytest.MonkeyPatch) -> None:
    api = Api()
    saved: list[dict[str, Any]] = []
    original = diagnostic._save

    def save(output: Any, report: dict[str, Any]) -> None:
        original(output, report)
        saved.append(json.loads(output.getvalue()))

    monkeypatch.setattr(diagnostic, "_save", save)
    code, report, rendered = run(monkeypatch, api)
    assert code == 0 and report["status"] == "precursor_token_observed"
    assert report["source_token_unchanged"] and report["source_unchanged"]
    assert report["derived_gate_checks"]["exact_single_restricting_sid"]
    assert report["source_is_token_restricted"]["subject"] == "source"
    assert report["source_is_token_restricted"]["native"]["raw_return"] == 0
    assert report["derived_is_token_restricted"]["subject"] == "derived"
    assert report["derived_is_token_restricted"]["native"]["raw_return"] == 1
    assert report["creation"]["flags_argument_is_not_write_effectiveness_evidence"]
    assert report["stage0_passed"] is False
    assert sorted(api.closed) == [11, 22] and len(api.closed) == 2
    assert len(rendered.encode("ascii")) < diagnostic.MAX_REPORT
    prepared = next(item for item in saved if item.get("creation", {}).get("state") == "prepared")
    assert prepared["source_before"]["restricted_sids"] == []
    assert not any(call["api"] == "CreateRestrictedToken" for call in prepared["native_calls"])
    assert all(query["payload_valid"] for query in report["token_queries"])
    assert any(query["read_query"]["returned_length"] == 4 for query in report["token_queries"] if query["class"] == 11)


@pytest.mark.parametrize("change", ["ac", "service", "restricted", "thread", "thread_partial", "open_partial"])
def test_source_context_fails_before_creation(monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    api = Api()
    if change == "ac": api.source_ac = 1
    if change == "service": api.source_user = "S-1-5-18"
    if change == "restricted": api.source_restricted = ["S-1-5-12"]
    if change == "thread": api.thread_ok, api.thread_error, api.thread_handle = 1, 0, 33
    if change == "thread_partial": api.thread_handle = 33
    if change == "open_partial": api.process_ok = 0
    code, report, _ = run(monkeypatch, api)
    assert code == 2 and report["status"] == "unknown"
    assert api.create_calls == 0 and report["incomplete"]
    if change in ("thread", "thread_partial"): assert api.closed == [33]
    if change == "open_partial": assert api.closed == [11]


@pytest.mark.parametrize("change", ["type", "user", "empty", "wrong", "extra", "is_restricted", "privilege", "source", "create_partial", "close"])
def test_derived_and_cleanup_fail_closed(monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    api = Api()
    if change == "type": api.derived_type = 2
    if change == "user": api.derived_user = "S-1-5-21-1-2-3-1002"
    if change == "empty": api.derived_sids = []
    if change == "wrong": api.derived_sids = ["S-1-5-12"]
    if change == "extra": api.derived_sids = ["S-1-5-12", "S-1-15-2-1"]
    if change == "is_restricted": api.derived_is_restricted = 0
    if change == "privilege": api.other_privilege_attributes = 2
    if change == "source": api.changed_source = True
    if change == "create_partial": api.create_ok = 0
    if change == "close": api.close_fail = {22}
    code, report, _ = run(monkeypatch, api)
    assert code == 2 and report["status"] == "unknown" and report["incomplete"]
    assert sorted(api.closed) == [11, 22] and api.closed.count(22) == 1
    if change == "close":
        assert report["all_token_handles_closed"] is False
        assert next(record for record in report["close_results"] if record["owner"] == "derived")["closed"] is False


@pytest.mark.parametrize("value", [-1, -2, -3, -4, -5, -6])
@pytest.mark.parametrize("subject", ["source", "derived", "thread_partial"])
def test_pseudo_outputs_are_rejected_and_never_closed(monkeypatch: pytest.MonkeyPatch, value: int, subject: str) -> None:
    api = Api()
    pointer = int(C.c_void_p(value).value or 0)
    if subject == "source": api.process_handle = pointer
    if subject == "derived": api.derived_handle = pointer
    if subject == "thread_partial": api.thread_handle = pointer
    code, report, _ = run(monkeypatch, api)
    assert code == 2 and report["status"] == "unknown"
    assert pointer not in api.closed
    assert report["all_token_handles_closed"] is False
    assert any(record["state"] == "invalid_or_duplicate_handle_withheld" for record in report["close_results"])
    if subject in ("source", "thread_partial"):
        assert api.create_calls == 0
    else:
        assert api.closed == [11]


def test_duplicate_owned_output_gets_at_most_one_close(monkeypatch: pytest.MonkeyPatch) -> None:
    api = Api()
    api.derived_handle = 11
    code, report, _ = run(monkeypatch, api)
    assert code == 2 and report["status"] == "unknown"
    assert api.closed == [11]
    assert report["all_token_handles_closed"] is False


@pytest.mark.parametrize("change", ["size", "read", "returned", "groups"])
def test_query_bounds_statuses_and_no_retry(monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    api = Api()
    if change == "size": api.probe_size = diagnostic.MAX_BUFFER + 1
    if change == "read": api.read_failure = (11, 1)
    if change == "returned": api.bad_read_length = (11, 1)
    if change == "groups": api.group_count_override = 129
    code, report, _ = run(monkeypatch, api)
    assert code == 2 and api.create_calls == 0
    failed = report["token_queries"][-1]
    assert failed["payload_valid"] is False and failed["incomplete"]
    assert failed["size_query"]["last_error"] == 122
    assert api.calls.count("GetTokenInformation") <= 4


def test_layout_flexible_arrays_sid_boundaries_and_luid_attributes() -> None:
    diagnostic._layout()
    assert diagnostic.PrivilegesLayout.Privileges.offset == 4
    assert diagnostic.GroupsLayout.Groups.offset == 8
    assert diagnostic._parse(11, struct.pack("<I", 0), 1000) == []
    assert diagnostic._parse(3, struct.pack("<I", 1) + struct.pack("<IiI", 99, -2, 0xFFFFFFFF), 1000) == [
        {"luid": {"low_dword": 99, "high_long": -2}, "attributes": 0xFFFFFFFF}]
    raw = sid_bytes(USER)
    assert diagnostic._sid(raw, 100, 100) == USER
    for address in (0, 99, 101, 100 + len(raw)):
        with pytest.raises(ValueError): diagnostic._sid(raw, 100, address)
    with pytest.raises(ValueError): diagnostic._sid(bytes((1, 16)) + bytes(70), 100, 100)
    with pytest.raises(ValueError): diagnostic._parse(2, struct.pack("<I", 1), 0)
    with pytest.raises(ValueError): diagnostic._parse(22, bytes(4), 0)


def test_synthetic_collision_stops_without_regeneration(monkeypatch: pytest.MonkeyPatch) -> None:
    value = UUID("00000001-0000-4000-8000-000000000002")
    monkeypatch.setattr(diagnostic, "uuid4", lambda: value)
    parts = struct.unpack("<4I", value.bytes)
    sid = "S-1-5-21-" + "-".join(map(str, parts))
    with pytest.raises(ValueError, match="collision"):
        diagnostic._synthetic({"user": {"sid": sid}, "groups": [], "restricted_sids": []})


def test_source_hash_change_prevents_success(monkeypatch: pytest.MonkeyPatch) -> None:
    api = Api()
    monkeypatch.setattr(diagnostic, "NativeApi", lambda: cast(diagnostic.NativeApi, api))
    monkeypatch.setattr(diagnostic, "os", type("Platform", (), {"name": "nt"})())
    monkeypatch.setattr(diagnostic, "_fingerprints", lambda: {"changed": "different"})
    report: dict[str, Any] = {"source_sha256": SOURCES.copy()}
    assert diagnostic._experiment(io.StringIO(), report) == 2
    assert report["status"] == "unknown" and not report["observations_valid"]
    assert sorted(api.closed) == [11, 22]


def test_cli_no_optin_and_exclusive_new_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(diagnostic, "ROOT", tmp_path)
    path = tmp_path / "data/validation/v06/token.json"
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", str(path)])
    assert diagnostic.main() == 0
    report = json.loads(path.read_text("ascii"))
    assert report["status"] == "not_executed_explicit_opt_in_required"
    assert report["scope"] == "precursor-token-only" and not report["stage0_passed"]
    with pytest.raises(ValueError): diagnostic.main()
    with pytest.raises(ValueError): diagnostic._new_report(tmp_path / "outside.json")


def test_binding_allowlist_and_no_shared_module_imports() -> None:
    source = Path(diagnostic.__file__).read_text("utf-8")
    tree = ast.parse(source)
    assert {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} <= {
        "__future__", "hashlib", "pathlib", "typing", "uuid"}
    for banned in ("CreateProcess", "CreateAppContainerProfile", "SetSecurityInfo", "RegOpenKey",
                   "ImpersonateLoggedOnUser", "AdjustTokenPrivileges", "NtCreateLowBoxToken"):
        assert banned not in source
    api = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "NativeApi")
    strings = {node.value for node in ast.walk(api) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert ALLOWED <= strings
    declaration = next(node.value for node in ast.walk(api) if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "declarations" for target in node.targets))
    assert isinstance(declaration, ast.Tuple)
    names = {entry.elts[1].value for entry in declaration.elts
             if isinstance(entry, ast.Tuple) and isinstance(entry.elts[1], ast.Constant)}
    assert names == ALLOWED


def test_evidence_budget_overflow_is_sticky_incomplete() -> None:
    report: dict[str, Any] = {"observations_valid": True, "source_before": {"oversized": "x" * diagnostic.MAX_REPORT}}
    output = io.StringIO()
    diagnostic._save(output, report)
    assert report["evidence_budget_exceeded"] and not report["observations_valid"]
    assert report["status"] == "unknown" and report["incomplete"]
    assert report["incomplete_observation_summaries"]["source_before"]["complete_required_fields_retained"] is False
