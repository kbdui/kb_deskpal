"""Immutable per-turn inputs and the optional application interaction boundary.

Execution receives reference data, never expression policy or assistant prose.
Concrete persistence and lifecycle remain in Application.
"""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from deskpal.llm.ports import LLMProvider
from deskpal.memory.contracts import RetrievalBundle


@dataclass(frozen=True)
class PreparedTurn:
    task_id: str
    privacy_epoch: int
    conversation_id: str = ""
    conversation_epoch: int = 0
    sequence: int = 0
    planning_items: tuple[Mapping[str, Any], ...] = ()
    history: tuple[Mapping[str, Any], ...] = ()
    style: str = ""
    memories: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    use_policy: str = "cloud_allowed"
    expression_epoch: int = 0
    consumed_profile_keys: frozenset[str] = frozenset()
    allowed_cloud_categories: frozenset[str] = frozenset()


class TurnInteractionPort(Protocol):
    def source_policy(self, task_id: str) -> str: ...

    def retrieve(self, request: str) -> RetrievalBundle: ...

    def prepare(self, task_id: str, request: str, bundle: RetrievalBundle) -> PreparedTurn: ...

    def calls(self, task_id: str, purpose: str) -> LLMProvider: ...

    def check(self, task_id: str, *, expression: bool = False) -> None: ...

    def publication(self, task_id: str) -> AbstractContextManager[None]: ...

    def publish(self, turn: PreparedTurn, request: str, response: str, *,
                facts: tuple[Mapping[str, Any], ...], signals: Mapping[str, Any]) -> None: ...

    def invalidate(self) -> None: ...

    def close(self) -> None: ...
