"""Opt-in V5 selected synthetic registry MIC observation; never stage-zero acceptance.

One driver-owned fresh profile, independent Low/Untrusted processes, a host-only
read-only root binding, and fixed eight-byte canaries. No ACL repair or retry.
The unbuilt executable sentinel blocks every API/profile operation.
"""
from __future__ import annotations

import argparse
import ctypes as C
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import struct
import threading
from typing import Any, Literal, TextIO, cast
from uuid import uuid4

import diagnose_appcontainer_integrity_smoke as smoke
import diagnose_registry_integrity_binding as binding
from deskpal.execution.protocol import FrameDecoder, FrameLimits, ProtocolViolation
from deskpal.execution.runtime import ProbeProfile, create_probe_profile, delete_probe_profile, worker_environment
from deskpal.execution.windows.launcher import LaunchRejected, LaunchSpec, WorkerResult

mic, token, core, ROOT = smoke.mic, smoke.mic.token, smoke.mic.core, smoke.ROOT
Session, NativeApi, SmokeWorker = mic.Session, mic.NativeApi, smoke.SmokeWorker
create_worker = core._create_worker
EXE_PIN = "UNBUILT"
FIXTURE = ROOT / "data/v06-stage0/native-registry-integrity-fixture-v5/fixture.exe"
SOURCE = ROOT / "tests/fixtures/windows/native_registry_integrity_probe_v5.c"
SOURCE_PIN = "3f39f093e791e6fbfd3dec45277b3c8fb93585e3abf4a224373be148d89dd6cc"
HELPER_PIN = "262ae0b879fbb3e09ca44f12e48ec7b3bc25aaac9da4b0b7374e8d8f6afe9fff"
FRAME_LIMITS = FrameLimits(max_frame_bytes=8192, max_stream_bytes=8196, max_depth=8,
    max_string_bytes=4096, max_container_items=64, max_nodes=1024, max_frames=1)
MAX_READ_EVENTS = 64
Phase = Literal["low", "untrusted"]
_REGISTERED: set[RegisteredWorker] = set()
_RETAINED: list[Any] = []
SNAP_KEYS = {"valid", "thread_absent", "type", "user", "package", "id", "ac", "cap", "il_known", "il",
    "il_attrs", "policy_known", "policy", "user_attrs"}
SD_KEYS = {"valid", "reason", "sd_bytes", "control", "owner", "dacl_bytes", "dacl_count", "acl_revision",
    "dacl_complete", "dacl_hex", "label_bytes", "label_count", "label_complete", "label_hex", "il", "label_mask",
    "label_flags", "package_count", "package_flags", "package_mask"}
FRAME_KEYS = {"probe", "mode", "boundary_failed", "positive_observed", "resistance_observed", "unknown", "problem",
    "cleanup_ok", "refused_handles", "release_failed", "canary_hex", "nonce", "token_snapshots", "security_snapshots",
    "gates", "key_owners", "thread_partial_hex", "thread_close_attempted", "unreleased_descriptor_hex", "canary_reads",
    "canary_read_encoding", "call_budget", "call_count_before_ipc", "maximum_total_calls", "call_blocks",
    "ipc_write_calls_planned", "node_bound", "frame_budget"}
QUERY_ORDER = (8, 1, 31, 10, 29, 30, 25, 27)


def _integer(value: Any, low: int = 0, high: int = 0xFFFFFFFF) -> bool:
    return type(value) is int and low <= value <= high


def _failure(report: dict[str, Any], evidence: str) -> None:
    report["security_boundary_failed"] = True
    items = report.setdefault("boundary_failure_evidence", [])
    if evidence not in items and len(items) < 16:
        items.append(evidence)


def _latch(frame: Any, report: dict[str, Any]) -> None:
    if not isinstance(frame, dict):
        return
    flag = frame.get("boundary_failed")
    if isinstance(flag, (int, float)) and flag != 0:
        _failure(report, "reported_boundary_failure")
    if report.get("phase") != "untrusted":
        return
    blocks = frame.get("call_blocks")
    if isinstance(blocks, list):
        for block in blocks[:2]:
            if not isinstance(block, list):
                continue
            for row in block[:40]:
                if (isinstance(row, list) and len(row) >= 4 and row[3] == 0
                        and (row[:2] in ([7, 4], [10, 4], [11, 2]))):
                    _failure(report, "reported_negative_permission_or_mutation_success")
    snapshots = frame.get("token_snapshots")
    if isinstance(snapshots, list):
        for snapshot in snapshots[:2]:
            if isinstance(snapshot, dict):
                if snapshot.get("il_known") == 1 and isinstance(snapshot.get("il"), (int, float)) and snapshot["il"] > 0:
                    _failure(report, "reported_runtime_integrity_above_untrusted")
                if snapshot.get("policy_known") == 1 and isinstance(snapshot.get("policy"), int) and not snapshot["policy"] & 1:
                    _failure(report, "reported_runtime_no_write_up_lost")


