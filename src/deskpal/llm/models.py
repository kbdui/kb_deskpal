from dataclasses import dataclass
from typing import Literal

from deskpal.tools.models import ToolCall


@dataclass(frozen=True)
class LLMUsage:
    """服务端实际用量；未知保持为空，绝不用字节数冒充 token 数。"""

    input_tokens: int | None = None
    cached_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class LLMResponse:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    provider: str = ""
    model: str = ""
    usage: LLMUsage | None = None
    request_id: str = ""

