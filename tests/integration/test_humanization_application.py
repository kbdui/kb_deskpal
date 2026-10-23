"""HM02-HM05 production-entry acceptance, using only temporary synthetic data.

No network provider is constructed. Barriers cap waits at ten seconds. Private
store reads below inspect durable evidence; concurrency always enters through
ApplicationService, including the public request_forget privacy barrier.
"""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from threading import Event
from time import monotonic
from uuid import UUID

import pytest

from deskpal.application.desktop import DesktopFacade
from deskpal.application.factory import create_application
from deskpal.companion import CompanionStorageError
from deskpal.config import LLMConfig, StorageConfig, load_config
from deskpal.memory.contracts import MemoryError, MemoryStatus
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolCall
from deskpal.llm.models import LLMResponse
from deskpal.workflow.models import WorkflowBlocked


WAIT = 10
DRAFT = "HM_NEUTRAL_DRAFT"
REPLY = "HM_UNVERIFIED_ASSISTANT_REPLY"
PRIVATE = "HM_PRIVATE_SOURCE_1849"


def envelope(reply=REPLY, *, cues=(), preferences=()):
    return json.dumps({"reply_text": reply, "cues": list(cues),
                       "preference_candidates": list(preferences)}, ensure_ascii=False)


class HumanizationRecordingProvider:
    """Record all provider exits, rejecting generate and hidden repair traffic."""

    def __init__(self):
        self.calls = []
        self.plans = {}
        self.replies = {}
        self.block = None
        self.entered = Event()
        self.release = Event()

    def chat(self, messages, *, tools=()):
        system = messages[0].content
        stage = ("expression" if system.startswith("[EXPRESSION]") else
                 "memory_evaluation" if system.startswith("Extract optional long-term memory") else
                 "planning")
        entry = {"stage": stage, "messages": [asdict(item) for item in messages],
                 "tools": [asdict(item) for item in tools]}
        self.calls.append(deepcopy(entry))
        if stage == self.block:
            self.entered.set()
            if not self.release.wait(WAIT):
                raise TimeoutError("synthetic model barrier expired")
        if stage == "planning":
            return LLMResponse(DRAFT, self.plans.get(messages[-1].content, ()))
        if stage == "memory_evaluation":
            return LLMResponse('{"candidates":[]}')
        request = json.loads(messages[-1].content)["request"]
        selected = self.replies.get(request)
        if isinstance(selected, Exception):
            raise selected
        if isinstance(selected, LLMResponse):
            return selected
        structured = "OUTPUT PROTOCOL" in system
        return LLMResponse(selected if selected is not None else envelope() if structured else REPLY)

    def generate(self, prompt):
        self.calls.append({"stage": "generate", "prompt": prompt})
        raise AssertionError("unexpected generate exit")


@pytest.fixture
def hm_config(tmp_path):
    return load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
        "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "DESKPAL_DATA_DIR": str(tmp_path / "data")})


@pytest.fixture
def wire(monkeypatch):
    from deskpal.application import factory

    provider = HumanizationRecordingProvider()
    monkeypatch.setattr(factory, "OfflineLLMProvider", lambda: provider)
    monkeypatch.setattr(factory, "OpenAILLMProvider", lambda *args, **kwargs: provider)
    return provider


def eventually(check, message):
    deadline = monotonic() + WAIT
    tick = Event()
    while not check():
        remaining = deadline - monotonic()
        if remaining <= 0:
            pytest.fail(message)
        tick.wait(min(0.01, remaining))


def idle(app):
    eventually(lambda: app.diagnostics()["dispatcher"]["active_task_id"] is None,
               "application did not leave its active task")


def run_turn(app, request, *, conversation=None, **parameters):
    if conversation is not None:
        parameters.update(conversation_id=conversation["conversation_id"],
                          expected_conversation_epoch=conversation["epoch"])
    task = app.submit(request, **parameters)
    result = app.wait(task.id, WAIT)
    idle(app)
    return result


def configure(app, **parameters):
    return app.companion_command("configure", expected_revision=app.companion_snapshot()["revision"],
                                 **parameters)


def interact(app, action, **parameters):
    return app.interaction_command(action, expected_revision=app.interaction_snapshot()["revision"],
                                   **parameters)


def companion_command(app, action, **parameters):
    return app.companion_command(action, expected_revision=app.companion_snapshot()["revision"],
                                 **parameters)


