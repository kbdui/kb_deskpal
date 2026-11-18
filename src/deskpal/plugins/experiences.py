"""插件角色经历的通用接收箱：持久接收不等于已经形成长期记忆。"""
from hashlib import sha256
import json
from typing import Any

from deskpal.workflow.models import WorkflowBlocked
from deskpal.workflow.store import canonical


PURPOSE = 'plugin_experiences'


def receive(host: Any, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """只由宿主装配的插件提交；不发布聊天、不启动任务、不接受操作指令。"""
    app = host.app
    context = host.context(task_id)
    if context is None or payload.get('plugin_id') != context.plugin_id:
        raise WorkflowBlocked('插件经历与受管来源不一致')
    if (payload.get('account_id') != context.account_id
            or payload.get('source_conversation') != context.conversation_id):
        raise WorkflowBlocked('插件经历的账号或会话不一致')
    host.runtime(context).validate_experience(context, payload)
    body = canonical(payload)
    if len(body.encode()) > 16000:
        raise ValueError('插件经历超过接收上限')
    digest = sha256(body.encode()).hexdigest()
    with app.store.transaction() as db:
        app.store.check_privacy(db)
        task = db.execute('SELECT scrubbed FROM task WHERE id=?', (task_id,)).fetchone()
        if not task or task['scrubbed']:
            raise WorkflowBlocked('插件经历来源任务不可用')
        old = db.execute('SELECT digest,items FROM context_snapshot WHERE task_id=? AND purpose=?', (task_id, PURPOSE)).fetchone()
        if old and (old['digest'] != digest or old['items'] != body):
            raise WorkflowBlocked('同一经历投递编号不能替换正文')
        if not old:
            db.execute('INSERT INTO context_snapshot VALUES(?,?,?,?)', (task_id, PURPOSE, digest, body))
            app.store.set_dependency_version('plugin_experience', task_id, 1, use_policy=payload['use_policy'])
            app.store.record_event('plugin_experience_received', task_id, {'plugin_id': context.plugin_id})
    return dict(id=task_id, revision=1, digest=digest, status='received')


def read(app: Any, task_id: str) -> dict[str, Any] | None:
    with app.store.read_connection() as db:
        row = db.execute('SELECT s.items,s.digest FROM context_snapshot s JOIN task t ON t.id=s.task_id '
            'WHERE s.task_id=? AND s.purpose=? AND t.scrubbed=0', (task_id, PURPOSE)).fetchone()
    if row is None:
        return None
    if sha256(row['items'].encode()).hexdigest() != row['digest']:
        raise WorkflowBlocked('插件经历接收记录校验失败')
    return dict(json.loads(row['items']))
