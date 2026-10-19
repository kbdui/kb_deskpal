"""Opt-in ordinary-host control on one new volatile HKCU\\Software leaf.

No AppContainer profile or worker is created. The host only probes opening the
leaf for KEY_SET_VALUE; it never writes a value or creates descendants. Success
does not establish AC isolation or explain registry-root propagation timing.
"""

from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import struct
import sys
from typing import TextIO, cast
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import diagnose_registry_handle_access as access  # noqa: E402
import diagnose_worker_registry as readonly  # noqa: E402
import diagnose_worker_registry_acl as acl  # noqa: E402

_NAME = re.compile(r"DeskpalV06Leaf_[0-9a-f]{32}\Z", re.ASCII)
_HKCU = W.HKEY(C.c_int32(0x80000001).value)
_PREDEFINED_VALUES = (*range(0x80000000, 0x80000008), 0x80000050, 0x80000060)
_PREDEFINED = {W.HKEY(C.c_int32(value).value).value for value in _PREDEFINED_VALUES} | set(_PREDEFINED_VALUES)
MAX_ACCEPTED_ACES = 32
MAX_ACCEPTED_RAW_BYTES = 2048


class LeafApi(access.AccessApi):
    def __init__(self) -> None:
        super().__init__()
        advapi = C.WinDLL("advapi32", use_last_error=True)
        self.delete_key = readonly._bind(advapi, "RegDeleteKeyW", [W.HKEY, W.LPCWSTR], C.c_int32)
        self.reg_set_key_security = readonly._bind(advapi, "RegSetKeySecurity",
            [W.HKEY, W.DWORD, C.c_void_p], C.c_int32)


class _KeyOwner(acl.HandleOwner):
    def __init__(self, api: LeafApi, role: str) -> None:
        super().__init__(api, key=True)
        self.role = role
        self.close_attempted = False
        self.close_status: int | None = None

    def require(self) -> None:
        if self.handle.value in (None, 0, C.c_void_p(-1).value) or self.handle.value in _PREDEFINED:
            raise ValueError("leaf_control_invalid_nonpredefined_handle")

    def close(self) -> None:
        if self.handle.value:
            if self.close_attempted:
                raise ValueError("leaf_control_prior_close_unconfirmed_no_retry")
            self.require()  # Never close a predefined/invalid partial result.
            self.close_attempted = True
            self.close_status = int(self.api.reg_close(self.handle))
            acl._status(self.close_status, "leaf_control_close_" + self.role)
            self.handle.value = None

    def record(self) -> dict[str, object]:
        return {"role": self.role, "close_attempted": self.close_attempted,
                "raw_close_status": self.close_status, "handle_remaining": bool(self.handle.value)}


class _Leaf:
    def __init__(self, api: LeafApi, session: acl.Session, report: dict[str, object], restore_mode: str = "original",
                 restore_setter: str = "set-security-info") -> None:
        self.api, self.session, self.report = api, session, report
        self.restore_mode = restore_mode  # Selected before this leaf is created; never a failure fallback.
        self.restore_setter = restore_setter
        self.parent = _KeyOwner(api, "parent")  # Separate owner: kept until after the one permitted delete.
        self.key = _KeyOwner(api, "leaf")
        session.handles.append(self.key)  # Register partial create output before RegCreateKeyExW.
        self.keys = [self.key]
        self.name = "DeskpalV06Leaf_" + uuid4().hex
        self.user = ""
        self.created = False
        self.before: dict[str, object] = {}
        self.applied: dict[str, object] = {}
        self.protected_baseline: dict[str, object] = {}
        self.restored = False
        self.probes: dict[str, dict[str, object]] = {}
        self.grants: dict[str, dict[str, object]] = {}
        report.update(leaf_name=self.name, leaf_parent="HKCU\\Software", volatile=True,
                      restore_mode=restore_mode, restore_setter=restore_setter, restored=False,
                      restore_setter_scope="New childless leaf mechanism comparison only; no recursive AC-root propagation or cleanup acceptance.",
                      snapshot_scope="OWNER + DACL and returned control metadata only; no full security descriptor claim.",
                      failed_leaf_must_not_be_reopened_deleted_or_reused=True,
                      host_probes=self.probes, granted_access=self.grants,
                      snapshot_input_limits={"max_aces": MAX_ACCEPTED_ACES,
                          "max_raw_ace_bytes": MAX_ACCEPTED_RAW_BYTES, "max_rejected_descriptors": 1})

    def require(self) -> None:
        if (self.session.closed or self.session.unknown or self.session.profile is not None
                or self.session.root is not None or not self.created or not _NAME.fullmatch(self.name)):
            raise ValueError("leaf_control_requires_own_new_live_leaf")
        self.parent.require()
        self.key.require()


