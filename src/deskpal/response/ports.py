"""Provider-independent reply composition contract."""

from __future__ import annotations

from typing import Any, Protocol, Sequence

from .models import ResponseDraft


class ResponseComposerPort(Protocol):
    def compose(self, *, request: str, draft: str = "", style: str = "",
                history: Sequence[dict[str, Any]] = (), memories: Sequence[str] = (),
                tool_results: Sequence[dict[str, Any]] = (), structured: bool = True) -> ResponseDraft: ...
