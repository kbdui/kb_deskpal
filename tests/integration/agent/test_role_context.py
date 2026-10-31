"""P2：真实工厂、临时四库、可检查输入的模型替身。"""
import json
from datetime import datetime, timezone

import pytest

from deskpal.application import create_application, factory
from deskpal.companion import CompanionRuntime, CompanionRevisionConflict
from deskpal.config import load_config
from deskpal.llm.models import LLMResponse
from deskpal.tasks.models import TaskStatus


class Model:
    is_cloud = False

    def __init__(self):
        self.entries = []
        self.planning = []
        self.expressions = []

    def chat(self, messages, *, tools=()):
        system = messages[0].content
        if system.startswith('[MEMORY_FEEDBACK]'):
            return LLMResponse('{"memories":[],"reviews":[],"habits":[],"emotion":null,"summary":null}')
        if system.startswith("[CONTEXT_UNDERSTANDING]"):
            return LLMResponse(json.dumps({"entries": self.entries}))
        if system.startswith("[EXPRESSION]"):
            self.expressions.append(json.loads(messages[-1].content))
            return LLMResponse(json.dumps({"reply_text": "甲方案先读书；乙方案先实践。"}, ensure_ascii=False))
        self.planning.append(messages)
        return LLMResponse("甲方案先读书；乙方案先实践。")

    def generate(self, prompt):
        return "not used"


