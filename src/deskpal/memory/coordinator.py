"""Controlled memory writes and runtime/legacy adapters; no Planner dependency."""

import hashlib
import json
import re
from dataclasses import asdict, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from .contracts import (
    ClaimType, Clock, DeletionReceipt, MemoryDraft, MemoryError, MemoryEvaluator,
    MemoryItem, MemoryNotFound, MemoryRepository, MemorySource, MemoryStatus,
    ProfileEntry, RetrievalBundle, UsePolicy, utc_now,
)
from .evaluation import PROFILE_KEYS, profile_hint, validate_draft
from .models import MemoryKind, MemoryRecord

MAX_CONTENT_BYTES = 8000


class _Retriever(Protocol):
    def retrieve(self, query: str) -> RetrievalBundle: ...

    def rebuild_index(self) -> tuple[str, ...]: ...


def _content(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise MemoryError("Memory content must be a non-empty string")
    try:
        if len(text.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise MemoryError("Memory content exceeds its byte limit")
    except UnicodeError:
        raise MemoryError("Memory content must be valid UTF-8") from None
    if (re.search(r"(?i)(?:api[_-]?key|secret|token|密码|密钥)\s*[:=：]\s*\S+", text)
            or re.search(r"\bsk-[A-Za-z0-9_-]{12,}\b", text)):
        raise MemoryError("Credential-like content must not be stored as memory")
    return text


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


class MemoryCoordinator:
    def __init__(self, repository: MemoryRepository, *, retriever: _Retriever | None = None,
                 evaluator: MemoryEvaluator | None = None, mode: str = "explicit_only",
                 cloud: bool = True, clock: Clock = utc_now) -> None:
        if mode not in {"explicit_only", "candidate_review", "paused"}:
            raise MemoryError("Unknown memory mode")
        if type(cloud) is not bool or not callable(clock):
            raise MemoryError("Invalid memory runtime configuration")
        self.repository = repository
        self._retriever = retriever
        self._evaluator = evaluator
        self.mode = mode
        self.cloud = cloud
        self._clock = clock

    def _get_retriever(self) -> _Retriever:
        if self._retriever is None:
            from .retrieval import MemoryRetriever
            self._retriever = MemoryRetriever(self.repository, cloud=self.cloud, clock=self._clock)
        return self._retriever

    def rebuild_index(self) -> tuple[str, ...]:
        """Explicit index maintenance; indexing never changes source acceptance."""
        return self._get_retriever().rebuild_index()

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise MemoryError("Memory clock must return an aware datetime")
        return now.astimezone(timezone.utc)

    @property
    def epoch(self) -> int:
        return self.repository.epoch

    def retrieve(self, query: str) -> RetrievalBundle:
        result = self._get_retriever().retrieve(query)
        if not isinstance(result, RetrievalBundle):
            raise MemoryError("Retriever returned an invalid context bundle")
        return result

    def _profile_fields(self, content: str, key: str = "", value: str = "") -> tuple[str, str]:
        hint = profile_hint(content)
        if key or value:
            if key not in PROFILE_KEYS or hint != (key, value):
                raise MemoryError("Profile fields must match an explicit supported preference")
            return key, value
        return hint if hint is not None else ("", "")

    def _check_conflict(self, key: str, value: str, scope: str, *, excluding: str = "",
                        owner_id: str = "local") -> None:
        if not key:
            return
        now = self._now()
        for item in self.repository.list_items(status=MemoryStatus.ACCEPTED):
            draft = item.draft
            if (item.id != excluding and item.source.owner_id == owner_id
                    and draft.profile_key == key and draft.scope == scope
                    and draft.subject == "user" and draft.claim_type == ClaimType.USER_REPORT
                    and self._eligible(item, now, enforce_cloud=False) and draft.value != value):
                raise MemoryError("Preference conflicts with an existing record; use revision-checked correct")

    def remember(self, content: str, *, task_id: str | None = None,
                 use_policy: UsePolicy = UsePolicy.CLOUD_ALLOWED, profile_key: str = "",
                 profile_value: str = "", scope: str = "global") -> MemoryItem:
        content = _content(content)
        if not isinstance(use_policy, UsePolicy) or not isinstance(scope, str) or not scope.strip() or len(scope) > 100:
            raise MemoryError("Invalid memory policy or scope")
        key, value = self._profile_fields(content, profile_key, profile_value)
        self._check_conflict(key, value, scope)
        now = self._now()
        source = MemorySource(content, task_id=task_id, created_at=now, use_policy=use_policy)
        draft = MemoryDraft(content, content, profile_key=key, predicate=key or "statement",
                            value=value, scope=scope, valid_from=now, extraction_confidence=0.0)
        return self.repository.ingest(source, draft, accepted=True)

    def record_execution(self, task_id: str, summary: str, *,
                         use_policy: UsePolicy = UsePolicy.CLOUD_ALLOWED) -> MemoryItem:
        summary = _content(summary)
        if not isinstance(task_id, str) or not task_id.strip():
            raise MemoryError("Execution memory requires a task id")
        if not isinstance(use_policy, UsePolicy):
            raise MemoryError("Invalid execution memory policy")
        # Compare original evidence, including superseded revisions, rather than
        # regenerating timestamps or allowing a retry to overwrite a correction.
        for item in self.repository.list_items(include_history=True):
            if (item.source.origin == "tool_result" and item.source.task_id == task_id
                    and item.draft.claim_type == ClaimType.OBSERVED_EVENT):
                if item.source.content != summary or item.source.use_policy != use_policy:
                    raise MemoryError("Execution task was already recorded with different evidence")
                current = self.repository.get(item.id)
                if current is None:
                    raise MemoryError("Execution memory is inconsistent")
                return current
        source = MemorySource(summary, origin="tool_result", task_id=task_id,
                              created_at=self._now(), use_policy=use_policy)
        draft = MemoryDraft(summary, summary, claim_type=ClaimType.OBSERVED_EVENT,
                            kind=MemoryKind.EPISODIC, subject="task", predicate="execution",
                            value=task_id, valid_from=source.created_at)
        return self.repository.ingest(source, draft, accepted=True,
                                      idempotency_key="execution:" + task_id)

    def learn(self, request: str, task_id: str, *,
              use_policy: UsePolicy = UsePolicy.CLOUD_ALLOWED) -> tuple[str, ...]:
        if self.mode != "candidate_review":
            return ()
        if self._evaluator is None:
            return ("Automatic memory evaluation is unavailable",)
        if use_policy != UsePolicy.CLOUD_ALLOWED:
            return ("Automatic memory evaluation skipped by use policy",)
        try:
            request = _content(request)
            if not isinstance(task_id, str) or not task_id.strip():
                raise MemoryError("Learning requires a task id")
            # Durable consumption precedes external evaluation. Even empty,
            # failed, rejected or forgotten extraction is not retried implicitly.
            if not self.repository.claim_learning(task_id):
                return ()
            now = self._now()
            digest = hashlib.sha256(request.encode("utf-8")).hexdigest()
            source = MemorySource(request, id=str(uuid5(NAMESPACE_URL, "deskpal/learn/" + task_id + "/" + digest)),
                                  task_id=task_id, created_at=now, use_policy=use_policy)
            candidates = self._evaluator.evaluate(source)
            maximum = getattr(self._evaluator, "max_candidates", 5)
            if (type(maximum) is not int or maximum <= 0 or not isinstance(candidates, tuple)
                    or len(candidates) > maximum):
                raise MemoryError("Evaluator returned invalid candidates")
            for draft in candidates:
                validate_draft(draft, source)
            for draft in candidates:
                # Repository owns candidate TTL; default expiry is cleared on
                # explicit acceptance. Evaluators cannot choose validity/pinning.
                bounded = replace(draft, valid_from=None, valid_to=None, expires_at=None,
                                  pinned=False, half_life_days=30.0)
                fingerprint = hashlib.sha256(json.dumps(_json_value(asdict(bounded)), sort_keys=True,
                                                       ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
                self.repository.ingest(source, bounded, accepted=False,
                                       idempotency_key="learning:" + task_id + ":" + fingerprint)
            return ()
        except Exception:
            return ("Automatic memory learning was incomplete",)

    def profile(self) -> tuple[ProfileEntry, ...]:
        from .retrieval import profile_entries
        return profile_entries(self.repository, clock=self._clock, include_local=True)

    def list_items(self, *, status: MemoryStatus | None = None,
                   include_history: bool = False) -> tuple[MemoryItem, ...]:
        return self.repository.list_items(status=status, include_history=include_history)

    def accept(self, memory_id: str, expected_revision: int) -> MemoryItem:
        item = self.repository.get(memory_id)
        if item is None:
            raise MemoryNotFound("Memory record does not exist")
        self._check_conflict(item.draft.profile_key, item.draft.value, item.draft.scope,
                             excluding=item.id, owner_id=item.source.owner_id)
        return self.repository.accept(memory_id, expected_revision)

    def reject(self, memory_id: str, expected_revision: int) -> MemoryItem:
        return self.repository.reject(memory_id, expected_revision)

    def correct(self, memory_id: str, expected_revision: int, content: str, *, temporal: bool = False) -> MemoryItem:
        content = _content(content)
        item = self.repository.get(memory_id)
        if item is None:
            raise MemoryNotFound("Memory record does not exist")
        key, value = self._profile_fields(content)
        self._check_conflict(key, value, item.draft.scope, excluding=memory_id,
                             owner_id=item.source.owner_id)
        now = self._now()
        source = MemorySource(content, task_id=item.source.task_id, created_at=now,
                              owner_id=item.source.owner_id, session_id=item.source.session_id,
                              use_policy=item.source.use_policy)
        draft = MemoryDraft(content, content, predicate=key or "statement", value=value,
                            profile_key=key, scope=item.draft.scope,
                            valid_from=now if temporal else item.draft.valid_from)
        return self.repository.revise(memory_id, expected_revision, source, draft, temporal=temporal)

    def forget(self, memory_id: str, expected_revision: int) -> DeletionReceipt:
        return self.repository.forget(memory_id, expected_revision)

    def export(self, *, include_history: bool = False) -> str:
        items = []
        for item in self.repository.list_items(include_history=include_history):
            if item.status == MemoryStatus.DELETED:
                if include_history:
                    items.append({"id": item.id, "revision": item.revision, "status": item.status.value})
                continue
            items.append(_json_value(asdict(item)))
        return json.dumps({"version": 1, "items": items}, ensure_ascii=False, allow_nan=False)

    def _eligible(self, item: MemoryItem, now: datetime, *, enforce_cloud: bool = True) -> bool:
        draft = item.draft
        start = draft.valid_from if draft.valid_from is not None else item.source.created_at
        return (item.status == MemoryStatus.ACCEPTED
                and draft.claim_type != ClaimType.LEGACY
                and (not enforce_cloud or not self.cloud or item.source.use_policy == UsePolicy.CLOUD_ALLOWED)
                and start <= now
                and (draft.valid_to is None or now < draft.valid_to)
                and (draft.expires_at is None or now < draft.expires_at))

    def valid(self, revisions: tuple[tuple[str, int], ...]) -> bool:
        now = self._now()
        for memory_id, revision in revisions:
            item = self.repository.get(memory_id)
            if item is None or item.revision != revision or not self._eligible(item, now):
                return False
        return True

    def cleanup_candidates(self) -> tuple[DeletionReceipt, ...]:
        now = self._now()
        return tuple(self.repository.forget(item.id, item.revision)
                     for item in self.repository.list_items(status=MemoryStatus.CANDIDATE)
                     if item.draft.expires_at is not None and item.draft.expires_at <= now)

    def save(self, record: MemoryRecord) -> MemoryRecord:
        if not isinstance(record, MemoryRecord):
            raise MemoryError("Expected a legacy MemoryRecord")
        _content(record.content)
        if record.id and self.repository.get(record.id) is not None:
            raise MemoryError("Legacy save cannot overwrite existing records; use revision-checked correct")
        source = MemorySource(record.content, origin="legacy", task_id=record.task_id,
                              created_at=record.created_at)
        draft = MemoryDraft(record.content, record.content, claim_type=ClaimType.LEGACY,
                            kind=record.kind, importance=record.importance,
                            extraction_confidence=0.0)
        item = self.repository.ingest(source, draft, accepted=False,
                                      idempotency_key="legacy:" + record.id if record.id else "")
        return replace(record, id=item.id)

    def search(self, query: str, *, limit: int = 5, kind: MemoryKind | None = None) -> tuple[MemoryRecord, ...]:
        if type(limit) is not int or limit <= 0:
            raise MemoryError("Memory limit must be a positive integer")
        if kind is not None and not isinstance(kind, MemoryKind):
            raise MemoryError("Memory kind must be a MemoryKind member")
        return tuple(record for record in self.retrieve(query).records if kind is None or record.kind == kind)[:limit]

    def delete(self, memory_id: str) -> bool:
        item = self.repository.get(memory_id)
        if item is None or item.status == MemoryStatus.DELETED:
            return False
        self.repository.forget(memory_id, item.revision)
        return True