def stages(wire):
    return [call["stage"] for call in wire.calls]


def last_expression(wire):
    return next(call for call in reversed(wire.calls) if call["stage"] == "expression")


def cloud(config):
    return replace(config, llm=LLMConfig(provider="openai", model="synthetic", api_key="synthetic"))


def test_hm02_same_intent_exact_plan_hash_across_personality_and_cue(hm_config, wire, monkeypatch):
    from deskpal.workflow import tasks

    # Independent stores with one stable synthetic root make full canonical and
    # hash equality meaningful without removing any authority-bearing field.
    monkeypatch.setattr(tasks, "uuid4", lambda: UUID("12345678-1234-4321-9876-123456789abc"))
    request = "neutral synthetic write and delete"
    wire.plans[request] = (
        ToolCall("write_text", {"path": "created.txt", "text": "exact bytes"}, "write"),
        ToolCall("document_delete", {"path": "victim.txt"}, "delete"))
    observations = []
    for index, (preset, cue) in enumerate((("warm", "supportive"), ("concise", "focused"))):
        settings = replace(hm_config, storage=StorageConfig(hm_config.storage.data_dir / str(index)))
        wire.calls.clear()
        with create_application(settings) as app:
            configure(app, enabled=True, emotions_enabled=True, preset=preset)
            companion_command(app, "cue", mode=cue, ttl_seconds=600)
            result = run_turn(app, request)
            assert result.status == TaskStatus.WAITING
            original = app.journal.get_plan(result.task_id)
            assert sha256(original.canonical_json.encode()).hexdigest() == original.hash
            observations.append((deepcopy(wire.calls), original.canonical_json, original.hash,
                result.planned_calls, result.authorization_requests, result.required_authorizations,
                result.required_high_risk_authorizations))
            assert result.required_authorizations == ("write_text", "document_delete")
            assert result.required_high_risk_authorizations == ("document_delete",)
            configure(app, preset="balanced")
            companion_command(app, "cue", mode="neutral", ttl_seconds=60)
            app.enqueue_confirmation(result.task_id, plan_hash=original.hash,
                                     command_id="same-waiting-confirmation")
            assert app.wait(result.task_id, WAIT).status == TaskStatus.WAITING
            idle(app)
            assert app.journal.get_plan(result.task_id) == original
            assert stages(wire) == ["planning"]
            assert not app.journal.attempts(result.task_id)
    assert observations[0] == observations[1]
    assert not (hm_config.sandbox.workspace_dir / "created.txt").exists()


def test_hm02_multiturn_has_neutral_and_expression_projections_and_session_isolation(hm_config, wire):
    initial = "synthetic project uses folder Maple"
    wire.plans[initial] = (ToolCall("echo", {"text": "verified folder Maple"}, "fact"),)
    with create_application(hm_config) as app:
        configure(app, enabled=True)
        conversation = app.conversation_open()
        first = run_turn(app, initial, conversation=conversation)
        assert first.status == TaskStatus.COMPLETED
        wire.calls.clear()
        second = run_turn(app, "rewrite that project explanation", conversation=conversation)
        assert second.status == TaskStatus.COMPLETED
        assert stages(wire) == ["planning", "expression"]
        planner = json.dumps(wire.calls[0], ensure_ascii=False)
        expression = json.loads(last_expression(wire)["messages"][-1]["content"])
        assert initial in planner and "tool_fact" in planner
        assert REPLY not in planner and "assistant_unverified" not in planner
        assert any(item["content"] == first.response and item["source_kind"] == "assistant_proposal"
                   for item in expression["history"])
        planning = app._humanization.context(second.task_id, "planning")
        presented = app._humanization.context(second.task_id, "expression")
        assert any(ref[1] == first.task_id + ":user" for ref in planning["refs"])
        assert not any(ref[1].endswith(":assistant") for ref in planning["refs"])
        assert any(ref[1] == first.task_id + ":assistant" for ref in presented["refs"])
        assert presented["items"] == []
        other = app.conversation_open()
        wire.calls.clear()
        assert run_turn(app, "independent neutral question", conversation=other).status == TaskStatus.COMPLETED
        assert initial not in json.dumps(wire.calls, ensure_ascii=False)
        assert REPLY not in json.dumps(wire.calls[0], ensure_ascii=False)
        assert json.loads(last_expression(wire)["messages"][-1]["content"])["history"] == []


