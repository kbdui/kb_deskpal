"""摘要提交与学习提交共用事务；跨库失败保留可恢复的投递。"""
import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE learning_summary_outbox(job_id TEXT PRIMARY KEY REFERENCES learning_job(id),payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','applied','invalidated')),error TEXT NOT NULL DEFAULT '')")


def validate(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute('PRAGMA table_info(learning_summary_outbox)')}
    if columns != {'job_id', 'payload', 'status', 'error'}:
        raise ValueError('Incomplete memory schema 4')