def _fingerprints() -> dict[str, str]:
    paths = ("scripts/diagnose_registry_leaf_control.py", "scripts/diagnose_registry_handle_access.py",
        "scripts/diagnose_worker_registry_acl.py", "scripts/diagnose_worker_registry.py",
        "src/deskpal/execution/runtime.py", "src/deskpal/execution/windows/_win32.py",
        "src/deskpal/execution/windows/launcher.py", "src/deskpal/execution/protocol.py",
        "src/deskpal/execution/__init__.py", "src/deskpal/execution/windows/__init__.py", "src/deskpal/__init__.py")
    return {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}


def _baseline(snapshot: dict[str, object], user: str) -> list[bytes]:
    acl._denied_sid("", user, deny_target="user")  # Includes the fixed service-user refusal.
    metadata, raw_hex = snapshot.get("metadata"), snapshot.get("raw_aces_hex")
    if (not isinstance(metadata, dict) or metadata.get("owner_sid") != user
            or metadata.get("dacl_null") is not False or metadata.get("dacl_present") is not True
            or type(metadata.get("dacl_protected")) is not bool or metadata.get("unparsed_ace_count") != 0
            or not isinstance(metadata.get("aces"), list) or not metadata["aces"]
            or not isinstance(raw_hex, list) or not 1 <= len(raw_hex) <= 512
            or not all(isinstance(item, str) for item in raw_hex) or snapshot.get("acl_revision") not in (2, 4)):
        raise ValueError("leaf_control_ordinary_baseline_invalid")
    parsed = metadata["aces"]
    entries = [bytes.fromhex(str(item)) for item in raw_hex]
    if len(entries) != len(parsed):
        raise ValueError("leaf_control_baseline_raw_mismatch")
    for raw, entry in zip(entries, parsed, strict=True):
        if (not isinstance(entry, dict) or entry.get("type") not in (0, 1)
                or not isinstance(entry.get("sid"), str)
                or raw != acl._ace(int(entry["type"]), int(entry["flags"]), int(entry["mask"]), str(entry["sid"]))):
            raise ValueError("leaf_control_baseline_raw_mismatch")
        sid = str(entry["sid"])
        if sid == "S-1-3-4" or sid == "S-1-15-2" or sid.startswith("S-1-15-2-"):
            raise ValueError("leaf_control_package_or_owner_rights_refused")
    for sid in (user, "S-1-5-18"):
        if not any(entry.get("sid") == sid and entry.get("type") == 0 and entry.get("mask") == 0xF003F
                   and not int(entry.get("flags", 0)) & 8 for entry in parsed):
            raise ValueError("leaf_control_user_or_system_full_allow_missing")
    return entries


def _acl_buffer(revision: int, entries: list[bytes]) -> bytes:
    size = 8 + sum(map(len, entries))
    if revision not in (2, 4) or size > 65535 or len(entries) > 514:
        raise ValueError("leaf_control_acl_budget")
    return struct.pack("<BBHHH", revision, 0, size, len(entries), 0) + b"".join(entries)


