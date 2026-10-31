"""主动聊天与提醒独立的持久暂停策略，不修改日程和系统权限。"""
from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from threading import RLock

from deskpal.workflow.store import WorkflowStore, canonical


class NotificationPolicy:
    def __init__(self, store: WorkflowStore, *, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.publication = RLock()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def snapshot(self) -> dict[str, Any]:
        with self.publication, self.store.transaction() as db:
            now = self.clock().isoformat()
            db.execute('UPDATE notification_policy SET paused=0,until_at=NULL,revision=revision+1 WHERE paused=1 AND until_at IS NOT NULL AND until_at<=?', (now,))
            return {row['kind']: dict(row) for row in db.execute('SELECT * FROM notification_policy')}

    def allowed(self, kind: str) -> bool:
        return not bool(self.snapshot()[kind]['paused'])

    def set(self, kind: str, paused: bool, until_at: str | None, command_id: str) -> dict[str, Any]:
        if kind not in {'chat', 'reminder'} or type(paused) is not bool or not isinstance(command_id, str) or not 1 <= len(command_id) <= 200:
            raise ValueError('Invalid notification control')
        if until_at is not None:
            until = datetime.fromisoformat(until_at)
            if until.tzinfo is None or until.utcoffset() is None or not paused:
                raise ValueError('Pause deadline requires an aware time')
            until_at = until.astimezone(timezone.utc).isoformat()
        digest = sha256(canonical([kind, paused, until_at]).encode()).hexdigest()
        with self.publication, self.store.transaction() as db:
            self.store.check_privacy(db)
            if db.execute('SELECT 1 FROM command WHERE id=? UNION SELECT 1 FROM conversation_receipt WHERE command_id=? UNION SELECT 1 FROM task_control_receipt WHERE command_id=?', (command_id, command_id, command_id)).fetchone():
                raise ValueError('Notification command namespace conflict')
            old = db.execute('SELECT fingerprint FROM notification_policy_receipt WHERE command_id=?', (command_id,)).fetchone()
            if old and old[0] != digest:
                raise ValueError('Notification command conflict')
            if not old:
                if db.execute('SELECT COUNT(*) FROM notification_policy_receipt').fetchone()[0] >= 10000:
                    raise ValueError('Notification receipt capacity reached')
                db.execute('INSERT INTO notification_policy_receipt VALUES(?,?)', (command_id, digest))
                db.execute('UPDATE notification_policy SET paused=?,until_at=?,revision=revision+1 WHERE kind=?', (int(paused), until_at, kind))
                if kind == 'reminder' and paused:
                    # 收件箱仍可读，不补弹暂停前尚未投递的提醒。
                    db.execute("UPDATE desktop_reminder_inbox SET delivery_state='missed' WHERE delivery_state='due'")
                self.store.record_event('notification_policy_changed', data={'kind': kind, 'paused': paused})
            return self.snapshot()