def _expected_token(host: dict[str, Any]) -> dict[str, Any]:
    return {"valid": 1, "thread_absent": 1, "type": host["type"], "user": host["user"]["sid"],
        "package": host["package_sid"], "id": [host["statistics"]["token_id"]["low_dword"],
        host["statistics"]["token_id"]["high_long"]], "ac": host["is_appcontainer"], "cap": len(host["capabilities"]),
        "il_known": 1, "il": int(host["integrity"]["sid"].rsplit("-", 1)[1]), "il_attrs": host["integrity"]["attributes"],
        "policy_known": 1, "policy": host["mandatory_policy"], "user_attrs": host["user"]["attributes"]}


def _sid_bytes(text: str) -> bytes:
    binding._canonical_sid(text)
    parts = [int(part) for part in text.split("-")[2:]]
    return bytes((1, len(parts) - 1)) + parts[0].to_bytes(6, "big") + struct.pack("<" + "I" * (len(parts) - 1), *parts[1:])


def _sd(snapshot: Any, package: str, user: str, *, root: bool) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or set(snapshot) != SD_KEYS:
        raise ValueError("registry_sd_schema")
    for key in SD_KEYS - {"owner", "dacl_hex", "label_hex"}:
        if not _integer(snapshot[key], 0, 1 if key in ("valid", "dacl_complete", "label_complete") else 0xFFFFFFFF):
            raise ValueError("registry_sd_c_integer")
    if (snapshot["valid"] != 1 or snapshot["reason"] or snapshot["dacl_complete"] != 1 or snapshot["label_complete"] != 1
            or not 20 <= snapshot["sd_bytes"] <= 65536 or not 8 <= snapshot["dacl_bytes"] <= 512
            or not 8 <= snapshot["label_bytes"] <= 64 or not 1 <= snapshot["dacl_count"] <= 8
            or snapshot["label_count"] != 1 or snapshot["owner"] != user):
        raise ValueError("registry_sd_unknown_or_budget")
    chunks = []
    for key, size in (("dacl_hex", snapshot["dacl_bytes"]), ("label_hex", snapshot["label_bytes"])):
        value = snapshot[key]
        if not isinstance(value, str) or len(value) != size * 2 or not re.fullmatch(r"[0-9a-f]+", value):
            raise ValueError("registry_sd_raw_acl")
        chunks.append(bytes.fromhex(value))
    dacl, label = chunks
    owner = _sid_bytes(user)
    dacl_offset, label_offset = 20 + len(owner), 20 + len(owner) + len(dacl)
    raw = struct.pack("<BBHIIII", 1, 0, snapshot["control"], 20, 0, label_offset, dacl_offset) + owner + dacl + label
    parsed = binding.parse_sd(raw, package, user)
    if (len(dacl) != struct.unpack_from("<H", dacl, 2)[0] or snapshot["dacl_count"] != len(parsed["dacl"])
            or snapshot["acl_revision"] != dacl[0] or snapshot["il"] != 4096 or snapshot["label_mask"] != 1
            or snapshot["label_flags"] != parsed["label"]["flags"] or snapshot["label_count"] != 1):
        raise ValueError("registry_sd_summary_disagrees")
    package_aces = [entry for entry in parsed["dacl"] if entry["sid"] == package]
    ace = package_aces[0]
    if (snapshot["package_count"] != 1 or snapshot["package_flags"] != ace["flags"]
            or snapshot["package_mask"] != ace["mask"] or ace["type"] != 0 or ace["mask"] != 0xF003F
            or (ace["flags"] != 0x23 if root else not ace["flags"] & 0x10 or bool(ace["flags"] & 8))):
        raise ValueError("registry_package_ace_gate")
    if any(not any(entry["sid"] == sid and entry["type"] == 0 and entry["mask"] == 0xF003F and not entry["flags"] & 8
            for entry in parsed["dacl"]) for sid in (user, "S-1-5-18")):
        raise ValueError("registry_host_system_allow_gate")
    return _sd_identity(parsed)


def _sd_identity(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: snapshot[key] for key in ("owner_sid", "control", "dacl", "label")}


