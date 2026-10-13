from typing import Protocol, Sequence

from deskpal.tools.models import ToolSpec

from .models import ChatMessage, LLMResponse


class LLMProvider(Protocol):
    def chat(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] = ()) -> LLMResponse: ...

    def generate(self, prompt: str) -> str: ...

