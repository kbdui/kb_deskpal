"""P2/P3 的正式工作流结构；唯一建表入口位于版本迁移。"""
import sqlite3

FIELDS = {
    "decision_round": ("task_id", "round", "hash", "body"),
    "task_amendment": ("task_id", "sequence", "command_id", "content", "consumed_round"),
    "task_origin": ("task_id", "kind", "basis"),
    "context_history": ("task_id", "purpose", "generation", "digest", "items", "refs"),
    "context_archive_source": ("task_id", "kind", "object_id", "revision"),
    "conversation_context_summary": ("conversation_id", "epoch", "revision", "content", "refs", "use_policy"),
}
SCHEMA = (
    "CREATE TABLE decision_round(task_id TEXT NOT NULL REFERENCES task(id),round INTEGER NOT NULL CHECK(round>=1),hash TEXT NOT NULL CHECK(length(hash)=64),body TEXT NOT NULL CHECK(json_valid(body)),PRIMARY KEY(task_id,round))",
    "CREATE TABLE task_amendment(task_id TEXT NOT NULL REFERENCES task(id),sequence INTEGER NOT NULL CHECK(sequence BETWEEN 1 AND 64),command_id TEXT NOT NULL UNIQUE CHECK(length(command_id) BETWEEN 1 AND 200),content TEXT NOT NULL CHECK(length(CAST(content AS BLOB)) BETWEEN 1 AND 8192),consumed_round INTEGER CHECK(consumed_round>=0),PRIMARY KEY(task_id,sequence))",
    "CREATE TABLE task_origin(task_id TEXT PRIMARY KEY REFERENCES task(id),kind TEXT NOT NULL CHECK(kind IN ('user','proactive')),basis TEXT NOT NULL CHECK(length(CAST(basis AS BLOB))<=8192))",
    "CREATE TABLE context_history(task_id TEXT NOT NULL REFERENCES task(id),purpose TEXT NOT NULL CHECK(purpose IN ('planning','expression')),generation INTEGER NOT NULL CHECK(generation>=1),digest TEXT NOT NULL CHECK(length(digest)=64),items TEXT NOT NULL CHECK(json_valid(items)),refs TEXT NOT NULL CHECK(json_valid(refs)),PRIMARY KEY(task_id,purpose,generation))",
    "CREATE TABLE context_archive_source(task_id TEXT NOT NULL REFERENCES task(id),kind TEXT NOT NULL CHECK(length(kind)>0),object_id TEXT NOT NULL CHECK(length(object_id)>0),revision INTEGER NOT NULL CHECK(revision>=1),PRIMARY KEY(task_id,kind,object_id,revision))",
    "CREATE TABLE conversation_context_summary(conversation_id TEXT PRIMARY KEY REFERENCES conversation(id),epoch INTEGER NOT NULL CHECK(epoch>=0),revision INTEGER NOT NULL CHECK(revision>=1),content TEXT NOT NULL CHECK(json_valid(content)),refs TEXT NOT NULL CHECK(json_valid(refs)),use_policy TEXT NOT NULL CHECK(use_policy IN ('cloud_allowed','local_only')))",
)
INDEXES = {
    "context_archive_source_object": "CREATE INDEX context_archive_source_object ON context_archive_source(kind,object_id)",
    "task_amendment_pending": "CREATE INDEX task_amendment_pending ON task_amendment(task_id,consumed_round,sequence)",
}


def migrate_schema_3(conn: sqlite3.Connection) -> None:
    """外层事务确保复制、约束检查和版本提交一起成功或回滚。"""
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    copied: list[str] = []
    for name, fields in FIELDS.items():
        if name in existing:
            columns = tuple(row[1] for row in conn.execute(f"PRAGMA table_info({name})"))
            if columns != fields:
                raise ValueError("Unrecognized workflow extension layout")
            conn.execute(f"ALTER TABLE {name} RENAME TO upgrading_v4_{name}")
            copied.append(name)
    for statement in SCHEMA:
        conn.execute(statement)
    for name in copied:
        columns_sql = ",".join(FIELDS[name])
        conn.execute(f"INSERT INTO {name}({columns_sql}) SELECT {columns_sql} FROM upgrading_v4_{name}")
        conn.execute(f"DROP TABLE upgrading_v4_{name}")
    for statement in INDEXES.values():
        conn.execute(statement)
    validate(conn)
    conn.execute("UPDATE schema_version SET version=4")


def validate(conn: sqlite3.Connection) -> None:
    for name, fields in FIELDS.items():
        if tuple(row[1] for row in conn.execute(f"PRAGMA table_info({name})")) != fields:
            raise ValueError("Incomplete workflow schema 4")
        if conn.execute(f"PRAGMA foreign_key_check({name})").fetchone() is not None:
            raise ValueError("Broken workflow extension reference")
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    if not set(INDEXES) <= indexes:
        raise ValueError("Missing workflow schema 4 index")
