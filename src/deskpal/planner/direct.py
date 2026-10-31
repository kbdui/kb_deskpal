"""Deterministic, explicitly requested tool calls for the CLI and embedding."""

from collections.abc import Sequence, Mapping
from deskpal.llm.ports import LLMProvider

from deskpal._json import snapshot_json
from deskpal.memory.models import MemoryRecord
from deskpal.tools.models import ToolCall, ToolSpec

from .models import Plan


class DirectToolPlanner:
    """Propose one trusted tool call; permissions and validation stay in Manager."""

    def __init__(self, call: ToolCall) -> None:
        self._call = ToolCall(call.name, snapshot_json(call.arguments), call.call_id)

    def plan(self, request: str, *, memories: Sequence[MemoryRecord] = (),
             tools: Sequence[ToolSpec] = ()) -> Plan:
        return Plan(steps=(ToolCall(self._call.name, snapshot_json(self._call.arguments), self._call.call_id),))

    def plan_context(self, request: str, *, memories: Sequence[MemoryRecord] = (),
                     tools: Sequence[ToolSpec] = (), references: Sequence[Mapping[str, object]] = (),
                     llm: LLMProvider) -> Plan:
        """显式工具 API 已确定目标；仍由 Core 保存上下文、检查权限和来源。"""
        return self.plan(request, memories=memories, tools=tools)
