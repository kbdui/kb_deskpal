"""Validated, non-authoritative response drafts."""

from __future__ import annotations

from dataclasses import dataclass



@dataclass(frozen=True)
class ResponseDiagnostic:
    """A bounded protocol location and fixed reason, with no model/user data."""

    group: str
    field: str
    code: str


@dataclass(frozen=True)
class ResponseDraft:
    reply_text: str
    degraded_reason: str | None = None
    diagnostics: tuple[ResponseDiagnostic, ...] = ()
