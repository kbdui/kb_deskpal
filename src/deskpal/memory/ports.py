from typing import Protocol, Sequence

from .models import MemoryKind, MemoryRecord


class MemoryManager(Protocol):
    def save(self, record: MemoryRecord) -> MemoryRecord:
        """Assign an id for a new record; an existing id replaces that record."""
        ...

    def search(self, query: str, *, limit: int = 5, kind: MemoryKind | None = None) -> Sequence[MemoryRecord]: ...

    def delete(self, memory_id: str) -> bool: ...

