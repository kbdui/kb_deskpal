"""QQ 人物认知、相处约定及经历投递；含义由模型判断，程序维护真实归属。"""
from datetime import datetime, timezone
import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from deskpal.memory.retrieval import _lexical
from deskpal.llm.context import role_view
from deskpal.workflow.models import WorkflowBlocked
from deskpal.workflow.store import canonical
from .store import now


INSTRUCTIONS = '''[QQ_SOCIAL_LEARNING]
只返回 JSON，可省略未变组：{"summary":"当前会话工作摘要","memories":[],"experiences":[]}。
本次只整理 QQ：角色人格唯一，联系人认知、各人约定和群聊状态留在插件，不直接修改主体人格。
summary 保留话题、设想与未决事项，不能凭空补全。memories 用于长期保留事实、自述、转述、印象、共同经历和相处约定；不逐条记流水账，已有相近内容优先更新或合并。
memories 每项字段：id（新建为 null，更新使用已有编号）、revision（新建0）、kind=fact|impression|episode|agreement、scope=person|conversation、content、reason、uncertainty、status=pending|accepted|archived、review_after（带时区时间或null）、sources（本批实际消息 sequence 数组）、people（人物关联数组）。总计最多8项。
people 每项：person_id（已知人物编号或null）、name、relation=source|subject|viewpoint|participant。未知人物只填名称和null，不凭昵称把人合并或猜QQ账号。
A介绍B或评价B时，来源是A，内容对象是B，观点持有人可能是A或被转述的C；正文与people同时保留这个区别，不把转述变成B的客观属性。角色印象保留为印象。
person 范围至少关联一个已知对象；conversation 约定只适用于来源会话。模型结合当前场合处理多人约定冲突，不按分数裁决，不把对A的要求推广给B，也不把社交亲近当作工具权限。
pending 必须给出晚于now的 review_after；到期或没安排时间的待确认记忆需复核，能确认则accepted，不再需要则archived，仍不确定则pending并安排下一次。可以长期保持不确定，不按年龄强制删除。
更新已有记录必须依据其原来源或本批消息；archived用于遗忘提炼，不删除聊天。新建内容必须有本批未整理消息；重复旧资料不新建。人物专属习惯不投递为角色全局习惯。
experiences 最多3项，每项 content、reason、uncertainty、sources。选择确实有价值的交流，以角色第一人称描述“我与谁经历了什么、我怎样理解或感受”，不是向本机用户发消息。角色设定、想象和第三方自述不是已验证现实，不编造线下亲历。
经历只建议交给主体后续学习，不承诺已经永久记住或改了人格；没有值得分享的内容可以空数组。不把私聊全部原文复制到主体，不提交工具命令。来源必须是本批实际消息，已发送只表示协议确认，不等于对方已读。全部经历的正文、理由和不确定性合计最多7000 UTF-8字节。
memories正文/理由各最多2000 UTF-8字节，uncertainty最多1000；每项人物与来源各最多8个。experiences正文最多1600、理由/不确定性各最多600字节；summary最多6000字节。只输出需要保存的简洁结果。'''


def text(value: Any, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value.encode()) > limit:
        raise ValueError('QQ 整理文本为空或超出上限')
    return value