def _assess(result: WorkerResult, phase: Phase, context: dict[str, Any], report: dict[str, Any],
            nonce: str, canary: str) -> dict[str, Any]:
    if len(result.frames) != 1:
        raise ValueError("registry_one_frame_required")
    frame = result.frames[0]
    if (set(frame) != FRAME_KEYS or frame["probe"] != "registry_integrity_v5"
            or frame["mode"] != ("seed-low" if phase == "low" else "deny-untrusted")
            or frame["nonce"] != nonce or frame["canary_hex"] != canary):
        raise ValueError("registry_frame_schema_or_arguments")
    for key in ("boundary_failed", "positive_observed", "resistance_observed", "unknown", "cleanup_ok", "release_failed", "thread_close_attempted"):
        if not _integer(frame[key], 0, 1):
            raise ValueError("registry_c_flag_requires_integer")
    for key in ("problem", "refused_handles", "call_budget", "call_count_before_ipc", "maximum_total_calls", "ipc_write_calls_planned", "node_bound", "frame_budget"):
        if not _integer(frame[key]):
            raise ValueError("registry_frame_integer")
    if (frame["boundary_failed"] or frame["unknown"] or frame["problem"] or frame["release_failed"] or frame["refused_handles"]
            or frame["cleanup_ok"] != 1 or frame["positive_observed"] != int(phase == "low")
            or frame["resistance_observed"] != int(phase == "untrusted") or frame["thread_close_attempted"]
            or frame["thread_partial_hex"] != "0" * 16 or frame["unreleased_descriptor_hex"] != "0" * 16):
        raise ValueError("registry_fixture_incomplete_or_boundary_failed")
    snapshots = frame["token_snapshots"]
    expected = _expected_token(context)
    if not isinstance(snapshots, list) or len(snapshots) != 1 or not isinstance(snapshots[0], dict) or set(snapshots[0]) != SNAP_KEYS:
        raise ValueError("registry_token_snapshot_schema")
    for key in SNAP_KEYS - {"user", "package", "id"}:
        if not _integer(snapshots[0][key], 0, 1 if key in ("valid", "thread_absent", "il_known", "policy_known") else 0xFFFFFFFF):
            raise ValueError("registry_token_snapshot_integer")
    identity = snapshots[0]["id"]
    if (not isinstance(identity, list) or len(identity) != 2 or not _integer(identity[0])
            or not _integer(identity[1], -(1 << 31), (1 << 31) - 1) or snapshots[0] != expected
            or expected["il"] != (4096 if phase == "low" else 0) or expected["policy"] != 1):
        raise ValueError("registry_runtime_token_not_host_verified_token")
    security = frame["security_snapshots"]
    if not isinstance(security, list) or len(security) not in ((2, 3) if phase == "low" else (2,)):
        raise ValueError("registry_security_snapshot_count")
    normalized = [_sd(value, context["package_sid"], context["user"]["sid"], root=index == 0) for index, value in enumerate(security)]
    if len({json.dumps(value, sort_keys=True) for value in security}) != len(security):
        raise ValueError("registry_noncanonical_snapshot_deduplication")
    if len(normalized) == 3 and normalized[2] != normalized[1]:
        raise ValueError("registry_disposable_inheritance_changed")
    blocks = frame["call_blocks"]
    if (not isinstance(blocks, list) or len(blocks) != 2 or any(not isinstance(block, list) or len(block) > 40 for block in blocks)
            or len(blocks[0]) != 40):
        raise ValueError("registry_call_blocks")
    calls = [row for block in blocks for row in block]
    for row in calls:
        if (not isinstance(row, list) or len(row) != 6 or not all(_integer(value) for value in row[:3] + row[4:])
                or not _integer(row[3], -(1 << 63), (1 << 63) - 1) or row[2] != 2):
            raise ValueError("registry_raw_call_row")
    if (len(calls) != (72 if phase == "low" else 62) or frame["call_count_before_ipc"] != len(calls)
            or frame["maximum_total_calls"] != len(calls) + 2 or frame["call_budget"] != 80 or frame["ipc_write_calls_planned"] != 2
            or frame["frame_budget"] != 8192):
        raise ValueError("registry_exact_call_budget")
    gates, reads = frame["gates"], frame["canary_reads"]
    count = 4 if phase == "low" else 3
    if (not isinstance(gates, list) or len(gates) != count or any(not isinstance(row, list) or len(row) != 6
            or not all(_integer(value, -1, 80) for value in row) for row in gates)
            or not isinstance(reads, list) or len(reads) != (1 if phase == "low" else 3)
            or frame["canary_read_encoding"] != "call_index,type,returned_bytes,fixed8buffer_hex_not_complete_on_failure"):
        raise ValueError("registry_gate_or_canary_schema")
    cursor = 0
    read_index = 0

    def take(api: int, detail: int, raw: int | None = 0, length: int | None = 0) -> None:
        nonlocal cursor
        if cursor >= len(calls):
            raise ValueError("registry_trace_short")
        row = calls[cursor]
        if (row[:3] != [api, detail, 2] or (row[3] == 0 or not _integer(row[3], -(1 << 31), (1 << 31) - 1) if raw is None else row[3] != raw)
                or length is not None and row[5] != length):
            raise ValueError("registry_trace_order_or_raw_result")
        cursor += 1

    def observed_sd(index: int, root: bool) -> None:
        take(4, 1 if root else 2, length=1)
        take(5, 0, security[index]["sd_bytes"], security[index]["sd_bytes"])
        take(6, 0)

    def start_gate(step: int, child: int = -1) -> None:
        row = gates[step - 1]
        if row[:2] != [step, cursor] or row[3:] != [0, 0, child]:
            raise ValueError("registry_gate_snapshot_or_actual_call_range")
        take(2, step, 0, 0)
        if calls[cursor - 1][4] != 1008:
            raise ValueError("registry_effective_thread_not_absent")
        for kind in QUERY_ORDER:
            take(1, kind, None, None)
            size = calls[cursor - 1][5]
            minimum = {8: 4, 1: 16 + len(_sid_bytes(expected["user"])), 31: 8 + len(_sid_bytes(expected["package"])),
                10: 56, 29: 4, 30: 8, 25: 28, 27: 4}[kind]
            if not minimum <= size <= 65536 or kind in (8, 10, 29, 27) and size != minimum:
                raise ValueError("registry_query_returned_length")
        if step == 1:
            take(3, 0x20000, length=1)
        observed_sd(0, True)
        if child >= 0:
            observed_sd(child, False)

    def end_gate(step: int) -> None:
        if gates[step - 1][2] != cursor:
            raise ValueError("registry_gate_end_call_index")

    def read_canary() -> None:
        nonlocal read_index
        index = cursor
        take(9, 3, length=8)
        row = reads[read_index]
        if (not isinstance(row, list) or len(row) != 4 or not all(_integer(value) for value in row[:3])
                or row != [index, 3, 8, canary]):
            raise ValueError("registry_raw_canary_receipt")
        read_index += 1

    if phase == "low":
        disposable = len(security) - 1
        start_gate(1); end_gate(1)
        take(7, 2, length=1); observed_sd(1, False)
        start_gate(2, 1); end_gate(2)
        take(8, 3, length=8); read_canary(); take(12, 2, length=1)
        start_gate(3); end_gate(3)
        take(7, 3, length=1); observed_sd(disposable, False)
        start_gate(4, disposable); end_gate(4)
        take(12, 3, length=1); take(11, 3)
        owners = [[1, 1, "0" * 16, 1], [2, 1, "0" * 16, 1], [3, 1, "0" * 16, 1], [4, 0, "0" * 16, 0]]
    else:
        for step in range(1, 4):
            start_gate(step)
            take(10, 2, length=1); observed_sd(1, False); read_canary(); take(12, 2, length=step)
            if gates[step - 1][5] != 1:
                raise ValueError("registry_negative_seed_reference")
            end_gate(step)
            take(7 if step == 1 else 10 if step == 2 else 11, 4 if step < 3 else 2, 5, 0)
        owners = [[1, 1, "0" * 16, 1], [2, 3, "0" * 16, 1], [3, 0, "0" * 16, 0], [4, 2, "0" * 16, 0]]
    observed_sd(0, True); take(12, 1, length=1)
    if (cursor != len(calls) or frame["key_owners"] != owners
            or any(not isinstance(row, list) or not _integer(row[0]) or not _integer(row[1]) or not _integer(row[3], 0, 1)
                   for row in frame["key_owners"])):
        raise ValueError("registry_owned_handle_cleanup_or_extra_call")
    bound = 128 + 36 + len(security) * 46 + count * 8 + len(reads) * 6 + len(calls) * 7
    if frame["node_bound"] != bound or bound > 1024:
        raise ValueError("registry_node_bound")
    if (result.exit_code != 0 or result.stdout or result.stderr or report.get("security_boundary_failed")
            or report.get("host_runtime_matches_prelaunch") is not True):
        raise ValueError("registry_exit_logs_or_independent_readback")
    report.update(fixture_body_confirmed=True, worker_code_executed=True)
    return {"root": normalized[0], "seed": normalized[1]}
