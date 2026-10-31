"""Provider contract tests with injected, network-free HTTP responses."""

import io
import json
import traceback
import urllib.error
import urllib.request

import pytest

from deskpal.config import LLMConfig
from deskpal.llm.errors import LLMError
from deskpal.llm.models import ChatMessage
from deskpal.llm.providers import OfflineLLMProvider, OpenAILLMProvider
from deskpal.tools.models import ToolCall, ToolSpec


FAKE_KEY = "fake-test-credential-keep-private"
USER = (ChatMessage("user", "你好"),)
ECHO = ToolSpec("echo", "回显文本", {"type": "object", "properties": {"text": {"type": "string"}},
                                    "required": ["text"], "additionalProperties": False})


def text_response(text="模型回复"):
    return {"status": "completed", "output": [{"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": text}]}]}


def function_output(**overrides):
    return {"type": "function_call", "call_id": "call-1", "name": "echo",
            "arguments": '{"text":"中文"}', **overrides}


class MockTransport:
    def __init__(self, body, *, status=200, error=None):
        self.body = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.status = status
        self.error = error
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        stream = io.BytesIO(self.body)
        stream.status = self.status
        return stream


def provider(transport, **kwargs):
    return OpenAILLMProvider(model="test-model", api_key=FAKE_KEY, transport=transport, **kwargs)


def test_responses_request_maps_configuration_messages_and_trusted_tool_schema():
    transport = MockTransport(text_response())
    config = LLMConfig(provider="openai", model="configured-model", api_key=FAKE_KEY,
                       base_url="https://service.invalid/v1/", timeout_seconds=4.5)
    response = OpenAILLMProvider(config, transport=transport).chat(
        (ChatMessage("system", "系统消息"), *USER), tools=(ECHO,))
    request, timeout = transport.calls[0]
    payload = json.loads(request.data)
    assert request.full_url == "https://service.invalid/v1/responses"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer " + FAKE_KEY
    assert request.get_header("Content-type") == "application/json"
    assert timeout == 4.5
    assert payload["model"] == "configured-model"
    assert payload["store"] is False
    assert payload["input"] == [{"role": "system", "content": "系统消息"},
                                {"role": "user", "content": "你好"}]
    tool = payload["tools"][0]
    assert tool["type"] == "function"
    assert tool["name"] == ECHO.name
    assert tool["description"] == ECHO.description
    assert tool["parameters"] == ECHO.parameters
    assert response.content == "模型回复"
    assert response.provider == "openai"
    assert response.model == "configured-model"
    assert response.tool_calls == ()


def test_assistant_tool_call_and_tool_result_history_map_by_call_id():
    transport = MockTransport(text_response("完成"))
    call = ToolCall("echo", {"text": "你好"}, "call-previous")
    response = provider(transport).chat((*USER, ChatMessage("assistant", "执行", tool_calls=(call,)),
                                         ChatMessage("tool", "你好", tool_call_id=call.call_id)))
    inputs = json.loads(transport.calls[0][0].data)["input"]
    assert inputs[1] == {"role": "assistant", "content": "执行"}
    assert inputs[2]["type"] == "function_call"
    assert inputs[2]["call_id"] == call.call_id
    assert inputs[2]["name"] == "echo"
    assert json.loads(inputs[2]["arguments"]) == {"text": "你好"}
    assert inputs[3] == {"type": "function_call_output", "call_id": call.call_id, "output": "你好"}
    assert response.content == "完成"


def test_response_returns_text_and_calls_while_discarding_reasoning():
    body = text_response("反馈")
    body["output"].insert(0, {"type": "reasoning", "summary": [{"text": "internal-reasoning-marker"}]})
    body["output"].append(function_output())
    response = provider(MockTransport(body)).chat(USER, tools=(ECHO,))
    assert response.content == "反馈"
    assert response.tool_calls == (ToolCall("echo", {"text": "中文"}, "call-1"),)
    assert "internal-reasoning-marker" not in repr(response)


def test_generate_uses_plain_message_and_returns_text():
    transport = MockTransport(text_response("生成结果"))
    assert provider(transport).generate("输入") == "生成结果"
    payload = json.loads(transport.calls[0][0].data)
    assert payload["input"] == [{"role": "user", "content": "输入"}]
    assert not payload.get("tools")


@pytest.mark.parametrize("include_text", [False, True])
def test_generate_rejects_tool_call_response_instead_of_returning_partial_text(include_text):
    body = text_response("partial-text") if include_text else {"output": []}
    body["output"].append(function_output())
    with pytest.raises(LLMError):
        provider(MockTransport(body)).generate("只生成文本")