def _relative_descriptor(planned_acl: bytes, protected: bool) -> bytes:
    """Owned SECURITY_DESCRIPTOR_RELATIVE containing only an exact DACL.

    The selected API's handling of protection flags is an observation question;
    this construction does not establish that Windows will apply those flags.
    """
    if not isinstance(planned_acl, bytes) or type(protected) is not bool or not 8 <= len(planned_acl) <= 8 + MAX_ACCEPTED_RAW_BYTES:
        raise ValueError("leaf_control_relative_acl_budget_or_type")
    revision, sbz1, size, count, sbz2 = struct.unpack_from("<BBHHH", planned_acl)
    if revision not in (2, 4) or sbz1 or sbz2 or size != len(planned_acl) or not 1 <= count <= MAX_ACCEPTED_ACES:
        raise ValueError("leaf_control_relative_acl_header")
    offset = 8
    for _ in range(count):
        if offset + 16 > len(planned_acl):
            raise ValueError("leaf_control_relative_ace_short")
        kind, _flags, ace_size = struct.unpack_from("<BBH", planned_acl, offset)
        sid_revision, sid_count = planned_acl[offset + 8:offset + 10]
        if (kind not in (0, 1) or sid_revision != 1 or sid_count > 15
                or ace_size != 16 + 4 * sid_count or ace_size % 4 or offset + ace_size > len(planned_acl)):
            raise ValueError("leaf_control_relative_ace_shape")
        offset += ace_size
    if offset != len(planned_acl):
        raise ValueError("leaf_control_relative_acl_trailing_bytes")
    control = 0x8000 | 0x4 | (0x1000 if protected else 0)
    header = struct.pack("<BBHIIII", 1, 0, control, 0, 0, 0, 20)
    if len(header) != 20:
        raise ValueError("leaf_control_relative_descriptor_layout")
    return header + planned_acl


def _plan(snapshot: dict[str, object], user: str) -> tuple[bytes, list[str]]:
    explicit: list[bytes] = []
    inherited: list[bytes] = []
    allow_seen = False
    for entry in _baseline(snapshot, user):
        if entry[1] & 0x10:
            if entry[1] & 0x20:
                raise ValueError("leaf_control_critical_inherited_conversion_refused")
            inherited.append(entry[:1] + bytes((entry[1] & ~0x10,)) + entry[2:])
        else:
            if inherited or (entry[0] == 1 and allow_seen):
                raise ValueError("leaf_control_original_order_not_canonical")
            allow_seen |= entry[0] == 0
            explicit.append(entry)
    entries = [acl._ace(1, 2, acl.DENY_MASK, user), *explicit,
               acl._ace(0, 2, 0x20000, "S-1-3-4"), *inherited]
    allow_seen = False
    for entry in entries:
        if entry[1] & 0x10 or (entry[0] == 1 and allow_seen):
            raise ValueError("leaf_control_converted_order_not_canonical")
        allow_seen |= entry[0] == 0
    return _acl_buffer(int(str(snapshot["acl_revision"])), entries), [entry.hex() for entry in entries]


def _protected_baseline_plan(snapshot: dict[str, object], user: str) -> tuple[bytes, list[str], dict[str, object]]:
    originals = _baseline(snapshot, user)
    entries: list[bytes] = []
    converted: list[int] = []
    inherited_seen = allow_seen = False
    for index, original in enumerate(originals):
        inherited = bool(original[1] & 0x10)
        if inherited:
            if original[1] & 0x20:
                raise ValueError("leaf_control_critical_inherited_conversion_refused")
            inherited_seen = True
            converted.append(index)
        elif inherited_seen:
            raise ValueError("leaf_control_original_order_not_canonical")
        entry = original[:1] + bytes((original[1] & ~0x10,)) + original[2:]
        if entry[0] == 1 and allow_seen:
            raise ValueError("leaf_control_converted_order_not_canonical")
        allow_seen |= entry[0] == 0
        entries.append(entry)
    expected = [entry.hex() for entry in entries]
    return _acl_buffer(int(str(snapshot["acl_revision"])), entries), expected, {
        "inherited_conversion_original_indices": converted,
        "only_inherited_bit_cleared_in_plan": True, "critical_aces_preserved_in_plan": True,
        "original_order_preserved_in_plan": True,
        "original_ace_plan": [{"original_index": index, "planned_index": index,
            "original_ace_hex": original.hex(), "planned_ace_hex": entry.hex(),
            "inherited_bit_cleared": index in converted, "critical": bool(original[1] & 0x20)}
            for index, (original, entry) in enumerate(zip(originals, entries, strict=True))]}


