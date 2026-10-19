"""Focused ordinary-leaf controls; every native/profile/worker boundary is fake."""

from __future__ import annotations

import copy
import ast
import ctypes as C
from ctypes import wintypes as W
import io
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import diagnose_registry_leaf_control as diagnostic
import diagnose_registry_handle_access as access
import diagnose_worker_registry as readonly
import diagnose_worker_registry_acl as acl

USER = "S-1-5-21-1-2-3-1001"
SOURCES = {"synthetic-fixed-input": "unchanged"}


def _forbidden(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("unmocked native/profile/worker/namespace mutation forbidden")


@pytest.fixture(autouse=True)
def _no_native(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(C, "WinDLL", _forbidden, raising=False)
    monkeypatch.setattr(diagnostic, "LeafApi", _forbidden)
    for name in ("WriteApi", "create_probe_profile", "delete_probe_profile", "stage_runtime",
                 "apply_readonly_dacl", "_seed", "_apply", "_restore_for_cleanup", "_bind_from_worker", "BoundRoot"):
        monkeypatch.setattr(acl, name, _forbidden)
    monkeypatch.setattr(acl.Session, "create", _forbidden)
    monkeypatch.setattr(readonly, "SuspendedRegistryApi", _forbidden)
    monkeypatch.setattr(access, "_create_worker", _forbidden)
    monkeypatch.setattr(acl, "_SESSIONS", set())
    monkeypatch.setattr(acl, "_BINDINGS", set())
    monkeypatch.setattr(acl, "_UNCONFIRMED", [])


def _snapshot(entries: list[tuple[int, int, int, str]] | None = None, *, protected: bool = False) -> dict[str, object]:
    values = entries if entries is not None else [(0, 0x22, 0xF003F, "S-1-5-32-544"),
        (0, 0x12, 0xF003F, USER), (0, 0x12, 0xF003F, "S-1-5-18")]
    return {"metadata": {"owner_sid": USER, "dacl_present": True, "dacl_null": False,
        "dacl_protected": protected, "unparsed_ace_count": 0,
        "aces": [dict(type=t, flags=f, mask=m, sid=s) for t, f, m, s in values]},
        "acl_revision": 2, "raw_aces_hex": [acl._ace(*value).hex() for value in values]}


def test_ordinary_plan_preserves_critical_bytes_and_only_converts_inherited_flags() -> None:
    original = _snapshot()
    untouched = copy.deepcopy(original)
    payload, planned = diagnostic._plan(original, USER)
    raw = cast(list[str], original["raw_aces_hex"])
    assert planned[0] == acl._ace(1, 2, 0xD0026, USER).hex() and planned[1] == raw[0]
    assert planned[2] == acl._ace(0, 2, 0x20000, "S-1-3-4").hex()
    assert planned[3:] == [acl._ace(0, 2, 0xF003F, sid).hex() for sid in (USER, "S-1-5-18")]
    assert payload[8:] == b"".join(bytes.fromhex(item) for item in planned) and original == untouched
    audit = acl._input_plan_audit(original, payload, planned, "", user=USER, deny_target="user")
    assert audit["critical_aces_preserved_in_plan"] is True
    assert audit["inherited_conversion_original_indices"] == [1, 2]


def test_protected_baseline_plan_removes_only_experiment_aces_and_preserves_original_order() -> None:
    original = _snapshot()
    untouched = copy.deepcopy(original)
    payload, planned, audit = diagnostic._protected_baseline_plan(original, USER)
    assert planned == [acl._ace(0, 0x22, 0xF003F, "S-1-5-32-544").hex(),
        acl._ace(0, 2, 0xF003F, USER).hex(), acl._ace(0, 2, 0xF003F, "S-1-5-18").hex()]
    assert payload == struct.pack("<BBHHH", 2, 0, 8 + sum(len(bytes.fromhex(item)) for item in planned), 3, 0) + b"".join(
        bytes.fromhex(item) for item in planned)
    assert original == untouched and audit["inherited_conversion_original_indices"] == [1, 2]
    assert audit["original_order_preserved_in_plan"] is True and audit["critical_aces_preserved_in_plan"] is True


@pytest.mark.parametrize("entries", [
    [(0, 0x22, 0xF003F, "S-1-5-32-544"), (0, 0x32, 0xF003F, USER), (0, 0x12, 0xF003F, "S-1-5-18")],
    [(0, 0x22, 0xF003F, "S-1-5-32-544"), (0, 0x12, 0xF003F, USER), (0, 0x12, 0xF003F, "S-1-5-18"),
        (1, 0x12, 2, "S-1-5-32-545")],
    [(0, 0x22, 0xF003F, "S-1-5-32-544"), (1, 2, 2, "S-1-5-32-545"),
        (0, 0x12, 0xF003F, USER), (0, 0x12, 0xF003F, "S-1-5-18")],
])
def test_protected_baseline_plan_refuses_critical_conversion_or_noncanonical_order(
    entries: list[tuple[int, int, int, str]],
) -> None:
    with pytest.raises(ValueError):
        diagnostic._protected_baseline_plan(_snapshot(entries), USER)


@pytest.mark.parametrize("damage", ["owner", "null", "empty", "package", "owner_rights", "unknown",
    "raw", "user_allow", "system_allow", "critical_inherited", "inherited_deny", "deny_after_allow"])
def test_ordinary_baseline_and_plan_never_soften_unexpected_local_defaults(damage: str) -> None:
    entries = [(0, 0x22, 0xF003F, "S-1-5-32-544"), (0, 0x12, 0xF003F, USER), (0, 0x12, 0xF003F, "S-1-5-18")]
    if damage in ("package", "owner_rights"):
        entries.append((0, 0x12, 0x20019, "S-1-15-2-1" if damage == "package" else "S-1-3-4"))
    if damage in ("user_allow", "system_allow"):
        index = 1 if damage == "user_allow" else 2
        entries[index] = (0, 0x12, 0x20019, entries[index][3])
    if damage == "critical_inherited":
        entries[1] = (0, 0x32, 0xF003F, USER)
    if damage == "inherited_deny":
        entries.append((1, 0x12, 2, "S-1-5-32-545"))
    if damage == "deny_after_allow":
        entries.insert(1, (1, 2, 2, "S-1-5-32-545"))
    original = _snapshot(entries)
    metadata = cast(dict[str, object], original["metadata"])
    if damage == "owner":
        metadata["owner_sid"] = "S-1-5-18"
    elif damage == "null":
        metadata["dacl_null"] = True
    elif damage == "unknown":
        metadata["unparsed_ace_count"] = 1
    elif damage == "empty":
        metadata["aces"], original["raw_aces_hex"] = [], []
    elif damage == "raw":
        cast(list[str], original["raw_aces_hex"])[0] = acl._ace(0, 2, 0xF003F, "S-1-5-32-544").hex()
    with pytest.raises(ValueError):
        diagnostic._plan(original, USER)


class _Api:
    def __init__(self, case: str) -> None:
        self.case = case
        self.set_calls = 0
        self.query_calls = 0
        self.host_calls = 0
        self.creates = 0
        self.probes = 0
        self.deleted: list[str] = []
        self.closed: list[int] = []
        self.stage = "original"
        self.last_error = 1008
        self.original = _snapshot(protected=case == "protected")
        self.applied: dict[str, object] = {}
        self.budget_raw: list[str] | None = None
        self.output = io.StringIO()
        self.session: acl.Session | None = None

    def current_process(self) -> int:
        return -1

    def current_thread(self) -> int:
        return -2

    def open_process_token(self, process: int, mask: int, output: Any) -> bool:
        assert (process, mask) == (-1, 8)
        self.host_calls += 1
        C.cast(output, C.POINTER(W.HANDLE)).contents.value = 11 if self.host_calls == 1 else 21
        return True

    def open_thread_token(self, thread: int, mask: int, as_self: bool, output: Any) -> bool:
        assert (thread, mask, as_self) == (-2, 8, True)
        after = self.host_calls == 2
        present = self.case == ("thread_after" if after else "thread_before")
        partial = self.case == ("partial_after" if after else "partial_before")
        C.cast(output, C.POINTER(W.HANDLE)).contents.value = (22 if after else 12) if present or partial else 0
        self.last_error = 5 if self.case == "thread_error" else 1008
        return present

    def close(self, handle: Any) -> bool:
        self.closed.append(int(handle.value))
        return not (self.case == "token_close" and handle.value == 11)

    def reg_close(self, handle: Any) -> int:
        self.closed.append(int(handle.value))
        failed = {"leaf_close": 50, "parent_close": 40, "probe_close": 60}.get(self.case)
        return 5 if handle.value == failed else 0

    def open_key(self, parent: Any, name: str, reserved: int, desired: int, output: Any) -> int:
        if name == "Software":
            assert parent.value == diagnostic._HKCU.value and reserved == 0 and desired == 4
            C.cast(output, C.POINTER(W.HANDLE)).contents.value = 40
            return 5 if self.case == "parent_error" else 0
        assert parent.value == 40 and diagnostic._NAME.fullmatch(name) and reserved == 0 and desired == 2
        self.probes += 1
        before = self.set_calls == 0
        status = 5 if not before else 0
        if self.case == "positive_probe" and before:
            status = 5
        if self.case == "negative_probe" and not before:
            status = 0
        partial = self.case == "negative_partial" and not before
        C.cast(output, C.POINTER(W.HANDLE)).contents.value = 60 if before and status == 0 else 62 if status == 0 or partial else 0
        return status

    def create_key(self, parent: Any, name: str, reserved: int, cls: object, options: int,
                   desired: int, sa: object, output: Any, disposition: Any) -> int:
        assert parent.value == 40 and diagnostic._NAME.fullmatch(name)
        assert (reserved, cls, options, desired, sa) == (0, None, 1, 0x60000, None)
        self.creates += 1
        C.cast(output, C.POINTER(W.HANDLE)).contents.value = 50
        C.cast(disposition, C.POINTER(W.DWORD)).contents.value = 2 if self.case == "existing" else 1
        if self.case == "create_partial":
            raise OSError("synthetic create wrapper failure")
        return 0

    def query_object(self, key: Any, kind: int, output: Any, size: int, length: Any) -> int:
        assert key.value == 50 and kind == 0 and size == 56
        self.query_calls += 1
        missing = self.case == ("missing_before" if self.query_calls == 1 else "missing_after")
        C.memset(output, 0xA5, size)
        C.cast(output, C.POINTER(C.c_uint32))[0] = 0
        C.cast(output, C.POINTER(C.c_uint32))[1] = 0x20000 if missing else 0x60000
        C.cast(length, C.POINTER(C.c_uint32)).contents.value = 55 if self.case == "query_length" else 56
        return -1073741816 if self.case == "query_status" else 0

    def set_security(self, key: Any, kind: int, information: int, owner: object, group: object,
                     buffer: Any, sacl: object) -> int:
        assert key.value == 50 and kind == 4 and owner is group is sacl is None
        phase = "apply" if self.set_calls == 0 else "restore"
        prepared = json.loads(self.output.getvalue())[phase]
        assert prepared["state"] == "set_prepared" and prepared["set_attempted"] is False
        assert prepared["prepared_record_does_not_establish_call_started"] is True
        payload = bytes.fromhex(prepared["planned_acl_hex"])
        assert C.string_at(buffer, len(payload)) == payload
        assert information == (0x20000004 if phase == "restore" and self.case != "protected" else 0x80000004)
        self.set_calls += 1
        assert self.set_calls <= 2
        if self.case in (phase + "_set", "restore_post_error") and phase == ("apply" if self.case == "apply_set" else "restore"):
            return 5
        if self.case == "restore_wrapper" and phase == "restore":
            raise OSError("synthetic restore wrapper failure")
        self.stage = "denied" if phase == "apply" else "original"
        return 0

    def delete_key(self, parent: Any, name: str) -> int:
        assert parent.value == 40 and diagnostic._NAME.fullmatch(name) and self.stage == "original"
        assert self.session is not None and self.session.closed and not self.session.unknown
        assert not any(owner.handle.value for owner in self.session.handles)
        assert 40 not in self.closed and self.closed.count(50) == 1
        self.deleted.append(name)
        return 5 if self.case == "delete" else 0

    set_value = impersonate = revert = _forbidden


def _environment(monkeypatch: pytest.MonkeyPatch, api: _Api) -> None:
    monkeypatch.setattr(diagnostic, "LeafApi", lambda: api)
    monkeypatch.setattr(diagnostic, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(C, "get_last_error", lambda: api.last_error, raising=False)
    monkeypatch.setattr(diagnostic, "_fingerprints", lambda: {"changed": "input"} if api.case == "sources" else dict(SOURCES))
    real_session = acl.Session

    def session(selected: object) -> acl.Session:
        assert selected is api
        result = real_session(cast(acl.WriteApi, api))
        api.session = result
        return result

    monkeypatch.setattr(acl, "Session", session)

    def metadata(selected: object, token: int, target: dict[str, object], expected: int) -> None:
        assert selected is api and token in (11, 12, 21, 22)
        user = "S-1-5-018" if api.case == "service" else "S-1-5-21-1-2-3-1002" if api.case == "user_changed" and token == 21 else USER
        target.update(status="observed", user_sid=user, token_type=expected,
                      is_appcontainer=1 if api.case == "appcontainer" else 0, impersonation_level=2 if expected == 2 else None)

    monkeypatch.setattr(access, "_token_metadata", metadata)
    _, raw = diagnostic._plan(api.original, USER)
    api.applied = copy.deepcopy(api.original)
    api.applied["raw_aces_hex"] = raw
    applied_metadata = cast(dict[str, object], api.applied["metadata"])
    applied_metadata["dacl_protected"] = True
    original_aces = cast(list[dict[str, object]], cast(dict[str, object], api.original["metadata"])["aces"])
    applied_metadata["aces"] = [dict(type=1, flags=2, mask=0xD0026, sid=USER), copy.deepcopy(original_aces[0]),
        dict(type=0, flags=2, mask=0x20000, sid="S-1-3-4"),
        *[dict(entry, flags=int(str(entry["flags"])) & ~0x10) for entry in original_aces[1:]]]

    def snapshot(selected: object, key: Any, package: str, user: str) -> dict[str, object]:
        assert selected is api and key.value == 50 and package == "" and user == USER
        if api.case == "restore_post_error" and api.set_calls == 2:
            raise OSError("synthetic post-failed snapshot failure")
        data = copy.deepcopy(api.applied if api.stage == "denied" else api.original)
        if api.set_calls == 1 and api.case.startswith("apply_") and api.case != "apply_set":
            metadata = cast(dict[str, object], data["metadata"])
            field = api.case.removeprefix("apply_")
            if field == "owner":
                metadata["owner_sid"] = "S-1-5-18"
            elif field == "protected":
                metadata["dacl_protected"] = False
            elif field == "null":
                metadata["dacl_null"] = True
            elif field == "revision":
                data["acl_revision"] = 4
            elif field == "raw":
                data["raw_aces_hex"] = api.original["raw_aces_hex"]
        if api.case == "restore_exact" and api.set_calls == 2:
            cast(dict[str, object], data["metadata"])["unexpected_normalization"] = True
        if api.case in ("budget_input", "budget_actual", "budget_bytes", "budget_hard") and (
            api.case != "budget_actual" or api.set_calls == 1
        ):
            raw = cast(list[str], data["raw_aces_hex"])
            metadata = cast(dict[str, object], data["metadata"])
            if api.case == "budget_bytes":
                sid = "S-1-5-" + "-".join(["1"] * 15)
                raw[:] = [acl._ace(0, 2, 0x20019, sid).hex()] * 32
                metadata["aces"] = [dict(type=0, flags=2, mask=0x20019, sid=sid)] * 32
            elif api.case == "budget_hard":
                raw[:] = ["00" * 65537]
            else:
                parsed = cast(list[dict[str, object]], metadata["aces"])
                parsed.extend([copy.deepcopy(parsed[0])] * (33 - len(parsed)))
                raw.extend([raw[0]] * (33 - len(raw)))
            api.budget_raw = list(raw)
            metadata["dacl_sha256"] = "a" * 64
        return data

    monkeypatch.setattr(acl, "_snapshot", snapshot)


@pytest.mark.parametrize("case", ["none", "protected", "appcontainer", "service", "thread_before", "partial_before",
    "thread_error", "thread_after", "partial_after", "user_changed", "parent_error", "create_partial", "existing",
    "missing_before", "missing_after", "query_status", "query_length", "positive_probe", "negative_probe",
    "negative_partial", "apply_set", "apply_owner", "apply_protected", "apply_null", "apply_revision", "apply_raw",
    "restore_set", "restore_wrapper", "restore_post_error", "restore_exact", "probe_close", "token_close",
    "leaf_close", "parent_close", "delete", "sources", "save_apply", "save_restore"])
def test_fixed_leaf_flow_preserves_every_boundary_failure_and_only_deletes_after_exact_restore_and_closes(
    monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    api = _Api(case)
    _environment(monkeypatch, api)
    if case.startswith("save_"):
        real_save = readonly._save

        def save(output: Any, report: dict[str, object]) -> None:
            phase = case.removeprefix("save_")
            audit = cast(dict[str, object], report.get(phase, {}))
            if audit.get("state") == "set_prepared":
                raise ValueError("synthetic pre-native audit save failure")
            real_save(output, report)

        monkeypatch.setattr(readonly, "_save", save)
    report: dict[str, object] = {"source_sha256": dict(SOURCES), "stage0_passed": False}
    success = case in ("none", "protected")
    assert diagnostic._experiment(api.output, report) == (0 if success else 2)
    saved = json.loads(api.output.getvalue())
    assert saved == report and saved["stage0_passed"] is False
    assert api.session is not None and api.session.profile is None and api.session.root is None
    assert api.closed.count(40) <= 1 and api.closed.count(50) <= 1 and api.closed.count(60) <= 1
    assert all(value not in diagnostic._PREDEFINED for value in api.closed)
    if success:
        assert saved["cleanup"] == "leaf_deleted" and saved["leaf_may_remain"] is False
        assert api.creates == 1 and api.set_calls == 2 and api.query_calls == 2 and api.probes == 2
        assert api.host_calls == 2 and api.closed.count(11) == api.closed.count(21) == 1
        assert saved["restore"]["actual"] == saved["original"] and saved["restore"]["gate_passed"] is True
        assert saved["host_context_before_restore"]["matches_initial_user"] is True
        assert len(saved["apply"]["gate_checks"]) == 5 and all(saved["apply"]["gate_checks"].values())
    else:
        assert saved["leaf_may_remain"] is True
        assert saved["cleanup_state"]["unknown"] is True or case == "delete"
        assert len(api.deleted) == (1 if case in ("delete", "parent_close") else 0)
    if case in ("thread_after", "partial_after", "user_changed", "negative_probe", "negative_partial", "missing_after", "save_restore"):
        assert api.set_calls == 1
        if case != "save_restore":
            assert "restore" not in saved
    if case in ("existing", "create_partial", "positive_probe", "query_status", "query_length", "missing_before", "probe_close", "save_apply"):
        assert api.set_calls == 0
    if case in ("restore_set", "restore_post_error"):
        assert saved["restore"]["raw_set_status"] == 5 and saved["restore"]["set_returned"] is True
        observed = saved["restore"]["post_failed_set_observation"]
        assert observed["status"] == ("unknown" if case == "restore_post_error" else "observed")
        assert saved["restore"]["error_type"] == "OSError"
    if case in ("apply_owner", "apply_protected", "apply_null", "apply_revision", "apply_raw"):
        assert api.set_calls == 1 and "restore" not in saved
        assert len(saved["apply"]["gate_checks"]) == 5
        assert sum(not value for value in saved["apply"]["gate_checks"].values()) == 1
    if case == "restore_wrapper":
        assert saved["restore"]["set_returned"] is False
        assert saved["restore"]["post_failed_set_observation"]["status"] == "observed"
    if case in ("token_close", "leaf_close", "parent_close", "probe_close"):
        assert saved["cleanup_state"]["resource_close_unconfirmed"] is True
    if case in ("save_apply", "save_restore"):
        audit = saved[case.removeprefix("save_")]
        assert audit["failed_at"] == "set_prepared" and audit["set_attempted"] is False


@pytest.mark.parametrize("value", [0x80000007, 0x80000050, 0x80000060, C.c_void_p(-2147483647).value, C.c_void_p(-1).value])
def test_invalid_or_predefined_partial_key_is_never_closed(value: int) -> None:
    api = _Api("none")
    owner = diagnostic._KeyOwner(cast(Any, api), "synthetic_invalid")
    owner.handle.value = value
    with pytest.raises(ValueError):
        owner.close()
    assert api.closed == []


@pytest.mark.parametrize("case", ["budget_input", "budget_actual", "budget_bytes", "budget_hard"])
def test_every_snapshot_budget_refusal_preserves_bounded_raw_evidence_without_set_retry_or_delete(
    monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    api = _Api(case)
    _environment(monkeypatch, api)
    report: dict[str, object] = {"source_sha256": dict(SOURCES), "stage0_passed": False}
    assert diagnostic._experiment(api.output, report) == 2
    saved = json.loads(api.output.getvalue())
    assert len(api.output.getvalue().encode("ascii")) <= 512 * 1024 and api.deleted == []
    assert api.set_calls == (1 if case == "budget_actual" else 0)
    assert saved["cleanup_state"]["unknown"] is True and saved["leaf_may_remain"] is True
    rejected = saved["rejected_security_descriptor"]
    assert "aces" not in rejected["metadata_summary"]
    assert rejected["metadata_summary"]["owner_sid"] == USER
    assert rejected["metadata_summary"]["dacl_sha256"] == "a" * 64
    assert rejected["raw_evidence_complete"] is (case != "budget_hard")
    if case == "budget_hard":
        assert rejected["raw_aces_hex"] is None and rejected["status"] == "unknown_hard_bound_or_raw_shape"
    else:
        raw = rejected["raw_aces_hex"]
        assert raw == api.budget_raw
        assert len(raw) == (32 if case == "budget_bytes" else 33)
        assert rejected["raw_ace_bytes"] == sum(len(bytes.fromhex(item)) for item in raw)
        assert rejected["status"] == "rejected_input_budget"
    if case == "budget_actual":
        assert "actual" not in saved["apply"] and saved["apply"]["actual_snapshot_obtained"] is False
        assert "restore" not in saved
    else:
        assert "original" not in saved and "apply" not in saved


def test_cli_default_is_unexecuted_and_exclusive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report = tmp_path / "new.json"
    monkeypatch.setattr(readonly, "_new_report", lambda _path: report)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", str(report)])
    assert diagnostic.main() == 0
    saved = json.loads(report.read_text(encoding="ascii"))
    assert saved["status"] == "not_executed_explicit_opt_in_required" and saved["profile_created"] is False
    assert saved["restore_mode"] == "original"
    assert saved["restore_setter"] == "set-security-info"
    with pytest.raises(FileExistsError):
        diagnostic.main()


@pytest.mark.parametrize("argument", ["--sid", "--hkey", "--profile", "--registry-path", "--command"])
def test_cli_cannot_select_old_or_arbitrary_targets(monkeypatch: pytest.MonkeyPatch, argument: str) -> None:
    monkeypatch.setattr(readonly, "_new_report", _forbidden)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", "new.json", "--experiment", argument, "arbitrary"])
    with pytest.raises(SystemExit) as error:
        diagnostic.main()
    assert error.value.code == 2


def test_cli_optin_records_failure_and_start_end_source_hashes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report = tmp_path / "experiment.json"
    monkeypatch.setattr(readonly, "_new_report", lambda _path: report)
    monkeypatch.setattr(diagnostic, "_fingerprints", lambda: dict(SOURCES))
    calls: list[bool] = []

    def experiment(output: Any, data: dict[str, object]) -> NoReturn:
        assert output.name == str(report) and data["source_sha256"] == SOURCES
        calls.append(True)
        raise ValueError("synthetic failure before native")

    monkeypatch.setattr(diagnostic, "_experiment", experiment)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", str(report), "--experiment"])
    assert diagnostic.main() == 2
    saved = json.loads(report.read_text(encoding="ascii"))
    assert calls == [True] and saved["status"] == "leaf_control_blocked"
    assert saved["source_sha256_end"] == SOURCES and saved["source_unchanged"] is True
    assert saved["profile_created"] is False and saved["worker_code_executed"] is False


class _StagedApi(_Api):
    def __init__(self, case: str) -> None:
        super().__init__(case)
        self.events: list[str] = []
        self.protected_baseline = _snapshot(entries=[(0, 0x22, 0xF003F, "S-1-5-32-544"),
            (0, 2, 0xF003F, USER), (0, 2, 0xF003F, "S-1-5-18")], protected=True)

    def create_key(self, *args: Any) -> int:
        assert json.loads(self.output.getvalue())["restore_mode"] == "protected-then-original"
        self.events.append("create")
        return super().create_key(*args)

    def open_process_token(self, process: int, mask: int, output: Any) -> bool:
        result = super().open_process_token(process, mask, output)
        self.events.append("host_" + str(self.host_calls))
        if self.host_calls == 3:
            C.cast(output, C.POINTER(W.HANDLE)).contents.value = 31
        return result

    def open_thread_token(self, thread: int, mask: int, as_self: bool, output: Any) -> bool:
        result = super().open_thread_token(thread, mask, as_self, output)
        if self.host_calls == 3 and self.case == "staged_host":
            self.last_error = 5
        return result

    def open_key(self, parent: Any, name: str, reserved: int, desired: int, output: Any) -> int:
        if name != "Software":
            self.events.append("probe_" + str(self.set_calls))
        if name != "Software" and self.stage == "protected_baseline":
            assert parent.value == 40 and diagnostic._NAME.fullmatch(name) and (reserved, desired) == (0, 2)
            self.probes += 1
            status = 5 if self.case == "staged_positive_probe" else 0
            C.cast(output, C.POINTER(W.HANDLE)).contents.value = 70 if status == 0 else 0
            return status
        return super().open_key(parent, name, reserved, desired, output)

    def query_object(self, key: Any, kind: int, output: Any, size: int, length: Any) -> int:
        status = super().query_object(key, kind, output, size, length)
        self.events.append("grant_" + str(self.query_calls))
        if self.query_calls == 3 and self.case == "staged_missing_wd":
            C.cast(output, C.POINTER(C.c_uint32))[1] = 0x20000
        return status

    def set_security(self, key: Any, kind: int, information: int, owner: object, group: object,
                     buffer: Any, sacl: object) -> int:
        assert key.value == 50 and kind == 4 and owner is group is sacl is None
        phase = ("apply", "restore_protected", "restore")[self.set_calls]
        saved = json.loads(self.output.getvalue())
        prepared = saved[phase]
        assert prepared["state"] == "set_prepared" and prepared["set_attempted"] is False
        assert prepared["prepared_record_does_not_establish_call_started"] is True
        assert saved["restored"] is False
        payload = bytes.fromhex(prepared["planned_acl_hex"])
        assert C.string_at(buffer, len(payload)) == payload
        assert information == (0x20000004 if phase == "restore" else 0x80000004)
        expected = self.applied if phase == "apply" else self.protected_baseline if phase == "restore_protected" else self.original
        assert prepared["expected_raw_aces_hex"] == expected["raw_aces_hex"]
        if phase != "apply":
            assert saved["apply"]["actual"] == self.applied and saved["apply"]["gate_passed"] is True
        if phase == "restore":
            assert saved["restore_protected"]["actual"] == self.protected_baseline
            assert saved["restore_protected"]["gate_passed"] is True
            assert saved["verified_protected_baseline"] == self.protected_baseline
            assert prepared["before"] == self.protected_baseline
        self.set_calls += 1
        self.events.append(phase)
        if self.case == "staged_" + phase + "_wrapper":
            raise OSError("synthetic staged wrapper failure")
        if self.case in ("staged_" + phase + "_set", "staged_" + phase + "_post_error"):
            return 5
        self.stage = {"apply": "denied", "restore_protected": "protected_baseline", "restore": "original"}[phase]
        return 0


def _staged_environment(monkeypatch: pytest.MonkeyPatch, api: _StagedApi) -> list[diagnostic._Leaf]:
    _environment(monkeypatch, api)
    real_leaf = diagnostic._Leaf
    leaves: list[diagnostic._Leaf] = []

    def leaf(*args: Any) -> diagnostic._Leaf:
        result = real_leaf(*args)
        leaves.append(result)
        return result

    monkeypatch.setattr(diagnostic, "_Leaf", leaf)
    fake_metadata = access._token_metadata

    def metadata(selected: Any, token: int, target: dict[str, object], expected: int) -> None:
        fake_metadata(selected, 21 if token == 31 else token, target, expected)
        if token == 31 and api.case == "staged_user_changed":
            target["user_sid"] = "S-1-5-21-1-2-3-1002"

    monkeypatch.setattr(access, "_token_metadata", metadata)
    fake_snapshot = acl._snapshot
    protected_reads = 0

    def snapshot(*args: Any) -> dict[str, object]:
        nonlocal protected_reads
        if api.case == "staged_restore_protected_post_error" and api.set_calls == 2:
            raise OSError("synthetic failed protected-set observation failure")
        if api.case == "staged_restore_post_error" and api.set_calls == 3:
            raise OSError("synthetic failed final-set observation failure")
        data = copy.deepcopy(api.protected_baseline) if api.stage == "protected_baseline" else fake_snapshot(*args)
        if api.stage == "protected_baseline":
            protected_reads += 1
            field = api.case.removeprefix("staged_gate_")
            metadata = cast(dict[str, object], data["metadata"])
            if field == "owner":
                metadata["owner_sid"] = "S-1-5-18"
            elif field == "protected":
                metadata["dacl_protected"] = False
            elif field == "null":
                metadata["dacl_null"] = True
            elif field == "present_false":
                metadata["dacl_present"] = False
            elif field == "present_missing":
                metadata.pop("dacl_present", None)
            elif field == "revision":
                data["acl_revision"] = 4
            elif field == "raw":
                data["raw_aces_hex"] = api.original["raw_aces_hex"]
            if api.case == "staged_before_final_drift" and protected_reads == 2:
                metadata["unexpected_control_change"] = True
        if api.set_calls == 3 and api.case == "staged_final_exact":
            cast(dict[str, object], data["metadata"])["unexpected_normalization"] = True
        return data

    monkeypatch.setattr(acl, "_snapshot", snapshot)
    return leaves


@pytest.mark.parametrize("case", ["staged_ok", "staged_restore_protected_set", "staged_restore_protected_wrapper",
    "staged_restore_protected_post_error", "staged_gate_owner", "staged_gate_protected", "staged_gate_null",
    "staged_gate_revision", "staged_gate_raw", "staged_gate_present_false", "staged_gate_present_missing",
    "staged_host", "staged_user_changed", "staged_positive_probe",
    "staged_missing_wd", "staged_before_final_drift", "staged_save_protected", "staged_save_final",
    "staged_restore_set", "staged_restore_wrapper", "staged_restore_post_error", "staged_final_exact"])
def test_staged_restore_preserves_deny_evidence_and_stops_at_every_failed_boundary(
    monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    api = _StagedApi(case)
    leaves = _staged_environment(monkeypatch, api)
    if case in ("staged_save_protected", "staged_save_final"):
        real_save = readonly._save
        failed_phase = "restore_protected" if case == "staged_save_protected" else "restore"

        def save(output: Any, report: dict[str, object]) -> None:
            if cast(dict[str, object], report.get(failed_phase, {})).get("state") == "set_prepared":
                raise ValueError("synthetic staged pre-native audit save failure")
            real_save(output, report)

        monkeypatch.setattr(readonly, "_save", save)
    report: dict[str, object] = {"source_sha256": dict(SOURCES), "stage0_passed": False,
        "restore_mode": "protected-then-original"}
    success = case == "staged_ok"
    assert diagnostic._experiment(api.output, report) == (0 if success else 2)
    saved = json.loads(api.output.getvalue())
    assert saved == report and saved["stage0_passed"] is False and api.creates == 1
    assert len(leaves) == 1 and leaves[0].applied == api.applied
    assert saved["apply"]["actual"] == api.applied and saved["apply"]["gate_passed"] is True
    assert leaves[0].restored is success and saved["restored"] is success
    if success:
        assert api.events == ["host_1", "create", "grant_1", "probe_0", "apply", "grant_2", "probe_1",
            "host_2", "restore_protected", "host_3", "probe_2", "grant_3", "restore"]
        assert saved["restore"]["actual"] == saved["original"] and api.set_calls == 3
        assert saved["restore"]["gate_checks"]["snapshot_matches_original"] is True
        assert saved["cleanup"] == "leaf_deleted" and len(api.deleted) == 1
        assert api.host_calls == api.query_calls == api.probes == 3
    else:
        assert api.deleted == [] and saved["leaf_may_remain"] is True and saved["cleanup_state"]["unknown"] is True
    if case == "staged_save_protected":
        assert api.set_calls == 1
    elif case.startswith("staged_restore_protected_") or case.startswith("staged_gate_"):
        assert api.set_calls == 2 and "restore" not in saved and api.host_calls == 2
    elif case != "staged_ok" and not case.startswith("staged_restore_") and case != "staged_final_exact":
        assert api.set_calls == 2
    if case.startswith("staged_gate_"):
        assert len(saved["restore_protected"]["gate_checks"]) == 5
        assert sum(not value for value in saved["restore_protected"]["gate_checks"].values()) == 1
        assert leaves[0].protected_baseline == {} and "verified_protected_baseline" not in saved
    if saved["restore_protected"]["gate_passed"]:
        assert leaves[0].protected_baseline == api.protected_baseline
        assert saved["verified_protected_baseline"] == api.protected_baseline
    for phase in ("restore_protected", "restore"):
        audit = saved.get(phase)
        if not isinstance(audit, dict):
            continue
        if case == "staged_" + phase + "_set" or case == "staged_" + phase + "_post_error":
            assert audit["raw_set_status"] == 5 and audit["set_returned"] is True
            observation = audit["post_failed_set_observation"]
            assert observation["status"] == ("unknown" if case.endswith("post_error") else "observed")
            if observation["status"] == "observed":
                assert observation["matches_before_call"] is True and observation["matches_original"] is False
        if case == "staged_" + phase + "_wrapper":
            assert audit["raw_set_status"] is None and audit["set_returned"] is False
            assert audit["post_failed_set_observation"]["status"] == "observed"
    if case == "staged_final_exact":
        assert api.set_calls == 3 and saved["restore"]["gate_checks"]["snapshot_matches_original"] is False
        assert all(value for name, value in saved["restore"]["gate_checks"].items() if name != "snapshot_matches_original")
    if case in ("staged_save_protected", "staged_save_final"):
        audit = saved["restore_protected" if case == "staged_save_protected" else "restore"]
        assert audit["failed_at"] == "set_prepared" and audit["set_attempted"] is False


@pytest.mark.parametrize("mode", ["original", "protected-then-original"])
def test_cli_records_selected_restore_mode_before_experiment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str,
) -> None:
    path = tmp_path / "mode.json"
    monkeypatch.setattr(readonly, "_new_report", lambda _path: path)
    monkeypatch.setattr(diagnostic, "_fingerprints", lambda: dict(SOURCES))
    calls: list[str] = []

    def experiment(output: Any, report: dict[str, object]) -> int:
        assert report["restore_mode"] == mode
        calls.append(mode)
        return 0

    monkeypatch.setattr(diagnostic, "_experiment", experiment)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", str(path), "--experiment", "--restore-mode", mode])
    assert diagnostic.main() == 0 and calls == [mode]
    assert json.loads(path.read_text(encoding="ascii"))["restore_mode"] == mode


def test_invalid_restore_mode_is_rejected_before_api_or_target_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readonly, "_new_report", _forbidden)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", "unused.json", "--restore-mode", "fallback"])
    with pytest.raises(SystemExit) as error:
        diagnostic.main()
    assert error.value.code == 2
    with pytest.raises(ValueError, match="restore_mode_invalid"):
        diagnostic._experiment(io.StringIO(), {"restore_mode": "fallback"})


@pytest.mark.parametrize("protected", [False, True])
def test_relative_descriptor_has_only_exact_owned_dacl(protected: bool) -> None:
    original = _snapshot()
    entries = [bytes.fromhex(item) for item in cast(list[str], original["raw_aces_hex"])]
    payload = diagnostic._acl_buffer(2, entries)
    descriptor = diagnostic._relative_descriptor(payload, protected)
    assert len(descriptor) == 20 + len(payload)
    assert struct.unpack_from("<BBHIIII", descriptor) == (1, 0, 0x9004 if protected else 0x8004, 0, 0, 0, 20)
    assert descriptor[20:] == payload
    assert descriptor[28:] == b"".join(entries)


@pytest.mark.parametrize("damage", ["short", "size", "revision", "count", "ace_size", "sid_shape", "trailing", "budget"])
def test_relative_descriptor_refuses_invalid_acl_structure_or_budget(damage: str) -> None:
    original = _snapshot()
    entries = [bytes.fromhex(item) for item in cast(list[str], original["raw_aces_hex"])]
    payload = bytearray(diagnostic._acl_buffer(2, entries))
    if damage == "short":
        payload = payload[:7]
    elif damage == "size":
        struct.pack_into("<H", payload, 2, len(payload) + 4)
    elif damage == "revision":
        payload[0] = 3
    elif damage == "count":
        struct.pack_into("<H", payload, 4, diagnostic.MAX_ACCEPTED_ACES + 1)
    elif damage == "ace_size":
        struct.pack_into("<H", payload, 10, 0xFFFC)
    elif damage == "sid_shape":
        payload[16] = 2
    elif damage == "trailing":
        payload.extend(bytes(4))
        struct.pack_into("<H", payload, 2, len(payload))
    else:
        payload = bytearray(9 + diagnostic.MAX_ACCEPTED_RAW_BYTES)
    with pytest.raises(ValueError, match="leaf_control_relative_"):
        diagnostic._relative_descriptor(bytes(payload), True)


def test_low_level_binding_uses_hkey_dword_pointer_and_signed_lstatus(monkeypatch: pytest.MonkeyPatch) -> None:
    tree = ast.parse(Path(diagnostic.__file__).read_text(encoding="utf-8"))
    declaration = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LeafApi")
    bound: list[tuple[Any, ...]] = []
    namespace: dict[str, Any] = {"access": SimpleNamespace(AccessApi=object), "C": C, "W": W,
                 "readonly": SimpleNamespace(_bind=lambda *args: bound.append(args))}
    monkeypatch.setattr(C, "WinDLL", lambda *args, **kwargs: "mock-advapi", raising=False)
    exec(compile(ast.Module(body=[declaration], type_ignores=[]), "<mock LeafApi binding>", "exec"), namespace)
    namespace["LeafApi"]()
    assert bound[-1] == ("mock-advapi", "RegSetKeySecurity", [W.HKEY, W.DWORD, C.c_void_p], C.c_int32)


class _LowLevelApi(_StagedApi):
    def __init__(self, case: str) -> None:
        super().__init__(case)
        self.high_level_calls = 0
        self.low_level_calls: list[str] = []

    def create_key(self, *args: Any) -> int:
        assert json.loads(self.output.getvalue())["restore_setter"] == "reg-set-key-security"
        return super().create_key(*args)

    def set_security(self, *args: Any) -> int:
        assert self.set_calls == 0  # Apply only; restore must never cross APIs.
        self.high_level_calls += 1
        return super().set_security(*args)

    def reg_set_key_security(self, key: Any, information: int, buffer: Any) -> int:
        assert key.value == 50 and self.set_calls in (1, 2)
        phase = "restore_protected" if self.set_calls == 1 else "restore"
        saved = json.loads(self.output.getvalue())
        prepared = saved[phase]
        assert prepared["state"] == "set_prepared" and prepared["set_attempted"] is False
        assert prepared["setter"] == "reg-set-key-security" and prepared["api"] == "RegSetKeySecurity"
        assert prepared["prepared_record_does_not_establish_call_started"] is True
        assert prepared["relative_descriptor_owner_group_sacl_offsets_zero"] is True
        assert saved["apply"]["api"] == "SetSecurityInfo" and saved["apply"]["gate_passed"] is True
        expected = self.protected_baseline if phase == "restore_protected" else self.original
        assert prepared["expected_raw_aces_hex"] == expected["raw_aces_hex"]
        assert information == prepared["security_information"] == (0x80000004 if phase == "restore_protected" else 0x20000004)
        descriptor = bytes.fromhex(prepared["planned_security_descriptor_hex"])
        assert C.string_at(buffer, len(descriptor)) == descriptor
        assert prepared["planned_security_descriptor_bytes"] == len(descriptor)
        assert struct.unpack_from("<BBHIIII", descriptor) == (1, 0, 0x9004 if phase == "restore_protected" else 0x8004, 0, 0, 0, 20)
        payload = bytes.fromhex(prepared["planned_acl_hex"])
        assert descriptor[20:] == payload and payload[8:] == b"".join(
            bytes.fromhex(item) for item in cast(list[str], expected["raw_aces_hex"]))
        grant_phase = "after_deny_before_restore" if phase == "restore_protected" else "after_protected_before_restore"
        assert saved["granted_access"][grant_phase]["granted_access"] == 0x60000
        self.low_level_calls.append(phase)
        self.set_calls += 1
        if self.case == "staged_" + phase + "_set":
            return 5
        self.stage = "protected_baseline" if phase == "restore_protected" else "original"
        return 0


@pytest.mark.parametrize("case", ["staged_ok", "staged_restore_protected_set", "staged_restore_set", "staged_gate_protected"])
def test_selected_low_level_restore_is_single_call_per_phase_without_fallback(
    monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    api = _LowLevelApi(case)
    _staged_environment(monkeypatch, api)
    report: dict[str, object] = {"source_sha256": dict(SOURCES), "stage0_passed": False,
        "restore_mode": "protected-then-original", "restore_setter": "reg-set-key-security"}
    success = case == "staged_ok"
    assert diagnostic._experiment(api.output, report) == (0 if success else 2)
    saved = json.loads(api.output.getvalue())
    assert saved["restore_setter"] == "reg-set-key-security" and api.high_level_calls == 1
    assert api.low_level_calls == (["restore_protected", "restore"] if case in ("staged_ok", "staged_restore_set") else ["restore_protected"])
    assert saved["restored"] is success and len(api.deleted) == int(success)
    if case == "staged_gate_protected":
        assert saved["restore_protected"]["gate_checks"]["protection_matches"] is False
        assert "restore" not in saved
    elif not success:
        phase = "restore_protected" if case == "staged_restore_protected_set" else "restore"
        assert saved[phase]["raw_set_status"] == 5 and saved[phase]["set_returned"] is True
        assert saved[phase]["post_failed_set_observation"]["matches_before_call"] is True
    if success:
        assert saved["restore"]["gate_checks"]["snapshot_matches_original"] is True
    else:
        assert saved["leaf_may_remain"] is True and saved["cleanup_state"]["unknown"] is True


@pytest.mark.parametrize("setter", ["set-security-info", "reg-set-key-security"])
def test_cli_records_restore_setter_before_experiment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, setter: str) -> None:
    path = tmp_path / "setter.json"
    monkeypatch.setattr(readonly, "_new_report", lambda _path: path)
    monkeypatch.setattr(diagnostic, "_fingerprints", lambda: dict(SOURCES))

    def experiment(_output: Any, report: dict[str, object]) -> int:
        assert report["restore_setter"] == setter
        return 0

    monkeypatch.setattr(diagnostic, "_experiment", experiment)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", str(path), "--experiment", "--restore-setter", setter])
    assert diagnostic.main() == 0
    assert json.loads(path.read_text(encoding="ascii"))["restore_setter"] == setter


def test_arbitrary_restore_setter_is_rejected_before_any_api_or_leaf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readonly, "_new_report", _forbidden)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--report", "unused.json", "--restore-setter", "fallback"])
    with pytest.raises(SystemExit) as error:
        diagnostic.main()
    assert error.value.code == 2
    with pytest.raises(ValueError, match="restore_setter_invalid"):
        diagnostic._experiment(io.StringIO(), {"restore_setter": "fallback"})
