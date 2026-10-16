"""Registration, audit, permission gating and dispatch of trusted tools."""

from dataclasses import dataclass
from typing import Any, Mapping

from deskpal.execution_log import ExecutionLog, NullExecutionLog
from deskpal.sandbox.errors import PathViolation, PermissionDenied, ToolNotFound
from deskpal.sandbox.workspace import WorkspaceSandbox

from ._schema import ArgumentError, freeze_json, snapshot_json, thaw_json, validate_arguments, validate_schema
from .builtin import EchoTool, ReadTextTool, TimeTool, WriteTextTool
from .models import PermissionLevel, ToolCall, ToolContext, ToolResult, ToolSpec
from .ports import Tool


@dataclass(frozen=True)
class _Registered:
    tool: Tool
    spec: ToolSpec


def _copy_spec(spec: ToolSpec) -> ToolSpec:
    return ToolSpec(spec.name, spec.description, thaw_json(spec.parameters), spec.permission_level,
                    spec.contract_version)


def _execution_error(exc: Exception) -> str:
    """Expose business categories, never arbitrary plugin exception text."""
    if isinstance(exc, ArgumentError):
        return str(exc)
    if isinstance(exc, PathViolation):
        return "File path violates the workspace policy"
    if isinstance(exc, FileNotFoundError):
        return "Workspace file does not exist"
    if isinstance(exc, FileExistsError):
        return "Workspace file already exists; explicit overwrite is required"
    if isinstance(exc, UnicodeError):
        return "File text must be valid UTF-8"
    if isinstance(exc, ValueError):
        return "Invalid tool value or configured file byte limit exceeded"
    if isinstance(exc, OSError):
        return "Workspace file operation failed"
    return "Tool execution failed"


