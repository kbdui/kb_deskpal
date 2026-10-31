"""角色的唯一状态存储；旧结构只在一次性导入时读取。"""

from contextlib import contextmanager
from collections.abc import Iterator, Callable
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from .errors import CompanionStorageError

from .schema import VERSION, SCHEMA, RECEIPT_SCHEMA, INDEXES, validate as validate_schema

STORAGE_SCHEMA_VERSION = VERSION
DEFAULT_CARD = "你是 Deskpal，与用户长期相处的聊天伙伴。尊重用户的选择，诚实说明已知、未知与实际完成的事情。"


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


def decode(text: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate role field")
            result[key] = value
        return result
    def reject(value: str) -> Any:
        raise ValueError("nonfinite role field")
    result = json.loads(text, object_pairs_hook=unique, parse_constant=reject)
    if not isinstance(result, dict):
        raise ValueError("invalid role state")
    return result


def initial() -> dict[str, Any]:
    return {"revision": 1, "reset_epoch": 0,
            "card": {"id": uuid4().hex, "revision": 1, "text": DEFAULT_CARD,
                     "use_policy": "cloud_allowed", "origin": "default"},
            "habits": [], "emotion": None, "user_understanding": [], "interaction_approach": []}


def _legacy(connection: sqlite3.Connection, version: int) -> dict[str, Any]:
    # 仅转换实际存在的设定；不用旧数值编造背景或情绪。
    from .legacy import validate_state, validate_interaction
    row = connection.execute("SELECT payload FROM state WHERE id=1").fetchone()
    if row is None or not isinstance(row[0], str) or len(row[0].encode("utf-8")) > 16384:
        raise ValueError("missing legacy character")
    old = decode(row[0])
    validate_state(old)
    settings: dict[str, Any] = {}
    if version == 2:
        config = connection.execute("SELECT payload FROM interaction_settings WHERE id=1").fetchone()
        if config is None or not isinstance(config[0], str) or len(config[0].encode("utf-8")) > 65536:
            raise ValueError("missing legacy settings")
        settings = decode(config[0])
        validate_interaction(settings)
    state = initial()
    identity = settings.get("identity", {})
    from .legacy import IDENTITY_DEFAULTS, PRESETS
    custom = (old["persona"]["preset"] != "balanced" or old["persona"]["style"] != PRESETS["balanced"]
              or bool(identity) and identity != IDENTITY_DEFAULTS)
    if custom or old["preferences"] or settings.get("preferences"):
        imported = {"原身份设定": identity, "原人格预设": old["persona"]["preset"],
                    "原表达参数（仅供理解，不作为人格轴）": old["persona"]["style"],
                    "原明确要求": old["preferences"], "原补充要求": settings.get("preferences", {})}
        state["card"].update(text="从旧备份导入的可编辑设定，未补充背景：\n" + encode(imported),
                             origin="legacy_import", use_policy="local_only")
    return state


class RoleRepository:
    def __init__(self, path: Path | str | None, *, validator: Callable[[dict[str, Any]], None] | None = None) -> None:
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._validator = validator
        self.connection = sqlite3.connect(":memory:" if path is None else str(path),
                                         timeout=5, isolation_level=None, check_same_thread=False)
        try:
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            if self.connection.execute("PRAGMA quick_check(1)").fetchall() != [("ok",)]:
                raise ValueError("invalid character database")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, VERSION):
                raise ValueError("unsupported character database")
            with self.transaction():
                if version != VERSION:
                    tables = {r[0] for r in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if version == 0 and tables:
                        raise ValueError("unrelated database")
                    if version == 3:
                        row = self.connection.execute("SELECT payload FROM role_state WHERE id=1").fetchone()
                        if row is None or len(row[0].encode("utf-8")) > 262144:
                            raise ValueError("invalid legacy role aggregate")
                        state = decode(row[0])
                    else:
                        state = initial() if version == 0 else _legacy(self.connection, version)
                    self._validate_state(state)
                    for statement in SCHEMA:
                        self.connection.execute(statement)
                    if version != 3:
                        self.connection.execute(RECEIPT_SCHEMA)
                        if version == 2:
                            self.connection.execute("INSERT INTO role_receipt SELECT command_id,fingerprint FROM command_receipts")
                    self.connection.execute("INSERT INTO role_meta VALUES(1,?,?)", (state["revision"], state["reset_epoch"]))
                    self.save(state)
                    for statement in INDEXES:
                        self.connection.execute(statement)
                    # 删除旧存储副本，历史回执仍保留；没有并行双写。
                    for name in ("state", "persona_versions", "receipts", "budget", "interaction_settings", "command_receipts", "role_state"):
                        self.connection.execute(f"DROP TABLE IF EXISTS {name}")
                    validate_schema(self.connection)
                    self.load()
                    self.connection.execute(f"PRAGMA user_version={VERSION}")
                else:
                    validate_schema(self.connection)
                    self.load()
        except Exception:
            self.connection.close()
            raise CompanionStorageError("companion_storage_unavailable") from None
        except BaseException:
            self.connection.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def _validate_state(self, state: dict[str, Any]) -> None:
        if (set(state) != set(initial()) or type(state["revision"]) is not int or state["revision"] < 1
                or type(state["reset_epoch"]) is not int or state["reset_epoch"] < 0
                or not isinstance(state["card"], dict)
                or any(not isinstance(state[key], list) or len(state[key]) > 64
                       for key in ("habits", "user_understanding", "interaction_approach"))
                or len(encode(state).encode("utf-8")) > 262144):
            raise CompanionStorageError("companion_state_invalid")
        if self._validator is not None:
            self._validator(state)
        identities = [record["id"] for layer in ("habits", "user_understanding", "interaction_approach")
                      for record in state[layer]]
        if len(identities) != len(set(identities)):
            raise CompanionStorageError("companion_record_identity_conflict")

    def load(self) -> dict[str, Any]:
        db = self.connection
        meta = db.execute("SELECT revision,reset_epoch FROM role_meta WHERE id=1").fetchone()
        card_row = db.execute("SELECT id,revision,text,use_policy,origin FROM character_card WHERE slot=1").fetchone()
        if meta is None or card_row is None:
            raise CompanionStorageError("companion_state_invalid")
        state: dict[str, Any] = {"revision": meta[0], "reset_epoch": meta[1],
                 "card": dict(zip(("id", "revision", "text", "use_policy", "origin"), card_row)),
                 "habits": [], "emotion": None, "user_understanding": [], "interaction_approach": []}
        fields = ("id", "content", "reason", "nature", "scope", "created_at", "expires_at", "use_policy", "source", "extras")
        for row in db.execute("SELECT layer,id,content,reason,nature,scope,created_at,expires_at,use_policy,source,extras FROM role_record ORDER BY layer,sort_order,id LIMIT 193"):
            record = dict(zip(fields, row[1:]))
            extras = decode(record.pop("extras"))
            record["source"] = decode(record["source"])
            if set(extras) & set(record):
                raise ValueError("Conflicting role metadata")
            record.update(extras)
            state[row[0]].append(record)
        emotion = db.execute("SELECT description_intensity,reason_sources,time_scope,uncertainty,source,expires_at,use_policy FROM current_emotion WHERE slot=1").fetchone()
        if emotion:
            state["emotion"] = dict(zip(("description_intensity", "reason_sources", "time_scope", "uncertainty", "source", "expires_at", "use_policy"), emotion))
            state["emotion"]["source"] = decode(state["emotion"]["source"])
        self._validate_state(state)
        return state

    def save(self, state: dict[str, Any]) -> None:
        self._validate_state(state)
        db = self.connection
        db.execute("UPDATE role_meta SET revision=?,reset_epoch=? WHERE id=1", (state["revision"], state["reset_epoch"]))
        card = state["card"]
        db.execute("INSERT INTO character_card VALUES(1,?,?,?,?,?) ON CONFLICT(slot) DO UPDATE SET id=excluded.id,revision=excluded.revision,text=excluded.text,use_policy=excluded.use_policy,origin=excluded.origin",
                   (card["id"], card["revision"], card["text"], card["use_policy"], card["origin"]))
        ids = []
        known = {"id", "content", "reason", "nature", "scope", "created_at", "expires_at", "use_policy", "source"}
        for layer in ("habits", "user_understanding", "interaction_approach"):
            for position, record in enumerate(state[layer]):
                ids.append(record["id"])
                values = (record["id"], layer, position, record["content"], record["reason"], record["nature"], record["scope"], record["created_at"], record.get("expires_at"), record["use_policy"], encode(record["source"]), encode({key: value for key, value in record.items() if key not in known}))
                db.execute("INSERT INTO role_record(id,layer,sort_order,content,reason,nature,scope,created_at,expires_at,use_policy,source,extras) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET layer=excluded.layer,sort_order=excluded.sort_order,content=excluded.content,reason=excluded.reason,nature=excluded.nature,scope=excluded.scope,created_at=excluded.created_at,expires_at=excluded.expires_at,use_policy=excluded.use_policy,source=excluded.source,extras=excluded.extras", values)
        if ids:
            db.execute("DELETE FROM role_record WHERE id NOT IN (" + ",".join("?" for _ in ids) + ")", ids)
        else:
            db.execute("DELETE FROM role_record")
        emotion = state["emotion"]
        if emotion is None:
            db.execute("DELETE FROM current_emotion")
        else:
            db.execute("INSERT INTO current_emotion VALUES(1,?,?,?,?,?,?,?) ON CONFLICT(slot) DO UPDATE SET description_intensity=excluded.description_intensity,reason_sources=excluded.reason_sources,time_scope=excluded.time_scope,uncertainty=excluded.uncertainty,source=excluded.source,expires_at=excluded.expires_at,use_policy=excluded.use_policy",
                (emotion["description_intensity"], emotion["reason_sources"], emotion["time_scope"], emotion["uncertainty"], encode(emotion["source"]), emotion["expires_at"], emotion["use_policy"]))

    def close(self) -> None:
        self.connection.close()
