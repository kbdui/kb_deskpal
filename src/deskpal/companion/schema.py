"""角色 schema 4：独立记录和可查询的范围，不保存第二份聚合人格。"""
import sqlite3

VERSION = 4
SCHEMA = (
    "CREATE TABLE role_meta(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>=1),reset_epoch INTEGER NOT NULL CHECK(reset_epoch>=0))",
    "CREATE TABLE character_card(slot INTEGER PRIMARY KEY CHECK(slot=1) REFERENCES role_meta(id),id TEXT NOT NULL UNIQUE CHECK(length(id)=32),revision INTEGER NOT NULL CHECK(revision>=1),text TEXT NOT NULL CHECK(length(trim(text))>0 AND length(CAST(text AS BLOB))<=8192),use_policy TEXT NOT NULL CHECK(use_policy IN ('cloud_allowed','local_only')),origin TEXT NOT NULL CHECK(origin IN ('default','user','legacy_import')))",
    "CREATE TABLE role_record(id TEXT PRIMARY KEY CHECK(length(id)>0),role_id INTEGER NOT NULL DEFAULT 1 CHECK(role_id=1) REFERENCES role_meta(id),layer TEXT NOT NULL CHECK(layer IN ('habits','user_understanding','interaction_approach')),sort_order INTEGER NOT NULL CHECK(sort_order>=0),content TEXT NOT NULL CHECK(length(trim(content))>0 AND length(CAST(content AS BLOB))<=1024),reason TEXT NOT NULL CHECK(length(CAST(reason AS BLOB))<=1024),nature TEXT NOT NULL CHECK(nature IN ('explicit','inferred')),scope TEXT NOT NULL CHECK(scope IN ('turn','conversation','day','persistent')),created_at REAL NOT NULL CHECK(created_at BETWEEN 0 AND 1e12),expires_at REAL CHECK(expires_at BETWEEN 0 AND 1e12),use_policy TEXT NOT NULL CHECK(use_policy IN ('cloud_allowed','local_only')),source TEXT NOT NULL CHECK(json_valid(source) AND json_type(source)='object'),extras TEXT NOT NULL CHECK(json_valid(extras) AND json_type(extras)='object'),source_task TEXT GENERATED ALWAYS AS(json_extract(source,'$.task_id')) VIRTUAL,source_conversation TEXT GENERATED ALWAYS AS(json_extract(source,'$.conversation_id')) VIRTUAL)",
    "CREATE TABLE current_emotion(slot INTEGER PRIMARY KEY CHECK(slot=1) REFERENCES role_meta(id),description_intensity TEXT NOT NULL,reason_sources TEXT NOT NULL,time_scope TEXT NOT NULL,uncertainty TEXT NOT NULL,source TEXT NOT NULL CHECK(json_valid(source) AND json_type(source)='object'),expires_at REAL CHECK(expires_at BETWEEN 0 AND 1e12),use_policy TEXT NOT NULL CHECK(use_policy IN ('cloud_allowed','local_only')))",
)
RECEIPT_SCHEMA = "CREATE TABLE role_receipt(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL)"
INDEXES = (
    "CREATE INDEX role_record_scope ON role_record(layer,scope,expires_at)",
    "CREATE INDEX role_record_task ON role_record(source_task)",
    "CREATE INDEX role_record_conversation ON role_record(source_conversation,scope)",
)
FIELDS = {
    "role_meta": ("id", "revision", "reset_epoch"),
    "character_card": ("slot", "id", "revision", "text", "use_policy", "origin"),
    "role_record": ("id", "role_id", "layer", "sort_order", "content", "reason", "nature", "scope", "created_at", "expires_at", "use_policy", "source", "extras"),
    "current_emotion": ("slot", "description_intensity", "reason_sources", "time_scope", "uncertainty", "source", "expires_at", "use_policy"),
    "role_receipt": ("id", "fingerprint"),
}


def validate(connection: sqlite3.Connection) -> None:
    for table, expected in FIELDS.items():
        if tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})")) != expected:
            raise ValueError("Incomplete character schema 4")
        if connection.execute(f"PRAGMA foreign_key_check({table})").fetchone():
            raise ValueError("Invalid character record reference")
    generated = {row[1] for row in connection.execute("PRAGMA table_xinfo(role_record)") if row[6] in (2, 3)}
    indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    if not {"source_task", "source_conversation"} <= generated or not {"role_record_scope", "role_record_task", "role_record_conversation"} <= indexes:
        raise ValueError("Missing character schema 4 access paths")