@pytest.mark.parametrize("body", [
    {}, {"output": {}}, [], {"status": "incomplete", "output": []},
    {"status": "failed", "error": {"message": FAKE_KEY}, "output": []},
    {"incomplete_details": {"reason": "max_output_tokens"}, "output": []},
    {"output": []}, {"output": [{"type": "reasoning"}]},
    {"output": [{"type": "unknown"}]}, {"output": [None]},
    {"output": [{"type": "message", "role": "user", "content": []}]},
    {"output": [{"type": "message", "content": [{"type": "output_text", "text": 1}]}]},
    {"output": [{"type": "message", "content": [{"type": "refusal", "refusal": FAKE_KEY}]}]},
    {"output": [{"type": "message", "content": [{"type": "unknown", "text": "x"}]}]},
    text_response("  "), {"output": [function_output(status="incomplete")]},
    {"output": [function_output(call_id="")]}, {"output": [function_output(name="")]},
    {"output": [function_output(), function_output()]},
])
def test_invalid_output_fails_with_sanitized_error(body):
    with pytest.raises(LLMError) as error:
        provider(MockTransport(body)).chat(USER)
    assert FAKE_KEY not in str(error.value)
    assert FAKE_KEY not in "".join(traceback.format_exception(error.type, error.value, error.tb))


@pytest.mark.parametrize("arguments", ["[]", "null", "3", '"text"', "not-json",
                                        '{"x":NaN}', '{"x":Infinity}', '{"x":1,"x":2}',
                                        '{"nested":{"x":1,"x":2}}', {"text": "x"}])
def test_tool_arguments_require_strict_json_object(arguments):
    with pytest.raises(LLMError):
        provider(MockTransport({"output": [function_output(arguments=arguments)]})).chat(USER)


@pytest.mark.parametrize("raw", [b'{"output":[],"output":[]}', b'{"output":[],"x":NaN}',
                                 b'{"output":[],"x":1e999}', b'not json', b'\xff'])
def test_malformed_response_json_is_rejected(raw):
    with pytest.raises(LLMError):
        provider(MockTransport(raw)).chat(USER)


@pytest.mark.parametrize("exception", [
    TimeoutError(FAKE_KEY), urllib.error.URLError(TimeoutError(FAKE_KEY)),
    urllib.error.URLError(FAKE_KEY), RuntimeError(FAKE_KEY),
    urllib.error.HTTPError("https://service.invalid/" + FAKE_KEY, 401, FAKE_KEY,
                           {"Authorization": FAKE_KEY}, io.BytesIO(("raw-http-body " + FAKE_KEY).encode())),
])
def test_transport_failures_hide_credentials_and_raw_body_in_exception_traceback(exception):
    transport = MockTransport(text_response(), error=exception)
    with pytest.raises(LLMError) as error:
        provider(transport).chat(USER)
    visible = str(error.value) + "".join(traceback.format_exception(error.type, error.value, error.tb))
    assert FAKE_KEY not in visible
    assert "raw-http-body" not in visible
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", [301, 401, 500])
def test_http_response_failure_does_not_parse_raw_error_body(status):
    with pytest.raises(LLMError) as error:
        provider(MockTransport(("raw-http-body " + FAKE_KEY).encode(), status=status)).chat(USER)
    assert FAKE_KEY not in str(error.value)
    assert "raw-http-body" not in str(error.value)


@pytest.mark.parametrize("messages", [
    (), (ChatMessage("tool", "result", tool_call_id="missing"),),
    (ChatMessage("assistant", "", tool_calls=(ToolCall("echo", {}, ""),)),),
    (ChatMessage("user", "x", tool_calls=(ToolCall("echo", {}, "call-1"),)),),
    (ChatMessage("assistant", "", tool_calls=(ToolCall("echo", {}, "call-1"),) * 2),),
    (ChatMessage("assistant", "", tool_calls=(ToolCall("echo", {}, "call-1"),)),
     ChatMessage("tool", "result", tool_call_id="call-1"),
     ChatMessage("tool", "duplicate", tool_call_id="call-1")),
])
def test_invalid_tool_history_fails_before_transport(messages):
    transport = MockTransport(text_response())
    with pytest.raises(LLMError):
        provider(transport).chat(messages)
    assert transport.calls == []


@pytest.mark.parametrize("tools", [
    (ECHO, ECHO), (ToolSpec("bad", "bad", {"type": "array"}),),
    (ToolSpec("bad", "bad", {"type": "object", "value": float("nan")}),),
])
def test_invalid_tool_descriptions_are_not_sent(tools):
    transport = MockTransport(text_response())
    with pytest.raises(LLMError):
        provider(transport).chat(USER, tools=tools)
    assert transport.calls == []


