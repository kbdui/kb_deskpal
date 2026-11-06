"""调用诊断只保存计量和固定错误码，不保存提示词、正文或认证头。"""

import sqlite3


def migrate_schema_5(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE model_call_diagnostic(call_id TEXT PRIMARY KEY REFERENCES task_call_attempt(id) ON DELETE CASCADE,started_at TEXT NOT NULL,finished_at TEXT,metadata TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata)))")
    conn.execute('CREATE INDEX model_call_diagnostic_recent ON model_call_diagnostic(started_at DESC,call_id)')
    conn.execute('UPDATE schema_version SET version=6')


def validate(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute('PRAGMA table_info(model_call_diagnostic)')}
    if columns != {'call_id', 'started_at', 'finished_at', 'metadata'}:
        raise ValueError('Incomplete model call diagnostics schema')
    foreign_keys = {(row[2], row[3], row[4], row[6]) for row in conn.execute('PRAGMA foreign_key_list(model_call_diagnostic)')}
    indexes = {row[1] for row in conn.execute('PRAGMA index_list(model_call_diagnostic)')}
    if (('task_call_attempt', 'call_id', 'id', 'CASCADE') not in foreign_keys
            or 'model_call_diagnostic_recent' not in indexes):
        raise ValueError('Incomplete model call diagnostics relations')
