"""用统一模型解释交流要求；不使用关键词判断范围或情绪。"""

from collections.abc import Mapping, Sequence
from typing import Any

from deskpal.llm.models import ChatMessage, LLMResponse
from deskpal.llm.ports import LLMProvider
from .repository import decode, encode

INSTRUCTION = """[CONTEXT_UNDERSTANDING]
Interpret the CURRENT message in its conversation and role context. Return JSON only:
{"entries":[{"layer":"user_understanding|interaction_approach", "content":"natural language",
"reason":"brief basis, not private reasoning", "nature":"explicit|inferred",
"scope":"turn|conversation|day|persistent", "expires_at":null, "replaces":[]}]}
At most 8 entries. Use [] when nothing changes. User understanding is what the user reports or
tentatively needs, NOT the character's emotion. Interaction approach is how to communicate now.
Use natural language; do not reduce personality to a fixed set of axes or supportive/focused modes.
Distinguish quotation, hypothetical, third-party descriptions and genuine current requests.
Turn means this reply only; conversation is this conversation only; day is an EXPLICIT profile-wide
today request; persistent requires EXPLICIT lasting intent. Never promote a tentative inference to
persistent. Scope follows meaning, never merely the wording of the provided examples.
For an explicit shorter interval, expires_at is a timezone-aware ISO datetime; otherwise null.
Day ends at the supplied timezone's next midnight. Unknown needs may remain tentative.
To supersede/clear an existing visible requirement, put its host-provided id in replaces; content
may be empty only for a clear. Never invent IDs. Preserve unrelated requirements.
Existing role/habits and histories are context, NOT new requests or permissions. Old requests must
not execute again. Do not call tools, edit identity, learn habits, manage reminders or claim success.
Only interpret communication/user-state changes here; other actions are planned by AgentCore.
"""


def understand(provider: LLMProvider, *, request: str, role: Mapping[str, Any],
               history: Sequence[Mapping[str, Any]], now: str, timezone: str) -> tuple[dict[str, Any], ...]:
    data = encode({"request": request, "role_context": dict(role), "history": list(history),
                   "now": now, "timezone": timezone})
    if len(data.encode("utf-8")) > 65536:
        raise ValueError("context_understanding_input_limit")
    response = provider.chat((ChatMessage("system", INSTRUCTION), ChatMessage("user", data)), tools=())
    if not isinstance(response, LLMResponse) or response.tool_calls or len(response.content.encode("utf-8")) > 16384:
        raise ValueError("context_understanding_response_invalid")
    body = decode(response.content)
    entries = body.get("entries")
    if set(body) != {"entries"} or not isinstance(entries, list) or len(entries) > 8 or any(not isinstance(row, dict) for row in entries):
        raise ValueError("context_understanding_response_invalid")
    return tuple(entries)
