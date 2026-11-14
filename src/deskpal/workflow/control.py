"""短事务控制任务暂停；不等待模型、工具或第二个执行者。"""
from hashlib import sha256
from typing import Any

from deskpal.tasks.models import Task, TaskStatus
from .store import WorkflowStore, canonical
from .tasks import PersistentTaskManager
from .models import WorkflowBlocked, QueueFull


class TaskControl:
    def __init__(self, store: WorkflowStore) -> None:
        self.store, self.tasks = store, PersistentTaskManager(store)

    def snapshot(self, task_id: str) -> dict[str, Any]:
        with self.store.read_connection() as db:
            row = db.execute('SELECT * FROM task_control WHERE task_id=?', (task_id,)).fetchone()
            return dict(row) if row else dict(task_id=task_id, requested=0, checkpoint='', elapsed=0.0)

    @staticmethod
    def _fingerprint(task_id: str, command_id: str, operation: str, payload: Any = None) -> str:
        if not isinstance(command_id, str) or not 1 <= len(command_id) <= 200:
            raise ValueError('Invalid task control command')
        values = [operation, task_id] if payload is None else [operation, task_id, payload]
        return sha256(canonical(values).encode()).hexdigest()

    def replayed(self, task_id: str, command_id: str, operation: str, payload: Any = None) -> bool:
        """历史回执仅证明受理，不恢复执行权，也不再次审查或执行旧操作。"""
        digest = self._fingerprint(task_id, command_id, operation, payload)
        with self.store.read_connection() as db:
            old = db.execute('SELECT fingerprint FROM task_control_receipt WHERE command_id=?', (command_id,)).fetchone()
            if old and old[0] != digest:
                raise ValueError('Task control command conflict')
            return old is not None

    def _receipt(self, task_id: str, command_id: str, operation: str, payload: Any = None) -> bool:
        digest = self._fingerprint(task_id, command_id, operation, payload)
        with self.store.transaction() as db:
            if db.execute('SELECT 1 FROM command WHERE id=? UNION SELECT 1 FROM conversation_receipt WHERE command_id=? UNION SELECT 1 FROM notification_policy_receipt WHERE command_id=?', (command_id, command_id, command_id)).fetchone():
                raise ValueError('Control command namespace conflict')
            old = db.execute('SELECT fingerprint FROM task_control_receipt WHERE command_id=?', (command_id,)).fetchone()
            if old:
                if old[0] != digest:
                    raise ValueError('Task control command conflict')
                return True
            if db.execute('SELECT COUNT(*) FROM task_control_receipt').fetchone()[0] >= 10000:
                raise WorkflowBlocked('Task control receipt capacity reached')
            db.execute('INSERT INTO task_control_receipt VALUES(?,?,?,?)', (command_id, digest, task_id, operation))
            return False

    def pause(self, task_id: str, command_id: str, *, active: bool = False) -> Task:
        with self.store.transaction() as db:
            self.store.check_privacy(db, task_id)
            task = self.tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if self._receipt(task_id, command_id, 'pause'):
                return task
            if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.PAUSED}:
                return task
            if task.status not in {TaskStatus.CREATED, TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.WAITING, TaskStatus.AWAITING_INPUT}:
                raise WorkflowBlocked('Task cannot be paused in this state')
            db.execute('INSERT OR IGNORE INTO task_control(task_id) VALUES(?)', (task_id,))
            db.execute('UPDATE task_control SET requested=1 WHERE task_id=?', (task_id,))
            db.execute('UPDATE task SET revision=revision+1 WHERE id=?', (task_id,))
            self.store.record_event('task_pause_requested', task_id)
            if not active and task.status in {TaskStatus.WAITING, TaskStatus.AWAITING_INPUT}:
                self.checkpoint(task_id, task.status.value, 0)
            return self.tasks.get(task_id) or task

    def cancel(self, task_id: str, command_id: str) -> Task:
        """取消与回执在同一事务提交，丢失网络响应后也可安全查询或重试。"""
        with self.store.transaction():
            task = self.tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if self._receipt(task_id, command_id, 'cancel'):
                return task
            return self.tasks.request_cancel(task_id)

    def checkpoint(self, task_id: str, mode: str, elapsed: float) -> bool:
        with self.store.transaction() as db:
            state = self.snapshot(task_id)
            task = self.tasks.get(task_id)
            if not state['requested'] or task is None or task.cancel_requested:
                return False
            db.execute('UPDATE task_control SET checkpoint=?,elapsed=elapsed+? WHERE task_id=?', (mode, elapsed, task_id))
            self.tasks.update_status(task_id, TaskStatus.PAUSED, result=task.result)
            self.tasks.finish_submission(task_id, 'Blocked')
            return True

    def resume(self, task_id: str, command_id: str) -> Task:
        with self.store.transaction() as db:
            self.store.check_privacy(db, task_id)
            task = self.tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if self._receipt(task_id, command_id, 'resume'):
                return task
            if task.status != TaskStatus.PAUSED or task.cancel_requested:
                raise WorkflowBlocked('Only a paused task may continue')
            self.store.check_dependencies(db, task_id)
            state = self.snapshot(task_id)
            if state['checkpoint'] == 'Waiting':
                from deskpal.planner.models import Plan
                from .journal import ExecutionJournal
                journal = ExecutionJournal(self.store)
                prior = journal.get_plan(task_id)
                if prior is None:
                    raise WorkflowBlocked('Missing authorization checkpoint')
                journal.save_round(task_id, Plan('恢复前重新核对未执行意图。', (), 'continue'), prior.tool_specs,
                    prior.workspace_dir, prior.memory_refs, replace_unstarted=True)
                db.execute("UPDATE task_control SET checkpoint='continue' WHERE task_id=?", (task_id,))
            status = TaskStatus.AWAITING_INPUT if state['checkpoint'] == 'AwaitingInput' else TaskStatus.CREATED
            count = db.execute("SELECT COUNT(*) FROM submission WHERE status IN('Queued','Claimed','Blocked') AND task_id!=?", (task_id,)).fetchone()[0]
            if count >= self.store.queue_capacity:
                raise QueueFull('Persistent submission queue is full')
            db.execute('UPDATE task_control SET requested=0 WHERE task_id=?', (task_id,))
            self.tasks.update_status(task_id, status, result=task.result)
            db.execute('UPDATE submission SET status=? WHERE task_id=?', ('Queued' if status == TaskStatus.CREATED else 'Blocked', task_id))
            self.store.record_event('task_continued', task_id)
            return self.tasks.get(task_id) or task
