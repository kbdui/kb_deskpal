"""The four trusted V0.1 tools; all filesystem I/O uses one sandbox policy."""

from datetime import datetime, timezone
from typing import Any, Mapping

from deskpal.sandbox.workspace import WorkspaceSandbox

from .models import PermissionLevel, ToolContext, ToolResult, ToolSpec


class EchoTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("echo", "原样返回文本。", {
            "type": "object", "properties": {"text": {"type": "string"}},
            "required": ["text"], "additionalProperties": False,
        })

    def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(True, output=arguments["text"])


class TimeTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("time", "返回当前 UTC 时间（ISO 8601）。", {
            "type": "object", "properties": {}, "additionalProperties": False,
        })

    def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(True, output=datetime.now(timezone.utc).isoformat())


class ReadTextTool:
    def __init__(self, sandbox: WorkspaceSandbox) -> None:
        self._sandbox = sandbox

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("read_text", "读取工作区内的 UTF-8 文件。", {
            "type": "object", "properties": {"path": {"type": "string"}},
            "required": ["path"], "additionalProperties": False,
        })

    def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(True, output=self._sandbox.read_text(arguments["path"]))


class WriteTextTool:
    def __init__(self, sandbox: WorkspaceSandbox) -> None:
        self._sandbox = sandbox

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("write_text", "写入工作区 UTF-8 文件；默认不覆盖，覆盖需显式设置 overwrite。", {
            "type": "object", "properties": {
                "path": {"type": "string"}, "text": {"type": "string"},
                "overwrite": {"type": "boolean", "default": False},
            }, "required": ["path", "text"], "additionalProperties": False,
        }, PermissionLevel.CONFIRM)

    def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        output = self._sandbox.write_text(arguments["path"], arguments["text"],
                                          overwrite=arguments.get("overwrite", False))
        return ToolResult(True, output=output)
