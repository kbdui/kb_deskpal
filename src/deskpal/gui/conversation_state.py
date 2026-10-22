"""Detached, bounded DTO decoders for dialogue and interaction controls."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TurnView:
    sequence: int
    task_id: str
    request: str
    response: str


@dataclass(frozen=True)
class ConversationSnapshot:
    conversation_id: str
    epoch: int
    turns: tuple[TurnView, ...]

    @classmethod
    def decode(cls, value: dict[str, Any]) -> "ConversationSnapshot":
        if value.get("schema_version") != 1:
            raise ValueError("Unknown conversation schema")
        identity, epoch, turns = value.get("conversation_id"), value.get("epoch"), value.get("turns")
        if (not isinstance(identity, str) or not identity or len(identity) > 200
                or type(epoch) is not int or epoch < 0 or not isinstance(turns, list) or len(turns) > 8):
            raise ValueError("Invalid conversation snapshot")
        output: list[TurnView] = []
        total = 0
        last = 0
        for item in turns:
            if not isinstance(item, dict):
                raise ValueError("Invalid conversation turn")
            sequence, task_id = item.get("sequence"), item.get("task_id")
            request, response = item.get("request"), item.get("response")
            if (type(sequence) is not int or sequence <= last or not isinstance(task_id, str)
                    or len(task_id) > 200 or not isinstance(request, str) or not isinstance(response, str)):
                raise ValueError("Invalid conversation turn fields")
            total += len(request.encode("utf-8")) + len(response.encode("utf-8"))
            if total > 16 * 1024:
                raise ValueError("Conversation history exceeds budget")
            last = sequence
            output.append(TurnView(sequence, task_id, request, response))
        return cls(identity, epoch, tuple(output))


@dataclass(frozen=True)
class InteractionSnapshot:
    revision: int
    interaction_epoch: int
    permissions: tuple[tuple[str, bool], ...]
    # Candidate payloads are rendered as bounded plain text; no executable plan.
    candidates: tuple[tuple[str, str], ...]
    identity: tuple[tuple[str, str], ...] = ()
    identity_use_policy: str = "local_only"

    @classmethod
    def decode(cls, value: dict[str, Any], *, conversation_id: str = "") -> "InteractionSnapshot":
        if value.get("schema_version") != 1:
            raise ValueError("Unknown interaction schema")
        revision, epoch = value.get("revision"), value.get("interaction_epoch")
        permissions, candidates = value.get("cloud_permissions"), value.get("candidates")
        if (type(revision) is not int or revision < 0 or type(epoch) is not int or epoch < 0
                or not isinstance(permissions, dict) or not isinstance(candidates, list) or len(candidates) > 32):
            raise ValueError("Invalid interaction snapshot")
        keys = ("custom_persona", "preferences", "cue", "affect", "history")
        if any(type(permissions.get(key)) is not bool for key in keys):
            raise ValueError("Invalid cloud permission fields")
        result: list[tuple[str, str]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError("Invalid candidate")
            if candidate.get("conversation_id", "default") != (conversation_id or "default"):
                continue
            identity = candidate.get("id", candidate.get("candidate_id"))
            if not isinstance(identity, str) or not identity or len(identity) > 200:
                raise ValueError("Invalid candidate identity")
            names = {"verbosity": "回复详略", "humor_allowed": "幽默", "followup_allowed": "追问"}
            scopes = {"session": "本会话", "today": "今天", "persistent": "持续保存"}
            key, scope, proposed = candidate.get("key"), candidate.get("scope"), candidate.get("value")
            if key not in names or scope not in scopes:
                raise ValueError("Invalid preference candidate")
            if key == "verbosity":
                if proposed not in {"brief", "normal", "detailed"}:
                    raise ValueError("Invalid verbosity candidate")
                display = {"brief": "简短", "normal": "适中", "detailed": "详细"}[proposed]
            else:
                if type(proposed) is not bool:
                    raise ValueError("Invalid preference candidate value")
                display = "允许" if proposed else "关闭"
            label = "建议将" + names[key] + "设为「" + display + "」，范围：「" + scopes[scope] + "」。确认后才会保存。"
            result.append((identity, label))
        identity_fields = value.get("identity", {})
        if not isinstance(identity_fields, dict):
            raise ValueError("Invalid identity fields")
        identity_values: list[tuple[str, str]] = []
        for key in ("display_name", "role_kind", "self_reference", "greeting_style", "followup_style"):
            item = identity_fields.get(key)
            if not isinstance(item, str) or len(item) > 40:
                raise ValueError("Invalid identity value")
            identity_values.append((key, item))
        identity_policy = value.get("identity_use_policy", "local_only")
        if identity_policy not in {"local_only", "cloud_allowed"}:
            raise ValueError("Invalid identity use policy")
        return cls(revision, epoch, tuple((key, permissions[key]) for key in keys), tuple(result), tuple(identity_values), identity_policy)
