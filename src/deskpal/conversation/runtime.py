"""Process-local conversation cache; never reconstructs dialogue from tasks."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, is_dataclass, replace
import json
from threading import RLock
from time import monotonic
from typing import Any
from uuid import uuid4

from .models import ConversationRef, ConversationTurn, ConversationWindow

MAX_CONVERSATIONS = 4
MAX_TURNS = 8
MAX_MESSAGES = 16
MAX_HISTORY_BYTES = 16 * 1024
MAX_ITEM_REFS = 128
MAX_CONTEXT_REFS = 512
MAX_PLANNING_ITEMS = 32
MAX_PLANNING_BYTES = 16 * 1024
IDLE_SECONDS = 1800


class ConversationConflict(ValueError):
    """An expired/closed conversation or epoch cannot receive new data."""


class ConversationCapacity(RuntimeError):
    """The user must close an existing conversation before opening another."""


def _ref(value: Any) -> ConversationRef:
    if isinstance(value, ConversationRef):
        return value
    if isinstance(value, (tuple, list)) and len(value) == 3:
        value = {"kind": value[0], "id": value[1], "revision": value[2]}
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if not isinstance(value, Mapping):
        raise ValueError("Context references must be structured")
    kind = value.get("kind", value.get("source_kind", "memory"))
    identity = value.get("id", value.get("object_id", value.get("source_id", value.get("memory_id"))))
    revision = value.get("revision", value.get("dependency_version"))
    policy = value.get("use_policy", "cloud_allowed")
    policy = getattr(policy, "value", policy)
    if (not isinstance(kind, str) or not kind or len(kind) > 64
            or not isinstance(identity, str) or not identity or len(identity) > 512
            or type(revision) is not int or revision < 1
            or policy not in {"cloud_allowed", "local_only"}):
        raise ValueError("Invalid context reference")
    return ConversationRef(kind, identity, revision, policy)


class ConversationRuntime:
    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._lock = RLock()
        self._epoch = 0
        self._windows: dict[str, ConversationWindow] = {}
        self._sequences: dict[str, int] = {}

    def _expire(self) -> None:
        now = self._clock()
        self._windows = {key: value for key, value in self._windows.items()
                         if 0 <= now - value.touched_at < IDLE_SECONDS}
        self._sequences = {key: value for key, value in self._sequences.items() if key in self._windows}

    def _window(self, identity: str, epoch: int | None = None) -> ConversationWindow:
        if not isinstance(identity, str) or (epoch is not None and (type(epoch) is not int or epoch < 0)):
            raise ConversationConflict("Invalid conversation identity or epoch")
        self._expire()
        window = self._windows.get(identity)
        if window is None or (epoch is not None and window.epoch != epoch):
            raise ConversationConflict("Conversation closed, expired or replaced")
        return window

    def open(self) -> dict[str, Any]:
        with self._lock:
            self._expire()
            if len(self._windows) >= MAX_CONVERSATIONS:
                raise ConversationCapacity("At most four conversations may remain open")
            identity = str(uuid4())
            window = ConversationWindow(identity, self._epoch, self._clock())
            self._windows[identity] = window
            self._sequences[identity] = 0
            return {"id": identity, "conversation_id": identity, "epoch": window.epoch}

    def close(self, conversation_id: str, expected_epoch: int) -> None:
        with self._lock:
            self._window(conversation_id, expected_epoch)
            del self._windows[conversation_id]
            self._sequences.pop(conversation_id, None)

    def reserve(self, conversation_id: str, expected_epoch: int) -> int:
        with self._lock:
            window = self._window(conversation_id, expected_epoch)
            sequence = self._sequences[conversation_id] + 1
            self._sequences[conversation_id] = sequence
            self._windows[conversation_id] = replace(window, touched_at=self._clock())
            return sequence

    def snapshot(self, conversation_id: str) -> dict[str, Any]:
        with self._lock:
            window = self._window(conversation_id)
            return {"schema_version": 1, "capabilities": ["bounded_history", "task_navigation"],
                    "conversation_id": window.id, "epoch": window.epoch,
                    "turns": [{"sequence": turn.sequence, "task_id": turn.task_id,
                               "request": turn.request, "response": turn.response,
                               "use_policy": turn.use_policy} for turn in window.turns],
                    "messages": len(window.turns) * 2,
                    "history_bytes": sum(turn.history_bytes for turn in window.turns)}

    def freeze(self, conversation_id: str, epoch: int, before_sequence: int, *,
               cloud: bool, history_allowed: bool) -> dict[str, Any]:
        """Freeze independent neutral planning and expression projections.

        Expression history owns provenance: historical user references are not
        new instructions or externally verified claims; assistant responses are
        unconfirmed proposals with stable task/sequence draft identity. Attached
        tool facts attest to host-recorded outcomes, not the truth of text in a
        tool's output. Only the trusted publisher supplies structured facts.
        """
        if type(before_sequence) is not int or before_sequence < 1:
            raise ValueError("A positive turn sequence is required")
        with self._lock:
            window = self._window(conversation_id, epoch)
            history: list[dict[str, Any]] = []
            planning: list[dict[str, Any]] = []
            history_rounds: list[tuple[dict[str, Any], dict[str, Any]]] = []
            planning_rounds: list[list[dict[str, Any]]] = []
            references: set[ConversationRef] = set()
            warnings: list[str] = []
            # Select complete recent rounds first. A round's user reference and
            # every host fact form one indivisible optional planning unit.
            for turn in reversed(window.turns):
                if turn.sequence >= before_sequence:
                    continue
                if cloud and (not history_allowed or turn.use_policy != "cloud_allowed"
                              or any(ref.use_policy != "cloud_allowed" for ref in turn.refs)):
                    warnings.append("history_use_not_allowed")
                    continue
                if len(references | set(turn.refs)) > MAX_CONTEXT_REFS:
                    warnings.append("context_reference_limit")
                    continue
                references.update(turn.refs)
                refs = [ref.to_dict() for ref in turn.refs]
                planning_refs = [ref.to_dict() for ref in turn.refs
                                 if not (ref.kind == "dialogue_turn" and ref.id.endswith(":assistant"))]
                source = {"kind": "dialogue_turn", "id": turn.task_id + ":user",
                          "revision": turn.sequence, "use_policy": turn.use_policy}
                assistant_source = {**source, "id": turn.task_id + ":assistant"}
                # Provenance is generated from the host's immutable turn, never
                # inferred from prose (including apparent user confirmation).
                # A draft version identifies one published response, not a
                # verified claim or authorization to execute its suggestions.
                history_round = (
                    {"role": "user", "content": turn.request, "verified": False,
                     "source_kind": "historical_user_reference", "instruction": False,
                     "externally_verified": False, "source_ref": source, "refs": refs},
                    {"role": "assistant", "content": turn.response, "verified": False,
                     "source_kind": "assistant_proposal", "draft_id": assistant_source["id"],
                     "draft_version": turn.sequence, "confirmed": False,
                     "instruction": False, "externally_verified": False,
                     "source_ref": assistant_source, "refs": refs,
                     "tool_facts": [{"kind": "tool_fact", "verified": True,
                                     "source_kind": "host_recorded_tool_outcome",
                                     "external_content_verified": False,
                                     "fact": json.loads(fact), "source_ref": assistant_source,
                                     "refs": refs} for fact in turn.facts]})
                # Expression provenance and tool outcomes consume the same
                # bounded history budget. Omit only complete expression rounds;
                # the existing neutral planning selection below is independent.
                candidate_history = [message for pair in history_rounds for message in pair]
                candidate_history.extend(history_round)
                history_encoded = json.dumps(candidate_history, ensure_ascii=False,
                                             allow_nan=False, sort_keys=True)
                if len(history_encoded.encode("utf-8")) <= MAX_HISTORY_BYTES:
                    history_rounds.append(history_round)
                else:
                    warnings.append("history_context_limit")
                round_items: list[dict[str, Any]] = [
                    {"kind": "user_reference", "content": turn.request,
                     "instruction": False, "source_ref": source, "refs": planning_refs}]
                round_items.extend({"kind": "tool_fact", "verified": True, "fact": json.loads(fact),
                                    "source_ref": source, "refs": planning_refs} for fact in turn.facts)
                candidate = planning + round_items
                encoded = json.dumps(candidate, ensure_ascii=False, allow_nan=False, sort_keys=True)
                if len(candidate) > MAX_PLANNING_ITEMS or len(encoded.encode("utf-8")) > MAX_PLANNING_BYTES:
                    warnings.append("planning_context_limit")
                    continue
                planning_rounds.append(round_items)
                planning.extend(round_items)
            history = [message for pair in reversed(history_rounds) for message in pair]
            planning = [item for group in reversed(planning_rounds) for item in group]
            self._windows[conversation_id] = replace(window, touched_at=self._clock())
            return {"conversation_id": conversation_id, "epoch": epoch, "history": history,
                    "planning_items": planning,
                    "refs": [(ref.kind, ref.id, ref.revision) for ref in sorted(references)],
                    "warnings": list(dict.fromkeys(warnings))}

    def publish(self, conversation_id: str, epoch: int, sequence: int, task_id: str,
                request: str, response: str, *, refs: Iterable[Any] = (),
                use_policy: str = "cloud_allowed", facts: Iterable[Any] = ()) -> None:
        use_policy = getattr(use_policy, "value", use_policy)
        if (type(sequence) is not int or sequence < 1 or not isinstance(task_id, str) or not task_id
                or not isinstance(request, str) or not isinstance(response, str)
                or use_policy not in {"cloud_allowed", "local_only"}):
            raise ValueError("Invalid conversation turn")
        # Bounded iteration prevents an untrusted/infinite source iterable consuming memory.
        references: set[ConversationRef] = set()
        for index, value in enumerate(refs):
            if index >= MAX_ITEM_REFS:
                return
            references.add(_ref(value))
            if len(references) > MAX_ITEM_REFS - 2:
                return  # Omit the whole dependent turn, never truncate its reference closure.
        references.add(ConversationRef("dialogue_turn", task_id + ":user", sequence, use_policy))
        references.add(ConversationRef("dialogue_turn", task_id + ":assistant", sequence, use_policy))
        frozen_facts: list[str] = []
        fact_bytes = 0
        for value in facts:
            if is_dataclass(value) and not isinstance(value, type):
                value = asdict(value)
            if not isinstance(value, Mapping):
                raise ValueError("Tool facts must be structured host facts")
            encoded = json.dumps(dict(value), ensure_ascii=False, allow_nan=False, sort_keys=True)
            fact_bytes += len(encoded.encode("utf-8"))
            if fact_bytes > MAX_HISTORY_BYTES or len(frozen_facts) >= MAX_MESSAGES:
                return
            frozen_facts.append(encoded)
        turn = ConversationTurn(sequence, task_id, request, response, tuple(sorted(references)),
                                use_policy, tuple(frozen_facts))
        with self._lock:
            window = self._window(conversation_id, epoch)
            if turn.history_bytes + fact_bytes > MAX_HISTORY_BYTES:
                return
            if any(item.sequence == sequence or item.task_id == task_id for item in window.turns):
                return
            turns = sorted((*window.turns, turn), key=lambda item: item.sequence)
            while (len(turns) > MAX_TURNS or len(turns) * 2 > MAX_MESSAGES
                   or sum(item.history_bytes + sum(len(fact.encode("utf-8")) for fact in item.facts)
                          for item in turns) > MAX_HISTORY_BYTES):
                turns.pop(0)
            self._windows[conversation_id] = replace(window, turns=tuple(turns), touched_at=self._clock())

    def invalidate(self) -> None:
        with self._lock:
            self._epoch += 1
            self._windows.clear()
            self._sequences.clear()
