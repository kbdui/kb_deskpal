"""Small, bounded UI state. This module has no application/database dependency."""

from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class PendingRequest:
    request_id: str
    operation: str
    route: str
    generation: int
    mutation: bool
    privacy_epoch: int
    command_key: str = ""


class RequestTracker:
    """Coalesce queries and keep only bounded request/command bookkeeping."""

    def __init__(self, limit: int = 32) -> None:
        self.limit = limit
        self.pending: dict[str, PendingRequest] = {}
        self.generations: dict[str, int] = {}
        self._serial = 0
        self._commands: OrderedDict[str, str] = OrderedDict()

    def invalidate(self, route: str | None = None) -> None:
        routes = tuple(self.generations) if route is None else (route,)
        for key in routes:
            self._serial += 1
            self.generations[key] = self._serial

    def generation(self, route: str) -> int:
        if route not in self.generations:
            self.invalidate(route)
        return self.generations[route]

    def existing(self, route: str) -> PendingRequest | None:
        return next((item for item in self.pending.values() if item.route == route), None)

    def current(self, request: PendingRequest) -> bool:
        return self.generations.get(request.route) == request.generation

    @staticmethod
    def command_key(operation: str, parameters: dict[str, Any]) -> str:
        return sha256(json.dumps([operation, parameters], sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def command_id(self, operation: str, parameters: dict[str, Any]) -> str:
        key = self.command_key(operation, parameters)
        if key not in self._commands:
            self._commands[key] = str(uuid4())
            if len(self._commands) > 128:
                self._commands.popitem(last=False)
        self._commands.move_to_end(key)
        return self._commands[key]

    def complete_command(self, key: str) -> None:
        self._commands.pop(key, None)

    def clear_commands(self) -> None:
        self._commands.clear()


def plain_json(value: Any) -> str:
    """Keep untrusted model/tool data as text, including URLs and HTML."""
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def tool_permissions(snapshot: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Recover permission requirements from the persisted plan and tool specs."""
    levels = {str(item.get("name", "")): int(item.get("permission_level", 0))
              for item in snapshot.get("tool_specs", []) if isinstance(item, dict)}
    ordinary: list[str] = []
    high: list[str] = []
    plan = snapshot.get("plan", {})
    for call in plan.get("steps", []) if isinstance(plan, dict) else []:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name", ""))
        # A missing spec must never result in a grant control.
        if levels.get(name) == 1 and name not in ordinary:
            ordinary.append(name)
        elif levels.get(name) == 2 and name not in high:
            high.append(name)
    return ordinary, high
