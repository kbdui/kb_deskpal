"""Opt-in single-mode own-token V4 probe after verified Untrusted AC lowering.

The executable and source pins identify the approved fixed build receipt.
No registry, object-write probe, privilege adjustment, fallback or repair.
"""
from __future__ import annotations

import argparse
import ctypes as C
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any, TextIO, cast
from uuid import uuid4

import diagnose_appcontainer_integrity_smoke as smoke
from deskpal.execution.protocol import FrameDecoder, FrameLimits, ProtocolViolation
from deskpal.execution.windows.launcher import LaunchRejected, LaunchSpec, WorkerResult
from deskpal.execution.runtime import create_probe_profile, delete_probe_profile, worker_environment

mic, token, core, ROOT = smoke.mic, smoke.mic.token, smoke.mic.core, smoke.ROOT
Session, NativeApi, SmokeApi, SmokeWorker = mic.Session, mic.NativeApi, smoke.SmokeApi, smoke.SmokeWorker
observe_integrity, check_job, source_after = mic._observe, mic._job_check, smoke._source_after
create_worker, new_report = core._create_worker, smoke.new_report
MODES = ("raise-il", "disable-no-write-up")
FIXTURE = ROOT / "data/v06-stage0/native-integrity-fixture-v4/fixture.exe"
FIXTURE_SHA256 = "c8d8c7fdd8d79dad2d97fa36d2299494314ee0540ad308b3efa3f65e7386448c"
FIXTURE_SOURCE = ROOT / "tests/fixtures/windows/native_integrity_probe_v4.c"
FIXTURE_SOURCE_SHA256 = "42836e8c603344739dc31a0602a1f371b31c1eba0ecdcfc6c6b95054bc1aac6c"
FRAME_LIMITS = FrameLimits(max_frame_bytes=4096, max_stream_bytes=4100, max_depth=8,
    max_string_bytes=4096, max_container_items=64, max_nodes=512, max_frames=1)
_UNCONFIRMED_OWNERS: list[Any] = []
SNAP_KEYS = {"valid", "thread_absent", "type", "user", "package", "id", "ac", "cap",
    "il_known", "il", "il_attrs", "policy_known", "policy", "user_attrs"}
FRAME_KEYS = {"probe", "mode", "attack_succeeded", "resistance_observed", "unknown", "problems",
    "identity_same", "cleanup_ok", "open", "set", "before", "after", "refused_handle_count",
    "set_info", "set_bytes", "call_budget", "call_count_before_ipc", "maximum_total_calls", "calls", "ipc_write_calls_planned"}
QUERY_ORDER = (8, 1, 31, 10, 29, 30, 25, 27)
MAX_READ_EVENTS = 64


def _integer(value: Any, low: int = 0, high: int = 0xFFFFFFFF) -> bool:
    return type(value) is int and low <= value <= high


def _latch(frame: Any, report: dict[str, Any]) -> None:
    if not isinstance(frame, dict):
        return
    evidence: list[str] = []
    if frame.get("attack_succeeded") == 1:
        evidence.append("reported_attack_success")
    operation = frame.get("set")
    if isinstance(operation, list) and len(operation) >= 2 and isinstance(operation[1], (int, float)) and operation[1] != 0:
        evidence.append("reported_set_success")
    for label in ("before", "after"):
        value = frame.get(label)
        if not isinstance(value, dict):
            continue
        if value.get("il_known") == 1 and isinstance(value.get("il"), (int, float)) and value["il"] > 0:
            evidence.append(label + ":il_above_host_untrusted")
        policy = value.get("policy")
        if value.get("policy_known") == 1 and isinstance(policy, int) and not policy & 1:
            evidence.append(label + ":no_write_up_lost")
    if evidence:
        report["security_boundary_failed"] = True
        report.setdefault("boundary_failure_evidence", []).extend(evidence[:6])


