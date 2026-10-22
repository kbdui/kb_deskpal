"""Finite style vocabulary. No user text is returned as an instruction."""

from __future__ import annotations

import json
from typing import Any

STYLE_KEYS = (
    "formality", "warmth", "humor", "verbosity", "expressiveness", "curiosity"
)
PRESETS: dict[str, dict[str, float]] = {
    "balanced": dict(zip(STYLE_KEYS, (0.5, 0.5, 0.2, 0.5, 0.4, 0.3))),
    "warm": dict(zip(STYLE_KEYS, (0.3, 0.8, 0.3, 0.6, 0.6, 0.4))),
    "concise": dict(zip(STYLE_KEYS, (0.5, 0.4, 0.0, 0.1, 0.2, 0.1))),
}


def _verbosity(value: float) -> str:
    return "brief" if value < 0.3 else "detailed" if value > 0.7 else "normal"


def baseline(style: dict[str, float]) -> tuple[float, float, float]:
    """Mild persona baseline; no user or task evaluation."""
    return (0.04 * style["warmth"], 0.12 + 0.12 * style["expressiveness"], 0.0)


def expression(valence: float, mode: str) -> str:
    if mode == "focused":
        return "focused"
    if mode == "supportive":
        return "neutral"
    if valence >= 0.12:
        return "pleased"
    if valence <= -0.12:
        return "focused"
    return "neutral"


def instruction(
    request: str,
    persona: dict[str, Any],
    *,
    cloud: bool,
    preferences: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    mode: str = "neutral",
    mode_source: str = "cue",
    pleasant: bool = False,
    cloud_permissions: dict[str, bool] | None = None,
    identity: dict[str, Any] | None = None,
    use_policy: str = "cloud_allowed",
) -> str:
    # Filter whole source categories before deriving any labels or inspecting
    # current text. In particular a local-only cue cannot leak as "no humor".
    permissions = cloud_permissions or {}
    permitted_source = not cloud or use_policy == "cloud_allowed"
    custom = not cloud or permitted_source and permissions.get("custom_persona", False)
    pref_allowed = not cloud or permitted_source and permissions.get("preferences", False)
    style = persona["style"] if custom else PRESETS[persona["preset"]]
    preferences = dict(preferences or {}) if pref_allowed else {}
    constraints = dict(constraints or {}) if pref_allowed else {}
    if cloud and not permissions.get("affect" if mode_source == "affect" else "cue", False):
        mode = "neutral"
    if cloud and not permissions.get("affect", False):
        pleasant = False
    if not permitted_source:
        request, mode, pleasant = "", "neutral", False
    if pref_allowed:
        # Deferred import avoids making the signal DTO depend on this policy.
        from .signals import ConstraintParser
        for item in ConstraintParser().parse(request):
            if constraints.get(item.key) is not False:
                constraints[item.key] = item.value
    merged = {**preferences, **constraints}
    verbosity = merged.get("verbosity", _verbosity(style["verbosity"]))
    if verbosity not in ("brief", "normal", "detailed"):
        verbosity = _verbosity(style["verbosity"])
    warm = style["warmth"] >= 0.65 or mode in ("supportive", "celebratory") or pleasant
    formal = style["formality"] >= 0.65
    humor = merged.get("humor_allowed", True) is not False and style["humor"] >= 0.25
    if constraints.get("humor_allowed") is True:
        humor = True
    vivid_allowed = style["expressiveness"] >= 0.55
    explore_allowed = style["curiosity"] >= 0.65
    if custom and identity is not None:
        explore_allowed = identity.get("followup_style") == "optional_one"
    if merged.get("followup_allowed") is False:
        explore_allowed = False
    if mode in ("supportive", "focused"):
        if merged.get("humor_allowed") is not True:
            humor = False
        if merged.get("followup_allowed") is not True:
            explore_allowed = False
        vivid_allowed = False
    tone = "温和" if warm else "平实"
    if formal:
        tone += "、正式"
    length = {"brief": "简洁", "normal": "适中", "detailed": "充分解释"}[verbosity]
    joke = "可少量轻松表达" if humor else "避免玩笑"
    vivid = "措辞可适度生动" if vivid_allowed else "措辞克制、少用表情"
    curiosity = (
        "完成回答后至多附一个可略过、不涉及敏感信息的探索邀请，不阻断任务"
        if explore_allowed else "不附加额外追问，不阻断任务"
    )
    # This describes phrasing only; no authority, action or tool vocabulary.
    result = f"表达风格：语气{tone}；篇幅{length}；{joke}；{vivid}；{curiosity}。当前用户明确的表达要求优先。"
    if mode in ("supportive", "focused", "celebratory"):
        result += " 当前短期表达模式：" + {"supportive": "支持", "focused": "专注", "celebratory": "轻度庆祝"}[mode] + "，不代表长期用户特征。"
    if custom and identity is not None:
        # Composer places this typed data in its user-data envelope, never in
        # the system instruction. No arbitrary role-card field exists.
        result += "\n[PERSONA_DATA]" + json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
    return result
