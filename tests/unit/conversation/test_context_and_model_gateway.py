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

from tests.support.database import sql_rows


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


