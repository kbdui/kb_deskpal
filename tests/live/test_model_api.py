"""Opt-in real API checks. Default collection/runs never access credentials/network.

Set DESKPAL_RUN_LIVE_API_TESTS=1 plus DEEPSEEK_API_KEY,
DESKPAL_LLM_MODEL and DESKPAL_LLM_BASE_URL to run. HTTP/auth/balance/network
failures are test failures, not skips. No local config file is read.
"""

import json
import os
import socket
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from deskpal.llm.models import ChatMessage
from deskpal.llm.errors import LLMError
from deskpal.llm.providers import OpenAILLMProvider
from deskpal.sandbox import WorkspaceSandbox
from deskpal.tools import BuiltinToolManager, ToolContext


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _BoundedLiveTransport:
    """Limit output cost and expose only safe failure categories to the test."""

    def __init__(self):
        self.failure_category = ""
        self.calls = 0

    def __call__(self, request, *, timeout):
        self.calls += 1
        payload = json.loads(request.data)
        payload["max_output_tokens"] = 128
        payload["reasoning"] = {"effort": "none"}
        declared = payload.get("tools", [])
        if len(declared) == 1 and declared[0].get("name") == "echo":
            payload["tool_choice"] = {"type": "function", "name": "echo"}
        limited = urllib.request.Request(request.full_url, data=json.dumps(payload).encode("utf-8"),
                                         headers=dict(request.header_items()), method=request.get_method())
        try:
            return urllib.request.build_opener(_RejectRedirects()).open(limited, timeout=timeout)
        except urllib.error.HTTPError as error:
            self.failure_category = f"HTTP {error.code}"
            raise
        except (TimeoutError, socket.timeout):
            self.failure_category = "timeout"
            raise
        except urllib.error.URLError:
            self.failure_category = "network error"
            raise


@pytest.fixture
def live_provider():
    if os.environ.get("DESKPAL_RUN_LIVE_API_TESTS") != "1":
        pytest.skip("Real API not run: DESKPAL_RUN_LIVE_API_TESTS is not 1")
    model = os.environ.get("DESKPAL_LLM_MODEL", "").strip()
    base_url = os.environ.get("DESKPAL_LLM_BASE_URL", "").strip()
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key.strip():
        pytest.skip("Real API not run: DEEPSEEK_API_KEY is not configured")
    if not model or not base_url:
        pytest.skip("Real API not run: explicit DESKPAL_LLM_MODEL and DESKPAL_LLM_BASE_URL are required")
    transport = _BoundedLiveTransport()
    try:
        provider = OpenAILLMProvider(model=model, api_key=key, base_url=base_url,
                                     timeout_seconds=30, transport=transport)
    except Exception:
        pytest.fail("Live provider configuration failed; sensitive details omitted", pytrace=False)
    return provider, transport


def _live_chat(live_provider, messages, *, tools=()):
    provider, transport = live_provider
    try:
        return provider.chat(messages, tools=tools)
    except Exception:
        category = transport.failure_category or "provider/response validation error"
        pytest.fail(f"Real API failed ({category}); credentials and response body omitted", pytrace=False)


def test_live_default_gate_never_looks_up_credentials_or_opens_network(monkeypatch):
    class GuardedEnvironment:
        def get(self, name, default=None):
            if name != "DESKPAL_RUN_LIVE_API_TESTS":
                raise AssertionError("Default live gate must not access credentials or service settings")
            return default

    def forbidden_network(*args, **kwargs):
        raise AssertionError("Default live gate must not access network")

    monkeypatch.setitem(globals(), "os", SimpleNamespace(environ=GuardedEnvironment()))
    monkeypatch.setattr(urllib.request, "build_opener", forbidden_network)
    with pytest.raises(pytest.skip.Exception, match="DESKPAL_RUN_LIVE_API_TESTS"):
        live_provider.__wrapped__()


