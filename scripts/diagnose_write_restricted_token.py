"""Opt-in precursor-token-only observation; never launches or impersonates.

This does not establish AC conversion, registry denial, WRITE_RESTRICTED bit
coverage, owner-rights behavior, or stage-zero acceptance. Only a new unnamed
derived token is created. No account, profile, registry or DACL API is bound.
"""

from __future__ import annotations

import argparse
import ctypes as C
from hashlib import sha256
import json
import os
from pathlib import Path
import struct
from typing import Any, TextIO
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
DWORD, LONG, BOOL, HANDLE = C.c_uint32, C.c_int32, C.c_int32, C.c_void_p
TOKEN_QUERY, TOKEN_DUPLICATE = 8, 2
FLAGS, MAX_BUFFER, MAX_ENTRIES = 0x9, 65536, 128
MAX_REPORT, MAX_CALLS = 512 * 1024, 96
QUERY_CLASSES = (("user", 1), ("groups", 2), ("privileges", 3),
                 ("type", 8), ("restricted_sids", 11), ("statistics", 10))
PSEUDO_HANDLES = frozenset(C.c_void_p(value).value for value in (-1, -2, -3, -4, -5, -6))


class Luid(C.Structure):
    _fields_ = [("LowPart", DWORD), ("HighPart", LONG)]


class LuidAttributes(C.Structure):
    _fields_ = [("Luid", Luid), ("Attributes", DWORD)]


class SidAttributes(C.Structure):
    _fields_ = [("Sid", C.c_void_p), ("Attributes", DWORD)]


class GroupsLayout(C.Structure):
    _fields_ = [("GroupCount", DWORD), ("Groups", SidAttributes * 1)]


class PrivilegesLayout(C.Structure):
    _fields_ = [("PrivilegeCount", DWORD), ("Privileges", LuidAttributes * 1)]


class Statistics(C.Structure):
    _fields_ = [("TokenId", Luid), ("AuthenticationId", Luid),
                ("ExpirationTime", C.c_int64), ("TokenType", DWORD),
                ("ImpersonationLevel", DWORD), ("DynamicCharged", DWORD),
                ("DynamicAvailable", DWORD), ("GroupCount", DWORD),
                ("PrivilegeCount", DWORD), ("ModifiedId", Luid)]


def _layout() -> None:
    if (C.sizeof(C.c_void_p) != 8 or C.sizeof(Luid) != 8 or C.alignment(Luid) != 4
            or C.sizeof(LuidAttributes) != 12 or LuidAttributes.Attributes.offset != 8
            or GroupsLayout.Groups.offset != 8 or PrivilegesLayout.Privileges.offset != 4
            or C.sizeof(SidAttributes) != 16 or C.sizeof(Statistics) != 56
            or Statistics.ModifiedId.offset != 48):
        raise ValueError("token_public_layout_mismatch")


class NativeApi:
    """Exact allowlist. Library loading occurs only after explicit opt-in."""

    def __init__(self) -> None:
        advapi = C.WinDLL("advapi32", use_last_error=True)
        kernel = C.WinDLL("kernel32", use_last_error=True)
        declarations = (
            (kernel, "GetCurrentProcess", [], HANDLE),
            (kernel, "GetCurrentThread", [], HANDLE),
            (kernel, "CloseHandle", [HANDLE], BOOL),
            (advapi, "OpenProcessToken", [HANDLE, DWORD, C.POINTER(HANDLE)], BOOL),
            (advapi, "OpenThreadToken", [HANDLE, DWORD, BOOL, C.POINTER(HANDLE)], BOOL),
            (advapi, "GetTokenInformation", [HANDLE, C.c_int, C.c_void_p, DWORD, C.POINTER(DWORD)], BOOL),
            (advapi, "IsTokenRestricted", [HANDLE], BOOL),
            (advapi, "LookupPrivilegeValueW", [C.c_wchar_p, C.c_wchar_p, C.POINTER(Luid)], BOOL),
            (advapi, "CreateRestrictedToken", [HANDLE, DWORD, DWORD, C.c_void_p,
                DWORD, C.c_void_p, DWORD, C.POINTER(SidAttributes), C.POINTER(HANDLE)], BOOL),
        )  # type: tuple[tuple[Any, str, list[Any], Any], ...]
        self.functions: dict[str, Any] = {}
        for library, name, arguments, result in declarations:
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result
            self.functions[name] = function

    def call(self, name: str, *arguments: Any) -> tuple[int, int]:
        C.set_last_error(0)
        result = self.functions[name](*arguments)
        error = C.get_last_error()  # Immediately after the native return.
        return int(result or 0), int(error)


