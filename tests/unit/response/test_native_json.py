"""Native JSON is an optional expression capability; all transport is synthetic."""

from collections.abc import Sequence
from dataclasses import asdict
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.request import Request

import pytest

from deskpal.config import ConfigError, LLMConfig, load_config
from deskpal.llm.calls import ModelCallGateway
from deskpal.llm.errors import LLMError
from deskpal.llm.models import ChatMessage, LLMResponse
from deskpal.llm.providers import OpenAILLMProvider
from deskpal.tools.models import ToolCall, ToolSpec
from deskpal.workflow import PersistentTaskManager, WorkflowBlocked, WorkflowStore
from deskpal.workflow.humanization import HumanizationStore


MESSAGES = (ChatMessage("user", "Return a JSON object."),)


class Transport:
    def __init__(self, output: list[dict[str, Any]] | None = None) -> None:
        self.requests: list[tuple[Request, float]] = []
        self.output = output if output is not None else [
            {"type": "message", "content": [{"type": "output_text", "text": '{"reply_text":"ok"}'}]}]
        self.timeout = False

    def __call__(self, request: Request, *, timeout: float) -> BytesIO:
        self.requests.append((request, timeout))
        if self.timeout:
            raise TimeoutError("synthetic secret must not escape")
        return BytesIO(json.dumps({"output": self.output}).encode())


def remote(transport: Transport, mode: str = "json_object") -> OpenAILLMProvider:
    return OpenAILLMProvider(LLMConfig(provider="openai", model="synthetic",
        api_key="synthetic-test-key", timeout_seconds=1.25,
        expression_output_format=mode), transport=transport)


def test_default_and_legacy_positional_config_remain_prompt() -> None:
    assert LLMConfig().expression_output_format == "prompt"
    assert LLMConfig("openai", "model", "https://example.test/v1", "KEY", 2, "fake").expression_output_format == "prompt"
    assert load_config(environ={}).llm.expression_output_format == "prompt"


@pytest.mark.parametrize("mode", ["prompt", "json_object"])
def test_output_format_toml_and_environment_override(tmp_path: Path, mode: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f'[llm]\nexpression_output_format = "{mode}"\n', encoding="utf-8")
    assert load_config(path, environ={}).llm.expression_output_format == mode
    other = "prompt" if mode == "json_object" else "json_object"
    assert load_config(path, environ={"DESKPAL_LLM_EXPRESSION_OUTPUT_FORMAT": other}).llm.expression_output_format == other


@pytest.mark.parametrize("value", ["", "json_schema", "JSON_OBJECT", True, 1, None])
def test_direct_config_rejects_invalid_modes(value: Any) -> None:
    with pytest.raises(ConfigError, match="expression_output_format"):
        LLMConfig(expression_output_format=value)


