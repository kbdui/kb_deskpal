"""Opt-in ordinary-AC registry DACL experiment on ONE fresh project profile.

The outer host owns the profile, Jobs, helper, and a noninheritable registry
management handle. No registry path, SID, PID or HKEY is accepted by the public
CLI. Values/names are never queried/enumerated. Synthetic values are eight bytes
per branch. This is not whole-namespace or stage-zero acceptance.
"""

from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
from dataclasses import asdict
from hashlib import sha256
import inspect
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
from time import monotonic, sleep
from typing import Any, NoReturn, TextIO
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import diagnose_worker_registry as readonly  # noqa: E402
from deskpal.execution.runtime import (  # noqa: E402
    ProbeProfile, RuntimeManifest, apply_readonly_dacl, create_probe_profile,
    delete_probe_profile, stage_runtime, verify_manifest, worker_environment,
)
from deskpal.execution.windows._win32 import _CREATE_LOCK, _create_worker  # noqa: E402
from deskpal.execution.windows.launcher import (  # noqa: E402
    CleanupOwner, LaunchRejected, LaunchSpec, WindowsLauncher,
)

MANAGEMENT_ACCESS = 0x60000  # READ_CONTROL | WRITE_DAC; never inherit to workers.
DENY_MASK = 0xD0026
CANARY = b"SYNTH06!"
MAX_MESSAGE = 128 * 1024
MAX_CLEANUP_RECORDS = 128
MAX_UNKNOWN_REASONS = 64
_UNCONFIRMED: list[object] = []
_SESSIONS: set[Session] = set()
_BINDINGS: set[BoundRoot] = set()
_MARKER = re.compile(r"DeskpalV06_[0-9a-f]{32}_[a-z]+\Z", re.ASCII)


