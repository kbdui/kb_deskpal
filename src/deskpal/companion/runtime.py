"""Bounded local Persona/Affect runtime.

The compatibility payload and affect reducer remain version 1; SQLite schema 2
adds isolated interaction settings. A receipt is retained for the lifetime
of the database, including resets. At 100,000 distinct terminal task IDs the
runtime refuses new effects instead of evicting deduplication keys. Only the
latest 32 persona versions are retained. There are at most 256 budget rows in a
rolling hour and at most 32 admitted outcomes per rolling minute. Current
signal evidence is bounded in memory; only explicit settings are persisted.

All mutations share an RLock and BEGIN IMMEDIATE transaction. State, receipt,
window debit and revision commit together. Read projections never debit a
window or persist a decayed state. A caller must catch CompanionStorageError
and preserve the authoritative task outcome; this service cannot run tools.
"""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Callable
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .policy import PRESETS, STYLE_KEYS, baseline, expression
from .interaction import InteractionState
from .interaction_runtime import InteractionOperations
from .constants import (SCHEMA_VERSION, REDUCER_VERSION, RECEIPT_LIMIT, PERSONA_HISTORY_LIMIT,
                        MINUTE_COUNT_LIMIT, HOUR_COUNT_LIMIT, MAX_OFFLINE_SECONDS, WINDOW_CAPS, HALF_LIVES, finite as _finite)
from .errors import (CompanionClosedError as CompanionClosedError, CompanionError as CompanionError,
                     CompanionRevisionConflict as CompanionRevisionConflict, CompanionStorageError as CompanionStorageError,
                     CompanionValidationError as CompanionValidationError)
from .models import IDENTITY_DEFAULTS
from .reducer import values_at, outcome_delta
from .repository import STORAGE_SCHEMA_VERSION, initial_interaction, load_interaction, save_interaction