@pytest.mark.parametrize("category", ["custom_persona", "preferences", "cue", "affect", "history"])
def test_hm03_cloud_category_allow_revoke_and_source_restrictions(hm_config, wire, category):
    with create_application(cloud(hm_config)) as app:
        configure(app, enabled=True, emotions_enabled=True, preset="balanced")
        assert not any(app.interaction_snapshot()["cloud_permissions"].values())
        conversation = app.conversation_open()
        if category == "custom_persona":
            configure(app, style={key: 1.0 for key in
                ("formality", "warmth", "humor", "verbosity", "expressiveness", "curiosity")})
            interact(app, "identity", display_name="HM_CLOUD_PERSONA")
        elif category == "preferences":
            companion_command(app, "preference", scope="persistent", verbosity="brief")
        elif category == "cue":
            companion_command(app, "cue", mode="supportive", ttl_seconds=600)
        elif category == "affect":
            wire.plans["synthetic missing document"] = (
                ToolCall("read_text", {"path": "absent.txt"}, "missing"),)
            result = run_turn(app, "synthetic missing document")
            assert result.status == TaskStatus.FAILED
            eventually(lambda: not app._humanization.pending_effects(), "effect was not delivered")
        else:
            assert run_turn(app, "HM_HISTORY_MARKER", conversation=conversation).status == TaskStatus.COMPLETED
        wire.calls.clear()
        assert run_turn(app, "neutral category sample", conversation=conversation).status == TaskStatus.COMPLETED
        denied = deepcopy(last_expression(wire))
        interact(app, "cloud_permissions", permissions={category: True})
        wire.calls.clear()
        assert run_turn(app, "neutral category sample", conversation=conversation).status == TaskStatus.COMPLETED
        allowed = deepcopy(last_expression(wire))
        if category == "custom_persona":
            assert "HM_CLOUD_PERSONA" not in json.dumps(denied)
            assert "HM_CLOUD_PERSONA" in json.dumps(allowed)
        elif category == "preferences":
            assert "篇幅简洁" not in denied["messages"][0]["content"]
            assert "篇幅简洁" in allowed["messages"][0]["content"]
        elif category in {"cue", "affect"}:
            assert "当前短期表达模式" not in denied["messages"][0]["content"]
            assert "当前短期表达模式" in allowed["messages"][0]["content"]
        else:
            assert json.loads(denied["messages"][-1]["content"])["history"] == []
            assert "HM_HISTORY_MARKER" in json.dumps(allowed)
        receipt = interact(app, "cloud_permissions", permissions={category: False})
        assert receipt["privacy_invalidated"]
        # Revocation invalidates in-process conversations, candidates and cue.
        wire.calls.clear()
        assert run_turn(app, "neutral category sample").status == TaskStatus.COMPLETED
        revoked = last_expression(wire)
        if category == "custom_persona":
            assert "HM_CLOUD_PERSONA" not in json.dumps(revoked)
        elif category == "preferences":
            assert "篇幅简洁" not in revoked["messages"][0]["content"]
        elif category in {"cue", "affect"}:
            assert "当前短期表达模式" not in revoked["messages"][0]["content"]
        else:
            assert json.loads(revoked["messages"][-1]["content"])["history"] == []
        interact(app, "cloud_permissions", permissions={category: True})
        if category == "custom_persona":
            interact(app, "identity", display_name="HM_LOCAL_PERSONA", use_policy="local_only")
        elif category == "preferences":
            companion_command(app, "preference", scope="persistent", verbosity="detailed", use_policy="local_only")
        elif category == "cue":
            companion_command(app, "cue", mode="focused", ttl_seconds=600, use_policy="local_only")
        wire.calls.clear()
        assert run_turn(app, "neutral local-source sample").status == TaskStatus.COMPLETED
        serialized = json.dumps(wire.calls, ensure_ascii=False)
        assert "HM_LOCAL_PERSONA" not in serialized
        if category == "preferences":
            assert "篇幅充分解释" not in last_expression(wire)["messages"][0]["content"]
        elif category == "cue":
            assert "当前短期表达模式" not in last_expression(wire)["messages"][0]["content"]
        before = deepcopy(wire.calls)
        assert run_turn(app, PRIVATE, use_policy="local_only").status == TaskStatus.NEEDS_REVIEW
        assert wire.calls == before
        assert "generate" not in stages(wire)