class _ReadEvidence:
    """Observe the existing ReadFile call before another pipe/poll can fail.

    This is evidence only: the canonical collector still owns all transport,
    deadline and cleanup decisions. No read, retry or changed native result.
    """

    def __init__(self, session: mic.Session, native: Any) -> None:
        readers = native.readers
        if (type(readers) is not tuple or len(readers) != 3
                or any(not _integer(handle, 1, (1 << 64) - 1) or C.c_void_p(handle).value in token.PSEUDO_HANDLES for handle in readers)
                or len(set(readers)) != 3):
            raise ValueError("regain_read_evidence_reader_registration")
        self.session, self.report = session, session.report
        self.roles = dict(zip(readers, ("control", "stdout", "stderr"), strict=True))
        self.original_read = native.api.read
        self.decoder = FrameDecoder(FRAME_LIMITS)
        self.finished = False
        self.prefixes = {role: bytearray() for role in self.roles.values()}
        self.evidence: dict[str, Any] = {"registered_readers": dict(zip(self.roles.values(), readers, strict=True)),
            "events": [], "calls_observed": 0, "events_withheld": 0, "frames": [],
            "channels": {role: {"observed_bytes": 0, "retained_bytes": 0, "truncated": False,
                "prefix_limit_bytes": 4101 if role == "control" else 4097, "prefix_hex": ""} for role in self.roles.values()}}
        self.report["read_evidence"] = self.evidence
        native.api.read = self.read

    def _unknown(self, reason: str) -> None:
        self.session.unknown("regain_read_evidence:" + reason)

    def _save(self) -> None:
        try:
            self.session.save()
        except BaseException as error:
            self.evidence.setdefault("save_error_type", type(error).__name__)
            self._unknown("save_failed")

    def _observe(self, args: tuple[Any, ...], raw: Any, error: int, raised: BaseException | None) -> None:
        self.evidence["calls_observed"] += 1
        event: dict[str, Any] = {"state": "wrapper_exception" if raised is not None else "returned",
            "raw_return": raw if type(raw) is int else None, "last_error": error}
        if raised is not None:
            event["error_type"] = type(raised).__name__
            self._unknown("native_read_exception")
        events = self.evidence["events"]
        if len(events) < MAX_READ_EVENTS:
            events.append(event)
        else:
            self.evidence["events_withheld"] += 1
            if "first_withheld_event" not in self.evidence:
                self.evidence["first_withheld_event"] = {"state": "event_budget", "event_limit": MAX_READ_EVENTS}
                self._unknown("event_budget")
        # Only inspect the exact ctypes objects supplied by canonical
        # NativeWorker.read_available, never dereference an arbitrary pointer.
        if len(args) != 5:
            event["evidence_error"] = "argument_count"
            self._unknown("argument_count")
            return
        handle, buffer, requested, count_pointer, overlapped = args
        role = self.roles.get(handle) if type(handle) is int else None
        count = getattr(count_pointer, "_obj", None)
        if (role is None or type(requested) is not int or not 0 < requested <= 16384 or overlapped is not None
                or not isinstance(buffer, C.Array) or getattr(buffer, "_type_", None) is not C.c_char
                or C.sizeof(buffer) != requested or type(count) is not core.DWORD
                or C.cast(count_pointer, C.c_void_p).value != C.addressof(count)):
            event["evidence_error"] = "unrecognized_read_abi"
            self._unknown("unrecognized_read_abi")
            return
        event.update(role=role, requested_bytes=requested, returned_bytes=count.value)
        if count.value > requested:
            event["evidence_error"] = "returned_count_exceeds_request"
            self._unknown("returned_count_exceeds_request")
            return
        if type(raw) is not int or not -(1 << 31) <= raw < 1 << 31 or not raw:
            self._unknown("native_read_not_successful")
        channel = self.evidence["channels"][role]
        channel["observed_bytes"] += count.value
        prefix = self.prefixes[role]
        keep = min(count.value, channel["prefix_limit_bytes"] - len(prefix))
        # Failed/partial calls retain their bounded output too. A complete
        # positive frame remains conservative failure evidence, never a pass.
        data = buffer.raw[:keep]
        prefix.extend(data)
        channel.update(retained_bytes=len(prefix), truncated=channel["observed_bytes"] > len(prefix), prefix_hex=prefix.hex())
        if role == "control" and "shadow_protocol_violation" not in self.evidence:
            for byte in data:
                try:
                    frames = self.decoder.feed(bytes((byte,)))
                except ProtocolViolation as failure:
                    self.evidence["shadow_protocol_violation"] = failure.code
                    self._unknown("shadow_protocol_violation")
                    break
                for frame in frames:
                    self.evidence["frames"].append(frame)
                    _latch(frame, self.report)
                    self._save()  # Before any later byte, pipe, poll or close.

    def read(self, *args: Any) -> Any:
        raw: Any = None
        raised: BaseException | None = None
        try:
            raw = self.original_read(*args)  # Exactly the original one call.
            return raw
        except BaseException as error:
            raised = error
            raise
        finally:
            native_error = C.get_last_error()
            try:
                try:
                    self._observe(args, raw, native_error, raised)
                except BaseException as observation_error:
                    self.evidence.setdefault("observation_error_type", type(observation_error).__name__)
                    self._unknown("observation_exception")
                self._save()
            finally:
                C.set_last_error(native_error)

    def finish(self, complete: bool) -> None:
        if self.finished:
            return
        self.finished = True
        self.evidence["collection_returned"] = complete
        if "shadow_protocol_violation" not in self.evidence:
            try:
                self.decoder.finish()
            except ProtocolViolation as error:
                self.evidence["shadow_protocol_violation"] = error.code
                self._unknown("shadow_protocol_violation")
        self._save()


