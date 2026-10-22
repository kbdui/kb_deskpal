"""Small explicit command grammar and untrusted model-signal validation.

Span validation establishes provenance, not semantic truth. These validators do
not diagnose feelings; uncertain semantic interpretation belongs to Composer.
"""

from __future__ import annotations

import re
from typing import Any

from .models import (PROFILE_PREFERENCE_KEYS, CueSignal, InteractionConstraint,
                     PreferenceSignal, normalize_profile_value, preference_valid)

_SCOPE = r"(?:(这一轮|这次|本轮|现在|本次对话|这个会话|本会话|今天|今日|for this reply|this session|today)[，,：:\s]*(?:都)?)?"
_PHRASES: tuple[tuple[str, str, str | bool], ...] = (
    (r"(?:请)?(?:回答|回复)?(?:简短(?:一点|一些|些)?|简洁(?:一点|一些|些)?|精简(?:一点|一些|些)?)(?:回答|回复)?|(?:please )?(?:be brief|keep (?:it|replies) brief|answer briefly)", "verbosity", "brief"),
    (r"(?:请)?(?:回答|回复)?(?:详细(?:一点|一些|些)?|展开说明)(?:回答|回复)?|(?:please )?(?:answer in detail|be detailed)", "verbosity", "detailed"),
    (r"(?:请)?(?:不要|别)(?:开玩笑|说笑|幽默)|(?:please )?(?:no jokes|no humor|do not joke|don't joke)", "humor_allowed", False),
    (r"(?:请)?(?:可以|允许)(?:开玩笑|幽默)|(?:you may|please) use humor", "humor_allowed", True),
    (r"(?:请)?(?:不要|别)(?:追问|反问)|(?:please )?(?:no follow[ -]up(?: questions)?|do not ask follow[ -]up questions)", "followup_allowed", False),
)


class ConstraintParser:
    """Only complete expression clauses; never persists a long-term preference."""

    def parse(self, request: str) -> tuple[InteractionConstraint, ...]:
        if not isinstance(request, str) or len(request.encode("utf-8")) > 65536:
            return ()
        # Quoted/attributed examples are intentionally outside this grammar.
        if any(mark in request for mark in ('"', "“", "”", "「", "」", "『", "』")):
            return ()
        found: list[InteractionConstraint] = []
        for clause in re.finditer(r"[^。！？!?；;\n]+", request):
            text = clause.group().strip().rstrip(".，, ")
            for phrase, key, value in _PHRASES:
                match = re.fullmatch(_SCOPE + "(?:" + phrase + ")", text, re.IGNORECASE)
                if match is None:
                    continue
                scope_word = (match.group(1) or "").casefold()
                scope = "today" if scope_word in ("今天", "今日", "today") else "session" if scope_word in ("本次对话", "这个会话", "本会话", "this session") else "once"
                offset = clause.start() + len(clause.group()) - len(clause.group().lstrip())
                found.append(InteractionConstraint(key, value, scope, (offset, offset + len(text))))
                break
        # Prohibitions win within one request. Order otherwise remains explicit.
        merged: dict[tuple[str, str], InteractionConstraint] = {}
        for item in found:
            old = merged.get((item.scope, item.key))
            if old is None or old.value is not False:
                merged[item.scope, item.key] = item
        return tuple(merged.values())[:8]

    @staticmethod
    def clears_cue(request: str) -> bool:
        """A few explicit correction commands, not a feeling classifier."""
        text = request.strip().rstrip("。.!！")
        return bool(re.fullmatch(r"(?:请)?(?:清除|撤销)(?:刚才的)?(?:情绪理解|情绪线索|感受判断)|(?:你)?理解错了|不是我[，,]\s*是我朋友[^。！？!?\n]{0,120}|(?:please )?clear (?:the )?(?:cue|feeling interpretation)", text, re.IGNORECASE))


def _span(value: Any, message: str) -> tuple[int, int] | None:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    start, end = value
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(message) or end - start > 256:
        return None
    if not message[start:end].strip():
        return None
    return start, end


def validate_cue(value: Any, message: str) -> CueSignal | None:
    if isinstance(value, CueSignal):
        value = {"mode": value.mode, "evidence_kind": value.evidence_kind, "span": value.span}
    if not isinstance(value, dict) or set(value) != {"mode", "evidence_kind", "span"}:
        return None
    mode, kind = value["mode"], value["evidence_kind"]
    if mode not in ("none", "supportive", "focused", "celebratory") or kind not in ("explicit_report", "tentative"):
        return None
    span = _span(value["span"], message)
    if span is None:
        return None
    if mode == "none":
        return CueSignal(mode, kind, span)
    evidence = message[span[0]:span[1]].casefold()
    # Conservative attribution guard, not an affect keyword classifier.
    if any(mark in message for mark in ('"', "“", "”", "「", "」", "『", "』")):
        return None
    # Generic negation is not a feeling classifier: e.g. “我不开心” and
    # “I don't feel well” are genuine negative self-reports. Semantic polarity
    # belongs to the generation call, not a substring veto on 不/not.
    if re.search(r"朋友|他说|她说|小说|角色|不是我|my friend|he said|she said", evidence):
        return None
    if kind == "explicit_report" and not re.search(r"我|\bi(?: am|'m| feel)\b", evidence):
        return None
    return CueSignal(mode, kind, span)


def validate_preference(value: Any, message: str) -> PreferenceSignal | None:
    if isinstance(value, PreferenceSignal):
        value = {"key": value.key, "value": value.value, "scope": value.scope, "span": value.span}
    if not isinstance(value, dict) or set(value) != {"key", "value", "scope", "span"}:
        return None
    normalized: str | bool
    if value["key"] in PROFILE_PREFERENCE_KEYS:
        if value["scope"] != "persistent":
            return None
        profile_value = normalize_profile_value(value["key"], value["value"])
        if profile_value is None:
            return None
        normalized = profile_value
    else:
        if not preference_valid(value["key"], value["value"]) or value["scope"] not in ("session", "today", "persistent"):
            return None
        normalized = value["value"]
    span = _span(value["span"], message)
    if span is None:
        return None
    return PreferenceSignal(value["key"], normalized, value["scope"], span)
