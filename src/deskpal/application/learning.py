"""应用层同步反馈与独立整理入口；模型等待期间不持数据库或发布锁。"""
from dataclasses import replace, asdict
from datetime import datetime, timezone, timedelta
import json
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from deskpal.agent.context import PreparedTurn
from deskpal.conversation.models import ConversationRef
from deskpal.llm.errors import RetryableLLMError
from deskpal.memory.contracts import ClaimType, MemoryDraft, MemoryItem, MemorySource, MemoryStatus, UsePolicy, RetrievalBundle
from deskpal.memory.coordinator import MemoryCoordinator
from deskpal.memory.feedback import evaluate, moment
from deskpal.memory.learning_store import LearningStore
from deskpal.memory.models import MemoryKind
from deskpal.memory.retrieval import _lexical
from deskpal.workflow.models import WorkflowBlocked, TaskCancellationRequested
from deskpal.tasks.models import TaskStatus

if TYPE_CHECKING:
    from .service import ApplicationService


class FeedbackLearning:
    def __init__(self, app: 'ApplicationService', memory: MemoryCoordinator) -> None:
        self.app, self.memory = app, memory
        # 生产装配保证具体持久化适配器；Core 只依赖 TurnFeedback 协议。
        from deskpal.memory.repository import SQLiteMemoryRepository
        if not isinstance(memory.repository, SQLiteMemoryRepository):
            raise ValueError('Feedback requires the owned memory repository')
        self.repository = memory.repository
        self.store = LearningStore(self.repository)
        # 旧版已明确保存的内容也补接真实消息关联；不推测缺失会话。
        for item in memory.list_items():
            if item.status == MemoryStatus.DELETED or item.source.origin not in ('user_message', 'user_confirmation') or not item.source.task_id:
                continue
            link = app._humanization.turn(item.source.task_id)
            if link:
                self.store.link(item.id, [dict(kind='dialogue_turn', id=item.source.task_id + ':user',
                    revision=link['sequence'], conversation_id=link['conversation_id'], available=True)])

    def _source_valid(self, refs: list[dict[str, Any]]) -> None:
        for ref in refs:
            if ref['kind'] == 'memory':
                item = self.repository.get(ref['id'])
                if item is None or item.status == MemoryStatus.DELETED or item.revision != ref['revision']:
                    raise WorkflowBlocked('Learning source changed')
            elif ref['kind'] == 'dialogue_turn' and ref.get('available', True):
                policy = self.app._humanization.reference_policy('dialogue_turn', ref['id'], ref['revision'])
                if policy is None:
                    # 本轮用户消息尚未发布，但真实任务和本轮归属已由宿主登记。
                    root = ref['id'].rsplit(':', 1)[0]
                    task = self.app.tasks.get(root)
                    turn = self.app._humanization.turn(root)
                    current = (ref['id'].endswith(':user') and task is not None
                        and task.status in {TaskStatus.CREATED, TaskStatus.PLANNING, TaskStatus.RUNNING}
                        and turn is not None and turn['sequence'] == ref['revision'])
                    policy = self.app._humanization.reference_policy('dialogue_turn', root, 1) if current else None
                if policy is None or (policy == 'local_only' and not self.app._interaction.allow_restricted):
                    raise WorkflowBlocked('Learning original message is no longer available')

    def recover(self) -> None:
        """只投递已提交的角色调整，绝不重新运行来源任务或盲目覆盖新角色。"""
        companion = self.app.core.companion_runtime
        apply = getattr(companion, 'apply_learning')
        for row in self.store.pending_effects():
            try:
                with self.app._privacy.publication(row['privacy_epoch']):
                    self._source_valid(json.loads(row['refs']))
                    body = json.loads(row['payload'])
                    followups = body.pop('followups', [])
                    if body['source'].get('memory_epoch') != self.memory.epoch:
                        raise WorkflowBlocked('Learning sources changed before role delivery')
                    if not row['conversation_id'].startswith('single:') and self.app._conversations.context_epoch(row['conversation_id']) != row['conversation_epoch']:
                        raise WorkflowBlocked('Learning conversation changed')
                    if body['habits'] or body['emotion'] is not None:
                        apply('learning:' + row['job_id'], **body)
                    for index, followup in enumerate(followups):
                        self.app.proactive.schedule_followup('learning:' + row['job_id'] + ':' + str(index), **followup)
                    self.store.ack(row['job_id'], valid=True)
            except (ValueError, WorkflowBlocked):
                self.store.ack(row['job_id'], valid=False)

    def review_candidates(self, now: datetime) -> list[dict[str, Any]]:
        """全体有效记录中选有界批次；已看修订和尝试时间分开，遗漏项不会被假记完成。"""
        coverage = json.loads(self.app.proactive._meta('review_coverage') or '{}')
        attempts = json.loads(self.app.proactive._meta('review_attempts') or '{}')
        candidates: list[MemoryItem] = []
        for item in self.memory.list_items():
            if (item.status not in (MemoryStatus.CANDIDATE, MemoryStatus.DISPUTED, MemoryStatus.ACCEPTED)
                    or item.draft.kind not in (MemoryKind.SEMANTIC, MemoryKind.EPISODIC)
                    or (item.draft.expires_at is not None and item.draft.expires_at <= now)
                    or (item.draft.review_after is not None and item.draft.review_after > now)
                    or (not self.app._interaction.allow_restricted and item.source.use_policy != UsePolicy.CLOUD_ALLOWED)):
                continue
            seen = coverage.get(item.id, {})
            attempted = attempts.get(item.id, {})
            if attempted.get('revision') == item.revision and attempted.get('count', 0) >= 3:
                continue  # 未处理不假记完成；无变化的同一修订也不能无限兜底调用。
            due = item.draft.review_after is not None and item.draft.review_after.timestamp() > seen.get('at', 0)
            if seen.get('revision') != item.revision or due:
                candidates.append(item)
        candidates.sort(key=lambda item: (attempts.get(item.id, {}).get('at', 0),
            0 if item.draft.review_after is not None else 1,
            item.draft.review_after or item.recorded_at, item.id))
        return [dict(kind='memory', id=item.id, revision=item.revision) for item in candidates[:16]]

    def _input(self, task_id: str, request: str, response: str, observations: tuple[dict[str, Any], ...],
               context: PreparedTurn, batch: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, MemoryItem]]:
        catalog: dict[str, Any] = {}
        refs: list[dict[str, Any]] = []
        now = self.app.proactive.clock()
        source_task = self.app.tasks.get(task_id)
        created_at = source_task.created_at.isoformat() if source_task else now.isoformat()
        current_ref = dict(kind='dialogue_turn', id=task_id + ':user', revision=max(1, context.sequence),
                           conversation_id=context.conversation_id, available=True)
        catalog['user'] = dict(content=request, role='user', origin='user_message', refs=[current_ref], use_policy=context.use_policy, created_at=created_at)
        refs.append(current_ref)
        if context.origin == 'proactive':
            catalog.pop('user', None)
            refs.remove(current_ref)
        # 助手草稿可供理解，不可作为独立事实或习惯的授权依据。
        catalog['assistant'] = dict(content=response, role='assistant', origin='assistant_output', refs=[])
        for index, entry in enumerate(context.history):
            source = entry.get('source_ref')
            if not source:
                continue
            ref = dict(kind='dialogue_turn', id=source['id'], revision=source['revision'],
                conversation_id=context.conversation_id, available=True, use_policy=source.get('use_policy', context.use_policy))
            historical_task = self.app.tasks.get(source['id'].rsplit(':', 1)[0])
            catalog[f'history{index}'] = dict(content=str(entry.get('content', '')), role=entry.get('role', 'assistant'),
                origin='user_message' if entry.get('role') == 'user' else 'assistant_output', refs=[ref],
                use_policy=source.get('use_policy', context.use_policy), created_at=historical_task.created_at.isoformat() if historical_task else created_at)
            refs.append(ref)
        for index, observation in enumerate(observations):
            content = json.dumps(observation, ensure_ascii=False, allow_nan=False)
            if len(content.encode('utf-8')) > 6000:
                content = json.dumps({key: observation.get(key) for key in ('tool_name', 'call_id', 'success', 'outcome')})
            ref = dict(kind='tool_result', id=task_id + ':' + str(index), revision=1,
                conversation_id=context.conversation_id, available=True)
            catalog[f'tool{index}'] = dict(content=content, role='tool', origin='tool_result', refs=[ref], use_policy=context.use_policy, created_at=created_at)
            refs.append(ref)
        candidates = [item for item in self.memory.list_items() if item.status in
            (MemoryStatus.CANDIDATE, MemoryStatus.DISPUTED, MemoryStatus.ACCEPTED)
            and item.draft.kind in (MemoryKind.SEMANTIC, MemoryKind.EPISODIC)
            and (item.draft.expires_at is None or item.draft.expires_at > now)
            and (self.app._interaction.allow_restricted or item.source.use_policy == UsePolicy.CLOUD_ALLOWED)]
        if request:
            candidates.sort(key=lambda item: (-_lexical(request, item.draft.content),
                0 if item.status != MemoryStatus.ACCEPTED else 1, -item.recorded_at.timestamp()))
        elif batch is None:
            candidates = [item for item in candidates if item.draft.review_after is None or item.draft.review_after <= now]
            candidates.sort(key=lambda item: (item.draft.review_after or item.recorded_at, item.id))
        selected = candidates[:16]
        if batch is not None:
            selected = []
            eligible = {item.id: item for item in candidates}
            for ref in batch['refs']:
                item = eligible.get(ref['id'])
                if item is None or item.revision != ref['revision']:
                    raise WorkflowBlocked('Frozen review target changed or became unavailable')
                selected.append(item)
        items = {item.id: item for item in selected}
        understandings: list[dict[str, Any]] = []
        for index, item in enumerate(selected):
            ref = dict(kind='memory', id=item.id, revision=item.revision, conversation_id=item.source.session_id, available=True)
            refs.append(ref)
            original_refs = self.store.refs(item.id)
            available = item.source.content != '[原消息已清除]'
            catalog[f'memory{index}'] = dict(content=item.source.content if available else '', role='evidence',
                origin=item.source.origin, refs=[ref], available=available, use_policy=item.source.use_policy.value)
            catalog[f'memory{index}']['created_at'] = item.source.created_at.isoformat()
            understandings.append(dict(id=item.id, revision=item.revision, status=item.status.value,
                content=item.draft.content, destination=item.draft.destination, claim_type=item.draft.claim_type.value,
                reason=item.draft.reason, uncertainty=item.draft.uncertainty,
                review_after=item.draft.review_after.isoformat() if item.draft.review_after else None,
                valid_from=item.draft.valid_from.isoformat() if item.draft.valid_from else None,
                valid_to=item.draft.valid_to.isoformat() if item.draft.valid_to else None,
                original_source_key=f'memory{index}', source_refs=original_refs))
        previous = self.app._conversation_repository.context_summary(context.conversation_id, context.conversation_epoch,
            allow_restricted=self.app._interaction.allow_restricted) if not context.conversation_id.startswith('single:') else None
        if previous and not previous.get('unavailable'):
            refs.extend(dict(kind=ref['kind'], id=ref['id'], revision=ref['revision'],
                conversation_id=context.conversation_id, available=True) for ref in previous['refs'])
        payload = dict(catalog=catalog, understandings=understandings, current_exchange=dict(request=request,
            response=response, assistant_is_proposal=True), role=json.loads(context.role_json),
            published_history=list(context.history), previous_summary=previous, now=now.isoformat())
        if batch is not None:
            payload['review_goal'] = batch.get('goal')
        # 宿主恢复实际可读原消息；模型旧理由不能冒充新的依据。
        source_refs = [ref for item in selected for ref in self.store.refs(item.id)]
        if batch is not None:
            source_refs.extend(batch['sources'])
        emotion = json.loads(context.role_json).get('current_emotion') if context.role_json else None
        if emotion:
            source_refs.extend(emotion['source'].get('refs', []))
        for ref in source_refs[:64]:
            if ref['kind'] != 'dialogue_turn' or not ref.get('available', True):
                continue
            policy = self.app._humanization.reference_policy(ref['kind'], ref['id'], ref['revision'])
            if policy is None or (policy == 'local_only' and not self.app._interaction.allow_restricted):
                continue
            source_task = self.app.tasks.get(ref['id'].rsplit(':', 1)[0])
            if source_task is None:
                continue
            key = 'source:' + ref['id']
            if key in catalog:
                continue
            is_user = ref['id'].endswith(':user')
            content = source_task.request if is_user else str((source_task.result or {}).get('response', ''))
            if not content or len(content.encode('utf-8')) > 8000:
                continue
            value = dict(content=content, role='user' if is_user else 'assistant',
                origin='user_message' if is_user else 'assistant_output', refs=[ref],
                available=True, use_policy=policy, created_at=source_task.created_at.isoformat())
            if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) + len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > 85000:
                continue
            catalog[key] = value
            refs.append(ref)
        # 整个历史单元按预算省略；不截断原话或否定词。
        while len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > 90000 and understandings:
            removed = understandings.pop()
            catalog.pop(removed['original_source_key'])
            items.pop(removed['id'])
            refs = [ref for ref in refs if not (ref['kind'] == 'memory' and ref['id'] == removed['id'])]
        return payload, refs, items

    def resume_work(self) -> None:
        """唯一后台的安全空闲边界：恢复未提交的评估，不重放来源工具。"""
        for job in self.store.recoverable():
            if self.app._closing:
                return
            if job['attempts'] >= 3:
                self.store.finish(job['id'], 'failed', 'retry_budget_exhausted')
                continue
            try:
                self.app._privacy.check(job['privacy_epoch'])
                self._source_valid(json.loads(job['refs']))
                task = self.app.tasks.get(job['task_id'])
                if task is None or task.status in (TaskStatus.CANCELLED, TaskStatus.FAILED):
                    raise WorkflowBlocked('Learning source task is unavailable')
                turn = self.app._interaction.prepare(task.id, task.request, RetrievalBundle(epoch=self.memory.epoch))
                snapshot = self.app.journal.get_plan(task.id)
                response = str((task.result or {}).get('response', snapshot.plan.reply if snapshot else ''))
                observations = tuple(asdict(attempt.result) for attempt in self.app.journal.attempts(task.id) if attempt.result)
                self._run(job['id'], 'turn', task.id, task.request, response, observations, turn)
            except (ValueError, WorkflowBlocked):
                self.store.finish(job['id'], 'cancelled', 'recovery_source_unavailable')
            finally:
                self.app._interaction.discard_tasks((job['task_id'],))

    @staticmethod
    def _references(row: dict[str, Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for key in row['sources']:
            for ref in catalog[key]['refs']:
                if ref not in result:
                    result.append(ref)
        return result

    def _draft(self, row: dict[str, Any], catalog: dict[str, Any], context: PreparedTurn) -> tuple[MemorySource, MemoryDraft]:
        evidence = next((catalog[key] for key in row['sources'] if catalog[key].get('role') != 'assistant'
            and catalog[key].get('available', True) and row['quote'] in catalog[key]['content']), None)
        if evidence is None:
            raise ValueError('Learning quote has no available original evidence')
        from deskpal.memory.coordinator import _content
        _content(row['content'])
        _content(row['quote'])
        policy = ('local_only' if context.use_policy == 'local_only' or any(catalog[key].get('use_policy') == 'local_only'
                  for key in row['sources']) else 'cloud_allowed')
        source = MemorySource(row['quote'], id=uuid4().hex, origin=evidence['origin'], task_id=context.task_id,
            session_id=context.conversation_id, use_policy=UsePolicy(policy), created_at=datetime.fromisoformat(evidence['created_at']))
        draft = MemoryDraft(row['content'], row['quote'], claim_type=ClaimType(row['claim_type']),
            kind=MemoryKind.EPISODIC if row['destination'] == 'episode' else MemoryKind.SEMANTIC,
            destination=row['destination'], reason=row['reason'], uncertainty=row['uncertainty'],
            review_after=moment(row['review_after']), valid_from=moment(row['valid_from']), valid_to=moment(row['valid_to']))
        return source, draft

    def finish(self, *, task_id: str, request: str, response: str,
               observations: tuple[dict[str, Any], ...], context: PreparedTurn | None) -> tuple[str, ...]:
        if context is None or self.memory.mode == 'paused':
            return ()
        # 管理工具可能已刷新本轮人格，使用最新的宿主快照。
        context = self.app._interaction.current_turn(task_id)
        if not context.conversation_id:
            # 独立任务保留真实的内部会话归属，不查询不存在的持久会话摘要。
            link = self.app._humanization.turn(task_id)
            if link is None or not link['conversation_id'].startswith('single:'):
                raise WorkflowBlocked('Missing standalone turn identity')
            context = replace(context, conversation_id=link['conversation_id'])
        return self._run('turn:' + task_id, 'turn', task_id, request, response, observations, context)

    def review(self, command_id: str, context: PreparedTurn, batch: dict[str, Any] | None = None) -> tuple[str, ...]:
        """P5/独立整理复用同一协议；不发布聊天、不调用来源工具。"""
        if not command_id or len(command_id) > 200:
            raise ValueError('Review requires stable work identity')
        return self._run('review:' + command_id, 'review', context.task_id, '', '', (), context, batch)

    def run_review(self, command_id: str) -> None:
        """由唯一 Dispatcher 调用；内部工作不发布消息，也没有工具执行能力。"""
        prior = self.store.job('review:' + command_id)
        if prior and prior['status'] in ('completed', 'cancelled'):
            return
        saved = self.app.proactive._meta('review_batch:' + command_id)
        if not saved:
            self.app.proactive.freeze_review(command_id, self.review_candidates(self.app.proactive.clock()))
            saved = self.app.proactive._meta('review_batch:' + command_id)
        batch = json.loads(saved)
        if batch.get('attention_id'):
            with self.app.store.read_connection() as db:
                attention = db.execute('SELECT * FROM attention WHERE id=?', (batch['attention_id'],)).fetchone()
            if attention is None or not self.app.proactive.valid(dict(attention)) or attention['status'] != 'running':
                return
            batch['goal'] = attention['goal']
        task = self.app.tasks.get(prior['task_id']) if prior else None
        task = task or self.app.tasks.create('内部维护复查')
        try:
            if task.status == TaskStatus.CREATED:
                self.app.tasks.update_status(task.id, TaskStatus.PLANNING)
                self.app.tasks.update_status(task.id, TaskStatus.RUNNING)
            role = getattr(self.app.core.companion_runtime, 'context')('single:' + task.id, 0, task.id,
                allow_restricted=self.app._interaction.allow_restricted)
            context = PreparedTurn(task.id, self.app._humanization.privacy()['epoch'],
                conversation_id='single:' + task.id, role_id=role['role_id'], role_revision=role['revision'],
                role_json=json.dumps(role, ensure_ascii=False), profile_id=self.app._interaction.profile_id,
                runtime_id=self.app._interaction.runtime_id)
            warnings = self.review(command_id, context, batch)
            attempts = json.loads(self.app.proactive._meta('review_attempts') or '{}')
            stamp = self.app.proactive.clock().timestamp()
            for ref in batch['refs']:
                old = attempts.get(ref['id'], {})
                attempts[ref['id']] = dict(revision=ref['revision'], at=stamp,
                    count=old.get('count', 0) + 1 if old.get('revision') == ref['revision'] else 1)
            self.app.proactive._meta('review_attempts', json.dumps(attempts))
            coverage = json.loads(self.app.proactive._meta('review_coverage') or '{}')
            for row in self.store.records(100):
                if row['job_id'] == 'review:' + command_id and row['memory_id'] and row['action'] in {'retain', 'adopt', 'update', 'archive', 'merge'}:
                    current = self.repository.get(row['memory_id'])
                    if current:
                        coverage[current.id] = dict(revision=current.revision, at=stamp)
            self.app.proactive._meta('review_coverage', json.dumps(coverage))
            with self.app.store.transaction() as db:
                db.execute('DELETE FROM proactive_meta WHERE key=?', ('review_batch:' + command_id,))
            self.app.tasks.update_status(task.id, TaskStatus.COMPLETED, result={'internal_learning': True, 'warnings': list(warnings)})
        except Exception:
            if self.store.job('review:' + command_id) is None:
                self.store.register('review:' + command_id, kind='review', task_id=task.id,
                    conversation_id='single:' + task.id, conversation_epoch=0,
                    privacy_epoch=self.app._humanization.privacy()['epoch'], role_revision=0, refs=batch['refs'])
                self.store.finish('review:' + command_id, 'cancelled', 'frozen_source_unavailable')
            self.app.tasks.update_status(task.id, TaskStatus.FAILED, error='内部整理未完成')

    def _run(self, identity: str, kind: str, task_id: str, request: str, response: str,
             observations: tuple[dict[str, Any], ...], context: PreparedTurn, batch: dict[str, Any] | None = None) -> tuple[str, ...]:
        source_epoch = self.memory.epoch
        payload, refs, items = self._input(task_id, request, response, observations, context, batch)
        if kind == 'review':
            payload['catalog'].pop('user', None)
            payload['catalog'].pop('assistant', None)
            refs = [ref for ref in refs if not ref['id'].startswith(task_id + ':')]
        job = self.store.register(identity, kind=kind, task_id=task_id, conversation_id=context.conversation_id,
            conversation_epoch=context.conversation_epoch, privacy_epoch=context.privacy_epoch,
            role_revision=context.role_revision, refs=refs)
        if job['status'] in ('completed', 'cancelled'):
            return ()
        def guard() -> None:
            self.app._privacy.check(context.privacy_epoch)
            if self.memory.epoch != source_epoch:
                raise WorkflowBlocked('Learning evidence changed while the model was working')
            if self.app._closing:
                raise TaskCancellationRequested('Application closing')
            if kind == 'turn':
                self.app._interaction.check(task_id)
            else:
                role = getattr(self.app.core.companion_runtime, 'context')(context.conversation_id, 0, task_id,
                    allow_restricted=self.app._interaction.allow_restricted)
                if role['revision'] != context.role_revision:
                    raise WorkflowBlocked('Role changed during review')
            self._source_valid(refs)
            if kind == 'review' and (identity.startswith('review:proactive-review:') or (batch and batch.get('attention_id'))):
                if not self.app.config.proactive.enabled:
                    raise WorkflowBlocked('Automatic follow-up disabled during review')
                if identity.startswith('review:proactive-review:'):
                    with self.app.store.read_connection() as db:
                        enabled = db.execute("SELECT enabled FROM proactive_method WHERE name='fallback'").fetchone()[0]
                    if not enabled:
                        raise WorkflowBlocked('Review trigger disabled during evaluation')
            if batch and batch.get('attention_id'):
                with self.app.store.read_connection() as db:
                    attention = db.execute('SELECT * FROM attention WHERE id=?', (batch['attention_id'],)).fetchone()
                if attention is None or attention['status'] != 'running' or not self.app.proactive.valid(dict(attention)):
                    raise WorkflowBlocked('Scheduled maintenance changed while reviewing')
        for _ in range(max(0, 3 - job['attempts'])):
            self.store.attempt(identity)
            try:
                guard()
                provider = (self.app._interaction.calls(task_id, 'memory_evaluation') if kind == 'turn' else
                            self.app._interaction.gateway.bind(task_id, 'memory_evaluation', guard))
                result = evaluate(provider, payload)
                guard()
                self._commit(identity, result, payload, refs, items, context, guard)
                return ()
            except RetryableLLMError:
                self.store.finish(identity, 'failed', 'temporary_model_failure')
            except (WorkflowBlocked, TaskCancellationRequested):
                self.store.finish(identity, 'cancelled', 'source_or_task_invalidated')
                raise
            except Exception:
                current = self.store.job(identity)
                if current and current['status'] == 'completed':
                    return ('记忆已保存，后续状态投递将在后台恢复时核对。',)
                self.store.finish(identity, 'failed', 'evaluation_or_commit_failed')
                return ('本轮记忆整理未完成，已保留工作记录；实际操作和回复正常提交。',)
        return ('本轮记忆整理重试已用尽；实际操作和回复正常提交。',)

    def _commit(self, identity: str, result: dict[str, Any], payload: dict[str, Any], refs: list[dict[str, Any]],
                items: dict[str, MemoryItem], context: PreparedTurn, guard: Any) -> None:
        catalog = payload['catalog']
        changed: dict[str, tuple[str, int]] = {}
        role_habits: list[dict[str, Any]] = []
        followups: list[dict[str, Any]] = []
        for row in result.get('followups', []):
            due = moment(row['due_at'])
            if due is None or due > self.app.proactive.clock() + timedelta(days=6):
                raise ValueError('Follow-up is outside the supported check window')
            actual = self._references(row, catalog)
            if any(ref['kind'] == 'tool_result' for ref in actual):
                if context.origin == 'user':
                    actual.append(dict(kind='dialogue_turn', id=context.task_id + ':user', revision=max(1, context.sequence)))
                else:
                    with self.app.store.read_connection() as db:
                        parent = db.execute('SELECT refs FROM attention WHERE task_id=?', (context.task_id,)).fetchone()
                    if parent:
                        actual.extend(json.loads(parent['refs']))
            actual = [ref for ref in actual if ref['kind'] in {'memory', 'dialogue_turn'} and ref.get('available', True)]
            actual = list({(ref['kind'], ref['id'], ref['revision']): ref for ref in actual}.values())
            if not actual or not any(catalog[key]['origin'] != 'assistant_output' and catalog[key].get('available', True) for key in row['sources']):
                raise ValueError('Follow-up requires actual available evidence')
            followups.append(dict(goal=row['goal'], reason=row['reason'], due_at=row['due_at'], mode=row['mode'], refs=actual[:16]))
        for habit in result['habits']:
            buffer = items.get(habit['buffer_id'])
            if not habit['explicit'] and (buffer is None or buffer.draft.destination != 'habit'):
                raise ValueError('Inferred growth requires an existing buffer')
            if not habit['explicit'] and buffer is not None and not any(row['id'] == buffer.id and row['decision'] == 'adopt' for row in result['reviews']):
                raise ValueError('Inferred habit must review and consume its buffer')
            if habit['explicit'] and not any(catalog[key]['origin'] in ('user_message', 'user_confirmation')
                    and catalog[key].get('available', True) for key in habit['sources']):
                raise ValueError('Explicit habit needs an actual user request')
            if not any(catalog[key]['origin'] != 'assistant_output' and catalog[key].get('available', True)
                       for key in habit['sources']):
                raise ValueError('Habit requires actual evidence')
            role_habits.append(habit)
        used: set[str] = set()
        with self.app._privacy.publication(context.privacy_epoch), self.repository.batch():
            guard()
            for row in result['memories']:
                source, draft = self._draft(row, catalog, context)
                # 精确相同文本只作为确定性去重；语义合并由 reviews 决定。
                duplicate = next((item for item in items.values() if item.draft.content == draft.content and item.draft.destination == draft.destination), None)
                if duplicate:
                    self.store.link(duplicate.id, self._references(row, catalog))
                    self.store.record(identity, duplicate.id, 'duplicate_retained', draft.content, draft.reason, draft.uncertainty)
                    continue
                item = self.repository.ingest(source, draft, idempotency_key=identity + ':' + str(len(used)))
                used.add(item.id)
                self.store.link(item.id, self._references(row, catalog))
                self.store.record(identity, item.id, 'buffered', draft.content, draft.reason, draft.uncertainty)
            for row in result['reviews']:
                previous = items.get(row['id'])
                if previous is None or row['revision'] != previous.revision or row['id'] in used:
                    raise ValueError('Unknown or repeated review target')
                used.add(row['id'])
                decision = row['decision']
                if decision in ('retain', 'archive') and previous.source.content == '[原消息已清除]':
                    source, draft = previous.source, replace(previous.draft, reason=row['reason'],
                        uncertainty=row['uncertainty'], review_after=moment(row['review_after']))
                else:
                    source, draft = self._draft(row, catalog, context)
                    if not row['temporal'] and draft.valid_from is None:
                        draft = replace(draft, valid_from=previous.draft.valid_from or previous.source.created_at,
                                        valid_to=draft.valid_to or previous.draft.valid_to)
                status = (MemoryStatus.ARCHIVED if decision in ('archive', 'merge') else
                          MemoryStatus.ACCEPTED if decision == 'adopt' else previous.status)
                if status == MemoryStatus.DISPUTED:
                    status = MemoryStatus.CANDIDATE
                if draft.destination == 'habit' and status == MemoryStatus.ACCEPTED:
                    if not any(habit['buffer_id'] == previous.id for habit in role_habits):
                        raise ValueError('Habit adoption requires role delivery')
                    status = MemoryStatus.ARCHIVED
                if row['temporal']:
                    if previous.status != MemoryStatus.ACCEPTED or draft.destination == 'habit':
                        raise ValueError('Temporal change requires an existing fact')
                    item = self.repository.revise(previous.id, previous.revision, source,
                        replace(draft, valid_from=draft.valid_from or datetime.now(timezone.utc)), temporal=True)
                else:
                    item = self.repository.update_understanding(previous.id, previous.revision, source, draft, status)
                changed[previous.id] = (item.id, item.revision)
                new_refs = [ref for ref in self._references(row, catalog) if not (ref['kind'] == 'memory' and ref['id'] == item.id)]
                self.store.link(item.id, self.store.refs(previous.id) + new_refs)
                if decision == 'merge':
                    target = items.get(row['target'])
                    if target is None or target.id == previous.id or target.id in used or target.status not in (MemoryStatus.ACCEPTED, MemoryStatus.CANDIDATE):
                        raise ValueError('Invalid merge target')
                    used.add(target.id)
                    merged = self.repository.update_understanding(target.id, target.revision, source, draft, target.status)
                    changed[target.id] = (merged.id, merged.revision)
                    self.store.link(target.id, self.store.refs(previous.id) + new_refs)
                self.store.record(identity, item.id, decision, draft.content, draft.reason, draft.uncertainty)
                items[item.id] = item
            for habit in role_habits:
                self.store.record(identity, habit['buffer_id'], 'habit', habit['content'], habit['reason'], habit['uncertainty'])
            emotion = result['emotion']
            if emotion is not None:
                if not any(catalog[key]['origin'] != 'assistant_output' and catalog[key].get('available', True)
                           for key in emotion['sources']):
                    raise ValueError('Emotion requires an actual exchange or event')
                expiry = moment(emotion['expires_at'])
                emotion = {**emotion, 'expires_at': expiry.timestamp() if expiry else None}
                self.store.record(identity, None, 'emotion', emotion['description_intensity'], emotion['reason_sources'], emotion['uncertainty'])
            # 输入候选不等于学习依据。只保存实际选用的来源，避免无关资料被遗忘时连带删习惯。
            selected_refs: list[dict[str, Any]] = []
            for row in [*result['memories'], *result['reviews'], *role_habits]:
                selected_refs.extend(self._references(row, catalog))
            if emotion is not None:
                selected_refs.extend(self._references(emotion, catalog))
            for followup in followups:
                selected_refs.extend(followup['refs'])
            def committed(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return [dict(ref, id=changed[ref['id']][0], revision=changed[ref['id']][1])
                    if ref['kind'] == 'memory' and ref['id'] in changed else ref for ref in values]
            committed_refs = committed(selected_refs)
            self.repository._db.execute('UPDATE learning_job SET refs=? WHERE id=?', (json.dumps(committed_refs), identity))
            if role_habits or emotion is not None or followups:
                for habit in role_habits:
                    habit['host_refs'] = committed(self._references(habit, catalog))
                if emotion is not None:
                    emotion['host_refs'] = committed(self._references(emotion, catalog))
                role_sources = [key for habit in role_habits for key in habit['sources']]
                if emotion is not None:
                    role_sources.extend(emotion['sources'])
                role_policy = ('local_only' if context.use_policy == 'local_only' or
                    any(catalog[key].get('use_policy') == 'local_only' for key in role_sources) else 'cloud_allowed')
                self.store.role_effect(identity, dict(expected_revision=context.role_revision,
                    source=dict(task_id=context.task_id, conversation_id=context.conversation_id, revision=max(1, context.sequence),
                        conversation_epoch=context.conversation_epoch, refs=committed_refs, memory_epoch=self.memory.epoch,
                        learning_at=self.app.proactive.clock().timestamp()),
                    habits=role_habits, emotion=emotion, use_policy=role_policy,
                    followups=[dict(row, refs=committed(row['refs'])) for row in followups]))
            self.store.finish(identity, 'completed')
        # SQLite 事务已提交；跨库靠幂等回执恢复，不把网络或另一个库放进本库事务。
        delivery_failed = False
        try:
            self.recover()
        except Exception:
            delivery_failed = True  # 本库已经提交，不能退回未完成后重复保存。
        if (role_habits or emotion is not None) and context.task_id in self.app._interaction._prepared:
            role = getattr(self.app.core.companion_runtime, 'context')(context.conversation_id, context.conversation_epoch,
                context.task_id, allow_restricted=self.app._interaction.allow_restricted)
            self.app._interaction.refresh_role(context.task_id, role['revision'])
        if changed and context.task_id in self.app._interaction._prepared:
            self.app._interaction.settle_learning(context.task_id, set(changed))
            self.app.core._task_dependencies[context.task_id] = tuple(ref for ref in
                self.app.core._task_dependencies.get(context.task_id, ()) if ref[0] not in changed)
            from deskpal.planner.models import Plan
            snapshot = self.app.journal.get_plan(context.task_id)
            if snapshot is not None:
                remaining = tuple(ref for ref in snapshot.memory_refs if ref[0] not in changed)
                self.app.journal.save_round(context.task_id, Plan(snapshot.plan.reply, action=snapshot.plan.action),
                    tuple(self.app.tools.list_tools()), str(self.app.core.workspace_dir), remaining)
            for old_id in changed:
                self.app._conversation_repository.invalidate_memory_summaries(old_id)
        summary = result['summary']
        historical = list({(ref['kind'], ref['id'], ref['revision']): ref for ref in refs
            if ref['kind'] == 'dialogue_turn' and not ref['id'].startswith(context.task_id + ':')}.values())
        if summary is not None and historical and not changed and not context.conversation_id.startswith('single:'):
            previous_summary = payload['previous_summary'] or {}
            self.app._conversation_repository.save_context_summary(context.conversation_id, context.conversation_epoch,
                previous_summary.get('revision', 0), summary,
                tuple(ConversationRef(ref['kind'], ref['id'], ref['revision'], ref.get('use_policy', context.use_policy)) for ref in historical))
        if delivery_failed:
            raise OSError('Committed role delivery awaits recovery')
