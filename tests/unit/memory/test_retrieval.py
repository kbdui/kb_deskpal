"""No network/model downloads: deterministic retrieval and SDK boundary tests."""

import math
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from deskpal.memory.contracts import (
    ClaimType, EmbeddingManifest, MemoryDraft, MemoryError, MemoryItem, MemorySource,
    MemoryStatus, UsePolicy,
)
from deskpal.memory.embedding import EmbeddingError, FastEmbedProvider, QUERY_PREFIX, normalized_vector
from deskpal.memory.retrieval import MemoryRetriever, profile_entries

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def item(id="a", content="我喜欢咖啡", **changes):
    draft = MemoryDraft(content, content, extraction_confidence=0.9)
    source = MemorySource(content, id="source-" + id, created_at=NOW)
    result = MemoryItem(id, 1, MemoryStatus.ACCEPTED, source, draft, NOW, NOW)
    for key, value in changes.items():
        if key in MemoryDraft.__dataclass_fields__:
            result = replace(result, draft=replace(result.draft, **{key: value}))
        elif key in {"origin", "use_policy"}:
            result = replace(result, source=replace(result.source, **{key: value}))
        else:
            result = replace(result, **{key: value})
    return result


class Repo:
    def __init__(self, *items):
        self.items = {item.id: item for item in items}
        self.epoch = 1
        self.index = []
        self.on_put = None
        self.get_hook = None

    def list_items(self, *, status=None, include_history=False):
        return tuple(item for item in self.items.values() if status is None or item.status == status)

    def get(self, memory_id, *, revision=None):
        if self.get_hook:
            self.get_hook(memory_id)
        return self.items.get(memory_id)

    def put_embedding(self, memory_id, revision, manifest, vector):
        if self.on_put:
            self.on_put()
        current = self.get(memory_id)
        if current is None or current.revision != revision:
            return False
        self.index.append((manifest, memory_id, revision, vector))
        return True

    def embeddings(self, manifest):
        return tuple((id, revision, vector) for version, id, revision, vector in self.index if version == manifest)


class Embed:
    manifest = EmbeddingManifest("test", 2, "v1")

    def __init__(self):
        self.callback = None
        self.fail = False
        self.document_fail = False
        self.batches = []

    def embed_query(self, text):
        if self.fail:
            raise RuntimeError("PRIVATE_USER_TEXT")
        return (1.0, 0.0)

    def embed_documents(self, texts):
        self.batches.append(len(texts))
        if self.callback:
            self.callback()
        if self.fail or self.document_fail:
            raise RuntimeError("PRIVATE_USER_TEXT")
        return tuple((1.0, 0.0) for _ in texts)


def retrieve(repo, query, **options):
    return MemoryRetriever(repo, clock=lambda: NOW, **options).retrieve(query)


def test_short_chinese_and_no_recent_fallback():
    repo = Repo(item(), item("b", "我喜欢茶", importance=1.0))
    assert [record.id for record in retrieve(repo, "咖啡").records] == ["a"]
    assert not retrieve(repo, "天气预报").records
    assert not retrieve(repo, " ").records


@pytest.mark.parametrize("changes", [
    {"status": MemoryStatus.LEGACY}, {"status": MemoryStatus.DELETED},
    {"claim_type": ClaimType.LEGACY}, {"scope": "work"},
    {"valid_from": NOW + timedelta(seconds=1)}, {"expires_at": NOW},
])
def test_ineligible_never_enters_context(changes):
    assert not retrieve(Repo(item(**changes)), "咖啡").records


def test_half_open_validity_boundaries():
    assert retrieve(Repo(item(valid_from=NOW, valid_to=NOW + timedelta(seconds=1))), "咖啡").records


@pytest.mark.parametrize('status', [MemoryStatus.CANDIDATE, MemoryStatus.DISPUTED])
def test_review_buffer_is_available_with_explicit_uncertainty_status(status):
    result = retrieve(Repo(item(status=status, uncertainty='暂时推测')), '咖啡')
    assert len(result.records) == 1
    assert status.value in result.records[0].content
    assert '暂时推测' in result.records[0].content


