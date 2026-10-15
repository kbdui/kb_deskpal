from dataclasses import dataclass

from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import PermissionLevel, ToolCall, ToolResult
from typing import Any, Mapping


@dataclass(frozen=True)
class AuthorizationRequest:
    """Concrete call preview; its data is a copy of the private pending plan."""

    name: str
    arguments: Mapping[str, Any]
    call_id: str
    permission_level: PermissionLevel


@dataclass(frozen=True)
class AgentResult:
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

