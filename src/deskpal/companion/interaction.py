"""Bounded, in-memory interaction provenance; no model or tool capability."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

from .models import PROFILE_PREFERENCE_KEYS, CueSignal, InteractionConstraint, PreferenceSignal
from .signals import ConstraintParser, validate_cue, validate_preference


class InteractionState:
    def __init__(self) -> None:
        self.epoch = 0
        self.sessions: dict[str, dict[str, dict[str, Any]]] = {}
        self.cues: dict[str, dict[str, Any]] = {}
        self.candidates: dict[str, dict[str, Any]] = {}
        self.resolved: dict[str, str] = {}
        self.prepared: dict[str, dict[str, Any]] = {}
        self.seen_conversations: dict[str, float] = {}
        self.parser = ConstraintParser()

    def clear(self) -> None:
        self.epoch += 1
        self.sessions.clear()
        self.cues.clear()
        self.candidates.clear()
        self.resolved.clear()
        self.prepared.clear()
        self.seen_conversations.clear()

    def prune(self, now: float) -> None:
        for candidate_id, item in tuple(self.candidates.items()):
            if item["expires_at"] <= now:
                del self.candidates[candidate_id]
        for conversation_id, item in tuple(self.cues.items()):
            if item["expires_at"] <= now:
                del self.cues[conversation_id]
        for task_id, item in tuple(self.prepared.items()):
            if item["expires_at"] <= now:
                del self.prepared[task_id]

    def prepare(self, request: str, task_id: str, conversation_id: str, use_policy: str, now: float) -> tuple[InteractionConstraint, ...]:
        self.prune(now)
        prior = self.prepared.get(task_id)
        if prior is not None:
            if prior["request"] != request or prior["conversation_id"] != conversation_id or prior["use_policy"] != use_policy:
                raise ValueError("companion_task_evidence_conflict")
            return self.parser.parse(request)
        if self.parser.clears_cue(request):
            self.cues.pop(conversation_id, None)
            self.epoch += 1
            self.prepared.clear()
            self.candidates = {key: value for key, value in self.candidates.items() if value["conversation_id"] != conversation_id}
            for value in self.candidates.values():
                value["generation"] = self.epoch
        # A tentative cue survives at most the next prepared turn.
        cue = self.cues.get(conversation_id)
        if cue and cue["evidence_kind"] == "tentative":
            if cue.get("used", False):
                self.cues.pop(conversation_id, None)
            else:
                cue["used"] = True
        if task_id not in self.prepared:
            if len(self.prepared) >= 8:
                del self.prepared[next(iter(self.prepared))]
            first = conversation_id not in self.seen_conversations
            if first and len(self.seen_conversations) >= 4:
                del self.seen_conversations[next(iter(self.seen_conversations))]
            self.seen_conversations[conversation_id] = now
            self.prepared[task_id] = {"request": request, "conversation_id": conversation_id, "first_in_conversation": first,
                "use_policy": use_policy, "epoch": self.epoch, "expires_at": now + 1800}
        return self.parser.parse(request)

    def add_session(self, conversation_id: str, item: InteractionConstraint, task_id: str, policy: str) -> None:
        if conversation_id not in self.sessions and len(self.sessions) >= 4:
            del self.sessions[next(iter(self.sessions))]
        self.sessions.setdefault(conversation_id, {})[item.key] = {
            "value": item.value, "source_ref": task_id, "span": list(item.span), "use_policy": policy}

    def session_values(self, conversation_id: str, *, cloud: bool) -> dict[str, Any]:
        return {key: entry["value"] for key, entry in self.sessions.get(conversation_id, {}).items()
                if not cloud or entry["use_policy"] == "cloud_allowed"}

    def cue_mode(self, conversation_id: str, *, cloud: bool, now: float) -> str:
        cue = self.cues.get(conversation_id)
        if cue and cue["expires_at"] > now and (not cloud or cue["use_policy"] == "cloud_allowed"):
            return str(cue["mode"])
        return "neutral"

    def accept(self, task_id: str, signals: Any, conversation_id: str, policy: str, now: float) -> None:
        self.prune(now)
        prepared = self.prepared.pop(task_id, None)
        if (prepared is None or prepared["epoch"] != self.epoch or prepared["conversation_id"] != conversation_id
                or prepared["use_policy"] != policy or not isinstance(signals, dict)
                or not set(signals) <= {"cues", "preference_candidates"}):
            return
        message = prepared["request"]
        cues = signals.get("cues", ())
        candidates = signals.get("preference_candidates", ())
        if not isinstance(cues, (tuple, list)) or len(cues) > 1 or not isinstance(candidates, (tuple, list)) or len(candidates) > 2:
            return
        for raw in cues:
            cue: CueSignal | None = validate_cue(raw, message)
            if cue is None:
                continue
            if cue.mode == "none":
                self.cues.pop(conversation_id, None)
                self.epoch += 1
                self.prepared.clear()
                self.candidates = {key: value for key, value in self.candidates.items()
                                   if value["conversation_id"] != conversation_id}
                for value in self.candidates.values():
                    value["generation"] = self.epoch
                continue
            if conversation_id not in self.cues and len(self.cues) >= 4:
                del self.cues[next(iter(self.cues))]
            self.cues[conversation_id] = {"mode": cue.mode, "evidence_kind": cue.evidence_kind,
                "source_ref": task_id, "span": list(cue.span), "use_policy": policy,
                "expires_at": now + (1800 if cue.evidence_kind == "explicit_report" else 300), "generation": self.epoch}
        for raw in candidates:
            candidate: PreferenceSignal | None = validate_preference(raw, message)
            # Profile suggestions are routed by Application to Memory's
            # existing source-bound review. Companion never stores them.
            if candidate is None or candidate.key in PROFILE_PREFERENCE_KEYS or len(self.candidates) >= 16:
                continue
            candidate_id = uuid4().hex
            self.candidates[candidate_id] = {"candidate_id": candidate_id, "key": candidate.key,
                "value": candidate.value, "scope": candidate.scope, "span": list(candidate.span),
                "source_ref": task_id, "conversation_id": conversation_id, "use_policy": policy,
                "expires_at": now + 600, "generation": self.epoch}

    def resolve(self, candidate_id: str, result: str) -> None:
        self.candidates.pop(candidate_id, None)
        self.resolved[candidate_id] = result
        if len(self.resolved) > 64:
            del self.resolved[next(iter(self.resolved))]

    def snapshot(self, config: dict[str, Any], now: float) -> dict[str, Any]:
        self.prune(now)
        return {"schema_version": 1, "interaction_epoch": self.epoch,
                "cloud_permissions": deepcopy(config["cloud_permissions"]), "identity": deepcopy(config["identity"]),
                "identity_use_policy": config["identity_use_policy"], "constraints": deepcopy(self.sessions),
                "cues": deepcopy(self.cues), "candidates": deepcopy(list(self.candidates.values()))}
