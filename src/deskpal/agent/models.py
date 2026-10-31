"""Agent 对外返回的数据结构；只描述结果，不负责执行或持久化。"""

from dataclasses import dataclass

from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import PermissionLevel, ToolCall, ToolResult
from typing import Any, Mapping


@dataclass(frozen=True)
class AuthorizationRequest:
    """需要用户确认的单次工具调用预览，不包含可变的内部计划对象。"""

    name: str
    arguments: Mapping[str, Any]
    call_id: str
    permission_level: PermissionLevel


@dataclass(frozen=True)
class AgentResult:
    """一次 Agent 请求的最终状态、回复及可供界面展示的执行信息。"""

    task_id: str
    status: TaskStatus
    response: str
    tool_results: tuple[ToolResult, ...] = ()
    required_authorizations: tuple[str, ...] = ()
    error: str | None = None
    required_high_risk_authorizations: tuple[str, ...] = ()
    authorization_requests: tuple[AuthorizationRequest, ...] = ()
    planned_calls: tuple[ToolCall, ...] = ()
    warnings: tuple[str, ...] = ()
    memory_effects: tuple[str, ...] = ()

