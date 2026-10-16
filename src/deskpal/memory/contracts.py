"""V0.3 immutable memory contracts; storage and SDKs depend on these types."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Protocol

from .models import MemoryKind, MemoryRecord


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


Clock = Callable[[], datetime]


class MemoryError(ValueError):
    """A safe, user-facing memory validation error."""


class RevisionConflict(MemoryError):
    pass


class MemoryNotFound(MemoryError):
    pass


class MemoryStatus(str, Enum):
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
    REJECTED = "rejected"
    DELETED = "deleted"
    LEGACY = "legacy"


class ClaimType(str, Enum):
    USER_REPORT = "user_report"
    OBSERVED_EVENT = "observed_event"
    EXTERNAL_CLAIM = "external_claim"
    HYPOTHESIS = "hypothesis"
    LEGACY = "legacy"


class UsePolicy(str, Enum):
    CLOUD_ALLOWED = "cloud_allowed"
    LOCAL_ONLY = "local_only"


@dataclass(frozen=True)
class MemorySource:
    content: str
    origin: str = "user_message"
    id: str = ""
    owner_id: str = "local"
    session_id: str = "default"
    task_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    use_policy: UsePolicy = UsePolicy.CLOUD_ALLOWED


@dataclass(frozen=True)
class MemoryDraft:
    content: str
    quote: str
    claim_type: ClaimType = ClaimType.USER_REPORT
    kind: MemoryKind = MemoryKind.SEMANTIC
    subject: str = "user"
    predicate: str = "statement"
    value: str = ""
    scope: str = "global"
    profile_key: str = ""
    importance: float = 0.5
    extraction_confidence: float = 0.0
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    expires_at: datetime | None = None
    pinned: bool = False
    half_life_days: float = 30.0


@dataclass(frozen=True)
class MemoryItem:
    id: str
    revision: int
    status: MemoryStatus
    source: MemorySource
    draft: MemoryDraft
    recorded_at: datetime
    accepted_at: datetime | None = None
    supersedes: str | None = None
    change_reason: str = ""


@dataclass(frozen=True)
class DeletionReceipt:
    memory_id: str
    revision: int
    epoch: int
    affected_task_ids: tuple[str, ...] = ()
    cleared: tuple[str, ...] = ("memory", "source", "index", "profile")
    retained: tuple[str, ...] = ("historical execution logs", "external copies and backups")


@dataclass(frozen=True)
class MemoryPage:
    items: tuple[MemoryItem, ...]
    offset: int
    has_more: bool
    epoch: int


@dataclass(frozen=True)
class ProfileEntry:
    key: str
    value: str
    scope: str
    memory_id: str
    revision: int
    use_policy: UsePolicy


@dataclass(frozen=True)
class RetrievalBundle:
    records: tuple[MemoryRecord, ...] = ()
    revisions: tuple[tuple[str, int], ...] = ()
    epoch: int = 0
    warnings: tuple[str, ...] = ()
    bytes_used: int = 0


@dataclass(frozen=True)
class EmbeddingManifest:
    model: str
    dimensions: int
    version: str


class EmbeddingProvider(Protocol):
    @property
    def manifest(self) -> EmbeddingManifest: ...

    def embed_query(self, text: str) -> tuple[float, ...]: ...

    def embed_documents(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


class MemoryRepository(Protocol):
    @property
    def epoch(self) -> int: ...

    def page_items(self, *, status: MemoryStatus | None = None, limit: int = 100,
                   offset: int = 0) -> MemoryPage: ...

    def ingest(self, source: MemorySource, draft: MemoryDraft, *,
               accepted: bool = False, idempotency_key: str = "") -> MemoryItem: ...

    def get(self, memory_id: str, *, revision: int | None = None) -> MemoryItem | None: ...

    def list_items(self, *, status: MemoryStatus | None = None,
                   include_history: bool = False) -> tuple[MemoryItem, ...]: ...

    def accept(self, memory_id: str, expected_revision: int) -> MemoryItem: ...

    def reject(self, memory_id: str, expected_revision: int) -> MemoryItem: ...

    def revise(self, memory_id: str, expected_revision: int, source: MemorySource,
               draft: MemoryDraft, *, temporal: bool = False) -> MemoryItem: ...

    def forget(self, memory_id: str, expected_revision: int) -> DeletionReceipt: ...

    def claim_learning(self, key: str) -> bool:
        """Atomically consume a root learning key, including failed/empty evaluations."""
        ...

    def put_embedding(self, memory_id: str, revision: int, manifest: EmbeddingManifest,
                      vector: tuple[float, ...]) -> bool: ...

    def embeddings(self, manifest: EmbeddingManifest) -> tuple[tuple[str, int, tuple[float, ...]], ...]: ...

    def close(self) -> None: ...


class MemoryEvaluator(Protocol):
    def evaluate(self, source: MemorySource) -> tuple[MemoryDraft, ...]: ...


class MemoryRuntime(Protocol):
    @property
    def epoch(self) -> int: ...

    def page_items(self, *, status: MemoryStatus | None = None, limit: int = 100,
                   offset: int = 0) -> MemoryPage: ...

    def retrieve(self, query: str) -> RetrievalBundle: ...

    def remember(self, content: str, *, task_id: str | None = None,
                 use_policy: UsePolicy = UsePolicy.CLOUD_ALLOWED, profile_key: str = "",
                 profile_value: str = "", scope: str = "global") -> MemoryItem: ...

    def record_execution(self, task_id: str, summary: str) -> MemoryItem: ...

    def learn(self, request: str, task_id: str) -> tuple[str, ...]: ...

    def profile(self) -> tuple[ProfileEntry, ...]: ...

    def list_items(self, *, status: MemoryStatus | None = None,
                   include_history: bool = False) -> tuple[MemoryItem, ...]: ...

    def accept(self, memory_id: str, expected_revision: int) -> MemoryItem: ...

    def reject(self, memory_id: str, expected_revision: int) -> MemoryItem: ...

    def correct(self, memory_id: str, expected_revision: int, content: str, *,
                temporal: bool = False) -> MemoryItem: ...

    def forget(self, memory_id: str, expected_revision: int) -> DeletionReceipt: ...

    def export(self, *, include_history: bool = False) -> str: ...

    def valid(self, revisions: tuple[tuple[str, int], ...]) -> bool: ...