def _sid(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 192 or not re.fullmatch(r"S-1-(?:0|[1-9][0-9]*)(?:-(?:0|[1-9][0-9]*)){0,15}", value):
        return False
    parts = [int(part) for part in value.split("-")[2:]]
    return parts[0] < 1 << 48 and all(part <= 0xFFFFFFFF for part in parts[1:])


def _host_readback(session: mic.Session) -> None:
    report = session.report
    if report.get("host_readback_attempted"):
        raise ValueError("regain_host_readback_retry_withheld")
    report["host_readback_attempted"] = True
    worker = next((owner for owner in session.owners if owner.label == "worker"), None)
    if worker is None or not worker.valid():
        raise ValueError("regain_retained_worker_token_missing")
    actual: dict[str, Any] = {}
    report["host_runtime_after"] = actual
    errors: list[dict[str, str]] = []
    for name, kind in (("integrity", 25), ("mandatory_policy", 27)):
        try:
            value = session.query(worker, kind, "host_runtime_after:" + name)
            actual[name] = value
            weakened = int(value["sid"].split("-")[-1]) > 0 if kind == 25 else not value & 1
            if weakened:
                report["security_boundary_failed"] = True
                report.setdefault("boundary_failure_evidence", []).append("independent_host_runtime:" + name)
        except BaseException as error:
            errors.append({"query": name, "error_type": type(error).__name__, "error": str(error)[:512]})
            report["host_readback_errors"] = errors
            session.unknown("regain_host_readback:" + name + ":" + type(error).__name__)
        session.save()
    report["host_runtime_matches_prelaunch"] = len(actual) == 2 and all(actual[name] == report["worker_after"][name] for name in actual)
    session.save()
    if errors:
        raise ValueError("regain_host_readback_incomplete")


def _snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != SNAP_KEYS:
        raise ValueError("regain_snapshot_keys")
    for name in SNAP_KEYS - {"user", "package", "id"}:
        if not _integer(value[name], 0, 1 if name in ("valid", "thread_absent", "il_known", "policy_known") else 0xFFFFFFFF):
            raise ValueError("regain_snapshot_integer")
    identity = value["id"]
    if not isinstance(identity, list) or len(identity) != 2 or not _integer(identity[0]) or not _integer(identity[1], -(1 << 31), (1 << 31) - 1):
        raise ValueError("regain_snapshot_luid")
    for name in ("user", "package"):
        if not _sid(value[name]) and not (value["valid"] == 0 and value[name] == ""):
            raise ValueError("regain_snapshot_sid")
    if value["package"] and (not value["package"].startswith("S-1-15-2-") or len(value["package"].split("-")) != 11):
        raise ValueError("regain_snapshot_package_sid")
    if value["policy_known"] and value["policy"] & ~3:
        raise ValueError("regain_snapshot_policy")
    if value["valid"] and (value["type"] != 1 or value["ac"] != 1 or value["cap"] != 0
            or not all(value[key] == 1 for key in ("thread_absent", "il_known", "policy_known")) or not value["il_attrs"] & 0x20):
        raise ValueError("regain_snapshot_valid_flag")
    return value


def _assess(result: WorkerResult, mode: str, host: dict[str, Any], report: dict[str, Any]) -> bool:
    if len(result.frames) != 1:
        raise ValueError("regain_one_frame_required")
    frame = cast(dict[str, Any], result.frames[0])
    if set(frame) != FRAME_KEYS or frame["probe"] != "own_token_v4" or frame["mode"] != mode:
        raise ValueError("regain_frame_schema_or_mode")
    for name in ("attack_succeeded", "resistance_observed", "unknown", "identity_same", "cleanup_ok"):
        if not _integer(frame[name], 0, 1):
            raise ValueError("regain_c_flag_requires_int_zero_or_one")
    for name in ("problems", "refused_handle_count", "set_info", "set_bytes", "call_budget",
                 "call_count_before_ipc", "maximum_total_calls", "ipc_write_calls_planned"):
        if not _integer(frame[name]):
            raise ValueError("regain_frame_integer")
    if (frame["problems"] & ~0x7FF or frame["refused_handle_count"] > 3 or frame["call_budget"] != 48
            or frame["ipc_write_calls_planned"] != 2 or frame["maximum_total_calls"] != frame["call_count_before_ipc"] + 2
            or frame["maximum_total_calls"] > 48 or (frame["set_info"], frame["set_bytes"]) != ((25, 28) if mode == MODES[0] else (27, 4))):
        raise ValueError("regain_mode_or_call_budget")
    opened, changed = frame["open"], frame["set"]
    if (not isinstance(opened, list) or len(opened) != 2 or not isinstance(changed, list) or len(changed) != 3
            or not _integer(opened[0], -(1 << 31), (1 << 31) - 1) or not _integer(opened[1])
            or not _integer(changed[0], 0, 1) or not _integer(changed[1], -(1 << 31), (1 << 31) - 1) or not _integer(changed[2])):
        raise ValueError("regain_open_set_raw_shape")
    before, after = _snapshot(frame["before"]), _snapshot(frame["after"])
    calls = frame["calls"]
    if not isinstance(calls, list) or len(calls) != frame["call_count_before_ipc"]:
        raise ValueError("regain_call_array_length")
    for row in calls:
        if (not isinstance(row, list) or len(row) != 6 or not all(_integer(item) for item in row[:3] + row[4:])
                or not _integer(row[3], -(1 << 31), (1 << 31) - 1) or row[0] not in (1, 2, 3, 4, 5) or row[2] != 2
                or (row[0] == 1 and (row[1] not in QUERY_ORDER or row[5] > 65536))
                or (row[0] == 2 and (row[1] not in (1, 2) or row[5] not in (0, 1)))
                or (row[0] == 3 and (row[1] != 0x88 or row[5] not in (0, 1)))
                or (row[0] == 4 and (row[1], row[5]) != (frame["set_info"], frame["set_bytes"]))
                or (row[0] == 5 and (row[1] not in (1, 2, 3) or row[5] != 0))):
            raise ValueError("regain_call_row")
        if row[0] == 1 and row[3] != 0:
            minimum = {8: 4, 1: 16, 31: 8, 10: 56, 29: 4, 30: 4, 25: 16, 27: 4}[row[1]]
            if row[5] < minimum or (row[1] in (8, 10, 29, 27) and row[5] != minimum):
                raise ValueError("regain_query_returned_length")
    opens, sets = [row for row in calls if row[0] == 3], [row for row in calls if row[0] == 4]
    closes, threads = [row for row in calls if row[0] == 5], [row for row in calls if row[0] == 2]
    if (len(opens) > 1 or len(sets) > 1 or len({row[1] for row in closes}) != len(closes)
            or len(threads) > 2 or len({row[1] for row in threads}) != len(threads)
            or opened != ([opens[0][3], opens[0][4]] if opens else [0, 0])
            or changed != ([1, sets[0][3], sets[0][4]] if sets else [0, 0, 0])
            or (sets and (not opens or not opens[0][3] or opens[0][5] != 1))):
        raise ValueError("regain_single_open_set_close_or_summary")
    roles = {row[1] for row in closes}
    statistics = sum(row[0:2] == [1, 10] for row in calls)
    expected_trace = [(2, 1), *([(5, 1)] if 1 in roles else []), *[(1, kind) for kind in QUERY_ORDER]]
    if opens:
        expected_trace += [(3, 0x88)]
    if statistics == 3:
        expected_trace += [(1, 10)]
    if sets:
        expected_trace += [(4, frame["set_info"])]
    expected_trace += [(2, 2), *([(5, 2)] if 2 in roles else []), *[(1, kind) for kind in QUERY_ORDER], *([(5, 3)] if 3 in roles else [])]
    if ([(row[0], row[1]) for row in calls] != expected_trace
            or statistics not in (2, 3) or (sets and statistics != 3)
            or (statistics == 3 and (not opens or not opens[0][3] or opens[0][5] != 1))
            or any(not any(row[1] == role and row[5] == 1 for row in threads) for role in roles & {1, 2})
            or (3 in roles and (not opens or opens[0][5] != 1))):
        raise ValueError("regain_native_trace_order")
    stable_keys = ("type", "user", "user_attrs", "package", "id", "ac", "cap", "il_attrs")
    identity_same = bool(before["valid"] and after["valid"] and all(before[key] == after[key] for key in stable_keys))
    attack = bool(changed[1] or (before["il_known"] and after["il_known"] and after["il"] > before["il"])
        or (before["policy_known"] and before["policy"] & 1 and after["policy_known"] and not after["policy"] & 1))
    denied = bool((opens and opened == [0, 5] and opens[0][5] == 0) or (changed[0] == 1 and changed[1] == 0 and changed[2] in (5, 1314)))
    unchanged = before["il"] == after["il"] and before["policy"] == after["policy"]
    baseline = bool(before["valid"] and before["il_known"] and before["il"] == 0 and before["policy_known"] and before["policy"] & 1)
    c_resistance = not attack and not frame["problems"] and baseline and identity_same and denied and bool(frame["cleanup_ok"]) and unchanged
    c_unknown = not attack and bool(frame["problems"] or not denied or not frame["cleanup_ok"] or not unchanged)
    if (frame["attack_succeeded"] != int(attack) or frame["identity_same"] != int(identity_same)
            or frame["resistance_observed"] != int(c_resistance) or frame["unknown"] != int(c_unknown)):
        raise ValueError("regain_report_flag_inconsistency")
    if frame["cleanup_ok"] != int(not frame["refused_handle_count"] and all(row[3] != 0 for row in closes)):
        raise ValueError("regain_cleanup_flag_inconsistency")
    for phase, snapshot in ((1, before), (2, after)):
        row = next(row for row in threads if row[1] == phase)
        if snapshot["thread_absent"] != int(row[3:6] == [0, 1008, 0]):
            raise ValueError("regain_thread_flag_inconsistency")
    report.update(fixture_body_confirmed=True, worker_code_executed=True)
    expected = {"valid": 1, "thread_absent": 1, "type": host["type"], "user": host["user"]["sid"],
        "user_attrs": host["user"]["attributes"], "package": host["package_sid"],
        "id": [host["statistics"]["token_id"]["low_dword"], host["statistics"]["token_id"]["high_long"]],
        "ac": host["is_appcontainer"], "cap": len(host["capabilities"]), "il_known": 1,
        "il": int(host["integrity"]["sid"].split("-")[-1]), "il_attrs": host["integrity"]["attributes"],
        "policy_known": 1, "policy": host["mandatory_policy"]}
    query_trace = [(2, 1), *[(1, kind) for kind in QUERY_ORDER], (3, 0x88)]
    if opened[0]:
        query_trace += [(1, 10), (4, frame["set_info"])]
    query_trace += [(2, 2), *[(1, kind) for kind in QUERY_ORDER]]
    if opened[0]:
        query_trace += [(5, 3)]
    trace_ok = [(row[0], row[1]) for row in calls] == query_trace and all(
        (row[3] != 0 if row[0] in (1, 5) else row[3:6] == [0, 1008, 0] if row[0] == 2 else True) for row in calls)
    report["runtime_context_matches_host"] = before == after == expected
    runtime = report.get("host_runtime_after", {})
    report["c_after_matches_host_runtime"] = (runtime.get("integrity") == {"sid": "S-1-16-" + str(after["il"]), "attributes": after["il_attrs"]}
        and runtime.get("mandatory_policy") == after["policy"])
    return bool(c_resistance and trace_ok and before == after == expected and expected["type"] == expected["ac"] == 1
        and expected["cap"] == expected["il"] == 0 and expected["policy"] & 1 and not frame["refused_handle_count"]
        and report.get("host_runtime_matches_prelaunch") is True and report["c_after_matches_host_runtime"]
        and result.exit_code == 0 and not result.stdout and not result.stderr and not report.get("security_boundary_failed"))


def _fingerprints() -> dict[str, str]:
    paths = ("scripts/diagnose_appcontainer_token_regain.py", "tests/unit/test_appcontainer_token_regain_diagnostic.py",
             "tests/fixtures/windows/native_integrity_probe_v4.c", "data/v06-stage0/native-integrity-fixture-v4/fixture.exe")
    return smoke._fingerprints() | {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}


def _experiment(output: TextIO, report: dict[str, Any]) -> int:
    session: mic.Session | None = None
    adapter: smoke.SmokeApi | None = None
    worker: smoke.SmokeWorker | None = None
    native: Any = None
    profile: Any = None
    destination: Path | None = None
    retained: list[Any] = []
    read_evidence: _ReadEvidence | None = None
    try:
        mode = report.get("mode")
        if mode not in MODES:
            raise ValueError("regain_fixed_mode_required")
        if not re.fullmatch(r"[0-9a-f]{64}", FIXTURE_SHA256):
            raise ValueError("regain_executable_unbuilt_no_approved_pin")
        if os.name != "nt":
            raise ValueError("regain_windows_x64_only")
        token._layout()
        for path, expected_pin in ((FIXTURE, FIXTURE_SHA256), (FIXTURE_SOURCE, FIXTURE_SOURCE_SHA256)):
            token._reject_reparse(path)
            if sha256(path.read_bytes()).hexdigest() != expected_pin:
                raise ValueError("regain_fixed_pin_mismatch")
        session = Session(NativeApi(), output, report)
        report.update(adapter_close_events=[], post_lower_job_calls=[])
        profile = create_probe_profile("deskpal.v06." + uuid4().hex)
        report.update(profile=asdict(profile), cleanup="pending")
        session.save()
        folder = Path(profile.folder)
        token._reject_reparse(folder)
        temporary, destination = folder / "Temp", folder / "native-integrity.exe"
        token._reject_reparse(temporary)
        temporary.mkdir(exist_ok=True)
        token._reject_reparse(temporary)
        token._reject_reparse(destination)
        with FIXTURE.open("rb") as source, destination.open("xb") as copied:
            copied.write(source.read())
        if sha256(destination.read_bytes()).hexdigest() != FIXTURE_SHA256:
            raise ValueError("regain_copy_pin_mismatch")
        windows = C.create_unicode_buffer(32768)
        length = session.call("GetWindowsDirectoryW", windows, len(windows))["raw_return"]
        if not 0 < length < len(windows):
            raise ValueError("regain_windows_directory")
        environment = worker_environment(Path(windows.value), temporary, local_appdata=folder)
        report["requested_child_environment"] = environment
        spec = LaunchSpec(destination, ("{ipc_write_handle}", mode), folder, profile.sid, environment)

        def observe(process: int) -> None:
            assert session is not None and adapter is not None
            report["original_adapter_verification_reached_resume_hook"] = True
            observe_integrity(session, process, profile.sid)
            check_job(adapter, spec, process, report)
            report["lowering_and_identity_observed"] = True

        adapter = SmokeApi(session, observe)
        try:
            with core._CREATE_LOCK:
                native, identity = create_worker(spec, _api=adapter)
        except LaunchRejected as error:
            report.update(launch_stage=error.stage, launch_winerror=error.winerror,
                          launch_cleanup_owner_present=error.cleanup_owner is not None)
            if error.cleanup_owner is not None:
                retained.append(error.cleanup_owner)
                report["launch_owner_disposition"] = "retained_no_automatic_retry"
            raise
        worker = SmokeWorker(native, identity, report)
        if native.api is not adapter:
            raise ValueError("regain_read_evidence_native_adapter_mismatch")
        read_evidence = _ReadEvidence(session, native)
        if adapter.failed_closes or report.get("unknown_reasons"):
            raise ValueError("regain_post_launch_unknown")
        report["collect"] = {"timeout_seconds": 5.0, "frame_limits": asdict(FRAME_LIMITS), "log_limit_bytes": 4096}
        session.save()
        try:
            result = worker.collect(5.0, frame_limits=FRAME_LIMITS, log_limit_bytes=4096)
        except BaseException as error:
            report["collect_error"] = {"error_type": type(error).__name__, "error": str(error)[:1024]}
            if error.__context__ is not None:
                report["collect_error"].update(context_error_type=type(error.__context__).__name__,
                    context_error=str(error.__context__)[:1024])
            read_evidence.finish(False)
            raise
        read_evidence.finish(True)
        report["raw_result"] = {"identity": asdict(result.identity), "exit_code": result.exit_code,
            "frames": list(result.frames), "stdout_hex": result.stdout.hex(), "stderr_hex": result.stderr.hex(), "elapsed_seconds": result.elapsed_seconds}
        for frame in result.frames:
            _latch(frame, report)
        session.save()  # Preserve raw result and any failure latch before assessment/cleanup.
        if report["read_evidence"]["frames"] != list(result.frames):
            session.unknown("regain_read_evidence_canonical_frame_mismatch")
        _host_readback(session)
        report["resistance_observed"] = _assess(result, mode, report["worker_after"], report)
        if not report["resistance_observed"]:
            raise ValueError("regain_resistance_unconfirmed_or_boundary_failed")
        session.save()
        worker.terminate()
        worker.close()
        report["worker_cleanup_confirmed"] = True
    except BaseException as error:
        report.update(error_type=type(error).__name__, error=str(error)[:1024])
        context = error.__context__
        if isinstance(error, ProtocolViolation):
            report["protocol_violation"] = error.code
        elif isinstance(context, ProtocolViolation):
            report["protocol_violation"] = context.code
        if isinstance(error, LaunchRejected):
            report.update(rejected_stage=error.stage, rejected_winerror=error.winerror)
        if session is not None:
            session.unknown("regain:" + type(error).__name__ + ":" + str(error))
    finally:
        # The retained query/adjust token survives process collection/reaping.
        # Try each independent runtime query once even if collect never returns
        # its local frames; do this before source_after closes token owners.
        if (session is not None and report.get("native_resume_called") is True
                and not report.get("host_readback_attempted")
                and any(owner.label == "worker" and owner.valid() for owner in session.owners)):
            try:
                _host_readback(session)
            except BaseException as error:
                report["host_readback_error"] = {"error_type": type(error).__name__, "error": str(error)[:512]}
                session.unknown("regain_host_readback_after_error:" + type(error).__name__)
        if worker is not None:
            if not worker.close_attempted and not worker.cleanup_failed:
                try:
                    worker.close()
                except BaseException as error:
                    report["necessary_cleanup_error"] = type(error).__name__
            if worker.cleanup_failed:
                retained.append(worker)
                report["worker_owner_disposition"] = "retained_no_automatic_retry"
        elif native is not None:
            try:
                native.close()
                report["unexpected_native_cleanup"] = "one_necessary_close_returned"
            except BaseException:
                retained.append(native)
                report["unexpected_native_cleanup"] = "failed_owner_retained_no_retry"
        if session is not None:
            source_after(session)
            retained.extend(owner for owner in session.owners if owner.valid())
            report.update(host_token_native_calls_observed=len(report["native_calls"]),
                          host_token_queries_observed=len(report["token_queries"]))
        if adapter is not None:
            report.update(creation_flags=adapter.creation_flags, resume_intercepted=adapter.resume_intercepted, actual_resume_calls=adapter.resume_calls)
            retained.extend(adapter.withheld_tokens)
            if adapter.failed_closes and native is not None:
                retained.append(native)
        _UNCONFIRMED_OWNERS.extend(retained)
        try:
            ending = _fingerprints()
            report.update(source_sha256_end=ending, source_unchanged=ending == report.get("source_sha256"))
        except BaseException as error:
            report.update(source_unchanged=False, source_end_error_type=type(error).__name__)
        accepted = (report.get("resistance_observed") is True and report.get("worker_cleanup_confirmed") is True
            and report.get("resume_count_verified") is True and report.get("actual_resume_calls") == 1
            and report.get("all_token_handles_closed") is True and report.get("source_token_unchanged") is True and report.get("source_unchanged") is True
            and not retained and adapter is not None and not adapter.failed_closes and not report.get("security_boundary_failed")
            and not report.get("unknown_reasons") and not report.get("evidence_budget_exceeded"))
        if accepted and destination is not None:
            try:
                token._reject_reparse(destination)
                report["copied_fixture_unchanged"] = sha256(destination.read_bytes()).hexdigest() == FIXTURE_SHA256
                accepted = report["copied_fixture_unchanged"] is True
            except BaseException:
                accepted = False
        if profile is not None:
            if accepted:
                try:
                    delete_probe_profile(profile)
                    report["cleanup"] = "profile_deleted"
                except BaseException:
                    accepted = False
                    report["cleanup"] = "failed_profile_may_remain"
            else:
                report["cleanup"] = "withheld_unknown_or_boundary_failure"
        report.update(status="security_boundary_failure" if report.get("security_boundary_failed") else "own_token_regain_resistance_observed" if accepted else "unknown",
                      observations_valid=accepted, incomplete=not accepted)
        token._save(output, report)
    return 0 if accepted and not report.get("evidence_budget_exceeded") else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--experiment", action="store_true")
    parser.add_argument("--mode", choices=MODES, required=True)
    args = parser.parse_args()
    path = new_report(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    token._reject_reparse(path)
    report: dict[str, Any] = {"mode": args.mode, "started_at": datetime.now(timezone.utc).isoformat(), "stage0_passed": False,
        "write_boundary_verified": False, "security_boundary_failed": False, "fixture_body_confirmed": False,
        "worker_code_executed": None, "native_resume_called": False, "cleanup": "not_required", "fallback_allowed": False,
        "registry_access_requested": False, "object_write_probe_requested": False, "fixture_sha256": FIXTURE_SHA256,
        "fixture_source_sha256": FIXTURE_SOURCE_SHA256, "token_native_call_budget": token.MAX_CALLS, "fixture_call_budget": 48,
        "resume_call_budget": 1, "evidence_budget_bytes": token.MAX_REPORT,
        "expected_complete_host_token_calls": 95, "expected_complete_host_token_queries": 42,
        "per_token_query_budget_bytes": token.MAX_BUFFER, "entry_count_budget": token.MAX_ENTRIES,
        "adapter_close_call_budget": mic.MAX_ADAPTER_CLOSES, "post_lower_job_call_budget": mic.MAX_JOB_CALLS,
        "scope": "One own-token fixed attack in one fresh AC worker; no repair after any weakening.",
        "coverage_gap": "No registry/file write denial, object LABEL or whole namespace acceptance."}
    with path.open("x", encoding="ascii", newline="\n") as output:
        if not args.experiment:
            report["status"] = "not_executed_explicit_opt_in_required"
            token._save(output, report)
            return 0
        try:
            report["source_sha256"] = _fingerprints()
            token._save(output, report)
            code = _experiment(output, report)
        except BaseException as error:
            report.update(status="security_boundary_failure" if report.get("security_boundary_failed") else "unknown",
                          error_type=type(error).__name__, observations_valid=False, incomplete=True)
            token._save(output, report)
            code = 2
    print(json.dumps({"report": str(path), "diagnostic_exit": code, "stage0_passed": False}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
