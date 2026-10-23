"""V0.8 synthetic integration boundaries; all model traffic is intercepted.

T07 deliberately distinguishes the public service's Busy rejection from the
Core's narrow privacy-generation barrier. It does not claim that a running
service accepts a concurrent forget command.
"""

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
from threading import Event
from time import monotonic

import pytest

from deskpal.application.desktop import DesktopFacade
from deskpal.application.factory import create_application
from deskpal.companion import CompanionRevisionConflict, CompanionRuntime, CompanionStorageError
from deskpal.config import LLMConfig, load_config
from deskpal.llm.models import LLMResponse
from deskpal.memory.contracts import MemoryStatus
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import PermissionLevel, ToolCall
from deskpal.workflow.models import WorkflowBlocked


WAIT_SECONDS = 10
DRAFT = "ordinary assistant draft"
EXPRESSION = "SYNTHETIC_COMPOSED_EXPRESSION"


class RecordingProvider:
    """Record every provider entry, including unexpected generate requests."""

    def __init__(self):
        self.calls = []
        self.plans = {}
        self.block_stage = None
        self.entered = Event()
        self.release = Event()

    def chat(self, messages, *, tools=()):
        system = messages[0].content
        if system.startswith("[REFLECT]"):
            stage = "reflect"
        elif system.startswith("[EXPRESSION]"):
            stage = "expression"
        elif system.startswith("Extract optional long-term memory candidates"):
            stage = "memory_evaluation"
        else:
            stage = "planner"
        entry = {
            "method": "chat", "stage": stage,
            "messages": [asdict(message) for message in messages],
            "tools": [asdict(spec) for spec in tools],
        }
        self.calls.append(deepcopy(entry))
        if stage == self.block_stage:
            self.entered.set()
            if not self.release.wait(WAIT_SECONDS):
                raise TimeoutError("synthetic provider barrier was not released")
        if stage == "planner":
            return LLMResponse(DRAFT, self.plans.get(messages[-1].content, ()))
        if stage == "memory_evaluation":
            return LLMResponse('{"candidates": []}')
        if stage == "expression":
            structured = "OUTPUT PROTOCOL" in system
            body = json.dumps({"reply_text": EXPRESSION, "cues": [], "preference_candidates": []})
            return LLMResponse(body if structured else EXPRESSION)
        return LLMResponse("Explicit tools completed.")

    def generate(self, prompt):
        self.calls.append({"method": "generate", "stage": "generate", "prompt": prompt})
        raise AssertionError("these application paths must not use generate")

    def clear(self):
        self.calls.clear()


@pytest.fixture
def config(tmp_path):
    return load_config(environ={
        "DESKPAL_LLM_PROVIDER": "offline",
        "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "DESKPAL_DATA_DIR": str(tmp_path / "data"),
    })


@pytest.fixture
def recorder(monkeypatch):
    from deskpal.application import factory

    provider = RecordingProvider()
    monkeypatch.setattr(factory, "OfflineLLMProvider", lambda: provider)
    monkeypatch.setattr(factory, "OpenAILLMProvider", lambda *args, **kwargs: provider)
    return provider


@pytest.fixture
def outcome_calls(monkeypatch):
    calls = []
    original = CompanionRuntime.record_outcome

    def recorded(runtime, task_id, status, *, tool_success=None, **parameters):
        calls.append((task_id, status, tool_success))
        return original(runtime, task_id, status, tool_success=tool_success, **parameters)

    monkeypatch.setattr(CompanionRuntime, "record_outcome", recorded)
    return calls


def command(app, action="configure", **parameters):
    revision = app.companion_snapshot()["revision"]
    return app.companion_command(action, expected_revision=revision, **parameters)


def run(app, request, **parameters):
    task = app.submit(request, **parameters)
    result = app.wait(task.id, timeout=WAIT_SECONDS)
    wait_idle(app)
    return result


