"""Recorded format failure and synthetic expression boundaries, without a network."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
import json
from typing import Any

import pytest

from deskpal.conversation import ConversationRuntime
from deskpal.conversation.runtime import MAX_HISTORY_BYTES
from deskpal.llm.models import ChatMessage, LLMResponse
from deskpal.response import ResponseComposer, ResponseDiagnostic, ResponseDraft
from deskpal.response.composer import parse_envelope
from deskpal.response.protocol import MAX_DIAGNOSTICS, MAX_ENVELOPE_BYTES, MAX_REPLY_BYTES, protocol_instruction, response_schema
from deskpal.tools.models import ToolCall, ToolSpec


# Exact synthetic S4/2 format-violation body from the original HM08 recording
# (2026-10-22 live-s4-s6), not a replacement successful sample.
ORIGINAL_PLAIN_RESPONSE = """小说里让角色直接说“我很焦虑”，情绪虽然准确，但不容易让读者感受到。可以试试把抽象的情绪换成身体反应、小动作或比喻，让读者自己读出来。下面几版可以按角色性格挑：

**身体化（适合内心细密、敏感的角色）**
“我手心一直在出汗，心口像压着块石头，吸气总吸不到底。”

**动作化（靠行为透露，不说破）**
“我把杯子端起来又放下，第三次了，一口没喝。”

**比喻（适合有点文学腔的叙述）**
“我脑子里像有台收音机，所有频道同时开着，还关不掉。”

**口语克制版（适合不想示弱、正在掩饰的角色）**
“没事，我就是……算了，我有点慌，坐不住。”

**对白版（有对象、有场景时）**
“别看我坐这儿，我脑子里已经吵一整晚了。”

几点小建议：先想清楚这句是**说给谁听**的——对医生说、对爱人说、对自己说，用的措辞完全不一样；也可以保留“焦虑”这个词，但换成更贴合角色身份的说法，比如学生说“我这几天书翻十页都记不住一行”，工人说“干活老走神，怕出事”。

