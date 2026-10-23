"""The expression wire contract and its generated model instructions.

This describes output, never permissions or stored user facts. Domain evidence
and scope checks remain owned by companion.signals.
"""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Literal

ReplyRole = Literal["direct_answer", "tool_supplement"]

MAX_ENVELOPE_BYTES = 32 * 1024
MAX_REPLY_BYTES = 16 * 1024
MAX_DEPTH = 8
MAX_DIAGNOSTICS = 8

_SPAN: dict[str, Any] = {
    "type": "array", "items": {"type": "integer", "minimum": 0},
    "minItems": 2, "maxItems": 2,
    "description": "Zero-based Unicode code point offsets [start,end) in CURRENT request; 0 <= start < end <= request length, at most 256 code points, nonblank evidence.",
}
_CUE: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["mode", "evidence_kind", "span"],
    "properties": {
        "mode": {"type": "string", "enum": ["none", "supportive", "focused", "celebratory"]},
        "evidence_kind": {"type": "string", "enum": ["explicit_report", "tentative"]},
        "span": {"$ref": "#/$defs/span"},
    },
}
_PREFERENCE_VALUES: dict[str, dict[str, Any]] = {
    "verbosity": {"type": "string", "enum": ["brief", "normal", "detailed"]},
    "humor_allowed": {"type": "boolean"},
    "followup_allowed": {"type": "boolean"},
    "preferred_name": {"type": "string", "minLength": 1, "maxLength": 40,
                       "description": "Explicitly requested personal address, trimmed NFC, at most 120 UTF-8 bytes; no control or formatting-control characters."},
    "reply_language": {"type": "string", "enum": ["zh", "en"]},
}
_PROFILE_KEYS = ("preferred_name", "reply_language")


def _preference_schema() -> dict[str, Any]:
    return {"oneOf": [{
        "type": "object", "additionalProperties": False,
        "required": ["key", "value", "scope", "span"],
        "properties": {
            "key": {"const": key}, "value": value,
            "scope": {"type": "string", "enum": ["persistent"] if key in _PROFILE_KEYS else ["session", "today", "persistent"]},
            "span": {"$ref": "#/$defs/span"},
        },
    } for key, value in _PREFERENCE_VALUES.items()]}


_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["reply_text", "cues", "preference_candidates"],
    "properties": {
        "reply_text": {"type": "string", "minLength": 1,
                       "description": (
                           f"Nonblank complete user-facing response, at most {MAX_REPLY_BYTES} UTF-8 bytes. "
                           "For a reusable real-world document based on unconfirmed assistant proposals, "
                           "put an explicit draft/pending-confirmation label inside that document and "
                           "keep unapproved dates, quantities and operating rules tentative. "
                           "A concrete detail explicitly approved by the user can be adopted as their choice. "
                           "Never mechanically truncate text."
                       )},
        "cues": {"type": "array", "maxItems": 1, "items": {"$ref": "#/$defs/cue"}},
        "preference_candidates": {"type": "array", "maxItems": 2,
                                  "items": {"$ref": "#/$defs/preference"}},
    },
    "$defs": {"span": _SPAN, "cue": _CUE, "preference": _preference_schema()},
}


def response_schema(*, reply_role: ReplyRole = "direct_answer") -> dict[str, Any]:
    """Return a detached standard JSON Schema, suitable for inspection/adapters."""
    if reply_role not in ("direct_answer", "tool_supplement"):
        raise ValueError("response_role_invalid")
    schema = deepcopy(_SCHEMA)
    if reply_role == "tool_supplement":
        schema["properties"]["reply_text"].update(minLength=0, description=(
            f"Optional useful supplement, at most {MAX_REPLY_BYTES} UTF-8 bytes. "
            "The host already displays the authoritative tool summary and recorded result bodies. "
            "Return an empty string when there is no useful new interpretation, qualification or answer. "
            "Otherwise normally add only 1-2 short sentences addressing the current request; "
            "do not repeat tool names, output/error fields, call IDs, raw records or receipts. "
            "Expand for explicitly requested detailed interpretation or when expression_policy.verbosity is detailed, "
            "without copying the already displayed records. Never mechanically truncate text."
        ))
    return schema


