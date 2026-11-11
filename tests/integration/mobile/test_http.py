"""回环 HTTP 验证：无公网访问，授权库和所有业务数据均为合成。"""
from contextlib import contextmanager
from http.client import HTTPConnection
import json
import socket

import pytest

from deskpal.application.client import ClientGateway
from deskpal.mobile.server import MobileRuntime


class Port:
    def __init__(self):
        self.calls = []

    def execute(self, operation, parameters):
        self.calls.append((operation, parameters))
        return {"operation": operation, "items": []}


def unused_port():
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


@contextmanager
def running(tmp_path):
    port = Port()
    gateway = ClientGateway(port)
    def call(operation, parameters, guard):
        assert guard()
        return {"ok": True, "data": gateway.execute(operation, parameters)}
    runtime = MobileRuntime(call, tmp_path / "access.sqlite3")
    runtime.start({"bind": "127.0.0.1", "port": unused_port(), "public_origin": ""})
    try:
        yield runtime, port
    finally:
        runtime.stop()


def request(runtime, path, data=None, *, cookie="", headers=None):
    connection = HTTPConnection("127.0.0.1", runtime.config["port"], timeout=5)
    fields = {"Origin": f"http://127.0.0.1:{runtime.config['port']}", "Content-Type": "application/json"}
    if cookie:
        fields["Cookie"] = cookie
    fields.update(headers or {})
    try:
        connection.request("GET" if data is None else "POST", path,
                           body=None if data is None else json.dumps(data).encode(), headers=fields)
        response = connection.getresponse()
        raw = response.read()
        body = json.loads(raw) if "application/json" in response.getheader("Content-Type", "") else raw
        return response.status, body, dict(response.getheaders())
    finally:
        connection.close()


def pair(runtime, **headers):
    code = runtime.local("mobile_pair", {})["pairing"]["code"]
    status, _, response_headers = request(runtime, "/api/pair", {"code": code, "name": "合成手机"}, headers=headers)
    assert status == 200
    cookie = response_headers["Set-Cookie"].split(";", 1)[0]
    status, session, _ = request(runtime, "/api/session", cookie=cookie, headers=headers)
    assert status == 200
    return cookie, session, response_headers


def command(runtime, session, operation="conversation_list", parameters=None):
    return {"instance_id": session["instance_id"], "operation": operation, "parameters": parameters or {}}


def test_pair_session_and_authenticated_narrow_command(tmp_path):
    with running(tmp_path) as (runtime, port):
        assert request(runtime, "/api/session")[0] == 401
        assert request(runtime, "/")[0] == 200
        assert request(runtime, "/mobile.js")[0] == 200
        cookie, session, headers = pair(runtime)
        assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
        response = request(runtime, "/api/command", command(runtime, session), cookie=cookie,
                           headers={"X-Deskpal-CSRF": session["csrf"]})
        assert response[0] == 200 and response[1]["data"]["items"] == []
        assert port.calls == [("conversation_list", {})]
        assert "no-store" in response[2]["Cache-Control"]


def test_csrf_host_and_operation_boundaries_never_reach_business(tmp_path):
    with running(tmp_path) as (runtime, port):
        cookie, session, _ = pair(runtime)
        body = command(runtime, session)
        assert request(runtime, "/api/command", body, cookie=cookie)[0] == 403
        good = {"X-Deskpal-CSRF": session["csrf"]}
        assert request(runtime, "/api/command", body, cookie=cookie, headers={**good, "Origin": "https://evil.example"})[0] == 403
        assert request(runtime, "/api/session", cookie=cookie, headers={"Host": "evil.example", "Origin": "http://evil.example"})[0] == 403
        assert request(runtime, "/api/command", command(runtime, session, "settings"), cookie=cookie, headers=good)[0] == 400
        assert request(runtime, "/api/command", command(runtime, session, "submit", {"authorized_tools": ["write_file"]}), cookie=cookie, headers=good)[0] == 400
        body["instance_id"] = "previous-role"
        assert request(runtime, "/api/command", body, cookie=cookie, headers=good)[0] == 409
        assert port.calls == []


def test_device_revocation_and_logout_invalidate_the_cookie(tmp_path):
    with running(tmp_path) as (runtime, _):
        cookie, session, _ = pair(runtime)
        runtime.local("mobile_revoke", {"id": runtime.store.devices()[0]["id"]})
        assert request(runtime, "/api/session", cookie=cookie)[0] == 401
        cookie, session, _ = pair(runtime)
        assert request(runtime, "/api/logout", {}, cookie=cookie, headers={"X-Deskpal-CSRF": session["csrf"]})[0] == 200
        assert request(runtime, "/api/session", cookie=cookie)[0] == 401


def test_public_origin_requires_https_and_sets_secure_cookie(tmp_path):
    with running(tmp_path) as (runtime, _):
        with pytest.raises(ValueError):
            runtime.start({"bind": "127.0.0.1", "port": unused_port(), "public_origin": "http://phone.example"})
        runtime.config["public_origin"] = "https://phone.example:8443"
        _, _, headers = pair(runtime, Host="phone.example:8443", Origin="https://phone.example:8443")
        assert "; Secure" in headers["Set-Cookie"]


def test_traversal_oversized_body_and_reused_pair_code(tmp_path):
    with running(tmp_path) as (runtime, _):
        assert request(runtime, "/../access.sqlite3")[0] == 404
        code = runtime.local("mobile_pair", {})["pairing"]["code"]
        assert request(runtime, "/api/pair", {"code": code})[0] == 200
        assert request(runtime, "/api/pair", {"code": code})[0] == 403
        assert request(runtime, "/api/pair", {"code": "猫" * 8})[0] == 403
        assert request(runtime, "/api/pair", {"code": "x" * 66000})[0] == 400


def test_disable_closes_listener_and_restore_uses_saved_configuration(tmp_path):
    with running(tmp_path) as (runtime, _):
        runtime.stop(disable=True)
        assert runtime.store.configuration()["enabled"] is False
        runtime.restore()
        assert not runtime.accepting
        runtime.start({"bind": "127.0.0.1", "port": unused_port(), "public_origin": ""})
        runtime.stop()
        runtime.restore()
        assert request(runtime, "/")[0] == 200


def test_busy_port_reports_stopped_state_without_breaking_desktop(tmp_path):
    with running(tmp_path) as (runtime, _), socket.socket() as occupied:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        state = runtime.local("mobile_enable", {"bind": "127.0.0.1", "port": occupied.getsockname()[1], "public_origin": ""})
        assert state["enabled"] is False and state["error"]
        assert runtime.store.configuration()["enabled"] is False