def _reject_reparse(path: Path) -> None:
    for item in (path, *path.parents):
        try:
            metadata = item.lstat()
        except FileNotFoundError:
            continue
        if item.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError("token_report_reparse_path")


def _new_report(path: Path) -> Path:
    _reject_reparse(path.absolute())
    target, allowed = path.resolve(), (ROOT / "data/validation/v06").resolve()
    if (not target.is_relative_to(allowed) or target == allowed or target.exists()
            or target.suffix.lower() != ".json" or target.is_reserved()
            or any(":" in part for part in target.parts[1:])):
        raise ValueError("token_report_requires_new_validation_json")
    return target


def _save(output: TextIO, report: dict[str, Any]) -> None:
    payload = json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
    if len(payload.encode("ascii")) > MAX_REPORT:
        # Fixed parser bounds normally keep all required fields below budget.
        # An unexpected overflow is an incomplete failure, never a shortened pass.
        report.update(status="unknown", incomplete=True, observations_valid=False,
                      evidence_budget_exceeded=True)
        for label in ("source_before", "derived", "source_after"):
            observation = report.pop(label, None)
            if observation is not None:
                encoded = json.dumps(observation, ensure_ascii=True, allow_nan=False, sort_keys=True).encode("ascii")
                report.setdefault("incomplete_observation_summaries", {})[label] = {
                    "serialized_bytes": len(encoded), "sha256": sha256(encoded).hexdigest(),
                    "complete_required_fields_retained": False}
        payload = json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
        if len(payload.encode("ascii")) > MAX_REPORT:
            raise ValueError("token_report_budget_incomplete")
    output.seek(0)
    output.write(payload)
    output.truncate()
    output.flush()


def _fingerprints() -> dict[str, str]:
    # No project modules are imported or used by this standalone diagnostic.
    paths = ("scripts/diagnose_write_restricted_token.py",
             "tests/unit/test_write_restricted_token_diagnostic.py")
    return {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}


def _sid(data: bytes, base: int, address: int) -> str:
    offset = address - base
    if not 0 <= offset <= len(data) - 8:
        raise ValueError("token_sid_pointer_outside_owned_buffer")
    revision, count = data[offset], data[offset + 1]
    if revision != 1 or count > 15 or offset + 8 + 4 * count > len(data):
        raise ValueError("token_sid_shape_or_bounds")
    authority = int.from_bytes(data[offset + 2:offset + 8], "big")
    parts = struct.unpack_from("<" + "I" * count, data, offset + 8)
    return "S-1-" + str(authority) + "".join("-" + str(part) for part in parts)


def _luid(value: Luid) -> dict[str, int]:
    return {"low_dword": int(value.LowPart), "high_long": int(value.HighPart)}


def _parse(kind: int, data: bytes, base: int) -> Any:
    if kind in (8, 29):
        if len(data) != 4:
            raise ValueError("token_scalar_length")
        value = struct.unpack("<I", data)[0]
        if kind == 8 and value not in (1, 2) or kind == 29 and value not in (0, 1):
            raise ValueError("token_scalar_value")
        return value
    if kind == 1:
        if len(data) < C.sizeof(SidAttributes):
            raise ValueError("token_user_short")
        entry = SidAttributes.from_buffer_copy(data)
        return {"sid": _sid(data, base, int(entry.Sid or 0)), "attributes": int(entry.Attributes)}
    if kind in (2, 11, 3):
        if len(data) < 4:
            raise ValueError("token_array_count_short")
        count = struct.unpack_from("<I", data)[0]
        if count > MAX_ENTRIES:
            raise ValueError("token_array_count_budget")
        if count == 0:
            return []  # A zero count does not imply the flexible-array header exists.
        offset, stride = (4, 12) if kind == 3 else (8, 16)
        if offset + count * stride > len(data):
            raise ValueError("token_array_entries_short")
        entries: list[dict[str, Any]] = []
        for index in range(count):
            start = offset + index * stride
            if kind == 3:
                privilege = LuidAttributes.from_buffer_copy(data, start)
                entries.append({"luid": _luid(privilege.Luid), "attributes": int(privilege.Attributes)})
            else:
                group = SidAttributes.from_buffer_copy(data, start)
                entries.append({"sid": _sid(data, base, int(group.Sid or 0)), "attributes": int(group.Attributes)})
        return entries
    if kind == 10:
        if len(data) != C.sizeof(Statistics):
            raise ValueError("token_statistics_length")
        statistics = Statistics.from_buffer_copy(data)
        return {"token_id": _luid(statistics.TokenId), "modified_id": _luid(statistics.ModifiedId)}
    raise ValueError("token_query_class_not_allowed")