def wait_idle(app):
    """Observe public worker diagnostics after terminal persistence/finally."""
    deadline = monotonic() + WAIT_SECONDS
    pause = Event()
    while True:
        if (app.diagnostics()["dispatcher"]["active_task_id"] is None
                and not app._humanization.pending_effects()):
            return
        remaining = deadline - monotonic()
        if remaining <= 0:
            pytest.fail("synthetic application did not finish terminal bookkeeping")
        pause.wait(min(0.01, remaining))


def stages(recorder):
    return [call["stage"] for call in recorder.calls]


def assert_no_generate(recorder):
    assert all(call["method"] == "chat" for call in recorder.calls)


def test_t01_two_desktop_clients_conflict_rollback_new_version_and_reset_preserves_work(config, recorder):
    with create_application(config) as app:
        memory = app.memory_command("remember", content="synthetic retained personal fact")
        task = run(app, "ordinary retained task")
        assert task.status == TaskStatus.COMPLETED
        task_before = app.tasks.get(task.task_id)
        memories_before = app.memory_command("list_items", include_history=True)
        left, right = DesktopFacade(app), DesktopFacade(app)
        base = left.execute("companion", {})
        assert right.execute("companion", {})["revision"] == base["revision"]
        saved = left.execute("companion_command", {
            "action": "configure", "expected_revision": base["revision"],
            "enabled": True, "emotions_enabled": True, "preset": "warm",
        })
        with pytest.raises(CompanionRevisionConflict):
            right.execute("companion_command", {
                "action": "configure", "expected_revision": base["revision"], "preset": "concise",
            })
        with pytest.raises(CompanionRevisionConflict):
            app.companion_command("reset", expected_revision=base["revision"], target="all")
        assert app.companion_snapshot()["persona"] == saved["persona"]
        newer = command(app, preset="concise")
        rolled = right.execute("companion_command", {
            "action": "rollback", "expected_revision": newer["revision"],
            "persona_version": saved["persona"]["version"],
        })
        assert rolled["persona"]["preset"] == "warm"
        assert rolled["persona"]["style"] == saved["persona"]["style"]
        assert rolled["persona"]["version"] > newer["persona"]["version"]
        reset = command(app, "reset", target="all")
        assert reset["reset_epoch"] > rolled["reset_epoch"]
        assert app.tasks.get(task.task_id) == task_before
        assert app.memory_command("list_items", include_history=True) == memories_before
        assert any(item.id == memory.id for item in memories_before)


def test_t02_presets_cues_do_not_change_planner_or_same_task_plan_authority(config, recorder):
    request = "synthetic plan requiring both grant classes"
    recorder.plans[request] = (
        ToolCall("write_text", {"path": "created.txt", "text": "exact synthetic data"}, "write-call"),
        ToolCall("document_delete", {"path": "victim.txt"}, "delete-call"),
    )
    with create_application(config) as app:
        planning_inputs = []
        for preset, mode in (("balanced", "neutral"), ("warm", "supportive"), ("concise", "focused")):
            command(app, enabled=True, emotions_enabled=True, preset=preset)
            command(app, "cue", mode=mode, ttl_seconds=600)
            waiting = run(app, request)
            assert waiting.status == TaskStatus.WAITING
            assert waiting.required_authorizations == ("write_text", "document_delete")
            assert waiting.required_high_risk_authorizations == ("document_delete",)
            assert [item.permission_level for item in waiting.authorization_requests] == [
                PermissionLevel.CONFIRM, PermissionLevel.HIGH_RISK,
            ]
            planning_inputs.append(deepcopy(recorder.calls[-1]))
            original = app.journal.get_plan(waiting.task_id)
            authority = json.loads(original.canonical_json)
            # Keep all security fields and the real task ID in the hash check.
            assert authority["task_id"] == waiting.task_id
            assert {"workspace_dir", "tool_specs", "memory_refs", "dependencies", "use_policy", "policy_version"} <= set(authority)
            assert sha256(original.canonical_json.encode("utf-8")).hexdigest() == original.hash
            call_count = len(recorder.calls)
            command(app, preset="concise" if preset != "concise" else "warm")
            command(app, "cue", mode="focused", ttl_seconds=60)
            app.enqueue_confirmation(waiting.task_id, plan_hash=original.hash)
            repeated = app.wait(waiting.task_id, timeout=WAIT_SECONDS)
            wait_idle(app)
            assert repeated.status == TaskStatus.WAITING
            assert repeated.planned_calls == waiting.planned_calls
            assert repeated.authorization_requests == waiting.authorization_requests
            assert repeated.required_authorizations == waiting.required_authorizations
            assert repeated.required_high_risk_authorizations == waiting.required_high_risk_authorizations
            current = app.journal.get_plan(waiting.task_id)
            assert current.canonical_json == original.canonical_json
            assert current.hash == original.hash
            assert len(recorder.calls) == call_count
            assert app.journal.attempts(waiting.task_id) == ()
        assert planning_inputs[0] == planning_inputs[1] == planning_inputs[2]
        assert not (config.sandbox.workspace_dir / "created.txt").exists()
        assert_no_generate(recorder)


