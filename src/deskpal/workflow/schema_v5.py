"""P5 控制与主动关注记录；仅由正式工作库迁移创建。"""
import sqlite3

SCHEMA = (
    "CREATE TABLE task_control(task_id TEXT PRIMARY KEY REFERENCES task(id),requested INTEGER NOT NULL DEFAULT 0 CHECK(requested IN(0,1)),checkpoint TEXT NOT NULL DEFAULT '',elapsed REAL NOT NULL DEFAULT 0 CHECK(elapsed>=0))",
    "CREATE TABLE task_control_receipt(command_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,task_id TEXT NOT NULL REFERENCES task(id),operation TEXT NOT NULL)",
    "CREATE TABLE notification_policy(kind TEXT PRIMARY KEY CHECK(kind IN('chat','reminder')),paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN(0,1)),until_at TEXT,revision INTEGER NOT NULL DEFAULT 1)",
    "INSERT INTO notification_policy(kind) VALUES('chat'),('reminder')",
    "CREATE TABLE notification_policy_receipt(command_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL)",
    "CREATE TABLE attention(id TEXT PRIMARY KEY,source_key TEXT NOT NULL UNIQUE,method TEXT NOT NULL,goal TEXT NOT NULL,basis TEXT NOT NULL,refs TEXT NOT NULL CHECK(json_valid(refs)),due_at TEXT NOT NULL,expires_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,task_id TEXT REFERENCES task(id),created_at TEXT NOT NULL)",
    "CREATE INDEX attention_due ON attention(status,due_at)",
    "CREATE TABLE proactive_method(name TEXT PRIMARY KEY,enabled INTEGER NOT NULL CHECK(enabled IN(0,1)))",
    "INSERT INTO proactive_method VALUES('event',1),('review',1),('opportunity',1),('fallback',1)",
    "CREATE TABLE proactive_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
)
REQUIRED = {'task_control', 'task_control_receipt', 'notification_policy',
            'notification_policy_receipt', 'attention', 'proactive_method', 'proactive_meta'}


def migrate_schema_4(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        conn.execute(statement)
    conn.execute('UPDATE schema_version SET version=5')
    validate(conn)


def validate(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not REQUIRED <= tables or conn.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('Incomplete P5 workflow schema')
    columns = {
        'task_control': {'task_id','requested','checkpoint','elapsed'},
        'task_control_receipt': {'command_id','fingerprint','task_id','operation'},
        'notification_policy': {'kind','paused','until_at','revision'},
        'notification_policy_receipt': {'command_id','fingerprint'},
        'attention': {'id','source_key','method','goal','basis','refs','due_at','expires_at','status','attempts','task_id','created_at'},
        'proactive_method': {'name','enabled'}, 'proactive_meta': {'key','value'},
    }
    for name, expected in columns.items():
        if {row[1] for row in conn.execute('PRAGMA table_info(' + name + ')')} != expected:
            raise ValueError('Invalid P5 workflow table layout')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='attention_due'").fetchone() is None:
        raise ValueError('Missing attention due index')
    if {row[0] for row in conn.execute('SELECT kind FROM notification_policy')} != {'chat', 'reminder'}:
        raise ValueError('Incomplete notification policy')