@pytest.mark.parametrize('status', [MemoryStatus.CANDIDATE, MemoryStatus.DISPUTED, MemoryStatus.ACCEPTED])
def test_sqlite_semantic_recall_includes_uncertain_current_heads_without_promoting_them(tmp_path, status):
    """Use the real vector store, with a paraphrase that has no lexical match."""
    import json
    from deskpal.memory.repository import SQLiteMemoryRepository

    with SQLiteMemoryRepository(tmp_path / 'memory.sqlite3', clock=lambda: NOW) as repo:
        source = MemorySource('雨后沿河散步', created_at=NOW)
        draft = MemoryDraft('雨后沿河散步', source.content, claim_type=ClaimType.HYPOTHESIS,
                            uncertainty='可能的放松方式，尚未确认')
        saved = repo.ingest(source, draft)
        if status == MemoryStatus.DISPUTED:
            # Historical disputed heads remain readable, though new learning uses candidates.
            with repo.batch():
                repo._db.execute("UPDATE memory_revisions SET status='disputed' WHERE id=? AND revision=?",
                                 (saved.id, saved.revision))
            saved = repo.get(saved.id)
        elif status != MemoryStatus.CANDIDATE:
            saved = repo.update_understanding(saved.id, saved.revision, saved.source, saved.draft, status)
        embed = Embed()
        retriever = MemoryRetriever(repo, embedding=embed, clock=lambda: NOW)
        assert not MemoryRetriever(repo, clock=lambda: NOW).retrieve('户外休闲').records
        assert retriever.ensure_index() == ()
        assert repo.embeddings(embed.manifest) == ((saved.id, saved.revision, (1.0, 0.0)),)
        result = retriever.retrieve('户外休闲')
        assert [record.id for record in result.records] == [saved.id]
        evidence = json.loads(result.records[0].content)
        assert evidence['status'] == status.value
        assert evidence['claim_type'] == 'hypothesis' and evidence['verified'] is False
        assert evidence['uncertainty'] == draft.uncertainty and evidence['selected_by'] == ['semantic']
        assert repo.get(saved.id).status == status
        # Deleted sources cannot re-enter through the now broader vector eligibility.
        repo.forget(saved.id, saved.revision)
        assert repo.embeddings(embed.manifest) == ()
        assert retriever.retrieve('户外休闲').records == ()


def test_sqlite_uncertain_vectors_keep_source_policy_and_revision_checks(tmp_path):
    from deskpal.memory.repository import SQLiteMemoryRepository

    with SQLiteMemoryRepository(tmp_path / 'private.sqlite3', clock=lambda: NOW) as repo:
        source = MemorySource('雨后沿河散步', created_at=NOW, use_policy=UsePolicy.LOCAL_ONLY)
        saved = repo.ingest(source, MemoryDraft(source.content, source.content))
        embed = Embed()
        local = MemoryRetriever(repo, embedding=embed, cloud=False, clock=lambda: NOW)
        assert local.ensure_index() == ()
        assert local.retrieve('户外休闲').records
        assert MemoryRetriever(repo, embedding=embed, cloud=True, clock=lambda: NOW).retrieve('户外休闲').records == ()
        changed = repo.update_understanding(saved.id, saved.revision, saved.source, saved.draft, MemoryStatus.ACCEPTED)
        assert repo.embeddings(embed.manifest) == ()
        assert not repo.put_embedding(saved.id, saved.revision, embed.manifest, (1.0, 0.0))
        assert local.ensure_index() == ()
        assert [row[:2] for row in repo.embeddings(embed.manifest)] == [(changed.id, changed.revision)]