class Owner:
    def __init__(self, label: str) -> None:
        self.label, self.handle = label, HANDLE()

    def valid(self) -> bool:
        return self.handle.value not in (None, 0) and self.handle.value not in PSEUDO_HANDLES


class Session:
    def __init__(self, api: NativeApi, output: TextIO, report: dict[str, Any]) -> None:
        self.api, self.output, self.report = api, output, report
        self.owners: list[Owner] = []
        report.update(native_calls=[], token_queries=[], close_results=[], unknown_reasons=[])

    def save(self) -> None:
        _save(self.output, self.report)

    def unknown(self, reason: str) -> None:
        reasons = self.report["unknown_reasons"]
        if len(reasons) < 16:
            reasons.append(reason[:2048])
        self.report.update(status="unknown", incomplete=True, observations_valid=False)

    def owner(self, label: str) -> Owner:
        owner = Owner(label)
        self.owners.append(owner)  # Registered before any native partial output.
        return owner

    def call(self, name: str, *arguments: Any, length: C.c_uint32 | None = None) -> dict[str, Any]:
        calls = self.report["native_calls"]
        if len(calls) >= MAX_CALLS:
            raise ValueError("token_native_call_budget")
        event: dict[str, Any] = {"api": name, "state": "call_entered", "raw_return": None,
                                 "last_error": None, "returned_length": None}
        calls.append(event)
        try:
            result, error = self.api.call(name, *arguments)
            event.update(state="returned", raw_return=result, last_error=error,
                         returned_length=None if length is None else int(length.value))
        except BaseException as error:
            event.update(state="wrapper_exception", error_type=type(error).__name__, error=str(error)[:512])
            raise
        finally:
            self.save()
        return event

    def query(self, owner: Owner, kind: int, label: str) -> Any:
        result: dict[str, Any] = {"subject": owner.label, "class": kind, "label": label,
                                  "payload_valid": False, "status": "unknown"}
        self.report["token_queries"].append(result)
        needed = DWORD()
        buffer: Any = None
        try:
            probe = self.call("GetTokenInformation", owner.handle, kind, None, 0,
                              C.byref(needed), length=needed)
            result["size_query"] = probe
            if probe["raw_return"] != 0 or probe["last_error"] != 122 or not 1 <= needed.value <= MAX_BUFFER:
                raise ValueError("token_size_query_status_or_budget")
            capacity = int(needed.value)
            buffer = C.create_string_buffer(capacity)
            received = DWORD()
            read = self.call("GetTokenInformation", owner.handle, kind, buffer, capacity,
                             C.byref(received), length=received)
            result["read_query"] = read
            if read["raw_return"] == 0 or not 1 <= received.value <= capacity:
                raise ValueError("token_read_status_or_length")
            data = bytes(buffer.raw[:received.value])
            result.update(buffer_sha256=sha256(data).hexdigest(), evidence_encoding="complete_required_structure_fields")
            value = _parse(kind, data, C.addressof(buffer))
            result.update(payload_valid=True, status="observed")
            return value  # Complete fields are retained once, in the named snapshot.
        except BaseException as error:
            result.update(status="unknown", payload_valid=False, incomplete=True,
                          error_type=type(error).__name__, error=str(error)[:512])
            if buffer is not None:
                raw = bytes(buffer.raw)
                result.update(buffer_sha256=sha256(raw).hexdigest(), bounded_buffer_prefix_hex=raw[:256].hex(),
                              allocated_bytes=len(raw), raw_prefix_complete=len(raw) <= 256)
            raise
        finally:
            self.save()

    def snapshot(self, owner: Owner, label: str, *, source: bool = False) -> dict[str, Any]:
        observation: dict[str, Any] = {}
        self.report[label] = observation
        for name, kind in QUERY_CLASSES:
            observation[name] = self.query(owner, kind, label + ":" + name)
        if source:
            observation["is_appcontainer"] = self.query(owner, 29, label + ":is_appcontainer")
        self.save()
        return observation

    def close(self) -> bool:
        complete = True
        seen: set[int] = set()
        for owner in reversed(self.owners):
            value = int(owner.handle.value or 0)
            record: dict[str, Any] = {"owner": owner.label, "had_handle": value != 0, "closed": None}
            self.report["close_results"].append(record)
            if value == 0:
                record.update(state="no_handle", closed=True)
                continue
            if not owner.valid() or value in seen:
                record.update(state="invalid_or_duplicate_handle_withheld", closed=False)
                self.unknown("close_invalid_or_duplicate:" + owner.label)
                complete = False
                continue
            seen.add(value)
            try:
                event = self.call("CloseHandle", owner.handle)
                record.update(native=event, state="returned", closed=event["raw_return"] != 0)
                if event["raw_return"] == 0:
                    raise ValueError("token_close_failed")
                owner.handle.value = None
            except BaseException as error:
                record.update(state="failed", closed=False, error_type=type(error).__name__, error=str(error)[:512])
                self.unknown("close_failed:" + owner.label)
                complete = False
        self.report["all_token_handles_closed"] = complete
        return complete


