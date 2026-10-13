"""Trusted tools must only be invoked through the manager and sandbox."""

from typing import Any, Mapping, Protocol, Sequence

from .models import ToolCall, ToolContext, ToolResult, ToolSpec


class Tool(Protocol):
    @property
    def spec(self) -> ToolSpec: ...

    def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult: ...


class ToolManager(Protocol):
    def register(self, tool: Tool) -> None: ...

    def list_tools(self) -> Sequence[ToolSpec]: ...

    def check_permission(self, call: ToolCall, context: ToolContext) -> None:
        """Raise PermissionDenied or ToolNotFound before invoking any tool."""
        ...

    def execute(self, call: ToolCall, context: ToolContext) -> ToolResult: ...

