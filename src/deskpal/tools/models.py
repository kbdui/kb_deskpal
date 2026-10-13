"""Shared tool DTOs. Tool schemas follow the JSON Schema object convention."""

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Mapping


class PermissionLevel(IntEnum):
    AUTOMATIC = 0
    CONFIRM = 1
    HIGH_RISK = 2


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, Any]
    permission_level: PermissionLevel = PermissionLevel.AUTOMATIC


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    call_id: str = ""


@dataclass(frozen=True)
class ToolContext:
    task_id: str
    workspace_dir: Path
    authorized_tools: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ToolResult:
    success: bool
    output: Any = None
    error: str | None = None
    tool_name: str = ""
    call_id: str = ""