class SocialMemory:
    def __init__(self, runtime: Any) -> None:
        self.runtime, self.store = runtime, runtime.store

    def people(self, account: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self.store.transaction() as db:
            for row in messages:
                speaker = row['speaker_id']
                external = speaker.removeprefix('qq:')
                if row['direction'] == 'out' or not external.isdecimal() or not 5 <= len(external) <= 20:
                    continue
                person = uuid5(NAMESPACE_URL, 'deskpal:qq:' + account + ':' + external).hex
                db.execute('INSERT OR IGNORE INTO qq_person VALUES(?,?,?,?)', (person, account, row['speaker_name'], row['created_at']))
                db.execute('UPDATE qq_person SET name=? WHERE id=?', (row['speaker_name'], person))
                db.execute("INSERT OR IGNORE INTO qq_identity VALUES('qq',?,?,?)", (account, external, person))
        return self.store.rows('SELECT p.id AS person_id,p.name,i.external_id AS account_id FROM qq_person p JOIN qq_identity i ON i.person_id=p.id WHERE p.account=? ORDER BY p.id', (account,))

    def view(self, context: Any, query: str = '', *, limit: int = 12, search: bool = False) -> list[dict[str, Any]]:
        rows = self.store.rows("SELECT * FROM qq_social_memory WHERE account=? AND status!='archived'", (context.account_id,))
        current = {row['speaker_id'].removeprefix('qq:') for row in self.store.admit(context.work_id)}
        identities = self.store.rows('SELECT external_id,person_id FROM qq_identity WHERE namespace=?', (context.account_id,))
        persons = {row['person_id'] for row in identities if row['external_id'] in current}
        accounts = {row['person_id']: row['external_id'] for row in identities}
        config, _ = self.runtime.settings()
        work = self.store.rows('SELECT review_targets FROM qq_work WHERE id=?', (context.work_id,))
        targets = set(json.loads(work[0]['review_targets'])) if work else set()
        selected = []
        for row in rows:
            kind, target = row['thread'].split(':')
            if not self.runtime.permitted(config, kind, target):
                continue
            links = self.store.rows('SELECT person_id,name,relation FROM qq_social_link WHERE memory_id=? ORDER BY position', (row['id'],))
            relevant = row['thread'] == context.conversation_id or any(link['person_id'] in persons for link in links)
            score = _lexical(query, row['content'] + ' ' + ' '.join(link['name'] + ' ' + (link['person_id'] or '') + ' ' + accounts.get(link['person_id'], '') for link in links)) if query else 0.0
            if row['scope'] == 'conversation' and row['thread'] != context.conversation_id:
                continue
            due = row['status'] == 'pending' and (not row['review_after'] or datetime.fromisoformat(row['review_after']) <= datetime.now(timezone.utc))
            if (relevant or score > 0) and (not search or not query or score > 0):
                priority = (-score, not due) if search else (not due, -score)
                selected.append((row['id'] not in targets, *priority, not relevant, row['updated_at'], dict(row, people=links)))
        selected.sort(key=lambda pair: pair[:5])
        result: list[dict[str, Any]] = []
        size = 0
        for *_, row in selected:
            row['evidence'] = json.loads(row['evidence'])
            cost = len(canonical(row).encode())
            if len(result) < limit and size + cost <= 16000:
                result.append(row); size += cost
        return result

    def freeze(self, context: Any, rows: list[dict[str, Any]], observations: Any = ()) -> dict[str, Any]:
        visible = self.view(context, '\n'.join(row['content'] for row in rows[-4:]))
        # 本轮主动回看的旧原文/认知也可作为依据，不能只在规划中看见却在学习时丢失。
        inspected, history = [], []
        for observation in observations:
            if not observation.get('success') or not isinstance(observation.get('output'), list):
                continue
            if observation.get('tool_name') == 'qq_history':
                history.extend(observation['output'])
            elif observation.get('tool_name') == 'qq_memory_search':
                inspected.extend(observation['output'])
        known = {item['id']: item for item in self.view(context, '\n'.join(str(item.get('content', '')) for item in inspected), limit=20, search=True)} if inspected else {}
        included = [known[item['id']] for item in inspected if isinstance(item, dict) and item.get('id') in known]
        existing = {item['id'] for item in included}
        visible = [*included, *(item for item in visible if item['id'] not in existing)][:12]
        # 更新已有记忆时，模型同时得到真实原依据；避免只有摘要却必须猜来源。
        source_ids = {seq for item in visible for seq in item['evidence']}
        sources = {row['sequence']: row for row in rows}
        size = sum(len(row['content'].encode()) for row in rows)
        for item in history:
            if not isinstance(item, dict) or type(item.get('sequence')) is not int:
                continue
            extra = self.store.rows('SELECT * FROM qq_message WHERE sequence=? AND account=? AND thread=?', (item['sequence'], context.account_id, context.conversation_id))
            if extra and item['sequence'] not in sources and size + len(extra[0]['content'].encode()) <= 24000:
                sources[item['sequence']] = extra[0]; size += len(extra[0]['content'].encode())
        for sequence in sorted(source_ids):
            extra = self.store.rows('SELECT * FROM qq_message WHERE sequence=? AND account=?', (sequence, context.account_id))
            if extra and sequence not in sources and size + len(extra[0]['content'].encode()) <= 24000:
                sources[sequence] = extra[0]; size += len(extra[0]['content'].encode())
        people = self.people(context.account_id, list(sources.values()))
        source_people = {row['speaker_id'].removeprefix('qq:') for row in sources.values()}
        linked = {link['person_id'] for item in visible for link in item['people']}
        people = [person for person in people if person['account_id'] in source_people or person['person_id'] in linked]
        task_id = self.store.rows('SELECT task_id FROM qq_work WHERE id=?', (context.work_id,))[0]['task_id']
        frozen = dict(now=now(), timezone=self.runtime.app.config.proactive.timezone,
            role=role_view(self.runtime.host.role_context(context, task_id)),
            previous_summary=self.store.summary(context.account_id, context.conversation_id),
            social_revision=self.store.rows('SELECT revision FROM qq_social_meta')[0]['revision'],
            memories=visible, people=people[:64], fresh_sources=[row['sequence'] for row in rows],
            messages=[{key: row[key] for key in ('sequence', 'platform_id', 'speaker_id', 'speaker_name', 'direction', 'content', 'created_at', 'thread', 'organized', 'reply_to', 'mentions')} for row in sources.values()])
        # 按完整条目收敛输入；未纳入的材料不标覆盖，下次仍可整理。
        while len(canonical(frozen).encode()) > 56000:
            if frozen['memories']:
                frozen['memories'].pop()
            else:
                old = next((row for row in frozen['messages'] if row['sequence'] not in frozen['fresh_sources']), None)
                if old is not None:
                    frozen['messages'].remove(old)
                elif len(frozen['fresh_sources']) > 1:
                    sequence = frozen['fresh_sources'].pop()
                    frozen['messages'] = [row for row in frozen['messages'] if row['sequence'] != sequence]
                elif frozen['people']:
                    frozen['people'].pop()
                else:
                    raise ValueError('本次 QQ 整理的必要资料超过预算')
        return frozen

    def validate(self, data: Any, frozen: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict) or set(data) - {'summary', 'memories', 'experiences'}:
            raise ValueError('QQ 整理字段无效')
        summary = text(data.get('summary', frozen['previous_summary']), 6000, empty=True)
        changes, experiences = data.get('memories', []), data.get('experiences', [])
        if not isinstance(changes, list) or len(changes) > 8 or not isinstance(experiences, list) or len(experiences) > 3:
            raise ValueError('QQ 整理条目数量无效')
        messages = {row['sequence']: row for row in frozen['messages']}
        persons = {row['person_id']: row for row in frozen['people']}
        existing = {row['id']: row for row in frozen['memories']}
        fresh = set(frozen['fresh_sources'])
        seen = set()
        for row in changes:
            required = {'id', 'revision', 'kind', 'scope', 'content', 'reason', 'uncertainty', 'status', 'review_after', 'sources', 'people'}
            if not isinstance(row, dict) or set(row) != required:
                raise ValueError('QQ 记忆字段无效')
            previous = existing.get(row['id']) if isinstance(row['id'], str) else None
            if row['id'] is not None and (previous is None or row['id'] in seen or row['revision'] != previous['revision']):
                raise ValueError('QQ 记忆修订已变化')
            if type(row['revision']) is not int or row['id'] is None and row['revision'] != 0:
                raise ValueError('QQ 记忆修订无效')
            seen.add(row['id']) if row['id'] else None
            if (row['kind'] not in {'fact', 'impression', 'episode', 'agreement'} or row['scope'] not in {'person', 'conversation'}
                    or row['status'] not in {'pending', 'accepted', 'archived'}):
                raise ValueError('QQ 记忆类型或状态无效')
            for key, maximum in (('content', 2000), ('reason', 2000), ('uncertainty', 1000)):
                text(row[key], maximum, empty=key == 'uncertainty')
            self._sources(row['sources'], messages)
            if previous is None and not fresh.intersection(row['sources']):
                raise ValueError('新 QQ 记忆需要未整理来源')
            deadline = row['review_after']
            if deadline is not None:
                parsed = datetime.fromisoformat(text(deadline, 64))
                if parsed.utcoffset() is None or parsed <= datetime.now(timezone.utc):
                    raise ValueError('QQ 复查时间必须在未来且带时区')
                row['review_after'] = parsed.astimezone(timezone.utc).isoformat()
            if row['status'] == 'pending' and deadline is None:
                raise ValueError('待确认 QQ 记忆必须安排复查')
            links = row['people']
            if not isinstance(links, list) or len(links) > 8:
                raise ValueError('QQ 人物关联无效')
            for link in links:
                if not isinstance(link, dict) or set(link) != {'person_id', 'name', 'relation'}:
                    raise ValueError('QQ 人物关联字段无效')
                if link['person_id'] is not None and link['person_id'] not in persons:
                    raise ValueError('不能猜测或合并未知人物账号')
                text(link['name'], 240)
                if link['relation'] not in {'source', 'subject', 'viewpoint', 'participant'}:
                    raise ValueError('QQ 人物归属无效')
            # 发言来源是程序已知事实，不依赖模型是否完整列出了 people。
            person_by_account = {person['account_id']: person for person in persons.values()}
            for sequence in row['sources']:
                source = messages[sequence]
                person = person_by_account.get(source['speaker_id'].removeprefix('qq:'))
                if person and not any(link['person_id'] == person['person_id'] and link['relation'] == 'source' for link in links):
                    links.append(dict(person_id=person['person_id'], name=person['name'], relation='source'))
            if row['scope'] == 'person' and not any(link['person_id'] for link in links):
                raise ValueError('人物约定需要实际人物')
        for row in experiences:
            if not isinstance(row, dict) or set(row) != {'content', 'reason', 'uncertainty', 'sources'}:
                raise ValueError('角色经历字段无效')
            for key, maximum in (('content', 1600), ('reason', 600), ('uncertainty', 600)):
                text(row[key], maximum, empty=key == 'uncertainty')
            self._sources(row['sources'], messages)
            if not fresh.intersection(row['sources']):
                raise ValueError('新经历必须包含实际未整理消息')
        if len(self.experience_content(experiences).encode()) > 7000:
            raise ValueError('分享经历总长度超过上限')
        return dict(summary=summary, memories=changes, experiences=experiences)

    @staticmethod
    def experience_content(experiences: list[dict[str, Any]]) -> str:
        return '\n\n'.join(row['content'] + '\n理由：' + row['reason'] + ('\n不确定之处：' + row['uncertainty'] if row['uncertainty'] else '') for row in experiences)

    @staticmethod
    def _sources(sources: Any, messages: dict[int, Any]) -> None:
        if (not isinstance(sources, list) or not 1 <= len(sources) <= 8
                or any(type(sequence) is not int or sequence not in messages for sequence in sources)):
            raise ValueError('QQ 整理必须使用实际来源消息')

    def commit(self, context: Any, task_id: str, result: dict[str, Any], frozen: dict[str, Any], sources: list[int], expected_summary: int) -> None:
        runtime = self.runtime
        clock = now()
        role_id = runtime.host.role_context(context, task_id)['role_id']
        policy = runtime.app._humanization.task_policy(task_id)
        # 跨库角色读取在本库事务外，避免与宿主事务互相持锁。
        with self.store.transaction() as db:
            actual = db.execute('SELECT revision FROM qq_social_meta').fetchone()[0]
            current = db.execute('SELECT revision FROM qq_summary WHERE account=? AND thread=?', (context.account_id, context.conversation_id)).fetchone()
            if actual != frozen['social_revision'] or (current[0] if current else 0) != expected_summary:
                raise WorkflowBlocked('QQ 整理依据已更新，本次旧结果未覆盖')
            for index, row in enumerate(result['memories']):
                identity = row['id'] or uuid5(NAMESPACE_URL, 'qq-learning:' + task_id + ':' + str(index)).hex
                previous = db.execute('SELECT * FROM qq_social_memory WHERE id=?', (identity,)).fetchone()
                if previous and (previous['account'] != context.account_id or previous['revision'] != row['revision']):
                    raise WorkflowBlocked('QQ 记忆修订冲突')
                thread = previous['thread'] if previous else context.conversation_id
                db.execute('INSERT INTO qq_social_memory VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET scope=excluded.scope,kind=excluded.kind,status=excluded.status,revision=excluded.revision,content=excluded.content,reason=excluded.reason,uncertainty=excluded.uncertainty,review_after=excluded.review_after,evidence=excluded.evidence,updated_at=excluded.updated_at',
                    (identity, context.account_id, thread, row['scope'], row['kind'], row['status'], row['revision'] + 1,
                     row['content'], row['reason'], row['uncertainty'], row['review_after'], canonical(row['sources']), clock, clock))
                db.execute('DELETE FROM qq_social_link WHERE memory_id=?', (identity,))
                for position, link in enumerate(row['people']):
                    db.execute('INSERT INTO qq_social_link VALUES(?,?,?,?,?)', (identity, position, link['person_id'], link['name'], link['relation']))
            if result['memories']:
                db.execute('UPDATE qq_social_meta SET revision=revision+1')
            db.execute('INSERT INTO qq_summary VALUES(?,?,?,1) ON CONFLICT(account,thread) DO UPDATE SET content=excluded.content,revision=revision+1',
                       (context.account_id, context.conversation_id, result['summary']))
            db.executemany('UPDATE qq_message SET organized=1 WHERE sequence=?', ((sequence,) for sequence in sources))
            if result['experiences']:
                records = {row['sequence']: row for row in frozen['messages']}
                events = [{key: records[sequence][key] for key in ('sequence', 'platform_id', 'speaker_id', 'direction', 'created_at', 'thread')}
                    for sequence in sorted({seq for row in result['experiences'] for seq in row['sources']})]
                payload = dict(plugin_id='qq', account_id=context.account_id, source_conversation=context.conversation_id,
                    role_id=role_id, speaker_id='character:' + role_id, speaker_name='当前角色', speaker_kind='character', channel='qq',
                    created_at=clock, use_policy=policy,
                    content=self.experience_content(result['experiences']), events=events)
                if len(canonical(payload).encode()) > 16000:
                    raise ValueError('经历及来源总长度超过接收上限')
                db.execute("INSERT INTO qq_experience_outbox(task_id,payload,state) VALUES(?,?,'pending')", (task_id, canonical(payload)))
            db.execute("UPDATE qq_learning SET state='completed' WHERE task_id=?", (task_id,))

    def deliver(self) -> None:
        for row in self.store.rows("SELECT * FROM qq_experience_outbox WHERE state='pending' ORDER BY rowid LIMIT 8"):
            try:
                receipt = self.runtime.host.receive_experiences(row['task_id'], json.loads(row['payload']))
                with self.store.transaction() as db:
                    db.execute("UPDATE qq_experience_outbox SET state='received',receipt=?,attempts=attempts+1,error='' WHERE task_id=?", (canonical(receipt), row['task_id']))
            except Exception:
                with self.store.transaction() as db:
                    db.execute("UPDATE qq_experience_outbox SET attempts=attempts+1,error='主体暂未接收，将在后续空闲边界重试' WHERE task_id=?", (row['task_id'],))

    def validate_experience(self, context: Any, payload: dict[str, Any]) -> None:
        # 宿主接收前比对已提交的固定发件箱，而不是相信外部正文自称身份。
        rows = self.store.rows('SELECT payload FROM qq_experience_outbox WHERE task_id=(SELECT task_id FROM qq_work WHERE id=?)', (context.work_id,))
        if not rows or rows[0]['payload'] != canonical(payload):
            raise WorkflowBlocked('经历未在插件整理事务中提交')
        text(payload['content'], 8000)
