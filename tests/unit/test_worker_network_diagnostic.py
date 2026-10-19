"""Pure mocks only: no native DLL binding, socket, profile, ACL or subprocess."""

from __future__ import annotations

import ast
from copy import deepcopy
import ctypes as C
from dataclasses import dataclass
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import diagnose_worker_network as diagnostic
from diagnose_worker_limits import ReportWriter
from deskpal.execution.runtime import ProbeProfile, RuntimeManifest
from deskpal.execution.windows.launcher import LaunchRejected, LaunchSpec


def _forbid(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("Native/network/process/profile access forbidden in pure-mock tests")


@pytest.fixture(autouse=True)
def _native_tripwires(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(C, "WinDLL", _forbid)
    monkeypatch.setattr(socket, "socket", _forbid)
    monkeypatch.setattr(subprocess, "Popen", _forbid)
    monkeypatch.setattr(diagnostic, "native_context", _forbid)
    monkeypatch.setattr(diagnostic, "set_caller_localappdata", _forbid)
    monkeypatch.setattr(diagnostic, "stage_runtime", _forbid)
    monkeypatch.setattr(diagnostic, "create_probe_profile", _forbid)
    monkeypatch.setattr(diagnostic, "delete_probe_profile", _forbid)
    monkeypatch.setattr(diagnostic, "apply_readonly_dacl", _forbid)
    monkeypatch.setattr(diagnostic, "_owner_sid", _forbid)
    monkeypatch.setattr(diagnostic, "ObserverApi", _forbid)
    monkeypatch.setattr(diagnostic, "_RETAINED", [])


@pytest.fixture
def worker() -> dict[str, Any]:
    tree = ast.parse(diagnostic.WORKER_SOURCE)
    prefix: list[ast.stmt] = []
    for statement in tree.body:
        if (isinstance(statement, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "payload" for target in statement.targets)):
            break
        prefix.append(statement)
    namespace: dict[str, Any] = {}
    # Definitions and ctypes structures only; no Winsock instance or output path.
    exec(compile(ast.Module(body=prefix, type_ignores=[]), "<network-worker-definitions>", "exec"), namespace)
    return namespace


class FakeWinsock:
    def __init__(self, namespace: dict[str, Any], *, connect_error: int = 0,
                 selected: int = 1, completion_error: int = 0, send_error: int = 0) -> None:
        self.ns = namespace
        self.connect_error, self.selected = connect_error, selected
        self.completion_error, self.send_error = completion_error, send_error
        self.last_error = 0
        self.handle = 2**42 + 31
        self.events: list[str] = []
        self.sent: list[bytes] = []
        self.addresses: list[tuple[int, tuple[int, ...]]] = []
        self.startup_return = 0
        self.cleanup_calls = 0

    def socket(self, family: int, kind: int, protocol: int) -> int:
        assert family == 2 and (kind, protocol) in ((1, 6), (2, 17))
        self.events.append("socket")
        return self.handle

    def error(self) -> int:
        return self.last_error

    def ioctl(self, handle: int, command: Any, value: Any) -> int:
        assert handle == self.handle and command.value & 0xffffffff == 0x8004667e
        assert C.cast(value, C.POINTER(C.c_uint32)).contents.value == 1
        self.events.append("nonblocking")
        return 0

    def _address(self, address: Any) -> None:
        value = C.cast(address, C.POINTER(self.ns["Address"])).contents
        self.addresses.append((value.port, tuple(value.address)))
        assert value.family == 2 and tuple(value.address) == (127, 0, 0, 1)

    def connect(self, handle: int, address: Any, size: int) -> int:
        assert handle == self.handle and size == 16 and "nonblocking" in self.events
        self._address(address)
        self.events.append("connect")
        self.last_error = self.connect_error
        return -1 if self.connect_error else 0

    def select(self, ignored: int, readable: Any, writable: Any, exceptional: Any, timeout: Any) -> int:
        limit = C.cast(timeout, C.POINTER(self.ns["Timeval"])).contents
        assert ignored == 0 and readable is None and (limit.seconds, limit.microseconds) == (1, 0)
        writes = C.cast(writable, C.POINTER(self.ns["FDSet"])).contents
        exceptions = C.cast(exceptional, C.POINTER(self.ns["FDSet"])).contents
        assert writes.sockets[0] == exceptions.sockets[0] == self.handle
        writes.count = 0 if self.completion_error else 1
        exceptions.count = 1 if self.completion_error else 0
        self.events.append("select")
        return self.selected

    def option(self, handle: int, level: int, option: int, value: Any, size: Any) -> int:
        assert (handle, level, option) == (self.handle, 0xffff, 0x1007)
        assert C.cast(size, C.POINTER(C.c_int32)).contents.value == 4
        C.cast(value, C.POINTER(C.c_int32)).contents.value = self.completion_error
        self.events.append("so_error")
        return 0

    def send(self, handle: int, data: Any, size: int, flags: int) -> int:
        assert handle == self.handle and size == 8 and flags == 0
        self.sent.append(C.string_at(data, size))
        self.last_error = self.send_error
        return -1 if self.send_error else 8

    def sendto(self, handle: int, data: Any, size: int, flags: int, address: Any, length: int) -> int:
        assert length == 16
        self._address(address)
        return self.send(handle, data, size, flags)

    def bind(self, handle: int, address: Any, size: int) -> int:
        assert handle == self.handle and size == 16
        self._address(address)
        assert self.addresses[-1][0] == 0
        self.events.append("bind")
        return 0

    def name(self, handle: int, address: Any, size: Any) -> int:
        assert handle == self.handle and C.cast(size, C.POINTER(C.c_int32)).contents.value == 16
        C.cast(address, C.POINTER(self.ns["Address"]))[0] = self.ns["address"](43210)
        return 0

    def listen(self, handle: int, backlog: int) -> int:
        assert handle == self.handle and backlog == 1
        self.events.append("listen")
        return 0

    def close(self, handle: int) -> int:
        assert handle == self.handle
        self.events.append("close")
        return 0

    def startup(self, version: int, data: Any) -> int:
        assert version == 0x202
        C.cast(data, C.POINTER(self.ns["WSAData"])).contents.version = 0x202
        return self.startup_return

    def cleanup(self) -> int:
        self.cleanup_calls += 1
        return 0


def test_worker_x64_abi_layout_is_exact(worker: dict[str, Any]) -> None:
    assert C.sizeof(worker["S"]) == 8
    assert C.sizeof(worker["FDSet"]) == 520
    assert worker["FDSet"].sockets.offset == 8
    assert C.sizeof(worker["WSAData"]) == 408
    assert worker["WSAData"].vendor.offset == 8
    assert C.sizeof(worker["Address"]) == 16
    assert C.sizeof(worker["Timeval"]) == 8


def test_worker_bindings_do_not_truncate_socket(worker: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    class Function:
        argtypes: object = None
        restype: object = None

    functions: dict[str, Function] = {}

    class Library:
        def __getattr__(self, name: str) -> Function:
            functions[name] = Function()
            return functions[name]

    monkeypatch.setattr(C, "WinDLL", lambda *args, **kwargs: Library())
    worker["Winsock"]()
    assert functions["socket"].restype is C.c_size_t
    assert cast(list[object], functions["connect"].argtypes)[0] is C.c_size_t
    assert cast(list[object], functions["closesocket"].argtypes) == [C.c_size_t]


def test_socket_creation_denial_has_no_fabricated_close(worker: dict[str, Any]) -> None:
    api = FakeWinsock(worker)
    api.handle, api.last_error = worker["INVALID"], 10013
    outcome = worker["operation"](api, "tcp_connect", 43210)
    assert outcome == {"status": "access_denied", "stage": "socket", "wsa_error": 10013,
                       "socket_created": False, "socket_closed": "not_created"}
    assert api.events == ["socket"]
    body = _body()
    body["operations"]["tcp_connect"] = outcome
    assert diagnostic.parse_worker(body) is body
    assert diagnostic.assess_loopback(body, {})["tcp_connect"].startswith("socket_creation_access_refusal")


@pytest.mark.parametrize("pending", [False, True])
def test_tcp_connect_sends_only_fixed_eight_bytes_after_completion(worker: dict[str, Any], pending: bool) -> None:
    api = FakeWinsock(worker, connect_error=10035 if pending else 0)
    outcome = worker["operation"](api, "tcp_connect", 43210)
    assert outcome["status"] == "success" and outcome["socket_closed"] is True
    assert api.sent == [b"SYNTH06!"]
    assert api.events[:2] == ["socket", "nonblocking"]
    assert ("select" in api.events) is pending
    assert ("so_error" in api.events) is pending
    assert api.events.count("close") == 1


@pytest.mark.parametrize(("error", "status"), [(10013, "access_denied"), (10061, "connection_refused"),
                                              (10060, "timeout"), (12345, "unknown_error")])
def test_tcp_direct_failures_are_distinct(worker: dict[str, Any], error: int, status: str) -> None:
    api = FakeWinsock(worker, connect_error=error)
    outcome = worker["operation"](api, "tcp_connect", 43210)
    assert outcome["status"] == status and outcome["stage"] == "connect"
    assert not api.sent and "select" not in api.events and api.events[-1] == "close"


def test_tcp_pending_timeout_does_not_send_or_claim_denial(worker: dict[str, Any]) -> None:
    api = FakeWinsock(worker, connect_error=10035, selected=0)
    outcome = worker["operation"](api, "tcp_connect", 43210)
    assert outcome["status"] == "timeout" and outcome["wsa_error"] is None
    assert not api.sent and "so_error" not in api.events and api.events[-1] == "close"


@pytest.mark.parametrize("error", [10013, 10061])
def test_tcp_exceptfds_requires_so_error(worker: dict[str, Any], error: int) -> None:
    api = FakeWinsock(worker, connect_error=10035, completion_error=error)
    outcome = worker["operation"](api, "tcp_connect", 43210)
    assert outcome["stage"] == "connect_completion" and outcome["wsa_error"] == error
    assert "so_error" in api.events and not api.sent


def test_udp_is_nonblocking_and_sends_exact_canary(worker: dict[str, Any]) -> None:
    api = FakeWinsock(worker)
    outcome = worker["operation"](api, "udp_send", 43210)
    assert outcome["status"] == "success" and api.sent == [b"SYNTH06!"]
    assert api.events[:2] == ["socket", "nonblocking"] and "select" not in api.events


@pytest.mark.parametrize("kind", ["tcp_bind_listen", "udp_bind"])
def test_bind_only_uses_loopback_port_zero(worker: dict[str, Any], kind: str) -> None:
    api = FakeWinsock(worker)
    outcome = worker["operation"](api, kind, 54321)
    assert api.addresses == [(0, (127, 0, 0, 1))]
    assert outcome["bound_port"] == 43210 and outcome["socket_closed"] is True
    assert ("listen" in api.events) is (kind == "tcp_bind_listen")


def test_wsa_startup_failure_is_not_last_error_or_cleanup(worker: dict[str, Any]) -> None:
    api = FakeWinsock(worker)
    api.startup_return = 10093
    worker["Winsock"] = lambda: api
    result = worker["run"]({"tcp_port": 40001, "udp_port": 40002})
    assert result["wsa_startup_return"] == 10093 and result["operations"] == {}
    assert api.cleanup_calls == 0 and not api.events


def test_successful_wsa_session_closes_all_sockets_and_cleans_up(worker: dict[str, Any]) -> None:
    api = FakeWinsock(worker)
    worker["Winsock"] = lambda: api
    result = worker["run"]({"tcp_port": 40001, "udp_port": 40002})
    assert set(result["operations"]) == set(diagnostic.OPS)
    assert api.events.count("close") == 4 and api.cleanup_calls == 1
    assert result["wsa_cleanup_return"] == 0


def test_wrong_abi_is_rejected_before_native_binding(worker: dict[str, Any]) -> None:
    worker["S"], worker["Winsock"] = C.c_uint32, _forbid
    with pytest.raises(RuntimeError, match="unsupported_socket_layout"):
        worker["run"]({"tcp_port": 40001, "udp_port": 40002})


@pytest.mark.parametrize("case", [{"tcp_port": 0, "udp_port": 1}, {"tcp_port": True, "udp_port": 1},
                                  {"tcp_port": 1, "udp_port": 2, "address": "8.8.8.8"}])
def test_worker_rejects_freeform_targets_before_wsa(worker: dict[str, Any], case: dict[str, object]) -> None:
    worker["Winsock"] = _forbid
    with pytest.raises(ValueError, match="invalid_internal_host_ports"):
        worker["run"](case)


def _body() -> dict[str, Any]:
    body: dict[str, Any] = {"kind": "loopback_network_probe", "scope_only_loopback": True, "wsa_startup_return": 0,
            "wsa_version": 0x202, "wsa_cleanup_return": 0,
            "operations": {name: {"status": "success", "stage": "send", "wsa_error": 0,
                                   "socket_created": True, "socket_closed": True} for name in diagnostic.OPS},
            "environment_keys": ["LOCALAPPDATA", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"],
            "isolated": 1, "no_site": 1, "site_loaded": False, "dont_write_bytecode": True}
    body["operations"]["tcp_connect"].update(bytes_sent=8, connect_completed=True)
    body["operations"]["udp_send"].update(bytes_sent=8)
    body["operations"]["tcp_bind_listen"].update(bind_completed=True, listen_completed=True)
    body["operations"]["udp_bind"].update(bind_completed=True)
    return body


@pytest.mark.parametrize("error", [10035, 10061, 10060, 9876])
def test_missing_arrival_and_other_errors_never_prove_denial(error: int) -> None:
    body = _body()
    body["operations"]["tcp_connect"].update(status="unknown_error", stage="connect", wsa_error=error)
    findings = diagnostic.assess_loopback(body, {"tcp_canary_received": False, "udp_canary_received": False})
    assert findings["tcp_connect"] == "unresolved_not_access_denial_proof"
    assert "reachability_unverified" in findings["tcp_bind_listen"]


def test_socket_creation_and_target_api_refusals_are_distinct() -> None:
    body = _body()
    item = body["operations"]["tcp_connect"]
    item.update(status="access_denied", stage="socket", wsa_error=10013)
    assert diagnostic.assess_loopback(body, {})["tcp_connect"].startswith("socket_creation_access_refusal")
    item["stage"] = "connect"
    assert diagnostic.assess_loopback(body, {})["tcp_connect"].startswith("api_access_refusal_observed")


@pytest.mark.parametrize("field", ["tcp_canary_received", "udp_canary_received", "drain_complete"])
def test_positive_requires_both_receipts_and_completed_drain(field: str) -> None:
    received: dict[str, object] = {"tcp_canary_received": True, "udp_canary_received": True, "drain_complete": True}
    received[field] = False
    assert diagnostic.positive_complete(_body(), received) is False


@pytest.mark.parametrize(("operation", "field"), [("tcp_connect", "bytes_sent"),
    ("tcp_connect", "connect_completed"), ("udp_send", "bytes_sent"),
    ("tcp_bind_listen", "bind_completed"), ("tcp_bind_listen", "listen_completed"), ("udp_bind", "bind_completed")])
def test_status_success_alone_does_not_complete_positive(operation: str, field: str) -> None:
    body = _body()
    del body["operations"][operation][field]
    assert diagnostic.positive_complete(body, {"tcp_canary_received": True, "udp_canary_received": True,
                                             "drain_complete": True}) is False


class FakeWriter:
    def __init__(self, path: Path, *, fail_profile_save: bool = False) -> None:
        self.path, self.fail_profile_save = path, fail_profile_save
        self.saved: list[dict[str, object]] = []
        self.closed = False

    def save(self, report: dict[str, object]) -> None:
        if self.fail_profile_save and "profile" in report:
            self.fail_profile_save = False
            raise OSError("synthetic first post-create report failure")
        self.saved.append(deepcopy(report))

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self, body: dict[str, Any], *, timeout: bool = False) -> None:
        self.body, self.timeout = body, timeout
        self.returncode: int | None = 0 if not timeout else None
        self.stdout = self.stderr = None
        self.killed = False

    def communicate(self, timeout: float) -> tuple[bytes, bytes]:
        import json
        assert timeout == 8
        if self.timeout:
            raise subprocess.TimeoutExpired("synthetic-worker", timeout)
        return json.dumps(self.body).encode("ascii"), b""

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -1

    def wait(self, timeout: float) -> int:
        assert timeout == 2
        assert self.returncode is not None
        return self.returncode


class FakeEndpoints:
    def __init__(self, *, positive: bool = True) -> None:
        self.positive, self.drains, self.closed = positive, 0, False

    def start(self) -> dict[str, int]:
        return {"tcp_port": 40001, "udp_port": 40002}

    def drain(self) -> dict[str, object]:
        self.drains += 1
        arrived = self.positive and self.drains == 1
        return {"tcp_canary_received": arrived, "udp_canary_received": arrived,
                "tcp_connections": 1 if arrived else 0, "udp_datagrams": 1 if arrived else 0,
                "drain_complete": True}

    def close(self) -> None:
        self.closed = True


@dataclass
class Harness:
    root: Path
    writer: FakeWriter
    report: dict[str, object]
    deleted: list[ProbeProfile]
    restricted_calls: list[LaunchSpec]
    process: FakeProcess
    endpoints: FakeEndpoints


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, positive: bool = True,
             timeout: bool = False, safe: bool = True, interrupt: bool = False,
             fail_profile_save: bool = False) -> Harness:
    root = tmp_path / "new-network-root"
    root.mkdir()
    folder = tmp_path / "synthetic-profile"
    folder.mkdir()
    profile = ProbeProfile("deskpal.v06.synthetic00", "S-1-15-2-1-2-3-4-5-6-7", str(folder))
    manifest = RuntimeManifest(str(root / "runtime"), str(root / "runtime/python.exe"), "312", "source", ())
    writer = FakeWriter(root / "network-report.json", fail_profile_save=fail_profile_save)
    report: dict[str, object] = {"stage0_passed": False, "scope_only_loopback": True}
    deleted: list[ProbeProfile] = []
    calls: list[LaunchSpec] = []
    process, endpoints = FakeProcess(_body(), timeout=timeout), FakeEndpoints(positive=positive)
    monkeypatch.setattr(diagnostic, "native_context", lambda: ("C:/Windows", "C:/synthetic-local", False))
    monkeypatch.setattr(diagnostic, "set_caller_localappdata", lambda value: None)
    monkeypatch.setattr(diagnostic, "stage_runtime", lambda *args, **kwargs: manifest)
    monkeypatch.setattr(diagnostic, "verify_manifest", lambda value: None)
    monkeypatch.setattr(diagnostic, "create_probe_profile", lambda name: profile)
    monkeypatch.setattr(diagnostic, "delete_probe_profile", deleted.append)
    monkeypatch.setattr(diagnostic, "apply_readonly_dacl", lambda *args, **kwargs: None)
    monkeypatch.setattr(diagnostic, "_owner_sid", lambda windows: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(diagnostic, "HostEndpoints", lambda: endpoints)
    monkeypatch.setattr(diagnostic, "ObserverApi", lambda: object())
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    def restricted(spec: LaunchSpec, api: object) -> tuple[dict[str, object], bool]:
        calls.append(spec)
        if interrupt:
            raise KeyboardInterrupt("synthetic nonreturning worker call")
        return {"application_success": True, "report": _body()}, safe

    monkeypatch.setattr(diagnostic, "run_restricted", restricted)
    return Harness(root, writer, report, deleted, calls, process, endpoints)


def _conduct(harness: Harness) -> int:
    return diagnostic.conduct(cast(ReportWriter, harness.writer), harness.report,
                              harness.root / "source/python.exe", harness.root)


def test_first_profile_report_failure_still_deletes_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    harness = _prepare(monkeypatch, tmp_path, fail_profile_save=True)
    with pytest.raises(OSError, match="post-create"):
        _conduct(harness)
    assert len(harness.deleted) == 1 and not harness.restricted_calls
    assert harness.report["cleanup"] == "profile_deleted"


def test_positive_missing_canary_prevents_restricted_execution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    harness = _prepare(monkeypatch, tmp_path, positive=False)
    assert _conduct(harness) == 2
    assert not harness.restricted_calls and len(harness.deleted) == 1
    assert harness.report["status"] == "blocked_positive_control_incomplete"


def test_host_timeout_is_reaped_without_running_restricted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    harness = _prepare(monkeypatch, tmp_path, timeout=True)
    with pytest.raises(subprocess.TimeoutExpired):
        _conduct(harness)
    assert harness.process.killed and not harness.restricted_calls
    assert len(harness.deleted) == 1 and harness.endpoints.closed


def test_unknown_restricted_cleanup_withholds_profile_and_drain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    harness = _prepare(monkeypatch, tmp_path, safe=False)
    assert _conduct(harness) == 2
    assert not harness.deleted and harness.endpoints.drains == 2
    assert harness.report["cleanup"] == "withheld_unconfirmed_owner"


def test_nonreturning_restricted_call_withholds_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    harness = _prepare(monkeypatch, tmp_path, interrupt=True)
    with pytest.raises(KeyboardInterrupt):
        _conduct(harness)
    assert not harness.deleted and harness.report["cleanup"] == "withheld_unconfirmed_owner"


def test_full_mock_matrix_preserves_scope_and_canonical_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    harness = _prepare(monkeypatch, tmp_path)
    assert _conduct(harness) == 0
    assert len(harness.restricted_calls) == 1 and harness.endpoints.drains == 3
    spec = harness.restricted_calls[0]
    assert set(spec.environment) == {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA"}
    assert spec.low_privilege is False and spec.args[-1] == "{ipc_write_handle}"
    assert harness.report["stage0_passed"] is False and harness.report["scope_only_loopback"] is True
    assert harness.report["cleanup"] == "profile_deleted"


def test_unknown_host_launch_owner_is_retained(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    harness = _prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "Popen", _forbid)
    with pytest.raises(AssertionError):
        _conduct(harness)
    assert not harness.deleted and harness.report["host_cleanup"] == "unconfirmed"
    assert diagnostic._RETAINED


def test_host_wait_unconfirmed_withholds_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    harness = _prepare(monkeypatch, tmp_path)

    def wait(timeout: float) -> NoReturn:
        raise subprocess.TimeoutExpired("synthetic-reap", timeout)

    monkeypatch.setattr(harness.process, "wait", wait)
    with pytest.raises(subprocess.TimeoutExpired):
        _conduct(harness)
    assert not harness.deleted and not harness.restricted_calls
    assert harness.report["host_cleanup"] == "unconfirmed" and diagnostic._RETAINED


def test_reparse_root_is_rejected_before_creation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "linked-root"

    def reject(path: Path) -> NoReturn:
        raise ValueError("synthetic reparse")

    monkeypatch.setattr(diagnostic, "_no_reparse", reject)
    with pytest.raises(ValueError, match="reparse"):
        diagnostic._new_root(target)
    assert not target.exists()


def test_main_never_marks_network_or_stage0_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    writer = FakeWriter(tmp_path / "network-report.json")
    monkeypatch.setattr(diagnostic, "_new_root", lambda path: tmp_path)
    monkeypatch.setattr(diagnostic, "ReportWriter", lambda path: writer)
    monkeypatch.setattr(diagnostic, "source_fingerprints", lambda: {"synthetic": "stable"})
    monkeypatch.setattr(sys, "getwindowsversion", lambda: SimpleNamespace(build=12345))
    monkeypatch.setattr(sys, "argv", ["diagnose_worker_network.py", "--python", "python.exe", "--root", str(tmp_path)])

    def conduct(output: ReportWriter, report: dict[str, object], python: Path, root: Path) -> int:
        report.update(cleanup="profile_deleted", status="synthetic_collected")
        return 0

    monkeypatch.setattr(diagnostic, "conduct", conduct)
    assert diagnostic.main() == 0
    final = writer.saved[-1]
    assert final["stage0_passed"] is False and final["network_isolation_accepted"] is False
    assert final["scope_only_loopback"] is True and final["fallback_allowed"] is False
    assert "LAN" in cast(list[str], final["unverified"]) and writer.closed


def test_collect_cleanup_owner_is_registered_without_real_launcher(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    extra = SimpleNamespace(close=lambda: None)
    owners: list[object] = []

    class Cleanup:
        launch_resolved = False
        worker_returned = False

        def __init__(self, api: object) -> None:
            self.observers = SimpleNamespace(attach=lambda worker: None)

        def add(self, owner: object) -> None:
            owners.append(owner)

    class Worker:
        identity = SimpleNamespace()

        def collect(self, timeout: float, *, log_limit_bytes: int) -> NoReturn:
            raise LaunchRejected("synthetic_collect_failure", cleanup_owner=extra)

    fake_worker = Worker()
    monkeypatch.setattr(diagnostic, "CaseCleanup", Cleanup)
    monkeypatch.setattr(diagnostic, "asdict", lambda identity: {"pid": 1})
    monkeypatch.setattr(diagnostic, "WindowsLauncher", lambda: SimpleNamespace(launch=lambda spec: fake_worker))
    monkeypatch.setattr(diagnostic, "_cleanup_case", lambda cleanup, entry: False)
    spec = LaunchSpec(tmp_path / "python.exe", (), tmp_path, "S-1-15-2-1-2-3-4-5-6-7", {"SYSTEMROOT": "C:/Windows", "WINDIR": "C:/Windows"})
    entry, safe = diagnostic.run_restricted(spec, cast(Any, object()))
    assert owners == [fake_worker, extra] and safe is False
    assert entry["rejected"] == "synthetic_collect_failure" and diagnostic._RETAINED
