"""Interaction storage extension and recoverable schema migration.

The existing affect payload remains schema 1. SQLite schema 2 adds an isolated
interaction settings row; receipts, budget and persona versions are untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from .models import CLOUD_CATEGORIES, IDENTITY_DEFAULTS, preference_valid
from .constants import (
    HOUR_COUNT_LIMIT, PERSONA_HISTORY_LIMIT, RECEIPT_LIMIT, REDUCER_VERSION,
    SCHEMA_VERSION, finite as _finite,
)
from .errors import CompanionClosedError, CompanionStorageError
from .policy import PRESETS, STYLE_KEYS, baseline

STORAGE_SCHEMA_VERSION = 2


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise ValueError("companion_interaction_invalid")
        result[key] = item
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError("companion_interaction_invalid")


def initial_interaction() -> dict[str, Any]:
    return {"cloud_permissions": dict.fromkeys(CLOUD_CATEGORIES, False),
            "identity": dict(IDENTITY_DEFAULTS), "identity_use_policy": "local_only",
            "preference_policies": {}, "preference_sources": {}, "preferences": {}, "identity_versions": {},
            "cloud_affect": {"values": [0.02, 0.168, 0.0], "anchor_utc": 0.0,
                             "mode": "neutral", "mode_until": 0.0}, "cloud_budget": []}


def validate_identity(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != set(IDENTITY_DEFAULTS):
        return False
    name = value["display_name"]
    return (isinstance(name, str) and len(name) <= 40
            and not any(ord(char) < 32 for char in name)
            and value["role_kind"] in ("assistant", "companion")
            and value["self_reference"] in ("neutral", "name", "first_person")
            and value["greeting_style"] in ("none", "brief")
            and value["followup_style"] in ("none", "optional_one"))


def validate_interaction(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != set(initial_interaction()):
        raise ValueError("companion_interaction_invalid")
    permissions = value["cloud_permissions"]
    if not isinstance(permissions, dict) or set(permissions) != set(CLOUD_CATEGORIES) or any(type(flag) is not bool for flag in permissions.values()):
        raise ValueError("companion_interaction_invalid")
    if not validate_identity(value["identity"]) or value["identity_use_policy"] not in ("local_only", "cloud_allowed"):
        raise ValueError("companion_interaction_invalid")
    history = value["identity_versions"]
    if not isinstance(history, dict) or len(history) > 32:
        raise ValueError("companion_interaction_invalid")
    for version, item in history.items():
        if not isinstance(version, str) or not version.isdecimal() or len(version) > 20 or int(version) < 1 or not isinstance(item, dict) or set(item) != {"identity", "use_policy"} or not validate_identity(item["identity"]) or item["use_policy"] not in ("local_only", "cloud_allowed"):
            raise ValueError("companion_interaction_invalid")
    affect = value["cloud_affect"]
    if not isinstance(affect, dict) or set(affect) != {"values", "anchor_utc", "mode", "mode_until"}:
        raise ValueError("companion_interaction_invalid")
    if not isinstance(affect["values"], list) or len(affect["values"]) != 3 or any(type(item) not in (int, float) or not low <= item <= high for item, low, high in zip(affect["values"], (-1, 0, -0.25), (1, 1, 0.25))):
        raise ValueError("companion_interaction_invalid")
    if affect["mode"] not in ("neutral", "focused") or any(type(affect[field]) not in (int, float) or not 0 <= affect[field] <= 1e12 for field in ("anchor_utc", "mode_until")):
        raise ValueError("companion_interaction_invalid")
    budget = value["cloud_budget"]
    if not isinstance(budget, list) or len(budget) > 256:
        raise ValueError("companion_interaction_invalid")
    for row in budget:
        if not isinstance(row, list) or len(row) != 4 or any(type(item) not in (float, int) or not low <= item <= high for item, low, high in zip(row, (0, -0.15, 0, -0.05), (1e12, 0.15, 0.15, 0.05))):
            raise ValueError("companion_interaction_invalid")
    policies = value["preference_policies"]
    if not isinstance(policies, dict) or not set(policies) <= {"persistent", "today"}:
        raise ValueError("companion_interaction_invalid")
    for scope, entries in policies.items():
        if not isinstance(entries, dict) or not set(entries) <= {"verbosity", "humor_allowed", "followup_allowed"} or any(policy not in ("local_only", "cloud_allowed") for policy in entries.values()):
            raise ValueError("companion_interaction_invalid")
    sources = value["preference_sources"]
    if not isinstance(sources, dict) or not set(sources) <= {"today", "persistent"}:
        raise ValueError("companion_interaction_invalid")
    for entries in sources.values():
        if not isinstance(entries, dict) or not set(entries) <= {"verbosity", "humor_allowed", "followup_allowed"}:
            raise ValueError("companion_interaction_invalid")
        for source in entries.values():
            if not isinstance(source, dict) or set(source) != {"source_ref", "span", "use_policy", "revision"} or not isinstance(source["source_ref"], str) or len(source["source_ref"].encode("utf-8")) > 256 or source["use_policy"] not in ("local_only", "cloud_allowed") or type(source["revision"]) is not int or source["revision"] < 0:
                raise ValueError("companion_interaction_invalid")
            span = source["span"]
            if span is not None and (not isinstance(span, list) or len(span) != 2 or any(type(offset) is not int for offset in span) or not 0 <= span[0] < span[1] <= 65536):
                raise ValueError("companion_interaction_invalid")
    prefs = value["preferences"]
    if not isinstance(prefs, dict) or not set(prefs) <= {"persistent", "today"}:
        raise ValueError("companion_interaction_invalid")
    for scope, entries in prefs.items():
        if not isinstance(entries, dict) or not set(entries) <= {"followup_allowed", "expires_at"}:
            raise ValueError("companion_interaction_invalid")
        if "followup_allowed" in entries and not preference_valid("followup_allowed", entries["followup_allowed"]):
            raise ValueError("companion_interaction_invalid")
        if scope == "today" and (type(entries.get("expires_at")) not in (float, int) or not 0 <= entries["expires_at"] <= 1e12):
            raise ValueError("companion_interaction_invalid")
        if scope == "persistent" and "expires_at" in entries:
            raise ValueError("companion_interaction_invalid")


def backup_schema_one(connection: sqlite3.Connection, path: Path | str | None) -> None:
    if path is not None:
        backup = Path(str(path) + ".schema1." + uuid4().hex + ".bak")
        with sqlite3.connect(str(backup)) as destination:
            connection.backup(destination)


def migrate_interaction(connection: sqlite3.Connection, path: Path | str | None, *,
                        newly_created: bool, backup_done: bool = False) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version == STORAGE_SCHEMA_VERSION:
        load_interaction(connection)
        count = connection.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0]
        if count > RECEIPT_LIMIT:
            raise ValueError("companion_command_capacity_invalid")
        return
    if version != 1:
        raise ValueError("companion_schema_unsupported")
    if path is not None and not newly_created and not backup_done:
        backup_schema_one(connection, path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("CREATE TABLE interaction_settings (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
        connection.execute("CREATE TABLE command_receipts (command_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)")
        save_interaction(connection, initial_interaction(), insert=True)
        connection.execute(f"PRAGMA user_version={STORAGE_SCHEMA_VERSION}")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def load_interaction(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute("SELECT payload FROM interaction_settings WHERE id=1").fetchone()
    if row is None or not isinstance(row[0], str) or len(row[0].encode("utf-8")) > 65536:
        raise ValueError("companion_interaction_invalid")
    value: dict[str, Any] = json.loads(row[0], object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    validate_interaction(value)
    return value


def save_interaction(connection: sqlite3.Connection, value: dict[str, Any], *, insert: bool = False) -> None:
    validate_interaction(value)
    payload = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > 65536:
        raise ValueError("companion_interaction_invalid")
    connection.execute("INSERT INTO interaction_settings VALUES(1, ?)" if insert else "UPDATE interaction_settings SET payload=? WHERE id=1", (payload,))



def _initial(now: float) -> dict[str, Any]:
    style = dict(PRESETS["balanced"])
    return {
        "schema_version": SCHEMA_VERSION,
        "reducer_version": REDUCER_VERSION,
        "revision": 0,
        "reset_epoch": 0,
        "enabled": False,
        "emotions_enabled": False,
        "persona": {"version": 1, "preset": "balanced", "style": style},
        "anchor_utc": now,
        "last_seen_utc": now,
        "values": list(baseline(style)),
        "preferences": {},
        "outcome_mode": "neutral",
        "outcome_mode_until": 0.0,
    }


class StateRepository:
    """SQLite ownership and invariant validation shared by the runtime facade."""

    _connection: sqlite3.Connection | None
    _closed: bool
    _wall_start: float

    def _restore_clock(self, state: dict[str, Any]) -> None:
        raise NotImplementedError

    def _initialize_storage(self, db_path: Path | str | None) -> None:
        self._connection: sqlite3.Connection | None = None
        try:
            if db_path is not None:
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                ":memory:" if db_path is None else str(db_path),
                timeout=5.0,
                check_same_thread=False,
                isolation_level=None,
            )
            self._connection = connection
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in (0, SCHEMA_VERSION, STORAGE_SCHEMA_VERSION):
                raise CompanionStorageError("companion_schema_unsupported")
            if version == 1:
                backup_schema_one(connection, db_path)
            connection.execute("BEGIN IMMEDIATE")
            if version == 0:
                # Refuse an unrelated SQLite file rather than adding tables.
                tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                if tables:
                    raise CompanionStorageError("companion_schema_unsupported")
                connection.execute(
                    "CREATE TABLE state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE persona_versions (version INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE receipts (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "task_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, "
                    "accepted_utc REAL NOT NULL, epoch INTEGER NOT NULL, "
                    "eligibility TEXT NOT NULL, delta_v REAL NOT NULL, "
                    "delta_a REAL NOT NULL, delta_m REAL NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE budget (sequence INTEGER PRIMARY KEY, accepted_utc REAL NOT NULL, "
                    "delta_v REAL NOT NULL, delta_a REAL NOT NULL, delta_m REAL NOT NULL)"
                )
                state = _initial(self._wall_start)
                connection.execute(
                    "INSERT INTO state VALUES (1, ?)", (self._encode(state),)
                )
                connection.execute(
                    "INSERT INTO persona_versions VALUES (1, ?)",
                    (self._encode(state["persona"]),),
                )
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self._validate_history(state)
            else:
                state = self._load()
                if version == STORAGE_SCHEMA_VERSION:
                    migrate_interaction(connection, db_path, newly_created=False)
                for table, limit in (("receipts", RECEIPT_LIMIT), ("budget", HOUR_COUNT_LIMIT), ("persona_versions", PERSONA_HISTORY_LIMIT)):
                    count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    if count > limit:
                        raise CompanionStorageError("companion_capacity_invalid")
                if connection.execute("SELECT COUNT(*) FROM persona_versions").fetchone()[0] == 0:
                    raise CompanionStorageError("companion_state_invalid")
                self._validate_history(state)
                self._budget_rows()
                self._restore_clock(state)
            connection.execute("COMMIT")
            if version != STORAGE_SCHEMA_VERSION:
                migrate_interaction(connection, db_path, newly_created=version == 0, backup_done=version == 1)
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError, CompanionStorageError):
            if self._connection is not None:
                try:
                    self._connection.close()
                except sqlite3.Error:
                    pass
            self._connection = None
            raise CompanionStorageError("companion_storage_unavailable") from None

    @staticmethod
    def _encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)

    def _db(self) -> sqlite3.Connection:
        if self._closed or self._connection is None:
            raise CompanionClosedError("companion_closed")
        return self._connection

    def _load(self) -> dict[str, Any]:
        row = self._db().execute("SELECT payload FROM state WHERE id=1").fetchone()
        if row is None or len(row[0]) > 16384:
            raise CompanionStorageError("companion_state_invalid")
        try:
            state: dict[str, Any] = json.loads(row[0])
            self._validate_state(state)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise CompanionStorageError("companion_state_invalid") from None
        return state

    @staticmethod
    def _validate_state(state: dict[str, Any]) -> None:
        keys = {"schema_version", "reducer_version", "revision", "reset_epoch", "enabled", "emotions_enabled", "persona", "anchor_utc", "last_seen_utc", "values", "preferences", "outcome_mode", "outcome_mode_until"}
        if not isinstance(state, dict) or set(state) != keys:
            raise ValueError
        if type(state["schema_version"]) is not int or state["schema_version"] != SCHEMA_VERSION:
            raise ValueError
        if type(state["reducer_version"]) is not int or state["reducer_version"] != REDUCER_VERSION:
            raise ValueError
        for field in ("revision", "reset_epoch"):
            if type(state[field]) is not int or state[field] < 0:
                raise ValueError
        for field in ("enabled", "emotions_enabled"):
            if type(state[field]) is not bool:
                raise ValueError
        persona = state["persona"]
        if not isinstance(persona, dict) or set(persona) != {"version", "preset", "style"}:
            raise ValueError
        if persona["preset"] not in PRESETS or type(persona["version"]) is not int or persona["version"] < 1:
            raise ValueError
        if not isinstance(persona["style"], dict) or set(persona["style"]) != set(STYLE_KEYS):
            raise ValueError
        if not all(_finite(v, 0, 1) for v in persona["style"].values()):
            raise ValueError
        values = state["values"]
        if not isinstance(values, list) or len(values) != 3:
            raise ValueError
        if not all(_finite(v, low, high) for v, low, high in zip(values, (-1, 0, -0.25), (1, 1, 0.25))):
            raise ValueError
        for field in ("anchor_utc", "last_seen_utc", "outcome_mode_until"):
            if not _finite(state[field], 0, 1e12):
                raise ValueError
        if state["last_seen_utc"] < state["anchor_utc"]:
            raise ValueError
        if state["outcome_mode"] not in ("neutral", "focused"):
            raise ValueError
        prefs = state["preferences"]
        if not isinstance(prefs, dict) or not set(prefs) <= {"today", "persistent"}:
            raise ValueError
        for scope, entry in prefs.items():
            if not isinstance(entry, dict) or not set(entry) <= {"verbosity", "humor_allowed", "expires_at"}:
                raise ValueError
            if "verbosity" in entry and entry["verbosity"] not in ("brief", "normal", "detailed"):
                raise ValueError
            if "humor_allowed" in entry and type(entry["humor_allowed"]) is not bool:
                raise ValueError
            if scope == "today" and not _finite(entry.get("expires_at"), 0, 1e12):
                raise ValueError
            if scope == "persistent" and "expires_at" in entry:
                raise ValueError

    def _validate_history(self, state: dict[str, Any]) -> None:
        rows = self._db().execute(
            "SELECT version, payload FROM persona_versions ORDER BY version DESC LIMIT ?",
            (PERSONA_HISTORY_LIMIT + 1,),
        ).fetchall()
        if not 1 <= len(rows) <= PERSONA_HISTORY_LIMIT:
            raise CompanionStorageError("companion_state_invalid")
        found_current = False
        for version, payload in rows:
            if type(version) is not int or version < 1 or not isinstance(payload, str) or len(payload) > 2048:
                raise CompanionStorageError("companion_state_invalid")
            persona = json.loads(payload)
            probe = {**state, "persona": persona}
            self._validate_state(probe)
            if persona["version"] != version:
                raise CompanionStorageError("companion_state_invalid")
            if version == state["persona"]["version"]:
                if persona != state["persona"]:
                    raise CompanionStorageError("companion_state_invalid")
                found_current = True
        if not found_current or rows[0][0] != state["persona"]["version"]:
            raise CompanionStorageError("companion_state_invalid")

    def _budget_rows(self) -> list[tuple[Any, ...]]:
        stored = self._db().execute(
            "SELECT b.accepted_utc, b.delta_v, b.delta_a, b.delta_m, b.sequence, "
            "r.delta_v, r.delta_a, r.delta_m, r.eligibility FROM budget b "
            "LEFT JOIN receipts r ON r.sequence=b.sequence LIMIT ?",
            (HOUR_COUNT_LIMIT + 1,),
        ).fetchall()
        if len(stored) > HOUR_COUNT_LIMIT:
            raise CompanionStorageError("companion_capacity_invalid")
        rows: list[tuple[Any, ...]] = []
        for row in stored:
            accepted, delta_v, delta_a, delta_m, sequence = row[:5]
            if type(sequence) is not int or sequence < 1:
                raise CompanionStorageError("companion_budget_invalid")
            if not (_finite(accepted, 0, 1e12) and _finite(delta_v, -0.15, 0.15)
                    and _finite(delta_a, 0, 0.15) and _finite(delta_m, -0.05, 0.05)):
                raise CompanionStorageError("companion_budget_invalid")
            if tuple(row[1:4]) != tuple(row[5:8]) or row[8] != "eligible":
                raise CompanionStorageError("companion_budget_invalid")
            rows.append(tuple(row[:5]))
        return rows

    def _save(self, state: dict[str, Any]) -> None:
        self._db().execute("UPDATE state SET payload=? WHERE id=1", (self._encode(state),))

    def _persona_version(self, state: dict[str, Any]) -> None:
        db = self._db()
        latest = db.execute("SELECT MAX(version) FROM persona_versions").fetchone()[0]
        state["persona"]["version"] = int(latest or 0) + 1
        db.execute("INSERT INTO persona_versions VALUES (?, ?)",
                   (state["persona"]["version"], self._encode(state["persona"])))
        db.execute("DELETE FROM persona_versions WHERE version NOT IN "
                   "(SELECT version FROM persona_versions ORDER BY version DESC LIMIT ?)",
                   (PERSONA_HISTORY_LIMIT,))

    def _rollback(self) -> None:
        try:
            self._db().execute("ROLLBACK")
        except sqlite3.Error:
            pass

