"""应用层统一准备角色、双方历史及有效要求；模型调用期间不持数据库锁。"""

from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import replace
from hashlib import sha256
import json
from threading import RLock
from typing import Any
from uuid import uuid4

from deskpal.agent.context import PreparedTurn
from deskpal.agent.ports import CompanionPort
from deskpal.companion.understanding import parse_understanding
from deskpal.companion.errors import CompanionValidationError
from deskpal.llm.context import role_aliases
from deskpal.companion.repository import encode
from deskpal.conversation.runtime import ConversationConflict, ConversationRuntime
from deskpal.llm.calls import ModelCallGateway
from deskpal.llm.ports import LLMProvider
from deskpal.memory.contracts import MemoryRuntime, RetrievalBundle
from deskpal.memory.models import MemoryRecord
from deskpal.memory.retrieval import _record
from deskpal.tasks.models import TaskStatus
from deskpal.workflow.humanization import HumanizationStore
from deskpal.workflow.models import WorkflowBlocked
from deskpal.workflow.tasks import PersistentTaskManager

from .privacy import PrivacyCoordinator


def _refs(values: Any) -> tuple[tuple[str, str, int], ...]:
    result: set[tuple[str, str, int]] = set()
    for value in values:
        item = ((str(value.get("kind", "memory")), str(value.get("id", "")), value.get("revision"))
                if isinstance(value, Mapping) else tuple(value))
        if len(item) != 3 or not item[0] or not item[1] or type(item[2]) is not int or item[2] < 1:
            raise ValueError("Context has an invalid source reference")
        result.add((str(item[0]), str(item[1]), item[2]))
        if len(result) > 512:
            raise ValueError("Context source budget exceeded")
    return tuple(sorted(result))