def _granted(leaf: _Leaf, phase: str) -> None:
    leaf.require()
    if phase in leaf.grants:
        raise ValueError("leaf_control_grant_query_repeat_refused")
    audit: dict[str, object] = {"status": "unknown", "information_class": 0,
        "structure_bytes": C.sizeof(access.PublicObjectBasicInformation), "returned_length": None,
        "raw_ntstatus": None, "attributes": None, "granted_access": None, "gate_passed": False}
    leaf.grants[phase] = audit
    length = C.c_uint32()
    try:
        structure = access.PublicObjectBasicInformation
        if C.sizeof(structure) != 56 or structure.Attributes.offset != 0 or structure.GrantedAccess.offset != 4:
            raise ValueError("leaf_control_public_basic_layout")
        result = structure()
        status = int(leaf.api.query_object(leaf.key.handle, 0, C.byref(result), 56, C.byref(length)))
        audit.update(raw_ntstatus=status, ntstatus_hex=f"0x{status & 0xFFFFFFFF:08x}", returned_length=length.value)
        if status != 0 or length.value != 56:
            raise ValueError("leaf_control_grant_status_or_length")
        granted = int(result.GrantedAccess)
        rc, wd = bool(granted & 0x20000), bool(granted & 0x40000)
        audit.update(status="observed", attributes=int(result.Attributes), granted_access=granted,
                     granted_access_hex=f"0x{granted:08x}", read_control=rc, write_dac=wd, gate_passed=rc and wd)
        if not (rc and wd):
            raise ValueError("leaf_control_actual_rc_or_wd_missing")
    except BaseException as error:
        audit.update(returned_length=length.value, error_type=type(error).__name__, error=str(error)[:2048])
        raise


def _probe(leaf: _Leaf, phase: str) -> None:
    leaf.require()
    if phase in leaf.probes:
        raise ValueError("leaf_control_probe_repeat_refused")
    owner = _KeyOwner(leaf.api, "probe_" + phase)
    leaf.keys.append(owner)
    leaf.session.handles.append(owner)
    audit: dict[str, object] = {"api": "RegOpenKeyExW", "requested_access": 2,
        "status": None, "gate_passed": False, "direct_close_confirmed": False}
    leaf.probes[phase] = audit
    try:
        try:
            status = int(leaf.api.open_key(leaf.parent.handle, leaf.name, 0, 2, C.byref(owner.handle)))
        finally:
            audit["handle_returned_after_call"] = bool(owner.handle.value)
        audit.update(status=status, handle_returned=bool(owner.handle.value))
        if phase in ("before_deny", "after_protected_restore"):
            acl._status(status, "leaf_control_positive_open")
            owner.require()
            owner.close()
            audit["direct_close_confirmed"] = True
        elif phase != "after_deny" or status != 5 or owner.handle.value:
            raise ValueError("leaf_control_host_open_not_cleanly_denied")
        audit["gate_passed"] = True
    except BaseException as error:
        audit.update(error_type=type(error).__name__, error=str(error)[:2048])
        raise


