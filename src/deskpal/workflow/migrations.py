"""Frozen workflow schema 2 additions; migrations never rewrite old plans."""

import sqlite3


SCHEMA_VERSION = 2
SCHEMA_2 = (
    "ALTER TABLE task ADD COLUMN privacy_epoch INTEGER NOT NULL DEFAULT 0",
    "CREATE TABLE privacy_control(id INTEGER PRIMARY KEY CHECK(id=1),epoch INTEGER NOT NULL,blocked INTEGER NOT NULL CHECK(blocked IN (0,1)))",
    "INSERT INTO privacy_control VALUES(1,0,0)",
    "CREATE TABLE privacy_request(command_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,job_id TEXT NOT NULL,memory_id TEXT NOT NULL,expected_revision INTEGER NOT NULL,epoch INTEGER NOT NULL)",
    "CREATE TABLE turn_ref(task_id TEXT PRIMARY KEY REFERENCES task(id),conversation_id TEXT NOT NULL,epoch INTEGER NOT NULL,sequence INTEGER NOT NULL,privacy_epoch INTEGER NOT NULL,UNIQUE(conversation_id,epoch,sequence))",
    "CREATE TABLE context_snapshot(task_id TEXT NOT NULL REFERENCES task(id),purpose TEXT NOT NULL,digest TEXT NOT NULL,items TEXT NOT NULL,PRIMARY KEY(task_id,purpose))",
    "CREATE TABLE context_dependency(task_id TEXT NOT NULL REFERENCES task(id),purpose TEXT NOT NULL,kind TEXT NOT NULL,object_id TEXT NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(task_id,purpose,kind,object_id))",
    "CREATE INDEX context_dependency_object ON context_dependency(kind,object_id)",
    "CREATE TABLE task_call_attempt(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES task(id),purpose TEXT NOT NULL,slot INTEGER NOT NULL,status TEXT NOT NULL,UNIQUE(task_id,purpose,slot))",
    "CREATE TABLE companion_effect_outbox(sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,task_id TEXT NOT NULL UNIQUE REFERENCES task(id),status TEXT NOT NULL,success INTEGER NOT NULL,failure INTEGER NOT NULL,epoch INTEGER NOT NULL,terminal_revision INTEGER NOT NULL,created_at TEXT NOT NULL,appraisal_version INTEGER NOT NULL DEFAULT 1)",
)


def migrate_schema_1(conn: sqlite3.Connection) -> None:
    """Called only after an online backup, inside the initialization transaction."""
    for statement in SCHEMA_2:
        conn.execute(statement)
    conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
