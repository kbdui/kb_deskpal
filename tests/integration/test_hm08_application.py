"""HR05: exercise revised expression semantics through the production facade."""

from dataclasses import asdict, replace
import io
import json

import pytest

from deskpal.application.factory import create_application
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolCall
from deskpal.llm.providers import OpenAILLMProvider
from deskpal.llm.errors import LLMError
from deskpal.response.templates import tool_summary

from test_humanization_application import (
    cloud, configure, envelope, hm_config, last_expression, run_turn, stages, wire,
)


def test_hr05_expression_sources_reach_model_without_promoting_drafts(hm_config, wire):
    request = "活动在周六举行，请先拟一份旧书交换通知，未确定规则留待确认。"
    suggestion = "建议每人最多带五本书，具体数量仍需确认。"
    wire.plans[request] = (ToolCall("echo", {"text": "synthetic verified venue record"}, "venue"),)
    wire.replies[request] = envelope(suggestion)
    with create_application(hm_config) as app:
        configure(app, enabled=True)
        conversation = app.conversation_open()
        first = run_turn(app, request, conversation=conversation)
        assert first.status == TaskStatus.COMPLETED
        for followup in ("就按这个。", "请压缩上一版，同时保留未确定的信息。"):
            wire.calls.clear()
            result = run_turn(app, followup, conversation=conversation)
            assert result.status == TaskStatus.COMPLETED
            assert stages(wire) == ["planning", "expression"]
            expression = json.loads(last_expression(wire)["messages"][-1]["content"])
            history = expression["history"]
            original = next(item for item in history if item["role"] == "user" and item["content"] == request)
            assert original["source_kind"] == "historical_user_reference"
            assert original["externally_verified"] is False
            draft = next(item for item in history if item["role"] == "assistant" and suggestion in item["content"])
            assert draft["source_kind"] == "assistant_proposal"
            assert draft["confirmed"] is False
            assert draft["draft_id"] == first.task_id + ":assistant"
            assert draft["draft_version"] == 1
            assert draft["tool_facts"][0]["verified"] is True
            assert draft["tool_facts"][0]["fact"]["success"] is True
            assert "synthetic verified venue record" in json.dumps(draft["tool_facts"])
            planner = json.dumps(wire.calls[0], ensure_ascii=False)
            assert suggestion not in planner and "assistant_proposal" not in planner
            assert request in planner and "synthetic verified venue record" in planner
            if followup.startswith("请压缩"):
                approval = next(item for item in history if item["content"] == "就按这个。")
                assert approval["externally_verified"] is False


def test_hr05_once_conflict_drops_preference_group_without_losing_valid_cue(hm_config, wire):
    request = "我今天很累。这一轮简短些。不要追问。"
    start = request.index("这一轮")
    wire.replies[request] = envelope("先做一个小步骤即可。",
        cues=[{"mode": "supportive", "evidence_kind": "explicit_report", "span": [0, 5]}],
        preferences=[{"key": "verbosity", "value": "brief", "scope": "session", "span": [start, start + 7]}])
    with create_application(hm_config) as app:
        configure(app, enabled=True, emotions_enabled=True, preset="balanced")
        conversation = app.conversation_open()
        result = run_turn(app, request, conversation=conversation)
        assert result.status == TaskStatus.COMPLETED
        assert result.response == "先做一个小步骤即可。"
        assert "response_signals_invalid" in result.warnings
        assert any("once_only" in warning for warning in result.warnings)
        snapshot = app.interaction_snapshot()
        assert snapshot["candidates"] == []
        assert snapshot["cues"][conversation["conversation_id"]]["mode"] == "supportive"
        assert snapshot["preferences"].get("persistent", {}) == {}
        assert stages(wire) == ["planning", "expression"]
        assert "篇幅简洁" in last_expression(wire)["messages"][0]["content"]
        policy = json.loads(last_expression(wire)["messages"][-1]["content"])["expression_policy"]
        assert policy == {"verbosity": "brief", "humor_allowed": False, "followup_allowed": False}
        wire.calls.clear()
        assert run_turn(app, "请继续原来的活动话题。", conversation=conversation).status == TaskStatus.COMPLETED
        assert stages(wire) == ["planning", "expression"]
        assert "篇幅简洁" not in last_expression(wire)["messages"][0]["content"]
        assert json.loads(last_expression(wire)["messages"][-1]["content"])["expression_policy"]["verbosity"] == "normal"


def test_hr05_invalid_envelope_does_not_publish_raw_metadata_or_retry(hm_config, wire):
    request = "小说角色的台词需要改写。"
    wire.replies[request] = "RAW_UNTRUSTED_REPLY_WITHOUT_JSON"
    with create_application(hm_config) as app:
        configure(app, enabled=True, emotions_enabled=True)
        conversation = app.conversation_open()
        result = run_turn(app, request, conversation=conversation)
        assert result.status == TaskStatus.COMPLETED
        assert "RAW_UNTRUSTED_REPLY_WITHOUT_JSON" not in result.response
        assert any("json_invalid" in warning for warning in result.warnings)
        assert app.interaction_snapshot()["candidates"] == []
        assert app.interaction_snapshot()["cues"] == {}
        assert stages(wire) == ["planning", "expression"]
        history = app.conversation_snapshot(conversation["conversation_id"])
        assert "RAW_UNTRUSTED_REPLY_WITHOUT_JSON" not in json.dumps(history)


