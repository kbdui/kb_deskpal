from typing import Iterable, Protocol

from deskpal.tasks.models import Task

from .models import AgentResult


class Agent(Protocol):
    def run(self, request: str, *, authorized_tools: Iterable[str] = ()) -> AgentResult: ...

    def handle_request(self, request: str, *, authorized_tools: Iterable[str] = ()) -> AgentResult:
        """Alias for run; authorization applies only to this request."""
        ...

    def cancel_task(self, task_id: str) -> Task:
        """Cancel through the application facade with a state audit event."""
        ...

    def close(self) -> None:
        """Close the agent and release resources explicitly owned by its factory."""
        ...

