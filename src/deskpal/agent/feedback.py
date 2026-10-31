"""同步反馈收尾契约；P4 提供实际学习实现，不能排回同一队列等待自己。"""
from typing import Any, Protocol
from .context import PreparedTurn


class TurnFeedback(Protocol):
    def finish(self, *, task_id: str, request: str, response: str,
               observations: tuple[dict[str, Any], ...], context: PreparedTurn | None) -> tuple[str, ...]: ...
