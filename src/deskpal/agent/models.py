from dataclasses import dataclass

from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolResult


@dataclass(frozen=True)
class AgentResult:
    task_id: str
    status: TaskStatus
    response: str
    tool_results: tuple[ToolResult, ...] = ()
    required_authorizations: tuple[str, ...] = ()
    error: str | None = None

