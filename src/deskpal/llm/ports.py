from typing import Protocol, Sequence

from deskpal.tools.models import ToolSpec

from .models import ChatMessage, LLMResponse


class LLMProvider(Protocol):
    def chat(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] = ()) -> LLMResponse: ...

    def generate(self, prompt: str) -> str: ...


class NativeJSONProvider(LLMProvider, Protocol):
    """Optional capability; existing Providers need only chat and generate."""

    @property
    def supports_json_output(self) -> bool: ...

    def chat_json(self, messages: Sequence[ChatMessage]) -> LLMResponse: ...