def test_hm03_revoke_inflight_expression_discards_old_return_and_no_next_send(hm_config, wire):
    app = create_application(cloud(hm_config))
    try:
        configure(app, enabled=True)
        interact(app, "cloud_permissions", permissions={"preferences": True})
        wire.block = "expression"
        wire.replies["synthetic revoke request"] = envelope("HM_LATE_REVOKED_REPLY")
        task = app.submit("synthetic revoke request")
        assert wire.entered.wait(WAIT)
        before = len(wire.calls)
        receipt = interact(app, "cloud_permissions", permissions={"preferences": False})
        assert receipt["privacy_invalidated"]
        wire.release.set()
        idle(app)
        assert app.tasks.get(task.id).status == TaskStatus.NEEDS_REVIEW
        with pytest.raises(WorkflowBlocked):
            app.wait(task.id, WAIT)
        assert app.tasks.get(task.id).result is None or "HM_LATE_REVOKED_REPLY" not in json.dumps(app.tasks.get(task.id).result)
        assert len(wire.calls) == before
        assert app.interaction_snapshot()["candidates"] == []
    finally:
        wire.release.set()
        assert app.close(WAIT)


def test_hm03_profile_is_excluded_before_ranking_and_natural_candidates_require_review(hm_config, wire, monkeypatch):
    from deskpal.memory import retrieval

    ranked_bodies = []
    original_lexical = retrieval._lexical

    def record_ranking(query, content):
        ranked_bodies.append(content)
        return original_lexical(query, content)

    monkeypatch.setattr(retrieval, "_lexical", record_ranking)
    with create_application(hm_config) as app:
        configure(app, enabled=True)
        fact = app.memory_command("remember", content="Maple deployment uses folder Copper")
        # Saturating the real retriever's selection limit with explicit profiles
        # must not displace the execution fact even for an identical query.
        retriever = app.memory._retriever
        monkeypatch.setattr(retriever, "limit", 1)
        app.memory_command("remember", content="以后请叫我Maple", profile_key="preferred_name",
                           profile_value="Maple")
        result = run_turn(app, "Maple deployment uses folder Copper")
        assert result.status == TaskStatus.COMPLETED
        planner = next(call for call in wire.calls if call["stage"] == "planning")
        assert "Copper" in json.dumps(planner)
        assert "preferred_name" not in json.dumps(planner)
        assert fact.draft.content in ranked_bodies
        assert "以后请叫我Maple" not in ranked_bodies
        assert app._humanization.context(result.task_id, "planning")["refs"] == (("memory", fact.id, fact.revision),)
        # Remove the independent explicit profile before reviewing a natural one.
        prior_profile = app.memory_command("profile")[0]
        app.request_forget(prior_profile.memory_id, prior_profile.revision, "forget-seeded-profile")
        request = "以后请叫我小星，并且以后用英语回复。"
        prefs = [
            {"key": "preferred_name", "value": "小星", "scope": "persistent", "span": [0, 8]},
            {"key": "reply_language", "value": "en", "scope": "persistent", "span": [9, len(request)]}]
        wire.replies[request] = envelope(preferences=prefs)
        completed = run_turn(app, request)
        assert completed.status == TaskStatus.COMPLETED
        candidates = app.memory_command("list_items", status=MemoryStatus.CANDIDATE)
        assert {item.draft.profile_key for item in candidates} == {"preferred_name", "reply_language"}
        assert all(item.source.task_id == completed.task_id for item in candidates)
        assert app.memory_command("profile") == ()
        name = next(item for item in candidates if item.draft.profile_key == "preferred_name")
        accepted = app.memory_command("accept", memory_id=name.id, expected_revision=name.revision)
        assert accepted.status == MemoryStatus.ACCEPTED
        language = next(item for item in candidates if item.draft.profile_key == "reply_language")
        assert app.memory_command("accept", memory_id=language.id,
                                  expected_revision=language.revision).status == MemoryStatus.ACCEPTED
        assert any(entry.key == "preferred_name" and entry.value == "小星" for entry in app.memory_command("profile"))
        assert any(entry.key == "reply_language" and entry.value == "en" for entry in app.memory_command("profile"))
        conflict = "以后请叫我小河。"
        wire.replies[conflict] = envelope(preferences=[{"key": "preferred_name", "value": "小河",
            "scope": "persistent", "span": [0, len(conflict)]}])
        assert run_turn(app, conflict).status == TaskStatus.COMPLETED
        conflicting = next(item for item in app.memory_command("list_items", status=MemoryStatus.CANDIDATE)
                           if item.draft.value == "小河")
        with pytest.raises(MemoryError, match="conflict"):
            app.memory_command("accept", memory_id=conflicting.id, expected_revision=conflicting.revision)
        receipt = app.request_forget(accepted.id, accepted.revision, "forget-natural-profile")
        assert receipt["status"] == "Completed"
        assert not any(entry.memory_id == accepted.id for entry in app.memory_command("profile"))
        assert app.memory.repository.get(accepted.id).status == MemoryStatus.DELETED


