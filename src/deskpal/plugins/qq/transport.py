"""本机 OneBot HTTP API 与签名事件回调；不使用代理或自动重定向。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError
from urllib.parse import urlsplit
import hmac
import ipaddress
import json
from typing import Any, Callable


class ProtocolError(RuntimeError):
    def __init__(self, message: str, *, unknown: bool = False) -> None:
        super().__init__(message)
        self.unknown = unknown


def verify_event_signature(secret: str, body: bytes, signature: str) -> bool:
    """校验 NapCat 对原始事件正文生成的 HMAC-SHA1 签名。"""
    expected = 'sha1=' + hmac.new(secret.encode(), body, 'sha1').hexdigest()
    return hmac.compare_digest(signature, expected)


def endpoint(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('OneBot 地址格式无效')
    parsed = urlsplit(value)
    try:
        local = parsed.hostname == 'localhost' or ipaddress.ip_address(parsed.hostname or '').is_loopback
        port = parsed.port
    except ValueError:
        local, port = False, None
    if (parsed.scheme != 'http' or not local or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/') or not port):
        raise ValueError('首版 OneBot 地址须为本机 HTTP 地址，例如 http://127.0.0.1:3000')
    return value.rstrip('/')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class OneBotClient:
    def __init__(self, base: str, token: str, timeout: float = 8) -> None:
        self.base, self.token, self.timeout = endpoint(base), token, timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(self, action: str, arguments: dict[str, Any] | None = None, *, effect: bool = False) -> dict[str, Any]:
        if action not in {'get_login_info', 'get_status', 'get_version_info', 'send_private_msg', 'send_group_msg'}:
            raise ValueError('OneBot action is not available in chat-only version')
        body = json.dumps(arguments or {}, ensure_ascii=False, allow_nan=False).encode('utf-8')
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        try:
            with self.opener.open(Request(self.base + '/' + action, data=body, headers=headers), timeout=self.timeout) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('Response exceeds limit')
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError('Invalid response')
            if value.get('status') != 'ok' or type(value.get('retcode')) is not int or value.get('retcode') != 0:
                raise ProtocolError('NapCat 未确认操作成功。', unknown=effect and value.get('status') == 'async')
            data = value.get('data')
            if not isinstance(data, dict):
                raise ValueError('Missing operation result')
            return data
        except ProtocolError:
            raise
        except HTTPError as error:
            raise ProtocolError('NapCat 接口未受理请求，请检查地址、Token 和服务状态。',
                                unknown=effect and error.code >= 500) from None
        except Exception:
            raise ProtocolError('无法确认 NapCat 的响应，请检查连接。', unknown=effect) from None


class EventReceiver:
    def __init__(self, port: int, secret: str, receive: Callable[[dict[str, Any]], None]) -> None:
        if type(port) is not int or not 1024 <= port <= 65535 or len(secret) < 16:
            raise ValueError('事件端口须在 1024–65535，签名密钥至少 16 个字符')
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass  # 不将请求正文、地址参数或凭据写入终端。

            def do_POST(self) -> None:
                self.connection.settimeout(3)
                try:
                    if self.path != '/onebot/events' or self.headers.get('Transfer-Encoding'):
                        self.send_error(404); return
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 65536:
                        self.send_error(413); return
                    body = self.rfile.read(size)
                    if not verify_event_signature(secret, body, self.headers.get('X-Signature', '')):
                        self.send_error(403); return
                    value = json.loads(body)
                    if not isinstance(value, dict):
                        self.send_error(400); return
                    receive(value)  # 先持久保存和去重，不在 HTTP 线程调用模型。
                    self.send_response(204); self.end_headers()
                except (ValueError, UnicodeError):
                    self.send_error(400)
                except Exception:
                    self.send_error(503)
        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.server.daemon_threads = True
        self.thread = Thread(target=self.server.serve_forever, name='deskpal-qq-events', daemon=True)

    def start(self) -> None:
        try:
            self.thread.start()
        except BaseException:
            self.server.server_close()
            raise

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
