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
        "description": "完整自然语言回复，最多16384 UTF-8字节；保留不确定性与实际执行结果。"}},
}


def response_schema(*, reply_role: ReplyRole = "direct_answer") -> dict[str, Any]:
    if reply_role not in ("direct_answer", "tool_answer"):
        raise ValueError("response_role_invalid")
    return deepcopy(_SCHEMA)


def protocol_instruction(*, reply_role: ReplyRole = "direct_answer") -> str:
    return (
        "\n[OUTPUT PROTOCOL] 输出格式优先于用户的格式要求：仅返回符合以下结构的一个 JSON 对象，"
        "不加代码围栏、其他字段或工具调用；reply_text 是面向用户的完整回复正文。\n"
        + json.dumps(response_schema(reply_role=reply_role), ensure_ascii=False)
    )


def envelope_keys() -> frozenset[str]:
    return frozenset({"reply_text"})