def test_hm03_timeout_command_retry_and_restart_do_not_refresh_model_budget(hm_config, wire):
    request = "synthetic reply timeout root"
    wire.replies[request] = TimeoutError("synthetic timed-out expression")
    with create_application(hm_config) as app:
        configure(app, enabled=True)
        result = run_turn(app, request, command_id="stable-timeout-root")
        assert result.status == TaskStatus.COMPLETED and result.response == DRAFT
        assert stages(wire) == ["planning", "expression"]
        before = deepcopy(wire.calls)
        repeated = run_turn(app, request, command_id="stable-timeout-root")
        assert repeated.task_id == result.task_id
        assert wire.calls == before
        with app.store.transaction() as conn:
            attempts = [tuple(row) for row in conn.execute(
                "SELECT purpose,slot,status FROM task_call_attempt WHERE task_id=? ORDER BY purpose", (result.task_id,))]
        assert attempts == [("expression", 1, "unknown"), ("planning", 1, "finished")]
    with create_application(hm_config) as recovered:
        assert wire.calls == before
        repeated = run_turn(recovered, request, command_id="stable-timeout-root")
        assert repeated.task_id == result.task_id
        assert wire.calls == before
        with recovered.store.transaction() as conn:
            assert conn.execute("SELECT COUNT(*) FROM task_call_attempt WHERE task_id=?",
                                (result.task_id,)).fetchone()[0] == 2


def test_hm03_local_tool_affect_and_body_stay_local_after_cloud_reopen(hm_config, wire):
    request = PRIVATE + " local failed tool"
    wire.plans[request] = (ToolCall("read_text", {"path": "absent.txt"}, "local-failure"),)
    with create_application(hm_config) as app:
        configure(app, enabled=True, emotions_enabled=True)
        result = run_turn(app, request, use_policy="local_only")
        assert result.status == TaskStatus.FAILED
        eventually(lambda: not app._humanization.pending_effects(), "local effect was not delivered")
        assert app.companion_snapshot()["affect"]["mode"] == "focused"
    wire.calls.clear()
    with create_application(cloud(hm_config)) as app:
        interact(app, "cloud_permissions", permissions={key: True for key in
            ("custom_persona", "preferences", "cue", "affect", "history")})
        assert run_turn(app, "neutral cloud sample after local work").status == TaskStatus.COMPLETED
        serialized = json.dumps(wire.calls, ensure_ascii=False)
        assert PRIVATE not in serialized
        assert "当前短期表达模式" not in last_expression(wire)["messages"][0]["content"]


def test_hm04_partial_tool_failure_keeps_facts_and_never_reexecutes(hm_config, wire):
    request = "synthetic partial tool task"
    wire.plans[request] = (ToolCall("echo", {"text": "HM_RELIABLE_PREFIX"}, "first"),
                          ToolCall("read_text", {"path": "absent.txt"}, "failed"))
    with create_application(hm_config) as app:
        configure(app, enabled=True, emotions_enabled=True)
        result = run_turn(app, request)
        assert result.status == TaskStatus.FAILED
        assert [item.success for item in result.tool_results] == [True, False]
        assert result.tool_results[0].output == "HM_RELIABLE_PREFIX"
        assert [item.status for item in app.journal.attempts(result.task_id)] == ["Succeeded", "Failed"]
        assert stages(wire) == ["planning"]
        saved = app.journal.get_plan(result.task_id)
        with pytest.raises(WorkflowBlocked):
            app.enqueue_confirmation(result.task_id, plan_hash=saved.hash)
        assert len(app.journal.attempts(result.task_id)) == 2