def test_t03_all_model_calls_disabled_chat_one_enabled_two_tool_two_management_zero(config, recorder):
    recorder.plans["tool task"] = (ToolCall("echo", {"text": "synthetic result"}, "echo-call"),)
    with create_application(config) as app:
        disabled = run(app, "ordinary chat")
        assert disabled.status == TaskStatus.COMPLETED and disabled.response == DRAFT
        assert stages(recorder) == ["planner"]
        recorder.clear()
        DesktopFacade(app).execute("companion", {})
        command(app, enabled=True, emotions_enabled=True, preset="warm")
        command(app, "preference", scope="session", verbosity="brief", humor_allowed=False)
        command(app, "cue", mode="focused", ttl_seconds=60)
        assert recorder.calls == []
        enabled = run(app, "ordinary chat")
        assert enabled.status == TaskStatus.COMPLETED and enabled.response == EXPRESSION
        assert stages(recorder) == ["planner", "expression"]
        expression = recorder.calls[-1]
        assert expression["tools"] == []
        assert "unverified" in expression["messages"][0]["content"]
        assert json.loads(expression["messages"][-1]["content"])["unverified_assistant_draft"] == DRAFT
        recorder.clear()
        done = run(app, "tool task")
        assert done.status == TaskStatus.COMPLETED
        assert stages(recorder) == ["planner", "expression"]
        assert recorder.calls[-1]["tools"] == []
        recorder.clear()
        command(app, enabled=False)
        assert recorder.calls == []
        assert run(app, "tool task").status == TaskStatus.COMPLETED
        assert stages(recorder) == ["planner", "expression"]
        assert_no_generate(recorder)


@pytest.mark.parametrize("enabled", [False, True])
def test_t03_candidate_review_calls_are_counted_separately(config, recorder, enabled):
    settings = replace(config, memory=replace(config.memory, mode="candidate_review"))
    with create_application(settings) as app:
        command(app, enabled=enabled, emotions_enabled=enabled)
        result = run(app, "synthetic explicit user evidence for this request")
        assert result.status == TaskStatus.COMPLETED
        counts = Counter(stages(recorder))
        assert counts == Counter({"planner": 1, "memory_evaluation": 1,
                                  **({"expression": 1} if enabled else {})})
        assert all(call["tools"] == [] for call in recorder.calls if call["stage"] != "planner")
        assert_no_generate(recorder)