def _opened(session: Session, owner: Owner, event: dict[str, Any]) -> None:
    event["output_handle_present"] = bool(owner.handle.value)
    event["output_handle_valid"] = owner.valid()
    if event["raw_return"] == 0 or not owner.valid():
        raise ValueError("token_open_or_create_failed:" + owner.label)


def _thread_absent(session: Session, label: str) -> None:
    owner = session.owner(label)
    thread = session.call("GetCurrentThread")["raw_return"]
    event = session.call("OpenThreadToken", HANDLE(thread), TOKEN_QUERY, True, C.byref(owner.handle))
    event["output_handle_present"] = bool(owner.handle.value)
    if event["raw_return"] != 0 or event["last_error"] != 1008 or owner.handle.value:
        raise ValueError("token_thread_context_not_absent")
    session.report[label] = {"status": "absent", "open": event}
    session.save()


def _synthetic(before: dict[str, Any]) -> tuple[bytes, str]:
    parts = (21, *struct.unpack("<4I", uuid4().bytes))
    sid = "S-1-5-" + "-".join(str(part) for part in parts)
    known = {before["user"]["sid"], *(entry["sid"] for entry in before["groups"]),
             *(entry["sid"] for entry in before["restricted_sids"])}
    if sid in known:
        raise ValueError("token_synthetic_sid_collision")
    return bytes((1, len(parts))) + (5).to_bytes(6, "big") + struct.pack("<5I", *parts), sid