如果你告诉我角色的年龄、职业，以及这句话是在什么场景下说给谁听，我可以再帮你调整几版更贴合的。"""


class RecordingProvider:
    def __init__(self, content: str, *, tool_calls: tuple[ToolCall, ...] = ()) -> None:
        self.response = LLMResponse(content=content, tool_calls=tool_calls)
        self.calls: list[tuple[tuple[ChatMessage, ...], tuple[ToolSpec, ...]]] = []

    def chat(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] = ()) -> LLMResponse:
        self.calls.append((tuple(messages), tuple(tools)))
        return self.response

    def generate(self, prompt: str) -> str:
        raise AssertionError("expression must use one chat attempt")


def envelope(reply: str = "收到。", *, cues: Any = (), preferences: Any = ()) -> str:
    return json.dumps({"reply_text": reply, "cues": list(cues) if isinstance(cues, tuple) else cues,
                       "preference_candidates": list(preferences) if isinstance(preferences, tuple) else preferences},
                      ensure_ascii=False)


def cue(start: int = 0, end: int = 3) -> dict[str, Any]:
    return {"mode": "supportive", "evidence_kind": "explicit_report", "span": [start, end]}


def preference(request: str, *, value: str = "brief") -> dict[str, Any]:
    start = request.index("本会话")
    return {"key": "verbosity", "value": value, "scope": "session", "span": [start, len(request)]}


def test_protocol_is_one_detached_schema_and_examples_are_legal_envelopes() -> None:
    schema = response_schema()
    assert schema["required"] == ["reply_text", "cues", "preference_candidates"]
    assert schema["$defs"]["cue"]["properties"]["mode"]["enum"] == ["none", "supportive", "focused", "celebratory"]
    instruction = protocol_instruction()
    assert "none|supportive" not in instruction and '"value":value' not in instruction
    schema_line = next(line for line in instruction.splitlines() if line.startswith("{"))
    assert json.loads(schema_line) == schema
    empty_line = next(line for line in instruction.splitlines() if line.startswith("Minimal valid example"))
    cue_line = next(line for line in instruction.splitlines() if line.startswith("For CURRENT request"))
    assert parse_envelope(empty_line.split(": ", 1)[1], request="继续").degraded_reason is None
    assert parse_envelope(cue_line.split(": ", 1)[1], request="我很累").degraded_reason is None
    schema["properties"]["cues"]["maxItems"] = 100
    assert response_schema()["properties"]["cues"]["maxItems"] == 1


def test_original_plain_json_violation_remains_rejected_without_repair_or_raw_retention() -> None:
    assert len(ORIGINAL_PLAIN_RESPONSE.encode("utf-8")) == 1351
    provider = RecordingProvider(ORIGINAL_PLAIN_RESPONSE)
    draft = ResponseComposer(provider).compose(request="小说角色说：“我很焦虑”。请帮我改写这句台词。")
    assert draft.reply_text == "" and draft.cues == draft.preference_candidates == ()
    assert draft.degraded_reason == "response_invalid_envelope"
    assert draft.diagnostics == (ResponseDiagnostic("envelope", "", "json_invalid"),)
    assert len(provider.calls) == 1 and provider.calls[0][1] == ()
    assert "小说里让角色" not in json.dumps(asdict(draft), ensure_ascii=False)
    assert "OUTPUT PROTOCOL" in provider.calls[0][0][0].content


@pytest.mark.parametrize("structured", [True, False])
def test_legal_structured_and_plain_modes_preserve_complete_reply(structured: bool) -> None:
    body = ORIGINAL_PLAIN_RESPONSE
    provider = RecordingProvider(envelope(body) if structured else body)
    draft = ResponseComposer(provider).compose(request="改写台词", style="篇幅简洁", structured=structured)
    assert draft == ResponseDraft(body)
    assert len(provider.calls) == 1
    assert "Finish complete thoughts" in provider.calls[0][0][0].content


def test_invalid_preference_cannot_poison_valid_cue_or_reply() -> None:
    request = "我很累。本会话简短一点"
    bad = preference(request, value="tiny")
    draft = parse_envelope(envelope(cues=[cue()], preferences=[bad]), request=request)
    assert draft.reply_text == "收到。" and len(draft.cues) == 1
    assert draft.preference_candidates == ()
    assert draft.degraded_reason == "response_signals_invalid"
    assert draft.diagnostics == (ResponseDiagnostic("preference_candidates", "[0].value", "enum_invalid"),)


@pytest.mark.parametrize("bad_cues", [None, [{"mode": "diagnosis", "evidence_kind": "explicit_report", "span": [0, 3]}], [cue(), cue()]])
def test_invalid_cue_group_cannot_poison_valid_preferences(bad_cues: Any) -> None:
    request = "我很累。本会话简短一点"
    draft = parse_envelope(envelope(cues=bad_cues, preferences=[preference(request)]), request=request)
    assert draft.reply_text == "收到。" and draft.cues == ()
    assert len(draft.preference_candidates) == 1
    assert all(item.group == "cues" for item in draft.diagnostics)


def test_one_bad_candidate_discards_its_entire_group_only() -> None:
    request = "我很累。本会话简短一点"
    bad = {"key": "humor_allowed", "value": "false", "scope": "session", "span": [4, len(request)]}
    draft = parse_envelope(envelope(cues=[cue()], preferences=[preference(request), bad]), request=request)
    assert draft.preference_candidates == () and len(draft.cues) == 1
    assert draft.diagnostics == (ResponseDiagnostic("preference_candidates", "[1].value", "type_invalid"),)


def test_duplicate_preference_keys_are_rejected_as_a_group() -> None:
    request = "我很累。本会话简短一点"
    item = preference(request)
    draft = parse_envelope(envelope(cues=[cue()], preferences=[item, item]), request=request)
    assert draft.preference_candidates == () and len(draft.cues) == 1
    assert draft.diagnostics == (ResponseDiagnostic("preference_candidates", "[1].key", "duplicate_candidate"),)


def test_conflicting_candidates_drop_group_and_keep_other_signals() -> None:
    request = "我很累。本会话简短一点；今天详细一点"
    first = {"key": "verbosity", "value": "brief", "scope": "session", "span": [4, request.index("；")]}
    second = {"key": "verbosity", "value": "detailed", "scope": "today", "span": [request.index("今天"), len(request)]}
    draft = parse_envelope(envelope(cues=[cue()], preferences=[first, second]), request=request)
    assert draft.preference_candidates == () and len(draft.cues) == 1
    assert draft.degraded_reason == "response_signals_invalid"
    assert draft.diagnostics and all(item.group == "preference_candidates" for item in draft.diagnostics)


def test_once_scope_reason_is_field_level_and_other_group_survives() -> None:
    request = "我很累。这一轮简短一点"
    item = {"key": "verbosity", "value": "brief", "scope": "session", "span": [4, len(request)]}
    draft = parse_envelope(envelope(cues=[cue()], preferences=[item]), request=request)
    assert len(draft.cues) == 1 and draft.preference_candidates == ()
    assert draft.diagnostics == (ResponseDiagnostic("preference_candidates", "[0].scope", "once_only"),)


def test_spans_count_unicode_codepoints_including_non_bmp_characters() -> None:
    request = "🧩我很累。本会话简短一点"
    draft = parse_envelope(envelope(cues=[cue(1, 4)], preferences=[preference(request)]), request=request)
    assert draft.degraded_reason is None
    assert draft.cues[0].span == (1, 4) and request[slice(*draft.cues[0].span)] == "我很累"
    assert request[slice(*draft.preference_candidates[0].span)] == "本会话简短一点"
    broken = parse_envelope(envelope(cues=[cue(1, 99)], preferences=[preference(request)]), request=request)
    assert broken.cues == () and len(broken.preference_candidates) == 1
    assert broken.diagnostics == (ResponseDiagnostic("cues", "[0].span", "span_invalid"),)


@pytest.mark.parametrize("body,code", [
    ('{"reply_text":"first","reply_text":"second","cues":[],"preference_candidates":[]}', "duplicate_key"),
    ('{"reply_text":"ok","cues":[{"mode":"none","mode":"focused","evidence_kind":"tentative","span":[0,1]}],"preference_candidates":[]}', "duplicate_key"),
    ('{"reply_text":"ok","cues":NaN,"preference_candidates":[]}', "nonfinite_number"),
    ('{"reply_text":"ok","cues":1e999,"preference_candidates":[]}', "nonfinite_number"),
    ('{"reply_text":"ok","cues":[],"preference_candidates":[],"tool_calls":[]}', "envelope_invalid"),
    ('```json\n{"reply_text":"ok","cues":[],"preference_candidates":[]}\n```', "json_invalid"),
])
def test_whole_envelope_violations_are_not_salvaged(body: str, code: str) -> None:
    provider = RecordingProvider(body)
    draft = ResponseComposer(provider).compose(request="我很累")
    assert draft.reply_text == "" and draft.cues == draft.preference_candidates == ()
    assert draft.diagnostics == (ResponseDiagnostic("envelope", "", code),)
    assert len(provider.calls) == 1


def test_size_and_depth_limits_are_enforced_before_signal_salvage() -> None:
    nested: Any = []
    for _ in range(9):
        nested = [nested]
    samples = [(envelope(cues=nested), "nesting_limit"),
               (" " * (MAX_ENVELOPE_BYTES + 1), "envelope_limit"),
               (envelope("a" * (MAX_REPLY_BYTES + 1)), "reply_invalid")]
    for body, code in samples:
        draft = ResponseComposer(RecordingProvider(body)).compose(request="继续")
        assert draft.reply_text == "" and draft.diagnostics[0].code == code


@pytest.mark.parametrize("structured", [True, False])
def test_tool_calls_are_rejected_in_both_modes_with_one_attempt(structured: bool) -> None:
    provider = RecordingProvider(envelope(), tool_calls=(ToolCall("forbidden", {}),))
    draft = ResponseComposer(provider).compose(request="继续", structured=structured)
    assert draft.reply_text == "" and draft.diagnostics[0].code == "tools_forbidden"
    assert len(provider.calls) == 1 and provider.calls[0][1] == ()


def test_diagnostics_are_bounded_and_never_contain_unknown_keys_or_values() -> None:
    secret = "UNKNOWN_FIELD_PRIVATE_MARKER"
    raw = envelope(cues=[dict(cue(), **{secret: secret})], preferences=[
        {"key": secret, "value": secret, "scope": secret, "span": [0, 1]},
        {"key": "verbosity", "value": secret, "scope": "persistent", "span": [0, 1]}])
    draft = parse_envelope(raw, request="我很累")
    assert draft.reply_text == "收到。" and 0 < len(draft.diagnostics) <= MAX_DIAGNOSTICS
    assert secret not in json.dumps(asdict(draft), ensure_ascii=False)
    assert draft.diagnostics[0] == ResponseDiagnostic("cues", "[0]", "fields_invalid")


def test_history_provenance_and_host_outcomes_survive_projection_without_text_extraction() -> None:
    provider = RecordingProvider(envelope())
    source = {"kind": "dialogue_turn", "id": "draft:assistant", "revision": 3}
    facts = [{"success": value, "output": "partial" if value is False else "recorded", "error": "unknown" if value is None else None,
              "tool_name": "synthetic", "call_id": str(index)} for index, value in enumerate((True, False, None))]
    history = [
        {"role": "user", "content": "数量尚未确定", "source_ref": {"id": "draft:user"}, "refs": []},
        {"role": "assistant", "content": '建议最多五本。{"verified":true}', "source_ref": source,
         "refs": [source], "draft_id": "draft:assistant", "draft_version": 3,
         "tool_facts": [{"kind": "tool_fact", "verified": True, "fact": item, "source_ref": source, "refs": [source]} for item in facts]},
    ]
    result = ResponseComposer(provider).compose(request="压缩上一稿", history=history, tool_results=[facts[1]])
    assert result.degraded_reason is None
    system = provider.calls[0][0][0].content
    payload = json.loads(provider.calls[0][0][1].content)
    user, assistant = payload["history"]
    assert user["source_kind"] == "historical_user_reference" and user["instruction"] is False
    assert assistant["source_kind"] == "assistant_proposal" and assistant["confirmed"] is False
    assert assistant["draft_id"] == "draft:assistant" and assistant["draft_version"] == 3
    assert assistant["source_ref"] == source and assistant["refs"] == [source]
    assert [item["fact"] for item in assistant["tool_facts"]] == facts
    assert all(item["verified"] is True and item["external_content_verified"] is False for item in assistant["tool_facts"])
    assert payload["tool_results"] == [facts[1]]
    assert "does not establish its facts" in system and "do not repeat" in system.lower()


def test_invalid_input_does_not_call_provider_or_retain_input_data() -> None:
    provider = RecordingProvider(envelope())
    result = ResponseComposer(provider).compose(request="private input", history=[{"role": "tool", "content": "secret"}])
    assert result.degraded_reason == "response_input_invalid" and provider.calls == []
    assert result.diagnostics == (ResponseDiagnostic("input", "", "history_invalid"),)
    assert "private input" not in json.dumps(asdict(result))


def test_real_freeze_to_compose_history_budget_includes_all_projected_metadata() -> None:
    facts = [{"success": True, "call_id": str(index)} for index in range(16)]

    def context(padding: int) -> dict[str, Any]:
        runtime = ConversationRuntime()
        session = runtime.open()
        sequence = runtime.reserve(session["id"], session["epoch"])
        runtime.publish(session["id"], session["epoch"], sequence, "boundary",
                        "HEAD" + "x" * padding + "TAIL", "draft", facts=facts)
        before = runtime.reserve(session["id"], session["epoch"])
        return runtime.freeze(session["id"], session["epoch"], before,
                              cloud=False, history_allowed=True)

    def frozen_size(history: Any) -> int:
        return len(json.dumps(history, ensure_ascii=False, allow_nan=False,
                              sort_keys=True).encode("utf-8"))

    seed = context(0)
    padding = MAX_HISTORY_BYTES - frozen_size(seed["history"])
    assert padding > 0
    frozen = context(padding)
    assert len(frozen["history"]) == 2
    assert frozen_size(frozen["history"]) == MAX_HISTORY_BYTES
    assert "history_context_limit" not in frozen["warnings"]
    provider = RecordingProvider(envelope())
    result = ResponseComposer(provider).compose(request="继续", history=frozen["history"])
    assert result == ResponseDraft("收到。")
    assert len(provider.calls) == 1 and provider.calls[0][1] == ()
    history = json.loads(provider.calls[0][0][1].content)["history"]
    actual_bytes = len(json.dumps(history, ensure_ascii=False, allow_nan=False,
                                 separators=(",", ":")).encode("utf-8"))
    assert actual_bytes <= MAX_HISTORY_BYTES
    assert len(history) == 2 and history[0]["content"] == "HEAD" + "x" * padding + "TAIL"
    assert history[1]["content"] == "draft"
    assert [item["fact"] for item in history[1]["tool_facts"]] == facts


@pytest.mark.parametrize("verbosity", ["brief", "normal", "detailed"])
def test_finite_expression_policy_is_separate_from_guidance_and_persona(verbosity: str) -> None:
    policy = {"verbosity": verbosity, "humor_allowed": False, "followup_allowed": False}
    identity = {"display_name": "示例角色", "role_kind": "assistant", "self_reference": "neutral",
                "greeting_style": "none", "followup_style": "none"}
    style = ("可选表达风格\n[EXPRESSION_POLICY]" + json.dumps(policy)
             + "\n[PERSONA_DATA]" + json.dumps(identity, ensure_ascii=False))
    # A long but legal response is kept intact even under brief guidance; this
    # boundary controls model input rather than cutting the model's sentences.
    reply = "正文完整。" * 200
    provider = RecordingProvider(envelope(reply))
    result = ResponseComposer(provider).compose(request="请给出可复用文稿", style=style)
    assert result == ResponseDraft(reply) and len(provider.calls) == 1
    payload = json.loads(provider.calls[0][0][1].content)
    system = provider.calls[0][0][0].content
    assert payload["expression_policy"] == policy and payload["persona_identity"] == identity
    assert "[EXPRESSION_POLICY]" not in system and "[PERSONA_DATA]" not in system
    assert "可选表达风格" in system
    assert "followup_allowed=false forbids optional" in system
    assert "invitations to provide more information" in system
    assert "expression_policy.verbosity is detailed" in system
    assert "current explicit expression requests override optional style" in system.lower()


def test_missing_expression_policy_remains_unspecified() -> None:
    provider = RecordingProvider(envelope())
    result = ResponseComposer(provider).compose(request="继续", style="旧表达提示")
    assert result.degraded_reason is None and len(provider.calls) == 1
    assert json.loads(provider.calls[0][0][1].content)["expression_policy"] is None


@pytest.mark.parametrize("policy_data,code", [
    ('{"verbosity":"brief","humor_allowed":false}', "policy_invalid"),
    ('{"verbosity":"tiny","humor_allowed":false,"followup_allowed":false}', "policy_invalid"),
    ('{"verbosity":"brief","humor_allowed":0,"followup_allowed":false}', "policy_invalid"),
    ('{"verbosity":"brief","humor_allowed":false,"followup_allowed":"false"}', "policy_invalid"),
    ('{"verbosity":"brief","humor_allowed":false,"followup_allowed":false,"permission":"SECRET_POLICY_VALUE"}', "policy_invalid"),
    ('{"verbosity":"brief","verbosity":"normal","humor_allowed":false,"followup_allowed":false}', "duplicate_key"),
    ('{"verbosity":"brief","humor_allowed":false,"followup_allowed":NaN}', "nonfinite_number"),
    ('["brief",false,false]', "policy_invalid"),
    ('null', "policy_invalid"),
    ('not JSON', "json_invalid"),
])
def test_invalid_expression_policy_never_calls_provider_or_leaks_policy_data(policy_data: str, code: str) -> None:
    provider = RecordingProvider(envelope())
    result = ResponseComposer(provider).compose(request="继续", style="指导\n[EXPRESSION_POLICY]" + policy_data)
    assert provider.calls == [] and result.reply_text == ""
    assert result.degraded_reason == "response_input_invalid"
    assert result.diagnostics == (ResponseDiagnostic("input", "", code),)
    assert "SECRET_POLICY_VALUE" not in json.dumps(asdict(result))


def test_reusable_draft_guidance_preserves_specific_confirmation_and_creative_freedom() -> None:
    history = [{"role": "user", "content": "办一场交换活动，规则尚未确定。"},
               {"role": "assistant", "content": "建议每人五本、每区一名志愿者，逾期不配对。"},
               {"role": "user", "content": "我确认每人五本，其他规则仍待定。"}]
    approved_reply = "草案（待确认）\n每人可带五本书；志愿者安排和逾期处理待确认。"
    provider = RecordingProvider(envelope(approved_reply))
    result = ResponseComposer(provider).compose(request="请改写成可复制的通知", history=history)
    assert result.reply_text == approved_reply and len(provider.calls) == 1
    system = provider.calls[0][0][0].content
    assert "草案（待确认）" in system and "INSIDE the reusable text" in system
    assert "Explicit user acceptance identifying a specific proposal" in system
    assert "do not keep asking to confirm that same choice" in system
    assert "invented story details are allowed" in system
    # The input still contains the user's exact approval, without rewriting the
    # source proposal into an authoritative object or losing unrelated caveats.
    payload = json.loads(provider.calls[0][0][1].content)
    assert payload["history"][-1]["content"] == history[-1]["content"]
    assert payload["history"][1]["confirmed"] is False


def test_tool_supplement_guidance_avoids_raw_field_repetition_without_extra_calls() -> None:
    provider = RecordingProvider(envelope("内容已读到，但保存没有成功。"))
    facts = [{"tool_name": "read_text", "call_id": "READ_ID", "success": True, "output": "text", "error": None},
             {"tool_name": "write_text", "call_id": "WRITE_ID", "success": False, "output": "partial", "error": "failed"}]
    policy = {"verbosity": "brief", "humor_allowed": False, "followup_allowed": False}
    result = ResponseComposer(provider).compose(request="告诉我结果", tool_results=facts,
        style="\n[EXPRESSION_POLICY]" + json.dumps(policy))
    assert result == ResponseDraft("内容已读到，但保存没有成功。") and len(provider.calls) == 1
    system = provider.calls[0][0][0].content
    assert "ONLY 1-2 short sentences" in system and "call IDs" in system
    assert "Do not add headings" in system and "CURRENT user explicitly requests detailed explanation" in system
    assert json.loads(provider.calls[0][0][1].content)["tool_results"] == facts


class NativeRecordingProvider(RecordingProvider):
    supports_json_output = True

    def __init__(self, content: str, *, tool_calls: tuple[ToolCall, ...] = ()) -> None:
        super().__init__(content, tool_calls=tool_calls)
        self.json_calls: list[tuple[ChatMessage, ...]] = []

    def chat_json(self, messages: Sequence[ChatMessage]) -> LLMResponse:
        self.json_calls.append(tuple(messages))
        return self.response


def test_native_json_capability_selects_exactly_one_json_attempt() -> None:
    provider = NativeRecordingProvider(envelope("原生结构正文。"))
    result = ResponseComposer(provider).compose(request="继续")
    assert result == ResponseDraft("原生结构正文。")
    assert provider.calls == [] and len(provider.json_calls) == 1
    messages = provider.json_calls[0]
    assert [message.role for message in messages] == ["system", "user"]
    assert "OUTPUT PROTOCOL" in messages[0].content
    assert json.loads(messages[1].content)["request"] == "继续"


@pytest.mark.parametrize("capability", [False, None, 1, "true"])
def test_native_json_requires_literal_true_capability(capability: Any) -> None:
    provider = NativeRecordingProvider(envelope())
    provider.supports_json_output = capability
    result = ResponseComposer(provider).compose(request="继续")
    assert result == ResponseDraft("收到。")
    assert len(provider.calls) == 1 and provider.calls[0][1] == ()
    assert provider.json_calls == []


def test_plain_mode_keeps_chat_even_with_native_json_capability() -> None:
    provider = NativeRecordingProvider("普通正文。")
    result = ResponseComposer(provider).compose(request="继续", structured=False)
    assert result == ResponseDraft("普通正文。")
    assert len(provider.calls) == 1 and provider.json_calls == []
    assert "OUTPUT PROTOCOL" not in provider.calls[0][0][0].content


@pytest.mark.parametrize("method_present", [False, True])
def test_broken_native_capability_is_rejected_without_fallback(method_present: bool) -> None:
    provider = RecordingProvider(envelope())
    setattr(provider, "supports_json_output", True)
    if method_present:
        setattr(provider, "chat_json", "not callable")
    result = ResponseComposer(provider).compose(request="继续")
    assert result.reply_text == "" and provider.calls == []
    assert result.degraded_reason == "response_input_invalid"
    assert result.diagnostics == (ResponseDiagnostic("input", "", "json_capability_invalid"),)


def test_incompatible_native_method_signature_never_falls_back_to_chat() -> None:
    class IncompatibleProvider(RecordingProvider):
        supports_json_output = True

        def chat_json(self) -> LLMResponse:
            raise AssertionError("an incompatible method must not run")

    provider = IncompatibleProvider(envelope())
    with pytest.raises(TypeError):
        ResponseComposer(provider).compose(request="继续")
    assert provider.calls == []


@pytest.mark.parametrize("tools", [(), (ToolCall("forbidden", {}),)])
def test_native_json_keeps_strict_parser_and_tool_rejection_without_retry(tools: tuple[ToolCall, ...]) -> None:
    provider = NativeRecordingProvider("ordinary non-JSON body", tool_calls=tools)
    result = ResponseComposer(provider).compose(request="继续")
    assert result.reply_text == "" and result.degraded_reason == "response_invalid_envelope"
    assert result.diagnostics[0].code == ("tools_forbidden" if tools else "json_invalid")
    assert len(provider.json_calls) == 1 and provider.calls == []


def test_native_provider_failure_propagates_after_one_attempt_without_fallback() -> None:
    class FailingProvider(NativeRecordingProvider):
        def chat_json(self, messages: Sequence[ChatMessage]) -> LLMResponse:
            self.json_calls.append(tuple(messages))
            raise TimeoutError("synthetic native attempt outcome unknown")

    provider = FailingProvider(envelope())
    with pytest.raises(TimeoutError, match="outcome unknown"):
        ResponseComposer(provider).compose(request="继续")
    assert len(provider.json_calls) == 1 and provider.calls == []


def test_tool_supplement_schema_and_examples_follow_only_host_role() -> None:
    direct = response_schema()
    supplement = response_schema(reply_role="tool_supplement")
    assert direct["properties"]["reply_text"]["minLength"] == 1
    assert supplement["properties"]["reply_text"]["minLength"] == 0
    assert supplement["required"] == direct["required"]
    assert "reply_role" not in supplement["properties"]
    assert "expression_policy.verbosity is detailed" in supplement["properties"]["reply_text"]["description"]
    instruction = protocol_instruction(reply_role="tool_supplement")
    assert "authoritative summary is already displayed" in instruction
    assert "reply_role=tool_supplement" in instruction
    examples = [line.split(": ", 1)[1] for line in instruction.splitlines()
                if line.startswith(("Minimal valid example", "For CURRENT request"))]
    for example in examples:
        parsed = parse_envelope(example, request="我很累", allow_empty_reply=True)
        assert parsed.reply_text == "" and parsed.degraded_reason is None
        with pytest.raises(ValueError, match="response_reply_invalid"):
            parse_envelope(example, request="我很累")
    assert parse_envelope(examples[1], request="我很累", allow_empty_reply=True).cues
    supplement["properties"]["reply_text"]["minLength"] = 99
    assert response_schema(reply_role="tool_supplement")["properties"]["reply_text"]["minLength"] == 0


@pytest.mark.parametrize("structured", [True, False])
def test_composer_accepts_empty_tool_supplement_but_rejects_empty_direct_answer(structured: bool) -> None:
    body = envelope("") if structured else ""
    facts = [{"success": None, "tool_name": "synthetic", "call_id": "original-id",
              "output": {"full": "unmodified content"}, "error": "unknown outcome"}]
    tool_provider = RecordingProvider(body)
    supplement = ResponseComposer(tool_provider).compose(request="说明结果", tool_results=facts, structured=structured)
    if structured:
        assert supplement == ResponseDraft("")
    else:
        assert supplement.degraded_reason == "response_invalid_envelope"
        assert supplement.diagnostics[0].code == "reply_invalid"
        system = tool_provider.calls[0][0][0].content
        assert "Plain text mode cannot abstain with an empty reply" in system
        assert "return an empty string" not in system
    payload = json.loads(tool_provider.calls[0][0][1].content)
    assert payload["reply_role"] == "tool_supplement" and payload["tool_results"] == facts
    direct_provider = RecordingProvider(body)
    direct = ResponseComposer(direct_provider).compose(request="直接回答", structured=structured)
    assert direct.degraded_reason == "response_invalid_envelope"
    assert direct.diagnostics[0].code == "reply_invalid"
    assert json.loads(direct_provider.calls[0][0][1].content)["reply_role"] == "direct_answer"
    assert len(tool_provider.calls) == len(direct_provider.calls) == 1


def test_empty_supplement_still_validates_signals_and_cannot_self_select_role() -> None:
    provider = NativeRecordingProvider(envelope("", cues=[cue()]))
    result = ResponseComposer(provider).compose(request="我很累", tool_results=[{"success": True, "output": "done"}])
    assert result.reply_text == "" and result.degraded_reason is None and len(result.cues) == 1
    assert len(provider.json_calls) == 1 and provider.calls == []
    for allow_empty in (False, True):
        raw = json.loads(envelope(""))
        raw["reply_role"] = "tool_supplement"
        with pytest.raises(ValueError, match="response_envelope_invalid"):
            parse_envelope(json.dumps(raw), request="继续", allow_empty_reply=allow_empty)
    with pytest.raises(ValueError, match="response_reply_invalid"):
        parse_envelope(envelope(""), request="继续", allow_empty_reply=1)
    with pytest.raises(ValueError, match="response_reply_invalid"):
        parse_envelope(envelope(" \n"), request="继续", allow_empty_reply=True)
    invalid = parse_envelope(envelope("", cues=[cue(0, 99)]), request="我很累", allow_empty_reply=True)
    assert invalid.reply_text == "" and invalid.cues == () and invalid.degraded_reason == "response_signals_invalid"