def test_t03_cloud_expression_uses_only_fixed_preset_and_blocks_local_only_intent(config, recorder):
    settings = replace(config, llm=LLMConfig(provider="openai", model="synthetic", api_key="synthetic-key"))
    with create_application(settings) as app:
        command(app, enabled=True, emotions_enabled=True, preset="balanced")
        assert run(app, "same cloud request").status == TaskStatus.COMPLETED
        baseline = deepcopy(recorder.calls)
        assert stages(recorder) == ["planner", "expression"]
        # These settings conflict with the fixed balanced preset in every axis.
        command(app, style={"formality": 1.0, "warmth": 1.0, "humor": 1.0,
                            "verbosity": 1.0, "expressiveness": 1.0, "curiosity": 1.0})
        command(app, "preference", scope="persistent", verbosity="detailed", humor_allowed=True)
        command(app, "cue", mode="supportive", ttl_seconds=600)
        # Ordinary chat has no trusted tool outcome and receives no affect reward.
        assert run(app, "synthetic affect-producing request").status == TaskStatus.COMPLETED
        assert app.companion_snapshot()["affect"]["valence"] <= 0.04
        recorder.clear()
        assert run(app, "same cloud request").status == TaskStatus.COMPLETED
        assert recorder.calls == baseline
        assert recorder.calls[-1]["tools"] == []
        # The fixed response-schema vocabulary names all possible signals. It
        # conveys no selected cue or preference; inspect actual data and the
        # optional guidance while preserving the full-request equality above.
        expression = recorder.calls[-1]
        payload = json.loads(expression["messages"][-1]["content"])
        assert payload["persona_identity"] is None and payload["memory_claims"] == []
        guidance = expression["messages"][0]["content"].split("Optional finite expression guidance:\n")[1].split("\nReturn exactly")[0]
        assert "当前短期表达模式" not in guidance
        assert "充分解释" not in guidance
        wire_data = json.dumps(payload, ensure_ascii=False)
        assert "supportive" not in wire_data
        # The selected finite policy is the public fixed preset, not the
        # conflicting local preference above. Full request equality with the
        # pre-edit baseline remains the noninterference check.
        assert payload["expression_policy"] == {
            "verbosity": "normal", "humor_allowed": False, "followup_allowed": False,
        }
        assert "persistent" not in wire_data and "reset_epoch" not in wire_data
        before = deepcopy(recorder.calls)
        blocked = run(app, "synthetic local-only intent", use_policy="local_only")
        assert blocked.status == TaskStatus.NEEDS_REVIEW
        assert recorder.calls == before
        assert_no_generate(recorder)


def test_t03_cli_management_forces_offline_without_model_key_or_calls(config, recorder, monkeypatch, capsys):
    from deskpal.application import cli

    overrides = []

    def isolated_config(path=None, *, provider_override=None):
        overrides.append(provider_override)
        assert provider_override == "offline"
        assert config.llm.api_key == ""
        return config

    monkeypatch.setattr(cli, "load_config", isolated_config)
    assert cli.main(["--companion", "show"]) == 0
    state = json.loads(capsys.readouterr().out)
    saved_version = None
    operations = [
        ("configure", {"enabled": True, "emotions_enabled": True, "preset": "warm"}),
        ("preference", {"scope": "today", "verbosity": "brief", "humor_allowed": False}),
        ("cue", {"mode": "focused", "ttl_seconds": 60}),
        ("reset", {"target": "affect"}),
        ("rollback", {}),
    ]
    for action, parameters in operations:
        if action == "rollback":
            parameters["persona_version"] = saved_version
        parameters["expected_revision"] = state["revision"]
        assert cli.main(["--companion", action, "--companion-parameters", json.dumps(parameters)]) == 0
        state = json.loads(capsys.readouterr().out)
        if action == "configure":
            saved_version = state["persona"]["version"]
    assert overrides == ["offline"] * 6
    assert recorder.calls == []


