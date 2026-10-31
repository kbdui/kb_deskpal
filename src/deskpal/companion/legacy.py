"""旧版角色库导入校验；不再提供预设、信号或数值情绪运行逻辑。"""

from __future__ import annotations

import math
from typing import Any

CLOUD_CATEGORIES = ("custom_persona", "preferences", "cue", "affect", "history")
IDENTITY_DEFAULTS: dict[str, Any] = {
    "display_name": "", "role_kind": "assistant", "self_reference": "neutral",
    "greeting_style": "none", "followup_style": "none",
}

def preference_valid(key: Any, value: Any) -> bool:
    """Companion-owned expression preferences only; excludes Memory profile."""
    if key == "verbosity":
        return isinstance(value, str) and value in ("brief", "normal", "detailed")
    return key in ("humor_allowed", "followup_allowed") and type(value) is bool

STYLE_KEYS = (
    "formality", "warmth", "humor", "verbosity", "expressiveness", "curiosity"
)

PRESETS: dict[str, dict[str, float]] = {
    "balanced": dict(zip(STYLE_KEYS, (0.5, 0.5, 0.2, 0.5, 0.4, 0.3))),
    "warm": dict(zip(STYLE_KEYS, (0.3, 0.8, 0.3, 0.6, 0.6, 0.4))),
    "concise": dict(zip(STYLE_KEYS, (0.5, 0.4, 0.0, 0.1, 0.2, 0.1))),
}

SCHEMA_VERSION = 1
REDUCER_VERSION = 1

def _finite(value: Any, low: float, high: float) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and low <= value <= high
    except OverflowError:
        return False







def initial_interaction() -> dict[str, Any]:
    return {"cloud_permissions": dict.fromkeys(CLOUD_CATEGORIES, False),
            "identity": dict(IDENTITY_DEFAULTS), "identity_use_policy": "local_only",
            "preference_policies": {}, "preference_sources": {}, "preferences": {}, "identity_versions": {},
            "cloud_affect": {"values": [0.02, 0.168, 0.0], "anchor_utc": 0.0,
                             "mode": "neutral", "mode_until": 0.0}, "cloud_budget": []}


