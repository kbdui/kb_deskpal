"""发现未整理的实际文本来源；只维护有界索引，不调用模型或复制聊天正文。"""
from hashlib import sha256
import json
from threading import RLock
from typing import Any

from deskpal.tasks.models import TaskStatus


class DeferredLearningSources:
    PAGE_SIZE = 64
    MAX_PENDING = 256
    SOURCE_BYTES = 8000
    BATCH_BYTES = 16000

    def __init__(self, app: Any, learning_store: Any) -> None:
        self.app, self.learning_store = app, learning_store
        self._lock = RLock()

    @staticmethod
    def key(ref: dict[str, Any]) -> str:
        return 'reflection:' + ref['id'] if ref['kind'] == 'reflection' else ref['id']

    def _entry(self, ref: dict[str, Any]) -> dict[str, Any] | None:
        task_id = ref['id'] if ref['kind'] == 'reflection' else ref['id'].rsplit(':', 1)[0]
        task = self.app.tasks.get(task_id)
        if task is None or task.status != TaskStatus.COMPLETED:
            return None
        if ref['kind'] == 'reflection':
            from deskpal.proactive.reflection import read
            value = read(self.app, task_id)
            if value is None or ref['revision'] != 1:
                return None
            content, policy = value['content'], value['use_policy']
            entry = {**value, 'origin': 'character_reflection', 'role': 'assistant', 'published': False}
        else:
            side = ref['id'].rsplit(':', 1)[-1]
            if side not in {'user', 'assistant'}:
                return None
            with self.app.store.read_connection() as db:
                row = db.execute("""SELECT i.body,i.use_policy,i.created_at,r.sequence
                    FROM conversation_item i JOIN turn_ref r ON r.task_id=i.task_id
                    JOIN conversation c ON c.id=i.conversation_id
                    JOIN conversation_turn p ON p.task_id=i.task_id JOIN task t ON t.id=i.task_id
                    WHERE i.task_id=? AND i.kind=? AND i.redacted_at IS NULL AND t.scrubbed=0
                    AND p.published=1 AND c.deleted_at IS NULL AND r.epoch=c.context_epoch""",
                    (task_id, side + '_message')).fetchone()
            if row is None or row['sequence'] != ref['revision'] or not row['body']:
                return None
            content, policy = row['body'], row['use_policy']
            entry = dict(role=side, origin='user_message' if side == 'user' else 'assistant_output',
                         published=True, created_at=row['created_at'])
        if self.app._humanization.reference_policy(ref['kind'], ref['id'], ref['revision']) is None:
            return None
        return {**entry, 'content': content, 'use_policy': policy, 'available': True, 'refs': [ref]}

    def load(self, material: dict[str, Any]) -> dict[str, Any] | None:
        entry = self._entry(material['ref'])
        if entry is None or sha256(entry['content'].encode('utf-8')).hexdigest() != material['fingerprint']:
            return None
        if entry['use_policy'] == 'local_only' and not self.app._interaction.allow_restricted:
            return None
        return entry

    def _scan(self) -> list[dict[str, Any]]:
        cursor = json.loads(self.app.proactive._meta('learning_material_cursor') or '["", ""]')
        with self.app.store.read_connection() as db:
            self.app.store.check_privacy(db)
            rows = db.execute("""SELECT t.id,t.updated_at FROM task t
                WHERE t.status='Completed' AND t.scrubbed=0 AND (t.updated_at,t.id)>(?,?)
                AND (EXISTS(SELECT 1 FROM context_snapshot s WHERE s.task_id=t.id AND s.purpose='reflection')
                    OR EXISTS(SELECT 1 FROM conversation_turn p WHERE p.task_id=t.id AND p.published=1))
                ORDER BY t.updated_at,t.id LIMIT ?""", (*cursor, self.PAGE_SIZE)).fetchall()
            refs = []
            for row in rows:
                if db.execute("SELECT 1 FROM context_snapshot WHERE task_id=? AND purpose='reflection'", (row['id'],)).fetchone():
                    refs.append(dict(kind='reflection', id=row['id'], revision=1, available=True))
                turn = db.execute('SELECT conversation_id,sequence FROM turn_ref WHERE task_id=?', (row['id'],)).fetchone()
                if turn:
                    refs.extend(dict(kind='dialogue_turn', id=row['id'] + ':' + side, revision=turn['sequence'],
                        conversation_id=turn['conversation_id'], available=True) for side in ('user', 'assistant'))
        # 到尾部重新轮转；较早的暂停任务后来完成也能在后续扫描中发现。
        next_cursor = [rows[-1]['updated_at'], rows[-1]['id']] if len(rows) == self.PAGE_SIZE else ['', '']
        self.app.proactive._meta('learning_material_cursor', json.dumps(next_cursor))
        return refs

    def pending(self, *, discover: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            old = json.loads(self.app.proactive._meta('learning_pending_materials') or '[]')
            refs = {self.key(item['ref']): item['ref'] for item in old}
            if discover:
                refs.update((self.key(ref), ref) for ref in self._scan())
            live: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
            for key, ref in refs.items():
                entry = self._entry(ref)
                if entry is None:
                    continue
                body = entry['content']
                material = dict(ref=ref, fingerprint=sha256(body.encode('utf-8')).hexdigest(),
                    created_at=entry['created_at'], bytes=len(body.encode('utf-8')))
                live[key] = material, entry
            covered = self.learning_store.organized_messages({key: entry['content'] for key, (_, entry) in live.items()})
            for key, (material, _) in live.items():
                if material['ref']['kind'] == 'reflection':
                    job = self.learning_store.job('turn:' + material['ref']['id'])
                    if job and job['status'] == 'completed' and any(ref['kind'] == 'reflection' and ref['id'] == material['ref']['id'] for ref in json.loads(job['refs'])):
                        covered.add(key)  # 旧版即时整理确实读过该成果，不再补学一遍。
            remaining = [(m, e) for key, (m, e) in live.items() if key not in covered]
            # 暂不可用的大文本/受限资料不能占满索引，挤掉本次实际能整理的材料。
            remaining.sort(key=lambda pair: (pair[0]['bytes'] > self.SOURCE_BYTES or
                (pair[1]['use_policy'] == 'local_only' and not self.app._interaction.allow_restricted),
                pair[0]['created_at'], self.key(pair[0]['ref'])))
            remaining = remaining[:self.MAX_PENDING]
            self.app.proactive._meta('learning_pending_materials', json.dumps([m for m, _ in remaining]))
            return [m for m, e in remaining if m['bytes'] <= self.SOURCE_BYTES and
                    (self.app._interaction.allow_restricted or e['use_policy'] != 'local_only')]

    def select(self) -> list[dict[str, Any]]:
        pending = self.pending()
        selected: list[dict[str, Any]] = []
        size = 0
        for material in pending:
            if len(selected) >= 8:
                break
            if size + material['bytes'] <= self.BATCH_BYTES:
                selected.append(material)
                size += material['bytes']
        return selected