def _snapshot(leaf: _Leaf) -> dict[str, object]:
    result = acl._snapshot(leaf.api, leaf.key.handle, "", leaf.user)
    raw = result.get("raw_aces_hex")
    count = len(raw) if isinstance(raw, list) else None
    hard_valid = (isinstance(raw, list) and len(raw) <= 512
        and all(isinstance(item, str) for item in raw)
        and sum(len(str(item)) for item in raw) <= 2 * readonly.MAX_SD_BYTES)
    raw_bytes: int | None = None
    if hard_valid:
        try:
            raw_bytes = sum(len(bytes.fromhex(str(item))) for item in cast(list[str], raw))
        except ValueError:
            hard_valid = False
    if (not hard_valid or count is None or raw_bytes is None
            or count > MAX_ACCEPTED_ACES or raw_bytes > MAX_ACCEPTED_RAW_BYTES):
        # Keep one complete bounded raw rejection, without duplicating parsed ACEs.
        if "rejected_security_descriptor" not in leaf.report:
            metadata = result.get("metadata")
            summary: dict[str, object] = {}
            if isinstance(metadata, dict):
                for field in ("owner_sid", "owner_matches_subject_user", "sd_bytes", "control", "revision",
                              "dacl_present", "dacl_null", "dacl_protected", "dacl_sha256"):
                    value = metadata.get(field)
                    summary[field] = value if value is None or type(value) in (bool, int) or (
                        isinstance(value, str) and len(value) <= 256) else "invalid_summary_field"
            leaf.report["rejected_security_descriptor"] = {
                "status": "rejected_input_budget" if hard_valid else "unknown_hard_bound_or_raw_shape",
                "raw_evidence_complete": hard_valid, "ace_count": count, "raw_ace_bytes": raw_bytes,
                "acl_revision": result.get("acl_revision") if type(result.get("acl_revision")) is int else None,
                "metadata_summary": summary,
                "raw_aces_hex": list(cast(list[str], raw)) if hard_valid else None,
            }
        leaf.session.mark_unknown("leaf_control_security_descriptor_budget_refused")
        raise ValueError("leaf_control_security_descriptor_budget_refused")
    return result


def _host(leaf: _Leaf, field: str) -> str:
    observation: dict[str, object] = {}
    try:
        user = access._host_context(leaf.session, observation)
    finally:
        leaf.report[field] = observation.get("host_context", {"status": "unknown"})
    context = cast(dict[str, object], leaf.report[field])
    context["leaf_context_gate_passed"] = False
    process = cast(dict[str, object], context["process"])
    if context.get("effective_source") != "process" or process.get("is_appcontainer") != 0:
        raise ValueError("leaf_control_requires_non_ac_process_effective_context")
    acl._denied_sid("", user, deny_target="user")
    if field != "host_context":
        context["matches_initial_user"] = user == leaf.user
        if user != leaf.user:
            raise ValueError("leaf_control_effective_user_changed_before_restore")
    context["leaf_context_gate_passed"] = True
    return user


