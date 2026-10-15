"""Focused V0.3 review regressions; fake models and temporary SQLite only."""

import pytest

import deskpal.application.factory as factory
from deskpal.config import load_config
from deskpal.memory.contracts import EmbeddingManifest, MemoryDraft, MemorySource
from deskpal.memory.coordinator import MemoryCoordinator
from deskpal.memory.repository import SQLiteMemoryRepository


def test_foreign_owner_profile_does_not_conflict_with_local_explicit_preference(tmp_path):
    with SQLiteMemoryRepository(tmp_path / "memory.sqlite3") as repository:
        repository.ingest(
            MemorySource("请用英文回答", owner_id="another-owner"),
            MemoryDraft("请用英文回答", "请用英文回答", predicate="reply_language",
                        profile_key="reply_language", value="en"), accepted=True)
        runtime = MemoryCoordinator(repository)
        local = runtime.remember("请用中文回答")
        assert local.source.owner_id == "local"
        assert [(entry.key, entry.value) for entry in runtime.profile()] == [("reply_language", "zh")]
        assert len(repository.list_items()) == 2


@pytest.mark.parametrize("construction_failure", [False, True])
def test_factory_owned_embedding_and_repository_are_released_on_close_or_later_failure(
        tmp_path, monkeypatch, construction_failure):
    closed = []
    repositories = []
    repository_type = factory.SQLiteMemoryRepository

    class Repository(repository_type):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            repositories.append(self)

        def close(self):
            closed.append("repository")
            super().close()

    class Model:
        manifest = EmbeddingManifest("fake-local", 2, "factory-review-v1")

        def __init__(self, **kwargs):
            self.closed = False

        def embed_query(self, text):
            return (1.0, 0.0)

        def embed_documents(self, texts):
            return tuple((1.0, 0.0) for _ in texts)

        def close(self):
            self.closed = True
            closed.append("embedding")

    monkeypatch.setattr(factory, "SQLiteMemoryRepository", Repository)
    monkeypatch.setattr(factory, "FastEmbedProvider", Model)
    if construction_failure:
        def fail_evaluator(*args, **kwargs):
            raise RuntimeError("synthetic evaluator construction failure")
        monkeypatch.setattr(factory, "LLMMemoryEvaluator", fail_evaluator)
    config = load_config(environ={
        "DESKPAL_LLM_PROVIDER": "offline", "DESKPAL_MEMORY_MODE": "candidate_review",
        "DESKPAL_EMBEDDING_PROVIDER": "fastembed", "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "DESKPAL_DATA_DIR": str(tmp_path / "data"),
    })
    if construction_failure:
        with pytest.raises(RuntimeError, match="synthetic evaluator"):
            factory.create_agent(config)
    else:
        core = factory.create_agent(config)
        core.close()
        core.close()
        with pytest.raises(RuntimeError):
            core.memory_command("list_items")
    assert closed == ["embedding", "repository"]
    with pytest.raises(RuntimeError, match="closed"):
        repositories[0].list_items()
