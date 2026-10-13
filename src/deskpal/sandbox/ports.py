from pathlib import Path
from typing import Protocol

from deskpal.tools.models import ToolContext, ToolSpec


class Sandbox(Protocol):
    def check_permission(self, spec: ToolSpec, context: ToolContext) -> None: ...

    def resolve_path(self, path: str, *, for_write: bool = False) -> Path:
        """Resolve within workspace; reject traversal, symlink/junction escapes."""
        ...

