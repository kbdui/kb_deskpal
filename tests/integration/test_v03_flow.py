"""V0.3 composition against real temporary SQLite and captured model boundaries."""

import json

import pytest

import deskpal.application.factory as factory
from deskpal.config import load_config
from deskpal.llm.models import LLMResponse
from deskpal.memory.contracts import EmbeddingManifest, MemoryStatus, RevisionConflict, UsePolicy
from deskpal.memory.repository import SQLiteMemoryRepository
from deskpal.planner import Plan
from deskpal.tools import ToolCall
from deskpal.tasks import TaskStatus


class CapturedLLM:
    def __init__(self):
        self.calls = []
        self.steps = ()
        self.evaluation_error = False
        self.reflection_error = False

    def chat(self, messages, *, tools=()):
        messages = tuple(messages)
        self.calls.append((messages, tuple(tools)))
        system = messages[0].content
        if system.startswith("Extract optional"):
            if self.evaluation_error:
                raise TimeoutError("fake evaluation timeout")
            evidence = json.loads(messages[-1].content)["evidence"]
            return LLMResponse(json.dumps({"candidates": [{
                "content": evidence, "quote": evidence, "subject": "user", "predicate": "statement",
                "value": "", "scope": "global", "profile_key": "",
                "importance": .5, "extraction_confidence": .8,
            }]}, ensure_ascii=False))
        if system.startswith("[EXPRESSION]"):
            if self.reflection_error:
                raise TimeoutError("fake reflection timeout")
            return LLMResponse("已根据实际工具结果反馈")
        return LLMResponse("收到", tool_calls=self.steps)

    def generate(self, prompt):
        raise AssertionError("All model requests must be captured as chat")


class CapturedEmbedding:
    manifest = EmbeddingManifest("fake-local", 2, "v1")

    def __init__(self):
        self.queries, self.documents = [], []

    def embed_query(self, text):
        self.queries.append(text)
        return (1.0, 0.0)

    def embed_documents(self, texts):
        self.documents.extend(texts)
        return tuple((1.0, 0.0) for _ in texts)


@pytest.fixture
def application(tmp_path, monkeypatch):
    llm = CapturedLLM()
    monkeypatch.setattr(factory, "OfflineLLMProvider", lambda *args, **kwargs: llm)
    monkeypatch.setattr(factory, "OpenAILLMProvider", lambda *args, **kwargs: llm)
    agents = []

    def create(*, mode="explicit_only", cloud=False, embedding=None, planner=None):
        env = {"DESKPAL_LLM_PROVIDER": "openai" if cloud else "offline",
               "DESKPAL_LLM_MODEL": "fake-model", "OPENAI_API_KEY": "fake-test-credential",
               "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
               "DESKPAL_DATA_DIR": str(tmp_path / "data"), "DESKPAL_MEMORY_MODE": mode}
        settings = load_config(environ=env)
        agent = factory.create_agent(settings, embedding=embedding, planner=planner)
        agents.append(agent)
        return agent

    yield create, llm, tmp_path
    for agent in agents:
        agent.close()


def test_explicit_remember_never_calls_planner_evaluator_or_reflect_and_survives_reopen(application):
    create, llm, root = application
    with create(mode="candidate_review", cloud=True) as core:
        result = core.run("记住：请用中文回答")
        assert result.status == TaskStatus.COMPLETED and result.memory_effects
        assert llm.calls == []
        item, = core.memory_command("list_items")
        assert item.status == MemoryStatus.ACCEPTED
        assert item.source.task_id == result.task_id
        assert core.memory_command("profile")[0].value == "zh"
    with SQLiteMemoryRepository(root / "data" / "memory.sqlite3") as repository:
        restored, = repository.list_items()
        assert restored.id == item.id and restored.source.content == "请用中文回答"


@pytest.mark.parametrize("mode,expected_candidates", [("explicit_only", 0), ("paused", 0), ("candidate_review", 1)])
def test_learning_requires_opt_in_then_review_cas_correction_and_forget(application, mode, expected_candidates):
    create, llm, _ = application
    with create(mode=mode, cloud=True) as core:
        result = core.run("我喜欢茶")
        assert result.status == TaskStatus.COMPLETED
        candidates = core.memory_command("list_items", status=MemoryStatus.CANDIDATE)
        assert len(candidates) == expected_candidates
        assert len([messages for messages, _ in llm.calls if messages[0].content.startswith("Extract optional")]) == expected_candidates
        if not candidates:
            return
        candidate, = candidates
        assert core.memory.retrieve("茶").records == ()
        accepted = core.memory_command("accept", memory_id=candidate.id, expected_revision=candidate.revision)
        assert core.memory.retrieve("茶").records
        with pytest.raises(RevisionConflict):
            core.memory_command("correct", memory_id=accepted.id, expected_revision=candidate.revision, content="我喜欢咖啡")
        corrected = core.memory_command("correct", memory_id=accepted.id, expected_revision=accepted.revision, content="我喜欢咖啡")
        assert core.memory.retrieve("茶").records == ()
        assert core.memory.retrieve("咖啡").records
        with pytest.raises(RevisionConflict):
            core.memory_command("forget", memory_id=corrected.id, expected_revision=accepted.revision)
        receipt = core.memory_command("forget", memory_id=corrected.id, expected_revision=corrected.revision)
        assert receipt.memory_id == corrected.id
        assert core.memory.retrieve("咖啡").records == ()
        assert "咖啡" not in core.memory_command("export", include_history=True)


