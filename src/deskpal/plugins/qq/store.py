"""QQ 独立档案库：真实发言者、原消息、任务、逐条发送回执与会话摘要。"""
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from time import time
from threading import RLock
from typing import Any, Iterator
from uuid import uuid4


SCHEMA = '''
CREATE TABLE qq_message(sequence INTEGER PRIMARY KEY AUTOINCREMENT,account TEXT NOT NULL,thread TEXT NOT NULL,
 platform_id TEXT NOT NULL,speaker_id TEXT NOT NULL,speaker_name TEXT NOT NULL,direction TEXT NOT NULL,
 content TEXT NOT NULL,created_at TEXT NOT NULL,eligible INTEGER NOT NULL,work_id TEXT,organized INTEGER NOT NULL DEFAULT 0,
 UNIQUE(account,thread,platform_id));
CREATE INDEX qq_pending ON qq_message(eligible,work_id,sequence);
CREATE INDEX qq_thread_history ON qq_message(account,thread,sequence);
CREATE TABLE qq_work(id TEXT PRIMARY KEY,account TEXT NOT NULL,thread TEXT NOT NULL,principal TEXT NOT NULL,
 task_id TEXT UNIQUE,state TEXT NOT NULL,anchor INTEGER NOT NULL,created_at TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',
 planning_sources TEXT NOT NULL DEFAULT '[]');
CREATE TABLE qq_send_batch(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,call_id TEXT NOT NULL,account TEXT NOT NULL,
 thread TEXT NOT NULL,draft_hash TEXT NOT NULL,anchor INTEGER NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,
 UNIQUE(task_id,call_id));
CREATE TABLE qq_send_command(batch_id TEXT NOT NULL,position INTEGER NOT NULL,content TEXT NOT NULL,state TEXT NOT NULL,
 platform_id TEXT,error TEXT NOT NULL DEFAULT '',PRIMARY KEY(batch_id,position),FOREIGN KEY(batch_id) REFERENCES qq_send_batch(id));
CREATE TABLE qq_summary(account TEXT NOT NULL,thread TEXT NOT NULL,content TEXT NOT NULL,revision INTEGER NOT NULL,
 PRIMARY KEY(account,thread));
CREATE TABLE qq_learning(task_id TEXT PRIMARY KEY,state TEXT NOT NULL,sources TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
 summary_revision INTEGER NOT NULL);
PRAGMA user_version=1;
'''

SOCIAL_SCHEMA = '''
CREATE TABLE qq_person(id TEXT PRIMARY KEY,account TEXT NOT NULL,name TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE qq_identity(platform TEXT NOT NULL,namespace TEXT NOT NULL,external_id TEXT NOT NULL,
 person_id TEXT NOT NULL REFERENCES qq_person(id),PRIMARY KEY(platform,namespace,external_id));
CREATE TABLE qq_social_meta(slot INTEGER PRIMARY KEY CHECK(slot=1),revision INTEGER NOT NULL);
INSERT INTO qq_social_meta VALUES(1,0);
CREATE TABLE qq_social_memory(id TEXT PRIMARY KEY,account TEXT NOT NULL,thread TEXT NOT NULL,scope TEXT NOT NULL,
 kind TEXT NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL,content TEXT NOT NULL,reason TEXT NOT NULL,
 uncertainty TEXT NOT NULL,review_after TEXT,evidence TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX qq_social_account ON qq_social_memory(account,status,review_after);
CREATE TABLE qq_social_link(memory_id TEXT NOT NULL REFERENCES qq_social_memory(id),position INTEGER NOT NULL,
 person_id TEXT REFERENCES qq_person(id),name TEXT NOT NULL,relation TEXT NOT NULL,PRIMARY KEY(memory_id,position));
CREATE INDEX qq_social_person ON qq_social_link(person_id,memory_id);
CREATE TABLE qq_experience_outbox(task_id TEXT PRIMARY KEY,payload TEXT NOT NULL,state TEXT NOT NULL,
 receipt TEXT,attempts INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '');
ALTER TABLE qq_learning ADD COLUMN input TEXT NOT NULL DEFAULT '{}';
ALTER TABLE qq_work ADD COLUMN trigger TEXT NOT NULL DEFAULT 'message';
ALTER TABLE qq_work ADD COLUMN review_targets TEXT NOT NULL DEFAULT '[]';
ALTER TABLE qq_message ADD COLUMN reply_to TEXT;
ALTER TABLE qq_message ADD COLUMN mentions TEXT NOT NULL DEFAULT '[]';
ALTER TABLE qq_send_batch ADD COLUMN delivery TEXT NOT NULL DEFAULT '{}';
CREATE TABLE qq_followup(id TEXT PRIMARY KEY,account TEXT NOT NULL,thread TEXT NOT NULL,principal TEXT NOT NULL,
 goal TEXT NOT NULL,reason TEXT NOT NULL,mode TEXT NOT NULL,due_at TEXT NOT NULL,state TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0,work_id TEXT,sources TEXT NOT NULL,created_at TEXT NOT NULL,error TEXT NOT NULL DEFAULT '');
CREATE INDEX qq_followup_due ON qq_followup(state,due_at);
CREATE TABLE qq_review_attempt(memory_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,next_attempt TEXT NOT NULL,attempts INTEGER NOT NULL);
PRAGMA user_version=2;
'''