def _source_equal(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return all(before[name] == after[name] for name, _kind in QUERY_CLASSES)


def _observe(session: Session) -> None:
    source, derived = session.owner("source"), session.owner("derived")
    _thread_absent(session, "thread_before")
    process = session.call("GetCurrentProcess")["raw_return"]
    opened = session.call("OpenProcessToken", HANDLE(process), TOKEN_QUERY | TOKEN_DUPLICATE,
                          C.byref(source.handle))
    _opened(session, source, opened)
    before = session.snapshot(source, "source_before", source=True)
    restricted = session.call("IsTokenRestricted", source.handle)
    session.report["source_is_token_restricted"] = {"subject": "source", "native": restricted}
    if (before["type"] != 1 or before["is_appcontainer"] != 0 or before["restricted_sids"]
            or restricted["raw_return"] != 0 or before["user"]["sid"] in ("S-1-5-18", "S-1-5-19", "S-1-5-20")):
        raise ValueError("token_source_context_rejected")
    notify = Luid()
    lookup = session.call("LookupPrivilegeValueW", None, "SeChangeNotifyPrivilege", C.byref(notify))
    if lookup["raw_return"] == 0:
        raise ValueError("token_fixed_privilege_lookup_failed")
    notify_value = _luid(notify)
    session.report["se_change_notify_luid"] = notify_value
    encoded, synthetic = _synthetic(before)
    sid_buffer = C.create_string_buffer(encoded)
    restricting = SidAttributes(C.addressof(sid_buffer), 0)
    session.report["creation"] = {"state": "prepared", "flags_argument": FLAGS,
        "disable_sid_count": 0, "delete_privilege_count": 0, "restricted_sid_count": 1,
        "synthetic_sid": synthetic, "synthetic_sid_attributes_argument": 0,
        "flags_argument_is_not_write_effectiveness_evidence": True,
        "prepared_does_not_prove_native_call_entered": True}
    session.save()  # Persist source evidence and planned arguments before creation.
    if session.report.get("evidence_budget_exceeded"):
        raise ValueError("token_prepared_evidence_incomplete")
    created = session.call("CreateRestrictedToken", source.handle, FLAGS, 0, None, 0, None,
                           1, C.byref(restricting), C.byref(derived.handle))
    session.report["creation"].update(state="returned", native=created)
    _opened(session, derived, created)
    actual = session.snapshot(derived, "derived")
    check = session.call("IsTokenRestricted", derived.handle)
    session.report["derived_is_token_restricted"] = {"subject": "derived", "native": check}
    expected = [{"sid": synthetic, "attributes": actual["restricted_sids"][0]["attributes"]}] if len(actual["restricted_sids"]) == 1 else []
    gates = {"primary": actual["type"] == 1,
        "same_user_sid": actual["user"]["sid"] == before["user"]["sid"],
        "exact_single_restricting_sid": bool(expected) and actual["restricted_sids"] == expected,
        "is_token_restricted": check["raw_return"] != 0,
        "no_other_enabled_privilege": all(not (entry["attributes"] & 2) or entry["luid"] == notify_value
                                          for entry in actual["privileges"])}
    session.report["derived_gate_checks"] = gates
    if not all(gates.values()):
        raise ValueError("token_derived_gate_failed")
    after = session.snapshot(source, "source_after")
    session.report["source_token_unchanged"] = _source_equal(before, after)
    _thread_absent(session, "thread_after")
    if not session.report["source_token_unchanged"]:
        raise ValueError("token_source_changed")
    session.report["observations_valid"] = True


def _experiment(output: TextIO, report: dict[str, Any]) -> int:
    session: Session | None = None
    try:
        if os.name != "nt":
            raise ValueError("token_windows_x64_only")
        _layout()
        session = Session(NativeApi(), output, report)
        _observe(session)
    except BaseException as error:
        report.update(status="unknown", incomplete=True, observations_valid=False,
                      error_type=type(error).__name__, error=str(error)[:2048])
        if session is not None:
            session.unknown("observation:" + type(error).__name__ + ":" + str(error))
    finally:
        if session is not None:
            session.close()
        try:
            ending = _fingerprints()
            report.update(source_sha256_end=ending, source_unchanged=ending == report.get("source_sha256"))
        except BaseException as error:
            report.update(source_unchanged=False, source_end_error_type=type(error).__name__, source_end_error=str(error)[:512])
        accepted = (report.get("observations_valid") is True and report.get("all_token_handles_closed") is True
                    and report.get("source_unchanged") is True and not report.get("unknown_reasons")
                    and not report.get("evidence_budget_exceeded"))
        report.update(status="precursor_token_observed" if accepted else "unknown", incomplete=not accepted,
                      observations_valid=accepted)
        _save(output, report)
    return 0 if accepted and report.get("observations_valid") is True else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--experiment", action="store_true")
    args = parser.parse_args()
    path = _new_report(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_reparse(path)
    report: dict[str, Any] = {"stage0_passed": False, "scope": "precursor-token-only",
        "ac_conversion_verified": False, "write_boundary_verified": False,
        "write_restricted_bit_coverage_verified": False, "owner_rights_behavior_verified": False,
        "process_started": False, "profile_created": False, "impersonation_requested": False,
        "registry_access_requested": False, "dacl_mutation_requested": False,
        "source_privilege_mutation_requested": False, "adjust_token_privileges_api_requested": False,
        "fallback_allowed": False,
        "observations_valid": False, "incomplete": True,
        "evidence_budget_bytes": MAX_REPORT, "per_token_query_budget_bytes": MAX_BUFFER,
        "entry_count_budget": MAX_ENTRIES,
        "coverage_gap": "No AC conversion, actual object access, write denial, or stage-zero acceptance."}
    with path.open("x", encoding="ascii") as output:
        if not args.experiment:
            report["status"] = "not_executed_explicit_opt_in_required"
            _save(output, report)
            return 0
        try:
            report["source_sha256"] = _fingerprints()
            _save(output, report)
            code = _experiment(output, report)
        except BaseException as error:
            report.update(status="unknown", incomplete=True, error_type=type(error).__name__, error=str(error)[:2048])
            _save(output, report)
            code = 2
    print(json.dumps({"report": str(path), "diagnostic_exit": code, "stage0_passed": False}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