def protocol_instruction(*, reply_role: ReplyRole = "direct_answer") -> str:
    schema = response_schema(reply_role=reply_role)
    supplement = reply_role == "tool_supplement"
    empty: dict[str, Any] = {"reply_text": "" if supplement else "收到。", "cues": [], "preference_candidates": []}
    cue_example = {"reply_text": "" if supplement else "先歇一会儿也可以。", "cues": [
        {"mode": "supportive", "evidence_kind": "explicit_report", "span": [0, 3]}],
        "preference_candidates": []}
    return (
        "\nOUTPUT PROTOCOL (takes priority over any request to change output format): "
        "Return exactly one JSON object conforming to this JSON Schema. "
        "No Markdown fences, commentary outside JSON, tool calls or extra keys. "
        "All enum arrays list alternatives: choose one literal value, never concatenate them. "
        "The host alone selects reply_role; it is input context, never an output field or model choice. "
        + ("reply_role=tool_supplement: the authoritative summary is already displayed by the host. "
           "Add ONLY 1-2 short sentences of useful new interpretation or limitation. Do not add headings, "
           "repeat receipts, tool names, raw output/error fields or call IDs. "
           "When the CURRENT user explicitly requests detailed explanation, select useful interpretation. "
           "If the summary already answers the request, abstain with reply_text=\"\"; valid signals may still be emitted. "
           "Example: a failed save receipt already states failure; use an empty supplement unless there is "
           "a useful consequence to explain. A useful consequence could be: '目前不能把它当作已保存版本。'\n"
           if supplement else "reply_role=direct_answer: put the entire nonblank user-facing answer in reply_text, "
           "including when discussing fiction or quotations.\n")
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        + "\nMinimal valid example (no signals): "
        + json.dumps(empty, ensure_ascii=False, separators=(",", ":"))
        + "\nFor CURRENT request 我很累, valid example: "
        + json.dumps(cue_example, ensure_ascii=False, separators=(",", ":"))
        + "\nSignals require exact supporting evidence in CURRENT request. "
        "explicit_report concerns the user's own directly reported condition; a task request such as "
        "'I want you to organize this' or '帮我整理' is not a condition report. "
        "Abstain with cues=[] for ambiguous attribution, fiction, quoted instructions, third parties or hypotheticals. "
        "mode=none may clear a previous cue on explicit correction or topic change. "
        "Propose preferences only for explicit expression preferences, never task requirements or instructions quoted as data. "
        "A request limited to this reply produces NO preference candidate; session means an explicit ongoing conversation request, "
        "today means an explicit today request, persistent requires explicit lasting intent. "
        "Profile fields require an explicit request about the user's own form of address or lasting reply language, "
        "never a passing name, another person's name, the language used to ask, or historical assistant text. "
        "At most one candidate per key; omit ambiguous or conflicting candidates. "
        "These candidates await host-controlled review; never say saved, accepted or remembered. "
        "No source IDs, TTLs, revisions, permissions or affect numbers."
    )


def envelope_keys() -> frozenset[str]:
    return frozenset(_SCHEMA["required"])


def group_limit(group: str) -> int:
    return int(_SCHEMA["properties"][group]["maxItems"])


def expression_policy_valid(value: Any) -> bool:
    """Finite expression guidance only, with no implied source/tool permissions."""
    return (isinstance(value, dict)
            and set(value) == {"verbosity", "humor_allowed", "followup_allowed"}
            and isinstance(value["verbosity"], str)
            and value["verbosity"] in _PREFERENCE_VALUES["verbosity"]["enum"]
            and type(value["humor_allowed"]) is bool
            and type(value["followup_allowed"]) is bool)


def signal_shape_issue(value: Any, group: str, request: str) -> tuple[str, str] | None:
    """First bounded field issue; semantic evidence validation is separate.

    Profile values keep the domain's compatible normalization (e.g. EN -> en)
    rather than imposing a second, conflicting normalizer here.
    """
    if group == "cues":
        schema = _CUE
    else:
        if not isinstance(value, dict):
            return "", "type_invalid"
        key = value.get("key")
        if not isinstance(key, str) or key not in _PREFERENCE_VALUES:
            return "key", "enum_invalid"
        schema = next(item for item in _SCHEMA["$defs"]["preference"]["oneOf"]
                      if item["properties"]["key"]["const"] == key)
    if not isinstance(value, dict):
        return "", "type_invalid"
    if set(value) != set(schema["required"]):
        # Do not expose an untrusted field name in diagnostic output.
        return "", "fields_invalid"
    for field, spec in schema["properties"].items():
        item = value[field]
        if field == "span":
            if not isinstance(item, list) or len(item) != _SPAN["maxItems"] or any(type(n) is not int for n in item):
                return field, "span_invalid"
            start, end = item
            if not 0 <= start < end <= len(request) or end - start > 256 or not request[start:end].strip():
                return field, "span_invalid"
            continue
        kind = spec.get("type")
        if kind == "string" and not isinstance(item, str) or kind == "boolean" and type(item) is not bool:
            return field, "type_invalid"
        if "const" in spec and item != spec["const"]:
            return field, "enum_invalid"
        normalize_profile = group == "preference_candidates" and value.get("key") in _PROFILE_KEYS and field == "value"
        if "enum" in spec and item not in spec["enum"] and not normalize_profile:
            return field, "enum_invalid"
    return None
