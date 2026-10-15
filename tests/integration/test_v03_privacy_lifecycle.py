"""Regression cases from independent V0.3 lifecycle/privacy review."""

from datetime import datetime, timedelta, timezone

import pytest

from deskpal.agent.core import AgentCore
from deskpal.llm.models import LLMResponse
from deskpal.memory.contracts import MemoryDraft, MemorySource, MemoryStatus, EmbeddingManifest
from deskpal.memory.coordinator import MemoryCoordinator
from deskpal.memory.repository import SQLiteMemoryRepository
from deskpal.memory.retrieval import MemoryRetriever
from deskpal.planner.models import Plan
from deskpal.sandbox.workspace import WorkspaceSandbox
from deskpal.tasks.manager import InMemoryTaskManager
from deskpal.tasks.models import TaskStatus
from deskpal.tools.manager import BuiltinToolManager


NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


class Clock:
    now = NOW

    def __call__(self):
        return self.now


class Planner:
    def __init__(self):
        self.calls = []

    def plan(self, request, *, memories=(), tools=()):
        self.calls.append((request, memories))
        return Plan("synthetic reply")


class NoLLM:
    def chat(self, *args, **kwargs):
        raise AssertionError("No cloud request was expected")


class Evaluator:
    def evaluate(self, source):
        return (MemoryDraft(source.content, source.content),)


class Log:
    def __init__(self, callback=None):
        self.records = []
        self.callback = callback

    def record(self, event, task_id=None, data=None):
        self.records.append((event, task_id, data))
        if self.callback:
            self.callback(event, data or {})


def core_for(root, memory, planner, log):
    tools = BuiltinToolManager(WorkspaceSandbox(root))
    tools.register_builtin()
    return AgentCore(llm=NoLLM(), planner=planner, tasks=InMemoryTaskManager(), memory=memory,
                     memory_runtime=memory, tools=tools, workspace_dir=root, execution_log=log)


def test_explicit_credential_like_memory_rejected_without_body_in_audit_or_task(tmp_path):
    with SQLiteMemoryRepository(":memory:") as repo:
        memory, planner, log = MemoryCoordinator(repo), Planner(), Log()
        with core_for(tmp_path, memory, planner, log) as core:
            marker = "sk-syntheticSensitiveValue123"
            result = core.run("记住：api_key=" + marker)
            assert result.status == TaskStatus.FAILED
            assert not memory.list_items() and not planner.calls
            assert marker not in repr(log.records)
            assert marker not in repr(core.tasks.list_tasks())


def test_expiry_at_planning_boundary_prevents_any_planner_call(tmp_path):
    clock = Clock()
    with SQLiteMemoryRepository(":memory:", clock=clock) as repo:
        item = repo.ingest(MemorySource("EXPIRING_MARKER", created_at=NOW),
                           MemoryDraft("EXPIRING_MARKER", "EXPIRING_MARKER",
                                       expires_at=NOW + timedelta(seconds=2)), accepted=True)
        memory = MemoryCoordinator(repo, clock=clock)

        def advance(event, data):
            if event == "stage_started" and data.get("stage") == "Think":
                clock.now += timedelta(seconds=3)

        planner = Planner()
        with core_for(tmp_path, memory, planner, Log(advance)) as core:
            result = core.run(item.draft.content)
            assert result.status == TaskStatus.FAILED and not planner.calls


def test_ttl_then_forget_still_scrubs_original_task_payload(tmp_path):
    clock = Clock()
    with SQLiteMemoryRepository(":memory:", clock=clock) as repo:
        memory = MemoryCoordinator(repo, evaluator=Evaluator(), mode="candidate_review", clock=clock)
        with core_for(tmp_path, memory, Planner(), Log()) as core:
            result = core.run("PRIVATE_CANDIDATE_SOURCE")
            candidate, = memory.list_items(status=MemoryStatus.CANDIDATE)
            clock.now += timedelta(days=8)
            assert repo.get(candidate.id).status == MemoryStatus.DELETED
            receipt = core.memory_command("forget", memory_id=candidate.id, expected_revision=candidate.revision)
            assert result.task_id in receipt.affected_task_ids
            assert core.tasks.get(result.task_id).request == "[forgotten]"
            assert core.tasks.get(result.task_id).result is None


def test_temporal_historical_semantic_index_remains_usable_without_reviving_current(tmp_path):
    clock = Clock()

    class Embedding:
        manifest = EmbeddingManifest("synthetic", 2, "temporal-test-v1")

        def embed_documents(self, texts):
            return tuple((1.0, 0.0) for _ in texts)

        def embed_query(self, text):
            return (1.0, 0.0)

    with SQLiteMemoryRepository(tmp_path / "temporal.sqlite3", clock=clock) as repo:
        old = repo.ingest(MemorySource("用户在茶室放松", created_at=NOW),
                          MemoryDraft("用户在茶室放松", "用户在茶室放松", valid_from=NOW), accepted=True)
        clock.now += timedelta(days=1)
        new = repo.revise(old.id, 1, MemorySource("用户改在公园散步", created_at=clock.now),
                          MemoryDraft("用户改在公园散步", "用户改在公园散步", valid_from=clock.now), temporal=True)
        retriever = MemoryRetriever(repo, embedding=Embedding(), clock=clock)
        bundle = retriever.retrieve("缓解压力方法", as_of=NOW + timedelta(hours=1))
        assert bundle.revisions == ((old.id, 2),)
        assert "semantic" in bundle.records[0].content
        repo.forget(new.id, new.revision)
        assert not retriever.retrieve("缓解压力方法").records


def test_live_api_script_default_gate_does_not_read_configuration(monkeypatch):
    from scripts import check_memory_api

    monkeypatch.setattr("sys.argv", ["check_memory_api.py"])

    def forbidden(*args, **kwargs):
        raise AssertionError("Default gate must not read credentials")

    monkeypatch.setattr(check_memory_api, "load_config", forbidden)
    with pytest.raises(SystemExit) as error:
        check_memory_api.main()
    assert error.value.code == 2


def test_default_retrieval_does_not_merge_foreign_owner_namespace():
    with SQLiteMemoryRepository(":memory:") as repo:
        repo.ingest(MemorySource("FOREIGN_NAMESPACE_MARKER", owner_id="other-owner"),
                    MemoryDraft("FOREIGN_NAMESPACE_MARKER", "FOREIGN_NAMESPACE_MARKER"), accepted=True)
        assert not MemoryRetriever(repo).retrieve("FOREIGN_NAMESPACE_MARKER").records


def test_local_cli_memory_management_needs_no_cloud_credential(tmp_path, monkeypatch, capsys):
    from deskpal.application.cli import main

    config = tmp_path / "api.toml"
    config.write_text('[llm]\nprovider="openai"\napi_key_env="UNSET_TEST_KEY"\n'
                      '[storage]\ndata_dir="local-data"\n[sandbox]\nworkspace_dir="local-workspace"\n', encoding="utf-8")
    monkeypatch.delenv("UNSET_TEST_KEY", raising=False)
    assert main(["--config", str(config), "--memory", "remember", "--content", "本地管理记录"]) == 0
    assert "accepted" in capsys.readouterr().out
    assert (tmp_path / "local-data" / "memory.sqlite3").exists()
