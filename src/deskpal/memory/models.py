from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class MemoryKind(str, Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    GOAL = "goal"
    RELATIONSHIP = "relationship"


@dataclass(frozen=True)
class MemoryRecord:
    content: str
    kind: MemoryKind = MemoryKind.EPISODIC
    id: str = ""
    importance: float = 0.5
    confidence: float = 1.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_access_at: datetime | None = None
    decay_rate: float = 0.0
    task_id: str | None = None