def test_general_recall_can_compare_a_past_interval_without_claiming_it_is_current():
    result = retrieve(Repo(item(valid_to=NOW)), '咖啡')
    assert result.records and NOW.isoformat() in result.records[0].content


def test_as_of_unknown_start_and_move_half_open_boundary():
    yesterday = NOW - timedelta(days=1)
    repo = Repo(item())
    retriever = MemoryRetriever(repo, clock=lambda: NOW)
    assert not retriever.retrieve("咖啡", as_of=yesterday).records
    old = item("old", "我住在北京", valid_from=NOW - timedelta(days=20), valid_to=yesterday)
    new = item("new", "我住在杭州", valid_from=yesterday)
    repo = Repo(old, new)
    retriever = MemoryRetriever(repo, clock=lambda: NOW)
    assert [record.id for record in retriever.retrieve("我住在", as_of=yesterday - timedelta(seconds=1)).records] == ["old"]
    assert [record.id for record in retriever.retrieve("我住在", as_of=yesterday).records] == ["new"]
    repo.items["old"] = replace(old, status=MemoryStatus.SUPERSEDED)
    assert not retriever.retrieve("北京", as_of=yesterday - timedelta(seconds=1)).records
    with pytest.raises(MemoryError, match="timezone-aware"):
        retriever.retrieve("咖啡", as_of=NOW.replace(tzinfo=None))


def test_historical_query_skips_current_profile_and_never_revives_expired_source():
    profile = item(profile_key="verbosity", value="brief")
    retriever = MemoryRetriever(Repo(profile), clock=lambda: NOW)
    assert not retriever.retrieve("天气", as_of=NOW).records
    expired = item(valid_from=NOW - timedelta(days=3), expires_at=NOW)
    retriever = MemoryRetriever(Repo(expired), clock=lambda: NOW)
    assert not retriever.retrieve("咖啡", as_of=NOW - timedelta(days=2)).records


def test_local_policy_applies_to_both_recall_and_profile():
    local = item(profile_key="verbosity", value="brief", use_policy=UsePolicy.LOCAL_ONLY)
    repo = Repo(local)
    assert not retrieve(repo, "咖啡").records
    assert not profile_entries(repo, clock=lambda: NOW)
    assert profile_entries(repo, clock=lambda: NOW, include_local=True)
    assert retrieve(repo, "无关内容", cloud=False).records


@pytest.mark.parametrize("changes", [
    {"claim_type": ClaimType.HYPOTHESIS}, {"subject": "friend"},
    {"origin": "imported_document"}, {"value": "unsupported"},
    {"profile_key": "permissions"},
])
def test_profile_rejects_non_user_or_invalid_projection(changes):
    fields = {"profile_key": "verbosity", "value": "brief", **changes}
    assert not profile_entries(Repo(item(**fields)), clock=lambda: NOW)


def test_exact_profile_load_conflicts_and_refs():
    profile = item(content="请用中文回答", profile_key="reply_language", value="zh")
    repo = Repo(profile)
    entries = profile_entries(repo, clock=lambda: NOW)
    assert [(entry.key, entry.value, entry.memory_id, entry.revision) for entry in entries] == [("reply_language", "zh", "a", 1)]
    bundle = retrieve(repo, "完全无关的新请求")
    assert bundle.revisions == (("a", 1),)
    assert '"profile"' in bundle.records[0].content
    assert '"source_origin":"user_message"' in bundle.records[0].content
    assert NOW.isoformat() in bundle.records[0].content
    assert not retrieve(repo, "").records
    repo.items["b"] = item("b", profile_key="reply_language", value="en")
    assert not profile_entries(repo, clock=lambda: NOW)


def test_importance_cannot_outvote_relevance_and_numeric_negation():
    repo = Repo(item("a", "咖啡预算100元", importance=0.1),
                item("b", "咖啡预算200元", importance=1.0),
                item("c", "我不喜欢咖啡", importance=0.9))
    budget = retrieve(repo, '咖啡预算100元').records
    negative = retrieve(repo, '我不喜欢咖啡').records
    assert budget[0].id == 'a' and {record.id for record in budget} >= {'a', 'b'}
    assert negative[0].id == 'c' and any(record.id == 'a' for record in negative)
    assert '我不喜欢咖啡' in negative[0].content


