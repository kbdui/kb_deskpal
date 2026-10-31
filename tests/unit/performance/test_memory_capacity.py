"""Structural performance guards, with exact numerical and freshness checks."""

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from deskpal.memory.contracts import EmbeddingManifest, MemoryDraft, MemorySource, MemoryStatus
from deskpal.memory.embedding import EmbeddingError, normalized_vector
from deskpal.memory.repository import SQLiteMemoryRepository
from deskpal.memory.retrieval import MemoryRetriever, _cosine, profile_entries

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


class Embedding:
    manifest = EmbeddingManifest("synthetic-performance", 2, "synthetic-performance-v1")

    def __init__(self):
        self.batches = 0
        self.on_query = None

    def embed_documents(self, texts):
        self.batches += 1
        return tuple((3.0, 4.0) for _ in texts)

    def embed_query(self, text):
        if self.on_query:
            self.on_query()
        return (3.0, 4.0)


def save(repository, content, **fields):
    return repository.ingest(MemorySource(content, created_at=NOW),
                             MemoryDraft(content, content, **fields), accepted=True)


@pytest.mark.parametrize("corpus_size", [8, 120])
def test_warm_retrieval_has_bounded_sql_and_no_reembedding(corpus_size):
    with SQLiteMemoryRepository(":memory:", clock=lambda: NOW) as repository:
        for index in range(corpus_size):
            save(repository, f"合成检索性能记录{index}")
        embedding = Embedding()
        retriever = MemoryRetriever(repository, embedding=embedding, clock=lambda: NOW)
        assert retriever.ensure_index() == ()
        batches = embedding.batches
        statements = []
        repository._db.set_trace_callback(statements.append)
        assert len(retriever.retrieve("检索记录").records) == 5
        selects = [sql for sql in statements if sql.startswith("SELECT")]
        assert len(selects) <= 50  # independent of corpus size; selected heads are rechecked
        assert sum("FROM memory_embeddings e" in sql for sql in selects) == 1
        assert sum("source_payload FROM memory_revisions" in sql for sql in selects) == 1
        assert embedding.batches == batches
        statements.clear()
        assert profile_entries(repository, clock=lambda: NOW) == ()
        assert not any("SELECT payload FROM memory_sources" in sql for sql in statements)


def test_status_filter_does_not_decode_unrelated_history():
    with SQLiteMemoryRepository(":memory:", clock=lambda: NOW) as repository:
        accepted = save(repository, "合成已接纳记录")
        rejected = repository.ingest(MemorySource("合成候选", created_at=NOW), MemoryDraft("合成候选", "合成候选"))
        repository.reject(rejected.id, rejected.revision)
        repository._db.execute("UPDATE memory_revisions SET draft='invalid' WHERE status='rejected'")
        assert repository.list_items(status=MemoryStatus.ACCEPTED, include_history=True) == (accepted,)


def test_retrieval_snapshot_reloads_clock_expiry_and_other_connection_deletion(tmp_path):
    now = [NOW]
    path = tmp_path / "synthetic.sqlite3"
    with SQLiteMemoryRepository(path, clock=lambda: now[0]) as repository:
        record = save(repository, "合成有效期记录", expires_at=NOW + timedelta(seconds=1))
        embedding = Embedding()
        retriever = MemoryRetriever(repository, embedding=embedding, clock=lambda: now[0])
        assert retriever.retrieve("有效期").records
        now[0] += timedelta(seconds=1)
        assert not retriever.retrieve("有效期").records
        assert not repository.embeddings(embedding.manifest)
        now[0] = NOW
        with SQLiteMemoryRepository(path, clock=lambda: now[0]) as other:
            embedding.on_query = lambda: other.forget(record.id, record.revision)
            result = retriever.retrieve("有效期")
            assert not result.records
            assert "memory_changed_during_retrieval: retry required" in result.warnings
            embedding.on_query = None
            assert not retriever.retrieve("有效期").records


def test_cosine_is_bit_exact_to_original_normalization_and_dot():
    generator = random.Random(42)
    for dimensions in (2, 512):
        query = normalized_vector(tuple(generator.uniform(-1, 1) for _ in range(dimensions)), dimensions)
        for scale in (1e-100, 0.1, 1.0, 1e100):
            indexed = tuple(generator.uniform(-1, 1) * scale for _ in range(dimensions))
            document = normalized_vector(indexed, dimensions)
            expected = math.fsum(left * right for left, right in zip(query, document))
            assert _cosine(query, indexed) == expected


def test_cosine_threshold_boundary_and_twentieth_place_ties_match_original():
    query = (1.0, 0.0)
    threshold = 0.55
    scores = [math.nextafter(threshold, 0), threshold, math.nextafter(threshold, 1)]
    scores += [0.7] * 30 + [0.8, 0.9]
    indexed = [(f"record-{index:02}", (score * 3, math.sqrt(1 - score ** 2) * 3))
               for index, score in enumerate(scores)]

    def original(vector):
        document = normalized_vector(vector, 2)
        return math.fsum(left * right for left, right in zip(query, document))

    previous = [(id_, original(vector)) for id_, vector in indexed]
    optimized = [(id_, _cosine(query, vector)) for id_, vector in indexed]
    assert optimized == previous
    assert sorted((pair for pair in optimized if pair[1] >= threshold),
                  key=lambda pair: (-pair[1], pair[0]))[:20] == sorted(
                      (pair for pair in previous if pair[1] >= threshold),
                      key=lambda pair: (-pair[1], pair[0]))[:20]


@pytest.mark.parametrize("indexed", [(), (1.0,), (0.0, 0.0), (float("nan"), 1.0),
                                    (float("inf"), 1.0), (1e308, 1e308), (1e-300, 1e-300)])
def test_cosine_rejects_same_invalid_vectors_as_original(indexed):
    with pytest.raises(EmbeddingError):
        normalized_vector(indexed, 2)
    with pytest.raises(EmbeddingError):
        _cosine((0.6, 0.8), indexed)
