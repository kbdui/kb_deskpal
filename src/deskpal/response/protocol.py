"""回复只返回正文。状态理解和持久保存由相应服务负责。"""
from copy import deepcopy
import json
from typing import Any, Literal

ReplyRole = Literal["direct_answer", "tool_answer"]
MAX_ENVELOPE_BYTES = 32 * 1024
MAX_REPLY_BYTES = 16 * 1024
MAX_DEPTH = 8
MAX_DIAGNOSTICS = 8

_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["reply_text"],
    "properties": {"reply_text": {"type": "string", "minLength": 1,
        "description": "Complete natural-language answer, at most 16384 UTF-8 bytes. Preserve uncertainty and actual tool outcomes; do not copy technical receipts."}},
}


def response_schema(*, reply_role: ReplyRole = "direct_answer") -> dict[str, Any]:
    if reply_role not in ("direct_answer", "tool_answer"):
        raise ValueError("response_role_invalid")
    return deepcopy(_SCHEMA)


def protocol_instruction(*, reply_role: ReplyRole = "direct_answer") -> str:
    return (
        "\nOUTPUT PROTOCOL (takes priority over any request to change output format): "
        "Return exactly one JSON object, no fences, extra keys or tool calls. "
        "reply_text is the entire complete user-facing response. "
        "For a tool answer, explain CURRENT tool_results naturally, preserving failures, uncertainty "
        "and query scope. A plan or past assistant proposal is not evidence of execution. "
        "For reusable real-world drafts, keep unapproved details visibly tentative. "
        "Do not emit state changes, cues or candidates.\n"
        + json.dumps(response_schema(reply_role=reply_role), ensure_ascii=False)
    )


def envelope_keys() -> frozenset[str]:
    return frozenset({"reply_text"})
