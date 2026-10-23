"""HM01–HM04 domain boundaries, synthetic data only and no remote providers.

Reuse the original deterministic clock and affect witnesses instead of copying
the reducer. Production publication, authorization and privacy races live in
the integration acceptance group; these probes exercise the hard domain caps.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import weakref
from zoneinfo import ZoneInfo

import pytest

from deskpal.companion import CompanionRuntime, CompanionValidationError
import deskpal.companion.runtime as companion_runtime
from deskpal.conversation.runtime import ConversationConflict, ConversationRuntime
from deskpal.llm.calls import ModelCallGateway
from deskpal.llm.errors import LLMError
from deskpal.llm.models import ChatMessage, LLMResponse
from deskpal.response.composer import parse_envelope
from deskpal.tools.models import ToolCall, ToolSpec
from deskpal.workflow import PersistentTaskManager, WorkflowBlocked, WorkflowStore
from deskpal.workflow.humanization import HumanizationStore

from test_v08_companion_runtime import Clock, receipt, runtime_at, sql_rows, values


def command(runtime: CompanionRuntime, action: str, **parameters: Any) -> dict[str, Any]:
    return runtime.command(action, expected_revision=runtime.snapshot()["revision"], **parameters)


def local_style(runtime: CompanionRuntime, request: str = "", *, conversation_id: str = "a",
                fallback: dict[str, Any] | None = None) -> str:
    return runtime.style_instruction(request, cloud=False, conversation_id=conversation_id,
                                     preferences=fallback)


def envelope(request: str, *, cues: list[dict[str, Any]] | None = None,
             preferences: list[dict[str, Any]] | None = None) -> str:
    return json.dumps({"reply_text": "收到。", "cues": cues or [],
                       "preference_candidates": preferences or []}, ensure_ascii=False)


def publish(runtime: ConversationRuntime, session: dict[str, Any], task: str,
            request: str = "user", response: str = "assistant", *,
            refs: Sequence[Any] = (), facts: Sequence[dict[str, Any]] = ()) -> None:
    sequence = runtime.reserve(session["id"], session["epoch"])
    runtime.publish(session["id"], session["epoch"], sequence, task, request, response,
                    refs=refs, facts=facts)


def freeze(runtime: ConversationRuntime, session: dict[str, Any]) -> dict[str, Any]:
    sequence = runtime.reserve(session["id"], session["epoch"])
    return runtime.freeze(session["id"], session["epoch"], sequence,
                          cloud=False, history_allowed=True)


def test_hm01_constraint_precedence_scopes_midnight_and_memory_fallback(tmp_path: Path) -> None:
    clock = Clock(wall=datetime(2026, 10, 22, 23, 59, 30,
                               tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    with runtime_at(tmp_path / "hm01-scopes.sqlite3", clock) as runtime:
        command(runtime, "configure", preset="warm")
        assert "篇幅适中" in local_style(runtime)
        assert "可少量轻松表达" in local_style(runtime)
        assert "篇幅充分解释" in local_style(runtime, fallback={"verbosity": "detailed"})
        command(runtime, "preference", scope="persistent", verbosity="normal")
        assert "篇幅适中" in local_style(runtime, fallback={"verbosity": "detailed"})

        current = runtime.prepare_interaction("这次简短一点；不要开玩笑", task_id="once",
                                              conversation_id="a")
        assert current["constraints"] == {"verbosity": "brief", "humor_allowed": False}
        assert "篇幅简洁" in local_style(runtime, "这次简短一点；不要开玩笑")
        assert "避免玩笑" in local_style(runtime, "这次简短一点；不要开玩笑")
        runtime.prepare_interaction("继续", task_id="next", conversation_id="a")
        assert "篇幅适中" in local_style(runtime)
        assert "可少量轻松表达" in local_style(runtime)

        runtime.prepare_interaction("本会话简短一点", task_id="session", conversation_id="a")
        runtime.prepare_interaction("今天详细一点", task_id="today", conversation_id="b")
        assert "篇幅简洁" in local_style(runtime, conversation_id="a")
        assert "篇幅充分解释" in local_style(runtime, conversation_id="b")
        today = runtime.snapshot()["preferences"]["today"]
        assert today["expires_at"] == clock.wall + 30
        clock.advance(29)
        assert "篇幅充分解释" in local_style(runtime, conversation_id="b")
        clock.advance(1)
        assert "篇幅适中" in local_style(runtime, conversation_id="b")
        assert "篇幅简洁" in local_style(runtime, conversation_id="a")
        runtime.close_conversation("a")
        assert "篇幅适中" in local_style(runtime, conversation_id="a")


def test_hm01_cue_correction_candidate_confirmation_receipts_and_expiry(tmp_path: Path) -> None:
    path, clock = tmp_path / "hm01-candidates.sqlite3", Clock()
    with runtime_at(path, clock) as runtime:
        request = "我今天很累，以后请不要开玩笑"
        runtime.prepare_interaction(request, task_id="source", conversation_id="a")
        draft = parse_envelope(envelope(request,
            cues=[{"mode": "supportive", "evidence_kind": "explicit_report", "span": [0, 5]}],
            preferences=[{"key": "humor_allowed", "value": False, "scope": "persistent",
                          "span": [6, len(request)]}]), request=request)
        runtime.accept_signals("source", {"cues": draft.cues,
            "preference_candidates": draft.preference_candidates}, conversation_id="a")
        snapshot = runtime.interaction_snapshot()
        assert snapshot["cues"]["a"]["source_ref"] == "source"
        assert snapshot["preferences"].get("persistent", {}) == {}
        assert sql_rows(path, "SELECT COUNT(*) FROM command_receipts") == [(0,)]
        assert "当前短期表达模式：支持" in local_style(runtime)
        assert "当前短期表达模式" not in local_style(runtime, conversation_id="b")
        candidate = snapshot["candidates"][0]
        parameters = {"expected_revision": runtime.snapshot()["revision"],
                      "candidate_id": candidate["candidate_id"], "command_id": "save-preference"}
        saved = runtime.command("confirm_candidate", **parameters)
        assert saved["preferences"]["persistent"] == {"humor_allowed": False}
        assert runtime.interaction_snapshot()["preference_sources"]["persistent"]["humor_allowed"]["source_ref"] == "source"
        assert runtime.command("confirm_candidate", **parameters) == saved
        assert sql_rows(path, "SELECT COUNT(*) FROM command_receipts WHERE command_id='save-preference'") == [(1,)]

        runtime.prepare_interaction("不是我，是我朋友", task_id="correction", conversation_id="a")
        assert runtime.interaction_snapshot()["cues"] == {}
        assert "当前短期表达模式" not in local_style(runtime)
        runtime.prepare_interaction(request, task_id="expires", conversation_id="a")
        runtime.accept_signals("expires", {"cues": draft.cues,
            "preference_candidates": draft.preference_candidates}, conversation_id="a")
        expired_id = runtime.interaction_snapshot()["candidates"][0]["candidate_id"]
        clock.advance(600)
        before = runtime.snapshot()
        with pytest.raises(CompanionValidationError, match="candidate_unavailable"):
            command(runtime, "confirm_candidate", candidate_id=expired_id, command_id="expired-confirm")
        assert runtime.snapshot() == before
        assert sql_rows(path, "SELECT COUNT(*) FROM command_receipts WHERE command_id='expired-confirm'") == [(0,)]
        assert "a" in runtime.interaction_snapshot()["cues"]
        clock.advance(1200)
        assert runtime.interaction_snapshot()["cues"] == {}


@pytest.mark.parametrize(("message", "signals"), [
    ("我很累", {"cues": [{"mode": "diagnosis", "evidence_kind": "explicit_report", "span": [0, 3]}]}),
    ("我很累", {"cues": [{"mode": "supportive", "evidence_kind": "explicit_report", "span": [0, 99]}]}),
    ("我朋友很累", {"cues": [{"mode": "supportive", "evidence_kind": "explicit_report", "span": [0, 5]}]}),
    ("他说“我很累”", {"cues": [{"mode": "supportive", "evidence_kind": "explicit_report", "span": [3, 6]}]}),
    ("我希望简短", {"preferences": [{"key": "verbosity", "value": "tiny", "scope": "persistent", "span": [0, 5]}]}),
], ids=["cue-enum", "cue-span", "friend", "quoted", "preference-enum"])
def test_hm01_invalid_signal_area_is_discarded_but_reply_survives(
    message: str, signals: dict[str, Any]
) -> None:
    draft = parse_envelope(envelope(message, **signals), request=message)
    assert draft.reply_text == "收到。"
    assert draft.degraded_reason == "response_signals_invalid"
    assert draft.cues == draft.preference_candidates == ()


def test_hm01_profile_spans_normalization_and_duplicate_json_key() -> None:
    request = "请叫我 E\u0301，以后用英文回答"
    end = request.index("，")
    draft = parse_envelope(envelope(request, preferences=[
        {"key": "preferred_name", "value": " E\u0301 ", "scope": "persistent", "span": [0, end]},
        {"key": "reply_language", "value": "EN", "scope": "persistent", "span": [end + 1, len(request)]},
    ]), request=request)
    assert draft.degraded_reason is None
    assert [(item.key, item.value, item.span) for item in draft.preference_candidates] == [
        ("preferred_name", "É", (0, end)), ("reply_language", "en", (end + 1, len(request)))]
    invalid = parse_envelope(envelope(request, preferences=[
        {"key": "preferred_name", "value": "Alice", "scope": "persistent", "span": [False, end]},
    ]), request=request)
    assert invalid.preference_candidates == ()
    assert invalid.degraded_reason == "response_signals_invalid"
    with pytest.raises(ValueError, match="duplicate_key"):
        parse_envelope('{"reply_text":"first","reply_text":"second","cues":[],"preference_candidates":[]}',
                       request=request)


def test_hm02_eight_complete_rounds_16_messages_and_16kib_then_close_after_100() -> None:
    runtime = ConversationRuntime()
    session, other = runtime.open(), runtime.open()
    for index in range(100):
        publish(runtime, session, f"round-{index}", "u" * 1024, "a" * 1024)
        current = runtime.snapshot(session["id"])
        assert len(current["turns"]) <= 8
        assert current["messages"] <= 16
        assert current["history_bytes"] <= 16384
    assert [item["task_id"] for item in current["turns"]] == [f"round-{i}" for i in range(92, 100)]
    assert current["messages"] == 16 and current["history_bytes"] == 16384
    assert runtime.snapshot(other["id"])["turns"] == []
    publish(runtime, session, "oversize", "x" * 16385)
    assert runtime.snapshot(session["id"])["turns"] == current["turns"]
    publish(runtime, session, "byte-pressure", "x" * 2048, "a")
    trimmed = runtime.snapshot(session["id"])
    assert len(trimmed["turns"]) == 7  # one extra byte evicts two whole old rounds
    assert trimmed["turns"][0]["task_id"] == "round-94"
    frozen = freeze(runtime, session)
    assert all("assistant" not in json.dumps(item) for item in frozen["planning_items"])
    window_ref = weakref.ref(runtime._windows[session["id"]])
    turn_ref = weakref.ref(runtime._windows[session["id"]].turns[-1])
    runtime.close(session["id"], session["epoch"])
    assert window_ref() is None and turn_ref() is None
    assert session["id"] not in runtime._sequences
    with pytest.raises(ConversationConflict):
        runtime.snapshot(session["id"])
    assert runtime.snapshot(other["id"])["turns"] == []


def test_hm02_reference_closures_are_128_per_item_and_512_per_context() -> None:
    runtime = ConversationRuntime()
    session = runtime.open()
    for index in range(5):
        publish(runtime, session, f"refs-{index}", refs=[("m", f"{index}-{n}", 1) for n in range(126)])
    assert len(runtime.snapshot(session["id"])["turns"]) == 5
    frozen = freeze(runtime, session)
    assert len(frozen["refs"]) == 512
    assert "context_reference_limit" in frozen["warnings"]
    assert "history_context_limit" in frozen["warnings"]
    assert len(json.dumps(frozen["history"], ensure_ascii=False).encode("utf-8")) <= 16384
    assert [identity for kind, identity, _ in frozen["refs"]
            if kind == "dialogue_turn" and identity.endswith(":user")] == [
        f"refs-{i}:user" for i in range(1, 5)]
    assert len(frozen["history"]) % 2 == 0
    assert all(len(item["refs"]) == 128 for item in frozen["history"])
    before = runtime.snapshot(session["id"])
    publish(runtime, session, "too-many-refs", refs=[("m", str(n), 1) for n in range(127)])
    assert runtime.snapshot(session["id"]) == before


def test_hm02_planning_caps_drop_whole_round_and_keep_user_plus_all_facts() -> None:
    runtime = ConversationRuntime()
    session = runtime.open()
    for index in range(8):
        publish(runtime, session, f"facts-{index}", request=f"rewrite-{index}",
                response="UNVERIFIED_ASSISTANT_MARKER", facts=[{"result": n} for n in range(3)])
    exact = freeze(runtime, session)
    assert len(exact["planning_items"]) == 32
    assert "UNVERIFIED_ASSISTANT_MARKER" not in json.dumps(exact["planning_items"])
    publish(runtime, session, "facts-8", facts=[{"result": n} for n in range(4)])
    capped = freeze(runtime, session)
    assert len(capped["planning_items"]) == 29
    assert "planning_context_limit" in capped["warnings"]
    assert [item["source_ref"]["id"] for item in capped["planning_items"] if item["kind"] == "user_reference"] == [
        f"facts-{i}:user" for i in range(2, 9)]
    assert sum(item["kind"] == "tool_fact" for item in capped["planning_items"]) == 22

    byte_session = runtime.open()
    publish(runtime, byte_session, "small", request="small", response="")
    publish(runtime, byte_session, "large", request="x" * 15900, response="",
            refs=[("memory", f"source-{n}", 1) for n in range(20)])
    frozen = freeze(runtime, byte_session)
    assert [item["content"] for item in frozen["history"]] == ["small", ""]
    assert "history_context_limit" in frozen["warnings"]
    assert "planning_context_limit" in frozen["warnings"]
    assert len(frozen["planning_items"]) == 1
    assert frozen["planning_items"][0]["content"] == "small"
    assert len(json.dumps(frozen["planning_items"], ensure_ascii=False).encode("utf-8")) <= 16384


class RecordingDomainProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, tuple[ToolSpec, ...]]] = []
        self.fail = False
        self.response = LLMResponse(content="ok")

    def chat(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] = ()) -> LLMResponse:
        self.calls.append(("chat", tuple(messages), tuple(tools)))
        if self.fail:
            raise TimeoutError("synthetic send outcome unknown")
        return self.response

    def generate(self, prompt: str) -> str:
        self.calls.append(("generate", prompt, ()))
        return "ok"


def test_hm03_one_attempt_per_purpose_timeout_unknown_and_restart_never_refund(tmp_path: Path) -> None:
    path = tmp_path / "hm03-call-budget.sqlite3"
    provider = RecordingDomainProvider()
    store = WorkflowStore(path)
    try:
        task = PersistentTaskManager(store).create("synthetic")
        ledger = HumanizationStore(store)
        gateway = ModelCallGateway(provider, ledger.reserve_call, ledger.finish_call)
        gateway.bind(task.id, "planning", lambda: None).chat([ChatMessage("user", "plan")])
        provider.fail = True
        with pytest.raises(TimeoutError):
            gateway.bind(task.id, "expression", lambda: None).chat([ChatMessage("user", "reply")])
        gateway.bind(task.id, "memory_evaluation", lambda: None).generate("evaluate")
        for purpose in ("planning", "expression", "memory_evaluation"):
            with pytest.raises(WorkflowBlocked, match="budget exhausted"):
                gateway.bind(task.id, purpose, lambda: None).generate("retry")
        rows = sql_rows(path, "SELECT purpose,status FROM task_call_attempt ORDER BY purpose")
        assert rows == [("expression", "unknown"), ("memory_evaluation", "finished"), ("planning", "finished")]
        interrupted = PersistentTaskManager(store).create("reserved then interrupted")
        ledger.reserve_call(interrupted.id, "planning")
    finally:
        store.close()
    reopened = WorkflowStore(path)
    try:
        ledger = HumanizationStore(reopened)
        gateway = ModelCallGateway(provider, ledger.reserve_call, ledger.finish_call)
        for purpose in ("planning", "expression", "memory_evaluation"):
            with pytest.raises(WorkflowBlocked, match="budget exhausted"):
                gateway.bind(task.id, purpose, lambda: None).generate("restart retry")
        with pytest.raises(WorkflowBlocked, match="budget exhausted"):
            ledger.reserve_call(interrupted.id, "planning")
        assert sql_rows(path, "SELECT status FROM task_call_attempt WHERE task_id=?", (interrupted.id,)) == [("unknown",)]
        assert len(provider.calls) == 3
    finally:
        reopened.close()


@pytest.mark.parametrize("purpose", ["expression", "memory_evaluation"])
def test_hm03_nonplanning_tool_inputs_and_outputs_are_forbidden(purpose: str) -> None:
    provider = RecordingDomainProvider()
    gateway = ModelCallGateway(provider)
    bound = gateway.bind("one-root", purpose, lambda: None)
    spec = ToolSpec("synthetic", "synthetic tool", {"type": "object"})
    with pytest.raises(LLMError, match="cannot use tools"):
        bound.chat([ChatMessage("user", "hello")], tools=[spec])
    with pytest.raises(LLMError, match="cannot use tools"):
        bound.chat([ChatMessage("tool", "untrusted result")])
    with pytest.raises(LLMError, match="cannot use tools"):
        bound.chat([ChatMessage("assistant", "", tool_calls=(ToolCall("synthetic"),))])
    assert provider.calls == []
    provider.response = LLMResponse(tool_calls=(ToolCall("synthetic"),))
    with pytest.raises(LLMError, match="forbidden tool calls"):
        bound.chat([ChatMessage("user", "hello")])
    assert len(provider.calls) == 1 and provider.calls[0][2] == ()
    with pytest.raises(LLMError, match="budget exhausted"):
        bound.generate("repair")
    assert len(provider.calls) == 1


def test_hm03_gateway_128kib_counts_serialized_messages_and_tool_descriptions() -> None:
    provider = RecordingDomainProvider()
    gateway = ModelCallGateway(provider)
    message = ChatMessage("user", "")
    overhead = len(json.dumps({"messages": [asdict(message)], "tools": []},
                              ensure_ascii=False, allow_nan=False).encode("utf-8"))
    gateway.bind("at-limit", "planning", lambda: None).chat([
        ChatMessage("user", "x" * (131072 - overhead))])
    with pytest.raises(LLMError, match="byte budget"):
        gateway.bind("over-limit", "planning", lambda: None).chat([
            ChatMessage("user", "x" * (131073 - overhead))])
    with pytest.raises(LLMError, match="byte budget"):
        gateway.bind("tool-limit", "planning", lambda: None).chat([message], tools=[
            ToolSpec("synthetic", "x" * 131072, {"type": "object"})])
    gateway.bind("generate-at-limit", "memory_evaluation", lambda: None).generate("x" * 131072)
    with pytest.raises(LLMError, match="byte budget"):
        gateway.bind("generate-over-limit", "expression", lambda: None).generate("x" * 131073)
    assert len(provider.calls) == 2


def test_hm04_untrusted_chat_outcomes_do_not_reward_or_debit(tmp_path: Path) -> None:
    path = tmp_path / "hm04-untrusted.sqlite3"
    with runtime_at(path, Clock()) as runtime:
        before = values(runtime.snapshot())
        for status in ("Completed", "Failed"):
            runtime.record_outcome(status, status, tool_success=None)
            assert receipt(path, status) == ("no_trusted_tool_outcome", 0.0, 0.0, 0.0)
        assert values(runtime.snapshot()) == before
        assert sql_rows(path, "SELECT COUNT(*) FROM budget") == [(0,)]
        runtime.record_outcome("Completed", "Completed", tool_success=True)
        runtime.record_outcome("Failed", "Failed", tool_success=False)
        assert values(runtime.snapshot()) == before
        assert sql_rows(path, "SELECT COUNT(*) FROM budget") == [(0,)]


def test_hm04_delayed_event_uses_terminal_time_for_budget_decay_and_restart(tmp_path: Path) -> None:
    path, clock = tmp_path / "hm04-delay.sqlite3", Clock()
    origin = clock.wall
    with runtime_at(path, clock) as runtime:
        baseline = values(runtime.snapshot())
        clock.advance(30)
        runtime.record_outcome("delayed", "Completed", tool_success=True, occurred_at=origin)
        assert sql_rows(path, "SELECT accepted_utc FROM budget") == [(origin,)]
        assert values(runtime.snapshot())[0] - baseline[0] == pytest.approx(0.08 * 2 ** (-30 / 300), abs=1e-12)
        runtime.record_outcome("exhaust", "Completed", tool_success=True, occurred_at=origin)
        runtime.record_outcome("exhaust-two", "Completed", tool_success=True, occurred_at=origin)
        runtime.record_outcome("exhaust-three", "Completed", tool_success=True, occurred_at=origin)
        assert sql_rows(path, "SELECT SUM(ABS(delta_v)) FROM budget")[0][0] == pytest.approx(0.25)
    restarted_clock = Clock(wall=origin + 59, monotonic=700)
    with runtime_at(path, restarted_clock, enable=False) as runtime:
        before = values(runtime.snapshot())
        runtime.record_outcome("at-59", "Completed", tool_success=True)
        assert values(runtime.snapshot())[0] == pytest.approx(before[0], abs=1e-12)
        restarted_clock.advance(1)
        before = values(runtime.snapshot())
        runtime.record_outcome("at-60", "Completed", tool_success=True)
        assert values(runtime.snapshot())[0] - before[0] == pytest.approx(0.08, abs=1e-12)
        runtime.record_outcome("too-old", "Completed", tool_success=True, occurred_at=origin - 1)
        assert receipt(path, "too-old") == ("event_too_old", 0.0, 0.0, 0.0)
        assert sql_rows(path, "SELECT accepted_utc FROM budget WHERE sequence=(SELECT sequence FROM receipts WHERE task_id='too-old')") == []


@pytest.mark.parametrize(("bracket", "lead", "eligible"), [
    (0.0, 0.004570, True),  # Witness from Windows 3.11's coarse clock failure.
    (0.0, 0.046, True),
    (0.0, 0.047, False),
    (0.008, 0.054, True),
    (0.008, 0.056, False),
    (0.0, 5.0, False),
    (0.1, 0.002, False),  # Startup pause cannot expand future permission.
])
def test_hm04_system_clock_quantization_has_bounded_future_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bracket: float, lead: float, eligible: bool
) -> None:
    path, clock = tmp_path / "hm04-clock-quantization.sqlite3", Clock()
    samples = [clock.monotonic - bracket]
    monkeypatch.setattr(companion_runtime.time, "time", clock.wall_now)
    monkeypatch.setattr(companion_runtime.time, "monotonic",
                        lambda: samples.pop() if samples else clock.monotonic_now())
    monkeypatch.setattr(companion_runtime.time, "get_clock_info",
                        lambda name: SimpleNamespace(resolution=0.015625))
    runtime = CompanionRuntime(path)
    try:
        command(runtime, "configure", enabled=True, emotions_enabled=True)
        runtime.record_outcome("quantized", "Failed", tool_success=False,
                               occurred_at=clock.wall + lead, use_policy="local_only")
        assert receipt(path, "quantized")[0] == ("eligible" if eligible else "event_time_in_future")
        assert runtime.snapshot()["affect"]["mode"] == ("focused" if eligible else "neutral")
        if eligible:
            # Clamp to delivery time; quantization allowance must not move the
            # budget or focused TTL into the future, including duplicate drain.
            origin = clock.wall
            assert sql_rows(path, "SELECT accepted_utc FROM budget") == [(origin,)]
            clock.advance(30)
            runtime.record_outcome("quantized", "Failed", tool_success=False,
                                   occurred_at=origin + lead, use_policy="local_only")
            assert sql_rows(path, "SELECT accepted_utc FROM budget") == [(origin,)]
            assert runtime.snapshot()["affect"]["mode"] == "focused"
            clock.advance(30)
            assert runtime.snapshot()["affect"]["mode"] == "neutral"
            clock.advance(0.004570)
            runtime.record_outcome("expired", "Failed", tool_success=False,
                                   occurred_at=origin, use_policy="local_only")
            assert receipt(path, "expired")[0] == "event_too_old"
            assert sql_rows(path, "SELECT accepted_utc FROM budget") == [(origin,)]
            assert runtime.snapshot()["affect"]["mode"] == "neutral"
        else:
            assert sql_rows(path, "SELECT COUNT(*) FROM budget") == [(0,)]
    finally:
        runtime.close()


@pytest.mark.parametrize("resolution", [float("nan"), float("inf"), -1.0, 0.0, 1.0])
def test_hm04_unusable_clock_resolution_keeps_strict_future_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolution: float
) -> None:
    clock = Clock()
    monkeypatch.setattr(companion_runtime.time, "time", clock.wall_now)
    monkeypatch.setattr(companion_runtime.time, "monotonic", clock.monotonic_now)
    monkeypatch.setattr(companion_runtime.time, "get_clock_info",
                        lambda name: SimpleNamespace(resolution=resolution))
    path = tmp_path / "hm04-invalid-clock-resolution.sqlite3"
    runtime = CompanionRuntime(path)
    try:
        command(runtime, "configure", enabled=True, emotions_enabled=True)
        runtime.record_outcome("future", "Failed", tool_success=False, occurred_at=clock.wall + 0.002)
        assert receipt(path, "future")[0] == "event_time_in_future"
        assert runtime.snapshot()["affect"]["mode"] == "neutral"
        assert sql_rows(path, "SELECT COUNT(*) FROM budget") == [(0,)]
    finally:
        runtime.close()


def test_hm04_injected_clock_and_reset_anchor_keep_strict_time_boundaries(tmp_path: Path) -> None:
    path, clock = tmp_path / "hm04-injected-clock.sqlite3", Clock()
    with runtime_at(path, clock) as runtime:
        runtime.record_outcome("future", "Failed", tool_success=False, occurred_at=clock.wall + 0.002)
        assert receipt(path, "future")[0] == "event_time_in_future"
        runtime.record_outcome("before-reset", "Failed", tool_success=False, occurred_at=clock.wall - 0.001)
        assert receipt(path, "before-reset")[0] == "event_precedes_current_anchor"
        assert runtime.snapshot()["affect"]["mode"] == "neutral"
        assert sql_rows(path, "SELECT COUNT(*) FROM budget") == [(0,)]


@pytest.mark.parametrize("injected", ["wall", "monotonic", "both"])
def test_hm04_injecting_either_clock_preserves_constructor_sampling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, injected: str
) -> None:
    clock, calls = Clock(), {"wall": 0, "monotonic": 0}

    def wall_now() -> float:
        calls["wall"] += 1
        return clock.wall

    def monotonic_now() -> float:
        calls["monotonic"] += 1
        return clock.monotonic

    def unexpected_clock_info(name: str) -> Any:
        raise AssertionError("Injected clock must not use system calibration")

    monkeypatch.setattr(companion_runtime.time, "time", wall_now)
    monkeypatch.setattr(companion_runtime.time, "monotonic", monotonic_now)
    monkeypatch.setattr(companion_runtime.time, "get_clock_info", unexpected_clock_info)
    runtime = CompanionRuntime(tmp_path / "hm04-clock-sampling.sqlite3",
        wall_clock=wall_now if injected in ("wall", "both") else None,
        monotonic_clock=monotonic_now if injected in ("monotonic", "both") else None)
    try:
        assert calls == {"wall": 1, "monotonic": 1}
        command(runtime, "configure", enabled=True, emotions_enabled=True)
        runtime.record_outcome("future", "Failed", tool_success=False, occurred_at=clock.wall + 0.002)
        assert runtime.snapshot()["affect"]["mode"] == "neutral"
    finally:
        runtime.close()