def validate_identity(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != set(IDENTITY_DEFAULTS):
        return False
    name = value["display_name"]
    return (isinstance(name, str) and len(name) <= 40
            and not any(ord(char) < 32 for char in name)
            and value["role_kind"] in ("assistant", "companion")
            and value["self_reference"] in ("neutral", "name", "first_person")
            and value["greeting_style"] in ("none", "brief")
            and value["followup_style"] in ("none", "optional_one"))


def validate_interaction(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != set(initial_interaction()):
        raise ValueError("companion_interaction_invalid")
    permissions = value["cloud_permissions"]
    if not isinstance(permissions, dict) or set(permissions) != set(CLOUD_CATEGORIES) or any(type(flag) is not bool for flag in permissions.values()):
        raise ValueError("companion_interaction_invalid")
    if not validate_identity(value["identity"]) or value["identity_use_policy"] not in ("local_only", "cloud_allowed"):
        raise ValueError("companion_interaction_invalid")
    history = value["identity_versions"]
    if not isinstance(history, dict) or len(history) > 32:
        raise ValueError("companion_interaction_invalid")
    for version, item in history.items():
        if not isinstance(version, str) or not version.isdecimal() or len(version) > 20 or int(version) < 1 or not isinstance(item, dict) or set(item) != {"identity", "use_policy"} or not validate_identity(item["identity"]) or item["use_policy"] not in ("local_only", "cloud_allowed"):
            raise ValueError("companion_interaction_invalid")
    affect = value["cloud_affect"]
    if not isinstance(affect, dict) or set(affect) != {"values", "anchor_utc", "mode", "mode_until"}:
        raise ValueError("companion_interaction_invalid")
    if not isinstance(affect["values"], list) or len(affect["values"]) != 3 or any(type(item) not in (int, float) or not low <= item <= high for item, low, high in zip(affect["values"], (-1, 0, -0.25), (1, 1, 0.25))):
        raise ValueError("companion_interaction_invalid")
    if affect["mode"] not in ("neutral", "focused") or any(type(affect[field]) not in (int, float) or not 0 <= affect[field] <= 1e12 for field in ("anchor_utc", "mode_until")):
        raise ValueError("companion_interaction_invalid")
    budget = value["cloud_budget"]
    if not isinstance(budget, list) or len(budget) > 256:
        raise ValueError("companion_interaction_invalid")
    for row in budget:
        if not isinstance(row, list) or len(row) != 4 or any(type(item) not in (float, int) or not low <= item <= high for item, low, high in zip(row, (0, -0.15, 0, -0.05), (1e12, 0.15, 0.15, 0.05))):
            raise ValueError("companion_interaction_invalid")
    policies = value["preference_policies"]
    if not isinstance(policies, dict) or not set(policies) <= {"persistent", "today"}:
        raise ValueError("companion_interaction_invalid")
    for scope, entries in policies.items():
        if not isinstance(entries, dict) or not set(entries) <= {"verbosity", "humor_allowed", "followup_allowed"} or any(policy not in ("local_only", "cloud_allowed") for policy in entries.values()):
            raise ValueError("companion_interaction_invalid")
    sources = value["preference_sources"]
    if not isinstance(sources, dict) or not set(sources) <= {"today", "persistent"}:
        raise ValueError("companion_interaction_invalid")
    for entries in sources.values():
        if not isinstance(entries, dict) or not set(entries) <= {"verbosity", "humor_allowed", "followup_allowed"}:
            raise ValueError("companion_interaction_invalid")
        for source in entries.values():
            if not isinstance(source, dict) or set(source) != {"source_ref", "span", "use_policy", "revision"} or not isinstance(source["source_ref"], str) or len(source["source_ref"].encode("utf-8")) > 256 or source["use_policy"] not in ("local_only", "cloud_allowed") or type(source["revision"]) is not int or source["revision"] < 0:
                raise ValueError("companion_interaction_invalid")
            span = source["span"]
            if span is not None and (not isinstance(span, list) or len(span) != 2 or any(type(offset) is not int for offset in span) or not 0 <= span[0] < span[1] <= 65536):
                raise ValueError("companion_interaction_invalid")
    prefs = value["preferences"]
    if not isinstance(prefs, dict) or not set(prefs) <= {"persistent", "today"}:
        raise ValueError("companion_interaction_invalid")
    for scope, entries in prefs.items():
        if not isinstance(entries, dict) or not set(entries) <= {"followup_allowed", "expires_at"}:
            raise ValueError("companion_interaction_invalid")
        if "followup_allowed" in entries and not preference_valid("followup_allowed", entries["followup_allowed"]):
            raise ValueError("companion_interaction_invalid")
        if scope == "today" and (type(entries.get("expires_at")) not in (float, int) or not 0 <= entries["expires_at"] <= 1e12):
            raise ValueError("companion_interaction_invalid")
        if scope == "persistent" and "expires_at" in entries:
            raise ValueError("companion_interaction_invalid")



def validate_state(state: dict[str, Any]) -> None:
    keys = {"schema_version", "reducer_version", "revision", "reset_epoch", "enabled", "emotions_enabled", "persona", "anchor_utc", "last_seen_utc", "values", "preferences", "outcome_mode", "outcome_mode_until"}
    if not isinstance(state, dict) or set(state) != keys:
        raise ValueError
    if type(state["schema_version"]) is not int or state["schema_version"] != SCHEMA_VERSION:
        raise ValueError
    if type(state["reducer_version"]) is not int or state["reducer_version"] != REDUCER_VERSION:
        raise ValueError
    for field in ("revision", "reset_epoch"):
        if type(state[field]) is not int or state[field] < 0:
            raise ValueError
    for field in ("enabled", "emotions_enabled"):
        if type(state[field]) is not bool:
            raise ValueError
    persona = state["persona"]
    if not isinstance(persona, dict) or set(persona) != {"version", "preset", "style"}:
        raise ValueError
    if persona["preset"] not in PRESETS or type(persona["version"]) is not int or persona["version"] < 1:
        raise ValueError
    if not isinstance(persona["style"], dict) or set(persona["style"]) != set(STYLE_KEYS):
        raise ValueError
    if not all(_finite(v, 0, 1) for v in persona["style"].values()):
        raise ValueError
    values = state["values"]
    if not isinstance(values, list) or len(values) != 3:
        raise ValueError
    if not all(_finite(v, low, high) for v, low, high in zip(values, (-1, 0, -0.25), (1, 1, 0.25))):
        raise ValueError
    for field in ("anchor_utc", "last_seen_utc", "outcome_mode_until"):
        if not _finite(state[field], 0, 1e12):
            raise ValueError
    if state["last_seen_utc"] < state["anchor_utc"]:
        raise ValueError
    if state["outcome_mode"] not in ("neutral", "focused"):
        raise ValueError
    prefs = state["preferences"]
    if not isinstance(prefs, dict) or not set(prefs) <= {"today", "persistent"}:
        raise ValueError
    for scope, entry in prefs.items():
        if not isinstance(entry, dict) or not set(entry) <= {"verbosity", "humor_allowed", "expires_at"}:
            raise ValueError
        if "verbosity" in entry and entry["verbosity"] not in ("brief", "normal", "detailed"):
            raise ValueError
        if "humor_allowed" in entry and type(entry["humor_allowed"]) is not bool:
            raise ValueError
        if scope == "today" and not _finite(entry.get("expires_at"), 0, 1e12):
            raise ValueError
        if scope == "persistent" and "expires_at" in entry:
            raise ValueError

