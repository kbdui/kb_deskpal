"""任务绑定的模型调用；规划有总预算，其余用途保持单次边界。"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
import json
from threading import RLock
from typing import TypeVar, cast
from uuid import uuid4
from time import monotonic

from deskpal.tools.models import ToolSpec

from .errors import LLMError
from .models import ChatMessage, LLMResponse
from .ports import LLMProvider
from .providers import OfflineLLMProvider, UnavailableLLMProvider
from .diagnostics import failure, sanitize


_T = TypeVar("_T")
_PURPOSES = frozenset({"context_understanding", "planning", "expression", "memory_evaluation"})


class ModelCallGateway:
    """The gateway has no mutable current task and never retries a Provider."""

    def __init__(self, provider: LLMProvider,
                 reserve: Callable[[str, str], str] | None = None,
                 finish: Callable[[str, str], None] | None = None, *,
                 providers: Mapping[str, LLMProvider] | None = None,
                 diagnose: Callable[[str, dict[str, object]], None] | None = None) -> None:
        self._provider = provider
        self.capability = ("offline_demo" if isinstance(provider, OfflineLLMProvider) else
                           "unavailable" if isinstance(provider, UnavailableLLMProvider) else
                           "local" if getattr(provider, "is_cloud", True) is False else "remote")
        self._reserve = reserve
        self._finish = finish
        self._diagnose = diagnose
        self._latest: dict[tuple[str, str], str] = {}
        self._providers = dict(providers or {})
        if not set(self._providers) <= _PURPOSES:
            raise ValueError("Unknown model purpose")
        self._lock = RLock()
        self._attempts: dict[tuple[str, str], str] = {}
        self._outcomes: dict[str, str] = {}

    def bind(self, task_id: str, purpose: str, guard: Callable[[], None]) -> LLMProvider:
        if not task_id or purpose not in _PURPOSES:
            raise ValueError("A model client requires an explicit task and known purpose")
        return _BoundProvider(self, task_id, purpose, guard)

    def _reserve_attempt(self, task_id: str, purpose: str) -> str:
        if self._reserve is not None:
            return self._reserve(task_id, purpose)
        with self._lock:
            key = (task_id, purpose)
            count = sum(1 for task, item in self._attempts if task == task_id and item.split(":")[0] == purpose)
            if count >= (24 if purpose == "planning" else 3 if purpose == "memory_evaluation" else 1):
                raise LLMError("Model call budget exhausted")
            call_id = str(uuid4())
            self._attempts[(task_id, purpose + ":" + str(count))] = call_id
            self._outcomes[call_id] = "reserved"
            return call_id

    def _finish_attempt(self, call_id: str, status: str) -> None:
        if self._finish is not None:
            self._finish(call_id, status)
        else:
            with self._lock:
                self._outcomes[call_id] = status

    def _invoke(self, task_id: str, purpose: str, guard: Callable[[], None],
                operation: Callable[[], _T], *, request_bytes: int = 0) -> _T:
        # A reservation remains consumed even when the following guard rejects,
        # the send fails or the caller cannot know whether bytes were written.
        guard()
        call_id = self._reserve_attempt(task_id, purpose)
        with self._lock:
            self._latest[(task_id, purpose)] = call_id
        started = monotonic()
        try:
            guard()
            result = operation()
            guard()
        except BaseException as error:
            self.annotate(task_id, purpose, {**failure(error), 'request_bytes': request_bytes,
                'elapsed_ms': int((monotonic() - started) * 1000)})
            try:
                self._finish_attempt(call_id, "unknown")
            except Exception:
                # A reserved row still consumes its slot. Preserve a privacy
                # guard failure rather than replacing it with bookkeeping I/O.
                pass
            raise
        self._finish_attempt(call_id, "finished")
        details: dict[str, object] = dict(error_code='ok', request_bytes=request_bytes,
            elapsed_ms=int((monotonic() - started) * 1000))
        if isinstance(result, LLMResponse):
            details.update(response_bytes=len(result.content.encode('utf-8')), request_id=result.request_id)
            if result.usage is not None:
                details.update({key: value for key, value in asdict(result.usage).items() if value is not None})
        self.annotate(task_id, purpose, details)
        # Privacy failures are deliberately propagated as their original type.
        guard()
        return result

    def annotate(self, task_id: str, purpose: str, values: dict[str, object]) -> None:
        with self._lock:
            call_id = self._latest.get((task_id, purpose))
        if call_id and self._diagnose is not None:
            # 诊断失败不改变工具结果或触发模型重发；生命周期清理仍由原边界处理。
            try:
                self._diagnose(call_id, sanitize(values))
            except Exception:
                pass


class _BoundProvider:
    def __init__(self, gateway: ModelCallGateway, task_id: str, purpose: str,
                 guard: Callable[[], None]) -> None:
        self._gateway = gateway
        self._task_id = task_id
        self._purpose = purpose
        self._guard = guard

    @property
    def supports_json_output(self) -> bool:
        provider = self._gateway._providers.get(self._purpose, self._gateway._provider)
        return (self._purpose == "expression" and getattr(provider, "supports_json_output", False) is True
                and callable(getattr(provider, "chat_json", None)))

    def chat_json(self, messages: Sequence[ChatMessage]) -> LLMResponse:
        if not self.supports_json_output:
            raise LLMError("Native JSON output is not enabled for this model purpose")
        return self._chat(messages, tools=(), json_output=True)

    def chat(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] = ()) -> LLMResponse:
        return self._chat(messages, tools=tools, json_output=False)

    def _chat(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec], json_output: bool) -> LLMResponse:
        if self._purpose != "planning" and (tools or any(message.tool_calls or message.role == "tool" for message in messages)):
            raise LLMError("This model purpose cannot use tools")
        allowed_tools = tools if self._purpose == "planning" else ()
        encoded = json.dumps({"messages": [asdict(message) for message in messages],
                              "tools": [asdict(tool) for tool in allowed_tools]},
                             ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 131072:
            raise LLMError("Model input exceeds byte budget")
        provider = self._gateway._providers.get(self._purpose, self._gateway._provider)
        def operation() -> LLMResponse:
            response: object
            if json_output:
                # Optional method: the base Protocol and old mocks need not
                # implement it. Validate both capability and returned DTO.
                method = getattr(provider, "chat_json", None)
                if self._purpose != "expression" or getattr(provider, "supports_json_output", False) is not True or not callable(method):
                    raise LLMError("Native JSON output is not enabled for this model purpose")
                response = cast(Callable[[Sequence[ChatMessage]], object], method)(messages)
            else:
                response = provider.chat(messages, tools=allowed_tools)
            if not isinstance(response, LLMResponse):
                raise LLMError("Model returned an invalid response")
            return response
        response = self._gateway._invoke(self._task_id, self._purpose, self._guard,
            operation, request_bytes=len(encoded.encode('utf-8')))
        if self._purpose != "planning" and response.tool_calls:
            raise LLMError("This model purpose returned forbidden tool calls")
        return response

    def generate(self, prompt: str) -> str:
        if not isinstance(prompt, str) or len(prompt.encode("utf-8")) > 131072:
            raise LLMError("Model input exceeds byte budget")
        provider = self._gateway._providers.get(self._purpose, self._gateway._provider)
        return self._gateway._invoke(self._task_id, self._purpose, self._guard,
            lambda: provider.generate(prompt), request_bytes=len(prompt.encode('utf-8')))
