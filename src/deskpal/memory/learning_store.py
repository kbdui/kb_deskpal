"""同一记忆连接上的学习账本；这里不调用模型、不执行工具。"""
import json
from hashlib import sha256
from typing import Any
from uuid import uuid4

from .repository import SQLiteMemoryRepository, _json, _stamp


class LearningStore:
    def __init__(self, repository: SQLiteMemoryRepository) -> None:
        self.memory = repository

    def job(self, identity: str) -> dict[str, Any] | None:
        with self.memory._lock:
            row = self.memory._db.execute('SELECT * FROM learning_job WHERE id=?', (identity,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _organized_token(task_id: str) -> str:
        return sha256(('organized-turn:' + task_id).encode('utf-8')).hexdigest()

    def organized_turns(self, task_ids: set[str]) -> set[str]:
        if not task_ids:
            return set()
        tokens = {self._organized_token(task_id): task_id for task_id in task_ids}
        slots = ','.join('?' for _ in task_ids)
        with self.memory._lock:
            db = self.memory._db
            covered = {tokens[row[0]] for row in db.execute(
                f'SELECT token FROM memory_learning_receipts WHERE token IN ({slots})', tuple(tokens))}
            covered.update(row[0] for row in db.execute(
                f"SELECT task_id FROM learning_job WHERE id IN ({slots}) AND kind='turn' AND status='completed'",
                tuple('turn:' + task_id for task_id in task_ids)))
            return covered

    def mark_organized(self, task_ids: set[str]) -> None:
        """Write content-free receipts in the same transaction as learning."""
        with self.memory.batch():
            stamp = _stamp(self.memory._now())
            self.memory._db.executemany(
                'INSERT OR IGNORE INTO memory_learning_receipts VALUES (?, ?)',
                ((self._organized_token(task_id), stamp) for task_id in task_ids))

    def register(self, identity: str, *, kind: str, task_id: str, conversation_id: str,
                 conversation_epoch: int, privacy_epoch: int, role_revision: int,
                 refs: list[dict[str, Any]]) -> dict[str, Any]:
        with self.memory.batch():
            stamp = _stamp(self.memory._now())
            self.memory._db.execute('INSERT OR IGNORE INTO learning_job(id,kind,status,task_id,conversation_id,conversation_epoch,privacy_epoch,role_revision,refs,created_at,updated_at) VALUES(?,?,\'pending\',?,?,?,?,?,?,?,?)',
                (identity, kind, task_id, conversation_id, conversation_epoch, privacy_epoch, role_revision, _json(refs), stamp, stamp))
            result = self.job(identity)
            assert result is not None
            return result

    def attempt(self, identity: str) -> int:
        with self.memory.batch():
            cursor = self.memory._db.execute("UPDATE learning_job SET status='running',attempts=attempts+1,error='',updated_at=? WHERE id=? AND attempts<3 AND status IN ('pending','running','failed')",
                (_stamp(self.memory._now()), identity))
            if not cursor.rowcount:
                raise ValueError('Learning work is finished or its budget is exhausted')
            job = self.job(identity)
            assert job is not None
            return int(job['attempts'])

    def finish(self, identity: str, status: str, error: str = '') -> None:
        if status not in ('completed', 'failed', 'cancelled'):
            raise ValueError('Invalid learning outcome')
        with self.memory.batch():
            self.memory._db.execute('UPDATE learning_job SET status=?,error=?,updated_at=? WHERE id=?',
                (status, error, _stamp(self.memory._now()), identity))

    def link(self, memory_id: str, refs: list[dict[str, Any]]) -> None:
        with self.memory.batch():
            self.memory._db.executemany('INSERT OR IGNORE INTO learning_link VALUES(?,?,?,?,?,?)',
                [(memory_id, ref['kind'], ref['id'], ref['revision'], ref.get('conversation_id', ''), int(ref.get('available', True))) for ref in refs])
            if self.memory._db.execute('SELECT COUNT(*) FROM learning_link WHERE memory_id=?', (memory_id,)).fetchone()[0] > 512:
                raise ValueError('Memory source budget exceeded')

    def refs(self, memory_id: str) -> list[dict[str, Any]]:
        with self.memory._lock:
            return [dict(kind=row['kind'], id=row['object_id'], revision=row['revision'],
                conversation_id=row['conversation_id'], available=bool(row['available']))
                for row in self.memory._db.execute('SELECT * FROM learning_link WHERE memory_id=? ORDER BY kind,object_id,revision', (memory_id,))]

    def record(self, job_id: str, memory_id: str | None, action: str, content: str, reason: str, uncertainty: str) -> None:
        with self.memory.batch():
            self.memory._db.execute('INSERT INTO learning_record VALUES(?,?,?,?,?,?,?,?)',
                (uuid4().hex, job_id, memory_id, action, content, reason, uncertainty, _stamp(self.memory._now())))

    def role_effect(self, job_id: str, payload: dict[str, Any]) -> None:
        with self.memory.batch():
            self.memory._db.execute("INSERT INTO learning_role_outbox VALUES(?,?,'pending')", (job_id, _json(payload)))

    def summary_effect(self, job_id: str, payload: dict[str, Any]) -> None:
        self.memory._db.execute("INSERT INTO learning_summary_outbox(job_id,payload) VALUES(?,?)", (job_id, _json(payload)))

    def pending_summaries(self) -> list[dict[str, Any]]:
        with self.memory._lock:
            return [dict(row) for row in self.memory._db.execute("SELECT o.*,j.conversation_id,j.conversation_epoch,j.privacy_epoch FROM learning_summary_outbox o JOIN learning_job j ON j.id=o.job_id WHERE o.status='pending' ORDER BY j.created_at,j.id LIMIT 100")]

    def summary_status(self, job_id: str, status: str, error: str = '') -> None:
        with self.memory.batch():
            self.memory._db.execute("UPDATE learning_summary_outbox SET status=?,error=?,payload=CASE WHEN ?='pending' THEN payload ELSE '{}' END WHERE job_id=?", (status, error, status, job_id))

    def pending_effects(self) -> list[dict[str, Any]]:
        with self.memory._lock:
            return [dict(row) for row in self.memory._db.execute("SELECT o.*,j.refs,j.task_id,j.conversation_id,j.conversation_epoch,j.privacy_epoch FROM learning_role_outbox o JOIN learning_job j ON j.id=o.job_id WHERE o.status='pending' ORDER BY j.created_at LIMIT 100")]

    def ack(self, job_id: str, *, valid: bool) -> None:
        with self.memory.batch():
            self.memory._db.execute('UPDATE learning_role_outbox SET status=?,payload=\'{}\' WHERE job_id=?',
                                   ('applied' if valid else 'invalidated', job_id))

    def records(self, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError('Invalid learning page size')
        with self.memory._lock:
            result = [dict(row) for row in self.memory._db.execute('SELECT r.*,j.refs AS sources,j.status AS work_status,o.status AS role_delivery FROM learning_record r JOIN learning_job j ON j.id=r.job_id LEFT JOIN learning_role_outbox o ON o.job_id=r.job_id ORDER BY r.created_at DESC,r.id LIMIT ?', (limit,))]
            for row in result:
                row['sources'] = json.loads(row['sources'])
            return result

    def recoverable(self) -> list[dict[str, Any]]:
        with self.memory._lock:
            return [dict(row) for row in self.memory._db.execute("SELECT * FROM learning_job WHERE kind='turn' AND (status IN ('pending','running') OR (status='failed' AND error='temporary_model_failure')) ORDER BY created_at LIMIT 32")]

    def redact_conversation(self, identity: str) -> None:
        """清原话副本、保留长期理解；不把清空聊天误作明确遗忘。"""
        with self.memory.batch():
            db = self.memory._db
            ids = [row[0] for row in db.execute("SELECT DISTINCT memory_id FROM learning_link WHERE conversation_id=? AND kind='dialogue_turn'", (identity,))]
            for memory_id in ids:
                for row in db.execute('SELECT revision,source_id,draft FROM memory_revisions WHERE id=?', (memory_id,)).fetchall():
                    source = db.execute('SELECT payload FROM memory_sources WHERE id=?', (row['source_id'],)).fetchone()
                    if source:
                        data = json.loads(source[0]); data['content'] = '[原消息已清除]'
                        draft = json.loads(row['draft']); draft['quote'] = '[原消息已清除]'
                        db.execute('UPDATE memory_sources SET payload=? WHERE id=?', (_json(data), row['source_id']))
                        db.execute('UPDATE memory_revisions SET draft=? WHERE id=? AND revision=?', (_json(draft), memory_id, row['revision']))
            db.execute("UPDATE learning_link SET available=0 WHERE conversation_id=? AND kind='dialogue_turn'", (identity,))
            changed_jobs = False
            for job in db.execute('SELECT id,refs FROM learning_job').fetchall():
                refs = json.loads(job['refs'])
                changed = False
                for ref in refs:
                    if ref.get('kind') == 'dialogue_turn' and ref.get('conversation_id') == identity and ref.get('available', True):
                        ref['available'] = False
                        changed = True
                if changed:
                    db.execute('UPDATE learning_job SET refs=? WHERE id=?', (_json(refs), job['id']))
                    changed_jobs = True
            if ids or changed_jobs:
                self.memory._bump()
            db.execute("UPDATE learning_job SET status='cancelled',error='conversation_cleared' WHERE conversation_id=? AND status IN ('pending','running','failed')", (identity,))
            db.execute("UPDATE learning_summary_outbox SET status='invalidated',payload='{}',error='conversation_cleared' WHERE status='pending' AND job_id IN (SELECT id FROM learning_job WHERE conversation_id=?)", (identity,))
            # 未投递的旧状态不能在重启后复活。已生效的长期习惯由角色服务保留。
            db.execute("UPDATE learning_role_outbox SET status='invalidated',payload='{}' WHERE status='pending' AND job_id IN (SELECT id FROM learning_job WHERE conversation_id=?)", (identity,))

    def erase_copies(self, ids: set[str]) -> None:
        """仅清除指定记忆的学习正文；同轮其它理解及角色状态保持独立。"""
        db = self.memory._db
        for identity in ids:
            db.execute("UPDATE learning_record SET understanding='',reason='',uncertainty='' WHERE memory_id=?", (identity,))
            db.execute('DELETE FROM learning_link WHERE memory_id=?', (identity,))
            db.execute("UPDATE learning_link SET available=0 WHERE kind='memory' AND object_id=?", (identity,))
        for row in db.execute("SELECT o.job_id,j.refs FROM learning_summary_outbox o JOIN learning_job j ON j.id=o.job_id WHERE o.status='pending'").fetchall():
            if any(ref['kind'] == 'memory' and ref['id'] in ids for ref in json.loads(row['refs'])):
                db.execute("UPDATE learning_summary_outbox SET status='invalidated',payload='{}',error='memory_forgotten' WHERE job_id=?", (row['job_id'],))
        # 未完成的旧学习不得重新引入已删除来源，但已完成记录不作级联擦除。
        for job in db.execute("SELECT id,refs FROM learning_job WHERE status IN ('pending','running','failed')").fetchall():
            if any(ref['kind'] == 'memory' and ref['id'] in ids for ref in json.loads(job['refs'])):
                db.execute("UPDATE learning_job SET status='cancelled',error='source_forgotten' WHERE id=?", (job['id'],))