@pytest.mark.parametrize("fault", ["invalid_envelope", "tool_calls", "timeout"])
def test_hm04_composer_failure_uses_authoritative_facts_without_tool_replay(hm_config, wire, fault):
    request = "synthetic tool result with failed composer"
    wire.plans[request] = (ToolCall("echo", {"text": "HM_TRUSTED_FACT"}, "echo"),)
    wire.replies[request] = ({"invalid_envelope": "not json", "tool_calls": LLMResponse(envelope(),
        (ToolCall("echo", {"text": "must not execute"}, "forbidden"),)),
        "timeout": TimeoutError("synthetic timeout")})[fault]
    with create_application(hm_config) as app:
        configure(app, enabled=True, emotions_enabled=True)
        result = run_turn(app, request)
        assert result.status == TaskStatus.COMPLETED
        assert "HM_TRUSTED_FACT" in result.response
        assert "must not execute" not in result.response
        assert len(app.journal.attempts(result.task_id)) == 1
        assert result.tool_results[0].success
        assert stages(wire) == ["planning", "expression"]
        with app.store.transaction() as conn:
            budget = [tuple(row) for row in conn.execute(
                "SELECT purpose,COUNT(*) FROM task_call_attempt WHERE task_id=? GROUP BY purpose", (result.task_id,))]
        assert sorted(budget) == [("expression", 1), ("planning", 1)]


@pytest.mark.parametrize("window", ["before_companion_commit", "after_commit_before_ack"])
def test_hm04_outbox_cross_store_restart_is_idempotent(hm_config, wire, monkeypatch, window):
    request = "synthetic reliable tool for recovery"
    wire.plans[request] = (ToolCall("echo", {"text": "reliable"}, "once"),)
    app = create_application(hm_config)
    delivered = Event()
    original_drain = app._effects.drain
    try:
        configure(app, enabled=True, emotions_enabled=True)
        before = app.companion_snapshot()["revision"]
        if window == "before_companion_commit":
            def fail(*args, **kwargs):
                raise CompanionStorageError("synthetic commit failure")
            monkeypatch.setattr(app.core.companion_runtime, "record_outcome", fail)
        else:
            def fail(effect_id):
                raise OSError("synthetic crash before outbox acknowledgement")
            monkeypatch.setattr(app._humanization, "ack_effect", fail)

        def observe_delivery():
            pending = bool(app._humanization.pending_effects())
            try:
                original_drain()
            finally:
                if pending:
                    delivered.set()

        monkeypatch.setattr(app._effects, "drain", observe_delivery)
        result = run_turn(app, request)
        assert result.status == TaskStatus.COMPLETED
        assert delivered.wait(WAIT)
        event = app._humanization.pending_effects()[0]
        assert event["id"] == "task:" + result.task_id
        assert len(app.journal.attempts(result.task_id)) == 1
        observed = app.companion_snapshot()["revision"]
        assert observed == before + (window == "after_commit_before_ack")
    finally:
        assert app.close(WAIT)
    # Patches belong to closed instances. The new production application owns
    # its new companion and gateway and never reconstructs conversation history.
    wire.calls.clear()
    with create_application(hm_config) as recovered:
        eventually(lambda: not recovered._humanization.pending_effects(), "outbox was not acknowledged")
        assert recovered.companion_snapshot()["revision"] == before + 1
        assert len(recovered.journal.attempts(result.task_id)) == 1
        assert len(recovered.tasks.list_tasks()) == 1
        assert wire.calls == []
        # Duplicate delivery remains harmless even after a companion reset.
        companion_command(recovered, "reset", target="affect")
        reset_revision = recovered.companion_snapshot()["revision"]
        with recovered.store.transaction() as conn:
            columns = tuple(event)
            conn.execute("INSERT INTO companion_effect_outbox (" + ",".join(columns) + ") VALUES ("
                         + ",".join("?" for _ in columns) + ")", tuple(event.values()))
        recovered._effects.drain()
        assert recovered.companion_snapshot()["revision"] == reset_revision
        assert not recovered._humanization.pending_effects()


