"""手机与桌面共用业务实现；远程入口只允许这里明确列出的操作。"""

from collections.abc import Mapping
from typing import Any, Protocol


class CommandPort(Protocol):
    def execute(self, operation: str, parameters: Mapping[str, Any]) -> dict[str, Any]: ...


# 字段白名单还承担边界约束：手机不能在普通提交时偷偷附带工具授权。
FIELDS = {
    "conversation_list": {"cursor", "limit", "archived"},
    "conversation_create": {"title", "command_id"},
    "conversation_timeline": {"id", "before", "after", "limit", "known_revision"},
    "conversation_item": {"id", "item_id", "offset", "limit"},
    "submit": {"request", "command_id", "conversation_id", "expected_conversation_epoch", "expected_context_epoch"},
    "command_status": {"command_id"},
    "task_updates": {"task_ids", "known_revisions"},
    "task_review": {"task_id"},
    "task": {"task_id"},
    "confirm": {"task_id", "command_id", "expected_revision", "plan_hash", "ordinary_tools", "high_risk_tools"},
    "task_pause": {"task_id", "command_id"},
    "task_continue": {"task_id", "command_id"},
    "task_append_requirement": {"task_id", "content", "command_id"},
    "task_answer": {"task_id", "content", "command_id"},
    "cancel": {"task_id", "command_id"},
    "reject_tools": {"task_id", "command_id", "plan_hash", "expected_revision"},
}
WRITES = frozenset({"conversation_create", "submit", "confirm", "task_pause", "task_continue",
                    "task_append_requirement", "task_answer", "cancel", "reject_tools"})


class ClientGateway:
    """复用现有 JSON facade，不复制任务执行、授权和数据库访问逻辑。"""

    def __init__(self, port: CommandPort) -> None:
        self.port = port

    def execute(self, operation: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
        if operation not in FIELDS or not isinstance(parameters, Mapping) or set(parameters) - FIELDS[operation]:
            raise ValueError("Unsupported mobile operation or fields")
        if operation in WRITES:
            command = parameters.get("command_id")
            if not isinstance(command, str) or not 1 <= len(command) <= 200:
                raise ValueError("Stable command identity required")
        if operation == "submit":
            if not isinstance(parameters.get("conversation_id"), str) or type(parameters.get("expected_conversation_epoch")) is not int:
                raise ValueError("Explicit conversation and epoch required")
        result = self.port.execute(operation, parameters)
        if operation == "task":
            from .tool_catalog import display_name
            for attempt in result.get("attempts", []):
                tool = (attempt.get("result") or {}).get("tool_name", "")
                attempt["display_name"] = display_name(tool) if tool else "工具操作"
        return result
