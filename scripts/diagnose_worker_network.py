"""Fixed loopback-only Winsock observations; never stage-zero acceptance.

Only --python (a staging source) and a NEW --root are accepted. No address,
port, capability, environment or policy overrides exist. Run actual experiments
only through the separately approved native/network/profile experiment command.
No firewall or loopback-exemption changes, LAN traffic, DNS or Internet access.

Bindings checked against official contracts (x64 layouts asserted in worker):
https://learn.microsoft.com/en-us/windows/win32/api/winsock/ns-winsock-fd_set
https://learn.microsoft.com/en-us/windows/win32/api/winsock2/ns-winsock2-wsadata
https://learn.microsoft.com/en-us/windows/win32/api/winsock2/nf-winsock2-select
https://learn.microsoft.com/en-us/windows/win32/api/winsock2/nf-winsock2-connect
https://learn.microsoft.com/en-us/windows/win32/api/winsock2/nf-winsock2-ioctlsocket
https://learn.microsoft.com/en-us/windows/win32/api/winsock2/nf-winsock2-wsastartup
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
import select
import socket
import subprocess
import sys
from time import monotonic
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deskpal.execution.protocol import ProtocolViolation  # noqa: E402
from deskpal.execution.runtime import (  # noqa: E402
    apply_readonly_dacl, create_probe_profile, delete_probe_profile, stage_runtime,
    verify_manifest, worker_environment,
)
from deskpal.execution.windows import LaunchRejected, LaunchSpec, WindowsLauncher  # noqa: E402
from diagnose_worker_limits import (  # noqa: E402
    CaseCleanup, ObserverApi, ReportWriter, _cleanup_case, _no_reparse,
)
from diagnose_worker_startup import native_context, set_caller_localappdata  # noqa: E402
from probe_worker_filesystem import _owner_sid  # noqa: E402

CANARY = b"SYNTH06!"
OPS = ("tcp_connect", "udp_send", "tcp_bind_listen", "udp_bind")
MAX_OUTPUT = 16 * 1024
_RETAINED: list[object] = []

# This source imports no socket module or third-party dependencies. ws2_32 is a
# system DLL; stage_runtime already includes _ctypes and its required libffi.
WORKER_SOURCE = r'''
import ctypes as C
import json
import os
import struct
import sys

S = C.c_size_t
I = C.c_int32
U = C.c_uint32
P = C.c_void_p
INVALID = S(-1).value
CANARY = b"SYNTH06!"

class Address(C.Structure):
    _fields_ = [("family", C.c_uint16), ("port", C.c_uint16),
                ("address", C.c_ubyte * 4), ("zero", C.c_ubyte * 8)]
class FDSet(C.Structure):
    _fields_ = [("count", U), ("sockets", S * 64)]
class Timeval(C.Structure):
    _fields_ = [("seconds", I), ("microseconds", I)]
class WSAData(C.Structure):
    _fields_ = [("version", C.c_uint16), ("high", C.c_uint16),
                ("max_sockets", C.c_uint16), ("max_datagram", C.c_uint16),
                ("vendor", P), ("description", C.c_char * 257),
                ("status", C.c_char * 129)]

def bind_api(dll, name, args, result=I):
    function = getattr(dll, name)
    function.argtypes, function.restype = args, result
    return function

def classify(error):
    return {10013: "access_denied", 10035: "would_block", 10061: "connection_refused",
            10060: "timeout"}.get(error, "unknown_error")

class Winsock:
    def __init__(self):
        w = C.WinDLL("ws2_32", use_last_error=True)
        self.startup = bind_api(w, "WSAStartup", [C.c_uint16, C.POINTER(WSAData)])
        self.cleanup = bind_api(w, "WSACleanup", [])
        self.error = bind_api(w, "WSAGetLastError", [])
        self.socket = bind_api(w, "socket", [I, I, I], S)
        self.close = bind_api(w, "closesocket", [S])
        self.ioctl = bind_api(w, "ioctlsocket", [S, I, C.POINTER(U)])
        self.connect = bind_api(w, "connect", [S, C.POINTER(Address), I])
        self.send = bind_api(w, "send", [S, P, I, I])
        self.sendto = bind_api(w, "sendto", [S, P, I, I, C.POINTER(Address), I])
        self.bind = bind_api(w, "bind", [S, C.POINTER(Address), I])
        self.listen = bind_api(w, "listen", [S, I])
        self.name = bind_api(w, "getsockname", [S, C.POINTER(Address), C.POINTER(I)])
        self.select = bind_api(w, "select", [I, P, C.POINTER(FDSet), C.POINTER(FDSet), C.POINTER(Timeval)])
        self.option = bind_api(w, "getsockopt", [S, I, I, P, C.POINTER(I)])

def address(port):
    value = Address()
    value.family = 2
    value.port = int.from_bytes(port.to_bytes(2, "big"), "little")
    value.address[:] = (127, 0, 0, 1)
    return value

def failure(stage, error):
    return {"status": classify(error), "stage": stage, "wsa_error": error}

def operation(api, kind, port=0):
    datagram = kind in ("udp_send", "udp_bind")
    handle = api.socket(2, 2 if datagram else 1, 17 if datagram else 6)
    if handle == INVALID:
        return failure("socket", api.error()) | {"socket_created": False, "socket_closed": "not_created"}
    outcome = {"status": "unknown_error", "stage": "not_started", "wsa_error": None}
    try:
        enabled = U(1)
        if api.ioctl(handle, I(-2147195266), C.byref(enabled)) == -1:  # FIONBIO 0x8004667e
            outcome = failure("ioctlsocket", api.error())
        else:
            target = address(port if kind in ("tcp_connect", "udp_send") else 0)
            if kind == "tcp_connect":
                returned = api.connect(handle, C.byref(target), C.sizeof(target))
                error = api.error() if returned == -1 else 0
                pending = error == 10035
                if pending:
                    writable, exceptional = FDSet(), FDSet()
                    writable.count = exceptional.count = 1
                    writable.sockets[0] = exceptional.sockets[0] = handle
                    timeout = Timeval(1, 0)
                    selected = api.select(0, None, C.byref(writable), C.byref(exceptional), C.byref(timeout))
                    if selected == -1:
                        outcome = failure("select", api.error())
                    elif selected == 0:
                        outcome = {"status": "timeout", "stage": "select", "wsa_error": None}
                    else:
                        socket_error, length = I(), I(C.sizeof(I))
                        if api.option(handle, 0xffff, 0x1007, C.byref(socket_error), C.byref(length)) == -1:
                            outcome = failure("getsockopt", api.error())
                        elif length.value != C.sizeof(I):
                            outcome = {"status": "unknown_error", "stage": "so_error_size", "wsa_error": None}
                        elif socket_error.value:
                            outcome = failure("connect_completion", socket_error.value)
                        elif writable.count == 1 and writable.sockets[0] == handle and exceptional.count == 0:
                            error = 0
                            pending = False
                        else:
                            outcome = {"status": "unknown_error", "stage": "select_sets", "wsa_error": None}
                elif error:
                    outcome = failure("connect", error)
                if not error and not pending:
                    data = C.create_string_buffer(CANARY)
                    count = api.send(handle, data, 8, 0)
                    outcome = failure("send", api.error()) if count == -1 else {
                        "status": "success" if count == 8 else "partial_send", "stage": "send",
                        "wsa_error": 0, "bytes_sent": count, "connect_completed": True}
            elif kind == "udp_send":
                data = C.create_string_buffer(CANARY)
                count = api.sendto(handle, data, 8, 0, C.byref(target), C.sizeof(target))
                outcome = failure("sendto", api.error()) if count == -1 else {
                    "status": "success" if count == 8 else "partial_send", "stage": "sendto",
                    "wsa_error": 0, "bytes_sent": count}
            else:
                if api.bind(handle, C.byref(target), C.sizeof(target)) == -1:
                    outcome = failure("bind", api.error())
                else:
                    outcome = {"status": "success", "stage": "bind", "wsa_error": 0, "bind_completed": True}
                    local, length = Address(), I(C.sizeof(Address))
                    if api.name(handle, C.byref(local), C.byref(length)) == -1:
                        outcome = failure("getsockname", api.error())
                    elif length.value != 16 or tuple(local.address) != (127, 0, 0, 1) or not local.port:
                        outcome = {"status": "unknown_error", "stage": "bound_address", "wsa_error": None}
                    else:
                        outcome["bound_port"] = int.from_bytes(int(local.port).to_bytes(2, "little"), "big")
                        if kind == "tcp_bind_listen":
                            if api.listen(handle, 1) == -1:
                                outcome = failure("listen", api.error()) | {"bind_completed": True}
                            else:
                                outcome.update(stage="listen", listen_completed=True)
    finally:
        outcome["socket_created"] = True
        outcome["socket_closed"] = api.close(handle) == 0
        if not outcome["socket_closed"]:
            outcome["socket_close_error"] = api.error()
    return outcome

def run(case):
    if C.sizeof(S) != 8 or C.sizeof(Address) != 16 or C.sizeof(FDSet) != 520 or FDSet.sockets.offset != 8:
        raise RuntimeError("unsupported_socket_layout")
    if C.sizeof(Timeval) != 8 or C.sizeof(WSAData) != 408 or WSAData.vendor.offset != 8:
        raise RuntimeError("unsupported_winsock_layout")
    if set(case) != {"tcp_port", "udp_port"} or any(type(p) is not int or not 1 <= p <= 65535 for p in case.values()):
        raise ValueError("invalid_internal_host_ports")
    api = Winsock()
    data = WSAData()
    started = api.startup(0x202, C.byref(data))
    result = {"kind": "loopback_network_probe", "scope_only_loopback": True,
              "wsa_startup_return": started, "wsa_version": data.version, "operations": {},
              "environment_keys": sorted(os.environ), "isolated": sys.flags.isolated,
              "no_site": sys.flags.no_site, "site_loaded": "site" in sys.modules,
              "dont_write_bytecode": sys.dont_write_bytecode}
    if started:
        return result
    try:
        if data.version != 0x202:
            raise RuntimeError("unsupported_winsock_version")
        for kind in ("tcp_connect", "udp_send", "tcp_bind_listen", "udp_bind"):
            result["operations"][kind] = operation(api, kind, case.get("tcp_port" if kind == "tcp_connect" else "udp_port", 0))
    finally:
        result["wsa_cleanup_return"] = api.cleanup()
    return result

payload = json.dumps(run(json.loads(sys.argv[1])), ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
if len(payload) > 16384:
    raise RuntimeError("worker_report_budget")
if len(sys.argv) == 3:
    import msvcrt
    fd = msvcrt.open_osfhandle(int(sys.argv[2]), os.O_WRONLY | os.O_BINARY)
    try:
        output = struct.pack("!I", len(payload)) + payload
        while output:
            written = os.write(fd, output)
            if written <= 0:
                raise RuntimeError("control_pipe_closed")
            output = output[written:]
    finally:
        os.close(fd)
else:
    os.write(1, payload)
'''


def source_fingerprints() -> dict[str, str]:
    paths = ("src/deskpal/__init__.py", "src/deskpal/execution/__init__.py",
             "src/deskpal/execution/protocol.py", "src/deskpal/execution/runtime.py",
             "src/deskpal/execution/windows/__init__.py", "src/deskpal/execution/windows/launcher.py",
             "src/deskpal/execution/windows/_win32.py", "scripts/diagnose_worker_limits.py",
             "scripts/diagnose_worker_startup.py", "scripts/probe_worker_filesystem.py",
             "scripts/diagnose_worker_network.py")
    return {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in paths}


class HostEndpoints:
    """Only two new noninheritable IPv4 loopback endpoints; bounded drains."""

    def __init__(self) -> None:
        self.sockets: list[socket.socket] = []

    def start(self) -> dict[str, int]:
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            endpoint = socket.socket(socket.AF_INET, kind)
            self.sockets.append(endpoint)
            endpoint.set_inheritable(False)
            endpoint.setblocking(False)
            endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            endpoint.bind(("127.0.0.1", 0))
            if kind == socket.SOCK_STREAM:
                endpoint.listen(4)
        return {"tcp_port": self.sockets[0].getsockname()[1], "udp_port": self.sockets[1].getsockname()[1]}

    def drain(self) -> dict[str, object]:
        result: dict[str, object] = {"tcp_canary_received": False, "udp_canary_received": False,
                                     "tcp_connections": 0, "udp_datagrams": 0, "drain_complete": True}
        tcp, udp = self.sockets
        for endpoint, label in ((tcp, "tcp"), (udp, "udp")):
            for index in range(5):
                readable, _, _ = select.select([endpoint], [], [], 0.2 if index == 0 else 0)
                if not readable:
                    break
                if index == 4:
                    result["drain_complete"] = False
                    break
                if label == "tcp":
                    connection, remote = endpoint.accept()
                    try:
                        connection.setblocking(False)
                        available, _, _ = select.select([connection], [], [], 0.2)
                        data = connection.recv(8) if available else b""
                    finally:
                        connection.close()
                    result["tcp_connections"] = index + 1
                else:
                    data, remote = endpoint.recvfrom(8)
                    result["udp_datagrams"] = index + 1
                if remote[0] == "127.0.0.1" and data == CANARY:
                    result[label + "_canary_received"] = True
        return result

    def close(self) -> None:
        failed: list[socket.socket] = []
        for endpoint in self.sockets:
            try:
                endpoint.close()
            except OSError:
                failed.append(endpoint)
        self.sockets = failed
        if failed:
            raise RuntimeError("network_endpoint_close_unconfirmed")


class HostWorkerOwner:
    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self.launch_started = False
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        if self.launch_started and self.process is None:
            raise RuntimeError("network_host_launch_ownership_unknown")
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=2)
            for pipe in (self.process.stdout, self.process.stderr):
                if pipe is not None:
                    pipe.close()
        self.closed = True


def parse_worker(body: object) -> dict[str, object]:
    if not isinstance(body, dict) or body.get("kind") != "loopback_network_probe":
        raise ValueError("network_worker_schema")
    if (body.get("scope_only_loopback") is not True or body.get("wsa_startup_return") != 0
            or body.get("wsa_version") != 0x202 or body.get("wsa_cleanup_return") != 0):
        raise ValueError("network_wsa_startup_or_cleanup_failed")
    operations = body.get("operations")
    if not isinstance(operations, dict) or set(operations) != set(OPS):
        raise ValueError("network_worker_operations_schema")
    for operation in operations.values():
        if not isinstance(operation, dict):
            raise ValueError("network_worker_operation_schema")
        created, closed = operation.get("socket_created"), operation.get("socket_closed")
        if not ((created is True and closed is True) or (
                created is False and closed == "not_created" and operation.get("stage") == "socket"
                and operation.get("status") != "success")):
            raise ValueError("network_worker_socket_cleanup_failed")
        if operation.get("status") not in {"success", "partial_send", "access_denied", "would_block",
                                           "connection_refused", "timeout", "unknown_error"}:
            raise ValueError("network_worker_status_schema")
        error = operation.get("wsa_error")
        if error is not None and (type(error) is not int or not 0 <= error <= 65535):
            raise ValueError("network_worker_error_schema")
        if operation.get("status") == "access_denied" and error != 10013:
            raise ValueError("network_worker_access_error_mismatch")
        sent = operation.get("bytes_sent")
        if sent is not None and (type(sent) is not int or not 0 <= sent <= 8):
            raise ValueError("network_worker_byte_budget")
    if (body.get("isolated") != 1 or body.get("no_site") != 1 or body.get("site_loaded") is not False
            or body.get("dont_write_bytecode") is not True
            or body.get("environment_keys") != ["LOCALAPPDATA", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"]):
        raise ValueError("network_worker_runtime_mismatch")
    return body


def positive_complete(body: dict[str, object], received: dict[str, object]) -> bool:
    operations = body.get("operations")
    if not isinstance(operations, dict) or set(operations) != set(OPS):
        return False
    for name in OPS:
        item = operations[name]
        if (not isinstance(item, dict) or item.get("status") != "success" or item.get("wsa_error") != 0
                or item.get("socket_created") is not True or item.get("socket_closed") is not True):
            return False
        if name in ("tcp_connect", "udp_send") and item.get("bytes_sent") != 8:
            return False
        if name == "tcp_connect" and item.get("connect_completed") is not True:
            return False
        if name in ("tcp_bind_listen", "udp_bind") and item.get("bind_completed") is not True:
            return False
        if name == "tcp_bind_listen" and item.get("listen_completed") is not True:
            return False
    return (received.get("tcp_canary_received") is True and received.get("udp_canary_received") is True
            and received.get("drain_complete") is True)


def assess_loopback(body: object, received: dict[str, object]) -> dict[str, str]:
    """A missing canary alone never establishes a denial or its cause."""
    findings: dict[str, str] = {}
    operations = body.get("operations") if isinstance(body, dict) else None
    for name in OPS:
        item = operations.get(name) if isinstance(operations, dict) else None
        if not isinstance(item, dict):
            findings[name] = "unresolved_no_valid_worker_observation"
            continue
        arrivals = received.get("tcp_canary_received" if name == "tcp_connect" else "udp_canary_received")
        if name in ("tcp_connect", "udp_send") and arrivals is True:
            findings[name] = "host_received_canary_loopback_access_observed"
        elif item.get("status") == "success":
            findings[name] = ("worker_api_success_receipt_unconfirmed" if name in ("tcp_connect", "udp_send")
                              else "local_bind_or_listen_succeeded_inbound_reachability_unverified")
        elif (item.get("status") == "access_denied" and item.get("wsa_error") == 10013
                and item.get("stage") in {"socket", "connect", "connect_completion", "sendto", "bind", "listen"}):
            findings[name] = ("socket_creation_access_refusal_observed_no_operation_attempt"
                              if item.get("stage") == "socket" else
                              "api_access_refusal_observed_cause_not_independently_attributed")
        else:
            findings[name] = "unresolved_not_access_denial_proof"
    return findings


def run_restricted(spec: LaunchSpec, api: ObserverApi) -> tuple[dict[str, object], bool]:
    entry: dict[str, object] = {"mode": "ordinary_appcontainer_readonly", "application_success": False}
    cleanup = CaseCleanup(api)
    started = monotonic()
    try:
        worker = WindowsLauncher().launch(spec)
        cleanup.add(worker)
        cleanup.launch_resolved = cleanup.worker_returned = True
        cleanup.observers.attach(worker)
        entry["identity"] = asdict(worker.identity)
        result = worker.collect(8, log_limit_bytes=MAX_OUTPUT)
        entry.update(exit_code=result.exit_code, frames=len(result.frames), stdout_bytes=len(result.stdout),
                     stderr_bytes=len(result.stderr))
        if result.exit_code != 0 or len(result.frames) != 1 or result.stdout or result.stderr:
            raise ValueError("network_restricted_application_failed")
        entry["raw_frame"] = result.frames[0]
        entry["report"] = parse_worker(result.frames[0])
        entry["application_success"] = True
    except LaunchRejected as error:
        cleanup.add(error.cleanup_owner)
        cleanup.launch_resolved = True
        entry.update(rejected=error.stage, winerror=error.winerror)
    except ProtocolViolation as error:
        entry["protocol_violation"] = error.code
    except Exception as error:
        entry["error_type"] = type(error).__name__
    finally:
        safe = _cleanup_case(cleanup, entry)
        if not safe:
            _RETAINED.append(cleanup)
        entry["elapsed_seconds"] = monotonic() - started
    return entry, safe


def _new_root(path: Path) -> Path:
    _no_reparse(path)
    resolved = path.resolve()
    allowed = (ROOT / "data/validation/v06").resolve()
    if (resolved == allowed or not resolved.is_relative_to(allowed) or resolved.exists()
            or any(":" in part or Path(part).is_reserved() for part in resolved.parts[1:])):
        raise ValueError("network_requires_new_validation_root")
    resolved.mkdir(parents=True, exist_ok=False)
    _no_reparse(resolved)
    return resolved


def conduct(writer: ReportWriter, report: dict[str, object], python: Path, root: Path) -> int:
    windows, local, elevated = native_context()
    set_caller_localappdata(local)
    report["host_token_elevated"] = elevated
    manifest = stage_runtime(python, root / "runtime", worker_source=WORKER_SOURCE)
    verify_manifest(manifest)
    report["manifest"] = asdict(manifest)
    writer.save(report)
    endpoints, ordinary = HostEndpoints(), HostWorkerOwner()
    profile = create_probe_profile("deskpal.v06." + uuid4().hex)
    try:
        report.update(profile=asdict(profile), cleanup="pending")
        writer.save(report)
        folder = Path(profile.folder)
        temporary = folder / "Temp"
        _no_reparse(temporary)
        temporary.mkdir(exist_ok=True)
        owner = _owner_sid(Path(windows))
        apply_readonly_dacl(manifest, owner_sid=owner, package_sid=profile.sid)
        apply_readonly_dacl(profile, owner_sid=owner, package_sid=profile.sid)
        environment = worker_environment(Path(windows), temporary, local_appdata=folder)
        ports = endpoints.start()
        report["host_endpoints"] = {"address": "127.0.0.1", **ports}
        arguments = ("-I", "-S", "-B", "-X", "utf8", str(Path(manifest.root) / "worker.py"),
                     json.dumps(ports, separators=(",", ":")))
        report["host_control"] = {"mode": "explicit_ordinary_positive_control_never_fallback"}
        ordinary.launch_started = True
        ordinary.process = subprocess.Popen([manifest.executable, *arguments], cwd=manifest.root,
            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True, creationflags=0x08000000)
        output, errors = ordinary.process.communicate(timeout=8)
        report["host_control"] = {"mode": "explicit_ordinary_positive_control_never_fallback",
                                  "exit_code": ordinary.process.returncode, "stdout_bytes": len(output),
                                  "stderr_bytes": len(errors)}
        if len(output) > MAX_OUTPUT or len(errors) > MAX_OUTPUT or ordinary.process.returncode != 0 or errors:
            raise ValueError("network_host_positive_application_failed")
        raw_body: object = json.loads(output)
        report["host_control"] = {"mode": "explicit_ordinary_positive_control_never_fallback",
                                  "exit_code": ordinary.process.returncode, "raw_report": raw_body}
        body = parse_worker(raw_body)
        ordinary.close()
        received = endpoints.drain()
        report["host_control"] = {"report": body, "received": received, "exit_code": 0,
                                  "mode": "explicit_ordinary_positive_control_never_fallback"}
        writer.save(report)
        if not positive_complete(body, received):
            report["status"] = "blocked_positive_control_incomplete"
            return 2
        drained = endpoints.drain()
        report["between_controls_drain"] = drained
        if (drained.get("drain_complete") is not True or drained.get("tcp_connections") != 0
                or drained.get("udp_datagrams") != 0):
            report["status"] = "blocked_endpoint_phase_contamination"
            return 2
        spec = LaunchSpec(Path(manifest.executable), arguments + ("{ipc_write_handle}",),
                          Path(manifest.root), profile.sid, environment)
        report["restricted_cleanup"] = "pending_or_unknown"
        entry, safe = run_restricted(spec, ObserverApi())
        report["restricted"] = entry
        report["restricted_cleanup"] = "confirmed" if safe else "unconfirmed"
        # Drain only after the worker has returned its cleanup fact. If it has
        # not stopped, phase separation and absence-of-traffic claims are unknown.
        if safe:
            received = endpoints.drain()
            report["restricted_received"] = received
            report["loopback_findings"] = assess_loopback(entry.get("report"), received)
        report["status"] = "loopback_observations_collected_requires_review"
        return 0 if safe and entry.get("application_success") is True else 2
    finally:
        safe_to_delete = report.get("restricted_cleanup") not in ("pending_or_unknown", "unconfirmed")
        try:
            ordinary.close()
        except BaseException as error:
            _RETAINED.append(ordinary)
            report.update(host_cleanup="unconfirmed", host_cleanup_error_type=type(error).__name__)
            safe_to_delete = False
        try:
            endpoints.close()
        except BaseException as error:
            _RETAINED.append(endpoints)
            report["endpoint_cleanup_error_type"] = type(error).__name__
            safe_to_delete = False
        if safe_to_delete and not _RETAINED:
            try:
                delete_probe_profile(profile)
                report["cleanup"] = "profile_deleted"
            except BaseException as error:
                report.update(cleanup="failed_profile_may_remain", cleanup_error_type=type(error).__name__)
        else:
            report["cleanup"] = "withheld_unconfirmed_owner"
        writer.save(report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True, dest="python_executable")
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt" or C.sizeof(C.c_void_p) != 8:
        parser.error("Windows x64 only")
    root = _new_root(args.root)
    report: dict[str, object] = {"started_at": datetime.now(timezone.utc).isoformat(),
        "stage0_passed": False, "network_isolation_accepted": False, "scope_only_loopback": True,
        "python_version": sys.version, "python_executable": sys.executable,
        "windows_build": sys.getwindowsversion().build, "source_sha256": source_fingerprints(),
        "cleanup": "not_required", "fallback_allowed": False,
        "unverified": ["LAN", "external_network", "IPv6", "inbound_reachability_to_worker_listener",
                       "firewall_and_loopback_exemption_state_not_changed_or_verified"],
        "interpretation": "10013 is an API access refusal; timeout/10035/10061/unknown are not denial proof; bind/listen success is not inbound reachability proof"}
    writer = ReportWriter(root / "network-report.json")
    code = 2
    try:
        writer.save(report)
        code = conduct(writer, report, args.python_executable, root)
    except Exception as error:
        report.update(status="network_diagnostic_failed", error_type=type(error).__name__)
    finally:
        try:
            report["source_sha256_end"] = source_fingerprints()
            report["source_unchanged"] = report["source_sha256_end"] == report["source_sha256"]
            if not report["source_unchanged"] or report.get("cleanup") != "profile_deleted":
                code = 2
            writer.save(report)
        finally:
            writer.close()
    print(json.dumps({"report": str(writer.path), "diagnostic_exit": code, "cleanup": report["cleanup"]}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