@pytest.mark.parametrize("url", ["http://remote.invalid/v1", "file:///tmp/api",
                                  "https://user:fake-password@service.invalid/v1",
                                  "https://service.invalid/v1?token=fake", "https://service.invalid/v1#fragment"])
def test_unsafe_endpoint_configuration_is_rejected(url):
    with pytest.raises(LLMError):
        provider(MockTransport(text_response()), base_url=url)


def test_default_transport_refuses_redirect_before_following_credentials(monkeypatch):
    visited = []

    def fake_build_opener(*handlers):
        class Opener:
            def open(self, request, *, timeout):
                visited.append(request.full_url)
                assert any(isinstance(handler, urllib.request.HTTPRedirectHandler) for handler in handlers)
                for handler in handlers:
                    if isinstance(handler, urllib.request.HTTPRedirectHandler):
                        follow = handler.redirect_request(request, None, 302, "redirect", {},
                                                          "https://other.invalid/steal")
                        if follow is not None:
                            visited.append(follow.full_url)
                raise urllib.error.HTTPError(request.full_url, 302, "redirect", {}, None)
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
    with pytest.raises(LLMError):
        OpenAILLMProvider(model="test-model", api_key=FAKE_KEY).chat(USER)
    assert visited == ["https://api.openai.com/v1/responses"]


@pytest.mark.parametrize("command,name,arguments", [
    ("echo 中文消息", "echo", {"text": "中文消息"}), ("time", "time", {}),
    ('read "folder/file name.txt"', "read_text", {"path": "folder/file name.txt"}),
    ('write "file name.txt" 第一行\n第二行', "write_text", {"path": "file name.txt", "text": "第一行\n第二行"}),
    ("帮我执行一个任务", "echo", {"text": "离线演示任务已执行"}),
])
def test_offline_commands_propose_only_registered_tools(command, name, arguments):
    tools = (ToolSpec(name, "demo", {"type": "object"}),)
    response = OfflineLLMProvider().chat((ChatMessage("user", command),), tools=tools)
    assert "离线演示" in response.content
    assert response.provider == "offline"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == name
    assert response.tool_calls[0].arguments == arguments
    assert response.tool_calls[0].call_id
    denied = OfflineLLMProvider().chat((ChatMessage("user", command),))
    assert denied.tool_calls == ()
    assert "离线演示" in denied.content


@pytest.mark.parametrize("command", ["你好", "write only-path", "read file extra-argument"])
def test_offline_plain_and_invalid_commands_do_not_claim_tool_execution(command):
    response = OfflineLLMProvider().chat((ChatMessage("user", command),), tools=(ECHO,))
    assert "离线演示" in response.content
    assert response.tool_calls == ()


@pytest.mark.parametrize("reflect_marker", [False, True])
def test_offline_reflection_summarizes_actual_results_without_repeating_command(reflect_marker):
    call = ToolCall("echo", {"text": "expected"}, "call-1")
    messages = (ChatMessage("system", "[REFLECT]" if reflect_marker else "总结"),
                ChatMessage("user", "echo expected"), ChatMessage("assistant", "", tool_calls=(call,)),
                ChatMessage("tool", "actual-output", tool_call_id="call-1"))
    response = OfflineLLMProvider().chat(messages)
    assert "actual-output" in response.content
    assert "离线演示" in response.content
    assert response.tool_calls == ()


def test_offline_generate_is_clearly_marked_as_demo():
    assert "离线演示" in OfflineLLMProvider().generate("你好")


def test_offline_prior_tool_results_do_not_hide_new_user_message():
    call = ToolCall("echo", {"text": "old"}, "call-old")
    messages = (ChatMessage("user", "echo old"),
                ChatMessage("assistant", "", tool_calls=(call,)),
                ChatMessage("tool", "old-tool-output", tool_call_id=call.call_id),
                ChatMessage("user", "新的普通问题"))
    response = OfflineLLMProvider().chat(messages)
    assert "新的普通问题" in response.content
    assert "old-tool-output" not in response.content
    assert response.tool_calls == ()


def test_offline_independent_reflect_request_accepts_results_as_plain_text():
    response = OfflineLLMProvider().chat((
        ChatMessage("system", "[REFLECT] 根据实际工具结果总结"),
        ChatMessage("user", "目标：echo expected\n实际工具结果：actual-output"),
    ))
    assert "actual-output" in response.content
    assert "离线演示" in response.content
    assert response.tool_calls == ()
