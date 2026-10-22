"""Current, attributed evidence retrieval; this module never writes instructions."""

import json
import math
import re
import unicodedata
from datetime import datetime

from .contracts import (
    ClaimType, Clock, EmbeddingManifest, EmbeddingProvider, MemoryError, MemoryItem, MemoryRepository,
    MemoryStatus, ProfileEntry, RetrievalBundle, UsePolicy, utc_now,
)
from .embedding import EmbeddingError, normalized_vector
from .models import MemoryRecord

# These are data fields, never authorization or executable policy.
PROFILE_KEYS = frozenset({"preferred_name", "reply_language", "verbosity"})
_QUERY_FILLERS = ("请告诉我", "你还记得", "用户", "请问", "我的", "什么", "哪些", "是否", "喜欢", "我", "的", "吗", "呢")


def _eligible(item: MemoryItem, now: datetime, *, include_local: bool, as_of: datetime | None = None) -> bool:
    draft = item.draft
    when = now if as_of is None else as_of
    start = draft.valid_from if draft.valid_from is not None else item.source.created_at
    return (
        item.status == MemoryStatus.ACCEPTED
        and item.source.owner_id == "local"
        and draft.claim_type != ClaimType.LEGACY
        and draft.scope == "global"
        and (include_local or item.source.use_policy == UsePolicy.CLOUD_ALLOWED)
        and start <= when
        and (draft.valid_to is None or when < draft.valid_to)
        and (draft.expires_at is None or now < draft.expires_at)
    )


def _indexable(item: MemoryItem, now: datetime) -> bool:
    # Current accepted heads may describe historical periods. Keep their
    # vectors available for explicit as_of, while retrieval applies validity.
    return (item.status == MemoryStatus.ACCEPTED and item.draft.claim_type != ClaimType.LEGACY
            and item.draft.scope == "global"
            and (item.draft.expires_at is None or now < item.draft.expires_at))


def _profile(item: MemoryItem) -> bool:
    draft = item.draft
    value_valid = (
        (draft.profile_key == "preferred_name" and 0 < len(draft.value.strip()) <= 40)
        or (draft.profile_key == "reply_language" and draft.value in {"zh", "en"})
        or (draft.profile_key == "verbosity" and draft.value in {"brief", "detailed"})
    )
    return (draft.claim_type == ClaimType.USER_REPORT and draft.subject == "user" and value_valid
            and item.source.origin in {"user_message", "user_confirmation"})


def profile_entries(
    repo: MemoryRepository, *, clock: Clock = utc_now, include_local: bool = False,
) -> tuple[ProfileEntry, ...]:
    """Project exact accepted user fields; conflicting values are not guessed."""
    now = clock()
    grouped: dict[str, list[MemoryItem]] = {}
    for item in repo.list_items(status=MemoryStatus.ACCEPTED):
        if not _profile(item):
            continue
        current = repo.get(item.id)
        if (current is None or current.revision != item.revision
                or not _eligible(current, now, include_local=include_local) or not _profile(current)):
            continue
        grouped.setdefault(current.draft.profile_key, []).append(current)
    entries: list[ProfileEntry] = []
    for key, items in sorted(grouped.items()):
        if len({item.draft.value for item in items}) != 1:
            continue
        item = max(items, key=lambda value: (value.recorded_at, value.id))
        entries.append(ProfileEntry(key, item.draft.value, item.draft.scope, item.id,
                                    item.revision, item.source.use_policy))
    return tuple(entries)


def _profile_ids(items: dict[str, MemoryItem]) -> set[str]:
    """Project the already eligible retrieval snapshot without rereading it.

    Selected records still pass the same final revision and epoch checks.
    """
    grouped: dict[str, list[MemoryItem]] = {}
    for item in items.values():
        if _profile(item):
            grouped.setdefault(item.draft.profile_key, []).append(item)
    return {max(group, key=lambda item: (item.recorded_at, item.id)).id
            for group in grouped.values() if len({item.draft.value for item in group}) == 1}


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _terms(text: str) -> set[str]:
    value = _plain(text)
    for filler in _QUERY_FILLERS:
        value = value.replace(filler, " ")
    result = set(re.findall(r"[a-z]+(?:[-_][a-z0-9]+)*|\d+(?:\.\d+)?", value))
    for segment in re.findall(r"[\u3400-\u9fff]+", value):
        if len(segment) == 1:
            if segment not in "不没无是在有和或了也都这那要能很":
                result.add(segment)
        else:
            result.update(segment[index:index + 2] for index in range(len(segment) - 1))
    return result