def _mutate(leaf: _Leaf, phase: str, output: TextIO) -> None:
    leaf.require()
    if phase not in ("apply", "restore_protected", "restore") or phase in leaf.report:
        raise ValueError("leaf_control_mutation_repeat_or_phase_refused")
    audit: dict[str, object] = {"state": "before_query", "set_attempted": False, "set_returned": False,
        "raw_set_status": None, "before_snapshot_obtained": False, "actual_snapshot_obtained": False,
        "gate_passed": False, "post_failed_set_observation": None}
    low_level = phase != "apply" and leaf.restore_setter == "reg-set-key-security"
    audit.update(setter="reg-set-key-security" if low_level else "set-security-info",
                 api="RegSetKeySecurity" if low_level else "SetSecurityInfo")
    leaf.report[phase] = audit
    try:
        current = _snapshot(leaf)
        audit.update(before=current, before_snapshot_obtained=True)
        baseline = leaf.before if phase == "apply" else (
            leaf.protected_baseline if phase == "restore" and leaf.restore_mode == "protected-then-original" else leaf.applied)
        if not baseline or current != baseline:
            raise ValueError("leaf_control_sd_changed_before_" + phase)
        audit["state"] = "input_planning"
        if phase == "apply":
            if not leaf.grants.get("before_deny", {}).get("gate_passed") or not leaf.probes.get("before_deny", {}).get("gate_passed"):
                raise ValueError("leaf_control_positive_prerequisites_missing")
            buffer_bytes, expected = _plan(leaf.before, leaf.user)
            audit.update(acl._input_plan_audit(leaf.before, buffer_bytes, expected, "", user=leaf.user, deny_target="user"))
            information, protected = 0x80000004, True
        else:
            if (not leaf.applied or not leaf.grants.get("after_deny_before_restore", {}).get("gate_passed")
                    or not leaf.probes.get("after_deny", {}).get("gate_passed")
                    or not cast(dict[str, object], leaf.report.get("host_context_before_restore", {})).get("leaf_context_gate_passed")):
                raise ValueError("leaf_control_restore_prerequisites_missing")
            if phase == "restore_protected":
                if leaf.restore_mode != "protected-then-original" or leaf.protected_baseline:
                    raise ValueError("leaf_control_protected_restore_mode_or_repeat_refused")
                buffer_bytes, expected, plan_audit = _protected_baseline_plan(leaf.before, leaf.user)
                audit.update(plan_audit)
                protected, information = True, 0x80000004
            else:
                if leaf.restore_mode == "protected-then-original" and (
                        not leaf.protected_baseline
                        or not cast(dict[str, object], leaf.report.get("restore_protected", {})).get("gate_passed")
                        or not leaf.grants.get("after_protected_before_restore", {}).get("gate_passed")
                        or not leaf.probes.get("after_protected_restore", {}).get("gate_passed")
                        or not cast(dict[str, object], leaf.report.get("host_context_after_protected_restore", {})).get("leaf_context_gate_passed")):
                    raise ValueError("leaf_control_final_restore_prerequisites_missing")
                entries = _baseline(leaf.before, leaf.user)
                buffer_bytes = _acl_buffer(int(str(leaf.before["acl_revision"])), entries)
                expected = [entry.hex() for entry in entries]
                protected = cast(dict[str, object], leaf.before["metadata"])["dacl_protected"] is True
                information = 0x80000004 if protected else 0x20000004
        audit.update(state="planned", planned_acl_hex=buffer_bytes.hex(), security_information=information,
                     original=leaf.before, expected_raw_aces_hex=expected,
                     expected_original=leaf.before if phase == "restore" else None,
                     expected_readback={"owner_sid": leaf.user, "dacl_protected": protected,
                         "dacl_null": False, "acl_revision": leaf.before["acl_revision"], "raw_aces_hex": expected})
        if low_level:
            descriptor = _relative_descriptor(buffer_bytes, protected)
            audit.update(planned_security_descriptor_hex=descriptor.hex(),
                         planned_security_descriptor_bytes=len(descriptor),
                         relative_descriptor_owner_group_sacl_offsets_zero=True)
            buffer = C.create_string_buffer(descriptor, len(descriptor))
        else:
            buffer = C.create_string_buffer(buffer_bytes)
        audit.update(state="set_prepared", prepared_record_does_not_establish_call_started=True)
        readonly._save(output, leaf.report)
        audit.update(state="set_call", set_attempted=True)
        try:
            if low_level:
                status = int(leaf.api.reg_set_key_security(leaf.key.handle, information, buffer))
            else:
                status = int(leaf.api.set_security(leaf.key.handle, 4, information, None, None, buffer, None))
            audit.update(state="set_returned", set_returned=True, raw_set_status=status)
            acl._status(status, "leaf_control_" + phase + "_dacl")
        except BaseException:
            try:
                observed = _snapshot(leaf)
                audit["post_failed_set_observation"] = {"status": "observed", "snapshot": observed,
                    "matches_before_call": observed == current, "matches_original": observed == leaf.before}
            except BaseException as observation_error:
                audit["post_failed_set_observation"] = {"status": "unknown",
                    "error_type": type(observation_error).__name__, "error": str(observation_error)[:1024]}
            raise
        audit["state"] = "readback_query"
        actual = _snapshot(leaf)
        audit.update(state="readback_obtained", actual=actual, actual_snapshot_obtained=True)
        metadata = actual.get("metadata")
        checks = {"owner_matches": isinstance(metadata, dict) and metadata.get("owner_sid") == leaf.user,
            "protection_matches": isinstance(metadata, dict) and metadata.get("dacl_protected") is protected,
            "dacl_nonnull": isinstance(metadata, dict) and metadata.get("dacl_present") is True
                and metadata.get("dacl_null") is False,
            "revision_matches": actual.get("acl_revision") == leaf.before.get("acl_revision"),
            "raw_aces_match": actual.get("raw_aces_hex") == expected}
        if phase == "restore":
            checks["snapshot_matches_original"] = actual == leaf.before
        audit["gate_checks"] = checks
        if not all(checks.values()):
            raise ValueError("leaf_control_exact_" + phase + "_gate_failed")
        audit.update(state="verified", gate_passed=True)
        if phase == "apply":
            leaf.applied = actual
        elif phase == "restore_protected":
            leaf.protected_baseline = actual
            leaf.report["verified_protected_baseline"] = actual
        else:
            leaf.restored = True
            leaf.report["restored"] = True
    except BaseException as error:
        audit.update(failed_at=audit["state"], state="failed", error_type=type(error).__name__, error=str(error)[:2048])
        raise


