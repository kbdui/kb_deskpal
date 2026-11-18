"""任务内的澄清问题和补充回答；保持原任务及其最终回复的唯一入口。"""
import sqlite3


def migrate_schema_8(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE conversation_exchange(
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversation(id),
        sequence INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('user_message','assistant_message')),
        task_id TEXT NOT NULL REFERENCES task(id), revision INTEGER NOT NULL DEFAULT 1,
        body TEXT NOT NULL, use_policy TEXT NOT NULL, created_at TEXT NOT NULL, redacted_at TEXT,
        UNIQUE(conversation_id,sequence))""")
    conn.execute("CREATE INDEX conversation_exchange_task ON conversation_exchange(task_id,sequence)")
    conn.execute("UPDATE schema_version SET version=9")


def validate(conn: sqlite3.Connection) -> None:
    expected = {'id','conversation_id','sequence','kind','task_id','revision','body','use_policy','created_at','redacted_at'}
    if {row[1] for row in conn.execute('PRAGMA table_info(conversation_exchange)')} != expected:
        raise ValueError('Incomplete workflow schema 9')