class InteractionCoordinator:
    def __init__(self, *, persistence: HumanizationStore, privacy: PrivacyCoordinator,
                 conversations: ConversationRuntime, tasks: PersistentTaskManager,
                 memory: MemoryRuntime, companion: CompanionPort | None,
                 provider: LLMProvider, cloud: bool, profile_id: str = "",
                 timezone_name: str = "Asia/Shanghai") -> None:
        self.persistence, self.privacy, self.conversations = persistence, privacy, conversations
        self.tasks, self.memory, self.companion = tasks, memory, companion
        self.profile_id, self.runtime_id = profile_id, uuid4().hex
        self.timezone_name = timezone_name
        # 出站边界：旧来源仍有限制，新业务不再询问五类云端许可。
        self.allow_restricted = not cloud
        evaluator = getattr(memory, "evaluation_provider", None)
        self.gateway = ModelCallGateway(provider, reserve=persistence.reserve_call,
            finish=persistence.finish_call, diagnose=persistence.diagnose_call,
            providers={"memory_evaluation": evaluator} if evaluator is not None else {})
        self._lock = RLock()
        self._prepared: OrderedDict[str, PreparedTurn] = OrderedDict()

    def source_policy(self, task_id: str) -> str:
        return self.tasks.task_use_policy(task_id)

    def current_turn(self, task_id: str) -> PreparedTurn:
        self.check(task_id)
        return self._prepared[task_id]

    def admit_tool_memories(self, task_id: str, refs: tuple[tuple[str, int], ...]) -> None:
        turn = self._prepared[task_id]
        if not self.memory.valid(refs):
            raise WorkflowBlocked("工具读取的记忆已失效。")
        read_policy = getattr(self.memory, "source_policy")
        restricted = any(getattr(read_policy(identity, revision), "value", None) != "cloud_allowed" for identity, revision in refs)
        if restricted:
            if not self.allow_restricted:
                raise WorkflowBlocked("工具返回了不能交给当前模型的来源。")
            self.persistence.tighten_policy(task_id, observed=True)
            turn = replace(turn, use_policy="local_only")
            self._prepared[task_id] = turn
        self.persistence.renew_memory(task_id, "", 0, turn.planning_items,
            extra_refs=tuple(("memory", identity, revision) for identity, revision in refs))

    def refresh_memory(self, task_id: str, memory_id: str, revision: int, replacement_id: str) -> PreparedTurn:
        turn = self._prepared[task_id]
        self.privacy.check(turn.privacy_epoch)
        if not self.memory.valid(((replacement_id, revision),)):
            raise WorkflowBlocked("更正后的记忆已经再次变化。")
        def refers(item: Mapping[str, Any]) -> bool:
            return any(kind == "memory" and identity == memory_id for kind, identity, _ in _refs(item.get("refs", ())))
        # 依赖旧记忆的完整历史单元全部省略，实际更正结果在工具观察中提供。
        revoked_tasks = {str(item.get("source_ref", {}).get("id", "")).rsplit(":", 1)[0]
                         for item in turn.planning_items if refers(item)}
        items = tuple(item for item in turn.planning_items if not refers(item) and
                      str(item.get("source_ref", {}).get("id", "")).rsplit(":", 1)[0] not in revoked_tasks)
        history = tuple(item for item in turn.history if not refers(item))
        self.persistence.renew_memory(task_id, memory_id, revision, items, replacement_id)
        snapshot = self.persistence.context(task_id, 'planning')
        records = self._memory_records(snapshot['refs'] if snapshot else ())
        updated = replace(turn, planning_items=items, history=history,
            conversation_summary=next((item for item in items if item.get("kind") == "conversation_summary"), None),
            memories=tuple(row.content for row in records), memory_records=records)
        self._prepared[task_id] = updated
        self.check(task_id)
        return updated

    def refresh_role(self, task_id: str, expected_revision: int) -> PreparedTurn:
        turn = self._prepared[task_id]
        self.privacy.check(turn.privacy_epoch)
        role = self._role(turn.conversation_id, turn.conversation_epoch, task_id)
        if role["revision"] != expected_revision:
            raise WorkflowBlocked("角色在管理操作后又被更改，请重新核对。")
        items = tuple({**dict(item), "data": role} if item.get("kind") == "role_context" else item for item in turn.planning_items)
        self.persistence.renew_memory(task_id, "", 0, items)
        updated = replace(turn, role_revision=role["revision"], role_json=encode(role), planning_items=items)
        self._prepared[task_id] = updated
        return updated

    def settle_learning(self, task_id: str, changed_ids: set[str], *, drop_summary: bool = False) -> None:
        """回答形成后的复核更新退出本轮旧引用；历史保留删除索引，不重做工具。"""
        turn = self._prepared[task_id]
        def retained(item: Mapping[str, Any]) -> bool:
            if drop_summary and item.get('kind') == 'conversation_summary':
                return False
            return not any(kind == 'memory' and identity in changed_ids for kind, identity, _ in _refs(item.get('refs', ())))
        planning = tuple(item for item in turn.planning_items if retained(item))
        with self.persistence.store.transaction() as db:
            # 沿用既有归档逻辑，保留旧快照和来源清理路径。
            self.persistence.renew_memory(task_id, '', 0, planning)
            for identity in changed_ids:
                db.execute("DELETE FROM dependency WHERE task_id=? AND kind='memory' AND object_id=?", (task_id, identity))
                db.execute("DELETE FROM context_dependency WHERE task_id=? AND kind='memory' AND object_id=?", (task_id, identity))
        self._prepared[task_id] = replace(turn, planning_items=planning,
            conversation_summary=next((item for item in planning if item.get("kind") == "conversation_summary"), None),
            history=tuple(item for item in turn.history if retained(item)),
            memories=tuple(row.content for row in turn.memory_records if row.id not in changed_ids),
            memory_records=tuple(row for row in turn.memory_records if row.id not in changed_ids),
            retrieval=replace(turn.retrieval,
                records=tuple(row for row in turn.retrieval.records if row.id not in changed_ids),
                revisions=tuple(ref for ref in turn.retrieval.revisions if ref[0] not in changed_ids)))

    def retrieve(self, request: str) -> RetrievalBundle:
        return self.memory.retrieve(request)

    def apply_decision(self, task_id: str, decision: dict[str, Any] | None) -> bool:
        """采用首次模型决策中的交流要求；补查时必须再判断，不能先执行提议。"""
        turn = self.current_turn(task_id)
        if any(item.get('kind') == 'context_decision_applied' for item in turn.planning_items):
            raise WorkflowBlocked('Initial context decision already applied')
        role = json.loads(turn.role_json)
        sources: dict[str, Mapping[str, Any]] = {'request': {}}
        index = 0
        for item in turn.planning_items:
            if item.get('kind') in {'user_reference', 'assistant_reference'}:
                sources[f'history:{index}'] = item
                index += 1
            elif item.get('kind') == 'conversation_summary':
                sources['summary'] = item
        rejected = False
        try:
            understood = parse_understanding(decision or {'entries': []}, sources=sources, aliases=role_aliases(role))
        except ValueError:
            understood = parse_understanding({'entries': []}, sources=sources)
            rejected = True
        apply = getattr(self.companion, 'apply_understanding')
        with self.privacy.publication(turn.privacy_epoch):
            self.check(task_id)
            kwargs = dict(expected_revision=turn.role_revision,
                conversation_id=turn.conversation_id or 'single:' + task_id,
                conversation_epoch=turn.conversation_epoch, sequence=turn.sequence, use_policy=turn.use_policy)
            try:
                apply(task_id, understood.entries, **kwargs)
            except CompanionValidationError:
                # 角色存储已回滚整份无效提议。仅撤下可选理解，不重放任何工具。
                understood = parse_understanding({'entries': []}, sources=sources)
                rejected = True
                apply(task_id, (), **kwargs)
            role = self._role(turn.conversation_id, turn.conversation_epoch, task_id)
            items = tuple({**dict(item), 'data': role} if item.get('kind') == 'role_context' else item
                          for item in turn.planning_items) + ({'kind': 'context_decision_applied'},)
            self.persistence.renew_memory(task_id, '', 0, items)
            turn = replace(turn, role_revision=role['revision'], role_json=encode(role), planning_items=items,
                warnings=turn.warnings + (('本轮交流要求未能解析或保存，已保留原有设置。',) if rejected else ()))
            self._prepared[task_id] = turn
        if not understood.retrieval_query:
            # 无效线索可能影响首份草稿；沿原资料再判断一次，不直接采用其行动提议。
            self.gateway.annotate(task_id, 'planning', {'processing_code': 'fields_invalid' if rejected else 'ok',
                **({'processing_field': 'context'} if rejected else {})})
            return rejected
        self.check(task_id)
        bundle = self.retrieve(understood.retrieval_query)
        self.check(task_id)
        if not self.memory.valid(bundle.revisions):
            raise WorkflowBlocked('Contextual retrieval changed')
        if any(getattr(self.memory, 'source_policy')(name, revision).value != 'cloud_allowed'
               for name, revision in bundle.revisions):
            if not self.allow_restricted:
                raise WorkflowBlocked('Contextual retrieval contains restricted sources')
            self.persistence.tighten_policy(task_id)
            turn = replace(turn, use_policy='local_only')
        refs = _refs(understood.retrieval_refs)
        items += ({'kind': 'retrieval_context', 'query': understood.retrieval_query,
                   'instruction': False, 'externally_verified': False, 'refs': list(understood.retrieval_refs)},)
        all_refs = tuple(sorted(set(turn.retrieval.revisions) | set(bundle.revisions)))
        self.persistence.renew_memory(task_id, '', 0, items,
            extra_refs=refs + tuple(('memory', name, revision) for name, revision in bundle.revisions))
        records = tuple({record.id: record for record in (*turn.memory_records, *bundle.records)}.values())
        combined = RetrievalBundle(records=records, revisions=all_refs,
            warnings=tuple(dict.fromkeys((*turn.retrieval.warnings, *bundle.warnings))))
        self._prepared[task_id] = replace(turn, planning_items=items, retrieval=combined,
            memory_records=records, memories=tuple(record.content for record in records),
            warnings=tuple(dict.fromkeys((*turn.warnings, *bundle.warnings))))
        self.check(task_id)
        return True

    def _memory_records(self, refs: Any) -> tuple[MemoryRecord, ...]:
        """按已冻结的真实引用恢复正文；不重新检索替换任务原先使用的证据。"""
        selected = tuple((name, revision) for kind, name, revision in refs if kind == 'memory')
        if not self.memory.valid(selected):
            raise WorkflowBlocked('任务引用的记忆已变化，不能恢复旧正文。')
        repository = getattr(self.memory, 'repository', None)
        records: list[MemoryRecord] = []
        size = 0
        for identity, revision in selected:
            item = repository.get(identity) if repository is not None else None
            if item is None or item.revision != revision:
                raise WorkflowBlocked('无法恢复任务的记忆依据。')
            record = _record(item, ('task_context',))
            length = len(record.content.encode('utf-8'))
            if len(records) < 20 and size + length <= 65536:
                records.append(record)
                size += length
        return tuple(records)

    def _role(self, conversation_id: str, epoch: int, task_id: str) -> dict[str, Any]:
        if not conversation_id:
            link = self.persistence.turn(task_id)
            conversation_id = str(link['conversation_id']) if link else 'single:' + task_id
        read = getattr(self.companion, "context", None)
        if not callable(read):
            raise WorkflowBlocked("角色资料不可读，无法准备本轮上下文。")
        return dict(read(conversation_id, epoch, task_id, allow_restricted=self.allow_restricted))

    def prepare_continuation(self, task_id: str) -> None:
        """恢复先核对来源，再换入当前角色；不再次执行上下文理解或工具。"""
        self._prepared.pop(task_id, None)
        self.persistence.check(task_id, 'planning')
        link = self.persistence.turn(task_id)
        saved = self.persistence.context(task_id, 'planning')
        if link is None or saved is None:
            raise WorkflowBlocked('Missing continuation context')
        identity, epoch = link['conversation_id'], link['epoch']
        if not identity.startswith('single:') and self.conversations.context_epoch(identity) != epoch:
            raise WorkflowBlocked('Conversation cleared while paused')
        role = self._role(identity, epoch, task_id)
        items = tuple({**dict(item), 'data': role} if item.get('kind') == 'role_context' else item for item in saved['items'])
        extra: set[tuple[str, str, int]] = set()
        if not identity.startswith('single:'):
            frozen = self.conversations.freeze(identity, epoch, 2 ** 63 - 1,
                cloud=not self.allow_restricted, history_allowed=True)
            items = tuple(item for item in items if item.get('kind') in {'role_context', 'task_origin', 'context_decision_applied', 'context_clock'}) + tuple(frozen['planning_items'])
            summary = self.conversations.repository.context_summary(identity, epoch,
                allow_restricted=self.allow_restricted) if self.conversations.repository else None
            if summary and not summary.get('unavailable'):
                items += (summary,)
            for item in items:
                extra.update(_refs(item.get('refs', ())))
                if item.get('source_ref'):
                    extra.update(_refs((item['source_ref'],)))
        self.persistence.renew_memory(task_id, '', 0, items, extra_refs=tuple(sorted(extra)))

    def prepare(self, task_id: str, request: str, bundle: RetrievalBundle) -> PreparedTurn:
        with self._lock:
            prior = self._prepared.get(task_id)
            if prior is not None:
                self.check(task_id)
                return prior
            for identity in tuple(self._prepared):
                task = self.tasks.get(identity)
                if task is None or task.status not in {TaskStatus.WAITING, TaskStatus.CREATED, TaskStatus.PLANNING, TaskStatus.RUNNING}:
                    self._prepared.pop(identity, None)
            if len(self._prepared) >= 128:
                raise WorkflowBlocked("Interaction admission capacity reached")
        privacy_epoch = int(self.persistence.privacy()["epoch"])
        self.privacy.check(privacy_epoch)
        use_policy = self.source_policy(task_id)
        if not self.allow_restricted and use_policy == "local_only":
            raise WorkflowBlocked("This request is local_only")
        link = self.persistence.turn(task_id)
        if link is None:
            self.persistence.bind_turn(task_id, "single:" + task_id, 0, 1)
            link = self.persistence.turn(task_id)
        assert link is not None
        identity, epoch, sequence = str(link["conversation_id"]), int(link["epoch"]), int(link["sequence"])
        repository = self.conversations.repository
        persistent = not identity.startswith("single:") and repository is not None and repository.contains(identity)
        role = self._role(identity, epoch, task_id)
        origin = self.privacy.journal.origin(task_id)
        saved = self.persistence.context(task_id, "planning")
        history: tuple[Mapping[str, Any], ...] = ()
        warnings: list[str] = list(bundle.warnings)
        planning: list[Mapping[str, Any]] = []
        if saved is not None:
            planning = list(saved["items"])
            original = next((item.get("data") for item in planning if item.get("kind") == "role_context"), None)
            if original is None or original.get("role_id") != role["role_id"] or original.get("revision") != role["revision"]:
                raise WorkflowBlocked("角色或有效要求已变化，请重新发起请求。")
            role = original
            # 仅恢复快照中已经登记来源的双方交流；不能在表达阶段偷偷加入未登记的新历史。
            history = tuple(dict(item,
                role='user' if item['kind'] == 'user_reference' else 'assistant',
                verified=False, source_kind='historical_user_reference' if item['kind'] == 'user_reference' else 'assistant_proposal')
                for item in planning if item.get('kind') in {'user_reference', 'assistant_reference'})
        else:
            if self.privacy.journal.get_plan(task_id) is not None:
                raise WorkflowBlocked("旧计划未绑定新版角色上下文，请重新发起请求。")
            if persistent:
                assert repository is not None
                frozen = self.conversations.freeze(identity, epoch, sequence,
                    cloud=not self.allow_restricted, history_allowed=True)
                history = tuple(frozen["history"])
                planning.extend(frozen["planning_items"])
                warnings.extend(frozen["warnings"])
                summary = repository.context_summary(identity, epoch, allow_restricted=self.allow_restricted)
                if summary is not None:
                    if summary.get("unavailable"):
                        warnings.append("conversation_summary_sources_unavailable")
                    else:
                        planning.insert(0, summary)
            # 本地模型可理解旧受限资料，但派生理解不能因此扩大外发范围。
            role_sources = [role["character_card"], *role["learned_habits"],
                            *role["user_understanding"], *role["interaction_approach"]]
            if role.get("current_emotion") is not None:
                role_sources.append(role["current_emotion"])
            if (any(row.get("use_policy") == "local_only" for row in role_sources)
                    or any(row.get("source_ref", {}).get("use_policy") == "local_only" for row in history)):
                use_policy = "local_only"
                if self.source_policy(task_id) != use_policy:
                    self.persistence.tighten_policy(task_id)
            base_revision = role["revision"]
            background_refs: set[tuple[str, str, int]] = set()
            for item in (*planning, *history):
                background_refs.update(_refs(item.get("refs", ())))
                if item.get("source_ref"):
                    background_refs.update(_refs((item["source_ref"],)))
            def guard() -> None:
                self.privacy.check(privacy_epoch)
                self.check(task_id)
                current = self._role(identity, epoch, task_id)
                if current["role_id"] != role["role_id"] or current["revision"] != base_revision:
                    raise WorkflowBlocked("角色上下文在理解期间已改变。")
                if persistent and self.conversations.context_epoch(identity) != epoch:
                    raise WorkflowBlocked("会话已清空，旧理解不再保存。")
                # 尚未冻结完整任务快照，也要校验本次理解/检索实际读取的历史来源。
                for kind, name, revision in background_refs:
                    if kind == "memory":
                        if not self.memory.valid(((name, revision),)):
                            raise WorkflowBlocked("交流背景引用的记忆已变化。")
                        policy = getattr(self.memory, "source_policy")(name, revision).value
                    else:
                        policy = self.persistence.reference_policy(kind, name, revision)
                    if policy is None or (policy == "local_only" and not self.allow_restricted):
                        raise WorkflowBlocked("交流背景来源已变化，不能继续使用旧理解。")
            guard()
            # 理解与首次规划合并；先提供原句检索结果，模型可在同次决策提出补查。
            bundle = self.retrieve(request)
            guard()
            warnings.extend(bundle.warnings)
            planning.append({'kind': 'context_clock', 'now': datetime.now(timezone.utc).isoformat(),
                             'timezone': self.timezone_name})
            planning.insert(0, {"kind": "role_context", "data": role, "authority": "character_and_scoped_communication_only"})
            planning.insert(1, {"kind": "task_origin", "data": origin,
                "instruction": False, "grants_permission": False})
            # 角色是必需资料；超预算只省略完整的最旧历史轮次。
            # 为首次决定的采用标记和可选补查线索留两个位置。
            while len(planning) > 30:
                oldest = next((item for item in planning if item.get("source_ref")), None)
                if oldest is None:
                    raise WorkflowBlocked("Context item budget exceeded")
                old_id = str(oldest["source_ref"]["id"]).rsplit(":", 1)[0]
                planning = [item for item in planning if not str(item.get("source_ref", {}).get("id", "")).startswith(old_id + ":")]
                warnings.append("planning_context_limit")
        refs = set(("memory", name, revision) for name, revision in bundle.revisions)
        for item in (*planning, *history):
            refs.update(_refs(item.get("refs", ())))
            if item.get("source_ref"):
                refs.update(_refs((item["source_ref"],)))
        for kind, name, revision in refs:
            if kind == "memory":
                read_policy = getattr(self.memory, "source_policy", None)
                policy = getattr(read_policy(name, revision), "value", None) if callable(read_policy) else None
            else:
                policy = self.persistence.reference_policy(kind, name, revision)
            if policy != "cloud_allowed":
                use_policy = "local_only"
        if use_policy == "local_only":
            if not self.allow_restricted:
                raise WorkflowBlocked("Context contains local_only sources")
            if self.source_policy(task_id) != use_policy:
                self.persistence.tighten_policy(task_id)
        if saved is None:
            digest = sha256(encode({"request": request, "items": planning, "refs": sorted(refs)}).encode("utf-8")).hexdigest()
            self.persistence.save_context(task_id, digest, planning, tuple(sorted(refs)), "planning")
        if self.persistence.context(task_id, "expression") is None:
            self.persistence.save_context(task_id, sha256(encode(sorted(refs)).encode("utf-8")).hexdigest(), (), tuple(sorted(refs)), "expression")
        memory_records = self._memory_records(saved['refs']) if saved is not None else tuple(bundle.records)
        if saved is not None:
            bundle = RetrievalBundle(records=memory_records,
                revisions=tuple((name, revision) for kind, name, revision in saved['refs']
                    if kind == 'memory' and name in {row.id for row in memory_records}))
        result = PreparedTurn(task_id=task_id, privacy_epoch=privacy_epoch,
            conversation_id=identity if persistent else "", conversation_epoch=epoch, sequence=sequence,
            planning_items=tuple(planning), history=history, style="", role_json=encode(role),
            memories=tuple(row.content for row in memory_records), memory_records=memory_records,
            warnings=tuple(dict.fromkeys(warnings)),
            use_policy=use_policy, profile_id=self.profile_id, runtime_id=self.runtime_id,
            role_id=role["role_id"], role_revision=role["revision"], retrieval=bundle,
            conversation_summary=next((item for item in planning if item.get("kind") == "conversation_summary"), None))
        result = replace(result, origin=origin["kind"])
        with self._lock:
            self._prepared[task_id] = result
        self.check(task_id)
        return result

    def check(self, task_id: str, *, expression: bool = False) -> None:
        value = self._prepared.get(task_id)
        if value is not None:
            self.privacy.check(value.privacy_epoch)
            if value.profile_id != self.profile_id or value.runtime_id != self.runtime_id:
                raise WorkflowBlocked("Backend generation changed")
            current = self._role(value.conversation_id, value.conversation_epoch, task_id)
            if current["role_id"] != value.role_id or current["revision"] != value.role_revision:
                raise WorkflowBlocked("角色背景已修改，旧快照不可继续使用。")
            if value.conversation_id:
                try:
                    if self.conversations.context_epoch(value.conversation_id) != value.conversation_epoch:
                        raise WorkflowBlocked("Conversation epoch expired")
                except ConversationConflict as error:
                    raise WorkflowBlocked("Conversation context expired") from error
        self.persistence.check(task_id, "expression" if expression else "planning")

    def calls(self, task_id: str, purpose: str) -> LLMProvider:
        return self.gateway.bind(task_id, purpose,
            guard=lambda: self.check(task_id, expression=purpose == "expression"))

    @contextmanager
    def publication(self, task_id: str) -> Iterator[None]:
        turn = self._prepared.get(task_id)
        epoch = turn.privacy_epoch if turn else int(self.persistence.privacy()["epoch"])
        with self.privacy.publication(epoch):
            self.check(task_id)
            yield

    def publish(self, turn: PreparedTurn, request: str, response: str, *,
                facts: tuple[Mapping[str, Any], ...], signals: Mapping[str, Any]) -> None:
        self.check(turn.task_id)
        context = self.persistence.context(turn.task_id, "expression")
        refs = _refs(context["refs"] if context else ())
        for side in ("user", "assistant"):
            self.persistence.store.set_dependency_version("dialogue_turn", turn.task_id + ":" + side,
                                                         turn.sequence, use_policy=turn.use_policy)
        if turn.conversation_id and turn.origin == "user":
            self.conversations.publish(turn.conversation_id, turn.conversation_epoch, turn.sequence,
                turn.task_id, request, response, refs=refs, use_policy=turn.use_policy, facts=facts)
        if turn.origin == 'proactive' and not response:
            with self.persistence.store.transaction() as db:
                db.execute("DELETE FROM conversation_item WHERE task_id=? AND kind IN ('assistant_message','task_card')", (turn.task_id,))
                db.execute("UPDATE conversation_turn SET published=0,refs='[]',facts='[]' WHERE task_id=?", (turn.task_id,))
                db.execute('UPDATE conversation SET timeline_revision=timeline_revision+1 WHERE id=?', (turn.conversation_id,))
        with self._lock:
            self._prepared.pop(turn.task_id, None)

    def invalidate(self) -> None:
        self.conversations.invalidate()

    def discard_tasks(self, task_ids: tuple[str, ...]) -> None:
        with self._lock:
            for task_id in task_ids:
                self._prepared.pop(task_id, None)

    def close(self) -> None:
        self.invalidate()
        with self._lock:
            self._prepared.clear()