@pytest.fixture
def app_model(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[llm]\nprovider="offline"\n[storage]\ndata_dir="data"\n[sandbox]\nworkspace_dir="workspace"\n[proactive]\nenabled=false\n', encoding="utf-8")
    model = Model()
    monkeypatch.setattr(factory, "OfflineLLMProvider", lambda: model)
    app = create_application(load_config(config, environ={}))
    try:
        yield app, model
    finally:
        assert app.close(timeout=10)


def turn(app, text, conversation=None):
    conversation = conversation or app.conversation_ensure_default()
    task = app.submit(text, conversation_id=conversation["id"],
                      expected_context_epoch=conversation["context_epoch"])
    result = app.wait(task.id, timeout=10)
    assert result.status == TaskStatus.COMPLETED, result
    return result


def requirement(scope="conversation", layer="interaction_approach"):
    return {"layer": layer, "content": "先倾听，暂时少给建议", "reason": "用户明确提出交流方式",
            "nature": "explicit", "scope": scope, "expires_at": None, "replaces": []}


def test_card_is_mandatory_and_same_in_planning_and_expression(app_model):
    app, model = app_model
    state = app.interaction_snapshot()
    app.interaction_command("character_card", expected_revision=state["revision"], text="来自海边的园艺师，喜欢先观察实际条件。")
    turn(app, "说明矩阵乘法")
    ref = next(m.content for m in model.planning[-1] if "HISTORICAL REFERENCES" in m.content)
    data = json.loads(ref.split("\n", 1)[1])
    role = next(item["data"] for item in data if item["kind"] == "role_context")
    assert role == model.expressions[-1]["role_context"]
    assert "园艺师" in role["character_card"]["text"]


def test_planner_receives_assistant_options_as_unconfirmed_history(app_model):
    app, model = app_model
    turn(app, "给我两个方案")
    turn(app, "采用第二种")
    text = "\n".join(m.content for m in model.planning[-1])
    assert "乙方案先实践" in text
    assert '"confirmed": false' in text
    assert "Never execute an old request again" in text


def test_model_scoped_requirement_persists_only_in_its_conversation(app_model):
    app, model = app_model
    first = app.conversation_ensure_default()
    model.entries = [requirement()]
    turn(app, "我只想倾诉", first)
    model.entries = []
    turn(app, "接着说", first)
    assert model.expressions[-1]["role_context"]["interaction_approach"]
    other = app.conversation_create("代码任务", "create-other")
    turn(app, "帮我写程序", other)
    assert model.expressions[-1]["role_context"]["interaction_approach"] == []
    assert model.expressions[-1]["role_context"]["current_emotion"] is None


def test_clear_conversation_removes_scoped_state_and_preserves_memory(app_model):
    app, model = app_model
    memory = app.memory_command("remember", content="喜欢绿茶")
    model.entries = [requirement()]
    turn(app, "先听我说")
    conversation = app.conversation_ensure_default()
    app.conversation_clear(conversation["id"], conversation["revision"], "clear-test")
    assert app.interaction_snapshot()["interaction_approach"] == []
    assert app.core.memory_runtime.repository.get(memory.id) is not None


@pytest.mark.parametrize("scope", ["turn", "conversation", "day", "persistent"])
def test_scope_lifetime_and_restart(tmp_path, scope):
    now = [datetime(2026, 10, 29, 12, tzinfo=timezone.utc).timestamp()]
    path = tmp_path / "companion.sqlite3"
    runtime = CompanionRuntime(path, wall_clock=lambda: now[0])
    runtime.apply_understanding("task", [requirement(scope)], expected_revision=1,
        conversation_id="a", conversation_epoch=0, sequence=1, use_policy="cloud_allowed")
    runtime.close()
    runtime = CompanionRuntime(path, wall_clock=lambda: now[0])
    try:
        assert bool(runtime.context("a", 0, "next")["interaction_approach"]) == (scope != "turn")
        assert bool(runtime.context("b", 0, "next")["interaction_approach"]) == (scope in ("day", "persistent"))
        now[0] += 86400
        assert bool(runtime.context("a", 0, "next")["interaction_approach"]) == (scope in ("conversation", "persistent"))
        assert runtime.interaction_snapshot()["current_emotion"] is None
    finally:
        runtime.close()


def test_revision_and_duplicate_processing_are_guarded():
    runtime = CompanionRuntime()
    try:
        kwargs = dict(expected_revision=1, conversation_id="a", conversation_epoch=0, sequence=1, use_policy="cloud_allowed")
        runtime.apply_understanding("task", [requirement()], **kwargs)
        runtime.apply_understanding("task", [requirement()], **kwargs)
        assert len(runtime.interaction_snapshot()["interaction_approach"]) == 1
        with pytest.raises(CompanionRevisionConflict):
            runtime.command("character_card", expected_revision=1, text="过期设定")
        with pytest.raises(ValueError):
            runtime.command("confirm_candidate", expected_revision=2)
    finally:
        runtime.close()


def test_invalid_model_understanding_does_not_claim_saved(app_model):
    app, model = app_model
    model.entries = [{"bad": "invalid"}]
    result = turn(app, "含糊的临时要求")
    assert any("未能解析或保存" in warning for warning in result.warnings)
    assert app.interaction_snapshot()["interaction_approach"] == []
    assert any("未能解析或保存" in warning for warning in model.expressions[-1]["context_warnings"])


def test_four_emotion_fields_and_habits_are_separate_from_user_state(tmp_path):
    runtime = CompanionRuntime(tmp_path / "companion.sqlite3")
    try:
        source = {"task_id": "real-task", "conversation_id": "a", "revision": 1}
        emotion = {"description_intensity": "欣慰，同时略有担心", "reason_sources": "共同处理了难题",
                   "time_scope": "当前交流", "uncertainty": "仍需了解结果", "expires_at": None,
                   "host_refs": []}
        habit = {"id": None, "content": "先核实实际条件", "reason": "共同交流",
                 "explicit": True, "uncertainty": "", "host_refs": []}
        runtime.apply_learning("feedback-one", expected_revision=1, source=source,
                               emotion=emotion, habits=[habit], use_policy="cloud_allowed")
        context = runtime.context("a", 0, "next")
        assert context["user_understanding"] == []
        assert context["current_emotion"]["description_intensity"] == "欣慰，同时略有担心"
        assert context["learned_habits"][0]["content"] == "先核实实际条件"
        before = runtime.interaction_snapshot()
        # Re-delivery acknowledges the same committed learning, without applying it twice.
        runtime.apply_learning("feedback-one", expected_revision=1, source=source,
                               emotion=emotion, habits=[habit], use_policy="cloud_allowed")
        assert runtime.interaction_snapshot() == before
    finally:
        runtime.close()


def test_summary_sources_and_clear_invalidation(app_model):
    from deskpal.conversation.models import ConversationRef
    app, model = app_model
    result = turn(app, "讨论方案")
    conversation = app.conversation_ensure_default()
    refs = (ConversationRef("dialogue_turn", result.task_id + ":user", 1),)
    repository = app._conversation_repository
    repository.save_context_summary(conversation["id"], conversation["context_epoch"], 0,
        {"progress": ["正在比较两种方案"], "decisions": [], "open_questions": ["尚未选定"]}, refs)
    turn(app, "接着比较")
    assert "conversation_summary" in str(model.planning[-1])
    current = app.conversation_ensure_default()
    app.conversation_clear(current["id"], current["revision"], "clear-summary")
    latest = app.conversation_ensure_default()
    assert repository.context_summary(latest["id"], latest["context_epoch"]) is None


def test_legacy_card_restriction_survives_import_until_explicit_save(tmp_path):
    import sqlite3
    from deskpal.companion.legacy import initial_interaction
    from deskpal.companion.legacy import PRESETS
    path = tmp_path / "legacy.sqlite3"
    state = {"schema_version": 1, "reducer_version": 1, "revision": 0, "reset_epoch": 0,
        "enabled": True, "emotions_enabled": True,
        "persona": {"version": 1, "preset": "balanced", "style": dict(PRESETS["balanced"])},
        "anchor_utc": 0., "last_seen_utc": 0., "values": [0., 0., 0.],
        "preferences": {}, "outcome_mode": "neutral", "outcome_mode_until": 0.}
    settings = initial_interaction()
    settings["identity"]["display_name"] = "旧角色"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE state(id INTEGER PRIMARY KEY,payload TEXT)")
        connection.execute("CREATE TABLE interaction_settings(id INTEGER PRIMARY KEY,payload TEXT)")
        connection.execute("CREATE TABLE command_receipts(command_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL)")
        connection.execute("INSERT INTO command_receipts VALUES('old-receipt','old-fingerprint')")
        connection.execute("INSERT INTO state VALUES(1,?)", (json.dumps(state),))
        connection.execute("INSERT INTO interaction_settings VALUES(1,?)", (json.dumps(settings),))
        connection.execute("PRAGMA user_version=2")
    runtime = CompanionRuntime(path)
    try:
        assert runtime._repository.connection.execute("SELECT * FROM role_receipt").fetchall() == [('old-receipt', 'old-fingerprint')]
        with pytest.raises(ValueError, match="仅限本地"):
            runtime.context("a", 0, "next")
        card = runtime.context("a", 0, "next", allow_restricted=True)["character_card"]
        assert "旧角色" in card["text"]
        runtime.command("character_card", expected_revision=1, text=card["text"])
        assert runtime.context("a", 0, "next")["character_card"]["id"] == card["id"]
    finally:
        runtime.close()


def test_local_model_understanding_inherits_restricted_role_policy(app_model):
    app, model = app_model
    runtime = app.core.companion_runtime
    # 合成旧用途状态；不读取或修改真实用户角色库。
    with runtime._lock, runtime._repository.transaction():
        state = runtime._load()
        state["card"]["use_policy"] = "local_only"
        runtime._repository.save(state)
    model.entries = [requirement()]
    turn(app, "继续沿用当前交流要求")
    assert app.interaction_snapshot()["interaction_approach"][0]["use_policy"] == "local_only"