def test_hm04_startup_consumes_expired_outbox_beyond_one_page_without_new_work(hm_config, wire, monkeypatch):
    request = "synthetic pagination seed tool"
    wire.plans[request] = (ToolCall("echo", {"text": "done"}, "once"),)
    with create_application(hm_config) as app:
        configure(app, enabled=True, emotions_enabled=True)
        monkeypatch.setattr(app._effects, "drain", lambda: None)
        assert run_turn(app, request).status == TaskStatus.COMPLETED
        event = app._humanization.pending_effects()[0]
        old = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
        with app.store.transaction() as conn:
            conn.execute("UPDATE companion_effect_outbox SET created_at=?", (old,))
            for index in range(100):
                task = app.tasks.create("synthetic expired recovery fixture " + str(index))
                clone = {**event, "id": "task:" + task.id, "task_id": task.id, "created_at": old}
                conn.execute("INSERT INTO companion_effect_outbox (" + ",".join(clone) + ") VALUES ("
                             + ",".join("?" for _ in clone) + ")", tuple(clone.values()))
        before = app.companion_snapshot()["revision"]
    wire.calls.clear()
    with create_application(hm_config) as recovered:
        eventually(lambda: not recovered._humanization.pending_effects(), "startup left a second outbox page")
        assert recovered.companion_snapshot()["revision"] == before
        assert wire.calls == []


@pytest.mark.parametrize("stale", ["expired", "deleted"])
def test_hm04_expired_or_deleted_outbox_event_cannot_replay(hm_config, wire, monkeypatch, stale):
    request = "synthetic tool tied to disposable source"
    wire.plans[request] = (ToolCall("echo", {"text": "done"}, "once"),)
    with create_application(hm_config) as app:
        configure(app, enabled=True, emotions_enabled=True)
        item = app.memory_command("remember", content="disposable source")
        original = app._effects.drain
        monkeypatch.setattr(app._effects, "drain", lambda: None)
        before = app.companion_snapshot()["revision"]
        result = run_turn(app, request, dependencies=(("memory", item.id, item.revision),))
        assert app._humanization.pending_effects()
        if stale == "expired":
            old = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
            with app.store.transaction() as conn:
                conn.execute("UPDATE companion_effect_outbox SET created_at=?", (old,))
        else:
            assert app.request_forget(item.id, item.revision, "delete-pending-effect")["status"] == "Completed"
            before = app.companion_snapshot()["revision"]
        original()
        assert app.companion_snapshot()["revision"] == before
        assert not app._humanization.pending_effects()
        assert len(app.journal.attempts(result.task_id)) == 1