def test_hr05_native_json_reaches_transport_only_for_expression_and_never_replays(hm_config, monkeypatch):
    from deskpal.application import factory

    payloads = []

    def transport(request, *, timeout):
        payload = json.loads(request.data)
        payloads.append(payload)
        expression = payload["input"][0]["content"].startswith("[EXPRESSION]")
        body = envelope("欢迎带来旧书。") if expression else "synthetic neutral draft"
        return io.BytesIO(json.dumps({"output": [{"type": "message", "content": [
            {"type": "output_text", "text": body}]}]}).encode("utf-8"))

    monkeypatch.setattr(factory, "OpenAILLMProvider",
                        lambda config, **kwargs: OpenAILLMProvider(config, transport=transport, **kwargs))
    settings = cloud(hm_config)
    settings = replace(settings, llm=replace(settings.llm, expression_output_format="json_object"))
    with create_application(settings) as app:
        configure(app, enabled=True)
        result = run_turn(app, "请写一句活动欢迎语。")
        assert result.status == TaskStatus.COMPLETED and result.response == "欢迎带来旧书。"
        assert len(payloads) == 2
        planning, expression = payloads
        assert "text" not in planning and planning["tools"]
        assert expression["text"] == {"format": {"type": "json_object"}}
        assert "tools" not in expression
        assert planning["store"] is False and expression["store"] is False
        assert app.wait(result.task_id, 1).response == result.response
        assert len(payloads) == 2


def test_hr05_empty_tool_supplement_preserves_receipt_and_valid_cue(hm_config, wire):
    request = "我很累，请执行合成回声并说明结果。"
    wire.plans[request] = (ToolCall("echo", {"text": "HM_COMPLETE_SYNTHETIC_BODY"}, "empty-supplement"),)
    wire.replies[request] = envelope("", cues=[{
        "mode": "supportive", "evidence_kind": "explicit_report", "span": [0, 3]}])
    with create_application(hm_config) as app:
        configure(app, enabled=True, emotions_enabled=True)
        conversation = app.conversation_open()
        result = run_turn(app, request, conversation=conversation)
        assert result.status == TaskStatus.COMPLETED
        assert result.response == tool_summary(tuple(asdict(item) for item in result.tool_results))
        assert "补充说明" not in result.response
        assert not any("Response generation failed" in warning or "response_" in warning for warning in result.warnings)
        assert len(app.journal.attempts(result.task_id)) == 1
        assert stages(wire) == ["planning", "expression"]
        assert app.interaction_snapshot()["cues"][conversation["conversation_id"]]["mode"] == "supportive"
        payload = json.loads(last_expression(wire)["messages"][-1]["content"])
        assert payload["reply_role"] == "tool_supplement"
        assert payload["tool_results"] == [asdict(item) for item in result.tool_results]
        assert payload["tool_results"][0]["output"] == "HM_COMPLETE_SYNTHETIC_BODY"
        assert app.wait(result.task_id, 1).response == result.response
        assert stages(wire) == ["planning", "expression"]
        with app.store.transaction() as conn:
            budget = [tuple(row) for row in conn.execute(
                "SELECT purpose,COUNT(*) FROM task_call_attempt WHERE task_id=? GROUP BY purpose", (result.task_id,))]
        assert sorted(budget) == [("expression", 1), ("planning", 1)]


def test_hr05_empty_direct_answer_still_degrades(hm_config, wire):
    request = "请直接给出活动建议。"
    wire.replies[request] = envelope("")
    with create_application(hm_config) as app:
        configure(app, enabled=True)
        result = run_turn(app, request)
        assert result.status == TaskStatus.COMPLETED and result.response
        assert any("reply_invalid" in warning for warning in result.warnings)
        assert any("Response generation failed" in warning for warning in result.warnings)
        assert json.loads(last_expression(wire)["messages"][-1]["content"])["reply_role"] == "direct_answer"
        assert stages(wire) == ["planning", "expression"]


@pytest.mark.parametrize("body", ["malformed json", LLMError("LLM returned no usable output"),
    envelope("", cues=[{"mode": "supportive", "evidence_kind": "explicit_report", "span": [0, 999]}])])
def test_hr05_invalid_or_missing_tool_supplement_keeps_failure_fallback(hm_config, wire, body):
    request = "请执行合成回声。"
    wire.plans[request] = (ToolCall("echo", {"text": "HM_TRUSTED_ONCE"}, "fallback"),)
    wire.replies[request] = body
    with create_application(hm_config) as app:
        configure(app, enabled=True)
        result = run_turn(app, request)
        assert result.status == TaskStatus.COMPLETED
        assert result.response == tool_summary(tuple(asdict(item) for item in result.tool_results))
        assert "补充说明" not in result.response
        assert any("Response generation failed" in warning for warning in result.warnings)
        assert len(app.journal.attempts(result.task_id)) == 1
        assert stages(wire) == ["planning", "expression"]