class CompanionRuntime(InteractionOperations):
    """Qt-free expression service; ``None`` opens an ephemeral in-memory DB.

    ``command`` returns the same snapshot shape as ``snapshot``. Validation,
    stale revisions, closed lifecycle and persistence failures are typed
    exceptions. The application owns fallback presentation and diagnostics.
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        timezone: str = "Asia/Shanghai",
        wall_clock: Callable[[], float] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(timezone, str) or len(timezone) > 128:
            raise CompanionValidationError("companion_timezone_invalid")
        try:
            self._timezone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise CompanionValidationError("companion_timezone_invalid") from None
        if (wall_clock is not None and not callable(wall_clock)) or (monotonic_clock is not None and not callable(monotonic_clock)):
            raise CompanionValidationError("companion_clock_invalid")
        self._wall_clock = wall_clock if wall_clock is not None else time.time
        self._monotonic_clock = monotonic_clock if monotonic_clock is not None else time.monotonic
        self._lock = threading.RLock()
        self._closed = False
        self._warnings: list[str] = []
        self._session_preferences: dict[str, Any] = {}
        self._cue: tuple[str, float] | None = None
        self._interaction = InteractionState()
        self._session_policies: dict[str, str] = {}
        self._event_time_tolerance = 0.001
        # Only the system clocks have discoverable quantization. Injected
        # clocks keep their original sampling contract and serialization bound.
        sample_start = self._monotonic_clock() if wall_clock is None and monotonic_clock is None else None
        self._wall_start = self._wall_clock()
        self._mono_start = self._monotonic_clock()
        if not _finite(self._wall_start, 0, 1e12) or not _finite(self._mono_start, 0, 1e12):
            raise CompanionValidationError("companion_clock_invalid")
        if sample_start is not None and _finite(sample_start, 0, 1e12):
            wall_resolution = time.get_clock_info("time").resolution
            mono_resolution = time.get_clock_info("monotonic").resolution
            if (_finite(wall_resolution, 0, 1) and wall_resolution > 0
                    and _finite(mono_resolution, 0, 1) and mono_resolution > 0):
                bracket = self._mono_start - sample_start
                # The bracket bounds startup sampling delay; its own measured
                # duration can undercount by one monotonic tick. Windows 3.11
                # clocks can each quantize to 15.625ms, despite float seconds.
                uncertainty = wall_resolution + 2 * mono_resolution + bracket
                if 0 <= bracket <= wall_resolution + mono_resolution and uncertainty < 1:
                    self._event_time_tolerance = max(self._event_time_tolerance, uncertainty)
                # One second is this application's conservative calibration
                # limit. A pause or unusable resolution grants no extra future
                # allowance; unknown age cannot become fresh affect.
        self._initialize_storage(db_path)

    def _now(self) -> float:
        # Stable elapsed time throughout this process, immune to wall-clock edits.
        monotonic_now = self._monotonic_clock()
        if not _finite(monotonic_now, 0, 1e12):
            raise CompanionStorageError("companion_clock_invalid")
        now = self._wall_start + max(0.0, monotonic_now - self._mono_start)
        if not _finite(now, 0, 1e12):
            raise CompanionStorageError("companion_clock_invalid")
        return now

    def _warn(self, warning: str) -> None:
        if warning not in self._warnings:
            self._warnings = (self._warnings + [warning])[-8:]

    def _restore_clock(self, state: dict[str, Any]) -> None:
        db = self._db()
        latest = db.execute("SELECT MAX(accepted_utc) FROM budget").fetchone()[0]
        reference = max(state["last_seen_utc"], state["anchor_utc"], latest or 0.0)
        elapsed = self._wall_start - reference
        if elapsed < 0 or elapsed > MAX_OFFLINE_SECONDS:
            # Unknown ages cannot refill a window: retain every budget debit
            # for a full fresh window using the new process's monotonic time.
            db.execute("UPDATE budget SET accepted_utc=?", (self._wall_start,))
            self._neutralize(state, self._wall_start)
            if int(db.execute("PRAGMA user_version").fetchone()[0]) == STORAGE_SCHEMA_VERSION:
                self._neutralize_cloud(state, self._wall_start)
                config = load_interaction(db)
                for row in config["cloud_budget"]:
                    row[0] = self._wall_start
                save_interaction(db, config)
            state["preferences"].pop("today", None)
            state["revision"] += 1
            state["reset_epoch"] += 1
            state["last_seen_utc"] = self._wall_start
            self._save(state)
            self._warn("companion_clock_anomaly_budget_retained")
        else:
            db.execute("DELETE FROM budget WHERE accepted_utc<=?", (self._wall_start - 3600,))

    @staticmethod
    def _neutralize(state: dict[str, Any], now: float) -> None:
        state["values"] = list(baseline(state["persona"]["style"]))
        state["anchor_utc"] = now
        state["outcome_mode"] = "neutral"
        state["outcome_mode_until"] = 0.0

    @staticmethod
    def _values(state: dict[str, Any], now: float) -> list[float]:
        return values_at(state, now)

    def _mode(self, state: dict[str, Any], now: float) -> str:
        if not state["enabled"] or not state["emotions_enabled"]:
            return "neutral"
        if self._cue is not None and self._cue[1] > now:
            return self._cue[0]
        if state["outcome_mode_until"] > now:
            return str(state["outcome_mode"])
        return "neutral"

    def _preferences(self, state: dict[str, Any], now: float) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for scope in ("persistent", "today"):
            entry = state["preferences"].get(scope, {})
            if scope == "today" and entry.get("expires_at", 0) <= now:
                continue
            merged.update({k: v for k, v in entry.items() if k != "expires_at"})
        merged.update(self._session_preferences)
        return merged

    def _snapshot(self, state: dict[str, Any], now: float) -> dict[str, Any]:
        active = state["enabled"] and state["emotions_enabled"]
        values = self._values(state, now) if active else list(baseline(state["persona"]["style"]))
        mode = self._mode(state, now)
        scoped: dict[str, Any] = {}
        for scope, entry in state["preferences"].items():
            if scope != "today" or entry["expires_at"] > now:
                scoped[scope] = deepcopy(entry)
        if self._session_preferences:
            scoped["session"] = dict(self._session_preferences)
        cue = self._cue
        return {
            "schema_version": SCHEMA_VERSION,
            "reducer_version": REDUCER_VERSION,
            "revision": state["revision"],
            "reset_epoch": state["reset_epoch"],
            "enabled": state["enabled"],
            "emotions_enabled": state["emotions_enabled"],
            "persona": deepcopy(state["persona"]),
            "persona_versions": [row[0] for row in self._db().execute("SELECT version FROM persona_versions ORDER BY version DESC LIMIT ?", (PERSONA_HISTORY_LIMIT,))],
            "affect": {
                "valence": values[0], "arousal": values[1], "mood": values[2],
                "mode": mode,
                "expression": expression(max(-1.0, min(1.0, values[0] + 0.2 * values[2])), mode) if active else "neutral",
            },
            "preferences": scoped,
            "constraints": {"humor_allowed": self._preferences(state, now).get("humor_allowed", True)},
            "cue": {"mode": cue[0], "remaining_seconds": max(0, int(cue[1] - now))} if cue and cue[1] > now else None,
            "warnings": list(self._warnings),
            "limits": {"receipts": RECEIPT_LIMIT, "persona_versions": PERSONA_HISTORY_LIMIT,
                       "minute_events": MINUTE_COUNT_LIMIT, "hour_events": HOUR_COUNT_LIMIT},
            "timezone": self._timezone.key,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            try:
                return self._snapshot(self._load(), self._now())
            except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError):
                self._warn("companion_storage_unavailable")
                raise CompanionStorageError("companion_storage_unavailable") from None

    @staticmethod
    def _fields(parameters: dict[str, Any], allowed: set[str]) -> None:
        if set(parameters) - allowed:
            raise CompanionValidationError("companion_unknown_field")

    @staticmethod
    def _revision(parameters: dict[str, Any], state: dict[str, Any]) -> None:
        expected = parameters.get("expected_revision")
        if type(expected) is not int or expected < 0:
            raise CompanionValidationError("companion_expected_revision_required")
        if expected != state["revision"]:
            raise CompanionRevisionConflict("companion_revision_conflict")

    def command(self, action: str, **parameters: Any) -> dict[str, Any]:
        command_id = parameters.pop("command_id", None)
        fingerprint = ""
        if command_id is not None:
            if not isinstance(command_id, str) or not command_id.strip() or len(command_id.encode("utf-8")) > 256:
                raise CompanionValidationError("companion_command_id_invalid")
            try:
                canonical = json.dumps({"action": action, "parameters": parameters}, sort_keys=True,
                                       ensure_ascii=True, allow_nan=False, separators=(",", ":"))
                if len(canonical.encode("utf-8")) > 8192:
                    raise ValueError
                fingerprint = sha256(canonical.encode("utf-8")).hexdigest()
            except (ValueError, TypeError, OverflowError):
                raise CompanionValidationError("companion_command_invalid") from None
        with self._lock:
            db = self._db()
            previous_session = dict(self._session_preferences)
            previous_cue = self._cue
            previous_interaction = deepcopy(self._interaction)
            previous_policies = dict(self._session_policies)
            try:
                db.execute("BEGIN IMMEDIATE")
                if command_id is not None:
                    receipt = db.execute("SELECT fingerprint FROM command_receipts WHERE command_id=?", (command_id,)).fetchone()
                    if receipt is not None:
                        if receipt[0] != fingerprint:
                            raise CompanionValidationError("companion_command_id_conflict")
                        # Replay the acknowledgement without replaying config,
                        # CAS, cue TTL, candidate confirmation or a reset. Use a
                        # fresh read projection so old cues cannot reappear.
                        result = self._snapshot(self._load(), self._now())
                        db.execute("COMMIT")
                        return result
                    if db.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] >= RECEIPT_LIMIT:
                        raise CompanionValidationError("companion_command_capacity_reached")
                state = self._load()
                self._revision(parameters, state)
                now = self._now()
                self._apply_command(state, action, parameters, now)
                self._sync_identity_version(state)
                state["revision"] += 1
                state["last_seen_utc"] = now
                self._save(state)
                # Materialize the bounded response before commit so a query
                # failure cannot turn a committed command into an error reply.
                result = self._snapshot(state, now)
                if command_id is not None:
                    db.execute("INSERT INTO command_receipts VALUES (?, ?)", (command_id, fingerprint))
                db.execute("COMMIT")
                return result
            except Exception as error:
                self._session_preferences, self._cue = previous_session, previous_cue
                self._interaction, self._session_policies = previous_interaction, previous_policies
                self._rollback()
                if isinstance(error, (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError)) and not isinstance(error, CompanionError):
                    self._warn("companion_storage_unavailable")
                    raise CompanionStorageError("companion_storage_unavailable") from None
                raise

    def _apply_command(self, state: dict[str, Any], action: str, p: dict[str, Any], now: float) -> None:
        common = {"expected_revision"}
        if action in ("cloud_permissions", "identity", "confirm_candidate", "reject_candidate", "clear_cue", "clear_constraints"):
            self._interaction_command(state, action, p, now)
            return
        if action == "configure":
            self._fields(p, common | {"enabled", "emotions_enabled", "preset", "style"})
            for flag in ("enabled", "emotions_enabled"):
                if flag in p:
                    if type(p[flag]) is not bool:
                        raise CompanionValidationError("companion_boolean_required")
                    state[flag] = p[flag]
            changed = "preset" in p or "style" in p
            if "preset" in p:
                if not isinstance(p["preset"], str) or p["preset"] not in PRESETS:
                    raise CompanionValidationError("companion_preset_invalid")
                state["persona"]["preset"] = p["preset"]
                state["persona"]["style"] = dict(PRESETS[p["preset"]])
            if "style" in p:
                style_edits = p["style"]
                if not isinstance(style_edits, dict) or not set(style_edits) <= set(STYLE_KEYS):
                    raise CompanionValidationError("companion_style_invalid")
                if not all(_finite(value, 0, 1) for value in style_edits.values()):
                    raise CompanionValidationError("companion_style_invalid")
                state["persona"]["style"].update(style_edits)
            if changed:
                self._persona_version(state)
            # Config changes have no affect reward and cannot preserve a cue
            # from a previously enabled presentation generation.
            self._neutralize(state, now)
            self._cue = None
            if "preset" in p or "enabled" in p or "emotions_enabled" in p:
                self._neutralize_cloud(state, now)
            if p.get("enabled") is False or p.get("emotions_enabled") is False:
                self._interaction.cues.clear()
                self._interaction.candidates.clear()
                self._interaction.prepared.clear()
                self._interaction.epoch += 1
            state["reset_epoch"] += 1
        elif action == "rollback":
            self._fields(p, common | {"persona_version"})
            version = p.get("persona_version")
            if type(version) is not int or version < 1:
                raise CompanionValidationError("companion_persona_version_invalid")
            row = self._db().execute("SELECT payload FROM persona_versions WHERE version=?", (version,)).fetchone()
            if row is None:
                raise CompanionValidationError("companion_persona_version_unavailable")
            if not isinstance(row[0], str) or len(row[0]) > 2048:
                raise CompanionStorageError("companion_state_invalid")
            state["persona"] = json.loads(row[0])
            self._validate_state(state)
            config = load_interaction(self._db())
            old_identity = config["identity_versions"].get(str(version), {"identity": dict(IDENTITY_DEFAULTS), "use_policy": "local_only"})
            config["identity"] = deepcopy(old_identity["identity"])
            config["identity_use_policy"] = old_identity["use_policy"]
            save_interaction(self._db(), config)
            self._persona_version(state)
            self._neutralize(state, now)
            self._cue = None
            self._neutralize_cloud(state, now)
            state["reset_epoch"] += 1
        elif action == "reset":
            self._fields(p, common | {"target"})
            target = p.get("target", "affect")
            if target not in ("affect", "persona", "all"):
                raise CompanionValidationError("companion_reset_target_invalid")
            if target in ("persona", "all"):
                state["persona"] = {"version": 1, "preset": "balanced", "style": dict(PRESETS["balanced"])}
                self._persona_version(state)
            if target == "all":
                state["preferences"] = {}
                self._session_preferences.clear()
                old_budget = load_interaction(self._db())["cloud_budget"]
                config = initial_interaction()
                config["cloud_budget"] = old_budget
                save_interaction(self._db(), config)
            elif target == "persona":
                config = load_interaction(self._db())
                config["identity"] = dict(IDENTITY_DEFAULTS)
                config["identity_use_policy"] = "local_only"
                save_interaction(self._db(), config)
            self._neutralize(state, now)
            self._cue = None
            if target == "all":
                self._interaction.clear()
                self._session_policies.clear()
            else:
                self._interaction.cues.clear()
                self._interaction.candidates.clear()
                self._interaction.prepared.clear()
                self._interaction.epoch += 1
            self._neutralize_cloud(state, now)
            state["reset_epoch"] += 1
        elif action == "preference":
            self._fields(p, common | {"scope", "verbosity", "humor_allowed", "followup_allowed", "conversation_id", "use_policy"})
            scope = p.get("scope")
            if scope not in ("session", "today", "persistent"):
                raise CompanionValidationError("companion_scope_invalid")
            preference_edits: dict[str, Any] = {}
            if "verbosity" in p:
                if p["verbosity"] not in ("brief", "normal", "detailed"):
                    raise CompanionValidationError("companion_verbosity_invalid")
                preference_edits["verbosity"] = p["verbosity"]
            if "humor_allowed" in p:
                if type(p["humor_allowed"]) is not bool:
                    raise CompanionValidationError("companion_boolean_required")
                preference_edits["humor_allowed"] = p["humor_allowed"]
            if "followup_allowed" in p:
                if type(p["followup_allowed"]) is not bool:
                    raise CompanionValidationError("companion_boolean_required")
                preference_edits["followup_allowed"] = p["followup_allowed"]
            if not preference_edits:
                raise CompanionValidationError("companion_preference_required")
            policy = p.get("use_policy", "cloud_allowed")
            conversation_id = p.get("conversation_id", "default")
            self._interaction_identity(conversation_id, policy)
            config = load_interaction(self._db())
            for key, value in preference_edits.items():
                self._set_interaction_preference(state, config, key, value, scope, conversation_id,
                                                 policy, "explicit_configuration", now)
            save_interaction(self._db(), config)
            # Original snapshot has only its original preference vocabulary.
            preference_edits = {k: v for k, v in preference_edits.items() if k in ("verbosity", "humor_allowed")}
            if scope == "session":
                if conversation_id == "default":
                    self._session_preferences.update(preference_edits)
                    self._session_policies.update(dict.fromkeys(preference_edits, policy))
            else:
                old = state["preferences"].get(scope, {})
                if scope == "today" and old.get("expires_at", 0) <= now:
                    old = {}
                entry = {**old, **preference_edits}
                if scope == "today":
                    local = datetime.fromtimestamp(now, self._timezone)
                    tomorrow = local.date() + timedelta(days=1)
                    midnight = datetime.combine(tomorrow, datetime.min.time(), tzinfo=self._timezone)
                    entry["expires_at"] = min(now + 86400, midnight.timestamp())
                state["preferences"][scope] = entry
        elif action == "cue":
            self._fields(p, common | {"mode", "ttl_seconds", "conversation_id", "use_policy"})
            mode = p.get("mode")
            ttl = p.get("ttl_seconds", 1800)
            conversation_id, policy = p.get("conversation_id", "default"), p.get("use_policy", "cloud_allowed")
            self._interaction_identity(conversation_id, policy)
            if mode not in ("neutral", "supportive", "focused"):
                raise CompanionValidationError("companion_cue_invalid")
            if type(ttl) is not int or not 1 <= ttl <= 1800:
                raise CompanionValidationError("companion_cue_ttl_invalid")
            if mode != "neutral" and conversation_id not in self._interaction.cues and len(self._interaction.cues) >= 4:
                raise CompanionValidationError("companion_cue_capacity_reached")
            if conversation_id == "default":
                self._cue = None if mode == "neutral" else (mode, now + ttl)
            self._interaction.cues.pop(conversation_id, None)
            self._interaction.epoch += 1
            self._interaction.prepared.clear()
            self._interaction.candidates.clear()
            if mode != "neutral":
                self._interaction.cues[conversation_id] = {"mode": mode, "evidence_kind": "explicit_configuration",
                    "source_ref": "explicit_configuration", "span": None, "use_policy": policy,
                    "expires_at": now + ttl, "generation": self._interaction.epoch}
        else:
            raise CompanionValidationError("companion_action_unknown")

    def record_outcome(self, task_id: str, status: str, *, tool_success: bool | None = None,
                       use_policy: str = "cloud_allowed", occurred_at: float | None = None) -> None:
        """Consume a root receipt once, optionally at its reliable terminal time.

        Delayed delivery does not restart decay or focused-mode TTL. Explicit
        stale, materially future or pre-reset events receive history only.
        """
        if not isinstance(task_id, str) or not task_id or len(task_id.encode("utf-8")) > 256:
            raise CompanionValidationError("companion_task_id_invalid")
        if not isinstance(status, str) or len(status) > 32:
            raise CompanionValidationError("companion_status_invalid")
        if tool_success is not None and type(tool_success) is not bool:
            raise CompanionValidationError("companion_boolean_required")
        if use_policy not in ("local_only", "cloud_allowed"):
            raise CompanionValidationError("companion_use_policy_invalid")
        if occurred_at is not None and not _finite(occurred_at, 0, 1e12):
            raise CompanionValidationError("companion_event_time_invalid")
        status = status.casefold()
        with self._lock:
            db = self._db()
            # Waiting, review, uncertain and cancellation cannot penalize the
            # user or consume the terminal completion identity prematurely.
            if status not in ("completed", "failed"):
                return
            try:
                db.execute("BEGIN IMMEDIATE")
                state = self._load()
                if db.execute("SELECT 1 FROM receipts WHERE task_id=?", (task_id,)).fetchone():
                    db.execute("COMMIT")
                    return
                count = db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
                if count >= RECEIPT_LIMIT:
                    self._warn("companion_receipt_capacity_effects_paused")
                    db.execute("COMMIT")
                    return
                delivery_now = self._now()
                now = delivery_now if occurred_at is None else min(float(occurred_at), delivery_now)
                time_reason: str | None = None
                if occurred_at is not None:
                    # Compare independent wall timestamps with the stable
                    # process clock only within its bounded calibration error.
                    if occurred_at > delivery_now + self._event_time_tolerance:
                        time_reason = "event_time_in_future"
                    elif delivery_now - now > 60:
                        time_reason = "event_too_old"
                predates_anchor = occurred_at is not None and now < state["anchor_utc"]
                can_project = (time_reason is None and not predates_anchor and state["enabled"]
                               and state["emotions_enabled"] and ((status == "completed" and tool_success is True)
                               or (status == "failed" and tool_success is False)))
                if occurred_at is None or can_project:
                    db.execute("DELETE FROM budget WHERE accepted_utc<=?", (now - 3600,))
                rows = self._budget_rows()
                minute = [r for r in rows if r[0] > now - 60]
                reason = "eligible"
                delta = [0.0, 0.0, 0.0]
                if time_reason is not None:
                    reason = time_reason
                elif predates_anchor:
                    reason = "event_precedes_current_anchor"
                elif not state["enabled"] or not state["emotions_enabled"]:
                    reason = "disabled"
                elif tool_success is None:
                    reason = "no_trusted_tool_outcome"
                elif status == "completed" and tool_success is False:
                    reason = "completion_not_reliably_successful"
                elif status == "failed" and tool_success is not False:
                    reason = "failure_not_classified_as_tool_failure"
                elif len(minute) >= MINUTE_COUNT_LIMIT or len(rows) >= HOUR_COUNT_LIMIT:
                    reason = "event_window_capacity"
                else:
                    delta = outcome_delta(status, rows, now)
                    values = self._values(state, now)
                    state["values"] = [max(low, min(high, value + step)) for value, step, low, high in zip(values, delta, (-1, 0, -0.25), (1, 1, 0.25))]
                    state["anchor_utc"] = now
                    if status == "failed":
                        state["outcome_mode"] = "focused"
                        state["outcome_mode_until"] = now + 60
                cursor = db.execute(
                    "INSERT INTO receipts(task_id,status,accepted_utc,epoch,eligibility,delta_v,delta_a,delta_m) VALUES (?,?,?,?,?,?,?,?)",
                    (task_id, status, now, state["reset_epoch"], reason, *delta),
                )
                if reason == "eligible":
                    db.execute("INSERT INTO budget VALUES (?,?,?,?,?)", (cursor.lastrowid, now, *delta))
                if (time_reason is None and use_policy == "cloud_allowed" and state["enabled"] and state["emotions_enabled"]
                        and ((status == "completed" and tool_success is True) or (status == "failed" and tool_success is False))):
                    config = load_interaction(db)
                    self._project_cloud_outcome(state, config, status, now)
                    save_interaction(db, config)
                state["revision"] += 1
                state["last_seen_utc"] = delivery_now
                self._save(state)
                db.execute("COMMIT")
            except Exception as error:
                self._rollback()
                if isinstance(error, (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError)) and not isinstance(error, CompanionError):
                    self._warn("companion_storage_unavailable")
                    raise CompanionStorageError("companion_storage_unavailable") from None
                raise

    def reset_for_privacy(self) -> None:
        """Clear local derived expression and session data; retain dedup/budget.

        Persistent and today preferences are explicit settings, not derived
        evidence; only ``reset(target='all')`` removes those settings.
        """
        with self._lock:
            self.invalidate_interaction()
            db = self._db()
            try:
                db.execute("BEGIN IMMEDIATE")
                state = self._load()
                now = self._now()
                self._neutralize(state, now)
                self._neutralize_cloud(state, now)
                state["reset_epoch"] += 1
                state["revision"] += 1
                state["last_seen_utc"] = now
                self._save(state)
                db.execute("COMMIT")
            except Exception as error:
                self._rollback()
                if isinstance(error, (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError)) and not isinstance(error, CompanionError):
                    self._warn("companion_storage_unavailable")
                    raise CompanionStorageError("companion_storage_unavailable") from None
                raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._session_preferences.clear()
            self._cue = None
            self._interaction.clear()
            self._session_policies.clear()
            connection, self._connection = self._connection, None
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    raise CompanionStorageError("companion_close_failed") from None