def test_opt_in_without_key_is_not_run_and_never_opens_network(monkeypatch):
    def forbidden_network(*args, **kwargs):
        raise AssertionError("Missing-key live gate must not access network")

    monkeypatch.setitem(globals(), "os", SimpleNamespace(environ={
        "DESKPAL_RUN_LIVE_API_TESTS": "1", "DESKPAL_LLM_MODEL": "fake-model",
        "DESKPAL_LLM_BASE_URL": "https://service.invalid/v1"}))
    monkeypatch.setattr(urllib.request, "build_opener", forbidden_network)
    with pytest.raises(pytest.skip.Exception, match="DEEPSEEK_API_KEY"):
        live_provider.__wrapped__()


@pytest.mark.parametrize("category", ["HTTP 401", "HTTP 402", "network error", "timeout"])
def test_enabled_live_failures_are_failures_without_sensitive_error_output(category):
    class BrokenProvider:
        def chat(self, messages, *, tools=()):
            raise LLMError("fake-credential and raw-response-marker must not be shown")

    transport = SimpleNamespace(failure_category=category)
    with pytest.raises(pytest.fail.Exception, match=category) as failed:
        _live_chat((BrokenProvider(), transport), (ChatMessage("user", "test"),))
    assert "fake-credential" not in str(failed.value)
    assert "raw-response-marker" not in str(failed.value)


def test_live_transport_bounds_tokens_disables_reasoning_and_forces_only_echo(monkeypatch):
    captured = []

    class FakeOpener:
        def open(self, request, *, timeout):
            captured.append(json.loads(request.data))
            return None

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: FakeOpener())
    transport = _BoundedLiveTransport()
    request = urllib.request.Request("https://service.invalid/responses", data=json.dumps(
        {"model": "fake-model", "store": False, "input": [],
         "tools": [{"type": "function", "name": "echo"}]}).encode("utf-8"))
    transport(request, timeout=30)
    assert captured[0]["max_output_tokens"] == 128
    assert captured[0]["reasoning"] == {"effort": "none"}
    assert captured[0]["tool_choice"] == {"type": "function", "name": "echo"}
    assert captured[0]["store"] is False


def test_live_plain_text_chat(live_provider):
    response = _live_chat(live_provider, (ChatMessage("user", "Reply with only the word OK."),))
    if not response.content.strip() or response.tool_calls:
        pytest.fail("Real API did not return plain text without tools; response omitted", pytrace=False)


def test_live_echo_call_executes_real_tool_and_reflects_independently(live_provider, tmp_path):
    manager = BuiltinToolManager(WorkspaceSandbox(tmp_path))
    manager.register_builtin()
    echo_specs = tuple(spec for spec in manager.list_tools() if spec.name == "echo")
    marker = "deskpal-live-echo"
    planned = _live_chat(live_provider, (
        ChatMessage("system", "For this test call the supplied echo tool exactly once. Do not answer before calling it."),
        ChatMessage("user", f"Call echo with text exactly {marker}."),
    ), tools=echo_specs)
    if len(planned.tool_calls) != 1 or planned.tool_calls[0].name != "echo":
        pytest.fail("Real API did not propose exactly one echo call; response omitted", pytrace=False)
    result = manager.execute(planned.tool_calls[0], ToolContext("live-smoke", tmp_path))
    if not result.success or result.output != marker:
        pytest.fail("Parsed real API echo arguments did not execute correctly; response omitted", pytrace=False)
    explicit = json.dumps({"request": "Echo the fixed marker", "tool_results": [
        {"tool_name": result.tool_name, "success": result.success, "output": result.output}]})
    reflected = _live_chat(live_provider, (
        ChatMessage("system", "[REFLECT] Summarize the explicit tool result in at most ten words. No tools."),
        ChatMessage("user", explicit),
    ), tools=())
    if not reflected.content.strip() or reflected.tool_calls:
        pytest.fail("Independent real API reflection did not return plain text; response omitted", pytrace=False)
