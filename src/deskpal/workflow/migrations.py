"""Frozen workflow schema 2 additions; migrations never rewrite old plans."""

import sqlite3
from uuid import uuid4

from deskpal.tasks.models import utc_now


SCHEMA_VERSION = 3
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
    conn.execute("UPDATE schema_version SET version=2")


SCHEMA_3 = (
    "CREATE TABLE desktop_reminder_cursor(source TEXT PRIMARY KEY,store_id TEXT NOT NULL,generation TEXT NOT NULL,after_id INTEGER NOT NULL,anchor TEXT NOT NULL,blocked INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE desktop_reminder_inbox(id TEXT PRIMARY KEY,source TEXT NOT NULL,source_id TEXT NOT NULL,event_id INTEGER NOT NULL,store_id TEXT NOT NULL,generation TEXT NOT NULL,occurrence_revision INTEGER NOT NULL,due_at TEXT NOT NULL,recorded_at TEXT NOT NULL,label TEXT NOT NULL,conversation_id TEXT,task_id TEXT,acknowledged_at TEXT,delivery_state TEXT NOT NULL,UNIQUE(source,store_id,generation,event_id))",
    "CREATE TABLE desktop_resource_link(source TEXT NOT NULL,source_id TEXT NOT NULL,store_id TEXT NOT NULL,generation TEXT NOT NULL,task_id TEXT NOT NULL,conversation_id TEXT,PRIMARY KEY(source,source_id,store_id,generation))",
    "CREATE TABLE conversation(id TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('default','project')),title TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,timeline_revision INTEGER NOT NULL DEFAULT 1,context_epoch INTEGER NOT NULL DEFAULT 0,next_sequence INTEGER NOT NULL DEFAULT 1,next_turn INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,archived_at TEXT,deleted_at TEXT)",
    "CREATE UNIQUE INDEX conversation_default ON conversation(kind) WHERE kind='default'",
    "CREATE TRIGGER conversation_keep_default BEFORE DELETE ON conversation WHEN OLD.kind='default' BEGIN SELECT RAISE(ABORT,'Default conversation cannot be deleted'); END",
    "CREATE TRIGGER conversation_fixed_default_title BEFORE UPDATE ON conversation WHEN OLD.kind='default' AND NEW.title!='日常聊天' BEGIN SELECT RAISE(ABORT,'Default conversation title is fixed'); END",
    "CREATE TRIGGER conversation_protect_default BEFORE UPDATE ON conversation WHEN OLD.kind='default' AND (NEW.kind!='default' OR NEW.deleted_at IS NOT NULL OR NEW.archived_at IS NOT NULL) BEGIN SELECT RAISE(ABORT,'Default conversation cannot be archived or deleted'); END",
    "CREATE INDEX conversation_directory ON conversation(archived_at,deleted_at,created_at,id)",
    "CREATE TABLE conversation_item(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversation(id),sequence INTEGER NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('user_message','assistant_message','task_card','reminder')),task_id TEXT REFERENCES task(id),revision INTEGER NOT NULL DEFAULT 1,body TEXT NOT NULL DEFAULT '',use_policy TEXT NOT NULL DEFAULT 'cloud_allowed',created_at TEXT NOT NULL,redacted_at TEXT,UNIQUE(conversation_id,sequence))",
    "CREATE UNIQUE INDEX conversation_task_item ON conversation_item(task_id,kind) WHERE kind!='reminder'",
    "CREATE TABLE conversation_item_source(item_id TEXT NOT NULL REFERENCES conversation_item(id) ON DELETE CASCADE,source_kind TEXT NOT NULL,source_id TEXT NOT NULL,source_revision INTEGER NOT NULL,use_policy TEXT NOT NULL,PRIMARY KEY(item_id,source_kind,source_id))",
    "CREATE INDEX conversation_source_object ON conversation_item_source(source_kind,source_id)",
    "CREATE TABLE conversation_turn(task_id TEXT PRIMARY KEY REFERENCES task(id),refs TEXT NOT NULL DEFAULT '[]',facts TEXT NOT NULL DEFAULT '[]',published INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE conversation_receipt(command_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,operation TEXT NOT NULL,conversation_id TEXT NOT NULL REFERENCES conversation(id),result_identity TEXT NOT NULL)",
    "CREATE TABLE conversation_cleanup(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversation(id),operation TEXT NOT NULL,status TEXT NOT NULL,affected_count INTEGER NOT NULL,created_at TEXT NOT NULL,pending_tasks TEXT NOT NULL DEFAULT '[]')",
)


def migrate_schema_2(conn: sqlite3.Connection) -> None:
    """Schema 3 is one atomic migration, including managed asset metadata."""
    from deskpal.assets.schema import ASSET_SCHEMA

    for statement in (*SCHEMA_3, *ASSET_SCHEMA):
        conn.execute(statement)
    now = utc_now().isoformat()
    conn.execute("INSERT INTO conversation(id,kind,title,created_at,updated_at) VALUES(?,'default','日常聊天',?,?)", (str(uuid4()), now, now))
    conn.execute("UPDATE schema_version SET version=3")
