"""单轮上下文及应用层交互协议。

规划与表达共用角色背景、有效要求和有来源的双方历史；
具体持久化、隐私生命周期和上下文发布由 Application 负责。
"""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

from deskpal.llm.ports import LLMProvider
from deskpal.memory.contracts import RetrievalBundle
from deskpal.memory.models import MemoryRecord


@dataclass(frozen=True)
class PreparedTurn:
    """应用层为一次请求准备的不可变快照，供规划、表达和发布阶段使用。"""

    task_id: str
    privacy_epoch: int
    conversation_id: str = ""
    conversation_epoch: int = 0
    sequence: int = 0
    planning_items: tuple[Mapping[str, Any], ...] = ()
    history: tuple[Mapping[str, Any], ...] = ()
    style: str = ""
    memories: tuple[str, ...] = ()
    memory_records: tuple[MemoryRecord, ...] = ()
    warnings: tuple[str, ...] = ()
    use_policy: str = "cloud_allowed"
    profile_id: str = ""
    runtime_id: str = ""
    role_id: str = ""
    role_revision: int = 0
    role_json: str = ""
    origin: str = "user"
    retrieval: RetrievalBundle = field(default_factory=RetrievalBundle)
    conversation_summary: Mapping[str, Any] | None = None


class TurnInteractionPort(Protocol):
    """AgentCore 与应用交互协调器之间的边界，避免核心层依赖具体服务。"""

    def source_policy(self, task_id: str) -> str: ...

    def current_turn(self, task_id: str) -> PreparedTurn: ...

    def retrieve(self, request: str) -> RetrievalBundle: ...

    def prepare(self, task_id: str, request: str, bundle: RetrievalBundle) -> PreparedTurn: ...

    def prepare_continuation(self, task_id: str) -> None: ...

    def apply_decision(self, task_id: str, decision: dict[str, Any] | None) -> bool: ...

    def calls(self, task_id: str, purpose: str) -> LLMProvider: ...

    def check(self, task_id: str, *, expression: bool = False) -> None: ...

    def publication(self, task_id: str) -> AbstractContextManager[None]: ...

    def publish(self, turn: PreparedTurn, request: str, response: str, *,
                facts: tuple[Mapping[str, Any], ...], signals: Mapping[str, Any]) -> None: ...

    def invalidate(self) -> None: ...

    def close(self) -> None: ...
