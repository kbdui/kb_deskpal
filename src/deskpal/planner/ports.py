from typing import Protocol, Sequence

from deskpal.memory.models import MemoryRecord
from deskpal.tools.models import ToolSpec

from .models import Plan


class Planner(Protocol):
    def plan(self, request: str, *, memories: Sequence[MemoryRecord] = (), tools: Sequence[ToolSpec] = ()) -> Plan: ...

