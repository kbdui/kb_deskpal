from dataclasses import dataclass
from typing import Any

from deskpal.tools.models import ToolCall


@dataclass(frozen=True)
class Plan:
    reply: str = ""
    steps: tuple[ToolCall, ...] = ()
    action: str = "answer"
    context: dict[str, Any] | None = None

