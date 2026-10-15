"""Synchronous demo and Responses API adapters, without third-party dependencies.

An injected transport follows ``urlopen(request, timeout=seconds)`` and returns a
readable response, optionally a context manager. Such transports are trusted to
preserve the default transport's prohibition on credential-bearing redirects.
"""

import ipaddress
import json
import math
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from typing import Any

from deskpal.config import LLMConfig
from deskpal.tools.models import ToolCall, ToolSpec

from .errors import LLMError
from .models import ChatMessage, LLMResponse


def _json_value(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json_value(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _json_value(item)
        return
    raise LLMError("LLM data must contain only finite JSON values")


def _json_dump(value: Any) -> str:
    try:
        _json_value(value)
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise LLMError("LLM data is not valid JSON") from None


def _json_load(value: str | bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    def invalid_constant(_: str) -> Any:
        raise ValueError("non-finite JSON number")

    try:
        result = json.loads(value, object_pairs_hook=pairs, parse_constant=invalid_constant)
        _json_value(result)
        return result
    except (TypeError, ValueError, UnicodeError, RecursionError, LLMError):
        raise LLMError("LLM response contains invalid JSON") from None


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_call(call: ToolCall) -> None:
    if not isinstance(call, ToolCall) or not _nonempty(call.name) or not _nonempty(call.call_id):
        raise LLMError("Tool calls require a name and a nonempty call_id")
    if not isinstance(call.arguments, Mapping):
        raise LLMError("Tool call arguments must be a JSON object")
    _json_dump(dict(call.arguments))


def _validate_inputs(messages: Sequence[ChatMessage], tools: Sequence[ToolSpec]) -> None:
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)) or not messages:
        raise LLMError("chat requires a nonempty sequence of messages")
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
        raise LLMError("tools must be a sequence of ToolSpec objects")
    names: set[str] = set()
    for spec in tools:
        if not isinstance(spec, ToolSpec) or not _nonempty(spec.name) or not isinstance(spec.description, str):
            raise LLMError("Invalid tool specification")
        if spec.name in names:
            raise LLMError("Duplicate tool names are not allowed")
        names.add(spec.name)
        if not isinstance(spec.parameters, Mapping) or spec.parameters.get("type") != "object":
            raise LLMError("Tool parameters must use a JSON Schema object")
        _json_dump(dict(spec.parameters))
    calls: set[str] = set()
    results: set[str] = set()
    for message in messages:
        if (not isinstance(message, ChatMessage) or not isinstance(message.role, str)
                or message.role not in {"system", "user", "assistant", "tool"}):
            raise LLMError("Invalid chat message role")
        if not isinstance(message.content, str) or not isinstance(message.tool_calls, (list, tuple)):
            raise LLMError("Invalid chat message content or tool_calls")
        if message.tool_calls and message.role != "assistant":
            raise LLMError("Only assistant messages may contain tool calls")
        if message.role == "tool":
            if not _nonempty(message.tool_call_id) or message.tool_call_id not in calls:
                raise LLMError("Tool results require a preceding matching call_id")
            if message.tool_call_id in results:
                raise LLMError("Duplicate tool results are not allowed")
            results.add(message.tool_call_id)
        elif message.tool_call_id is not None:
            raise LLMError("Only tool messages may contain tool_call_id")
        for call in message.tool_calls:
            _validate_call(call)
            if call.call_id in calls:
                raise LLMError("Duplicate tool call_id values are not allowed")
            calls.add(call.call_id)


class OfflineLLMProvider:
    """Deterministic command demo; this provider does not understand natural language.

    Commands: ``echo text``, ``time``, ``read path``, ``write path text``.
    A path containing spaces may be double-quoted. The exact acceptance phrase
    ``帮我执行一个任务`` selects echo. Commands only propose registered tools.
    """

    def __init__(self, model: str = "offline-demo") -> None:
        if not _nonempty(model):
            raise LLMError("Offline model label must not be empty")
        self.model = model

    def generate(self, prompt: str) -> str:
        return self.chat((ChatMessage("user", prompt),)).content

    def chat(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] = ()) -> LLMResponse:
        _validate_inputs(messages, tools)
        latest_user = max((index for index, message in enumerate(messages) if message.role == "user"), default=-1)
        reflect = any(message.role == "system" and "[REFLECT]" in message.content for message in messages)
        if reflect:
            outputs = [message.content for message in messages if message.role == "tool"]
            if not outputs and latest_user >= 0:
                outputs = [messages[latest_user].content]
            return self._response("[离线演示] 执行反馈：\n" + "\n".join(outputs))
        # Only tool feedback after the latest user turn triggers reflection;
        # earlier completed tools must not hide a new user request.
        outputs = [message.content for index, message in enumerate(messages)
                   if index > latest_user and message.role == "tool"]
        if not tools and outputs:
            return self._response("[离线演示] 工具结果：\n" + "\n".join(outputs))
        prompt = next((message.content.strip() for message in reversed(messages) if message.role == "user"), "")
        if not prompt:
            raise LLMError("Offline chat requires a nonempty user message")
        name = ""
        arguments: dict[str, Any] = {}
        if prompt == "帮我执行一个任务":
            name, arguments = "echo", {"text": "离线演示任务已执行"}
        elif prompt == "time":
            name = "time"
        elif prompt.startswith("echo "):
            name, arguments = "echo", {"text": prompt[5:]}
        elif prompt.startswith("tool "):
            parts = prompt.split(maxsplit=2)
            try:
                parsed = _json_load(parts[2]) if len(parts) == 3 else {}
                if not isinstance(parsed, dict) or len(parts) < 2:
                    raise LLMError("Tool arguments must be an object")
                name, arguments = parts[1], parsed
            except LLMError:
                return self._response('[离线演示] 命令格式：tool 工具名 {"参数名":"值"}，参数须为有限JSON对象。')
        elif prompt.startswith(("read ", "write ")):
            match = re.fullmatch(r'(read|write)\s+(?:"([^"\n]+)"|(\S+))(?:\s+([\s\S]+))?', prompt)
            if match and match[1] == "read" and match[4] is None:
                name, arguments = "read_text", {"path": match[2] or match[3]}
            elif match and match[1] == "write" and match[4] is not None:
                name, arguments = "write_text", {"path": match[2] or match[3], "text": match[4]}
            else:
                return self._response('[离线演示] 命令格式：read 路径 或 write 路径 文本；含空格的路径请加双引号。')
        if name:
            if name not in {spec.name for spec in tools}:
                return self._response(f"[离线演示] 当前未提供 {name} 工具，未执行命令。")
            existing_ids = {call.call_id for message in messages for call in message.tool_calls}
            index = 1
            while f"offline_call_{index}" in existing_ids:
                index += 1
            call = ToolCall(name=name, arguments=arguments, call_id=f"offline_call_{index}")
            return self._response(f"[离线演示] 已生成 {name} 工具计划。", (call,))
        return self._response("[离线演示] 这是固定规则演示，不具备真实自然语言理解。输入 echo 文本、time、read 路径、write 路径 文本可演示工具；你说：" + prompt)

    def _response(self, content: str, calls: tuple[ToolCall, ...] = ()) -> LLMResponse:
        return LLMResponse(content, calls, "offline", self.model)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _default_transport(request: urllib.request.Request, *, timeout: float) -> Any:
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