THROTTLE_SCHEMA = '''
ALTER TABLE qq_message ADD COLUMN received_at REAL NOT NULL DEFAULT 0;
ALTER TABLE qq_message ADD COLUMN direct_call INTEGER NOT NULL DEFAULT 0;
ALTER TABLE qq_work ADD COLUMN started_at REAL;
ALTER TABLE qq_work ADD COLUMN direct_call INTEGER NOT NULL DEFAULT 0;
CREATE TABLE qq_group_gate(account TEXT NOT NULL,thread TEXT NOT NULL,last_started REAL NOT NULL DEFAULT 0,
 last_direct REAL NOT NULL DEFAULT 0,PRIMARY KEY(account,thread));
UPDATE qq_message SET eligible=0 WHERE thread LIKE 'group:%' AND work_id IS NULL;
PRAGMA user_version=3;
'''

TABLE_COLUMNS = {
    'qq_message': ('sequence', 'account', 'thread', 'platform_id', 'speaker_id', 'speaker_name', 'direction',
                   'content', 'created_at', 'eligible', 'work_id', 'organized', 'reply_to', 'mentions'),
    'qq_work': ('id', 'account', 'thread', 'principal', 'task_id', 'state', 'anchor', 'created_at', 'error', 'planning_sources', 'trigger', 'review_targets'),
    'qq_send_batch': ('id', 'task_id', 'call_id', 'account', 'thread', 'draft_hash', 'anchor', 'state', 'created_at', 'delivery'),
    'qq_send_command': ('batch_id', 'position', 'content', 'state', 'platform_id', 'error'),
    'qq_summary': ('account', 'thread', 'content', 'revision'),
    'qq_learning': ('task_id', 'state', 'sources', 'attempts', 'summary_revision', 'input'),
    'qq_person': ('id', 'account', 'name', 'created_at'),
    'qq_identity': ('platform', 'namespace', 'external_id', 'person_id'),
    'qq_social_meta': ('slot', 'revision'),
    'qq_social_memory': ('id', 'account', 'thread', 'scope', 'kind', 'status', 'revision', 'content', 'reason',
                         'uncertainty', 'review_after', 'evidence', 'created_at', 'updated_at'),
    'qq_social_link': ('memory_id', 'position', 'person_id', 'name', 'relation'),
    'qq_experience_outbox': ('task_id', 'payload', 'state', 'receipt', 'attempts', 'error'),
    'qq_followup': ('id', 'account', 'thread', 'principal', 'goal', 'reason', 'mode', 'due_at', 'state', 'attempts', 'work_id', 'sources', 'created_at', 'error'),
    'qq_review_attempt': ('memory_id', 'revision', 'next_attempt', 'attempts'),
}
TABLE_COLUMNS['qq_message'] += ('received_at', 'direct_call')
TABLE_COLUMNS['qq_work'] += ('started_at', 'direct_call')
TABLE_COLUMNS['qq_group_gate'] = ('account', 'thread', 'last_started', 'last_direct')


