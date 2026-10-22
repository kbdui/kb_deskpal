"""Validated, non-authoritative response drafts."""

from __future__ import annotations

from dataclasses import dataclass

from deskpal.companion.models import CueSignal, PreferenceSignal


@dataclass(frozen=True)
class ResponseDraft:
    reply_text: str
    cues: tuple[CueSignal, ...] = ()
    preference_candidates: tuple[PreferenceSignal, ...] = ()
    degraded_reason: str | None = None
