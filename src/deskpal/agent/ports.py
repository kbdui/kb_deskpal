"""Agent 与可选陪伴状态的协议定义，供应用层注入实现。"""

from typing import Any, Iterable, Protocol

from deskpal.tasks.models import Task

from .models import AgentResult


class CompanionPort(Protocol):
    """角色背景参与理解与表达；不能授予工具权限。"""

    def snapshot(self) -> dict[str, Any]: ...

    def command(self, action: str, **parameters: Any) -> dict[str, Any]: ...


    def style_instruction(self, request: str, *, cloud: bool,
                          preferences: dict[str, Any] | None = None,
                          constraints: dict[str, Any] | None = None,
                          mode: str | None = None, conversation_id: str = "default",
                          use_policy: str = "cloud_allowed") -> str: ...

    def reset_for_privacy(self) -> None: ...

    def close(self) -> None: ...


class Agent(Protocol):
    """应用可依赖的 Agent 操作面；授权只对当前请求生效。"""

    def run(self, request: str, *, authorized_tools: Iterable[str] = (),
            high_risk_authorized_tools: Iterable[str] = ()) -> AgentResult: ...

    def handle_request(self, request: str, *, authorized_tools: Iterable[str] = (),
                       high_risk_authorized_tools: Iterable[str] = ()) -> AgentResult:
        """Alias for run; authorization applies only to this request."""
        ...

    def confirm_task(self, task_id: str, *, authorized_tools: Iterable[str] = (),
                     high_risk_authorized_tools: Iterable[str] = ()) -> AgentResult:
        """Authorize the stored Waiting plan without invoking the planner again."""
        ...

    def cancel_task(self, task_id: str) -> Task:
        """Cancel through the application facade with a state audit event."""
        ...

    def close(self) -> None:
        """Close the agent and release resources explicitly owned by its factory."""
        ...