@pytest.mark.parametrize("action", ["correct", "forget"])
def test_changed_plan_evidence_revokes_grants_scrubs_task_and_cannot_execute(application, action):
    create, llm, root = application
    llm.steps = (ToolCall("echo", {"text": "automatic must wait"}, "auto"),
                 ToolCall("write_text", {"path": "茶.txt", "text": "old fact"}, "write"))
    with create() as core:
        item = core.memory_command("remember", content="我喜欢茶")
        waiting = core.run("茶")
        assert waiting.status == TaskStatus.WAITING
        assert not waiting.tool_results
        kwargs = {"memory_id": item.id, "expected_revision": item.revision}
        if action == "correct":
            kwargs["content"] = "我喜欢咖啡"
        core.memory_command(action, **kwargs)
        task = core.tasks.get(waiting.task_id)
        assert task.status == TaskStatus.CANCELLED
        assert task.request == "[forgotten]" and task.result is None
        with pytest.raises(ValueError):
            core.confirm_task(waiting.task_id, authorized_tools=("write_text",))
        assert not (root / "workspace" / "茶.txt").exists()
    entries = [json.loads(line) for line in (root / "data" / "execution.jsonl").read_text(encoding="utf-8").splitlines()]
    assert not any(entry["event"] == "tool_started" for entry in entries)


def test_local_only_never_enters_cloud_planner_reflect_evaluator_or_content_logs(application):
    create, llm, root = application
    llm.steps = (ToolCall("echo", {"text": "public result"}, "echo"),)
    with create(mode="candidate_review", cloud=True) as core:
        private = core.memory_command("remember", content="请叫我LOCAL_PRIVATE_MARKER", use_policy=UsePolicy.LOCAL_ONLY)
        core.memory_command("remember", content="PUBLIC_MEMORY_MARKER")
        assert llm.calls == []
        assert core.memory_command("profile")[0].value == "LOCAL_PRIVATE_MARKER"
        result = core.run("PUBLIC_MEMORY_MARKER")
        assert result.status == TaskStatus.COMPLETED
        sent = "\n".join(message.content for messages, _ in llm.calls for message in messages)
        assert "PUBLIC_MEMORY_MARKER" in sent
        assert "LOCAL_PRIVATE_MARKER" not in sent
        assert len(llm.calls) == 3  # Planner, independent Reflect, optional evaluation.
        assert any(item.id == private.id and item.source.use_policy == UsePolicy.LOCAL_ONLY
                   for item in core.memory_command("list_items"))
    assert "LOCAL_PRIVATE_MARKER" not in (root / "data" / "execution.jsonl").read_text(encoding="utf-8")


@pytest.mark.parametrize("fault", ["evaluation", "reflection"])
def test_optional_model_failure_preserves_completed_tool_and_durable_episode(application, fault):
    create, llm, root = application
    llm.steps = (ToolCall("echo", {"text": "actual output"}, "echo"),)
    llm.evaluation_error = fault == "evaluation"
    llm.reflection_error = fault == "reflection"
    with create(mode="candidate_review", cloud=True) as core:
        result = core.run("执行一次 echo")
        assert result.status == TaskStatus.COMPLETED and result.error is None
        assert len(result.tool_results) == 1 and result.tool_results[0].output == "actual output"
        assert result.warnings and result.memory_effects
        assert core.tasks.get(result.task_id).result["tool_results"][0]["output"] == "actual output"
        episodes = [item for item in core.memory_command("list_items") if item.source.origin == "tool_result"]
        assert len(episodes) == 1 and episodes[0].source.task_id == result.task_id
        expression_calls = [(messages, tools) for messages, tools in llm.calls
                            if messages[0].content.startswith("[EXPRESSION]")]
        assert len(expression_calls) == 1
        assert expression_calls[0][1] == ()
        if fault == "reflection":
            assert result.response == "工具结果（记录事实）：\n1. echo：成功。actual output"
            assert "Response generation failed; authoritative evidence is unchanged" in result.warnings
    events = [json.loads(line)["event"] for line in (root / "data" / "execution.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events.count("tool_started") == 1


def test_unrelated_memory_is_not_recent_fallback_and_explicit_index_rebuild_uses_injection(application):
    create, llm, _ = application
    with create() as core:
        core.memory_command("remember", content="无关事实苹果")
        assert core.run("天气预报").status == TaskStatus.COMPLETED
        assert "无关事实苹果" not in "\n".join(message.content for messages, _ in llm.calls for message in messages)
    embedding = CapturedEmbedding()
    with create(embedding=embedding) as core:
        assert core.memory_command("rebuild_index") == ()
        assert embedding.documents == ["无关事实苹果"]
        assert core.run("水果偏好").status == TaskStatus.COMPLETED
        assert embedding.queries


def test_cancellation_after_episode_commit_stops_optional_cloud_learning(application, monkeypatch):
    create, llm, _ = application
    llm.steps = (ToolCall("echo", {"text": "actual output"}, "echo"),)
    with create(mode="candidate_review", cloud=True) as core:
        original = core.memory.record_execution

        def commit_then_cancel(task_id, summary, **kwargs):
            item = original(task_id, summary, **kwargs)
            core.cancel_task(task_id)
            return item

        monkeypatch.setattr(core.memory, "record_execution", commit_then_cancel)
        result = core.run("我喜欢茶")
        assert result.status == TaskStatus.CANCELLED
        assert len(result.tool_results) == 1 and result.tool_results[0].success
        assert not any(messages[0].content.startswith("Extract optional") for messages, _ in llm.calls)
        assert core.memory_command("list_items", status=MemoryStatus.CANDIDATE) == ()