def test_budget_keeps_whole_record_and_accurate_utf8_count():
    repo = Repo(item())
    full = retrieve(repo, "咖啡")
    size = len(full.records[0].content.encode("utf-8"))
    assert full.bytes_used == size
    assert retrieve(repo, "咖啡", max_bytes=size).records == full.records
    too_small = retrieve(repo, "咖啡", max_bytes=size - 1)
    assert not too_small.records and too_small.bytes_used == 0
    assert any("budget" in warning for warning in too_small.warnings)


def test_semantic_threshold_manifest_and_stale_revision():
    repo, embedding = Repo(item()), Embed()
    retriever = MemoryRetriever(repo, embedding=embedding, clock=lambda: NOW)
    assert retriever.rebuild_index() == ()
    assert retrieve(repo, "拿铁", embedding=embedding).records
    repo.index.append((EmbeddingManifest("different", 2, "v2"), "a", 1, (1.0, 0.0)))
    embedding.manifest = EmbeddingManifest("test", 2, "v2")
    embedding.document_fail = True
    assert not retrieve(repo, "拿铁", embedding=embedding).records
    embedding.manifest = EmbeddingManifest("test", 2, "v1")
    repo.items["a"] = replace(repo.items["a"], revision=2, draft=replace(repo.items["a"].draft, content="我只喝茶"))
    assert not retrieve(repo, "拿铁", embedding=embedding).records
    repo.index = [(embedding.manifest, "a", 2, (0.7, math.sqrt(1 - 0.7 ** 2)))]
    assert not retrieve(repo, "拿铁", embedding=embedding, semantic_threshold=0.75).records
    assert retrieve(repo, "拿铁", embedding=embedding, semantic_threshold=0.65).records


def test_incremental_ensure_only_encodes_missing_revision_pairs():
    repo, embedding = Repo(item()), Embed()
    retriever = MemoryRetriever(repo, embedding=embedding, clock=lambda: NOW)
    assert retriever.retrieve("拿铁").records
    assert embedding.batches == [1]
    assert retriever.retrieve("拿铁").records
    assert embedding.batches == [1]
    repo.items["a"] = replace(repo.items["a"], revision=2)
    assert retriever.retrieve("拿铁").revisions == (("a", 2),)
    assert embedding.batches == [1, 1]


def test_semantic_failure_is_safe_lexical_degradation():
    embedding = Embed()
    embedding.fail = True
    bundle = retrieve(Repo(item()), "咖啡", embedding=embedding, startup_warnings=("cache_unavailable",))
    assert bundle.records and "cache_unavailable" in bundle.warnings
    assert any("semantic_failed" in warning for warning in bundle.warnings)
    assert "PRIVATE_USER_TEXT" not in repr(bundle)
    assert retrieve(Repo(), "", startup_warnings=("cache_unavailable",)).warnings == ("cache_unavailable",)


def test_rebuild_bounded_batch_and_revision_races():
    repo = Repo(*(item(str(index)) for index in range(19)))
    embedding = Embed()
    retriever = MemoryRetriever(repo, embedding=embedding, clock=lambda: NOW)
    assert retriever.rebuild_index() == ()
    assert max(embedding.batches) <= 8
    repo, embedding = Repo(item()), Embed()
    embedding.callback = lambda: repo.items.clear()
    warnings = MemoryRetriever(repo, embedding=embedding, clock=lambda: NOW).rebuild_index()
    assert not repo.index and any("revision_changed" in value for value in warnings)
    repo, embedding = Repo(item()), Embed()
    repo.on_put = lambda: repo.items.clear()
    warnings = MemoryRetriever(repo, embedding=embedding, clock=lambda: NOW).rebuild_index()
    assert not repo.index and warnings


