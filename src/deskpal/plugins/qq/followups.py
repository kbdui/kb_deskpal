"""QQ 聊天后续跟进与认知复查；检查只入队，模型仍由宿主唯一 worker 调用。"""
from datetime import datetime, timedelta, timezone
import json
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from deskpal.workflow.models import WorkflowBlocked
from deskpal.workflow.store import canonical
from .social import text
from .store import now


class QqFollowups:
    def __init__(self, runtime: Any) -> None:
        self.runtime, self.store = runtime, runtime.store

    def schedule(self, context: Any, task_id: str, call_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        goal, reason = text(arguments['goal'], 2000), text(arguments['reason'], 1000)
        due = datetime.fromisoformat(text(arguments['due_at'], 64))
        clock = datetime.now(timezone.utc)
        if due.utcoffset() is None or not clock < due <= clock + timedelta(days=6):
            raise ValueError('QQ 跟进时间须带时区，且在未来六天内')
        mode = arguments['mode']
        config, _ = self.runtime.settings()
        if mode not in {'internal', 'chat'} or mode == 'chat' and not config['allow_proactive']:
            raise WorkflowBlocked('未预先允许主动 QQ 发言，本次未安排')
        identity = uuid5(NAMESPACE_URL, 'qq-followup:' + task_id + ':' + call_id).hex
        work = self.store.rows('SELECT planning_sources FROM qq_work WHERE id=?', (context.work_id,))[0]
        sources = json.loads(work['planning_sources'])
        principal = context.principal_id
        requested = arguments.get('requester_sequence')
        if requested is not None:
            messages = self.store.rows("SELECT speaker_id FROM qq_message WHERE sequence=? AND account=? AND thread=? AND direction='in'", (requested, context.account_id, context.conversation_id))
            if requested not in sources or not messages:
                raise ValueError('跟进请求人必须来自本轮实际规划消息')
            principal = messages[0]['speaker_id']
        stamp = due.astimezone(timezone.utc).isoformat()
        with self.store.transaction() as db:
            old = db.execute('SELECT * FROM qq_followup WHERE id=?', (identity,)).fetchone()
            if old:
                if (old['goal'], old['reason'], old['mode'], old['due_at'], old['principal']) != (goal, reason, mode, stamp, principal):
                    raise ValueError('跟进编号已绑定其他内容')
                return dict(id=identity, state=old['state'], due_at=stamp)
            db.execute("INSERT INTO qq_followup(id,account,thread,principal,goal,reason,mode,due_at,state,sources,created_at) VALUES(?,?,?,?,?,?,?,?,'pending',?,?)",
                (identity, context.account_id, context.conversation_id, principal, goal, reason, mode, stamp, canonical(sources), now()))
        return dict(id=identity, state='pending', due_at=stamp, mode=mode)

    def cancel(self, context: Any, identity: str) -> dict[str, Any]:
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM qq_followup WHERE id=? AND account=? AND thread=?', (identity, context.account_id, context.conversation_id)).fetchone()
            if not row:
                raise ValueError('当前会话不存在这项 QQ 跟进')
            db.execute("UPDATE qq_followup SET state='cancelled' WHERE id=?", (identity,))
            if row['work_id']:
                db.execute("UPDATE qq_work SET state='blocked',error='QQ 跟进已取消' WHERE id=? AND state IN ('queued','running')", (row['work_id'],))
        return dict(id=identity, state='cancelled', completed_sends_retained=True)

    def current(self, context: Any) -> list[dict[str, Any]]:
        return self.store.rows("SELECT id,goal,reason,mode,due_at,state,principal FROM qq_followup WHERE account=? AND thread=? AND state IN ('pending','running') ORDER BY due_at LIMIT 12", (context.account_id, context.conversation_id))

    def next_work(self, config: dict[str, Any]) -> dict[str, Any] | None:
        clock = now()
        # 失败只有限重试；当前会话已有工作时留待下个安全边界。
        candidates = self.store.rows("SELECT * FROM qq_followup WHERE account=? AND state='pending' AND due_at<=? AND attempts<3 ORDER BY due_at LIMIT 16", (config['account_id'], clock))
        for item in candidates:
            kind, target = item['thread'].split(':')
            if not self.runtime.permitted(config, kind, target) or item['mode'] == 'chat' and not config['allow_proactive']:
                with self.store.transaction() as db:
                    db.execute("UPDATE qq_followup SET state='blocked',error='现行授权不允许这项跟进' WHERE id=?", (item['id'],))
                continue
            if item['mode'] == 'internal' and self.runtime.app.config.memory.mode == 'paused':
                continue
            work = self._create(config['account_id'], item['thread'], item['principal'], 'followup' if item['mode'] == 'chat' else 'review')
            if work:
                with self.store.transaction() as db:
                    db.execute("UPDATE qq_followup SET state='running',attempts=attempts+1,work_id=? WHERE id=?", (work['id'], item['id']))
                return work
        if self.runtime.app.config.memory.mode == 'paused':
            return None
        due = self.store.rows("SELECT m.* FROM qq_social_memory m LEFT JOIN qq_review_attempt r ON r.memory_id=m.id WHERE m.account=? AND m.status='pending' AND (m.review_after IS NULL OR m.review_after<=?) AND (r.memory_id IS NULL OR r.revision!=m.revision OR r.next_attempt<=?) ORDER BY m.review_after LIMIT 16", (config['account_id'], clock, clock))
        for item in due:
            kind, target = item['thread'].split(':')
            if not self.runtime.permitted(config, kind, target):
                continue
            work = self._create(config['account_id'], item['thread'], 'system:qq-review', 'review')
            if work:
                with self.store.transaction() as db:
                    db.execute('UPDATE qq_work SET review_targets=? WHERE id=?', (canonical([item['id']]), work['id']))
                    old = db.execute('SELECT revision,attempts FROM qq_review_attempt WHERE memory_id=?', (item['id'],)).fetchone()
                    attempts = old['attempts'] + 1 if old and old['revision'] == item['revision'] else 1
                    delay = 15 * 4 ** min(attempts - 1, 3)
                    db.execute('INSERT INTO qq_review_attempt VALUES(?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET revision=excluded.revision,next_attempt=excluded.next_attempt,attempts=excluded.attempts',
                        (item['id'], item['revision'], (datetime.now(timezone.utc) + timedelta(minutes=delay)).isoformat(), attempts))
                return work
        return None

    def _create(self, account: str, thread: str, principal: str, trigger: str) -> dict[str, Any] | None:
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM qq_work WHERE account=? AND thread=? AND state IN ('queued','running')", (account, thread)).fetchone():
                return None
            identity = uuid4().hex
            db.execute("INSERT INTO qq_work(id,account,thread,principal,state,anchor,created_at,trigger) VALUES(?,?,?,?,'queued',0,?,?)", (identity, account, thread, principal, now(), trigger))
            return dict(db.execute('SELECT * FROM qq_work WHERE id=?', (identity,)).fetchone())

    def settle(self, work_id: str, completed: bool) -> None:
        with self.store.transaction() as db:
            row = db.execute("SELECT id,attempts FROM qq_followup WHERE work_id=? AND state='running'", (work_id,)).fetchone()
            if row:
                sent = db.execute('SELECT 1 FROM qq_send_batch b JOIN qq_work w ON w.task_id=b.task_id WHERE w.id=?', (work_id,)).fetchone()
                db.execute('UPDATE qq_followup SET state=?,due_at=?,error=? WHERE id=?',
                    ('completed' if completed else 'blocked' if sent else 'failed' if row['attempts'] >= 3 else 'pending',
                     (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(), '' if completed else '上次跟进未完成；已有发送组不自动重放，无发送时有限重试', row['id']))
