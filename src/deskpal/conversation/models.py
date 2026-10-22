"""Immutable records for the bounded, process-local dialogue window."""

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class ConversationRef:
    kind: str
    id: str
    revision: int
    use_policy: str = "cloud_allowed"

    def __post_init__(self) -> None:
        if (not isinstance(self.kind, str) or not self.kind or len(self.kind) > 64
                or not isinstance(self.id, str) or not self.id or len(self.id) > 512
                or type(self.revision) is not int or self.revision < 1
                or self.use_policy not in {"cloud_allowed", "local_only"}):
            raise ValueError("Invalid conversation reference")

    def to_dict(self) -> dict[str, str | int]:
        return {"kind": self.kind, "id": self.id, "revision": self.revision,
                "use_policy": self.use_policy}


@dataclass(frozen=True)
class ConversationTurn:
    sequence: int
    task_id: str
    request: str
    response: str
    refs: tuple[ConversationRef, ...]
    use_policy: str
    # Facts are canonical JSON strings: immutable and detached from callers.
    facts: tuple[str, ...] = ()

    @property
    def history_bytes(self) -> int:
        return len(self.request.encode("utf-8")) + len(self.response.encode("utf-8"))


@dataclass(frozen=True)
class ConversationWindow:
    id: str
    epoch: int
    touched_at: float
    turns: tuple[ConversationTurn, ...] = ()
