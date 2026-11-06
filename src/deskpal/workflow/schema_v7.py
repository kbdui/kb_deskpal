"""滚动摘要的独立来源、幂等投递及统一免打扰配置。"""
import json
import sqlite3


def migrate_schema_6(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE conversation_summary_source(conversation_id TEXT NOT NULL REFERENCES conversation(id),epoch INTEGER NOT NULL,kind TEXT NOT NULL,object_id TEXT NOT NULL,revision INTEGER NOT NULL,use_policy TEXT NOT NULL,PRIMARY KEY(conversation_id,epoch,kind,object_id))")
    conn.execute("CREATE TABLE conversation_summary_delivery(job_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,epoch INTEGER NOT NULL,revision INTEGER NOT NULL)")
    conn.execute("CREATE TABLE notification_settings(id INTEGER PRIMARY KEY CHECK(id=1),quiet_start INTEGER NOT NULL CHECK(quiet_start BETWEEN 0 AND 1439),quiet_end INTEGER NOT NULL CHECK(quiet_end BETWEEN 0 AND 1439),reminder_bypass INTEGER NOT NULL CHECK(reminder_bypass IN (0,1)))")
    conn.execute("CREATE TABLE notification_deferred(reminder_id TEXT PRIMARY KEY REFERENCES desktop_reminder_inbox(id) ON DELETE CASCADE,released_at TEXT)")
    for row in conn.execute('SELECT conversation_id,epoch,refs FROM conversation_context_summary').fetchall():
        for ref in json.loads(row['refs']):
            conn.execute('INSERT OR REPLACE INTO conversation_summary_source VALUES(?,?,?,?,?,?)',
                         (row['conversation_id'], row['epoch'], ref['kind'], ref['id'], ref['revision'], ref.get('use_policy', 'cloud_allowed')))
    conn.execute("UPDATE conversation_context_summary SET refs='[]'")
    conn.execute('UPDATE schema_version SET version=7')


def validate(conn: sqlite3.Connection) -> None:
    required = {'conversation_summary_source', 'conversation_summary_delivery', 'notification_settings', 'notification_deferred'}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required <= tables:
        raise ValueError('Incomplete workflow schema 7')