def test_t04_confirmation_replay_runs_one_tool_and_one_terminal_emotion(config, recorder, outcome_calls, monkeypatch):
    request = "confirmed synthetic write"
    recorder.plans[request] = (ToolCall("write_text", {"path": "once.txt", "text": "written once"}, "write"),)
    app = create_application(config)
    entered, release = Event(), Event()
    executed = []
    original = app.tools.execute

    def blocked(call, context):
        executed.append((call.name, dict(call.arguments)))
        entered.set()
        if not release.wait(WAIT_SECONDS):
            raise TimeoutError("synthetic tool barrier was not released")
        return original(call, context)

    try:
        command(app, enabled=True, emotions_enabled=True)
        waiting = run(app, request)
        assert waiting.status == TaskStatus.WAITING
        assert outcome_calls == []
        saved = app.journal.get_plan(waiting.task_id)
        monkeypatch.setattr(app.tools, "execute", blocked)
        facade = DesktopFacade(app)
        parameters = {"task_id": waiting.task_id, "plan_hash": saved.hash,
                      "ordinary_tools": ["write_text"], "high_risk_tools": [], "command_id": "same-confirmation"}
        before = app.companion_snapshot()
        assert facade.execute("confirm", parameters)["accepted"]
        assert entered.wait(WAIT_SECONDS)
        assert facade.execute("confirm", parameters)["accepted"]
        assert outcome_calls == []
        release.set()
        assert app.wait(waiting.task_id, WAIT_SECONDS).status == TaskStatus.COMPLETED
        wait_idle(app)
        assert facade.execute("confirm", parameters)["accepted"]
        assert executed == [("write_text", {"path": "once.txt", "text": "written once"})]
        assert outcome_calls == [(waiting.task_id, "Completed", True)]
        assert len(app.journal.attempts(waiting.task_id)) == 1
        after = app.companion_snapshot()
        assert after["revision"] == before["revision"] + 1
        assert app.companion_snapshot()["revision"] == after["revision"]
        assert (config.sandbox.workspace_dir / "once.txt").read_text(encoding="utf-8") == "written once"
    finally:
        release.set()
        assert app.close(WAIT_SECONDS)


@pytest.mark.parametrize("fault", ["completion_commit", "step_receipt"])
def test_t04_tool_success_with_failed_commit_or_uncertain_never_records_completion(config, recorder, outcome_calls, monkeypatch, fault):
    recorder.plans["faulted tool task"] = (ToolCall("echo", {"text": "actual tool success"}, "echo"),)
    with create_application(config) as app:
        command(app, enabled=True, emotions_enabled=True)
        before = app.companion_snapshot()
        if fault == "completion_commit":
            original = app.tasks.update_status

            def fail_completion(task_id, status, **parameters):
                if status == TaskStatus.COMPLETED:
                    raise OSError("synthetic failed completion commit")
                return original(task_id, status, **parameters)

            monkeypatch.setattr(app.tasks, "update_status", fail_completion)
        else:
            def fail_receipt(attempt_id, result):
                assert result.success
                raise OSError("synthetic failed step outcome commit")

            monkeypatch.setattr(app.journal, "finish_step", fail_receipt)
        result = run(app, "faulted tool task")
        current = app.tasks.get(result.task_id)
        assert not any(status == "Completed" for _, status, _ in outcome_calls)
        assert len(app.journal.attempts(result.task_id)) == 1
        if fault == "completion_commit":
            assert current.status == result.status == TaskStatus.FAILED
            assert result.tool_results[0].success
            assert outcome_calls == []
            assert app.companion_snapshot()["revision"] == before["revision"]
        else:
            assert current.status == result.status == TaskStatus.UNCERTAIN
            assert outcome_calls == []
            assert app.companion_snapshot()["revision"] == before["revision"]
            saved = app.journal.get_plan(result.task_id)
            with pytest.raises(WorkflowBlocked):
                app.enqueue_confirmation(result.task_id, plan_hash=saved.hash)
    if fault == "step_receipt":
        before_restart = deepcopy(recorder.calls)
        with create_application(config) as recovered:
            assert recovered.tasks.get(result.task_id).status == TaskStatus.UNCERTAIN
            assert len(recovered.journal.attempts(result.task_id)) == 1
            with pytest.raises(WorkflowBlocked):
                recovered.enqueue_confirmation(result.task_id, plan_hash=saved.hash)
            assert recorder.calls == before_restart
            assert outcome_calls == []


