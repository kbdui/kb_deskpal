"""Finite host evidence checks shared by generation and publication.

Supporting spans prove provenance, never arbitrary natural-language semantics.
Unknown or ambiguous saving scope is deliberately left without a candidate.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
import unicodedata

from .models import (PROFILE_PREFERENCE_KEYS, CueSignal, InteractionConstraint,
                     PreferenceSignal, normalize_profile_value, preference_valid)

_SCOPE_WORDS: tuple[tuple[str, str], ...] = (
    (r"这一轮|这轮|本轮|这次|本次回答|这次回答|现在|for this (?:reply|response|turn)|this time|this reply|just this once", "once"),
    (r"本次对话|这个会话|本会话|本轮对话|这次对话|(?:for |during )?this (?:session|conversation)", "session"),
    (r"今天|今日|today", "today"),
    (r"以后|今后|往后|长期|默认|一直|总是|我(?:更)?(?:喜欢|习惯|偏好)|from now on|going forward|in future|always|\bi prefer\b", "persistent"),
)
_SCOPE_RE = re.compile("|".join(f"(?P<s{i}>{_SCOPE_WORDS[i][0]})" for i in (1, 0, 2, 3)), re.IGNORECASE)
_PHRASES: tuple[tuple[str, str, str | bool], ...] = (
    (r"(?:请)?(?:回答|回复)?(?:简短(?:一点|一些|些|点)?|简洁(?:一点|一些|些|点)?|精简(?:一点|一些|些|点)?)(?:回答|回复)?|(?:please )?(?:be brief|keep (?:it|replies|your (?:reply|replies|answer)) brief|answer briefly)", "verbosity", "brief"),
    (r"(?:请)?(?:回答|回复)?(?:详细(?:一点|一些|些|点)?|展开说明)(?:回答|回复)?|(?:please )?(?:answer in detail|be detailed)", "verbosity", "detailed"),
    (r"(?:请)?(?:不要|别)(?:再|总是|一直)?(?:开玩笑|说笑|幽默)|(?:please )?(?:no jokes|no humor|do not joke|don't joke)", "humor_allowed", False),
    (r"(?:请)?(?:可以|允许)(?:开玩笑|幽默)|(?:you may|please) use humor", "humor_allowed", True),
    (r"(?:请)?(?:不要|别|少)(?:再)?(?:追问|反问)|(?:请)?(?:不要|别)问我问题|(?:please )?(?:no follow[ -]up(?: questions)?|do not ask follow[ -]up questions)", "followup_allowed", False),
)
_ATTRIBUTION = re.compile(r"朋友|他说|她说|他们说|小说|角色|台词|转述|引用|不是我|\b(?:my friend|he said|she said|they said|quotation|fictional|a character)\b", re.IGNORECASE)
_HYPOTHETICAL = re.compile(r"假如|假设|如果|是否|是不是|\b(?:if|suppose|imagine|whether)\b", re.IGNORECASE)
_QUOTE = re.compile(r'"[^"\n]*(?:"|$)|“[^”\n]*(?:”|$)|「[^」\n]*(?:」|$)|『[^』\n]*(?:』|$)|`[^`\n]*(?:`|$)|(?<!\w)\'[^\'\n]+\'(?!\w)')
_CLAUSE = re.compile(r"[^。！？!?；;.\n，,]+")
_CORRECTION = re.compile(
    r"(?:请)?(?:清除|撤销)(?:刚才的)?(?:情绪理解|情绪线索|感受判断)|(?:你)?理解错了|"
    r"(?:please )?clear (?:the )?(?:cue|feeling interpretation)|\byou (?:misunderstood|got (?:it|me) wrong)\b", re.IGNORECASE)


@dataclass(frozen=True)
class PreferenceReview:
    """Public, text-free rejection reason; validate_preference stays compatible."""

    signal: PreferenceSignal | None
    reason: str | None = None


def _overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _clauses(message: str) -> tuple[tuple[str, tuple[int, int]], ...]:
    # Mask quoted material while preserving Unicode offsets. Independent
    # commands after quotations retain their own local evidence.
    masked = list(message)
    for match in _QUOTE.finditer(message):
        masked[match.start():match.end()] = " " * (match.end() - match.start())
    plain = "".join(masked)
    clauses: list[tuple[str, tuple[int, int]]] = []
    pending: tuple[int, int] | None = None
    for match in _CLAUSE.finditer(plain):
        location = match.span()
        if pending is not None:
            # finditer skips sentence punctuation, so pending alone does not
            # establish adjacency ("今天，。不要开玩笑" must not inherit).
            if re.fullmatch(r"[,，][ \t]*", plain[pending[1]:match.start()]) is not None:
                location = (pending[0], match.end())
            else:
                clauses.append((plain[pending[0]:pending[1]], pending))
        text = plain[location[0]:location[1]]
        pending = None
        # A scope-only prefix separated by a comma is still local evidence:
        # "For this session, please be brief." Do not cross sentence breaks.
        if _SCOPE_RE.fullmatch(text.strip()) is not None and plain[match.end():match.end() + 1] in (",", "，"):
            pending = location
            continue
        clauses.append((text, location))
    return tuple(clauses)


def _scope_at(text: str, start: int, end: int) -> str | None:
    scopes = list(_SCOPE_RE.finditer(text))
    preceding = [match for match in scopes if match.start() <= start]
    # Bind the closest preceding marker; consider a suffix only without one.
    marker = preceding[-1] if preceding else next((match for match in scopes if match.start() >= end), None)
    if marker is None or marker.lastgroup is None:
        return None
    return _SCOPE_WORDS[int(marker.lastgroup[1:])][1]


def _unsafe(text: str) -> bool:
    return bool(_ATTRIBUTION.search(text) or _HYPOTHETICAL.search(text))


def _expression_evidence(message: str) -> tuple[InteractionConstraint, ...]:
    found: list[InteractionConstraint] = []
    for text, location in _clauses(message):
        if _unsafe(_sentence_context(message, location)):
            continue
        for phrase, key, value in _PHRASES:
            for match in re.finditer(phrase, text, re.IGNORECASE):
                # Bounded mention/negation guard, not a polarity classifier.
                if re.search(r"(?:不要|不是|并不|不用|无需|没让|不想|不喜欢)(?:你|再|总是|这样|这么|太)?\s*$|\b(?:not|never|don't|do not)\s*$", text[:match.start()], re.IGNORECASE):
                    continue
                if key == "verbosity" and re.search(r"(?:简短|简洁|精简|详细)$", match.group()) and re.match(r"[\u4e00-\u9fff]", text[match.end():]):
                    # A modifier inside a requested artifact ("详细计划")
                    # does not establish a lasting answer-style preference.
                    continue
                scope = _scope_at(text, match.start(), match.end()) or "once"
                found.append(InteractionConstraint(key, value, scope,
                    (location[0] + match.start(), location[0] + match.end())))
    return tuple(sorted(found, key=lambda item: item.span))


class ConstraintParser:
    """Explicit finite expression commands; never persists long-term choices."""

    def parse(self, request: str) -> tuple[InteractionConstraint, ...]:
        if not isinstance(request, str) or len(request.encode("utf-8")) > 65536:
            return ()
        merged: dict[tuple[str, str], InteractionConstraint] = {}
        for item in _expression_evidence(request):
            if item.scope == "persistent":
                continue
            old = merged.get((item.scope, item.key))
            if old is None or old.value is not False:
                merged[item.scope, item.key] = item
        return tuple(merged.values())[:8]

    @staticmethod
    def clears_cue(request: str) -> bool:
        """Explicit corrections may sit beside a new task request."""
        return any(_CORRECTION.search(text) is not None for text, _ in _clauses(request)) or bool(
            re.search(r"不是我[，,]\s*是我朋友|\bnot me[, ]+my friend\b", request, re.IGNORECASE))


def _span(value: Any, message: str) -> tuple[int, int] | None:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    start, end = value
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(message) or end - start > 256:
        return None
    if not message[start:end].strip():
        return None
    return start, end


def _evidence_clauses(message: str, span: tuple[int, int]) -> tuple[tuple[str, tuple[int, int]], ...]:
    return tuple((text, location) for text, location in _clauses(message) if _overlap(location, span))


def _quoted(message: str, span: tuple[int, int]) -> bool:
    return any(_overlap(match.span(), span) for match in _QUOTE.finditer(message))


def _sentence_context(message: str, span: tuple[int, int]) -> str:
    # Attribution/condition can precede a comma and the supporting quote.
    return " ".join(match.group() for match in re.finditer(r"[^。！？!?；;.\n]+", message)
                    if _overlap(match.span(), span))


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
    if ConstraintParser.clears_cue(message) or _quoted(message, span):
        return None
    clauses = _evidence_clauses(message, span)
    if not clauses or _unsafe(_sentence_context(message, span)):
        return None
    evidence = message[span[0]:span[1]]
    # The same guard covers both kinds: tentative is no escape hatch for a
    # task, quotation, third-person report or explicit denial.
    context = " ".join(text for text, _ in clauses)
    # Explicit denial/correction discourse only. Generic grammatical negation
    # can itself report a real negative condition ("I don't feel well").
    if re.search(r"我(?:今天|现在|其实)?并不|我不是(?:在)?(?:说|指|讲)|\bi(?:\s+am|'m) not saying\b|\bi (?:didn't|did not) say\b|\bi (?:don't|do not) mean\b", context, re.IGNORECASE):
        return None
    if re.search(r"(?:请|帮我|给我|帮忙)|我(?:现在)?(?:想|希望|需要|要)|\b(?:please|help me|give me|i (?:want|need|would like))\b", context, re.IGNORECASE):
        return None
    if not re.search(r"我(?:今天|现在|最近|目前|刚才|可能|真的|确实|已经|有些|有点|很|感到|感觉|觉得|正在|正|在|没有|不)|\bi(?:\s+am|'m|\s+feel|\s+felt|\s+have|\s+was|\s+(?:don't|do not) feel)\b", evidence, re.IGNORECASE):
        return None
    return CueSignal(mode, kind, span)


def review_preference(value: Any, message: str) -> PreferenceReview:
    if isinstance(value, PreferenceSignal):
        value = {"key": value.key, "value": value.value, "scope": value.scope, "span": value.span}
    if not isinstance(value, dict) or set(value) != {"key", "value", "scope", "span"}:
        return PreferenceReview(None, "invalid_shape")
    key, scope = value["key"], value["scope"]
    if not isinstance(key, str) or not isinstance(scope, str):
        return PreferenceReview(None, "invalid_shape")
    normalized: str | bool
    if key in PROFILE_PREFERENCE_KEYS:
        profile_value = normalize_profile_value(key, value["value"])
        if profile_value is None:
            return PreferenceReview(None, "invalid_value")
        normalized = profile_value
        if scope != "persistent":
            return PreferenceReview(None, "scope_conflict")
    else:
        if not preference_valid(key, value["value"]) or scope not in ("session", "today", "persistent"):
            return PreferenceReview(None, "invalid_value")
        normalized = value["value"]
    span = _span(value["span"], message)
    if span is None:
        return PreferenceReview(None, "invalid_span")
    clauses = _evidence_clauses(message, span)
    if _quoted(message, span) or not clauses or _unsafe(_sentence_context(message, span)):
        return PreferenceReview(None, "untrusted_evidence")
    if key in PROFILE_PREFERENCE_KEYS:
        reason = _review_profile(key, str(normalized), message, span, clauses)
        if reason is not None:
            return PreferenceReview(None, reason)
    else:
        all_evidence = [item for item in _expression_evidence(message) if item.key == key]
        bound = [item for item in all_evidence if any(_overlap(item.span, location) for _, location in clauses)]
        # Dropping a preceding scope token from the quote cannot broaden it.
        exact = [item for item in bound if _overlap(item.span, span)]
        selected = exact or bound
        if selected:
            meanings = {(item.scope, item.value) for item in selected}
            if len(meanings) > 1:
                return PreferenceReview(None, "ambiguous_scope")
            expected_scope, expected_value = next(iter(meanings))
            if normalized != expected_value:
                return PreferenceReview(None, "value_conflict")
            if expected_scope == "once":
                return PreferenceReview(None, "once_only")
            if scope != expected_scope:
                return PreferenceReview(None, "scope_conflict")
        else:
            # Unsupported paraphrases need explicit local scope evidence;
            # never borrow a scope from another independent clause/key.
            scopes = {_scope_at(text, max(0, span[0] - location[0]), min(len(text), span[1] - location[0]))
                      for text, location in clauses}
            if scopes == {"once"}:
                return PreferenceReview(None, "once_only")
            if scopes != {scope}:
                return PreferenceReview(None, "scope_conflict" if any(scopes) else "ambiguous_scope")
            # This remains an unconfirmed model interpretation, not a host
            # claim that a paraphrase's value has been semantically proved.
    return PreferenceReview(PreferenceSignal(key, normalized, scope, span))


def _review_profile(key: str, value: str, message: str, span: tuple[int, int],
                    clauses: tuple[tuple[str, tuple[int, int]], ...]) -> str | None:
    evidence = unicodedata.normalize("NFC", message[span[0]:span[1]]).casefold()
    if re.search(r"不要|别|不是|并不|\b(?:not|never|don't|do not)\b", evidence, re.IGNORECASE):
        return "untrusted_evidence"
    if key == "preferred_name":
        pattern = r"叫我|称呼我|\bcall me\b|\baddress me as\b"
        request = re.search(pattern, evidence)
        if request is None or value.casefold() not in evidence[request.end():]:
            return "untrusted_evidence"
    else:
        pattern = r"(?:用|使用)(?:英文|英语|中文|汉语)(?:回答|回复)?|(?:reply|answer|respond|speak)(?: to me)? in (?:english|chinese)"
        request = re.search(pattern, evidence)
        if request is None:
            return "untrusted_evidence"
        expected = "en" if re.search(r"英文|英语|english", request.group()) else "zh"
        if value != expected:
            return "value_conflict"
    # Address requests themselves supply a profile intent; language requests
    # require lasting scope. Explicit temporary scope restricts either field.
    scopes = {_scope_at(text, match.start(), match.end()) for text, location in clauses
              for match in re.finditer(pattern, text, re.IGNORECASE)
              if _overlap((location[0] + match.start(), location[0] + match.end()), span)}
    if scopes - {None, "persistent"}:
        return "scope_conflict"
    if key == "reply_language" and scopes != {"persistent"}:
        return "ambiguous_scope"
    return None


def validate_preference(value: Any, message: str) -> PreferenceSignal | None:
    return review_preference(value, message).signal