class WriteApi(readonly.RegistryApi):
    """Separate experimental write bindings; readonly.RegistryApi is untouched."""

    def __init__(self) -> None:
        super().__init__()
        advapi = C.WinDLL("advapi32", use_last_error=True)
        kernel = C.WinDLL("kernel32", use_last_error=True)
        bind = readonly._bind
        self.open_key = bind(advapi, "RegOpenKeyExW",
            [W.HKEY, W.LPCWSTR, W.DWORD, W.DWORD, C.POINTER(W.HKEY)], C.c_int32)
        self.create_key = bind(advapi, "RegCreateKeyExW",
            [W.HKEY, W.LPCWSTR, W.DWORD, W.LPWSTR, W.DWORD, W.DWORD,
             C.c_void_p, C.POINTER(W.HKEY), C.POINTER(W.DWORD)], C.c_int32)
        self.set_value = bind(advapi, "RegSetValueExW",
            [W.HKEY, W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD], C.c_int32)
        self.set_security = bind(advapi, "SetSecurityInfo",
            [W.HANDLE, C.c_int, W.DWORD, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p], W.DWORD)
        self.open_process = bind(kernel, "OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE)
        self.current_process = bind(kernel, "GetCurrentProcess", [], W.HANDLE)
        self.duplicate_handle = bind(kernel, "DuplicateHandle",
            [W.HANDLE, W.HANDLE, W.HANDLE, C.POINTER(W.HANDLE), W.DWORD, W.BOOL, W.DWORD], W.BOOL)
        self.peek = bind(kernel, "PeekNamedPipe",
            [W.HANDLE, C.c_void_p, W.DWORD, C.c_void_p, C.POINTER(W.DWORD), C.c_void_p], W.BOOL)


def _status(status: int, stage: str) -> None:
    if status != 0:
        raise OSError(status, stage)


class HandleOwner:
    def __init__(self, api: WriteApi, *, key: bool = False) -> None:
        self.api, self.key = api, key
        self.handle = W.HANDLE()

    def close(self) -> None:
        if self.handle.value:
            if self.key:
                _status(int(self.api.reg_close(self.handle)), "acl_reg_close")
            else:
                readonly._require(self.api.close(self.handle), "acl_handle_close")
            self.handle.value = None


def _sid_bytes(value: str) -> bytes:
    parts = value.split("-")
    if len(parts) < 4 or parts[:2] != ["S", "1"] or len(parts) - 3 > 15:
        raise ValueError("acl_sid_shape")
    authority = int(parts[2])
    sub = [int(item) for item in parts[3:]]
    if not 0 <= authority < 2**48 or any(not 0 <= item < 2**32 for item in sub):
        raise ValueError("acl_sid_range")
    return bytes((1, len(sub))) + authority.to_bytes(6, "big") + struct.pack("<" + "I" * len(sub), *sub)


def _ace(kind: int, flags: int, mask: int, sid: str) -> bytes:
    subject = _sid_bytes(sid)
    return struct.pack("<BBHI", kind, flags, 8 + len(subject), mask) + subject


def _raw_aces(api: WriteApi, key: W.HANDLE) -> tuple[int, list[bytes]]:
    descriptor, dacl = C.c_void_p(), C.c_void_p()
    try:
        _status(int(api.get_security(key, 4, 4, None, None, C.byref(dacl), None,
                                     C.byref(descriptor))), "acl_raw_security")
        if not descriptor.value or not dacl.value:
            raise ValueError("acl_missing_descriptor_or_dacl")
        size = int(api.sd_length(descriptor))
        if not 20 <= size <= readonly.MAX_SD_BYTES:
            raise ValueError("acl_sd_budget")
        control, revision = C.c_uint16(), W.DWORD()
        readonly._require(api.sd_control(descriptor, C.byref(control), C.byref(revision)), "acl_sd_control")
        if not control.value & 0x8000:
            raise ValueError("acl_sd_not_relative")
        start, end = descriptor.value, descriptor.value + size
        if not start <= dacl.value <= end - 8:
            raise ValueError("acl_pointer_outside_sd")
        acl_size = C.c_uint16.from_address(dacl.value + 2).value
        count = C.c_uint16.from_address(dacl.value + 4).value
        if not 8 <= acl_size <= end - dacl.value or count > 512:
            raise ValueError("acl_raw_budget")
        entries: list[bytes] = []
        for index in range(count):
            pointer = C.c_void_p()
            readonly._require(api.get_ace(dacl, index, C.byref(pointer)), "acl_raw_get_ace")
            address = int(pointer.value or 0)
            if not dacl.value + 8 <= address <= dacl.value + acl_size - 4:
                raise ValueError("acl_raw_ace_pointer")
            length = C.c_uint16.from_address(address + 2).value
            if length < 16 or address + length > dacl.value + acl_size:
                raise ValueError("acl_raw_ace_length")
            entries.append(C.string_at(address, length))
        return C.c_ubyte.from_address(dacl.value).value, entries
    finally:
        if descriptor.value:
            api.local_free(descriptor)


def _snapshot(api: WriteApi, key: W.HANDLE, package: str, user: str) -> dict[str, object]:
    metadata = readonly._security_metadata(api, key, package, user)
    revision, entries = _raw_aces(api, key)
    parsed = metadata["aces"]
    if not isinstance(parsed, list) or len(parsed) != len(entries) or metadata["unparsed_ace_count"] != 0:
        raise ValueError("acl_unknown_ace_blocks_mutation")
    if revision not in (2, 4) or any(raw[0] not in (0, 1) for raw in entries):
        raise ValueError("acl_unknown_ace_blocks_mutation")
    for raw, entry in zip(entries, parsed, strict=True):
        if (not isinstance(entry, dict) or raw != _ace(int(entry["type"]), int(entry["flags"]),
                int(entry["mask"]), str(entry["sid"]))):
            raise ValueError("acl_metadata_and_raw_snapshot_disagree")
    return {"metadata": metadata, "acl_revision": revision,
            "raw_aces_hex": [raw.hex() for raw in entries]}


def _baseline(snapshot: dict[str, object], package: str, user: str) -> None:
    metadata = snapshot.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("owner_sid") != user:
        raise ValueError("acl_owner_not_fresh_subject_user")
    entries = metadata.get("aces")
    if not isinstance(entries, list) or metadata.get("dacl_null") is not False:
        raise ValueError("acl_baseline_missing")
    package_entries = [entry for entry in entries if isinstance(entry, dict) and entry.get("sid") == package]
    if len(package_entries) != 1 or any(package_entries[0].get(key) != value
            for key, value in (("type", 0), ("flags", 0x23), ("mask", 0xF003F))):
        raise ValueError("acl_critical_package_allow_unexpected")
    for subject in (user, "S-1-5-18"):
        if not any(isinstance(entry, dict) and entry.get("sid") == subject and entry.get("type") == 0
                   and entry.get("mask") == 0xF003F and not int(entry.get("flags", 0)) & 8 for entry in entries):
            raise ValueError("acl_host_or_system_full_access_missing")
    if metadata.get("unparsed_ace_count") != 0:
        raise ValueError("acl_unknown_ace_blocks_mutation")
    if any(isinstance(entry, dict) and isinstance(entry.get("sid"), str)
           and ((str(entry["sid"]).startswith("S-1-15-2-") and entry["sid"] != package)
                or entry["sid"] == "S-1-3-4") for entry in entries):
        raise ValueError("acl_other_package_or_existing_owner_rights")


def _validate_deny_target(deny_target: str) -> str:
    if deny_target not in ("package", "user"):
        raise ValueError("acl_deny_target_invalid")
    return deny_target


def _validate_cleanup_restore(deny_target: str, restore_for_cleanup: bool) -> None:
    _validate_deny_target(deny_target)
    if type(restore_for_cleanup) is not bool or (restore_for_cleanup and deny_target != "user"):
        raise ValueError("acl_cleanup_restore_requires_user_deny")


def _new_restore_audit(enabled: bool) -> dict[str, object]:
    return {"enabled": enabled, "state": "not_started" if enabled else "disabled",
            "set_attempted": False, "set_returned": False,
            "post_failed_set_observation": None,
            "actual_snapshot_obtained": False, "readback_gate_passed": False,
            "individual_adapter_handle_closes_verified": False,
            "popen_own_process_handle_verified": False}


def _denied_sid(package: str, user: str, *, deny_target: str = "package") -> str:
    _validate_deny_target(deny_target)
    if deny_target == "package":
        return package
    # Compare SID bytes so alternate decimal spellings cannot select a service.
    subject = _sid_bytes(user)
    if subject in tuple(_sid_bytes("S-1-5-" + str(rid)) for rid in (18, 19, 20)):
        raise ValueError("acl_service_user_deny_refused")
    return user


def _plan(snapshot: dict[str, object], package: str, user: str, *,
          deny_target: str = "package") -> tuple[bytes, list[str]]:
    denied_sid = _denied_sid(package, user, deny_target=deny_target)
    _baseline(snapshot, package, user)
    raw_hex = snapshot.get("raw_aces_hex")
    if not isinstance(raw_hex, list) or not all(isinstance(item, str) for item in raw_hex):
        raise ValueError("acl_raw_aces_missing")
    entries = [bytes.fromhex(str(item)) for item in raw_hex]
    metadata = snapshot["metadata"]
    if not isinstance(metadata, dict) or not isinstance(metadata.get("aces"), list):
        raise ValueError("acl_plan_metadata_missing")
    parsed = metadata["aces"]
    if len(entries) != len(parsed) or any(
        not isinstance(item, dict) or raw != _ace(int(item["type"]), int(item["flags"]),
            int(item["mask"]), str(item["sid"]))
        for raw, item in zip(entries, parsed, strict=True)
    ):
        raise ValueError("acl_plan_metadata_raw_mismatch")
    explicit: list[bytes] = []
    inherited: list[bytes] = []
    allow_seen = False
    for entry in entries:
        if len(entry) < 16 or entry[0] not in (0, 1) or struct.unpack_from("<H", entry, 2)[0] != len(entry):
            raise ValueError("acl_unknown_or_malformed_ace")
        if entry[1] & 0x10:
            if entry[1] & 0x20:
                raise ValueError("acl_critical_inherited_conversion_refused")
            # Explicit protected-DACL input policy, never post-readback repair.
            inherited.append(entry[:1] + bytes((entry[1] & ~0x10,)) + entry[2:])
        else:
            if inherited or (entry[0] == 1 and allow_seen):
                raise ValueError("acl_original_order_not_canonical")
            allow_seen |= entry[0] == 0
            explicit.append(entry)
    result = [_ace(1, 2, DENY_MASK, denied_sid), *explicit,
              _ace(0, 2, 0x20000, "S-1-3-4"), *inherited]
    allow_seen = False
    for entry in result:
        if entry[1] & 0x10 or (entry[0] == 1 and allow_seen):
            # A converted inherited deny is not silently moved before allows.
            raise ValueError("acl_converted_order_not_canonical")
        allow_seen |= entry[0] == 0
    size = 8 + sum(map(len, result))
    if size > 65535 or len(result) > 514:
        raise ValueError("acl_plan_budget")
    revision = int(str(snapshot["acl_revision"]))
    if revision not in (2, 4):
        raise ValueError("acl_plan_revision_invalid")
    return struct.pack("<BBHHH", revision, 0, size, len(result), 0) + b"".join(result), [item.hex() for item in result]


def _input_plan_audit(snapshot: dict[str, object], acl: bytes, planned_hex: list[str],
                      package: str, *, user: str = "", deny_target: str = "package") -> dict[str, object]:
    denied_sid = _denied_sid(package, user, deny_target=deny_target)
    original_hex = snapshot.get("raw_aces_hex")
    if not isinstance(original_hex, list) or not all(isinstance(item, str) for item in original_hex):
        raise ValueError("acl_raw_aces_missing")
    originals = [bytes.fromhex(str(item)) for item in original_hex]
    planned = [bytes.fromhex(item) for item in planned_hex]
    explicit_count = sum(not bool(item[1] & 0x10) for item in originals)
    if (len(planned) != len(originals) + 2 or planned[0] != _ace(1, 2, DENY_MASK, denied_sid)
            or planned[explicit_count + 1] != _ace(0, 2, 0x20000, "S-1-3-4")):
        raise ValueError("acl_plan_added_aces_changed")
    size = 8 + sum(map(len, planned))
    header = struct.pack("<BBHHH", int(str(snapshot["acl_revision"])), 0, size, len(planned), 0)
    if acl != header + b"".join(planned):
        raise ValueError("acl_plan_buffer_and_expected_disagree")
    records: list[dict[str, object]] = []
    converted_original: list[int] = []
    converted_planned: list[int] = []
    for index, original in enumerate(originals):
        inherited, critical = bool(original[1] & 0x10), bool(original[1] & 0x20)
        if inherited and critical:
            raise ValueError("acl_critical_inherited_conversion_refused")
        planned_index = index + (1 if index < explicit_count else 2)
        permitted = original[:1] + bytes((original[1] & ~0x10,)) + original[2:]
        if planned[planned_index] != permitted:
            raise ValueError("acl_plan_original_changed_outside_inherited_bit")
        if inherited:
            converted_original.append(index)
            converted_planned.append(planned_index)
        records.append({"original_index": index, "planned_index": planned_index,
            "original_ace_hex": original.hex(), "planned_ace_hex": planned[planned_index].hex(),
            "inherited_bit_cleared": inherited, "critical": critical})
    return {"deny_target": deny_target, "denied_sid": denied_sid, "original_ace_plan": records,
        "inherited_conversion_original_indices": converted_original,
        "inherited_conversion_planned_indices": converted_planned,
        "original_aces_preserved_in_plan": not converted_original,
        "critical_aces_preserved_in_plan": True,
        "only_inherited_bit_cleared_in_plan": True}


class Session:
    def __init__(self, api: WriteApi, *, deny_target: str = "package", restore_for_cleanup: bool = False) -> None:
        _validate_cleanup_restore(deny_target, restore_for_cleanup)
        self.deny_target = _validate_deny_target(deny_target)
        self.restore_for_cleanup = restore_for_cleanup
        self.verified_reaped_phases: set[str] = set()
        self.cleanup_restore_invoked = False
        self.cleanup_dacl_restore = _new_restore_audit(restore_for_cleanup)
        self.api = api
        self.profile: ProbeProfile | None = None
        self.root: BoundRoot | None = None
        self.owners: list[CleanupOwner] = []
        self.helpers: list[BindingHelperOwner] = []
        self.handles: list[HandleOwner] = []
        self.unknown = False
        self.evidence_unknown = False
        self.unknown_reasons: list[dict[str, str]] = []
        self.unknown_reasons_truncated = False
        self.cleanup_records: list[dict[str, object]] = []
        self.cleanup_records_truncated = False
        self.resource_close_unconfirmed = False
        self.closed = False
        self._created_profile: ProbeProfile | None = None
        _SESSIONS.add(self)

    def create(self) -> ProbeProfile:
        if self.closed or self.profile is not None or self._created_profile is not None:
            raise ValueError("acl_session_profile_already_created")
        self.profile = create_probe_profile("deskpal.v06." + uuid4().hex)
        self._created_profile = self.profile
        return self.profile

    def require_fresh(self) -> ProbeProfile:
        if (self not in _SESSIONS or self.closed or self.unknown or self.profile is None
                or self.profile is not self._created_profile):
            raise ValueError("acl_requires_own_fresh_session")
        return self.profile

    def mark_unknown(self, reason: str, *, resource: bool = False) -> None:
        self.unknown = True
        # Classification must survive truncation of the bounded detail list.
        if not resource:
            self.evidence_unknown = True
        entry = {"category": "resource_unconfirmed" if resource else "evidence_unknown",
                 "reason": reason[:256]}
        if entry not in self.unknown_reasons:
            if len(self.unknown_reasons) < MAX_UNKNOWN_REASONS:
                self.unknown_reasons.append(entry)
            else:
                self.unknown_reasons_truncated = True

    def cleanup_state(self) -> dict[str, object]:
        return {"unknown": self.unknown,
                "evidence_unknown": self.evidence_unknown,
                "resource_close_unconfirmed": self.resource_close_unconfirmed,
                "individual_adapter_handle_closes_verified": False,
                "popen_own_process_handle_verified": False,
                "unknown_reasons": list(self.unknown_reasons),
                "unknown_reasons_truncated": self.unknown_reasons_truncated,
                "resources": list(self.cleanup_records),
                "resources_truncated": self.cleanup_records_truncated}

    def _close_resource(self, owner: CleanupOwner, label: str) -> None:
        record: dict[str, object] = {"resource": label, "owner_type": type(owner).__name__,
                                     "close_call_completed": False, "closed": None, "reaped": None,
                                     "close_scope": "owner_contract_only"}
        try:
            owner.close()
            record["close_call_completed"] = True
        except BaseException as error:
            self.resource_close_unconfirmed = True
            reason = label + ":" + type(error).__name__ + ":" + str(error)[:192]
            self.mark_unknown(reason, resource=True)
            record.update(error_type=type(error).__name__, unknown_reason=reason[:256])
            _UNCONFIRMED.append(owner)
        if isinstance(owner, BindingHelperOwner):
            # Popen's own process handle is not explicitly closed/verified here.
            record["close_scope"] = "helper_reap_and_streams_only"
            record["reaped"] = owner.reaped
            streams = {name: stream is None or stream.closed for name, stream in (
                ("stdin", owner.process.stdin), ("stdout", owner.process.stdout),
                ("stderr", owner.process.stderr))}
            record["streams_closed"] = streams
            if not owner.reaped:
                self.resource_close_unconfirmed = True
                self.mark_unknown(label + ":helper_reap_unconfirmed", resource=True)
        elif isinstance(owner, HandleOwner):
            record["close_scope"] = "direct_win32_close_status"
            record["closed"] = not bool(owner.handle.value)
        elif isinstance(owner, BoundRoot):
            record["close_scope"] = "direct_win32_close_status"
            record["closed"] = not bool(owner.owner.handle.value)
        if len(self.cleanup_records) < MAX_CLEANUP_RECORDS:
            self.cleanup_records.append(record)
        else:
            self.cleanup_records_truncated = True

    def close_resources(self) -> bool:
        self.closed = True
        _SESSIONS.discard(self)
        self.resource_close_unconfirmed = False
        for category, owners in (("helper", self.helpers), ("worker", self.owners),
                                 ("handle", list(reversed(self.handles)))):
            for index, owner in enumerate(owners):
                self._close_resource(owner, category + ":" + str(index))
        if self.root is not None:
            self._close_resource(self.root, "bound_root")
        return not self.unknown and all(owner.reaped for owner in self.helpers)


class BoundRoot:
    def __init__(self, session: Session) -> None:
        profile = session.require_fresh()
        if session.root is not None:
            raise ValueError("acl_requires_own_fresh_session")
        self.session, self.profile = session, profile
        self.owner = HandleOwner(session.api, key=True)
        self.user = ""
        self.before: dict[str, object] = {}
        self.dacl_audit: dict[str, object] = {"state": "not_started", "readback_gate_passed": False,
                                              "deny_target": session.deny_target, "denied_sid": None}
        self.validated = False
        session.root = self  # Own partial-success DuplicateHandle output first.
        _BINDINGS.add(self)

    def require(self) -> None:
        self.session.require_fresh()
        if (self not in _BINDINGS or self.session.root is not self
                or self.session.profile is not self.profile or not self.validated
                or not self.owner.handle.value or self.session.unknown):
            raise ValueError("acl_requires_live_session_owned_binding")

    def close(self) -> None:
        self.owner.close()
        self.validated = False
        _BINDINGS.discard(self)


def _helper_exit(report: dict[str, object], code: int) -> NoReturn:
    try:
        readonly._emit_helper(report)
    finally:
        os._exit(code)


def _decode_message(payload: bytes) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("acl_helper_duplicate_json_key")
            result[key] = value
        return result

    def constant(_value: str) -> NoReturn:
        raise ValueError("acl_helper_nonfinite_json")

    if len(payload) > MAX_MESSAGE:
        raise ValueError("acl_helper_message_budget")
    value: object = json.loads(payload, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError("acl_helper_message_shape")
    return value


def _binding_helper(token: int, package: str, user: str, nonce: str) -> int:
    api = WriteApi()
    duplicate, thread, location, exported = W.HANDLE(), W.HANDLE(), W.HKEY(), W.HKEY()
    impersonating = False
    fatal_revert = False
    result: dict[str, object] = {"status": "binding_helper_failed", "stage0_passed": False}
    code = 2
    try:
        primary = readonly._token_metadata(api, token)
        readonly._verify_token(primary, package, user, 1)
        result["primary_token"] = primary
        readonly._require(api.duplicate_token(token, 0xC, None, 2, 2, C.byref(duplicate)), "acl_duplicate_token")
        # Revert even if a diagnostic wrapper raises after native success.
        impersonating = True
        readonly._require(api.impersonate(duplicate), "acl_impersonate")
        readonly._require(api.open_thread_token(api.current_thread(), 8, True, C.byref(thread)), "acl_thread_token")
        effective = readonly._token_metadata(api, int(thread.value or 0))
        readonly._verify_token(effective, package, user, 2)
        result["effective_thread_token"] = effective
        _status(int(api.get_registry(MANAGEMENT_ACCESS, C.byref(location))), "acl_get_registry_management")
        if not location.value:
            raise ValueError("acl_management_root_missing")
        _status(int(api.open_key(location, None, 0, MANAGEMENT_ACCESS, C.byref(exported))), "acl_export_same_root")
        if not exported.value:
            raise ValueError("acl_exported_root_missing")
        snapshot = _snapshot(api, exported, package, user)
        _baseline(snapshot, package, user)
        try:
            reverted = bool(api.revert())
        except BaseException:
            reverted = False
        if not reverted:
            fatal_revert = True
            _helper_exit(result | {"status": "fatal_revert_failed", "revert_confirmed": False}, 77)
        impersonating = False
        result.update(status="binding_ready", nonce=nonce, exported_key=int(exported.value or 0),
                      snapshot=snapshot, revert_confirmed=True)
        readonly._emit_helper(result)
        if sys.stdin.buffer.readline(128) != ("ACK " + nonce + "\n").encode("ascii"):
            raise ValueError("acl_binding_ack_missing")
        result["status"] = "binding_helper_complete"
        code = 0
    except Exception as error:
        result.update(error_type=type(error).__name__, error=str(error)[:2048])
    finally:
        if impersonating and not fatal_revert:
            try:
                reverted = bool(api.revert())
            except BaseException:
                reverted = False
            if not reverted:
                fatal_revert = True
                _helper_exit(result | {"status": "fatal_revert_failed", "revert_confirmed": False}, 77)
            result["revert_confirmed"] = True
        if not fatal_revert:
            errors: list[str] = []
            for name, key in (("exported_key", exported), ("location", location)):
                try:
                    if key.value:
                        _status(int(api.reg_close(key)), "acl_helper_reg_close")
                except BaseException:
                    errors.append(name)
            for name, handle in (("thread", thread), ("duplicate", duplicate), ("primary", W.HANDLE(token))):
                try:
                    if handle.value:
                        readonly._require(api.close(handle), "acl_helper_handle_close")
                except BaseException:
                    errors.append(name)
            result["close_errors"] = errors
            if errors:
                result["status"] = "binding_helper_close_failed"
                code = 2
    readonly._emit_helper(result)
    return code


def _pipe_chunk(api: WriteApi, stream: Any) -> bytes:
    import msvcrt
    if stream is None:
        raise ValueError("acl_helper_pipe_missing")
    handle = msvcrt.get_osfhandle(stream.fileno())
    available = W.DWORD()
    success = api.peek(handle, None, 0, None, C.byref(available), None)
    if not success and C.get_last_error() == 109:  # ERROR_BROKEN_PIPE after exit.
        return b""
    readonly._require(success, "acl_helper_pipe_peek")
    # Never block for a newline or for more than currently available bytes.
    return os.read(stream.fileno(), min(available.value, 4096)) if available.value else b""


class BindingHelperOwner(readonly.HelperOwner):
    """Registered trusted helper, with bounded concurrent stdout/stderr drains."""

    def __init__(self, process: subprocess.Popen[bytes], api: WriteApi) -> None:
        super().__init__(process)
        self.api = api
        self.stderr = bytearray()

    def pump(self, *, ready: bool) -> bytes:
        deadline, data = monotonic() + 10, bytearray()
        while monotonic() < deadline:
            output = _pipe_chunk(self.api, self.process.stdout)
            errors = _pipe_chunk(self.api, self.process.stderr)
            data.extend(output)
            self.stderr.extend(errors)
            if len(data) > MAX_MESSAGE or len(self.stderr) > 4096:
                raise ValueError("acl_helper_output_budget")
            if ready and b"\n" in data:
                line, trailing = bytes(data).split(b"\n", 1)
                if trailing:
                    raise ValueError("acl_helper_unexpected_early_second_message")
                return line
            if self.process.poll() is not None and not output and not errors:
                self.process.wait(timeout=3)
                self.reaped = True
                if ready:
                    raise ValueError("acl_helper_exited_before_binding")
                return bytes(data)
            if not output and not errors:
                sleep(0.005)
        raise TimeoutError("acl_helper_output_timeout")

    def collect(self) -> tuple[bytes, bytes]:
        try:
            return self.pump(ready=False), bytes(self.stderr)
        except BaseException:
            try:
                self.close()
            except BaseException:
                _UNCONFIRMED.append(self)
            raise

    def close(self) -> None:
        if not self.reaped:
            try:
                self.process.kill()
            except OSError:
                pass
            self.process.wait(timeout=3)
            self.reaped = True
        failure: BaseException | None = None
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except BaseException as error:
                failure = error
        if failure is not None:
            raise failure


def _ready(api: WriteApi, owner: BindingHelperOwner) -> dict[str, object]:
    if owner.api is not api:
        raise ValueError("acl_helper_api_mismatch")
    return _decode_message(owner.pump(ready=True))


def _bind_from_worker(session: Session, process: int, environment: dict[str, str]) -> dict[str, object]:
    session.require_fresh()
    try:
        return _bind_from_worker_impl(session, process, environment)
    except BaseException as error:
        # A failed handshake never authorizes profile deletion, even if later
        # reaping succeeds. Keep the session and all registered owners for review.
        session.mark_unknown("binding_handshake:" + type(error).__name__ + ":" + str(error))
        raise


def _bind_from_worker_impl(session: Session, process: int, environment: dict[str, str]) -> dict[str, object]:
    profile = session.require_fresh()
    api = session.api
    token, host, helper_process = HandleOwner(api), HandleOwner(api), HandleOwner(api)
    session.handles.extend((token, host, helper_process))
    readonly._require(api.open_process_token(process, 0xA, C.byref(token.handle)), "acl_subject_token")
    readonly._require(api.open_process_token(api.current_process(), 8, C.byref(host.handle)), "acl_host_token")
    user = readonly._token_sid(api, readonly._token_data(api, int(host.handle.value or 0), 1))
    subject = readonly._token_metadata(api, int(token.handle.value or 0))
    readonly._verify_token(subject, profile.sid, user, 1)
    nonce = uuid4().hex
    try:
        os.set_handle_inheritable(int(token.handle.value or 0), True)
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {"handle_list": [int(token.handle.value or 0)]}
        child = subprocess.Popen([sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()),
            "--_binding-helper", str(token.handle.value), profile.sid, user, nonce],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True, startupinfo=startup, env=environment, cwd=ROOT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        owner = BindingHelperOwner(child, api)
        session.helpers.append(owner)
    finally:
        os.set_handle_inheritable(int(token.handle.value or 0), False)
    ready = _ready(api, owner)
    if ready.get("status") != "binding_ready" or ready.get("nonce") != nonce or ready.get("revert_confirmed") is not True:
        raise ValueError("acl_helper_not_verified_ready")
    for field, kind in (("primary_token", 1), ("effective_thread_token", 2)):
        metadata = ready.get(field)
        if not isinstance(metadata, dict):
            raise ValueError("acl_helper_token_metadata_missing")
        readonly._verify_token(metadata, profile.sid, user, kind)
    # PID comes only from this registered Popen, never command-line/caller data.
    helper_process.handle.value = api.open_process(0x40, False, child.pid)
    readonly._require(helper_process.handle.value, "acl_open_own_helper_duplicate")
    exported = ready.get("exported_key")
    if type(exported) is not int or not 0 < exported < 2**64:
        raise ValueError("acl_exported_handle_shape")
    binding = BoundRoot(session)
    readonly._require(api.duplicate_handle(helper_process.handle, exported, api.current_process(),
        C.byref(binding.owner.handle), MANAGEMENT_ACCESS, False, 0), "acl_pull_same_root")
    binding.user = user
    binding.before = _snapshot(api, binding.owner.handle, profile.sid, user)
    _baseline(binding.before, profile.sid, user)
    if binding.before != ready.get("snapshot"):
        session.mark_unknown("acl_pulled_root_snapshot_mismatch")
        raise ValueError("acl_pulled_root_snapshot_mismatch")
    if child.stdin is None:
        raise ValueError("acl_helper_ack_pipe_missing")
    child.stdin.write(("ACK " + nonce + "\n").encode("ascii"))
    child.stdin.flush()
    stdout, stderr = owner.collect()
    if len(stdout) > MAX_MESSAGE or len(stderr) > 4096:
        raise ValueError("acl_helper_final_budget")
    completed = _decode_message(stdout)
    if (child.returncode != 0
            or not owner.reaped or stderr
            or completed.get("status") != "binding_helper_complete" or completed.get("close_errors") != []
            or any(completed.get(field) != ready.get(field) for field in (
                "nonce", "primary_token", "effective_thread_token", "exported_key", "snapshot", "revert_confirmed"))):
        session.mark_unknown("acl_helper_final_not_confirmed")
        raise ValueError("acl_helper_final_not_confirmed")
    binding.validated = True
    token.close()
    host.close()
    helper_process.close()
    return {"outer_subject": subject, "helper_ready": ready, "helper_final": completed,
            "helper_reaped": owner.reaped, "root_binding": "candidate_this_fresh_session_only"}


def _seed(binding: BoundRoot, names: dict[str, str]) -> list[dict[str, object]]:
    binding.require()
    result: list[dict[str, object]] = []
    for label, name in names.items():
        if not _MARKER.fullmatch(name):
            raise ValueError("acl_seed_name_invalid")
        owner = HandleOwner(binding.session.api, key=True)
        binding.session.handles.append(owner)
        disposition = W.DWORD()
        _status(int(binding.session.api.create_key(binding.owner.handle, name, 0, None, 0,
            0x60002, None, C.byref(owner.handle), C.byref(disposition))), "acl_seed_create")
        if disposition.value != 1:
            binding.session.mark_unknown("acl_seed_existing_refused_no_write_no_delete")
            raise ValueError("acl_seed_existing_refused_no_write_no_delete")
        _status(int(binding.session.api.set_value(owner.handle, "synthetic", 0, 3, CANARY, len(CANARY))), "acl_seed_set")
        result.append({"label": label, "name": name, "bytes": len(CANARY),
                       "snapshot": _snapshot(binding.session.api, owner.handle, binding.profile.sid, binding.user)})
        owner.close()
    return result


def _apply(binding: BoundRoot) -> dict[str, object]:
    try:
        return _apply_checked(binding)
    except BaseException as error:
        binding.dacl_audit.update(failed_at=binding.dacl_audit["state"], state="failed",
                                  error_type=type(error).__name__, error=str(error)[:2048])
        binding.session.mark_unknown("dacl_apply:" + type(error).__name__ + ":" + str(error))
        raise


def _apply_checked(binding: BoundRoot) -> dict[str, object]:
    audit = binding.dacl_audit
    audit.update(state="before_query", binding_baseline=binding.before, deny_target=binding.session.deny_target,
                 before_snapshot_obtained=False, set_attempted=False, set_returned=False,
                 actual_snapshot_obtained=False, readback_gate_passed=False)
    binding.require()
    denied_sid = _denied_sid(binding.profile.sid, binding.user, deny_target=binding.session.deny_target)
    audit["denied_sid"] = denied_sid
    current = _snapshot(binding.session.api, binding.owner.handle, binding.profile.sid, binding.user)
    audit.update(state="before_obtained", before=current, before_snapshot_obtained=True)
    if current != binding.before:
        binding.session.mark_unknown("acl_baseline_changed_before_apply")
        raise ValueError("acl_baseline_changed_before_apply")
    audit["state"] = "input_planning"
    acl, expected = _plan(current, binding.profile.sid, binding.user, deny_target=binding.session.deny_target)
    plan_audit = _input_plan_audit(current, acl, expected, binding.profile.sid,
                                  user=binding.user, deny_target=binding.session.deny_target)
    audit.update(state="planned", planned_acl_hex=acl.hex(), expected_raw_aces_hex=expected,
                 original_raw_aces_hex=current["raw_aces_hex"],
                 expected_readback={"owner_sid": binding.user, "dacl_protected": True,
                     "dacl_null": False, "acl_revision": current["acl_revision"],
                     "raw_aces_hex": expected})
    audit.update(plan_audit)
    buffer = C.create_string_buffer(acl)
    audit.update(state="set_call", set_attempted=True)
    status = int(binding.session.api.set_security(binding.owner.handle, 4, 0x80000004,
        None, None, buffer, None))
    audit.update(state="set_returned", set_returned=True, set_security_status=status)
    _status(status, "acl_apply_protected_dacl")
    audit["state"] = "readback_query"
    actual = _snapshot(binding.session.api, binding.owner.handle, binding.profile.sid, binding.user)
    audit.update(state="readback_obtained", after=actual, actual_snapshot_obtained=True)
    metadata = actual.get("metadata")
    checks = {"owner_matches": isinstance(metadata, dict) and metadata.get("owner_sid") == binding.user,
              "dacl_protected": isinstance(metadata, dict) and metadata.get("dacl_protected") is True,
              "dacl_nonnull": isinstance(metadata, dict) and metadata.get("dacl_null") is False,
              "acl_revision_matches": actual.get("acl_revision") == current.get("acl_revision"),
              "raw_aces_match": actual.get("raw_aces_hex") == expected}
    audit["gate_checks"] = checks
    if not all(checks.values()):
        raise ValueError("acl_readback_exact_gate_failed")
    audit.update(state="verified", readback_gate_passed=True,
                 original_aces_preserved_bytewise=plan_audit["original_aces_preserved_in_plan"],
                 critical_aces_preserved_bytewise=True,
                 only_inherited_bit_cleared_bytewise=True)
    return audit


def _restore_guards(session: Session) -> dict[str, bool]:
    root = session.root
    return {
        "enabled": session.restore_for_cleanup,
        "user_deny": session.deny_target == "user",
        "fresh_live_binding": (session in _SESSIONS and not session.closed
            and session.profile is not None and session.profile is session._created_profile
            and root is not None and root in _BINDINGS and root.session is session
            and root.profile is session.profile and root.validated and bool(root.owner.handle.value)),
        "default_worker_verified_reaped": "default" in session.verified_reaped_phases,
        "readonly_worker_verified_reaped": "readonly" in session.verified_reaped_phases,
        "helpers_present": bool(session.helpers),
        "all_helpers_reaped": all(owner.reaped for owner in session.helpers),
        "no_unknown": not (session.unknown or session.evidence_unknown or session.resource_close_unconfirmed),
        "no_pending_cleanup_owner": not session.owners,
        "apply_exact_gate_passed": (root is not None and root.dacl_audit.get("state") == "verified"
            and root.dacl_audit.get("readback_gate_passed") is True
            and isinstance(root.dacl_audit.get("after"), dict)),
        "not_previously_invoked": not session.cleanup_restore_invoked,
    }


def _original_dacl(binding: BoundRoot) -> tuple[bytes, int]:
    """Rebuild only the saved original DACL; do not normalize inherited ACEs."""
    original = binding.before
    _baseline(original, binding.profile.sid, binding.user)
    metadata, raw_hex = original.get("metadata"), original.get("raw_aces_hex")
    if (not isinstance(metadata, dict) or type(metadata.get("dacl_protected")) is not bool
            or not isinstance(metadata.get("aces"), list)
            or not isinstance(raw_hex, list) or len(raw_hex) > 512
            or not all(isinstance(item, str) for item in raw_hex)):
        raise ValueError("acl_cleanup_restore_original_invalid")
    entries = [bytes.fromhex(str(item)) for item in raw_hex]
    parsed = metadata["aces"]
    if len(entries) != len(parsed) or any(
        not isinstance(item, dict) or raw != _ace(int(item["type"]), int(item["flags"]),
            int(item["mask"]), str(item["sid"]))
        for raw, item in zip(entries, parsed, strict=True)
    ):
        raise ValueError("acl_cleanup_restore_original_raw_mismatch")
    revision, size = int(str(original.get("acl_revision"))), 8 + sum(map(len, entries))
    if revision not in (2, 4) or size > 65535:
        raise ValueError("acl_cleanup_restore_original_budget")
    information = 4 | (0x80000000 if metadata["dacl_protected"] else 0x20000000)
    return struct.pack("<BBHHH", revision, 0, size, len(entries), 0) + b"".join(entries), information


def _restore_for_cleanup(binding: BoundRoot) -> dict[str, object]:
    session, audit = binding.session, binding.session.cleanup_dacl_restore
    try:
        guards = _restore_guards(session)
        audit.update(state="guard_checks", guards=guards)
        session.cleanup_restore_invoked = True
        if not all(guards.values()):
            raise ValueError("acl_cleanup_restore_guards_failed")
        binding.require()
        audit.update(state="before_query", expected=binding.before,
                     expected_before_restore=binding.dacl_audit["after"])
        current = _snapshot(session.api, binding.owner.handle, binding.profile.sid, binding.user)
        audit.update(state="before_obtained", before=current)
        if current != binding.dacl_audit["after"]:
            raise ValueError("acl_cleanup_restore_before_changed")
        audit["state"] = "original_planning"
        acl, information = _original_dacl(binding)
        audit.update(state="planned", planned_acl_hex=acl.hex(), security_information=information)
        buffer = C.create_string_buffer(acl)
        audit.update(state="set_call", set_attempted=True)
        status = int(session.api.set_security(binding.owner.handle, 4, information, None, None, buffer, None))
        audit.update(state="set_returned", set_returned=True, set_security_status=status)
        if status != 0:
            try:
                observed = _snapshot(session.api, binding.owner.handle, binding.profile.sid, binding.user)
                audit["post_failed_set_observation"] = {"status": "observed", "snapshot": observed}
            except Exception as observation_error:
                audit["post_failed_set_observation"] = {
                    "status": "unknown",
                    "error_type": type(observation_error).__name__,
                    "error": str(observation_error)[:1024],
                }
        _status(status, "acl_cleanup_restore_dacl")
        audit["state"] = "readback_query"
        actual = _snapshot(session.api, binding.owner.handle, binding.profile.sid, binding.user)
        audit.update(state="readback_obtained", actual=actual, actual_snapshot_obtained=True)
        checks = {"snapshot_matches_original": actual == binding.before}
        audit["gate_checks"] = checks
        if not all(checks.values()):
            raise ValueError("acl_cleanup_restore_exact_gate_failed")
        audit.update(state="verified", readback_gate_passed=True)
        return audit
    except BaseException as error:
        audit.update(failed_at=audit["state"], state="failed",
                     error_type=type(error).__name__, error=str(error)[:2048], readback_gate_passed=False)
        session.mark_unknown("cleanup_restore:" + type(error).__name__ + ":" + str(error))
        raise


def _parse_worker_sd(sd: bytes) -> dict[str, object]:
    """Parse one copied self-relative SD, without native calls or enumeration."""
    if not 20 <= len(sd) <= 65536:
        raise ValueError("worker_sd_size_budget")
    revision, _reserved, control, owner, _group, _sacl, dacl = struct.unpack_from("<BBHLLLL", sd)
    if revision != 1 or not control & 0x8000:
        raise ValueError("worker_sd_not_self_relative_revision1")
    if not 20 <= owner <= len(sd) - 8:
        raise ValueError("worker_sd_owner_bounds")
    sid_revision, count = sd[owner], sd[owner + 1]
    if sid_revision != 1 or count > 15 or owner + 8 + 4 * count > len(sd):
        raise ValueError("worker_sd_owner_sid_bounds")
    authority = int.from_bytes(sd[owner + 2:owner + 8], "big")
    sub = [str(struct.unpack_from("<L", sd, owner + 8 + 4 * index)[0]) for index in range(count)]
    owner_sid = "S-1-" + str(authority) + "".join("-" + item for item in sub)
    result: dict[str, object] = {"owner_sid": owner_sid, "sd_bytes": len(sd),
        "control": control, "sd_revision": revision, "dacl_present": bool(control & 4),
        "dacl_null": not bool(dacl), "dacl_protected": bool(control & 0x1000),
        "acl_revision": None, "raw_aces_hex": []}
    if not control & 4:
        if dacl:
            raise ValueError("worker_sd_absent_dacl_has_offset")
        return result
    if not dacl:
        return result
    if not 20 <= dacl <= len(sd) - 8:
        raise ValueError("worker_sd_acl_bounds")
    acl_revision, _reserved, size, ace_count, _reserved2 = struct.unpack_from("<BBHHH", sd, dacl)
    if acl_revision not in (2, 4) or not 8 <= size <= len(sd) - dacl or ace_count > 32:
        raise ValueError("worker_sd_acl_budget_or_revision")
    cursor, end, total = dacl + 8, dacl + size, 0
    raw: list[str] = []
    for _index in range(ace_count):
        if cursor > end - 4:
            raise ValueError("worker_sd_ace_header_bounds")
        length = struct.unpack_from("<H", sd, cursor + 2)[0]
        if length < 4 or length % 4 or cursor + length > end:
            raise ValueError("worker_sd_ace_bounds")
        total += length
        if total > 2048:
            raise ValueError("worker_sd_ace_bytes_budget")
        raw.append(sd[cursor:cursor + length].hex())
        cursor += length
    result.update(acl_revision=acl_revision, raw_aces_hex=raw)
    return result


# The staged worker uses the same pure parser exercised by host-side mocks.
WORKER_SOURCE = inspect.getsource(_parse_worker_sd) + r'''
import ctypes as C
from ctypes import wintypes as W
import json, os, struct, sys, msvcrt

def bind(dll, name, args, result):
    f = getattr(dll, name); f.argtypes = args; f.restype = result; return f

a = C.WinDLL("advapi32", use_last_error=True)
k = C.WinDLL("kernel32", use_last_error=True)
u = C.WinDLL("userenv", use_last_error=True)
getroot = bind(u, "GetAppContainerRegistryLocation", [W.DWORD,C.POINTER(W.HKEY)], C.c_int32)
close = bind(a, "RegCloseKey", [W.HKEY], C.c_int32)
openkey = bind(a, "RegOpenKeyExW", [W.HKEY,W.LPCWSTR,W.DWORD,W.DWORD,C.POINTER(W.HKEY)], C.c_int32)
create = bind(a, "RegCreateKeyExW", [W.HKEY,W.LPCWSTR,W.DWORD,W.LPWSTR,W.DWORD,W.DWORD,C.c_void_p,C.POINTER(W.HKEY),C.POINTER(W.DWORD)], C.c_int32)
setvalue = bind(a, "RegSetValueExW", [W.HKEY,W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD], C.c_int32)
delete = bind(a, "RegDeleteKeyW", [W.HKEY,W.LPCWSTR], C.c_int32)
opentoken = bind(a, "OpenProcessToken", [W.HANDLE,W.DWORD,C.POINTER(W.HANDLE)], W.BOOL)
gettoken = bind(a, "GetTokenInformation", [W.HANDLE,C.c_int,C.c_void_p,W.DWORD,C.POINTER(W.DWORD)], W.BOOL)
sidstring = bind(a, "ConvertSidToStringSidW", [C.c_void_p,C.POINTER(C.c_void_p)], W.BOOL)
free = bind(k, "LocalFree", [C.c_void_p], C.c_void_p)
closehandle = bind(k, "CloseHandle", [W.HANDLE], W.BOOL)
current = bind(k, "GetCurrentProcess", [], W.HANDLE)
getsecurity = bind(a, "GetSecurityInfo", [W.HANDLE,C.c_int,W.DWORD,C.POINTER(C.c_void_p),C.c_void_p,C.POINTER(C.c_void_p),C.c_void_p,C.POINTER(C.c_void_p)], W.DWORD)
sdlength = bind(a, "GetSecurityDescriptorLength", [C.c_void_p], W.DWORD)
case = json.loads(sys.argv[1]); out = {"kind":"registry_acl_worker", "phase":case["phase"], "close_errors":[], "checks":{}}
def closekey(key):
    if key.value and close(key) != 0: out["close_errors"].append("key")
def token_info(token, kind):
    size = W.DWORD(); gettoken(token,kind,None,0,C.byref(size))
    if not 0 < size.value <= 65536: raise ValueError("token budget")
    data = C.create_string_buffer(size.value)
    if not gettoken(token,kind,data,len(data),C.byref(size)): raise OSError("token query")
    return data
def sid(data):
    text = C.c_void_p()
    if not sidstring(C.c_void_p.from_buffer(data),C.byref(text)): raise OSError("sid conversion")
    try: return C.wstring_at(text)
    finally: free(text)

def observe_sd(root, stage, call_index):
    observation = {"stage":stage, "sd_call_index":call_index, "status":"unknown"}
    descriptor = C.c_void_p(); owner = C.c_void_p(); dacl = C.c_void_p()
    try:
        status = int(getsecurity(root,4,5,C.byref(owner),None,C.byref(dacl),None,C.byref(descriptor)))
        if status != 0: raise OSError(status,"worker_sd_query")
        if not descriptor.value: raise ValueError("worker_sd_descriptor_missing")
        length = int(sdlength(descriptor))
        if not 20 <= length <= 65536: raise ValueError("worker_sd_size_budget")
        observation["snapshot"] = _parse_worker_sd(C.string_at(descriptor,length))
        observation["status"] = "observed"
    except Exception as error:
        observation.update(error_type=type(error).__name__,error=str(error)[:256])
    finally:
        if descriptor.value:
            try:
                if free(descriptor): raise OSError("worker_sd_free_failed")
            except Exception as error:
                observation.update(status="unknown",release_error_type=type(error).__name__,release_error=str(error)[:256])
    return observation
token = W.HANDLE()
if not opentoken(current(),8,C.byref(token)): raise OSError("open own token")
try:
    out["identity"] = {"user_sid":sid(token_info(token,1)), "package_sid":sid(token_info(token,31)),
        "integrity_sid":sid(token_info(token,25)), "is_appcontainer":W.DWORD.from_buffer(token_info(token,29)).value,
        "capability_count":W.DWORD.from_buffer(token_info(token,30)).value,
        "token_type":W.DWORD.from_buffer(token_info(token,8)).value, "impersonation_level":None}
finally:
    if not closehandle(token): out["close_errors"].append("token")
root = W.HKEY(); hr = getroot(0x20000,C.byref(root))
out["root_read_control_hresult"] = hr & 0xffffffff
out["root_read_control_precedes_permission_calls"] = True
try:
    if hr == 0:
        out["root_sd_before"] = observe_sd(root,"before_permission_calls",1)
    try:
        for label, mask in (("set",2),("create",4),("delete",0x10000),("wd",0x40000),("wo",0x80000)):
            key = W.HKEY()
            try:
                status = getroot(mask,C.byref(key))
                out["checks"]["root_"+label] = {"api":"GetAppContainerRegistryLocation","access":mask,"status":status & 0xffffffff}
            finally: closekey(key)
        if hr != 0: raise OSError(hr,"worker_read_control_root")
        child = W.HKEY(); disposition = W.DWORD()
        status = create(root,case["new_child"],0,None,0,2,None,C.byref(child),C.byref(disposition))
        entry = {"api":"RegCreateKeyExW_read_control_parent","status":status,"disposition":disposition.value}
        out["checks"]["readonly_parent_create"] = entry
        try:
            if status == 0:
                if disposition.value != 1:
                    out["existing_object_refused"] = True
                else: entry["set_status"] = setvalue(child,"synthetic",0,3,b"SYNTH06!",8)
        finally: closekey(child)
        for label, mask in (("set",2),("wd",0x40000),("wo",0x80000)):
            child = W.HKEY(); status = openkey(root,case["seeded"][label],0,mask,C.byref(child))
            entry = {"api":"RegOpenKeyExW_seeded","access":mask,"status":status}
            out["checks"]["seeded_"+label] = entry
            try:
                if status == 0 and label == "set": entry["set_status"] = setvalue(child,"synthetic",0,3,b"SYNTH06!",8)
            finally: closekey(child)
        out["checks"]["seeded_delete"] = {"api":"RegDeleteKeyW_seeded","status":delete(root,case["seeded"]["delete"])}
    finally:
        if hr == 0: out["root_sd_after"] = observe_sd(root,"after_permission_calls",2)
finally: closekey(root)
payload = json.dumps(out,ensure_ascii=True,allow_nan=False,separators=(",",":")).encode("ascii")
if len(payload) > 16384: raise ValueError("worker output budget")
fd = msvcrt.open_osfhandle(int(sys.argv[2]),os.O_WRONLY|os.O_BINARY)
try:
    frame = struct.pack("!I",len(payload))+payload
    while frame:
        count = os.write(fd,frame)
        if count <= 0: raise RuntimeError("control pipe closed")
        frame = frame[count:]
finally: os.close(fd)
'''


def _worker_sd_gate(value: object, stage: str, call_index: int) -> None:
    if (not isinstance(value, dict) or value.get("status") != "observed"
            or value.get("stage") != stage or type(value.get("sd_call_index")) is not int
            or value.get("sd_call_index") != call_index):
        raise ValueError("acl_worker_sd_observation_unknown")
    snapshot = value.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("acl_worker_sd_snapshot_missing")
    owner, control, size = snapshot.get("owner_sid"), snapshot.get("control"), snapshot.get("sd_bytes")
    if (not isinstance(owner, str) or len(owner) > 256 or type(control) is not int
            or not 0 <= control <= 0xFFFF or not control & 0x8000 or type(size) is not int
            or not 20 <= size <= 65536 or type(snapshot.get("sd_revision")) is not int
            or snapshot.get("sd_revision") != 1):
        raise ValueError("acl_worker_sd_metadata_invalid")
    _sid_bytes(owner)
    for label, expected in (("dacl_present", bool(control & 4)),
                            ("dacl_protected", bool(control & 0x1000))):
        if snapshot.get(label) is not expected:
            raise ValueError("acl_worker_sd_control_disagrees")
    raw = snapshot.get("raw_aces_hex")
    if not isinstance(raw, list) or len(raw) > 32 or type(snapshot.get("dacl_null")) is not bool:
        raise ValueError("acl_worker_sd_ace_budget")
    total = 0
    for item in raw:
        if not isinstance(item, str) or len(item) > 4096:
            raise ValueError("acl_worker_sd_ace_budget")
        ace = bytes.fromhex(item)
        total += len(ace)
        if (item != ace.hex() or len(ace) < 4 or len(ace) % 4
                or struct.unpack_from("<H", ace, 2)[0] != len(ace) or total > 2048):
            raise ValueError("acl_worker_sd_ace_shape_or_budget")
    if snapshot["dacl_null"] or not snapshot["dacl_present"]:
        if raw or snapshot.get("acl_revision") is not None:
            raise ValueError("acl_worker_sd_null_acl_disagrees")
    elif type(snapshot.get("acl_revision")) is not int or snapshot.get("acl_revision") not in (2, 4):
        raise ValueError("acl_worker_sd_acl_revision_invalid")


def _worker_gate(frame: dict[str, object], profile: ProbeProfile, user: str, phase: str) -> None:
    if phase not in ("default", "readonly"):
        raise ValueError("acl_worker_phase_invalid")
    identity = frame.get("identity")
    if not isinstance(identity, dict):
        raise ValueError("acl_worker_identity_missing")
    readonly._verify_token(identity, profile.sid, user, 1)
    if frame.get("root_read_control_precedes_permission_calls") is not True:
        raise ValueError("acl_worker_sd_observation_order_missing")
    _worker_sd_gate(frame.get("root_sd_before"), "before_permission_calls", 1)
    _worker_sd_gate(frame.get("root_sd_after"), "after_permission_calls", 2)
    if (frame.get("kind") != "registry_acl_worker" or frame.get("phase") != phase
            or frame.get("close_errors") != [] or frame.get("existing_object_refused")
            or frame.get("root_read_control_hresult") != 0):
        raise ValueError("acl_worker_observation_incomplete")
    checks = frame.get("checks")
    expected = {"root_set", "root_create", "root_delete", "root_wd", "root_wo",
                "readonly_parent_create", "seeded_set", "seeded_delete", "seeded_wd", "seeded_wo"}
    if not isinstance(checks, dict) or set(checks) != expected:
        raise ValueError("acl_worker_check_set_incomplete")
    for label, entry in checks.items():
        if not isinstance(entry, dict):
            raise ValueError("acl_worker_check_shape")
        mask = {"set": 2, "create": 4, "delete": 0x10000, "wd": 0x40000, "wo": 0x80000}
        expected_api = ("GetAppContainerRegistryLocation" if label.startswith("root_") else
                        "RegCreateKeyExW_read_control_parent" if label == "readonly_parent_create" else
                        "RegDeleteKeyW_seeded" if label == "seeded_delete" else "RegOpenKeyExW_seeded")
        if (entry.get("api") != expected_api or (label.startswith("root_") or label in (
                "seeded_set", "seeded_wd", "seeded_wo")) and entry.get("access") != mask[label.rsplit("_", 1)[1]]):
            raise ValueError("acl_worker_operation_mismatch")
        desired = 0 if phase == "default" else (0x80070005 if label.startswith("root_") else 5)
        if type(entry.get("status")) is not int or entry.get("status") != desired:
            raise ValueError("acl_worker_positive_or_denial_gate_failed:" + str(label))
        if phase == "default" and label in ("readonly_parent_create", "seeded_set"):
            if entry.get("set_status") != 0:
                raise ValueError("acl_worker_positive_write_failed")
        if phase == "default" and label == "readonly_parent_create" and entry.get("disposition") != 1:
            raise ValueError("acl_worker_positive_existing_key")


def _worker(session: Session, manifest: RuntimeManifest, environment: dict[str, str],
            case: dict[str, object], report: dict[str, object]) -> None:
    if session.profile is None or session.root is None:
        raise ValueError("acl_worker_requires_session")
    session.root.require()
    verify_manifest(manifest)
    spec = LaunchSpec(Path(manifest.executable), ("-I", "-S", "-B", "-X", "utf8",
        str(Path(manifest.root) / "worker.py"), json.dumps(case, ensure_ascii=True), "{ipc_write_handle}"),
        Path(manifest.root), session.profile.sid, environment)
    phase = str(case["phase"])
    data: dict[str, object] = {}
    reap: dict[str, object] = {
        "state": "before_launch",
        "terminate_attempted": False, "terminate_and_wait_completed": False,
        "context_exit_completed": False, "strict_worker_gate_passed": False,
        "basis": "Trusted host RunningWorker.terminate contract: Job active processes zero and process signaled.",
        "individual_adapter_handle_closes_verified": False,
        "popen_own_process_handle_verified": False,
    }
    try:
        with WindowsLauncher().launch(spec) as worker:
            result = worker.collect(15)
            if session.restore_for_cleanup:
                # Keep the collected evidence even if explicit reap or close fails.
                data = asdict(result)
                data["stdout"], data["stderr"] = result.stdout.decode("utf-8", "replace"), result.stderr.decode("utf-8", "replace")
                data["host_terminate_and_wait"] = reap
                report[phase + "_worker"] = data
                reap.update(state="terminate_call", terminate_attempted=True)
                worker.terminate()
                reap.update(state="context_exit", terminate_and_wait_completed=True)
        if not session.restore_for_cleanup:
            data = asdict(result)
            data["stdout"], data["stderr"] = result.stdout.decode("utf-8", "replace"), result.stderr.decode("utf-8", "replace")
            report[phase + "_worker"] = data
        else:
            reap.update(state="host_snapshot", context_exit_completed=True)
        observation: dict[str, object] = {"stage": "after_worker_context_exit", "status": "unknown"}
        data["host_bound_root_after_worker"] = observation
        data["sd_observation_scope"] = (
            "Worker before follows its first READ_CONTROL location API call; snapshots bound only "
            "the recorded window. SD equality does not prove key-object identity.")
        try:
            observation["snapshot"] = _snapshot(session.api, session.root.owner.handle,
                                                 session.profile.sid, session.root.user)
            observation["status"] = "observed"
        except BaseException as error:
            observation.update(error_type=type(error).__name__, error=str(error)[:256])
            raise
        if session.restore_for_cleanup:
            reap["state"] = "worker_result_gate"
        if result.exit_code != 0 or len(result.frames) != 1 or not isinstance(result.frames[0], dict):
            raise ValueError("acl_worker_startup_or_frame_failed")
        if session.restore_for_cleanup:
            reap["state"] = "worker_gate"
        _worker_gate(result.frames[0], session.profile, session.root.user, phase)
        if session.restore_for_cleanup:
            reap.update(state="verified", strict_worker_gate_passed=True)
            session.verified_reaped_phases.add(phase)
    except BaseException as error:
        if session.restore_for_cleanup:
            reap.update(failed_at=reap["state"], state="failed",
                        error_type=type(error).__name__, error=str(error)[:2048])
        # Missing/malformed evidence includes existing-name collisions and key
        # close failures. Never delete a profile after an unknown observation.
        session.mark_unknown("worker_" + str(case["phase"]) + ":" + type(error).__name__ + ":" + str(error))
        owner = getattr(error, "cleanup_owner", None)
        if owner is not None:
            session.owners.append(owner)
        raise


def _experiment(output: TextIO, report: dict[str, object], python: Path, runtime: Path, *,
                deny_target: str = "package", restore_for_cleanup: bool = False) -> int:
    _validate_cleanup_restore(deny_target, restore_for_cleanup)
    report["deny_target"] = _validate_deny_target(deny_target)
    report["denied_sid"] = None
    report["cleanup_dacl_restore"] = _new_restore_audit(restore_for_cleanup)
    if os.name != "nt" or C.sizeof(C.c_void_p) != 8:
        raise ValueError("acl_experiment_windows_x64_only")
    api = WriteApi()
    session = Session(api, deny_target=deny_target, restore_for_cleanup=restore_for_cleanup)
    report["cleanup_dacl_restore"] = session.cleanup_dacl_restore
    code = 2
    try:
        profile = session.create()
        report.update(profile=asdict(profile), cleanup="pending")
        readonly._save(output, report)
        readonly._reject_reparse(Path(profile.folder))
        temporary = Path(profile.folder) / "Temp"
        readonly._reject_reparse(temporary)
        temporary.mkdir(exist_ok=True)
        windows = C.create_unicode_buffer(32768)
        length = int(api.windows_directory(windows, len(windows)))
        if not 0 < length < len(windows):
            raise ValueError("acl_windows_directory_failed")
        environment = worker_environment(Path(windows.value), temporary, local_appdata=Path(profile.folder))
        readonly._reject_reparse(readonly.FIXTURE)
        if sha256(readonly.FIXTURE.read_bytes()).hexdigest() != readonly.FIXTURE_SHA256:
            raise ValueError("acl_fixture_pin_mismatch")
        destination = Path(profile.folder) / "native-fixture.exe"
        readonly._reject_reparse(destination)
        with readonly.FIXTURE.open("rb") as source, destination.open("xb") as target:
            target.write(source.read())
        binding_report: dict[str, object] = {}

        def observe(process: int) -> None:
            binding_report.update(_bind_from_worker(session, process, environment))

        suspended = readonly.SuspendedRegistryApi(observe)
        try:
            with _CREATE_LOCK:
                native, _identity = _create_worker(LaunchSpec(destination, (), destination.parent,
                    profile.sid, environment), _api=suspended)
            session.owners.append(native)
            raise ValueError("acl_binding_unexpected_resume_return")
        except LaunchRejected as error:
            if error.cleanup_owner is not None:
                session.owners.append(error.cleanup_owner)
                session.mark_unknown("suspended_worker_reap_unconfirmed", resource=True)
            if error.stage != readonly.STOP_STAGE or error.cleanup_owner is not None:
                raise
        report["binding"] = binding_report
        binding = session.root
        if binding is None:
            raise ValueError("acl_binding_missing")
        binding.require()
        report["denied_sid"] = _denied_sid(profile.sid, binding.user, deny_target=session.deny_target)
        binding.dacl_audit["denied_sid"] = report["denied_sid"]
        manifest = stage_runtime(python, runtime, worker_source=WORKER_SOURCE)
        verify_manifest(manifest)
        report["runtime"] = {"root": manifest.root, "python_tag": manifest.python_tag,
            "file_count": len(manifest.files), "manifest_sha256": sha256(json.dumps(asdict(manifest), sort_keys=True).encode()).hexdigest()}
        apply_readonly_dacl(manifest, owner_sid=binding.user, package_sid=profile.sid)
        positive = {label: "DeskpalV06_" + uuid4().hex + "_" + label for label in ("set", "delete", "wd", "wo")}
        negative = {label: "DeskpalV06_" + uuid4().hex + "_" + label for label in ("set", "delete", "wd", "wo")}
        report["seeded"] = _seed(binding, {"default_" + key: value for key, value in positive.items()} |
                                   {"readonly_" + key: value for key, value in negative.items()})
        _worker(session, manifest, environment, {"phase": "default", "seeded": positive,
            "new_child": "DeskpalV06_" + uuid4().hex + "_created"}, report)
        readonly._save(output, report)
        report["dacl"] = _apply(binding)
        readonly._save(output, report)
        _worker(session, manifest, environment, {"phase": "readonly", "seeded": negative,
            "new_child": "DeskpalV06_" + uuid4().hex + "_created"}, report)
        if session.restore_for_cleanup:
            _restore_for_cleanup(binding)
        report["status"] = "selected_registry_canaries_denied_namespace_coverage_incomplete"
        code = 0
    except Exception as error:
        report.update(status="acl_experiment_blocked", error_type=type(error).__name__, error=str(error)[:2048])
    finally:
        if session.restore_for_cleanup and session.cleanup_dacl_restore["readback_gate_passed"] is not True:
            if session.cleanup_dacl_restore["state"] == "not_started":
                session.cleanup_dacl_restore.update(state="withheld", failed_at="before_restore_prerequisites",
                                                    guards=_restore_guards(session))
            session.mark_unknown("cleanup_restore_not_verified")
        resources_safe = session.close_resources()
        if session.root is not None:
            report["dacl"] = session.root.dacl_audit
        report["cleanup_state"] = session.cleanup_state()
        if resources_safe and session.profile is not None:
            try:
                delete_probe_profile(session.profile)
                report["cleanup"] = "profile_deleted"
            except Exception as error:
                report.update(cleanup="failed_profile_may_remain", cleanup_error=str(error)[:2048])
                code = 2
        elif session.profile is not None:
            report["cleanup"] = "withheld_unknown_helper_worker_key_or_binding"
            code = 2
        readonly._save(output, report)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True, dest="python_executable")
    parser.add_argument("--experiment", action="store_true")
    parser.add_argument("--deny-target", choices=("package", "user"), default="package",
                        help="Deny the verified package SID or verified subject/host user SID.")
    parser.add_argument("--restore-for-cleanup", action="store_true",
                        help="After both strict worker gates and trusted reap, restore this fresh user-control DACL for cleanup.")
    args = parser.parse_args()
    if args.restore_for_cleanup and args.deny_target != "user":
        parser.error("--restore-for-cleanup requires --deny-target user")
    path = readonly._new_report(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    readonly._reject_reparse(path)
    report: dict[str, object] = {"stage0_passed": False, "binding_verified": False,
        "deny_target": args.deny_target, "denied_sid": None,
        "cleanup_dacl_restore": _new_restore_audit(args.restore_for_cleanup),
        "scope": "ordinary AC; one fresh session; selected eight-byte synthetic branches only",
        "critical_aces_removed": False, "registry_values_read": False, "registry_names_enumerated": False,
        "fallback_allowed": False, "coverage_gap": "Unenumerated and protected descendants, other namespace/view paths remain unverified.",
        "cleanup": "not_required"}
    with path.open("x", encoding="ascii") as output:
        if not args.experiment:
            report["status"] = "not_executed_explicit_opt_in_required"
            readonly._save(output, report)
            return 0
        sources = readonly._fingerprints() | {"scripts/diagnose_worker_registry_acl.py": sha256(Path(__file__).read_bytes()).hexdigest()}
        report["source_sha256"] = sources
        try:
            runtime = path.parent / (path.stem + "-runtime")
            readonly._reject_reparse(runtime)
            if runtime.exists():
                raise ValueError("acl_runtime_requires_new_directory")
            code = _experiment(output, report, args.python_executable, runtime, deny_target=args.deny_target,
                               restore_for_cleanup=args.restore_for_cleanup)
        except Exception as error:
            report.update(status="acl_experiment_blocked", error_type=type(error).__name__, error=str(error)[:2048])
            code = 2
        report["source_sha256_end"] = readonly._fingerprints() | {"scripts/diagnose_worker_registry_acl.py": sha256(Path(__file__).read_bytes()).hexdigest()}
        report["source_unchanged"] = sources == report["source_sha256_end"]
        if not report["source_unchanged"]:
            code = 2
        readonly._save(output, report)
    print(json.dumps({"report": str(path), "diagnostic_exit": code, "stage0_passed": False}))
    return code


if __name__ == "__main__":
    if len(sys.argv) == 6 and sys.argv[1] == "--_binding-helper":
        raise SystemExit(_binding_helper(int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]))
    raise SystemExit(main())