def _lexical(query: str, content: str) -> float:
    terms = _terms(query)
    if not terms:
        return 0.0
    common = terms & _terms(content)
    return len(common) / len(terms) if common else 0.0


def _compatible(query: str, content: str) -> bool:
    """Exact numeric constraints beat cosine; preserve explicit negation."""
    numbers = set(re.findall(r"\d+(?:\.\d+)?", _plain(query)))
    if numbers and not numbers.issubset(set(re.findall(r"\d+(?:\.\d+)?", _plain(content)))):
        return False
    # Questions asking about polarity remain eligible on either side. A direct
    # negative query must not match a positive assertion by shared nouns alone.
    question = any(word in query for word in ("是否", "是不是", "有没有", "吗", "？", "?"))
    if not question and _negative(_plain(query)) and not _negative(_plain(content)):
        return False
    return True


def _negative(text: str) -> bool:
    return bool(re.search(r"不|没|无|never|\bnot\b|\bno\b", text))


def _cosine(vector: tuple[float, ...], indexed: tuple[float, ...]) -> float:
    """Normalize stored numeric vectors without allocating a second tuple.

    The sum of squares being finite and positive also proves every component
    is finite and the vector nonzero. Keep division before multiplication and
    math.fsum, exactly as normalized_vector followed by the original dot product.
    """
    try:
        if len(indexed) != len(vector):
            raise ValueError
        norm = math.sqrt(math.fsum(value * value for value in indexed))
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError
        return math.fsum(left * (right / norm) for left, right in zip(vector, indexed))
    except (TypeError, ValueError, OverflowError):
        raise EmbeddingError("Embedding vector has invalid dimensions or non-finite values") from None


def _record(item: MemoryItem, reasons: tuple[str, ...]) -> MemoryRecord:
    draft = item.draft
    evidence = {
        "type": "memory_evidence", "memory_id": item.id, "revision": item.revision,
        "claim_type": draft.claim_type.value, "source_origin": item.source.origin,
        "source_id": item.source.id, "source_time": item.source.created_at.isoformat(),
        "recorded_at": item.recorded_at.isoformat(), "scope": draft.scope,
        "valid_from": draft.valid_from.isoformat() if draft.valid_from else None,
        "valid_to": draft.valid_to.isoformat() if draft.valid_to else None,
        "expires_at": draft.expires_at.isoformat() if draft.expires_at else None,
        "selected_by": reasons, "content": draft.content,
    }
    if "profile" in reasons:
        evidence["profile"] = {"key": draft.profile_key, "value": draft.value}
    return MemoryRecord(
        json.dumps(evidence, ensure_ascii=False, separators=(",", ":")), kind=draft.kind,
        id=item.id, importance=draft.importance, confidence=draft.extraction_confidence,
        created_at=item.recorded_at, task_id=item.source.task_id,
    )