def _source_end(report: dict[str, object]) -> bool:
    ending = _fingerprints()
    report["source_sha256_end"] = ending
    same = report.get("source_sha256") == ending
    report["source_unchanged"] = same
    return same


def _experiment(output: TextIO, report: dict[str, object]) -> int:
    restore_mode = report.get("restore_mode", "original")
    if restore_mode not in ("original", "protected-then-original"):
        raise ValueError("leaf_control_restore_mode_invalid")
    restore_setter = report.get("restore_setter", "set-security-info")
    if restore_setter not in ("set-security-info", "reg-set-key-security"):
        raise ValueError("leaf_control_restore_setter_invalid")
    if os.name != "nt" or C.sizeof(C.c_void_p) != 8:
        raise ValueError("leaf_control_windows_x64_only")
    api = LeafApi()
    session = acl.Session(api)  # Ownership container only; never create/adopt a profile or BoundRoot.
    leaf = _Leaf(api, session, report, str(restore_mode), str(restore_setter))
    code = 2
    report["cleanup"] = "pending"
    try:
        leaf.user = _host(leaf, "host_context")
        readonly._save(output, report)
        parent_audit: dict[str, object] = {"api": "RegOpenKeyExW", "requested_access": 4, "status": None}
        report["parent_open"] = parent_audit
        try:
            status = int(api.open_key(_HKCU, "Software", 0, 4, C.byref(leaf.parent.handle)))
        finally:
            parent_audit["handle_returned_after_call"] = bool(leaf.parent.handle.value)
        parent_audit["status"] = status
        acl._status(status, "leaf_control_open_existing_software")
        leaf.parent.require()
        disposition = W.DWORD()
        create: dict[str, object] = {"api": "RegCreateKeyExW", "options": 1, "requested_access": 0x60000,
                                   "security_attributes_null": True, "status": None, "disposition": None}
        report["leaf_create"] = create
        try:
            status = int(api.create_key(leaf.parent.handle, leaf.name, 0, None, 1, 0x60000, None,
                                        C.byref(leaf.key.handle), C.byref(disposition)))
        finally:
            create.update(disposition=disposition.value, handle_returned_after_call=bool(leaf.key.handle.value))
        create.update(status=status, disposition=disposition.value)
        acl._status(status, "leaf_control_create_volatile_leaf")
        if disposition.value != 1:
            raise ValueError("leaf_control_existing_leaf_no_mutation_or_delete")
        leaf.created = True
        leaf.require()
        leaf.before = _snapshot(leaf)
        report["original"] = leaf.before
        _baseline(leaf.before, leaf.user)
        _granted(leaf, "before_deny")
        _probe(leaf, "before_deny")
        _mutate(leaf, "apply", output)
        readonly._save(output, report)
        _granted(leaf, "after_deny_before_restore")
        _probe(leaf, "after_deny")
        _host(leaf, "host_context_before_restore")
        if leaf.restore_mode == "protected-then-original":
            _mutate(leaf, "restore_protected", output)
            readonly._save(output, report)
            _host(leaf, "host_context_after_protected_restore")
            _probe(leaf, "after_protected_restore")
            _granted(leaf, "after_protected_before_restore")
        _mutate(leaf, "restore", output)
        report["status"] = "ordinary_volatile_leaf_control_restored"
        code = 0
    except BaseException as error:
        session.mark_unknown("leaf_control:" + type(error).__name__ + ":" + str(error))
        report.update(status="leaf_control_blocked", error_type=type(error).__name__, error=str(error)[:2048])
        if not isinstance(error, Exception):
            raise
    finally:
        try:
            if not _source_end(report):
                session.mark_unknown("leaf_control_source_changed")
        except BaseException as error:
            session.mark_unknown("leaf_control_source_end_unknown")
            report.update(source_unchanged=False, source_end_error_type=type(error).__name__, source_end_error=str(error)[:2048])
        # Close every token/probe/leaf before considering deletion; retain only parent.
        safe = session.close_resources()
        deletion: dict[str, object] = {"api": "RegDeleteKeyW", "attempted": False, "status": None}
        report["leaf_delete"] = deletion
        try:
            if code == 0 and safe and leaf.created and leaf.restored and not any(owner.handle.value for owner in session.handles):
                leaf.parent.require()
                deletion["attempted"] = True
                status = int(api.delete_key(leaf.parent.handle, leaf.name))
                deletion["status"] = status
                acl._status(status, "leaf_control_delete_exact_new_leaf")
                report["cleanup"] = "leaf_deleted"
            else:
                report["cleanup"] = "withheld_unknown_or_unrestored_leaf"
                code = 2
        except BaseException as error:
            session.mark_unknown("leaf_delete:" + type(error).__name__ + ":" + str(error))
            report.update(cleanup="failed_leaf_may_remain", cleanup_error=str(error)[:2048])
            code = 2
        finally:
            try:
                leaf.parent.close()
            except BaseException as error:
                session.resource_close_unconfirmed = True
                acl._UNCONFIRMED.append(leaf.parent)
                session.mark_unknown("parent_close:" + type(error).__name__ + ":" + str(error), resource=True)
                report.update(cleanup="parent_close_unconfirmed_leaf_may_remain", parent_close_error=str(error)[:2048])
                code = 2
            report["parent_handle_close"] = leaf.parent.record()
        report["key_handle_closes"] = [owner.record() for owner in leaf.keys]
        report["cleanup_state"] = session.cleanup_state()
        report["leaf_may_remain"] = report["cleanup"] != "leaf_deleted"
        readonly._save(output, report)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--experiment", action="store_true")
    parser.add_argument("--restore-mode", choices=("original", "protected-then-original"), default="original")
    parser.add_argument("--restore-setter", choices=("set-security-info", "reg-set-key-security"), default="set-security-info")
    args = parser.parse_args()
    path = readonly._new_report(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    readonly._reject_reparse(path)
    report: dict[str, object] = {"stage0_passed": False, "cleanup": "not_required", "profile_created": False,
        "worker_code_executed": False, "registry_values_read": False, "registry_values_written": False,
        "registry_names_enumerated": False, "fallback_allowed": False, "restore_mode": args.restore_mode,
        "restore_setter": args.restore_setter,
        "individual_adapter_handle_closes_verified": False, "popen_own_process_handle_verified": False,
        "scope": "One new ordinary-host volatile leaf; one host KEY_SET_VALUE open probe before and after deny.",
        "limits": "No AC isolation acceptance, no conclusions about retained roots or propagation timing; RegSetKeySecurity is not a recursive-root cleanup proof."}
    with path.open("x", encoding="ascii") as output:
        if not args.experiment:
            report["status"] = "not_executed_explicit_opt_in_required"
            readonly._save(output, report)
            return 0
        code = 2
        try:
            report["source_sha256"] = _fingerprints()
            code = _experiment(output, report)
        except Exception as error:
            report.update(status="leaf_control_blocked", error_type=type(error).__name__, error=str(error)[:2048])
        if "source_sha256_end" not in report:
            try:
                _source_end(report)
            except Exception as error:
                report.update(source_unchanged=False, source_end_error_type=type(error).__name__, source_end_error=str(error)[:2048])
        if report.get("source_unchanged") is not True:
            code = 2
        readonly._save(output, report)
    print(json.dumps({"report": str(path), "diagnostic_exit": code, "stage0_passed": False}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
