"""Interaction facade operations, separated from affect and terminal events."""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Collection
from datetime import datetime, timedelta
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from .errors import CompanionError, CompanionStorageError, CompanionValidationError
from .constants import HOUR_COUNT_LIMIT, MINUTE_COUNT_LIMIT
from .interaction import InteractionState
from .models import CLOUD_CATEGORIES, IDENTITY_DEFAULTS, InteractionConstraint, preference_valid
from .policy import PRESETS, baseline, instruction
from .reducer import outcome_delta, values_at
from .repository import StateRepository, load_interaction, save_interaction, validate_identity


class InteractionOperations(StateRepository):
    """Host-locked interaction state and explicit configuration operations."""

    _interaction: InteractionState
    _session_preferences: dict[str, Any]
    _session_policies: dict[str, str]
    _cue: tuple[str, float] | None
    _lock: Any
    _timezone: ZoneInfo

    def _now(self) -> float:
        raise NotImplementedError

    @staticmethod
    def _values(state: dict[str, Any], now: float) -> list[float]:
        raise NotImplementedError

    @staticmethod
    def _fields(parameters: dict[str, Any], allowed: set[str]) -> None:
        raise NotImplementedError

    def _warn(self, warning: str) -> None:
        raise NotImplementedError

    def _sync_identity_version(self, state: dict[str, Any]) -> None:
        config = load_interaction(self._db())
        config["identity_versions"][str(state["persona"]["version"])] = {
            "identity": deepcopy(config["identity"]), "use_policy": config["identity_use_policy"]}
        retained = {str(row[0]) for row in self._db().execute("SELECT version FROM persona_versions")}
        config["identity_versions"] = {version: entry for version, entry in config["identity_versions"].items() if version in retained}
        save_interaction(self._db(), config)

    def _neutralize_cloud(self, state: dict[str, Any], now: float) -> None:
        config = load_interaction(self._db())
        config["cloud_affect"] = {"values": list(baseline(PRESETS[state["persona"]["preset"]])),
                                  "anchor_utc": now, "mode": "neutral", "mode_until": 0.0}
        save_interaction(self._db(), config)

    @staticmethod
    def _cloud_values(state: dict[str, Any], config: dict[str, Any], now: float) -> list[float]:
        affect = config["cloud_affect"]
        return values_at({"values": affect["values"], "anchor_utc": affect["anchor_utc"],
                          "persona": {"style": PRESETS[state["persona"]["preset"]]}}, now)

    def _project_cloud_outcome(self, state: dict[str, Any], config: dict[str, Any], status: str, now: float) -> None:
        # These budgets are independent of local-only events. Reusing the
        # local delta/window occupancy would itself leak those events.
        if now < config["cloud_affect"]["anchor_utc"]:
            return
        rows = [tuple(row) for row in config["cloud_budget"] if row[0] > now - 3600]
        config["cloud_budget"] = [list(row) for row in rows]
        if len(rows) >= HOUR_COUNT_LIMIT or sum(row[0] > now - 60 for row in rows) >= MINUTE_COUNT_LIMIT:
            return
        delta = outcome_delta(status, rows, now)
        values = self._cloud_values(state, config, now)
        config["cloud_affect"]["values"] = [max(low, min(high, value + step)) for value, step, low, high in zip(values, delta, (-1, 0, -0.25), (1, 1, 0.25))]
        config["cloud_affect"]["anchor_utc"] = now
        config["cloud_budget"].append([now, *delta])
        if status == "failed":
            config["cloud_affect"]["mode"] = "focused"
            config["cloud_affect"]["mode_until"] = now + 60

    @staticmethod
    def _interaction_identity(conversation_id: Any, use_policy: Any) -> None:
        if not isinstance(conversation_id, str) or not conversation_id or len(conversation_id.encode("utf-8")) > 256:
            raise CompanionValidationError("companion_conversation_invalid")
        if use_policy not in ("local_only", "cloud_allowed"):
            raise CompanionValidationError("companion_use_policy_invalid")

    def _today_expiry(self, now: float) -> float:
        local = datetime.fromtimestamp(now, self._timezone)
        tomorrow = local.date() + timedelta(days=1)
        midnight = datetime.combine(tomorrow, datetime.min.time(), tzinfo=self._timezone)
        return min(now + 86400, midnight.timestamp())

    def _set_interaction_preference(self, state: dict[str, Any], config: dict[str, Any], key: str,
                                    value: Any, scope: str, conversation_id: str, policy: str,
                                    source_ref: str, now: float, span: tuple[int, int] | None = None) -> None:
        if not preference_valid(key, value) or scope not in ("session", "today", "persistent"):
            raise CompanionValidationError("companion_preference_invalid")
        old_policy = (self._interaction.sessions.get(conversation_id, {}).get(key, {}).get("use_policy")
                      if scope == "session" else config["preference_policies"].get(scope, {}).get(key))
        if old_policy == "cloud_allowed" and policy == "local_only":
            self._interaction.epoch += 1
            self._interaction.prepared.clear()
            self._interaction.candidates.clear()
        if scope == "session":
            self._interaction.add_session(conversation_id, InteractionConstraint(key, value, scope, span or (0, 0)), source_ref, policy)
            return
        target = config["preferences"] if key == "followup_allowed" else state["preferences"]
        entry = target.setdefault(scope, {})
        if scope == "today" and entry.get("expires_at", 0) <= now:
            entry.clear()
        entry[key] = value
        if scope == "today":
            entry["expires_at"] = self._today_expiry(now)
        config["preference_policies"].setdefault(scope, {})[key] = policy
        config["preference_sources"].setdefault(scope, {})[key] = {"source_ref": source_ref,
            "span": list(span) if span else None, "use_policy": policy, "revision": state["revision"] + 1}

    def _interaction_command(self, state: dict[str, Any], action: str, p: dict[str, Any], now: float) -> None:
        config = load_interaction(self._db())
        common = {"expected_revision"}
        if action == "cloud_permissions":
            self._fields(p, common | {"permissions"})
            edits = p.get("permissions")
            if not isinstance(edits, dict) or not edits or not set(edits) <= set(CLOUD_CATEGORIES) or any(type(flag) is not bool for flag in edits.values()):
                raise CompanionValidationError("companion_cloud_permissions_invalid")
            revoked = any(config["cloud_permissions"][key] and not flag for key, flag in edits.items())
            config["cloud_permissions"].update(edits)
            if revoked:
                self._interaction.clear()
                self._session_preferences.clear()
                self._session_policies.clear()
                self._cue = None
        elif action == "identity":
            self._fields(p, common | set(IDENTITY_DEFAULTS) | {"use_policy"})
            identity = {**config["identity"], **{key: value for key, value in p.items() if key in IDENTITY_DEFAULTS}}
            policy = p.get("use_policy", "cloud_allowed")
            if not validate_identity(identity) or policy not in ("local_only", "cloud_allowed"):
                raise CompanionValidationError("companion_identity_invalid")
            if config["identity_use_policy"] == "cloud_allowed" and policy == "local_only":
                self._interaction.clear()
            config["identity"], config["identity_use_policy"] = identity, policy
            self._persona_version(state)
        elif action in ("confirm_candidate", "reject_candidate"):
            self._fields(p, common | {"candidate_id"})
            candidate_id = p.get("candidate_id")
            if not isinstance(candidate_id, str) or not candidate_id or len(candidate_id) > 64:
                raise CompanionValidationError("companion_candidate_invalid")
            terminal = "confirmed" if action == "confirm_candidate" else "rejected"
            if self._interaction.resolved.get(candidate_id) == terminal:
                return
            self._interaction.prune(now)
            item = self._interaction.candidates.get(candidate_id)
            if item is None or item["generation"] != self._interaction.epoch:
                raise CompanionValidationError("companion_candidate_unavailable")
            if action == "confirm_candidate":
                self._set_interaction_preference(state, config, item["key"], item["value"], item["scope"],
                    item["conversation_id"], item["use_policy"], item["source_ref"], now, (item["span"][0], item["span"][1]))
            self._interaction.resolve(candidate_id, terminal)
        elif action == "clear_cue":
            self._fields(p, common | {"conversation_id"})
            conversation_id = p.get("conversation_id")
            if conversation_id is not None:
                self._interaction_identity(conversation_id, "local_only")
                self._interaction.cues.pop(conversation_id, None)
            else:
                self._interaction.cues.clear()
            self._cue = None
            self._interaction.epoch += 1
            self._interaction.prepared.clear()
            self._interaction.candidates.clear()
        elif action == "clear_constraints":
            self._fields(p, common | {"scope", "conversation_id"})
            scope = p.get("scope", "all")
            conversation_id = p.get("conversation_id", "default")
            self._interaction_identity(conversation_id, "local_only")
            if scope not in ("session", "today", "persistent", "all"):
                raise CompanionValidationError("companion_scope_invalid")
            for selected in (("session", "today", "persistent") if scope == "all" else (scope,)):
                if selected == "session":
                    self._interaction.sessions.pop(conversation_id, None)
                    if conversation_id == "default":
                        self._session_preferences.clear()
                        self._session_policies.clear()
                else:
                    state["preferences"].pop(selected, None)
                    config["preferences"].pop(selected, None)
                    config["preference_policies"].pop(selected, None)
                    config["preference_sources"].pop(selected, None)
            self._interaction.epoch += 1
            self._interaction.prepared.clear()
            self._interaction.candidates.clear()
        save_interaction(self._db(), config)

    def interaction_snapshot(self) -> dict[str, Any]:
        with self._lock:
            try:
                state, now = self._load(), self._now()
                config = load_interaction(self._db())
                result = self._interaction.snapshot(config, now)
                result["revision"] = state["revision"]
                result["preferences"] = {**deepcopy(state["preferences"]), "extra": deepcopy(config["preferences"])}
                result["preference_policies"] = deepcopy(config["preference_policies"])
                result["preference_sources"] = deepcopy(config["preference_sources"])
                return result
            except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError):
                raise CompanionStorageError("companion_storage_unavailable") from None

    def prepare_interaction(self, request: str, *, task_id: str, conversation_id: str = "default",
                            use_policy: str = "cloud_allowed") -> dict[str, Any]:
        if not isinstance(request, str) or len(request.encode("utf-8")) > 65536 or not isinstance(task_id, str) or not task_id or len(task_id.encode("utf-8")) > 256:
            raise CompanionValidationError("companion_interaction_request_invalid")
        self._interaction_identity(conversation_id, use_policy)
        with self._lock:
            previous = deepcopy(self._interaction)
            previous_cue = self._cue
            db = self._db()
            try:
                db.execute("BEGIN IMMEDIATE")
                state, now = self._load(), self._now()
                config = load_interaction(db)
                if not state["enabled"]:
                    db.execute("COMMIT")
                    return {"schema_version": 1, "interaction_epoch": self._interaction.epoch,
                            "constraints": {}, "preferences": {}, "mode": "neutral", "use_policy": use_policy,
                            "consumed_profile_keys": ()}
                constraints = self._interaction.prepare(request, task_id, conversation_id, use_policy, now)
                if self._interaction.parser.clears_cue(request):
                    self._cue = None
                once: dict[str, Any] = {}
                changed = False
                for item in constraints:
                    if item.scope == "once":
                        once[item.key] = item.value
                    else:
                        self._set_interaction_preference(state, config, item.key, item.value, item.scope,
                                                         conversation_id, use_policy, task_id, now, item.span)
                        changed = True
                if changed:
                    state["revision"] += 1
                    state["last_seen_utc"] = now
                    self._save(state)
                    save_interaction(db, config)
                result = {"schema_version": 1, "interaction_epoch": self._interaction.epoch,
                    "constraints": once, "preferences": self._interaction.session_values(conversation_id, cloud=False),
                    "mode": self._interaction.cue_mode(conversation_id, cloud=False, now=now), "use_policy": use_policy,
                    "consumed_profile_keys": tuple({item.key for item in constraints if item.key == "verbosity"})}
                db.execute("COMMIT")
                return result
            except Exception as error:
                self._interaction = previous
                self._cue = previous_cue
                self._rollback()
                if isinstance(error, (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError)) and not isinstance(error, CompanionError):
                    raise CompanionStorageError("companion_storage_unavailable") from None
                raise

    def accept_signals(self, task_id: str, signals: Any, *, conversation_id: str = "default",
                       use_policy: str = "cloud_allowed") -> None:
        self._interaction_identity(conversation_id, use_policy)
        with self._lock:
            state = self._load()
            if not state["enabled"]:
                self._interaction.prepared.pop(task_id, None)
                return
            if not state["emotions_enabled"] and isinstance(signals, dict):
                signals = {**signals, "cues": ()}
            self._interaction.accept(task_id, signals, conversation_id, use_policy, self._now())

    def invalidate_interaction(self) -> None:
        """Immediately discard in-flight evidence, cues and candidates, even if SQL fails later."""
        with self._lock:
            self._interaction.clear()
            self._session_preferences.clear()
            self._session_policies.clear()
            self._cue = None

    def forget_sources(self, task_ids: Collection[str]) -> None:
        """Erase source-derived preference copies in one companion transaction.

        Explicit configuration and identity settings have independent authority.
        Source-owned in-memory evidence is invalidated before fallible storage;
        callers must retain their durable privacy block if this method raises.
        Effect and command receipts and both outcome budgets are retained.
        """
        if isinstance(task_ids, (str, bytes)) or not isinstance(task_ids, Collection):
            raise CompanionValidationError("companion_task_ids_invalid")
        if any(not isinstance(task_id, str) or not task_id or len(task_id.encode("utf-8")) > 256
               for task_id in task_ids):
            raise CompanionValidationError("companion_task_ids_invalid")
        affected = frozenset(task_ids) - {"explicit_configuration"}
        if not affected:
            return
        with self._lock:
            db = self._db()
            # Do not restore these references on rollback: privacy use blocking
            # precedes physical erasure, including a failed erasure attempt.
            self._interaction.epoch += 1
            self._interaction.prepared.clear()
            for conversation_id, entries in tuple(self._interaction.sessions.items()):
                for key, entry in tuple(entries.items()):
                    if entry["source_ref"] in affected:
                        del entries[key]
                if not entries:
                    del self._interaction.sessions[conversation_id]
            for conversation_id, cue in tuple(self._interaction.cues.items()):
                if cue["source_ref"] in affected:
                    del self._interaction.cues[conversation_id]
                else:
                    cue["generation"] = self._interaction.epoch
            for candidate_id, candidate in tuple(self._interaction.candidates.items()):
                if candidate["source_ref"] in affected:
                    del self._interaction.candidates[candidate_id]
                else:
                    candidate["generation"] = self._interaction.epoch
            try:
                db.execute("BEGIN IMMEDIATE")
                state = self._load()
                config = load_interaction(db)
                changed = False
                for scope in ("today", "persistent"):
                    sources = config["preference_sources"].get(scope, {})
                    for key, source in tuple(sources.items()):
                        if source["source_ref"] not in affected:
                            continue
                        for preferences in (state["preferences"], config["preferences"]):
                            entry = preferences.get(scope, {})
                            entry.pop(key, None)
                            if not set(entry) - {"expires_at"}:
                                preferences.pop(scope, None)
                        config["preference_policies"].get(scope, {}).pop(key, None)
                        del sources[key]
                        changed = True
                    if not sources:
                        config["preference_sources"].pop(scope, None)
                    if not config["preference_policies"].get(scope):
                        config["preference_policies"].pop(scope, None)
                if changed:
                    state["revision"] += 1
                    state["last_seen_utc"] = self._now()
                    self._save(state)
                    save_interaction(db, config)
                db.execute("COMMIT")
            except Exception as error:
                self._rollback()
                if isinstance(error, (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError)) and not isinstance(error, CompanionError):
                    raise CompanionStorageError("companion_storage_unavailable") from None
                raise

    def close_conversation(self, conversation_id: str) -> None:
        self._interaction_identity(conversation_id, "local_only")
        with self._lock:
            self._interaction.sessions.pop(conversation_id, None)
            self._interaction.cues.pop(conversation_id, None)
            self._interaction.seen_conversations.pop(conversation_id, None)
            for candidate_id, entry in tuple(self._interaction.candidates.items()):
                if entry["conversation_id"] == conversation_id:
                    del self._interaction.candidates[candidate_id]
            for task_id, entry in tuple(self._interaction.prepared.items()):
                if entry["conversation_id"] == conversation_id:
                    del self._interaction.prepared[task_id]
            self._interaction.epoch += 1
            for entry in self._interaction.candidates.values():
                entry["generation"] = self._interaction.epoch
            for entry in self._interaction.cues.values():
                entry["generation"] = self._interaction.epoch
            for entry in self._interaction.prepared.values():
                entry["epoch"] = self._interaction.epoch
            if conversation_id == "default":
                self._session_preferences.clear()
                self._session_policies.clear()
                self._cue = None

    def style_instruction(self, request: str, *, cloud: bool, preferences: dict[str, Any] | None = None,
                          constraints: dict[str, Any] | None = None, mode: str | None = None,
                          conversation_id: str = "default", use_policy: str = "cloud_allowed") -> str:
        if not isinstance(request, str) or type(cloud) is not bool:
            raise CompanionValidationError("companion_style_request_invalid")
        self._interaction_identity(conversation_id, use_policy)
        if preferences is not None and not isinstance(preferences, dict) or constraints is not None and not isinstance(constraints, dict):
            raise CompanionValidationError("companion_style_request_invalid")
        if mode is not None and mode not in ("neutral", "supportive", "focused", "celebratory"):
            raise CompanionValidationError("companion_cue_invalid")
        with self._lock:
            try:
                state = self._load()
                if not state["enabled"]:
                    return ""
                now = self._now()
                config = load_interaction(self._db())
                permissions = config["cloud_permissions"]
                # Filter sources before combining preferences. A newer local
                # override must not hide or alter the eligible older fallback.
                merged = dict(preferences or {}) if not cloud or permissions["preferences"] else {}
                for scope in ("persistent", "today"):
                    for store in (state["preferences"], config["preferences"]):
                        entry = store.get(scope, {})
                        if scope == "today" and entry.get("expires_at", 0) <= now:
                            continue
                        for key, value in entry.items():
                            if key != "expires_at" and (not cloud or config["preference_policies"].get(scope, {}).get(key, "local_only") == "cloud_allowed"):
                                merged[key] = value
                merged.update({key: value for key, value in self._session_preferences.items()
                    if not cloud or self._session_policies.get(key, "local_only") == "cloud_allowed"})
                merged.update(self._interaction.session_values(conversation_id, cloud=cloud))
                selected_mode = self._interaction.cue_mode(conversation_id, cloud=cloud, now=now)
                if selected_mode == "neutral" and self._cue is not None and self._cue[1] > now:
                    selected_mode = self._interaction.cue_mode("default", cloud=cloud, now=now)
                mode_source = "cue"
                if cloud and not permissions["cue"]:
                    selected_mode = "neutral"
                if not state["emotions_enabled"]:
                    selected_mode = "neutral"
                if state["emotions_enabled"] and mode is not None and (not cloud or use_policy == "cloud_allowed"):
                    selected_mode = mode
                outcome = config["cloud_affect"] if cloud else {"mode": state["outcome_mode"], "mode_until": state["outcome_mode_until"]}
                if selected_mode == "neutral" and state["emotions_enabled"] and outcome["mode_until"] > now and (not cloud or permissions["affect"]):
                    selected_mode, mode_source = outcome["mode"], "affect"
                # Cloud affect uses a separate fixed-preset projection; no
                # custom axes or local-only terminal events enter it.
                pleasant = False
                if state["emotions_enabled"] and (not cloud or permissions["affect"]):
                    values = self._cloud_values(state, config, now) if cloud else self._values(state, now)
                    pleasant = values[0] + 0.2 * values[2] >= 0.12
                identity = deepcopy(config["identity"]) if not cloud or config["identity_use_policy"] == "cloud_allowed" else None
                if identity is not None:
                    turn = next((entry for entry in reversed(tuple(self._interaction.prepared.values()))
                                 if entry["conversation_id"] == conversation_id), None)
                    if turn is None or not turn["first_in_conversation"]:
                        identity["greeting_style"] = "none"
                return instruction(request, state["persona"], cloud=cloud,
                                   preferences=merged, constraints=constraints,
                                   mode=selected_mode, mode_source=mode_source, pleasant=pleasant, cloud_permissions=permissions,
                                   identity=identity, use_policy=use_policy)
            except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError):
                self._warn("companion_storage_unavailable")
                raise CompanionStorageError("companion_storage_unavailable") from None