class MemoryRetriever:
    """Fuse qualified lexical/semantic ranks, then apply bounded evidence budget.

    Default cosine threshold 0.55 comes from the versioned synthetic calibration
    set, not a universal accuracy claim. Importance/decay break relevance ties. Repository revision
    and epoch checks fail closed if a concurrent mutation is observed; Core
    must still validate the bundle immediately before external use.
    """

    def __init__(
        self, repository: MemoryRepository, *, embedding: EmbeddingProvider | None = None,
        cloud: bool = True, clock: Clock = utc_now, limit: int = 5,
        max_bytes: int = 8000, semantic_threshold: float = 0.55,
        startup_warnings: tuple[str, ...] = (),
    ) -> None:
        if (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
                or isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0):
            raise MemoryError("Retrieval limit and max_bytes must be positive integers")
        if (isinstance(semantic_threshold, bool) or not isinstance(semantic_threshold, (int, float))
                or not math.isfinite(semantic_threshold) or not -1 <= semantic_threshold <= 1):
            raise MemoryError("semantic_threshold must be finite and between -1 and 1")
        if not isinstance(cloud, bool):
            raise MemoryError("cloud must be boolean")
        if not isinstance(startup_warnings, tuple) or not all(isinstance(value, str) for value in startup_warnings):
            raise MemoryError("startup_warnings must be a tuple of safe strings")
        self.repository = repository
        self.embedding = embedding
        self.cloud = cloud
        self.clock = clock
        self.limit = limit
        self.max_bytes = max_bytes
        self.semantic_threshold = semantic_threshold
        self.startup_warnings = startup_warnings

    def rebuild_index(self) -> tuple[str, ...]:
        """Explicitly recompute accepted current heads under one manifest."""
        return self._build_index(only_missing=False)

    def ensure_index(self) -> tuple[str, ...]:
        """Incrementally encode missing current revision/manifest pairs only."""
        return self._build_index(only_missing=True)

    def _build_index(self, *, only_missing: bool, include_profiles: bool = True) -> tuple[str, ...]:
        if self.embedding is None:
            return ("semantic_disabled: lexical retrieval only",)
        warnings: list[str] = []
        try:
            manifest = self.embedding.manifest
            existing = {(memory_id, revision) for memory_id, revision, _ in self.repository.embeddings(manifest)} if only_missing else set()
            items = self.repository.list_items(status=MemoryStatus.ACCEPTED)
            if not include_profiles:
                items = tuple(item for item in items if not item.draft.profile_key)
            for offset in range(0, len(items), 8):
                batch = []
                for item in items[offset:offset + 8]:
                    if (item.id, item.revision) in existing:
                        continue
                    current = self.repository.get(item.id)
                    if (current is not None and current.revision == item.revision
                            and _indexable(current, self.clock())):
                        batch.append(current)
                if not batch:
                    continue
                try:
                    vectors = self.embedding.embed_documents(tuple(item.draft.content for item in batch))
                    if len(vectors) != len(batch) or self.embedding.manifest != manifest:
                        raise EmbeddingError("Embedding version or batch count changed")
                    vectors = tuple(normalized_vector(vector, manifest.dimensions) for vector in vectors)
                except Exception:
                    warnings.append("index_embedding_failed: lexical retrieval remains available")
                    continue
                for item, vector in zip(batch, vectors):
                    before = self.repository.get(item.id)
                    if (before is None or before.revision != item.revision
                            or not _indexable(before, self.clock())):
                        warnings.append("index_revision_changed: stale vector skipped")
                        continue
                    if self.embedding.manifest != manifest:
                        warnings.append("index_manifest_changed: rebuild required")
                        return tuple(dict.fromkeys(warnings))
                    saved = self.repository.put_embedding(item.id, item.revision, manifest, vector)
                    after = self.repository.get(item.id)
                    if (not saved or after is None or after.revision != item.revision
                            or not _indexable(after, self.clock())):
                        warnings.append("index_revision_changed: stale vector is unusable")
        except Exception:
            warnings.append("index_rebuild_failed: lexical retrieval remains available")
        return tuple(dict.fromkeys(warnings))

    def retrieve(self, query: str, *, as_of: datetime | None = None) -> RetrievalBundle:
        return self._retrieve(query, as_of=as_of, include_profiles=True)

    def retrieve_for_planning(self, query: str) -> RetrievalBundle:
        """Exclude expression profiles before ranking or consuming any budget.

        Custom retrievers used by production must provide this independent
        projection; filtering an already ranked result is insufficient because
        profile records could have displaced ordinary factual evidence.
        """
        return self._retrieve(query, as_of=None, include_profiles=False)

    def _retrieve(self, query: str, *, as_of: datetime | None,
                  include_profiles: bool) -> RetrievalBundle:
        if not isinstance(query, str):
            raise MemoryError("Retrieval query must be text")
        if as_of is not None and (not isinstance(as_of, datetime) or as_of.utcoffset() is None):
            raise MemoryError("as_of must be a timezone-aware datetime")
        epoch = self.repository.epoch
        if not query.strip():
            return RetrievalBundle(epoch=epoch, warnings=self.startup_warnings)
        now = self.clock()
        warnings: list[str] = list(self.startup_warnings)
        # Read each snapshot once on the common, already-indexed path. This is
        # query-local data, never a cross-query cache: TTL, deletion and changes
        # made by another connection are observed again on every retrieval.
        accepted = self.repository.list_items(status=MemoryStatus.ACCEPTED)
        if not include_profiles:
            accepted = tuple(item for item in accepted if not item.draft.profile_key)
        indexed_rows: tuple[tuple[str, int, tuple[float, ...]], ...] = ()
        index_manifest: EmbeddingManifest | None = None
        if self.embedding is not None:
            try:
                index_manifest = self.embedding.manifest
                indexed_rows = self.repository.embeddings(index_manifest)
                existing = {(id_, revision) for id_, revision, _ in indexed_rows}
                if any(_indexable(item, now) and (item.id, item.revision) not in existing for item in accepted):
                    warnings.extend(self.ensure_index() if include_profiles else
                                    self._build_index(only_missing=True, include_profiles=False))
                    accepted = self.repository.list_items(status=MemoryStatus.ACCEPTED)
                    if not include_profiles:
                        accepted = tuple(item for item in accepted if not item.draft.profile_key)
                    indexed_rows = self.repository.embeddings(index_manifest)
            except Exception:
                warnings.append("index_rebuild_failed: lexical retrieval remains available")
        items = {item.id: item for item in accepted
                 if _eligible(item, now, include_local=not self.cloud, as_of=as_of)}
        if not include_profiles:
            indexed_rows = tuple(row for row in indexed_rows if row[0] in items)
        profiles = _profile_ids(items) if include_profiles and as_of is None else set()
        lexical = [(item.id, _lexical(query, item.draft.content)) for item in items.values()
                   if _compatible(query, item.draft.content)]
        lexical = sorted(((id_, score) for id_, score in lexical if score >= 0.25),
                         key=lambda pair: (-pair[1], pair[0]))
        semantic: list[tuple[str, float]] = []
        if self.embedding is None:
            warnings.append("semantic_disabled: lexical retrieval only")
        else:
            try:
                manifest = self.embedding.manifest
                if manifest != index_manifest:
                    raise EmbeddingError("Embedding manifest changed")
                vector = normalized_vector(self.embedding.embed_query(query), manifest.dimensions)
                for memory_id, revision, indexed in indexed_rows:
                    item = items.get(memory_id)
                    if item is None or item.revision != revision or not _compatible(query, item.draft.content):
                        continue
                    try:
                        score = _cosine(vector, indexed)
                    except EmbeddingError:
                        warnings.append("invalid_index_vector: skipped")
                        continue
                    if score >= self.semantic_threshold:
                        semantic.append((memory_id, score))
                if self.embedding.manifest != manifest:
                    raise EmbeddingError("Embedding manifest changed")
                semantic.sort(key=lambda pair: (-pair[1], pair[0]))
                if not indexed_rows:
                    warnings.append("semantic_index_empty: rebuild required; lexical retrieval only")
            except Exception:
                semantic = []
                warnings.append("semantic_failed: lexical retrieval only")
        ranks: dict[str, float] = {}
        reasons: dict[str, list[str]] = {}
        for channel, ranked in (("lexical", lexical), ("semantic", semantic)):
            for rank, (memory_id, _) in enumerate(ranked[:max(20, self.limit * 4)], 1):
                ranks[memory_id] = ranks.get(memory_id, 0) + 1 / (60 + rank)
                reasons.setdefault(memory_id, []).append(channel)
        for memory_id in profiles & items.keys():
            reasons.setdefault(memory_id, []).append("profile")
            ranks.setdefault(memory_id, 0)

        def priority(memory_id: str) -> tuple[float, ...]:
            item = items[memory_id]
            age_days = max(0.0, (now - item.recorded_at).total_seconds() / 86400)
            activation = item.draft.importance * 2 ** (-age_days / max(item.draft.half_life_days, 0.001))
            return (float(memory_id in profiles), ranks[memory_id], activation, item.recorded_at.timestamp())

        selected: list[MemoryRecord] = []
        revisions: list[tuple[str, int]] = []
        used = 0
        for memory_id in sorted(ranks, key=lambda key: tuple(-value for value in priority(key)) + (key,)):
            current = self.repository.get(memory_id)
            if (current is None or current.revision != items[memory_id].revision
                    or not _eligible(current, self.clock(), include_local=not self.cloud, as_of=as_of)):
                warnings.append("retrieval_revision_changed: stale evidence skipped")
                continue
            record = _record(current, tuple(reasons[memory_id]))
            size = len(record.content.encode("utf-8"))
            if used + size > self.max_bytes:
                warnings.append("memory_budget_omitted: complete evidence did not fit")
                continue
            selected.append(record)
            revisions.append((memory_id, current.revision))
            used += size
            if len(selected) == self.limit:
                break
        final_epoch = self.repository.epoch
        if final_epoch != epoch or any(
            (current := self.repository.get(memory_id)) is None or current.revision != revision
            or not _eligible(current, self.clock(), include_local=not self.cloud, as_of=as_of)
            for memory_id, revision in revisions
        ):
            return RetrievalBundle(epoch=self.repository.epoch, warnings=tuple(dict.fromkeys(
                warnings + ["memory_changed_during_retrieval: retry required"])))
        return RetrievalBundle(tuple(selected), tuple(revisions), epoch,
                               tuple(dict.fromkeys(warnings)), used)


__all__ = ["MemoryRetriever", "profile_entries", "PROFILE_KEYS"]
