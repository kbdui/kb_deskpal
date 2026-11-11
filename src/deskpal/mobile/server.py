"""有界单用户 HTTP 网关。建议由 FRP 在外层提供 HTTPS，不信任转发头。"""

from collections import OrderedDict
from collections.abc import Callable
from hashlib import sha256
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import json
from pathlib import Path
import secrets
from socket import getaddrinfo, gethostname, AF_INET, SOCK_STREAM
from threading import BoundedSemaphore, RLock, Thread
from time import monotonic
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from deskpal.application.client import FIELDS
from .access import AccessStore, Pairing

ASSETS = Path(__file__).parent / "assets"
MAX_BODY = 65536
COOKIE = "deskpal_mobile"
Call = Callable[[str, dict[str, Any], Callable[[], bool]], dict[str, Any]]


def origin(value: str) -> str:
    if any(ord(char) < 33 for char in value):
        raise ValueError("公网地址不能包含空白或控制字符。")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("公网地址必须是 HTTPS 来源地址，不含路径或凭据。")
    port = parsed.port  # 同时验证端口是否有效。
    hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    if ":" in hostname:
        hostname = "[" + hostname + "]"
    return "https://" + hostname + (f":{port}" if port is not None and port != 443 else "")


class LimitedServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 8

    def __init__(self, address: tuple[str, int], runtime: "MobileRuntime") -> None:
        self.runtime = runtime
        self.slots = BoundedSemaphore(8)
        super().__init__(address, Handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            request.settimeout(5)
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # URL、Cookie 和用户正文不得进入默认 stderr 请求日志。
        pass


class MobileRuntime:
    """一个桌面后台对应一个实例，角色切换时旧实例永久失效。"""

    def __init__(self, call: Call, path: Path, names: dict[str, str] | None = None) -> None:
        self.call = call
        self.store = AccessStore(path)
        self.pairing = Pairing(self.store)
        self.instance_id = str(uuid4())
        self.csrf_secret = secrets.token_hex(32)
        self.names = names or {"user": "你", "assistant": "Deskpal"}
        self.lock = RLock()
        self.server: LimitedServer | None = None
        self.thread: Thread | None = None
        self.accepting = False
        self.error = ""
        self.config = self.store.configuration()
        self.rates: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self.pair_rates: OrderedDict[str, tuple[float, int]] = OrderedDict()

    def restore(self) -> None:
        if self.config["enabled"]:
            try:
                self.start(self.config)
            except (OSError, ValueError):
                self.error = "手机访问未能启动，请检查端口或重新配置。"

    def start(self, config: dict[str, Any]) -> None:
        bind, port = config.get("bind"), config.get("port")
        if bind not in {"127.0.0.1", "0.0.0.0"} or type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("请使用本机或局域网监听，以及 1024–65535 的端口。")
        public = config.get("public_origin", "")
        if not isinstance(public, str) or len(public) > 300:
            raise ValueError("Invalid public origin")
        public = origin(public) if public else ""
        self.stop()
        self.config = {"enabled": True, "bind": bind, "port": port, "public_origin": public}
        try:
            server = LimitedServer((bind, port), self)
        except OSError:
            self.config["enabled"] = False
            self.error = "端口无法使用，可能已被占用；请更换端口后重新开启。"
            self.store.configure(**self.config)
            raise
        with self.lock:
            self.server = server
            self.accepting = True
            self.error = ""
            self.thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .1},
                                 name="deskpal-mobile-http", daemon=True)
            self.thread.start()
        try:
            self.store.configure(**self.config)
        except Exception:
            self.stop()
            raise

    def reject_new(self) -> None:
        with self.lock:
            self.accepting = False
        self.pairing.clear()

    def stop(self, *, disable: bool = False) -> None:
        self.reject_new()
        server, thread = self.server, self.thread
        self.server = self.thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        if disable:
            self.config["enabled"] = False
            self.store.configure(**self.config)

    def status(self) -> dict[str, Any]:
        port = self.config["port"]
        addresses = [f"http://127.0.0.1:{port}"]
        if self.config["bind"] == "0.0.0.0":
            try:
                addresses += [f"http://{address}:{port}" for address in sorted({
                    item[4][0] for item in getaddrinfo(gethostname(), None, AF_INET, SOCK_STREAM)
                    if ip_address(item[4][0]).is_private and not ip_address(item[4][0]).is_loopback})][:8]
            except OSError:
                pass
        if self.config["public_origin"]:
            addresses.insert(0, self.config["public_origin"])
        return {"enabled": self.accepting, "configuration": dict(self.config),
                "addresses": addresses, "devices": self.store.devices(), "error": self.error}

    def local(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        if operation == "mobile_enable":
            if set(args) != {"bind", "port", "public_origin"}:
                raise ValueError("Invalid mobile configuration")
            try:
                self.start(args)
            except OSError:
                return self.status()
        elif operation == "mobile_disable" and not args:
            self.stop(disable=True)
        elif operation == "mobile_pair" and not args:
            if not self.accepting:
                raise ValueError("Enable mobile access first")
            return {**self.status(), "pairing": self.pairing.generate()}
        elif operation == "mobile_revoke" and set(args) == {"id"}:
            identity = args["id"]
            if identity is not None and (not isinstance(identity, str) or len(identity) != 32):
                raise ValueError("Invalid device identity")
            self.store.revoke(identity)
        elif operation != "mobile_status" or args:
            raise ValueError("Unknown mobile setting operation")
        return self.status()

    def valid(self, token: str) -> bool:
        return self.accepting and self.store.valid(token)

    def csrf(self, token: str) -> str:
        return sha256((self.csrf_secret + token).encode()).hexdigest()

    def rate(self, address: str, *, pairing: bool = False) -> bool:
        with self.lock:
            rates = self.pair_rates if pairing else self.rates
            start, count = rates.pop(address, (monotonic(), 0))
            if monotonic() - start > 60:
                start, count = monotonic(), 0
            rates[address] = (start, count + 1)
            while len(rates) > 256:
                rates.popitem(last=False)
            return count < (10 if pairing else 180)


class Handler(BaseHTTPRequestHandler):
    server: LimitedServer
    server_version = "Deskpal"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        pass

    @property
    def runtime(self) -> MobileRuntime:
        return self.server.runtime

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.reply(code, {"error": "请求无法处理。", "code": "Rejected"})

    def reply(self, status: int, value: Any, *, kind: str = "application/json; charset=utf-8", cookie: str = "") -> None:
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def request_origin(self) -> str:
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or len(hosts[0]) > 300:
            raise ValueError("Invalid host")
        host = hosts[0].lower()
        public = self.runtime.config["public_origin"]
        if public and host == urlsplit(public).netloc:
            return public
        parsed = urlsplit("http://" + host)
        if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or parsed.port != self.server.server_port:
            raise ValueError("Invalid host")
        if parsed.hostname != "localhost":
            address = ip_address(parsed.hostname or "")
            if address.version != 4 or not (address.is_private or address.is_loopback):
                raise ValueError("Host is not allowed")
            if self.runtime.config["bind"] == "127.0.0.1" and not address.is_loopback:
                raise ValueError("Host is not allowed")
        return "http://" + host

    def token(self) -> str:
        try:
            cookies: SimpleCookie = SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
            return cookies[COOKIE].value if COOKIE in cookies else ""
        except CookieError:
            return ""

    def preflight(self, *, post: bool = False) -> str | None:
        try:
            expected = self.request_origin()
        except (ValueError, TypeError):
            self.reply(403, {"error": "访问地址未被允许，请检查手机访问设置。", "code": "Host"})
            return None
        supplied = self.headers.get("Origin")
        if (post and supplied != expected) or (supplied is not None and supplied != expected):
            self.reply(403, {"error": "请求来源不匹配。", "code": "Origin"})
            return None
        if not self.runtime.accepting or self.runtime.server is not self.server:
            self.reply(503, {"error": "手机访问已关闭。", "code": "Unavailable"})
            return None
        if not self.runtime.rate(self.client_address[0]):
            self.reply(429, {"error": "请求过于频繁，请稍后重试。", "code": "RateLimit"})
            return None
        return expected

    def do_GET(self) -> None:
        if not self.preflight():
            return
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/mobile.js": ("mobile.js", "text/javascript; charset=utf-8"),
                  "/mobile.css": ("mobile.css", "text/css; charset=utf-8")}
        if self.path in assets:
            filename, kind = assets[self.path]
            self.reply(200, (ASSETS / filename).read_bytes(), kind=kind)
        elif self.path == "/api/session":
            token = self.token()
            if not self.runtime.valid(token):
                self.reply(401, {"error": "请先使用电脑显示的配对码登录。", "code": "Unauthorized"})
                return
            self.reply(200, {"instance_id": self.runtime.instance_id, "csrf": self.runtime.csrf(token),
                             "names": dict(self.runtime.names)})
        else:
            self.reply(404, {"error": "页面不存在。", "code": "NotFound"})

    def do_POST(self) -> None:
        expected = self.preflight(post=True)
        if expected is None:
            return
        lengths = self.headers.get_all("Content-Length", [])
        try:
            if len(lengths) != 1 or not lengths[0].isdigit() or self.headers.get("Transfer-Encoding"):
                raise ValueError("Invalid length")
            length = int(lengths[0])
            if not 0 < length <= MAX_BODY or self.headers.get_content_type() != "application/json":
                raise ValueError("Invalid body")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete body")
            data = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
            if not isinstance(data, dict):
                raise ValueError("Object required")
        except (ValueError, UnicodeError, OSError, RecursionError):
            self.reply(400, {"error": "请求格式无效或内容过长。", "code": "Rejected"})
            return
        if self.path == "/api/pair":
            if not self.runtime.rate(self.client_address[0], pairing=True):
                self.reply(429, {"error": "配对尝试过于频繁，请稍后再试。", "code": "RateLimit"})
                return
            try:
                code, name = data.get("code"), data.get("name", "我的手机")
                if (set(data) - {"code", "name"} or not isinstance(code, str) or len(code) != 8
                        or not code.isascii() or not code.isdigit() or not isinstance(name, str) or not 1 <= len(name.strip()) <= 60):
                    raise ValueError("Invalid pairing")
                token = self.runtime.pairing.exchange(code, name.strip())
            except ValueError:
                self.reply(403, {"error": "配对码无效、已使用或已过期，请在电脑重新生成。", "code": "Pairing"})
                return
            secure = "; Secure" if expected.startswith("https:") else ""
            self.reply(200, {"paired": True}, cookie=f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000{secure}")
            return
        token = self.token()
        if not self.runtime.valid(token):
            self.reply(401, {"error": "登录已失效，请重新配对。", "code": "Unauthorized"})
            return
        supplied_csrf = self.headers.get("X-Deskpal-CSRF", "")
        if not supplied_csrf.isascii() or not secrets.compare_digest(supplied_csrf, self.runtime.csrf(token)):
            self.reply(403, {"error": "会话已变化，请重新连接。", "code": "Session"})
            return
        if self.path == "/api/logout":
            # 浏览器登出同时撤销该设备的服务端凭据。
            with self.runtime.store.connection() as db:
                db.execute("DELETE FROM device WHERE digest=?", (sha256(token.encode()).hexdigest(),))
            self.reply(200, {"ok": True}, cookie=f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
            return
        if self.path != "/api/command":
            self.reply(404, {"error": "接口不存在。", "code": "NotFound"})
            return
        if data.get("instance_id") != self.runtime.instance_id:
            self.reply(409, {"error": "后台或角色已切换，请重新连接。", "code": "InstanceChanged"})
            return
        operation, args = data.get("operation"), data.get("parameters")
        if (set(data) != {"instance_id", "operation", "parameters"} or not isinstance(operation, str)
                or operation not in FIELDS or not isinstance(args, dict) or set(args) - FIELDS[operation]):
            self.reply(400, {"error": "手机端不支持此操作。", "code": "Rejected"})
            return
        try:
            result = self.runtime.call(operation, args, lambda: self.runtime.server is self.server and self.runtime.valid(token))
            if self.runtime.server is not self.server or not self.runtime.valid(token):
                self.reply(401, {"error": "设备访问已撤销。", "code": "Unauthorized"})
            else:
                self.reply(200 if result.get("ok") else 409, result)
        except TimeoutError:
            self.reply(202, {"ok": False, "error": {"code": "Pending", "message": "尚未收到回执，请查询状态后重试。"}})
        except (ValueError, RuntimeError, BufferError):
            self.reply(503, {"error": "后台暂不可用，请稍后重新连接。", "code": "Unavailable"})
