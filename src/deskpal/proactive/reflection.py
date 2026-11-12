"""角色内部思考成果：复用任务快照持久化，不把草稿伪装成聊天。"""
import json
from hashlib import sha256
from typing import Any

from deskpal.workflow.models import WorkflowBlocked
from deskpal.workflow.store import canonical
from deskpal.tasks.models import TaskStatus


def read(app: Any, task_id: str) -> dict[str, Any] | None:
    with app.store.read_connection() as db:
        row = db.execute("SELECT s.items FROM context_snapshot s JOIN task t ON t.id=s.task_id "
            "WHERE s.task_id=? AND s.purpose='reflection' AND t.scrubbed=0", (task_id,)).fetchone()
    return json.loads(row[0]) if row else None


def record(app: Any, task_id: str, topic: str, content: str, reason: str) -> dict[str, Any]:
    if app.journal.origin(task_id)['kind'] != 'proactive':
        raise WorkflowBlocked('内部思考成果只允许主动任务提交')
    for text, limit in ((topic, 512), (content, 4000), (reason, 2000)):
        if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > limit:
            raise ValueError('思考成果过长或为空')
    role = app.core.companion_runtime.interaction_snapshot()
    with app.store.transaction() as db:
        app.store.check_privacy(db)
        task = app.tasks.get(task_id)
        if task is None or task.cancel_requested or task.status != TaskStatus.RUNNING:
            raise WorkflowBlocked('思考任务已取消')
        old = read(app, task_id)
        if old:
            if any(old[key] != value for key, value in (('topic', topic), ('content', content), ('reason', reason))):
                raise ValueError('每项任务只提交一份最终思考成果')
            return old
        value = dict(topic=topic, content=content, reason=reason, task_id=task_id,
            speaker_id='character:' + role['character_card']['id'], speaker_kind='character', speaker_name='当前角色',
            created_at=app.proactive.clock().isoformat(), use_policy=app._humanization.task_policy(task_id))
        body = canonical(value)
        db.execute('INSERT INTO context_snapshot VALUES(?,?,?,?)',
            (task_id, 'reflection', sha256(body.encode()).hexdigest(), body))
        app.store.set_dependency_version('reflection', task_id, 1, use_policy=value['use_policy'])
        app.store.record_event('character_reflection_recorded', task_id)
        return value