@pytest.mark.parametrize("blocked", ["planning", "expression", "started_tool"])
def test_hm05_public_forget_orders_inflight_send_started_and_late_publication(hm_config, wire, monkeypatch, blocked):
    app = create_application(hm_config)
    tool_entered, tool_release = Event(), Event()
    executed = []
    request = PRIVATE + "。我很累。以后请叫我小星，以后详细回答。"
    start = request.index("我很累")
    wire.replies[request] = envelope("HM_LATE_PRIVATE_REPLY", cues=[{"mode": "supportive",
        "evidence_kind": "explicit_report", "span": [start, start + 3]}], preferences=[
        {"key": "preferred_name", "value": "小星", "scope": "persistent",
         "span": [request.index("以后请叫"), request.index("，")]},
        {"key": "verbosity", "value": "detailed", "scope": "persistent",
         "span": [request.index("以后详细"), len(request) - 1]}])
    if blocked == "started_tool":
        wire.plans[request] = (ToolCall("echo", {"text": "already started"}, "first"),
                               ToolCall("echo", {"text": "must never start"}, "second"))
        original_execute = app.tools.execute

        def hold_tool(call, context):
            executed.append(call.call_id)
            tool_entered.set()
            if not tool_release.wait(WAIT):
                raise TimeoutError("synthetic tool barrier expired")
            return original_execute(call, context)

        monkeypatch.setattr(app.tools, "execute", hold_tool)
    else:
        wire.block = blocked
    try:
        configure(app, enabled=True, emotions_enabled=True)
        conversation = app.conversation_open()
        item = app.memory_command("remember", content=PRIVATE)
        task = app.submit(request, dependencies=(("memory", item.id, item.revision),),
            conversation_id=conversation["conversation_id"], expected_conversation_epoch=conversation["epoch"])
        assert (tool_entered if blocked == "started_tool" else wire.entered).wait(WAIT)
        if blocked == "started_tool":
            assert [attempt.status for attempt in app.journal.attempts(task.id)] == ["Started"]
        calls_before = deepcopy(wire.calls)
        attempts_before = len(app.journal.attempts(task.id))
        receipt = DesktopFacade(app).execute("request_forget", {"memory_id": item.id,
            "expected_revision": item.revision, "command_id": "public-concurrent-forget"})
        assert receipt["accepted"] and receipt["status"] == "AcceptedPending"
        assert receipt["privacy_epoch"] > 0
        assert app.cleanup_status()["blocked"] and app.cleanup_status()["waiting_for_worker"]
        assert app.request_forget(item.id, item.revision, "public-concurrent-forget")["job_id"] == receipt["job_id"]
        with pytest.raises(WorkflowBlocked):
            app.submit("must not be admitted while deletion is pending")
        assert wire.calls == calls_before
        assert len(app.journal.attempts(task.id)) == attempts_before
        wire.release.set()
        tool_release.set()
        with pytest.raises(WorkflowBlocked):
            app.wait(task.id, WAIT)
        idle(app)
        eventually(lambda: app.cleanup_status()["status"] == "Completed", "privacy cleanup did not finish")
        persisted = app.tasks.get(task.id)
        assert persisted.status in {TaskStatus.NEEDS_REVIEW, TaskStatus.UNCERTAIN, TaskStatus.FAILED}
        assert persisted.result is None
        with pytest.raises(WorkflowBlocked):
            app.wait(task.id, WAIT)
        assert len(wire.calls) == len(calls_before)
        assert len(app.journal.attempts(task.id)) == attempts_before
        assert executed == (["first"] if blocked == "started_tool" else [])
        assert app.memory.repository.get(item.id).status == MemoryStatus.DELETED
        state = app.interaction_snapshot()
        assert state["cues"] == {} and state["candidates"] == [] and state["constraints"] == {}
        assert not any(entry.source.task_id == task.id for entry in
                       app.memory_command("list_items") if entry.status != MemoryStatus.DELETED)
        assert PRIVATE not in app.tasks.get(task.id).request
        assert not app._humanization.pending_effects()
        with pytest.raises(WorkflowBlocked):
            app.enqueue_confirmation(task.id, plan_hash="0" * 64)
        # A fresh public turn cannot pick the deleted body back up from history.
        wire.block = None
        wire.calls.clear()
        fresh = app.conversation_open()
        assert run_turn(app, "new neutral request", conversation=fresh).status == TaskStatus.COMPLETED
        serialized = json.dumps(wire.calls, ensure_ascii=False)
        assert PRIVATE not in serialized and "HM_LATE_PRIVATE_REPLY" not in serialized
    finally:
        wire.release.set()
        tool_release.set()
        assert app.close(WAIT)


def test_hm05_cleanup_failure_stays_pending_and_restart_retries_public_forget(hm_config, wire, monkeypatch):
    app = create_application(hm_config)
    wire.block = "expression"
    try:
        configure(app, enabled=True)
        conversation = app.conversation_open()
        item = app.memory_command("remember", content=PRIVATE)
        task = app.submit(PRIVATE, dependencies=(("memory", item.id, item.revision),),
            conversation_id=conversation["conversation_id"], expected_conversation_epoch=conversation["epoch"])
        assert wire.entered.wait(WAIT)

        def fail_reset():
            raise CompanionStorageError("synthetic cleanup unavailable")

        monkeypatch.setattr(app.core.companion_runtime, "reset_for_privacy", fail_reset)
        receipt = app.request_forget(item.id, item.revision, "restart-forget")
        assert receipt["status"] == "AcceptedPending"
        wire.release.set()
        idle(app)
        assert not app.retry_cleanup()
        assert app.cleanup_status()["status"] == "Pending"
        assert app.journal.pending_deletions()
        with pytest.raises(WorkflowBlocked):
            app.submit("blocked until cleanup succeeds")
    finally:
        wire.release.set()
        assert app.close(WAIT)
    wire.block = None
    wire.calls.clear()
    with create_application(hm_config) as recovered:
        assert recovered.cleanup_status()["status"] == "Completed"
        assert recovered.retry_cleanup()
        assert recovered.request_forget(item.id, item.revision, "restart-forget")["status"] == "Completed"
        assert recovered.memory.repository.get(item.id).status == MemoryStatus.DELETED
        with pytest.raises(ValueError):
            recovered.conversation_snapshot(conversation["conversation_id"])
        with pytest.raises(WorkflowBlocked):
            recovered.enqueue_confirmation(task.id, plan_hash="0" * 64)
        assert run_turn(recovered, "post-restart neutral question").status == TaskStatus.COMPLETED
        assert PRIVATE not in json.dumps(wire.calls)