@pytest.mark.parametrize("literal", ['"json_schema"', '""', "true", "42", "[]"])
def test_toml_mode_is_validated(tmp_path: Path, literal: str) -> None:
    path = tmp_path / "invalid.toml"
    path.write_text(f"[llm]\nexpression_output_format = {literal}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="expression_output_format"):
        load_config(path, environ={})


@pytest.mark.parametrize("value", ["", "json_schema", "true"])
def test_environment_mode_is_validated(value: str) -> None:
    with pytest.raises(ConfigError, match="expression_output_format"):
        load_config(environ={"DESKPAL_LLM_EXPRESSION_OUTPUT_FORMAT": value})


def test_json_transport_format_is_explicit_and_normal_calls_keep_original_payload() -> None:
    transport = Transport()
    provider = remote(transport)
    assert provider.supports_json_output is True
    assert provider.chat_json(MESSAGES).content == '{"reply_text":"ok"}'
    provider.chat(MESSAGES)
    provider.generate("ordinary generation")
    payloads = [json.loads(request.data or b"") for request, _ in transport.requests]
    assert payloads[0]["text"] == {"format": {"type": "json_object"}}
    assert "text" not in payloads[1] and "text" not in payloads[2]
    assert all("tools" not in payload and payload["store"] is False for payload in payloads)
    assert [timeout for _, timeout in transport.requests] == [1.25] * 3
    assert all(request.full_url.endswith("/v1/responses") for request, _ in transport.requests)


def test_disabled_native_json_makes_zero_transport_calls() -> None:
    transport = Transport()
    provider = remote(transport, "prompt")
    assert provider.supports_json_output is False
    with pytest.raises(LLMError, match="not enabled"):
        provider.chat_json(MESSAGES)
    assert transport.requests == []


@pytest.mark.parametrize("purpose", ["planning", "memory_evaluation"])
def test_gateway_exposes_native_json_only_for_expression(purpose: str) -> None:
    transport = Transport()
    bound = ModelCallGateway(remote(transport)).bind("root", purpose, lambda: None)
    assert getattr(bound, "supports_json_output") is False
    with pytest.raises(LLMError, match="not enabled"):
        getattr(bound, "chat_json")(MESSAGES)
    assert transport.requests == []


@pytest.mark.parametrize("capability,method", [(1, lambda _: LLMResponse()),
    ("true", lambda _: LLMResponse()), (True, None), (True, "not callable"), (False, None)])
def test_gateway_requires_literal_true_and_callable(capability: Any, method: Any) -> None:
    provider = SimpleNamespace(supports_json_output=capability, chat_json=method)
    bound = ModelCallGateway(provider).bind("root", "expression", lambda: None)
    assert getattr(bound, "supports_json_output") is False
    with pytest.raises(LLMError, match="not enabled"):
        getattr(bound, "chat_json")(MESSAGES)


def test_gateway_selects_expression_provider_and_supports_legacy_mocks() -> None:
    legacy = SimpleNamespace(chat=lambda *args, **kwargs: LLMResponse("legacy"), generate=lambda _: "legacy")
    native = remote(Transport())
    gateway = ModelCallGateway(legacy, providers={"expression": native})
    assert gateway.bind("root", "planning", lambda: None).chat(MESSAGES).content == "legacy"
    expression = gateway.bind("root", "expression", lambda: None)
    assert getattr(expression, "supports_json_output") is True
    assert getattr(expression, "chat_json")(MESSAGES).content == '{"reply_text":"ok"}'
    assert getattr(ModelCallGateway(legacy).bind("other", "expression", lambda: None), "supports_json_output") is False


@pytest.mark.parametrize("native_first", [True, False])
def test_chat_and_json_share_durable_root_budget_across_new_gateway(tmp_path: Path, native_first: bool) -> None:
    transport = Transport()
    provider = remote(transport)
    path = tmp_path / "budget.sqlite3"
    store = WorkflowStore(path)
    try:
        task = PersistentTaskManager(store).create("synthetic root")
        ledger = HumanizationStore(store)
        bound = ModelCallGateway(provider, ledger.reserve_call, ledger.finish_call).bind(task.id, "expression", lambda: None)
        if native_first:
            getattr(bound, "chat_json")(MESSAGES)
        else:
            bound.chat(MESSAGES)
    finally:
        store.close()
    store = WorkflowStore(path)
    try:
        ledger = HumanizationStore(store)
        rebound = ModelCallGateway(provider, ledger.reserve_call, ledger.finish_call).bind(task.id, "expression", lambda: None)
        with pytest.raises(WorkflowBlocked, match="budget exhausted"):
            if native_first:
                rebound.chat(MESSAGES)
            else:
                getattr(rebound, "chat_json")(MESSAGES)
        assert len(transport.requests) == 1
    finally:
        store.close()


def test_json_timeout_is_sanitized_consumes_budget_and_never_retries() -> None:
    transport = Transport()
    transport.timeout = True
    finishes: list[tuple[str, str]] = []
    gateway = ModelCallGateway(remote(transport), finish=lambda call, status: finishes.append((call, status)))
    bound = gateway.bind("root", "expression", lambda: None)
    with pytest.raises(LLMError, match="^LLM request timed out$"):
        getattr(bound, "chat_json")(MESSAGES)
    assert [status for _, status in finishes] == ["unknown"]
    with pytest.raises(LLMError, match="budget exhausted"):
        bound.chat(MESSAGES)
    assert len(transport.requests) == 1


class PrivacyFailure(RuntimeError):
    pass


@pytest.mark.parametrize("reject_at,expected_sends,expected_finishes", [
    (1, 0, []), (2, 0, ["unknown"]), (3, 1, ["unknown"]), (4, 1, ["finished"])])
def test_native_json_preserves_privacy_guard_before_and_after_send(
    reject_at: int, expected_sends: int, expected_finishes: list[str],
) -> None:
    transport = Transport()
    checks = 0
    finishes: list[str] = []
    failure = PrivacyFailure("synthetic privacy epoch changed")

    def guard() -> None:
        nonlocal checks
        checks += 1
        if checks == reject_at:
            raise failure

    gateway = ModelCallGateway(remote(transport), finish=lambda _, status: finishes.append(status))
    bound = gateway.bind("root", "expression", guard)
    with pytest.raises(PrivacyFailure) as caught:
        getattr(bound, "chat_json")(MESSAGES)
    assert caught.value is failure
    assert len(transport.requests) == expected_sends
    assert finishes == expected_finishes
    if reject_at > 1:
        with pytest.raises(LLMError, match="budget exhausted"):
            gateway.bind("root", "expression", lambda: None).chat(MESSAGES)
        assert len(transport.requests) == expected_sends


def test_native_json_128kib_serialized_boundary_and_tool_input_rejection() -> None:
    transport = Transport()
    gateway = ModelCallGateway(remote(transport))
    bound = gateway.bind("root", "expression", lambda: None)
    empty = ChatMessage("user", "")
    overhead = len(json.dumps({"messages": [asdict(empty)], "tools": []}, ensure_ascii=False).encode())
    with pytest.raises(LLMError, match="byte budget"):
        getattr(bound, "chat_json")([ChatMessage("user", "x" * (131073 - overhead))])
    for message in [ChatMessage("tool", "result"),
                    ChatMessage("assistant", "", tool_calls=(ToolCall("synthetic", {}, "call"),))]:
        with pytest.raises(LLMError, match="cannot use tools"):
            getattr(bound, "chat_json")([message])
    with pytest.raises(LLMError, match="cannot use tools"):
        bound.chat(MESSAGES, tools=[ToolSpec("synthetic", "", {"type": "object"})])
    assert transport.requests == []
    getattr(bound, "chat_json")([ChatMessage("user", "x" * (131072 - overhead))])
    assert len(transport.requests) == 1


def test_provider_native_json_rejects_tool_history_without_sending() -> None:
    transport = Transport()
    provider = remote(transport)
    calls = (ToolCall("synthetic", {}, "call"),)
    with pytest.raises(LLMError, match="cannot use tools"):
        provider.chat_json([ChatMessage("assistant", "", tool_calls=calls),
                            ChatMessage("tool", "result", tool_call_id="call")])
    assert transport.requests == []


def test_native_json_tool_output_is_rejected_without_retry() -> None:
    transport = Transport([{"type": "function_call", "call_id": "call", "name": "synthetic", "arguments": "{}"}])
    bound = ModelCallGateway(remote(transport)).bind("root", "expression", lambda: None)
    with pytest.raises(LLMError, match="forbidden tool calls"):
        getattr(bound, "chat_json")(MESSAGES)
    with pytest.raises(LLMError, match="budget exhausted"):
        bound.chat(MESSAGES)
    assert len(transport.requests) == 1


@pytest.mark.parametrize("result", [None, {}, '{"reply_text":"ok"}', LLMResponse(tool_calls=(ToolCall("synthetic"),))])
def test_gateway_native_json_validates_provider_response_dto(result: Any) -> None:
    sent: list[Sequence[ChatMessage]] = []

    def chat_json(messages: Sequence[ChatMessage]) -> Any:
        sent.append(messages)
        return result

    provider = SimpleNamespace(supports_json_output=True, chat_json=chat_json)
    bound = ModelCallGateway(provider).bind("root", "expression", lambda: None)
    with pytest.raises(LLMError, match="invalid response|forbidden tool calls"):
        getattr(bound, "chat_json")(MESSAGES)
    with pytest.raises(LLMError, match="budget exhausted"):
        getattr(bound, "chat_json")(MESSAGES)
    assert len(sent) == 1
