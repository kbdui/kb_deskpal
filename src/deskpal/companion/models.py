"""Finite interaction data. Evidence is host-bound after response publication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import unicodedata

CLOUD_CATEGORIES = ("custom_persona", "preferences", "cue", "affect", "history")
PREFERENCE_KEYS = ("verbosity", "humor_allowed", "followup_allowed")
PROFILE_PREFERENCE_KEYS = ("preferred_name", "reply_language")
IDENTITY_DEFAULTS: dict[str, Any] = {
    "display_name": "", "role_kind": "assistant", "self_reference": "neutral",
    "greeting_style": "none", "followup_style": "none",
}


@dataclass(frozen=True)
class InteractionConstraint:
    key: str
    value: str | bool
    scope: str
    span: tuple[int, int]


@dataclass(frozen=True)
class CueSignal:
    mode: str
    evidence_kind: str
    span: tuple[int, int]


@dataclass(frozen=True)
class PreferenceSignal:
    key: str
    value: str | bool
    scope: str
    span: tuple[int, int]


def preference_valid(key: Any, value: Any) -> bool:
    """Companion-owned expression preferences only; excludes Memory profile."""
    if key == "verbosity":
        return isinstance(value, str) and value in ("brief", "normal", "detailed")
    return key in ("humor_allowed", "followup_allowed") and type(value) is bool


def normalize_profile_value(key: Any, value: Any) -> str | None:
    """Normalize an untrusted Memory profile suggestion without accepting it."""
    if not isinstance(value, str):
        return None
    if key == "reply_language":
        language = value.strip().lower()
        return language if language in ("zh", "en") else None
    if key != "preferred_name":
        return None
    if any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value):
        return None
    name = unicodedata.normalize("NFC", value.strip())
    try:
        if not name or len(name) > 40 or len(name.encode("utf-8")) > 120:
            return None
    except UnicodeError:
        return None
    return name


def profile_preference_valid(key: Any, value: Any) -> bool:
    return normalize_profile_value(key, value) is not None
