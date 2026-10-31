"""P4 的统一缓冲、学习工作与跨库投递；业务内容仍使用记忆修订结构。"""
import json
import sqlite3

SCHEMA = (
    "CREATE TABLE learning_job(id TEXT PRIMARY KEY NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('turn','review')),status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed','cancelled')),attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),task_id TEXT NOT NULL,conversation_id TEXT NOT NULL,conversation_epoch INTEGER NOT NULL,privacy_epoch INTEGER NOT NULL,role_revision INTEGER NOT NULL,refs TEXT NOT NULL CHECK(json_valid(refs)),error TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
    "CREATE TABLE learning_link(memory_id TEXT NOT NULL,kind TEXT NOT NULL,object_id TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>=1),conversation_id TEXT NOT NULL,available INTEGER NOT NULL DEFAULT 1 CHECK(available IN (0,1)),PRIMARY KEY(memory_id,kind,object_id,revision))",
    "CREATE INDEX learning_link_source ON learning_link(kind,object_id)",
    "CREATE TABLE learning_record(id TEXT PRIMARY KEY NOT NULL,job_id TEXT NOT NULL REFERENCES learning_job(id),memory_id TEXT,action TEXT NOT NULL,understanding TEXT NOT NULL,reason TEXT NOT NULL,uncertainty TEXT NOT NULL,created_at TEXT NOT NULL)",
    "CREATE INDEX learning_record_memory ON learning_record(memory_id)",
    "CREATE TABLE learning_role_outbox(job_id TEXT PRIMARY KEY NOT NULL REFERENCES learning_job(id),payload TEXT NOT NULL CHECK(json_valid(payload)),status TEXT NOT NULL CHECK(status IN ('pending','applied','invalidated')))",
)
TABLES = {'learning_job', 'learning_link', 'learning_record', 'learning_role_outbox'}


def migrate(connection: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        connection.execute(statement)
    # 去掉旧的七天默认 TTL，不把未审核项直接升级为事实。
    for identity, revision, status, raw, default_expiry, origin in connection.execute(
            "SELECT r.id,r.revision,r.status,r.draft,r.default_expiry,json_extract(s.payload,'$.origin') "
            "FROM memory_revisions r JOIN memory_sources s ON s.id=r.source_id").fetchall():
        data = json.loads(raw)
        if default_expiry:
            data['expires_at'] = None
        data.setdefault('destination', 'episode' if data['kind'] == 'episodic' else 'facts')
        data.setdefault('reason', '从旧版记录保留，尚未经过新版模型复核')
        data.setdefault('uncertainty', '保持原来源性质；采用不等于客观真实')
        data.setdefault('review_after', None)
        # 工具元信息仍可查历史，不再作为角色经历参与普通召回。
        if (origin == 'tool_result' and data.get('predicate') == 'execution') or data['kind'] in ('working', 'goal', 'relationship'):
            status = 'archived'
        connection.execute('UPDATE memory_revisions SET draft=?,status=?,default_expiry=0 WHERE id=? AND revision=?',
                           (json.dumps(data, ensure_ascii=False, allow_nan=False), status, identity, revision))
    connection.execute("UPDATE memory_meta SET value=value+1 WHERE name='epoch'")


def validate(connection: sqlite3.Connection) -> None:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not TABLES <= tables or connection.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('Incomplete memory schema 3')