def test_mutation_during_retrieval_returns_no_evidence():
    repo = Repo(item())
    def mutate(_):
        repo.epoch += 1
    repo.get_hook = mutate
    bundle = retrieve(repo, "咖啡")
    assert not bundle.records and not bundle.revisions
    assert any("changed_during" in warning for warning in bundle.warnings)


@pytest.mark.parametrize("options", [{"limit": 0}, {"limit": True}, {"max_bytes": -1},
    {"semantic_threshold": float("nan")}, {"semantic_threshold": 2}, {"cloud": 1}])
def test_constructor_validation(options):
    with pytest.raises(MemoryError):
        MemoryRetriever(Repo(), **options)


def fake_sdk(monkeypatch, tmp_path):
    import deskpal.memory.embedding as module
    root = tmp_path / "snapshot"
    root.mkdir()
    (root / "model.onnx").write_bytes(b"fixture-weights")
    (root / "tokenizer.json").write_text("{}", encoding="utf-8")
    calls = []
    class Tokenizer:
        @classmethod
        def from_str(cls, text): return cls()
        def to_str(self): return "{}"
        def no_truncation(self): pass
        def no_padding(self): pass
        def encode(self, text): return SimpleNamespace(ids=list(range(len(text) + 2)))
    class SDK:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.model = SimpleNamespace(_model_dir=root, tokenizer=Tokenizer())
        def query_embed(self, texts, **kwargs):
            calls.append(("query", texts, kwargs))
            return [(2.0,) + (0.0,) * 511]
        def passage_embed(self, texts, **kwargs):
            calls.append(("documents", texts, kwargs))
            return [(2.0,) + (0.0,) * 511 for text in texts]
    monkeypatch.setattr(module.importlib.metadata, "version", lambda _: "0.9.0")
    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=SDK))
    return root, calls


def test_fastembed_offline_cpu_prefix_manifest_and_close(monkeypatch, tmp_path):
    root, calls = fake_sdk(monkeypatch, tmp_path)
    provider = FastEmbedProvider(cache_dir=tmp_path)
    assert calls[0]["local_files_only"] is True
    assert calls[0]["providers"] == ["CPUExecutionProvider"]
    assert calls[0]["threads"] == 4
    assert provider.manifest.dimensions == 512
    assert "assets-sha256=" in provider.manifest.version
    first_manifest = provider.manifest
    assert math.isclose(sum(value * value for value in provider.embed_query("咖啡")), 1)
    assert calls[-1][1] == [QUERY_PREFIX + "咖啡"]
    provider.embed_documents(("咖啡", "茶"))
    assert calls[-1][1] == ["咖啡", "茶"]
    (root / "model.onnx").write_bytes(b"different-weights")
    other = FastEmbedProvider(cache_dir=tmp_path)
    assert first_manifest != other.manifest
    with pytest.raises(EmbeddingError, match="512 tokens"):
        provider.embed_documents(("字" * 513,))
    provider.close()
    provider.close()
    with pytest.raises(EmbeddingError, match="closed"):
        provider.embed_query("咖啡")
    with pytest.raises(EmbeddingError, match="closed"):
        provider.embed_documents(())


def test_missing_model_does_not_import_sdk(monkeypatch, tmp_path):
    import deskpal.memory.embedding as module
    def forbidden(*args):
        pytest.fail("SDK must not be imported for a missing cache")
    monkeypatch.setattr(module.importlib, "import_module", forbidden)
    with pytest.raises(EmbeddingError, match="cache is missing"):
        FastEmbedProvider(cache_dir=tmp_path / "missing")


@pytest.mark.parametrize("vector", [(0.0, 0.0), (float("nan"), 1), (float("inf"), 1), (1.0,)])
def test_invalid_vectors(vector):
    with pytest.raises(EmbeddingError):
        normalized_vector(vector, 2)
