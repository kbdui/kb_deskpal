"""主动聊天与提醒独立的持久暂停策略，不修改日程和系统权限。"""
from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from threading import RLock
from zoneinfo import ZoneInfo

from deskpal.workflow.store import WorkflowStore, canonical


class NotificationPolicy:
    def __init__(self, store: WorkflowStore, *, clock: Callable[[], datetime] | None = None,
                 quiet_start: str = "22:00", quiet_end: str = "08:00", zone: str = "UTC") -> None:
        self.store = store
        self.publication = RLock()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.zone = ZoneInfo(zone)
        def minute(value: str) -> int:
            hour, minutes = map(int, value.split(':'))
            return hour * 60 + minutes
        with store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO notification_settings VALUES(1,?,?,0)', (minute(quiet_start), minute(quiet_end)))

    def snapshot(self) -> dict[str, Any]:
        with self.publication, self.store.transaction() as db:
            now = self.clock().isoformat()
            db.execute('UPDATE notification_policy SET paused=0,until_at=NULL,revision=revision+1 WHERE paused=1 AND until_at IS NOT NULL AND until_at<=?', (now,))
            result = {row['kind']: dict(row) for row in db.execute('SELECT * FROM notification_policy')}
            result['quiet'] = dict(db.execute('SELECT quiet_start,quiet_end,reminder_bypass FROM notification_settings WHERE id=1').fetchone())
            result['quiet']['timezone'] = str(self.zone)
            return result

    def allowed(self, kind: str) -> bool:
        return not bool(self.snapshot()[kind]['paused'])

    def is_quiet(self, now: datetime | None = None) -> bool:
        config = self.snapshot()['quiet']
        local = (now or self.clock()).astimezone(self.zone)
        minute = local.hour * 60 + local.minute
        start, end = config['quiet_start'], config['quiet_end']
        return start != end and (start <= minute < end if start < end else minute >= start or minute < end)

    def reminder_allowed_now(self) -> bool:
        return self.allowed('reminder') and (bool(self.snapshot()['quiet']['reminder_bypass']) or not self.is_quiet())

    def set_quiet(self, quiet_start: int, quiet_end: int, reminder_bypass: bool,
                  command_id: str) -> dict[str, Any]:
        if (any(type(v) is not int or not 0 <= v < 1440 for v in (quiet_start, quiet_end))
                or type(reminder_bypass) is not bool or not isinstance(command_id, str) or not 1 <= len(command_id) <= 200):
            raise ValueError('Invalid quiet hours')
        digest = sha256(canonical(['quiet', quiet_start, quiet_end, reminder_bypass]).encode()).hexdigest()
        with self.publication, self.store.transaction() as db:
            self.store.check_privacy(db)
            if db.execute('SELECT 1 FROM command WHERE id=? UNION SELECT 1 FROM conversation_receipt WHERE command_id=? UNION SELECT 1 FROM task_control_receipt WHERE command_id=?', (command_id, command_id, command_id)).fetchone():
                raise ValueError('Notification command namespace conflict')
            old = db.execute('SELECT fingerprint FROM notification_policy_receipt WHERE command_id=?', (command_id,)).fetchone()
            if old and old[0] != digest:
                raise ValueError('Notification command conflict')
            if not old:
                db.execute('INSERT INTO notification_policy_receipt VALUES(?,?)', (command_id, digest))
                db.execute('UPDATE notification_settings SET quiet_start=?,quiet_end=?,reminder_bypass=? WHERE id=1',
                           (quiet_start, quiet_end, int(reminder_bypass)))
                self.store.record_event('notification_policy_changed', data={'kind': 'quiet'})
            return self.snapshot()

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
                    db.execute("UPDATE desktop_reminder_inbox SET delivery_state='missed' WHERE delivery_state IN ('due','quiet')")
                self.store.record_event('notification_policy_changed', data={'kind': kind, 'paused': paused})
            return self.snapshot()
