"""仅决定何时给群聊一次思考机会；是否发言仍由原规划模型判断。"""
import sqlite3
from typing import Any


def ready_thread(db: sqlite3.Connection, account: str, rows: Any, config: dict[str, Any], clock: float) -> Any:
    ready = []
    for row in rows:
        direct = row['direct_at'] is not None
        if row['thread'].startswith('private:'):
            due, priority = clock, 0
        else:
            gate = db.execute('SELECT last_started,last_direct FROM qq_group_gate WHERE account=? AND thread=?', (account, row['thread'])).fetchone()
            last, last_direct = (gate[0], gate[1]) if gate else (0, 0)
            # 防止系统时间回拨导致异常长时间冻结。
            if last > clock or last_direct > clock:
                last, last_direct = min(last, clock), min(last_direct, clock)
                db.execute('UPDATE qq_group_gate SET last_started=?,last_direct=? WHERE account=? AND thread=?',
                           (last, last_direct, account, row['thread']))
            if direct:
                due = max(row['direct_at'] + 2, last_direct + 10 if last_direct and config.get('direct_call_cooldown', True) else 0)
                priority = 1
            else:
                due = max(row['first_at'] + 10, last + config.get('group_interval_seconds', 60) if gate else 0)
                priority = 2
        if due <= clock:
            ready.append((priority, row['first_sequence'], row))
    return min(ready, key=lambda item: item[:2])[2] if ready else None


def started(db: sqlite3.Connection, work_id: str, clock: float) -> None:
    """实际首次规划才消费机会，quiet、表达/学习失败均不退回这次冷却。"""
    work = db.execute('SELECT * FROM qq_work WHERE id=?', (work_id,)).fetchone()
    if not work or work['started_at'] is not None:
        return
    db.execute('UPDATE qq_work SET started_at=? WHERE id=?', (clock, work_id))
    if work['trigger'] != 'message' or not work['thread'].startswith('group:'):
        return
    db.execute('INSERT INTO qq_group_gate(account,thread,last_started,last_direct) VALUES(?,?,?,?) '
        'ON CONFLICT(account,thread) DO UPDATE SET last_started=excluded.last_started,last_direct=CASE WHEN ? THEN excluded.last_direct ELSE qq_group_gate.last_direct END',
        (work['account'], work['thread'], clock, clock if work['direct_call'] else 0, work['direct_call']))
