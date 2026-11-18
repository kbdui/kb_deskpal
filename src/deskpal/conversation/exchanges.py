"""保存任务中的实际问答；与任务状态、补充命令使用同一工作流事务。"""
import sqlite3
from deskpal.tasks.models import utc_now
from deskpal.workflow.models import WorkflowBlocked


def append(conn: sqlite3.Connection, task_id: str, identity: str, kind: str, text: str) -> None:
    if conn.execute('SELECT 1 FROM conversation_exchange WHERE id=?', (identity,)).fetchone():
        return
    link = conn.execute('SELECT conversation_id,epoch FROM turn_ref WHERE task_id=?', (task_id,)).fetchone()
    if link is None:
        return
    if link['conversation_id'].startswith('single:'):
        # 无持久会话的后台/API任务仍由task_amendment保存补充，不伪造聊天会话。
        return
    conversation = conn.execute('SELECT * FROM conversation WHERE id=?', (link['conversation_id'],)).fetchone()
    if (not conversation or conversation['deleted_at'] or conversation['archived_at']
            or conversation['context_epoch'] != link['epoch']):
        raise WorkflowBlocked('会话已变化，补充内容未写入；草稿已保留。')
    total = conn.execute('SELECT (SELECT COUNT(*) FROM conversation_item)+(SELECT COUNT(*) FROM conversation_exchange)').fetchone()[0]
    if total >= 100000:
        raise WorkflowBlocked('聊天记录容量已满，补充内容未写入。')
    policy = conn.execute('SELECT use_policy FROM task WHERE id=?', (task_id,)).fetchone()[0]
    conn.execute('INSERT INTO conversation_exchange(id,conversation_id,sequence,kind,task_id,body,use_policy,created_at) VALUES(?,?,?,?,?,?,?,?)',
        (identity, link['conversation_id'], conversation['next_sequence'], kind, task_id, text, policy, utc_now().isoformat()))
    conn.execute('UPDATE conversation SET next_sequence=next_sequence+1,timeline_revision=timeline_revision+1 WHERE id=?', (link['conversation_id'],))


def question(conn: sqlite3.Connection, task_id: str, text: str) -> None:
    count = conn.execute('SELECT COUNT(*) FROM decision_round WHERE task_id=?', (task_id,)).fetchone()[0]
    append(conn, task_id, task_id + ':question:' + str(count), 'assistant_message', text)


def supplement(conn: sqlite3.Connection, task_id: str, command_id: str, text: str) -> None:
    append(conn, task_id, 'supplement:' + command_id, 'user_message', text)
    # 原最终回复占位移到回答之后；历史澄清正文已单独保存，不被最终回复覆盖。
    link = conn.execute('SELECT conversation_id FROM turn_ref WHERE task_id=?', (task_id,)).fetchone()
    if link and not link[0].startswith('single:'):
        sequence = conn.execute('SELECT next_sequence FROM conversation WHERE id=?', (link[0],)).fetchone()[0]
        conn.execute("UPDATE conversation_item SET sequence=?,body='',revision=revision+1 WHERE id=? AND redacted_at IS NULL",
                     (sequence, task_id + ':assistant_message'))
        conn.execute('UPDATE conversation SET next_sequence=next_sequence+1,timeline_revision=timeline_revision+1 WHERE id=?', (link[0],))


def user_text(conn: sqlite3.Connection, task_id: str, original: str) -> str:
    rows = conn.execute("SELECT body FROM conversation_exchange WHERE task_id=? AND kind='user_message' AND redacted_at IS NULL ORDER BY sequence", (task_id,)).fetchall()
    return original + ''.join('\n\n[用户补充回答]\n' + row[0] for row in rows)