def validate(db: sqlite3.Connection) -> None:
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version not in (1, 2, 3):
        raise ValueError('不支持的 QQ 插件数据库版本')
    for table, expected in TABLE_COLUMNS.items():
        if version < 3:
            if table == 'qq_group_gate':
                continue
            if table in {'qq_message', 'qq_work'}:
                expected = expected[:-2]
        if version == 1 and table not in {'qq_message', 'qq_work', 'qq_send_batch', 'qq_send_command', 'qq_summary', 'qq_learning'}:
            continue
        if version == 1 and table == 'qq_learning':
            expected = expected[:-1]
        if version == 1 and table == 'qq_work':
            expected = expected[:-2]
        if version == 1 and table == 'qq_message':
            expected = expected[:-2]
        if version == 1 and table == 'qq_send_batch':
            expected = expected[:-1]
        if tuple(row[1] for row in db.execute('PRAGMA table_info(' + table + ')')) != expected:
            raise ValueError('QQ 插件数据库结构不完整')
    if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise ValueError('QQ 发送回执存在失效引用')


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QqStore:
    def __init__(self, path: Path) -> None:
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink > 1)):
            raise ValueError('QQ 数据库必须是当前档案下的独立普通文件')
        self.path = path
        self.lock = RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self._initialize()
        except Exception:
            self.db.close()
            raise

    def _initialize(self) -> None:
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=3000')
        if self.db.execute('PRAGMA user_version').fetchone()[0] == 0:
            self.db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA + '\nCOMMIT;')
        validate(self.db)
        if self.db.execute('PRAGMA user_version').fetchone()[0] == 1:
            self.db.executescript('BEGIN IMMEDIATE;\n' + SOCIAL_SCHEMA + '\nCOMMIT;')
            validate(self.db)
        if self.db.execute('PRAGMA user_version').fetchone()[0] == 2:
            self.db.executescript('BEGIN IMMEDIATE;\n' + THROTTLE_SCHEMA + '\nCOMMIT;')
            validate(self.db)
        with self.transaction() as db:
            # 请求发出后崩溃，不能猜失败并重发。
            db.execute("UPDATE qq_send_command SET state='unknown',error='上次发送中断，结果需核对' WHERE state='sending'")
            db.execute("UPDATE qq_send_batch SET state='blocked' WHERE id IN (SELECT batch_id FROM qq_send_command WHERE state='unknown')")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock, self.db:
            yield self.db

    def rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args)]

    def receive(self, account: str, thread: str, platform_id: str, speaker: str, name: str,
                body: str, stamp: str, *, outgoing: bool, eligible: bool, reply_to: str | None = None, mentions: tuple[str, ...] = (),
                direct_call: bool = False, received_at: float | None = None) -> bool:
        with self.transaction() as db:
            return bool(db.execute('INSERT OR IGNORE INTO qq_message(account,thread,platform_id,speaker_id,speaker_name,direction,content,created_at,eligible,reply_to,mentions,received_at,direct_call) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (account, thread, platform_id, speaker, name[:120], 'out' if outgoing else 'in', body, stamp, int(eligible), reply_to, json.dumps(mentions),
                 time() if received_at is None else received_at, int(direct_call))).rowcount)

    def reserve_work(self, account: str, *, config: dict[str, Any] | None = None, clock: float | None = None) -> dict[str, Any] | None:
        from .throttle import ready_thread
        clock = time() if clock is None else clock
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM qq_work WHERE account=? AND state='queued' ORDER BY created_at LIMIT 1", (account,)).fetchone()
            if existing:
                return dict(existing)
            db.execute('UPDATE qq_message SET received_at=? WHERE account=? AND work_id IS NULL AND received_at>?', (clock, account, clock))
            rows = db.execute("""SELECT m.thread,MIN(m.sequence) AS first_sequence,MIN(m.received_at) AS first_at,
                MIN(CASE WHEN m.direct_call=1 THEN m.received_at END) AS direct_at
                FROM qq_message m WHERE m.account=? AND m.eligible=1 AND m.work_id IS NULL
                AND NOT EXISTS(SELECT 1 FROM qq_work w WHERE w.account=m.account AND w.thread=m.thread AND w.state IN ('queued','running'))
                GROUP BY m.thread""", (account,)).fetchall()
            row = ready_thread(db, account, rows, config or {}, clock)
            if row is None:
                return None
            identity = uuid4().hex
            group = row['thread'].startswith('group:')
            # 群聊取最新一批，直接呼叫优先保留；私聊维持原来的顺序。
            order = 'direct_call DESC,sequence DESC' if group else 'sequence'
            candidates = db.execute('SELECT sequence,content,speaker_id FROM qq_message WHERE account=? AND thread=? AND eligible=1 AND work_id IS NULL ORDER BY ' + order + ' LIMIT 8',
                                    (account, row['thread'])).fetchall()
            selected, size = [], 0
            for item in candidates:
                cost = len(item['content'].encode('utf-8'))
                if size + cost > 12000:
                    continue
                selected.append(item['sequence']); size += cost
            if not selected:
                return None
            anchor = max(selected)
            principal = next(item['speaker_id'] for item in candidates if item['sequence'] == selected[0])
            db.execute('INSERT INTO qq_work(id,account,thread,principal,task_id,state,anchor,created_at,error,direct_call) VALUES(?,?,?,?,NULL,?,?,?,?,?)',
                (identity, account, row['thread'], principal, 'queued', anchor, now(), '', int(row['direct_at'] is not None)))
            db.executemany('UPDATE qq_message SET work_id=? WHERE sequence=? AND work_id IS NULL', ((identity, sequence) for sequence in selected))
            if group:
                # 只撤下旧积压的独立唤醒资格，不认领/删除正文，不伪造已整理。
                db.execute('UPDATE qq_message SET eligible=0 WHERE account=? AND thread=? AND work_id IS NULL', (account, row['thread']))
            return dict(db.execute('SELECT * FROM qq_work WHERE id=?', (identity,)).fetchone())

    def admit(self, work_id: str) -> list[dict[str, Any]]:
        with self.transaction() as db:
            work = db.execute('SELECT * FROM qq_work WHERE id=?', (work_id,)).fetchone()
            if work is None:
                raise ValueError('QQ 工作不存在')
            # 这里只构造候选上下文；模型真正收到规划输入时才认领新来信。
            owned = db.execute('SELECT * FROM qq_message WHERE work_id=? ORDER BY sequence LIMIT 64', (work_id,)).fetchall()
            recent = db.execute('SELECT * FROM qq_message WHERE account=? AND thread=? ORDER BY sequence DESC LIMIT 24',
                (work['account'], work['thread'])).fetchall()
            rows = [*owned, *[row for row in recent if row['sequence'] not in {item['sequence'] for item in owned}]]
            selected, size = [], 0
            for row in rows:
                cost = len(row['content'].encode('utf-8'))
                if size + cost > 16000:
                    continue
                selected.append(dict(row)); size += cost
            return sorted(selected, key=lambda row: row['sequence'])

    def mark_planning_input(self, work_id: str, sequences: tuple[int, ...], *, claim: bool = True) -> None:
        """保存最近一次实际规划输入；收尾查看、摘要整理不能吞掉尚未判断的来信。"""
        with self.transaction() as db:
            from .throttle import started
            started(db, work_id, time())
            work = db.execute('SELECT account,thread FROM qq_work WHERE id=?', (work_id,)).fetchone()
            if work is None:
                raise ValueError('QQ 工作不存在')
            if not claim:
                sequences = tuple(sequence for sequence in sequences if db.execute('SELECT 1 FROM qq_message WHERE sequence=? AND (eligible=0 OR work_id IS NOT NULL)', (sequence,)).fetchone())
            db.execute('UPDATE qq_work SET planning_sources=? WHERE id=?',
                       (json.dumps(sequences), work_id))
            if claim:
                db.executemany('UPDATE qq_message SET work_id=? WHERE sequence=? AND account=? AND thread=? AND eligible=1 AND work_id IS NULL',
                               ((work_id, sequence, work['account'], work['thread']) for sequence in sequences))

    def learning_input(self, work_id: str) -> list[dict[str, Any]]:
        work = self.rows('SELECT account,thread,planning_sources FROM qq_work WHERE id=?', (work_id,))[0]
        sequences = json.loads(work['planning_sources'])
        slots = ','.join('?' for _ in sequences) or 'NULL'
        rows = self.rows('SELECT * FROM qq_message WHERE account=? AND thread=? AND organized=0 AND (sequence IN (' + slots + ") OR (work_id=? AND direction='out')) ORDER BY sequence",
                         (work['account'], work['thread'], *sequences, work_id))
        # 旧轮次选择暂缓的材料仍能在后续整理；不认领最后规划之后的新来信。
        cutoff = max(sequences, default=0)
        older = self.rows('SELECT * FROM qq_message WHERE account=? AND thread=? AND organized=0 AND sequence<=? ORDER BY sequence LIMIT 64',
                          (work['account'], work['thread'], cutoff))
        seen = {row['sequence'] for row in rows}
        rows.extend(row for row in older if row['sequence'] not in seen)
        selected, size = [], 0
        for row in rows:
            cost = len(row['content'].encode('utf-8'))
            if size + cost <= 16000:
                selected.append(row); size += cost
        return selected

    def history(self, account: str, thread: str, query: str = '', before: int | None = None, message_id: str = '') -> list[dict[str, Any]]:
        """按需回看当前会话原文，整条纳入预算，不截断一句话。"""
        rows = self.rows("SELECT * FROM qq_message WHERE account=? AND thread=? AND (?='' OR instr(content,?)>0) AND (? IS NULL OR sequence<?) AND (?='' OR platform_id=?) ORDER BY sequence DESC LIMIT 30",
                         (account, thread, query, query, before, before, message_id, message_id))
        selected, size = [], 0
        for row in rows:
            cost = len(row['content'].encode())
            if size + cost <= 16000:
                selected.append(row); size += cost
        return list(reversed(selected))

    def summary(self, account: str, thread: str) -> str:
        rows = self.rows('SELECT content FROM qq_summary WHERE account=? AND thread=?', (account, thread))
        return rows[0]['content'] if rows else ''

    def batch(self, task_id: str, call_id: str, account: str, thread: str, draft: str,
              messages: list[str] | None = None, *, delivery: dict[str, Any] | None = None) -> dict[str, Any] | None:
        delivery = delivery or {}
        encoded = json.dumps(delivery, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        fingerprint = sha256((draft + ('\n' + encoded if delivery else '')).encode()).hexdigest()
        with self.transaction() as db:
            prior = db.execute('SELECT * FROM qq_send_batch WHERE task_id=? AND call_id=?', (task_id, call_id)).fetchone()
            if prior:
                if prior['draft_hash'] != fingerprint or (prior['account'], prior['thread']) != (account, thread):
                    raise ValueError('发送编号已绑定其他内容')
                return dict(prior)
            if messages is None:
                return None
            identity = uuid4().hex
            anchor = db.execute("SELECT COALESCE(MAX(sequence),0) FROM qq_message WHERE account=? AND thread=? AND direction='in'", (account, thread)).fetchone()[0]
            db.execute('INSERT INTO qq_send_batch VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (identity, task_id, call_id, account, thread, fingerprint, anchor, 'pending', now(), encoded))
            db.executemany("INSERT INTO qq_send_command VALUES(?,?,?,'pending',NULL,'')", ((identity, index, text) for index, text in enumerate(messages)))
            return dict(db.execute('SELECT * FROM qq_send_batch WHERE id=?', (identity,)).fetchone())

    def close(self) -> None:
        with self.lock:
            self.db.close()
