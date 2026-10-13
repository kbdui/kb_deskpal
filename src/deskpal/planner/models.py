from dataclasses import dataclass

from deskpal.tools.models import ToolCall


@dataclass(frozen=True)
class Plan:
    reply: str = ""
    steps: tuple[ToolCall, ...] = ()