class BuiltinToolManager:
    """Execute trusted tools only; registration metadata is immutable internally."""

    def __init__(self, sandbox: WorkspaceSandbox, *, execution_log: ExecutionLog | None = None) -> None:
        self._sandbox = sandbox
        self._log = execution_log if execution_log is not None else NullExecutionLog()
        self._tools: dict[str, _Registered] = {}

    def register(self, tool: Tool) -> None:
        source = tool.spec
        if not isinstance(source.name, str) or not source.name.strip():
            raise ValueError("Tool name must be a non-empty string")
        if source.name in self._tools:
            raise ValueError(f"Tool {source.name} is already registered")
        if not isinstance(source.description, str):
            raise ValueError("Tool description must be a string")
        if not isinstance(source.contract_version, str) or not source.contract_version.strip():
            raise ValueError("Tool contract version must be a non-empty string")
        if type(source.permission_level) not in (int, PermissionLevel):
            raise ValueError("Tool permission level must be 0, 1 or 2")
        try:
            permission = PermissionLevel(source.permission_level)
        except ValueError:
            raise ValueError("Tool permission level must be 0, 1 or 2") from None
        parameters = snapshot_json(source.parameters)
        if not isinstance(parameters, Mapping):
            raise ValueError("Tool parameter schema must be an object")
        validate_schema(parameters)
        spec = ToolSpec(source.name, source.description, freeze_json(parameters), permission,
                        source.contract_version)
        self._log.record("tool_registered", data={"tool_name": spec.name, "permission_level": int(permission)})
        self._tools[spec.name] = _Registered(tool, spec)

    def register_builtin(self) -> None:
        """Register echo, time, read_text and write_text on this sandbox."""
        # Check all names before mutation so a second call cannot partly register.
        if any(name in self._tools for name in ("echo", "time", "read_text", "write_text")):
            raise ValueError("One or more builtin tools are already registered")
        tools: tuple[Tool, ...] = (EchoTool(), TimeTool(), ReadTextTool(self._sandbox), WriteTextTool(self._sandbox))
        for tool in tools:
            self.register(tool)

    def list_tools(self) -> tuple[ToolSpec, ...]:
        return tuple(_copy_spec(entry.spec) for entry in self._tools.values())

    def _lookup(self, call: ToolCall, context: ToolContext) -> _Registered:
        entry = self._tools.get(call.name)
        if entry is None:
            self._log.record("tool_failed", context.task_id,
                             {"tool_name": call.name, "call_id": call.call_id, "error": "Unknown tool"})
            raise ToolNotFound(f"Tool {call.name} is not registered")
        return entry

    def _permission(self, entry: _Registered, call: ToolCall, context: ToolContext) -> None:
        try:
            self._sandbox.check_permission(_copy_spec(entry.spec), context)
        except (PermissionDenied, PathViolation) as exc:
            self._log.record("permission_checked", context.task_id,
                             {"tool_name": entry.spec.name, "call_id": call.call_id, "allowed": False})
            self._log.record("tool_failed", context.task_id,
                             {"tool_name": entry.spec.name, "call_id": call.call_id,
                              "error": "Explicit authorization required" if isinstance(exc, PermissionDenied)
                              else "Invalid workspace context"})
            raise
        self._log.record("permission_checked", context.task_id,
                         {"tool_name": entry.spec.name, "call_id": call.call_id, "allowed": True})

    def check_permission(self, call: ToolCall, context: ToolContext) -> None:
        self._permission(self._lookup(call, context), call, context)

    def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        argument_error: str | None = None
        arguments: Any = None
        try:
            # Snapshot at the call boundary, before sandbox/audit callbacks run.
            arguments = snapshot_json(call.arguments)
        except Exception as exc:
            argument_error = _execution_error(exc)
        entry = self._lookup(call, context)
        self._permission(entry, call, context)
        if argument_error is not None:
            return self._failed(call, context, argument_error)
        try:
            validate_arguments(arguments, entry.spec.parameters)
        except Exception as exc:
            return self._failed(call, context, _execution_error(exc))
        self._log.record("tool_started", context.task_id,
                         {"tool_name": entry.spec.name, "call_id": call.call_id, "arguments": snapshot_json(arguments)})
        try:
            result = entry.tool.execute(arguments, context)
            if not isinstance(result, ToolResult) or type(result.success) is not bool:
                raise ArgumentError("Tool returned an invalid result")
            if result.success:
                output = snapshot_json(result.output)
                normalized = ToolResult(True, output=output, tool_name=entry.spec.name, call_id=call.call_id)
            else:
                if result.error is not None and not isinstance(result.error, str):
                    raise ArgumentError("Tool returned an invalid error")
                normalized = ToolResult(False, output=snapshot_json(result.output),
                                        error=result.error or "Tool execution failed",
                                        tool_name=entry.spec.name, call_id=call.call_id)
        except (PathViolation, PermissionDenied) as exc:
            self._log.record("tool_failed", context.task_id,
                             {"tool_name": entry.spec.name, "call_id": call.call_id,
                              "error": "File path violates the workspace policy" if isinstance(exc, PathViolation)
                              else "Explicit authorization required"})
            raise
        except Exception as exc:
            return self._failed(call, context, _execution_error(exc))
        # Audit writes sit outside tool error handling: log failures must propagate.
        if not normalized.success:
            self._log.record("tool_failed", context.task_id, self._result_data(normalized))
        else:
            self._log.record("tool_completed", context.task_id, self._result_data(normalized))
        return normalized

    @staticmethod
    def _result_data(result: ToolResult) -> dict[str, Any]:
        return {"tool_name": result.tool_name, "call_id": result.call_id,
                "success": result.success, "output": snapshot_json(result.output), "error": result.error}

    def _failed(self, call: ToolCall, context: ToolContext, error: str) -> ToolResult:
        result = ToolResult(False, error=error, tool_name=call.name, call_id=call.call_id)
        self._log.record("tool_failed", context.task_id, self._result_data(result))
        return result