def test_t07_busy_forget_is_not_accepted_and_core_generation_discards_late_expression(config, recorder, outcome_calls):
    app = create_application(config)
    recorder.block_stage = "expression"
    try:
        command(app, enabled=True, emotions_enabled=True, preset="warm")
        item = app.memory_command("remember", content="SYNTHETIC_PRIVATE_BARRIER_FACT")
        task = app.submit("SYNTHETIC_PRIVATE_BARRIER_FACT")
        assert recorder.entered.wait(WAIT_SECONDS)
        before = app.companion_snapshot()
        # This is the real management gate; do not bypass it to claim support
        # for accepting service-level concurrent deletion during model work.
        with pytest.raises(RuntimeError, match="execution is active"):
            app.memory_command("forget", memory_id=item.id, expected_revision=item.revision)
        assert app.journal.pending_deletions() == ()
        assert app.memory.repository.get(item.id).status == MemoryStatus.ACCEPTED
        assert app.companion_snapshot()["reset_epoch"] == before["reset_epoch"]
        # Narrow Core barrier coverage is intentionally a separate assertion.
        app.core.reset_companion_for_privacy()
        reset = app.companion_snapshot()
        assert reset["reset_epoch"] == before["reset_epoch"] + 1
        recorder.release.set()
        result = app.wait(task.id, WAIT_SECONDS)
        wait_idle(app)
        assert result.status == TaskStatus.COMPLETED
        assert result.response == DRAFT and result.response != EXPRESSION
        assert app.tasks.get(task.id).result["response"] == DRAFT
        assert outcome_calls == []
        after = app.companion_snapshot()
        assert after["revision"] == reset["revision"]
        assert after["reset_epoch"] == reset["reset_epoch"]
        assert after["affect"]["mood"] == 0.0
        receipt = app.memory_command("forget", memory_id=item.id, expected_revision=item.revision)
        assert not isinstance(receipt, dict), "the idle service should finish the real deletion"
        assert app.memory.repository.get(item.id).status == MemoryStatus.DELETED
        assert app.journal.pending_deletions() == ()
    finally:
        recorder.release.set()
        assert app.close(WAIT_SECONDS)


def test_t07_privacy_reset_failure_keeps_deletion_pending_until_successful_retry(config, recorder, monkeypatch):
    attempts = []
    original = CompanionRuntime.reset_for_privacy

    def fail_once(runtime):
        attempts.append("reset")
        if len(attempts) == 1:
            raise CompanionStorageError("synthetic_reset_failure")
        return original(runtime)

    with create_application(config) as app:
        command(app, enabled=True, emotions_enabled=True)
        item = app.memory_command("remember", content="synthetic fact to erase after reset recovery")
        epoch = app.companion_snapshot()["reset_epoch"]
        monkeypatch.setattr(CompanionRuntime, "reset_for_privacy", fail_once)
        pending = app.memory_command("forget", memory_id=item.id, expected_revision=item.revision)
        assert pending["status"] == "Pending" and pending["blocked"]
        assert app.diagnostics()["cleanup_pending"]
        assert len(app.journal.pending_deletions()) == 1
        assert app.memory.repository.get(item.id).status == MemoryStatus.ACCEPTED
        with pytest.raises(WorkflowBlocked):
            app.companion_snapshot()
        assert app.retry_cleanup()
        assert attempts == ["reset", "reset"]
        assert app.journal.pending_deletions() == ()
        assert not app.diagnostics()["cleanup_pending"]
        assert app.memory.repository.get(item.id).status == MemoryStatus.DELETED
        recovered = app.companion_snapshot()
        assert recovered["reset_epoch"] == epoch + 1
        assert "companion_storage_unavailable" not in recovered["warnings"]
        assert recorder.calls == []
