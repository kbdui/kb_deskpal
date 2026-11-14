"""P5 主动协调器：只安排机会；思考、工具和学习仍由唯一 worker 执行。"""
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any
from uuid import uuid4

from deskpal.tasks.models import TaskStatus
from deskpal.workflow.models import WorkflowBlocked
from deskpal.workflow.store import canonical
from .policy import NotificationPolicy


class ProactiveCoordinator:
    MIN_INTERVAL = 900
    MAX_ATTENTIONS = 1000

    def __init__(self, app: Any, policy: NotificationPolicy, *, clock: Callable[[], datetime] | None = None) -> None:
        self.app, self.store, self.policy = app, app.store, policy
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # 可信内置方法注册表；扩展方法只报告机会，不是插件执行框架。
        self.triggers: dict[str, Callable[[], None]] = {
            'event': self.collect, 'review': lambda: None,
            'opportunity': self.opportunity, 'fallback': self._review, 'maintenance': lambda: None,
            'reflection': lambda: None}
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO proactive_method VALUES('maintenance',1)")
            db.execute("INSERT OR IGNORE INTO proactive_method VALUES('reflection',1)")

    def register_trigger(self, name: str, trigger: Callable[[], None]) -> None:
        if not isinstance(name, str) or not 1 <= len(name) <= 32 or not name.replace('_', '').isalnum() or not callable(trigger):
            raise ValueError('Invalid internal trigger')
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO proactive_method VALUES(?,1)', (name,))
        self.triggers[name] = trigger

    def unregister_trigger(self, name: str) -> None:
        self.method(name, False)
        self.triggers.pop(name, None)

    def page(self, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid attention page')
        with self.store.read_connection() as db:
            self.store.check_privacy(db)
            rows = db.execute('SELECT id,goal AS title,basis AS description,method,due_at,expires_at,status,attempts,task_id,created_at FROM attention ORDER BY created_at DESC,id LIMIT ? OFFSET ?', (limit+1, offset)).fetchall()
            return dict(items=[dict(row) for row in rows[:limit]], offset=offset, has_more=len(rows)>limit)

    def method(self, name: str, enabled: bool) -> None:
        if name not in self.triggers or type(enabled) is not bool:
            raise ValueError('Invalid proactive trigger')
        with self.store.transaction() as db:
            db.execute('UPDATE proactive_method SET enabled=? WHERE name=?', (int(enabled), name))
            if not enabled:
                if name == 'fallback':
                    self._meta('pending_review', '')
                db.execute("UPDATE attention SET status='cancelled' WHERE method=? AND status IN ('pending','running')", (name,))

    def end_attention(self, id: str, expected_status: str, command_id: str) -> dict[str, Any]:
        """用户结束尚未开始的关注；状态核对与幂等回执同一事务提交。

        已在执行的事项通过任务控制取消，不能把“结束关注”假报为撤回操作。
        """
        if not isinstance(id, str) or not 1 <= len(id) <= 200 or expected_status != 'pending':
            raise ValueError('Invalid attention selection')
        if not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 200:
            raise ValueError('A stable command identity is required')
        key = 'attention_end:' + command_id
        result = dict(id=id, status='ended')
        with self.store.transaction() as db:
            self.store.check_privacy(db)
            old = db.execute('SELECT value FROM proactive_meta WHERE key=?', (key,)).fetchone()
            if old:
                if json.loads(old[0]) != result: raise WorkflowBlocked('Command identity already used')
                return result
            row = db.execute('SELECT status FROM attention WHERE id=?', (id,)).fetchone()
            if row is None or row[0] != expected_status:
                raise WorkflowBlocked('关注状态已变化，请重新读取；正在执行的任务请使用任务控制。')
            db.execute("UPDATE attention SET status='ended' WHERE id=? AND status='pending'", (id,))
            db.execute('INSERT INTO proactive_meta(key,value) VALUES(?,?)', (key, canonical(result)))
        return result

    def offer(self, *, key: str, method: str, goal: str, basis: str, refs: list[dict[str, Any]],
              due_at: str | None = None, expires_at: str | None = None) -> str:
        now = self.clock()
        if method not in self.triggers or not goal.strip() or len(goal.encode()) > 4096 or not basis.strip() or len(basis.encode()) > 4096:
            raise ValueError('Invalid attention')
        due = now if due_at is None else datetime.fromisoformat(due_at)
        end = now + timedelta(days=2) if expires_at is None else datetime.fromisoformat(expires_at)
        if due.tzinfo is None or end.tzinfo is None or not now <= due < end <= now + timedelta(days=7):
            raise ValueError('Attention requires a bounded aware time window')
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise ValueError('Invalid attention source key')
        for ref in refs:
            if (set(ref) != {'kind', 'id', 'revision'} or ref['kind'] not in {'proposal', 'memory', 'dialogue_turn', 'reflection'}
                    or not isinstance(ref['id'], str) or not 1 <= len(ref['id']) <= 200
                    or type(ref['revision']) is not int or ref['revision'] < 1):
                raise ValueError('Invalid attention reference')
        if not refs or len(refs) > 16 or len(canonical(refs).encode()) > 8192:
            raise ValueError('Attention requires bounded real sources')
        identity = uuid4().hex
        with self.store.transaction() as db:
            self.store.check_privacy(db)
            enabled = db.execute('SELECT enabled FROM proactive_method WHERE name=?', (method,)).fetchone()[0]
            if not enabled:
                raise WorkflowBlocked('Trigger disabled')
            old = db.execute('SELECT id,refs,status,basis FROM attention WHERE source_key=?', (key,)).fetchone()
            if old:
                if method == 'event' and old['status'] == 'pending':
                    merged = {canonical(ref): ref for ref in (*json.loads(old['refs']), *refs)}
                    selected = list(merged.values())[:16]
                    reason = old['basis'] if basis in old['basis'] else old['basis'] + '\n' + basis
                    if len(reason.encode()) > 4096:
                        reason = old['basis']
                    db.execute('UPDATE attention SET refs=?,basis=? WHERE id=?', (canonical(selected), reason, old['id']))
                return str(old[0])
            if db.execute('SELECT COUNT(*) FROM attention').fetchone()[0] >= self.MAX_ATTENTIONS:
                raise WorkflowBlocked('Attention capacity reached')
            db.execute('INSERT INTO attention(id,source_key,method,goal,basis,refs,due_at,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (identity, key, method, goal, basis, canonical(refs), due.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat(), now.isoformat()))
        return identity

    def schedule(self, task_id: str, goal: str, reason: str, due_at: str, mode: str = 'chat') -> str:
        due = datetime.fromisoformat(due_at)
        now = self.clock()
        due = max(due, now + timedelta(seconds=self.MIN_INTERVAL))
        origin = self.app.journal.origin(task_id)
        with self.store.read_connection() as db:
            own = db.execute('SELECT refs FROM attention WHERE task_id=?', (task_id,)).fetchone()
            turn = self.app._humanization.turn(task_id)
            if own:
                refs = json.loads(own[0])
            elif turn and origin['kind'] == 'user':
                refs = [dict(kind='dialogue_turn', id=task_id + ':user', revision=turn['sequence'])]
            else:
                from .reflection import read
                if read(self.app, task_id) is None:
                    raise WorkflowBlocked('No real attention source')
                refs = [dict(kind='reflection', id=task_id, revision=1)]
            if mode == 'reflection':
                from .reflection import read
                own = dict(kind='reflection', id=task_id, revision=1)
                if read(self.app, task_id) is not None:
                    refs = [own, *(ref for ref in refs if ref != own)][:16]
        with self.store.read_connection() as db:
            count = db.execute("SELECT COUNT(*) FROM attention WHERE refs=? AND created_at>=?", (canonical(refs), (now-timedelta(days=1)).isoformat())).fetchone()[0]
        if count >= 4:
            raise WorkflowBlocked('This source has reached the daily recheck budget')
        return self.schedule_followup('task:' + sha256(canonical([task_id, goal, reason, due_at, mode]).encode()).hexdigest(),
            goal=goal, reason=reason, due_at=due.isoformat(), mode=mode, refs=refs)

    def schedule_followup(self, command: str, *, goal: str, reason: str, due_at: str,
                          mode: str, refs: list[dict[str, Any]]) -> str:
        """聊天工具和反馈学习共用关注记录；内部维护不产生另一套情绪定时器。"""
        if mode not in {'internal', 'chat', 'reflection'}:
            raise ValueError('Invalid follow-up mode')
        now = self.clock()
        due = max(datetime.fromisoformat(due_at), now + timedelta(seconds=self.MIN_INTERVAL))
        clean = [dict(kind=ref['kind'], id=ref['id'], revision=ref['revision']) for ref in refs]
        key = 'followup:' + sha256(command.encode()).hexdigest()
        with self.store.read_connection() as db:
            old = db.execute('SELECT id FROM attention WHERE source_key=?', (key,)).fetchone()
            if old:
                return str(old[0])
            count = db.execute('SELECT COUNT(*) FROM attention WHERE refs=? AND created_at>=?',
                (canonical(clean), (now - timedelta(days=1)).isoformat())).fetchone()[0]
        if count >= 4:
            raise WorkflowBlocked('This source has reached the daily recheck budget')
        return self.offer(key=key, method={'internal': 'maintenance', 'chat': 'review', 'reflection': 'reflection'}[mode],
            goal=goal, basis=reason, refs=clean, due_at=due.isoformat(),
            expires_at=min(due + timedelta(days=1), now + timedelta(days=7)).isoformat())

    def _refresh_due_sources(self, row: dict[str, Any]) -> dict[str, Any]:
        """到期时换用最新依据；执行中仍严格校验冻结版本，避免旧结论提交。"""
        original = json.loads(row['refs'])
        refs = []
        for ref in original:
            if ref['kind'] == 'memory':
                item = self.app.learning.repository.follow_replacement(ref['id'])
                if item is not None and self.app._memory_runtime.valid(((item.id, item.revision),)):
                    refs.append({**ref, 'id': item.id, 'revision': item.revision})
            elif ref['kind'] == 'dialogue_turn':
                if self.app._humanization.reference_policy(ref['kind'], ref['id'], ref['revision']) is not None:
                    refs.append(ref)
            else:
                refs.append(ref)
        if refs != original:
            value = canonical(refs)
            basis = '原依据已有变更。请只根据当前可用来源重新判断是否继续、调整或结束此项跟进，不沿用旧结论。'
            with self.store.transaction() as db:
                updated = db.execute("UPDATE attention SET refs=?,basis=?,status=CASE WHEN ? THEN status ELSE 'invalidated' END WHERE id=? AND status='pending' AND refs=?",
                    (value, basis, int(bool(refs)), row['id'], row['refs']))
            if updated.rowcount:
                row = {**row, 'refs': value, 'basis': basis, 'status': row['status'] if refs else 'invalidated'}
        return row

    def valid(self, row: dict[str, Any]) -> bool:
        if row['status'] in {'invalidated', 'cancelled', 'expired'}:
            return False
        if row['method'] not in self.triggers or self.clock() >= datetime.fromisoformat(row['expires_at']):
            return False
        with self.store.read_connection() as db:
            if not db.execute('SELECT enabled FROM proactive_method WHERE name=?', (row['method'],)).fetchone()[0]:
                return False
        for ref in json.loads(row['refs']):
            if ref['kind'] == 'proposal':
                try:
                    proposal = self.app.proposals.preflight(ref['id'])
                    if proposal.use_policy == 'local_only' and self.app.config.llm.is_cloud:
                        return False
                except (ValueError, WorkflowBlocked):
                    return False
            elif ref['kind'] == 'memory':
                if not self.app._memory_runtime.valid(((ref['id'], ref['revision']),)):
                    return False
            else:
                policy = self.app._humanization.reference_policy(ref['kind'], ref['id'], ref['revision'])
                if ref['kind'] == 'reflection':
                    from .reflection import read
                    if read(self.app, ref['id']) is None:
                        return False
                if policy is None or (policy == 'local_only' and self.app.config.llm.is_cloud):
                    return False
        return True

    def _meta(self, key: str, value: str | None = None) -> str:
        with self.store.transaction() as db:
            if value is not None:
                db.execute('INSERT INTO proactive_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
            row = db.execute('SELECT value FROM proactive_meta WHERE key=?', (key,)).fetchone()
            return str(row[0]) if row else ''

    def collect(self) -> None:
        # 复用既有可靠事件/截止时间接收，不把规则提议当成用户授权。
        self.app.rules.poll()
        for item in self.app.proposals.list(limit=10):
            if self.clock() >= datetime.fromisoformat(item.expires_at):
                continue
            key = 'event:' + sha256(canonical([item.goal_id, item.goal_execution_version, item.milestone_id,
                int(datetime.fromisoformat(item.due_at).timestamp()) // self.MIN_INTERVAL]).encode()).hexdigest()
            self.offer(key=key, method='event', goal=item.request, basis=item.reason,
                refs=[dict(kind='proposal', id=item.id, revision=item.revision)], expires_at=item.expires_at)

    def opportunity(self) -> None:
        with self.store.read_connection() as db:
            rows = db.execute("SELECT t.id,r.sequence,t.created_at FROM task t JOIN turn_ref r ON r.task_id=t.id LEFT JOIN task_origin o ON o.task_id=t.id WHERE t.status='Completed' AND t.scrubbed=0 AND (o.kind IS NULL OR o.kind='user') AND EXISTS(SELECT 1 FROM attempt a WHERE a.task_id=t.id) ORDER BY t.updated_at DESC LIMIT 8").fetchall()
        for row in rows:
            # 只处理近期实际工作；重开不补播长期积压。
            if (self.clock() - datetime.fromisoformat(row['created_at'])).total_seconds() > 600:
                continue
            self.offer(key='opportunity:' + row['id'], method='opportunity',
                goal='结合这项实际工作的结果和最新交流，判断是否有值得主动表达的后续关心；无必要则安静结束。',
                basis='近期完成了一项用户发起的实际工作；这只是检查机会，不是再次执行原任务的要求。',
                refs=[dict(kind='dialogue_turn', id=row['id'] + ':user', revision=row['sequence'])])

    def _enqueue_review(self, identity: str) -> None:
        def run() -> None:
            # 入队之后可能开始遗忘；此时保留批次，恢复后重新核对，不能假记复查完成。
            if (self.app._closing or self.app._cleanup_pending
                    or self.app._humanization.privacy()['blocked']):
                return
            with self.store.read_connection() as db:
                enabled = db.execute("SELECT enabled FROM proactive_method WHERE name='fallback'").fetchone()[0]
            batch = self._meta('review_batch:' + identity)
            attention = json.loads(batch).get('attention_id') if batch else None
            if identity.startswith('proactive-review:') and batch and not json.loads(batch).get('refs'):
                return  # 旧“满八份”批次已撤销，不能在队列恢复时偷偷执行。
            if attention:
                with self.store.read_connection() as db:
                    row = db.execute('SELECT * FROM attention WHERE id=?', (attention,)).fetchone()
                enabled = bool(row and row['status'] == 'running' and self.valid(dict(row)))
            if enabled and not self.app._closing:
                self.app.learning.run_review(identity)
        self.app._dispatcher.enqueue_internal('learning:' + identity, run)

    def _review(self) -> None:
        now = self.clock()
        previous = self._meta('last_review')
        if not self._meta('pending_review') and previous and (now - datetime.fromisoformat(previous)).total_seconds() < self.MIN_INTERVAL:
            return
        pending = self._meta('pending_review')
        if pending:
            job = self.app.learning.store.job('review:' + pending)
            frozen = self._meta('review_batch:' + pending)
            if (frozen and not json.loads(frozen).get('refs')
                    and (not job or job['status'] not in {'completed', 'failed', 'cancelled'})):
                if job:
                    self.app.learning.store.finish(job['id'], 'cancelled', 'obsolete_material_threshold')
                with self.store.transaction() as db:
                    db.execute('DELETE FROM proactive_meta WHERE key=?', ('review_batch:' + pending,))
                    self._meta('pending_review', '')
                    self._meta('last_review', now.isoformat())
                return
            if job and job['status'] in {'completed', 'failed', 'cancelled'}:
                self._meta('pending_review', '')
                self._meta('last_review', now.isoformat())
                return
            else:
                self._enqueue_review(pending)
                return
        refs = self.app.learning.review_candidates(now)
        if refs:
            materials = self.app.learning.materials.select()
            identity = 'proactive-review:' + uuid4().hex
            self.freeze_review(identity, refs, materials=materials)
            self._meta('pending_review', identity)
            self._enqueue_review(identity)
        self._meta('last_review', now.isoformat())

    def freeze_review(self, identity: str, refs: list[dict[str, Any]], *, attention: dict[str, Any] | None = None,
                      materials: list[dict[str, Any]] | None = None) -> None:
        """触发和执行读取同一份有界标识、修订及目标，不在执行时重新选批。"""
        value = dict(refs=refs,
            attention_id=attention['id'] if attention else None,
            sources=json.loads(attention['refs']) if attention else [], materials=materials or [])
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO proactive_meta VALUES(?,?)', ('review_batch:' + identity, canonical(value)))

    def _maintenance(self) -> bool:
        """模型安排的内部维护也走原关注队列和唯一 Dispatcher，不受聊天免打扰影响。"""
        with self.store.read_connection() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM attention WHERE method='maintenance' AND status IN ('pending','running') AND due_at<=? ORDER BY due_at,id LIMIT 1", (self.clock().isoformat(),))]
        if not rows:
            return False
        item = self._refresh_due_sources(rows[0])
        identity = 'attention-review:' + item['id']
        job = self.app.learning.store.job('review:' + identity)
        if job and job['status'] in {'completed', 'failed', 'cancelled'}:
            with self.store.transaction() as db:
                db.execute('UPDATE attention SET status=? WHERE id=?', ('done' if job['status'] == 'completed' else 'failed', item['id']))
            return True
        if not self.valid(item):
            with self.store.transaction() as db:
                db.execute("UPDATE attention SET status='invalidated' WHERE id=?", (item['id'],))
            return True
        selected = [ref for ref in json.loads(item['refs']) if ref['kind'] == 'memory']
        known = {row['id'] for row in selected}
        relevant = self.app._interaction.retrieve(item['goal'])
        selected.extend(dict(kind='memory', id=identity, revision=revision)
            for identity, revision in relevant.revisions if identity not in known)
        known = {row['id'] for row in selected}
        selected.extend(ref for ref in self.app.learning.review_candidates(self.clock()) if ref['id'] not in known)
        self.freeze_review(identity, selected[:16], attention=item)
        with self.store.transaction() as db:
            # 用户可能已在上面的检索期间结束此关注，不能将其重新激活。
            changed = db.execute("UPDATE attention SET status='running' WHERE id=? AND status IN ('pending','running')", (item['id'],))
            if not changed.rowcount:
                return True
        self._enqueue_review(identity)
        return True

    def poll(self) -> None:
        app, now = self.app, self.clock()
        if app._closing or app._cleanup_pending or not app.config.proactive.enabled:
            return
        self.policy.snapshot()
        with self.store.read_connection() as db:
            available = {row['name']: bool(row['enabled']) for row in db.execute('SELECT * FROM proactive_method')}
        for name, trigger in tuple(self.triggers.items()):
            if name != 'fallback' and available.get(name, False):
                trigger()
        with self.store.transaction() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM attention WHERE status='running' LIMIT 32")]
            for row in rows:
                task = app.tasks.get(row['task_id'])
                if task and task.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.NEEDS_REVIEW, TaskStatus.UNCERTAIN}:
                    retry = task.status == TaskStatus.FAILED and row['attempts'] < 3 and not app.journal.attempts(task.id) and isinstance(task.result, dict) and task.result.get('retryable_model_failure', False)
                    db.execute('UPDATE attention SET status=?,due_at=? WHERE id=?',
                        ('pending' if retry else 'done', (now + timedelta(seconds=self.MIN_INTERVAL)).isoformat(), row['id']))
            db.execute("UPDATE attention SET status='expired' WHERE status='pending' AND expires_at<=?", (now.isoformat(),))
            methods = {row['name']: bool(row['enabled']) for row in db.execute('SELECT * FROM proactive_method')}
        dispatcher = app._dispatcher.diagnostics()
        if dispatcher['active_task_id'] or dispatcher['pending_operations'] or dispatcher['internal_active']:
            return
        if methods.get('maintenance', False) and app.config.memory.mode == 'automatic' and app.config.llm.ready and self._maintenance():
            return
        if methods.get('reflection', False) and app.config.llm.ready and self._think():
            return
        if methods.get('fallback', False) and 'fallback' in self.triggers and app.config.memory.mode == 'automatic' and app.config.llm.ready:
            self.triggers['fallback']()
        if not self.policy.allowed('chat') or not app.config.llm.ready or self.policy.is_quiet(now):
            return
        last = self._meta('last_chat')
        if last and (now - datetime.fromisoformat(last)).total_seconds() < self.MIN_INTERVAL:
            return
        with self.store.read_connection() as db:
            row = db.execute("SELECT * FROM attention WHERE status='pending' AND method NOT IN ('maintenance','reflection') AND due_at<=? ORDER BY due_at,id LIMIT 1", (now.isoformat(),)).fetchone()
        if row is None:
            return
        item = self._refresh_due_sources(dict(row))
        with self.store.read_connection() as db:
            daily = db.execute("SELECT COUNT(*) FROM task_origin o JOIN task t ON t.id=o.task_id WHERE o.kind='proactive' AND t.created_at>=?", ((now-timedelta(days=1)).isoformat(),)).fetchone()[0]
        if daily >= 16:
            return
        if not self.valid(item):
            with self.store.transaction() as db:
                db.execute("UPDATE attention SET status='invalidated' WHERE id=?", (item['id'],))
            return
        basis, source_policy = self._source_context(item)
        # 提交、任务来源、关注关联同一事务，唯一 worker 再执行。
        with app._dispatcher.control(), self.store.transaction() as db:
            current = db.execute('SELECT status FROM attention WHERE id=?', (item['id'],)).fetchone()
            if current[0] != 'pending':
                return
            task = app.submit_proactive(item['goal'], basis, 'attention:' + item['id'] + ':' + str(item['attempts']), use_policy=source_policy)
            app.journal.set_dependencies(task.id, tuple(ref for ref in json.loads(item['refs']) if ref['kind'] != 'proposal'))
            db.execute("UPDATE attention SET status='running',task_id=?,attempts=attempts+1 WHERE id=?", (task.id, item['id']))
            self._meta('last_chat', now.isoformat())

    def _source_context(self, item: dict[str, Any]) -> tuple[str, str]:
        sources: list[dict[str, Any]] = []
        policy = 'cloud_allowed'
        for ref in json.loads(item['refs']):
            source_policy: Any = None
            body: Any = None
            if ref['kind'] == 'memory':
                memory = self.app.learning.repository.get(ref['id'])
                if memory:
                    source_policy = memory.source.use_policy.value
                    body = dict(content=memory.draft.content, uncertainty=memory.draft.uncertainty, status=memory.status.value)
            elif ref['kind'] == 'proposal':
                proposal = self.app.proposals.preflight(ref['id'])
                source_policy = proposal.use_policy
                body = dict(reason=proposal.reason, request=proposal.request)
            elif ref['kind'] == 'reflection':
                from .reflection import read
                body = read(self.app, ref['id'])
                source_policy = body['use_policy'] if body else None
            else:
                source_policy = self.app._humanization.reference_policy(ref['kind'], ref['id'], ref['revision'])
                task = self.app.tasks.get(ref['id'].rsplit(':', 1)[0])
                if task:
                    body = dict(request=task.request, actual_result=task.result)
            if source_policy != 'cloud_allowed':
                policy = 'local_only'
            value = dict(ref=ref, data=body, instruction=False, grants_permission=False)
            if len(canonical(value).encode()) > 4096:
                value = dict(ref=ref, omitted=True, instruction=False)
            if len(canonical(sources + [value]).encode()) <= 6000:
                sources.append(value)
        basis = canonical(dict(reason=item['basis'], sources=sources))
        if len(basis.encode()) > 8192:
            basis = canonical(dict(reason=item['basis'], source_refs=json.loads(item['refs'])))
        return basis, policy

    def _think(self) -> bool:
        """低频空闲机会与模型安排共用预算，不把通知暂停等同于暂停内部思考。"""
        now = self.clock()
        with self.app._dispatcher.control(), self.store.transaction() as db:
            stamps = [s for s in json.loads(self._meta('reflection_runs') or '[]')
                if datetime.fromisoformat(s) > now - timedelta(days=1)]
            if len(stamps) >= 3 or (stamps and now - datetime.fromisoformat(stamps[-1]) < timedelta(hours=1)):
                return False
            due = self._meta('reflection_next')
            if not due:
                self._meta('reflection_next', (now + timedelta(hours=4)).isoformat())
            row = db.execute("SELECT * FROM attention WHERE method='reflection' AND status='pending' AND due_at<=? ORDER BY due_at,id LIMIT 1", (now.isoformat(),)).fetchone()
            if row:
                item = self._refresh_due_sources(dict(row))
                if not self.valid(item):
                    db.execute("UPDATE attention SET status='invalidated' WHERE id=?", (item['id'],))
                    return False
                basis, policy = self._source_context(item)
                goal = item['goal']
                key = 'reflection:' + item['id'] + ':' + str(item['attempts'])
            elif due and now >= datetime.fromisoformat(due):
                goal = '结合自己的身份、兴趣、相关经历与未完成的思考，自主选择一个值得探索的方向；没有新想法可以安静结束。'
                basis, policy = canonical(dict(purpose='reflection', opportunity='free', reason='低频自主思考机会，不是用户请求')), 'cloud_allowed'
                key = 'reflection:free:' + due
            else:
                return False
            # 预留次数与实际提交同一事务；失败回滚，不在重启后补做全部积压。
            task = self.app.submit_proactive(goal, basis, key, use_policy=policy)
            if row:
                self.app.journal.set_dependencies(task.id, tuple(json.loads(item['refs'])))
                db.execute("UPDATE attention SET status='running',task_id=?,attempts=attempts+1 WHERE id=? AND status='pending'", (task.id, item['id']))
            self._meta('reflection_runs', canonical(stamps + [now.isoformat()]))
            self._meta('reflection_next', (now + timedelta(hours=4)).isoformat())
            self.store.record_event('character_reflection_submitted', task.id)
            return True

    def validate_task(self, task_id: str, refs: Any = ()) -> None:
        with self.store.read_connection() as db:
            row = db.execute('SELECT * FROM attention WHERE task_id=?', (task_id,)).fetchone()
        if row is not None and not self.valid(dict(row)):
            raise WorkflowBlocked('Attention source expired or changed')

    def can_publish(self, task_id: str) -> bool:
        if self.app._closing or not self.policy.allowed('chat') or self.policy.is_quiet(self.clock()):
            return False
        with self.store.read_connection() as db:
            row = db.execute('SELECT * FROM attention WHERE task_id=?', (task_id,)).fetchone()
        return row is None or (row['status'] in {'running', 'done'} and self.valid(dict(row)))
