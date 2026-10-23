"""HM08 host scope/discourse guards; deterministic synthetic evidence only."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deskpal.companion.interaction import InteractionState
from deskpal.companion.signals import ConstraintParser, review_preference, validate_cue, validate_preference

from test_v08_companion_runtime import Clock, runtime_at


def preference(message: str, key: str = "verbosity", value: str | bool = "brief",
               scope: str = "persistent", evidence: str | None = None) -> dict[str, Any]:
    text = message if evidence is None else evidence
    start = message.index(text)
    return {"key": key, "value": value, "scope": scope, "span": [start, start + len(text)]}


def cue(message: str, evidence: str | None = None, kind: str = "explicit_report",
        mode: str = "focused") -> dict[str, Any]:
    text = message if evidence is None else evidence
    start = message.index(text)
    return {"mode": mode, "evidence_kind": kind, "span": [start, start + len(text)]}


@pytest.mark.parametrize("message", [
    "这一轮简短些。不要开玩笑。不要追问。",
    "请先解释结果，这一轮回答简短一点，然后我再决定下一步。",
    "For this reply, please keep your answer brief; explain the result first.",
])
@pytest.mark.parametrize("scope", ["session", "today", "persistent"])
def test_once_commands_cannot_become_saved_candidates(message: str, scope: str) -> None:
    raw = preference(message, scope=scope)
    assert validate_preference(raw, message) is None
    assert review_preference(raw, message).reason == "once_only"
    assert any(item.key == "verbosity" and item.scope == "once" for item in ConstraintParser().parse(message))


@pytest.mark.parametrize(("message", "key", "value", "scope", "expected"), [
    ("今天请不要开玩笑，我还要整理材料。", "humor_allowed", False, "persistent", "scope_conflict"),
    ("本次对话回复简短一点，再讲清楚原因。", "verbosity", "brief", "today", "scope_conflict"),
    ("This session, please be brief.", "verbosity", "detailed", "session", "value_conflict"),
    ("今天不要开玩笑", "humor_allowed", True, "today", "value_conflict"),
    ("以后请不要追问", "followup_allowed", True, "persistent", "value_conflict"),
])
def test_scope_and_value_conflicts_have_text_free_reasons(
    message: str, key: str, value: str | bool, scope: str, expected: str
) -> None:
    result = review_preference(preference(message, key, value, scope), message)
    assert result.signal is None
    assert result.reason == expected
    assert message not in result.reason


@pytest.mark.parametrize(("message", "key", "value", "scope", "evidence"), [
    ("This session, please be brief.", "verbosity", "brief", "session", "please be brief"),
    ("今天，请不要开玩笑", "humor_allowed", False, "today", "请不要开玩笑"),
])
def test_scope_only_comma_prefix_accepts_adjacent_command(
    message: str, key: str, value: str | bool, scope: str, evidence: str
) -> None:
    candidate = validate_preference(preference(message, key, value, scope, evidence), message)
    assert candidate is not None and candidate.scope == scope
    assert [(item.key, item.value, item.scope) for item in ConstraintParser().parse(message)] == [(key, value, scope)]


@pytest.mark.parametrize("separator", ["，。", ",.", "，；", "，\n"])
def test_scope_only_comma_prefix_cannot_cross_sentence_or_line(separator: str) -> None:
    message = "今天" + separator + "不要开玩笑"
    result = review_preference(preference(message, "humor_allowed", False, "today", "不要开玩笑"), message)
    assert result.signal is None and result.reason == "once_only"
    assert [(item.key, item.scope) for item in ConstraintParser().parse(message)] == [("humor_allowed", "once")]


def test_scope_only_comma_prefix_stops_after_one_complete_command() -> None:
    message = "今天，不要开玩笑，简短些"
    assert validate_preference(preference(message, "humor_allowed", False, "today", "不要开玩笑"), message) is not None
    assert review_preference(preference(message, scope="today", evidence="简短些"), message).reason == "once_only"
    assert {(item.key, item.scope) for item in ConstraintParser().parse(message)} == {
        ("humor_allowed", "today"), ("verbosity", "once")}


@pytest.mark.parametrize(("message", "reason"), [
    ('"今天"，请不要开玩笑', "once_only"),
    ("我的朋友说今天，请不要开玩笑", "untrusted_evidence"),
    ("My friend said today, please no jokes.", "untrusted_evidence"),
])
def test_scope_only_comma_prefix_cannot_borrow_quoted_or_third_person_scope(message: str, reason: str) -> None:
    evidence = "请不要开玩笑" if "请不要开玩笑" in message else "please no jokes"
    result = review_preference(preference(message, "humor_allowed", False, "today", evidence), message)
    assert result.signal is None and result.reason == reason


@pytest.mark.parametrize(("message", "key", "value", "evidence"), [
    ("From now on, reply in English.", "reply_language", "en", "reply in English"),
    ("以后，请叫我小星", "preferred_name", "小星", "请叫我小星"),
    ("以后，答复尽量言简意赅", "verbosity", "brief", "答复尽量言简意赅"),
])
def test_scope_only_comma_prefix_supports_profile_and_model_paraphrase(
    message: str, key: str, value: str, evidence: str
) -> None:
    candidate = validate_preference(preference(message, key, value, "persistent", evidence), message)
    assert candidate is not None and candidate.scope == "persistent"


def test_narrow_quote_cannot_drop_its_host_once_scope() -> None:
    message = "结果先解释一下；这一轮请回答简短些，之后我再考虑。"
    raw = preference(message, scope="session", evidence="回答简短些")
    assert review_preference(raw, message).reason == "once_only"


def test_independent_keys_and_scopes_do_not_pollute_each_other() -> None:
    message = "这一轮简短些。以后不要开玩笑。"
    assert review_preference(preference(message, evidence="这一轮简短些"), message).reason == "once_only"
    accepted = validate_preference(preference(message, "humor_allowed", False,
                                             evidence="以后不要开玩笑"), message)
    assert accepted is not None and accepted.scope == "persistent"


def test_same_key_multiple_scopes_bind_to_their_evidence() -> None:
    message = "这一轮简短些；本会话详细回答。"
    assert review_preference(preference(message, scope="session"), message).reason == "ambiguous_scope"
    accepted = validate_preference(preference(message, value="detailed", scope="session",
                                             evidence="详细回答"), message)
    assert accepted is not None and accepted.value == "detailed"
    assert review_preference(preference(message, scope="session", evidence="简短些"), message).reason == "once_only"


def test_unknown_paraphrase_keeps_model_candidate_only_with_local_scope() -> None:
    # This is deliberately outside the finite host expression vocabulary.
    message = "以后答复尽量言简意赅"
    accepted = validate_preference(preference(message), message)
    assert accepted is not None and accepted.scope == "persistent"
    assert ConstraintParser().parse(message) == ()
    message = "答复尽量言简意赅"
    assert review_preference(preference(message), message).reason == "ambiguous_scope"
    message = "这一轮答复尽量言简意赅，以后不要开玩笑"
    assert review_preference(preference(message, evidence="答复尽量言简意赅"), message).reason == "once_only"


@pytest.mark.parametrize("message", [
    "我的朋友说，以后不要开玩笑", "He said, from now on no jokes.",
    "假设以后不要开玩笑", "小说角色说：以后不要开玩笑", '他说"以后不要开玩笑"',
])
def test_third_person_quotation_and_condition_do_not_create_preferences(message: str) -> None:
    raw = preference(message, "humor_allowed", False,
                     evidence="以后不要开玩笑" if "以后不要开玩笑" in message else "from now on no jokes")
    assert review_preference(raw, message).reason == "untrusted_evidence"
    assert ConstraintParser().parse(message) == ()


def test_own_independent_request_after_third_person_sentence_is_valid() -> None:
    message = '我的朋友说："不要开玩笑"。今天请不要开玩笑。'
    accepted = validate_preference(preference(message, "humor_allowed", False, "today",
                                             evidence="今天请不要开玩笑"), message)
    assert accepted is not None
    assert [(item.scope, item.key) for item in ConstraintParser().parse(message)] == [("today", "humor_allowed")]


def test_profile_language_and_address_keep_independent_lasting_scope() -> None:
    message = "这一轮简短些。以后用英文回答。请叫我小星。"
    language = validate_preference(preference(message, "reply_language", "en",
                                              evidence="以后用英文回答"), message)
    name = validate_preference(preference(message, "preferred_name", "小星", evidence="请叫我小星"), message)
    assert language is not None and name is not None
    message = "这一轮请用英文回答。以后不要开玩笑"
    assert review_preference(preference(message, "reply_language", "en", evidence="这一轮请用英文回答"), message).reason == "scope_conflict"
    message = "以后用英文回答这一轮简短些"
    assert validate_preference(preference(message, "reply_language", "en", evidence="以后用英文回答"), message) is not None
    message = "请叫我 E\u0301，以后用英语回复"
    name = validate_preference(preference(message, "preferred_name", "É", evidence="请叫我 E\u0301"), message)
    assert name is not None and name.value == "É"


@pytest.mark.parametrize("message", [
    "请给我一个今天能做完的小步骤，不要说教。",
    "现在我想专心写通知，请直接给一个简短开头。",
    "我希望你帮我整理清单", "I want you to write a short notice.",
    "Please help me organize the list.", "活动通知通常应该包含哪些信息？",
])
@pytest.mark.parametrize("kind", ["explicit_report", "tentative"])
def test_task_requests_are_not_user_state_cues(message: str, kind: str) -> None:
    assert validate_cue(cue(message, kind=kind), message) is None


@pytest.mark.parametrize("message", [
    "我的朋友说，我今天很累", "He said, I feel tired.",
    '小说角色说："我很累"', "我并不难过", "I am not saying I feel sad.", "I didn't say I feel sad.",
    "如果我很累", "Imagine I feel tired.",
])
@pytest.mark.parametrize("kind", ["explicit_report", "tentative"])
def test_cue_abstention_is_shared_by_both_evidence_kinds(message: str, kind: str) -> None:
    evidence = "我今天很累" if message.startswith("我的朋友") else "I feel tired" if message.startswith("He said") else None
    assert validate_cue(cue(message, evidence, kind), message) is None


@pytest.mark.parametrize("message", ["我今天很累", "我觉得事情太多", "我没有精神", "我不开心", "I don't feel well.", "I feel overwhelmed.", "I am concentrating."])
def test_declarative_self_reports_do_not_require_affect_keyword_classification(message: str) -> None:
    assert validate_cue(cue(message), message) is not None


@pytest.mark.parametrize(("field", "bad"), [("key", []), ("key", {}), ("scope", []), ("scope", {}), ("value", []), ("value", {}), ("span", {})])
def test_malformed_direct_preference_inputs_are_rejected_without_type_errors(field: str, bad: Any) -> None:
    message = "以后不要开玩笑"
    raw = preference(message, "humor_allowed", False)
    raw[field] = bad
    result = review_preference(raw, message)
    assert result.signal is None and result.reason in ("invalid_shape", "invalid_value", "invalid_span")


def test_correction_wins_over_model_cue_and_invalidates_old_candidates() -> None:
    state = InteractionState()
    message = "以后不要开玩笑"
    state.prepare(message, "old", "a", "local_only", 0)
    state.accept("old", {"preference_candidates": [preference(message, "humor_allowed", False)]}, "a", "local_only", 1)
    assert len(state.candidates) == 1
    correction = "你理解错了，请帮我写通知。我今天很累。"
    state.prepare(correction, "new", "a", "local_only", 2)
    assert not state.candidates
    assert validate_cue(cue(correction, "我今天很累"), correction) is None
    state.accept("new", {"cues": [cue(correction, "我今天很累")]}, "a", "local_only", 3)
    assert not state.cues


def test_accept_rechecks_forged_once_candidate_and_clear_discards_old_evidence() -> None:
    state = InteractionState()
    once = "这一轮简短些。不要开玩笑。不要追问。"
    state.prepare(once, "once", "a", "cloud_allowed", 0)
    state.accept("once", {"preference_candidates": [preference(once, scope="session")]}, "a", "cloud_allowed", 1)
    assert not state.candidates
    message = "以后不要开玩笑"
    state.prepare(message, "private", "a", "local_only", 2)
    state.clear()
    state.accept("private", {"preference_candidates": [preference(message, "humor_allowed", False)]}, "a", "local_only", 3)
    assert not state.candidates and not state.cues and not state.prepared


@pytest.mark.parametrize("expression_request", ["这一轮简短些，本会话详细回答", "本会话详细回答，这一轮简短些"])
def test_runtime_once_wins_in_both_orders_then_session_remains(tmp_path: Path, expression_request: str) -> None:
    with runtime_at(tmp_path / "scope.sqlite3", Clock()) as runtime:
        prepared = runtime.prepare_interaction(expression_request, task_id="mixed", conversation_id="a")
        assert prepared["constraints"] == {"verbosity": "brief"}
        assert "篇幅简洁" in runtime.style_instruction(expression_request, cloud=False, conversation_id="a", constraints=prepared["constraints"])
        runtime.prepare_interaction("继续", task_id="next", conversation_id="a")
        assert "篇幅充分解释" in runtime.style_instruction("继续", cloud=False, conversation_id="a")
        assert "篇幅简洁" in runtime.style_instruction("这轮详细回答", cloud=False, conversation_id="a", constraints={"verbosity": "brief"})