class OpenAILLMProvider:
    """Responses adapter. Credentials are provided explicitly, never read here.

    Reasoning output items are discarded. Unknown output/content types, refusal,
    incomplete responses, malformed or duplicate calls fail closed with LLMError.
    The default urllib opener rejects all redirects before following them.

    Reasoning-model native multi-turn tool continuation is not supported: it
    requires retaining reasoning items, which our DTO deliberately excludes.
    V0.1 Core reflects through an independent request containing ordinary text
    tool results. Native call/output history mapping remains available for
    models that do not require reasoning-item continuation.
    """

    def __init__(
        self,
        config: LLMConfig | None = None,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        max_output_tokens: int | None = None,
        transport: Callable[..., Any] | None = None,
    ) -> None:
        if config is not None and not isinstance(config, LLMConfig):
            raise LLMError("config must be an LLMConfig")
        values = config or LLMConfig()
        self.model = values.model if model is None else model
        self._api_key = values.api_key if api_key is None else api_key
        selected_url = values.base_url if base_url is None else base_url
        self.timeout_seconds = values.timeout_seconds if timeout_seconds is None else timeout_seconds
        if max_output_tokens is not None and (type(max_output_tokens) is not int or max_output_tokens <= 0):
            raise LLMError("max_output_tokens must be a positive integer")
        self.max_output_tokens = max_output_tokens
        if not _nonempty(self.model) or not _nonempty(self._api_key):
            raise LLMError("OpenAI provider requires a model and an API key")
        if any(char in self._api_key for char in "\r\n"):
            raise LLMError("Invalid API credential format")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)):
            raise LLMError("timeout_seconds must be a positive finite number")
        try:
            if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
                raise ValueError
        except (ValueError, OverflowError):
            raise LLMError("timeout_seconds must be a positive finite number") from None
        if transport is not None and not callable(transport):
            raise LLMError("transport must be callable")
        self._transport = transport or _default_transport
        self._url = self._validate_url(selected_url) + "/responses"

    @staticmethod
    def _validate_url(value: Any) -> str:
        try:
            if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
                raise ValueError
            parsed = urllib.parse.urlsplit(value)
            if not parsed.hostname or parsed.username is not None or parsed.password is not None:
                raise ValueError
            if parsed.query or parsed.fragment or "%" in parsed.netloc or "\\" in value:
                raise ValueError
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError
            if parsed.scheme == "http":
                try:
                    loopback = ipaddress.ip_address(parsed.hostname).is_loopback
                except ValueError:
                    loopback = parsed.hostname.lower() == "localhost"
                if not loopback:
                    raise ValueError
            elif parsed.scheme != "https":
                raise ValueError
            return value.rstrip("/")
        except (ValueError, TypeError):
            raise LLMError("base_url must be HTTPS (or loopback HTTP), without credentials, query or fragment") from None

    def generate(self, prompt: str) -> str:
        if not _nonempty(prompt):
            raise LLMError("generate requires a nonempty prompt")
        response = self.chat((ChatMessage("user", prompt),))
        if response.tool_calls or not response.content.strip():
            raise LLMError("generate requires a text response without tool calls")
        return response.content

    def chat(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] = ()) -> LLMResponse:
        _validate_inputs(messages, tools)
        inputs: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "tool":
                inputs.append({"type": "function_call_output", "call_id": message.tool_call_id, "output": message.content})
                continue
            if message.content or not message.tool_calls:
                inputs.append({"role": message.role, "content": message.content})
            for call in message.tool_calls:
                inputs.append({"type": "function_call", "call_id": call.call_id, "name": call.name,
                               "arguments": _json_dump(dict(call.arguments))})
        payload: dict[str, Any] = {"model": self.model, "store": False, "input": inputs}
        if self.max_output_tokens is not None:
            payload["max_output_tokens"] = self.max_output_tokens
        if tools:
            payload["tools"] = [{"type": "function", "name": spec.name, "description": spec.description,
                                 "parameters": dict(spec.parameters), "strict": False} for spec in tools]
        raw = self._request(_json_dump(payload).encode("utf-8"))
        return self._parse_response(raw)

    def _request(self, data: bytes) -> Any:
        try:
            request = urllib.request.Request(self._url, data=data, method="POST", headers={
                "Authorization": "Bearer " + self._api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
            response = self._transport(request, timeout=self.timeout_seconds)
            context = response if hasattr(response, "__enter__") else nullcontext(response)
            with context as stream:
                status = getattr(stream, "status", None)
                if status is None and hasattr(stream, "getcode"):
                    status = stream.getcode()
                if status is not None and (not isinstance(status, int) or not 200 <= status < 300):
                    raise LLMError("LLM HTTP request failed")
                body = stream.read(4 * 1024 * 1024 + 1)
                if not isinstance(body, (bytes, str)) or len(body) > 4 * 1024 * 1024:
                    raise LLMError("LLM response is invalid or exceeds the size limit")
            return _json_load(body)
        except LLMError:
            # Even an injected transport's LLMError message is untrusted.
            raise LLMError("LLM response processing failed") from None
        except urllib.error.HTTPError:
            raise LLMError("LLM HTTP request failed") from None
        except (TimeoutError, socket.timeout):
            raise LLMError("LLM request timed out") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise LLMError("LLM request timed out") from None
            raise LLMError("LLM network request failed") from None
        except Exception:
            # A trusted injected transport may use its own exception types.
            raise LLMError("LLM request failed") from None

    def _parse_response(self, raw: Any) -> LLMResponse:
        if not isinstance(raw, dict) or not isinstance(raw.get("output"), list):
            raise LLMError("Malformed LLM response")
        if (raw.get("error") is not None or raw.get("incomplete_details") is not None
                or raw.get("status", "completed") != "completed"):
            raise LLMError("LLM response failed or is incomplete")
        texts: list[str] = []
        calls: list[ToolCall] = []
        ids: set[str] = set()
        for item in raw["output"]:
            if not isinstance(item, dict):
                raise LLMError("Malformed LLM output item")
            kind = item.get("type")
            if kind == "reasoning":
                continue
            if item.get("status", "completed") != "completed":
                raise LLMError("LLM output is incomplete")
            if kind == "message":
                if item.get("role", "assistant") != "assistant" or not isinstance(item.get("content"), list):
                    raise LLMError("Malformed LLM message output")
                for part in item["content"]:
                    if not isinstance(part, dict):
                        raise LLMError("Malformed LLM message content")
                    if part.get("type") == "refusal":
                        raise LLMError("LLM refused the request")
                    if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                        raise LLMError("Unsupported LLM message content type")
                    texts.append(part["text"])
            elif kind == "function_call":
                if not _nonempty(item.get("call_id")) or not _nonempty(item.get("name")):
                    raise LLMError("LLM tool call requires a name and call_id")
                if item["call_id"] in ids:
                    raise LLMError("Duplicate LLM tool call_id")
                ids.add(item["call_id"])
                if not isinstance(item.get("arguments"), str):
                    raise LLMError("LLM tool arguments must be JSON text")
                arguments = _json_load(item["arguments"])
                if not isinstance(arguments, dict):
                    raise LLMError("LLM tool arguments must be a JSON object")
                calls.append(ToolCall(item["name"], arguments, item["call_id"]))
            else:
                raise LLMError("Unsupported LLM output type")
        content = "\n".join(texts)
        if not content.strip() and not calls:
            raise LLMError("LLM returned no usable output")
        return LLMResponse(content, tuple(calls), "openai", self.model)


__all__ = ["OfflineLLMProvider", "OpenAILLMProvider"]
