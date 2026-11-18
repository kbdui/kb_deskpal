"""QQ 首版：签名接收 → 持久去重 → 同一 Dispatcher/Core → 受限文字回复。"""
from datetime import datetime, timezone
from dataclasses import asdict
import json
import re
from threading import RLock
from typing import Any

from deskpal.llm.models import ChatMessage
from deskpal.plugins.execution import TaskExecutionContext
from deskpal.plugins.manifest import PluginManifest
from deskpal.plugins.runtime import PageSpec, PluginCommandError
from deskpal.credentials import CredentialStoreError
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import PermissionLevel, ToolResult, ToolSpec
from deskpal.workflow.models import WorkflowBlocked
from deskpal.workflow.store import canonical
from .interaction import QqFeedback, QqInteraction
from .secrets import QqSecrets
from .store import QqStore, now
from .social import SocialMemory
from .followups import QqFollowups
from .transport import EventReceiver, OneBotClient, ProtocolError, endpoint


MANIFEST = PluginManifest('qq', 'QQ 聊天', '通过 NapCat 进行已授权的文字私聊和群聊',
                          event_sources=('message', 'review'), storage=True, page_title='QQ 聊天')
DEFAULT = dict(enabled=False, paused=False, endpoint='http://127.0.0.1:3000', event_port=8780,
               account_id='', nickname='', private_ids=[], group_ids=[], group_mode='mention', profile_key='', allow_proactive=False,
               group_interval_seconds=60, direct_call_cooldown=True)


def ids(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [item.strip() for item in re.split(r'[,，\s]+', value) if item.strip()]
    if (not isinstance(value, list) or len(value) > 100 or any(not isinstance(item, str)
            or not re.fullmatch(r'\*|[0-9]{5,20}', item) for item in value)
            or '*' in value and len(value) != 1):
        raise ValueError('授权范围请填写 QQ/群号码，以逗号分隔；* 表示明确允许全部来信')
    return list(dict.fromkeys(value))


class QqTool:
    def __init__(self, runtime: 'QqRuntime', name: str) -> None:
        self.runtime, self.name = runtime, name
        descriptions = dict(qq_send='向当前已授权 QQ 会话发送文字（插件内润色及分条）',
            qq_history='按需回看当前 QQ 会话原文，可按关键词或更早位置查询',
            qq_memory_search='查询 QQ 长期人物认知、转述归属和相处约定',
            qq_host_memory_search='按相关性查询同一角色的主体私人记忆，保留来源，模型判断是否适合透露',
            qq_schedule_followup='安排当前 QQ 会话的后续内部复查或已预授权的主动聊天，时间在未来六天内',
            qq_cancel_followup='取消当前 QQ 会话中尚未完成的后续跟进，已发送消息保留')
        properties = ({'draft': {'type': 'string'}, 'mention_ids': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 6},
                       'reply_to_message_id': {'type': 'string'}} if name == 'qq_send' else
            {'query': {'type': 'string'}, 'before_sequence': {'type': 'integer', 'minimum': 1}, 'message_id': {'type': 'string'}} if name == 'qq_history' else
            {'goal': {'type': 'string'}, 'reason': {'type': 'string'}, 'due_at': {'type': 'string'},
             'mode': {'type': 'string', 'enum': ['internal', 'chat']}, 'requester_sequence': {'type': 'integer', 'minimum': 1}} if name == 'qq_schedule_followup' else
            {'id': {'type': 'string'}} if name == 'qq_cancel_followup' else {'query': {'type': 'string'}})
        self.spec = ToolSpec(name, descriptions[name],
            {'type': 'object', 'properties': properties,
             'required': ['draft'] if name == 'qq_send' else ['query'] if name.endswith('memory_search') else
                ['goal', 'reason', 'due_at', 'mode'] if name == 'qq_schedule_followup' else ['id'] if name == 'qq_cancel_followup' else [],
             'additionalProperties': False}, PermissionLevel.AUTOMATIC)

    def execute(self, arguments: Any, context: Any) -> ToolResult:
        try:
            return self._execute(arguments, context)
        except WorkflowBlocked:
            raise
        except ValueError as error:
            return ToolResult(False, error=str(error))

    def _execute(self, arguments: Any, context: Any) -> ToolResult:
        channel = self.runtime.host.context(context.task_id)
        self.runtime.check_context(channel)
        if self.name == 'qq_history':
            rows = self.runtime.store.history(channel.account_id, channel.conversation_id,
                str(arguments.get('query', '')), arguments.get('before_sequence'), str(arguments.get('message_id', '')))
            self.runtime.interaction.history_reads.setdefault(context.task_id, set()).update(row['sequence'] for row in rows)
            return ToolResult(True, [{key: row[key] for key in ('sequence', 'platform_id', 'speaker_id', 'speaker_name', 'direction', 'content', 'created_at', 'reply_to', 'mentions')} for row in rows])
        if self.name == 'qq_memory_search':
            return ToolResult(True, self.runtime.social.view(channel, str(arguments['query']), search=True))
        if self.name == 'qq_host_memory_search':
            from deskpal.llm.context import memory_view
            bundle = self.runtime.interaction.recall(context.task_id, str(arguments['query']))
            return ToolResult(True, [memory_view(record.content) for record in bundle.records])
        if self.name == 'qq_schedule_followup':
            return ToolResult(True, self.runtime.followups.schedule(channel, context.task_id, context.call_id, dict(arguments)))
        if self.name == 'qq_cancel_followup':
            return ToolResult(True, self.runtime.followups.cancel(channel, str(arguments['id'])))
        delivery = {key: arguments[key] for key in ('mention_ids', 'reply_to_message_id') if arguments.get(key)}
        return self.runtime.send(context.task_id, context.call_id, str(arguments['draft']), delivery)


class QqRuntime:
    tool_names = ('qq_send', 'qq_history', 'qq_memory_search', 'qq_host_memory_search', 'qq_schedule_followup', 'qq_cancel_followup')
    read_only_tools = frozenset({'qq_history', 'qq_memory_search', 'qq_host_memory_search'})

    def __init__(self, app: Any, host: Any) -> None:
        self.app, self.host = app, host
        self.store = QqStore(app.config.storage.data_dir / 'plugin_qq.sqlite3')
        self.social = SocialMemory(self)
        self.followups = QqFollowups(self)
        self.secrets = QqSecrets(app.config.storage.data_dir)
        self.interaction, self.feedback = QqInteraction(self), QqFeedback(self)
        self.receiver: EventReceiver | None = None
        self.lock = RLock()
        self.error = ''
        self.connected = False
        self.stopping = False
        self.closed = False
        self.app.plugins.register_page('qq', PageSpec((), lambda *_: [], detail=self.page, action=self.page_command))
        self.app.plugins.register_tick('qq', self.tick)

    def settings(self) -> tuple[dict[str, Any], int]:
        value, revision = self.app.plugins.data('qq').state('connection', DEFAULT)
        if not isinstance(value, dict):
            raise ValueError('QQ 配置结构无效，请检查当前档案配置')
        config = {**DEFAULT, **value}
        if (any(type(config[key]) is not bool for key in ('enabled', 'paused', 'allow_proactive', 'direct_call_cooldown'))
                or type(config['group_interval_seconds']) is not int or config['group_interval_seconds'] not in {30, 60, 180}
                or type(config['event_port']) is not int or not 1024 <= config['event_port'] <= 65535
                or not isinstance(config['group_mode'], str) or config['group_mode'] not in {'mention', 'all'}
                or any(not isinstance(config[key], str) for key in ('account_id', 'nickname', 'profile_key'))
                or any(not isinstance(config[key], list) for key in ('private_ids', 'group_ids'))):
            raise ValueError('QQ 配置字段无效，请重新核对连接设置')
        endpoint(config['endpoint'])
        ids(config['private_ids']); ids(config['group_ids'])
        return config, revision

    @staticmethod
    def permitted(config: dict[str, Any], kind: str, target: str) -> bool:
        scope = config['private_ids' if kind == 'private' else 'group_ids']
        return target in scope or '*' in scope

    def check_context(self, context: TaskExecutionContext | None) -> None:
        config, _ = self.settings()
        if (context is None or context.plugin_id != 'qq' or context.channel != 'qq'
                or context.conversation_kind not in {'private', 'group'} or self.stopping
                or context.trigger not in {'message', 'review', 'followup'} or context.delivery != 'tools'
                or context.trigger == 'followup' and not config['allow_proactive']
                or context.trigger == 'review' and self.app.config.memory.mode == 'paused'
                or not config['enabled'] or config['paused'] or context.account_id != config['account_id']
                or config['profile_key'] != self.secrets.identity
                or not self.permitted(config, context.conversation_kind, context.conversation_id.split(':')[-1])):
            raise WorkflowBlocked('QQ 已暂停、账号已变化或当前会话没有授权；本次未继续操作。')
        work = self.store.rows('SELECT account,thread,principal,state,trigger FROM qq_work WHERE id=?', (context.work_id,))
        if (not work or (work[0]['account'], work[0]['thread'], work[0]['principal']) !=
                (context.account_id, context.conversation_id, context.principal_id)
                or work[0]['state'] not in {'queued', 'running'}
                or work[0]['trigger'] != context.trigger
                or not re.fullmatch(context.conversation_kind + r':[0-9]{5,20}', context.conversation_id)):
            raise WorkflowBlocked('QQ 任务与实际来信会话不匹配')

    def allowed(self, context: TaskExecutionContext, call: Any) -> bool:
        try:
            self.check_context(context)
            if context.trigger == 'review' and call.name == 'qq_send':
                return False
            if call.name == 'qq_schedule_followup' and call.arguments.get('mode') == 'chat' and not self.settings()[0]['allow_proactive']:
                return False
            return call.name in {*self.tool_names, 'time'}
        except WorkflowBlocked:
            return False

    def start(self) -> None:
        try:
            config, _ = self.settings()
            if not config['enabled']:
                return
            if config['profile_key'] != self.secrets.identity:
                self.error = '档案路径已变化或来自备份恢复，请重新核对账号与授权范围并保存；不会自动发送旧积压。'
                return
            info = OneBotClient(config['endpoint'], self.secrets.get('token')).call('get_login_info')
            if str(info.get('user_id', '')) != config['account_id']:
                raise ValueError('NapCat 当前登录账号与绑定账号不同，请在插件页面重新核对。')
            receiver = EventReceiver(config['event_port'], self.secrets.get('event_secret'), self.receive)
            receiver.start()
            self.receiver, self.connected, self.error = receiver, True, ''
            self.app._dispatcher.enqueue_internal('qq:inbox', self.poll)
        except Exception as error:
            self.connected = False
            self.error = str(error) if isinstance(error, (ValueError, ProtocolError)) else 'QQ 连接未启动，请检查 NapCat、签名密钥及本机端口。'

    def stop_receiver(self) -> None:
        receiver, self.receiver = self.receiver, None
        self.connected = False
        if receiver:
            receiver.close()

    def page_command(self, action: str, parameters: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.command(action, parameters)
        except (ValueError, ProtocolError, CredentialStoreError) as error:
            raise PluginCommandError(str(error)) from None

    def command(self, action: str, parameters: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            config, revision = self.settings()
            if action == 'refresh':
                return self.page()
            if action == 'pause':
                if type(parameters.get('paused')) is not bool or type(parameters.get('expected_revision')) is not int or parameters.get('expected_revision') != revision:
                    raise ValueError('QQ 状态已变化，请刷新后重试')
                config['paused'] = parameters['paused']
                self.app.plugins.data('qq').set_state('connection', config, expected_revision=revision)
                if config['paused']:
                    with self.store.transaction() as db:
                        db.execute('UPDATE qq_message SET eligible=0 WHERE work_id IS NULL')
                        db.execute("UPDATE qq_work SET state='blocked',error='QQ 已暂停，原工作不自动续发' WHERE state IN ('queued','running')")
                        db.execute("UPDATE qq_followup SET state='blocked',error='QQ 已暂停，旧主动跟进不自动补发' WHERE state IN ('pending','running') AND mode='chat'")
                self.app._dispatcher.enqueue_internal('qq:inbox', self.poll)
                return self.page()
            if action != 'configure':
                raise ValueError('未知 QQ 页面操作')
            allowed = {'endpoint', 'event_port', 'account_id', 'private_ids', 'group_ids', 'group_mode',
                       'token', 'event_secret', 'enabled', 'allow_proactive', 'expected_revision',
                       'group_interval_seconds', 'direct_call_cooldown'}
            if set(parameters) - allowed or type(parameters.get('expected_revision')) is not int or parameters.get('expected_revision') != revision:
                raise ValueError('QQ 配置版本或字段无效，请重新读取')
            enabled = parameters.get('enabled', False)
            if type(enabled) is not bool:
                raise ValueError('QQ 启用状态须为布尔值')
            port = parameters.get('event_port', 8780)
            if type(port) is not int or not 1024 <= port <= 65535:
                raise ValueError('回调端口须为 1024–65535 的整数')
            base = endpoint(parameters.get('endpoint', config['endpoint']))
            mode = parameters.get('group_mode', 'mention')
            if mode not in {'mention', 'all'}:
                raise ValueError('群聊响应方式无效')
            account = str(parameters.get('account_id', '')).strip()
            if account and not re.fullmatch(r'[0-9]{5,20}', account):
                raise ValueError('绑定账号请填写 QQ 号码')
            updated = dict(config, enabled=enabled, endpoint=base, event_port=port, group_mode=mode,
                private_ids=ids(parameters.get('private_ids', [])), group_ids=ids(parameters.get('group_ids', [])),
                allow_proactive=parameters.get('allow_proactive', False),
                group_interval_seconds=parameters.get('group_interval_seconds', config['group_interval_seconds']),
                direct_call_cooldown=parameters.get('direct_call_cooldown', config['direct_call_cooldown']))
            if (type(updated['group_interval_seconds']) is not int or updated['group_interval_seconds'] not in {30, 60, 180}
                    or type(updated['direct_call_cooldown']) is not bool):
                raise ValueError('普通群聊思考间隔须为30秒、1分钟或3分钟，直接呼叫限频须为开关值')
            if type(updated['allow_proactive']) is not bool:
                raise ValueError('主动聊天许可须为布尔值')
            supplied = {key: parameters.get(key, '') for key in ('token', 'event_secret')}
            for key, value in supplied.items():
                if not isinstance(value, str) or len(value) > 1000 or any(ord(char) < 32 for char in value):
                    raise ValueError('QQ Token 或签名密钥格式无效')
            token = supplied['token'] or self.secrets.get('token')
            secret = supplied['event_secret'] or self.secrets.get('event_secret')
            if enabled:
                if len(secret) < 16:
                    raise ValueError('请先设置至少 16 个字符的事件签名密钥，并在 NapCat HTTP 客户端的 Token 栏填写相同内容')
                info = OneBotClient(base, token).call('get_login_info')
                actual = str(info.get('user_id', ''))
                if not re.fullmatch(r'[0-9]{5,20}', actual) or account and account != actual:
                    raise ValueError('NapCat 当前登录 QQ 与填写账号不一致，本次未启用')
                updated.update(account_id=actual, nickname=str(info.get('nickname', ''))[:120])
                updated['profile_key'] = self.secrets.identity
            for key, value in supplied.items():
                if value:
                    self.secrets.put(key, value)
            self.stop_receiver()
            if updated['group_mode'] != 'all':
                with self.store.transaction() as db:
                    db.execute("UPDATE qq_message SET eligible=0 WHERE thread LIKE 'group:%' AND direct_call=0 AND work_id IS NULL")
            if (not enabled or updated['account_id'] != config['account_id']
                    or config['profile_key'] and config['profile_key'] != self.secrets.identity):
                with self.store.transaction() as db:
                    db.execute('UPDATE qq_message SET eligible=0 WHERE work_id IS NULL')
                    db.execute("UPDATE qq_work SET state='blocked',error='连接、账号或档案已变化，旧工作未自动继续' WHERE state IN ('queued','running')")
                    db.execute("UPDATE qq_followup SET state='blocked',error='连接、账号或档案已变化，旧跟进未自动继续' WHERE state IN ('pending','running')")
            self.app.plugins.data('qq').set_state('connection', updated, expected_revision=revision)
            self.start()
            self.app._dispatcher.wake()
            return self.page()

    def page(self) -> dict[str, Any]:
        config, revision = self.settings()
        records = self.store.rows('SELECT speaker_id,speaker_name,direction,content,created_at,thread FROM qq_message WHERE account=? ORDER BY sequence DESC LIMIT 15', (config['account_id'],))
        blocked = self.store.rows("SELECT b.thread,c.position,c.content,c.state,c.error FROM qq_send_command c JOIN qq_send_batch b ON b.id=c.batch_id WHERE c.state IN ('unknown','failed') ORDER BY b.created_at DESC LIMIT 10")
        for record in (*records, *blocked):
            record['content'] = record['content'][:300]
        work = self.store.rows('SELECT state,error,created_at FROM qq_work WHERE account=? ORDER BY created_at DESC LIMIT 10', (config['account_id'],))
        memories = self.store.rows("SELECT kind,status,content,reason,uncertainty,review_after FROM qq_social_memory WHERE account=? AND status!='archived' ORDER BY updated_at DESC LIMIT 8", (config['account_id'],))
        for item in memories:
            for key in ('content', 'reason', 'uncertainty'):
                item[key] = item[key][:300]
        experiences = self.store.rows('SELECT task_id,state,error FROM qq_experience_outbox ORDER BY rowid DESC LIMIT 8')
        followups = self.store.rows("SELECT goal,mode,due_at,state FROM qq_followup WHERE account=? AND state IN ('pending','running') ORDER BY due_at LIMIT 8", (config['account_id'],))
        page = dict(kind='qq_chat', config={key: value for key, value in config.items() if key != 'profile_key'}, revision=revision, connected=self.connected,
            model_ready=self.app.config.llm.ready and self.app.config.llm.provider != 'offline',
            error=self.error, callback=f"http://127.0.0.1:{config['event_port']}/onebot/events",
            records=records, blocked=blocked, work=work, memories=memories, experiences=experiences, followups=followups,
            instructions='NapCat 在项目外运行并登录专属 QQ。启用 HTTP 服务器（本机地址/端口与本页一致，服务器 Token 填入本页 OneBot Token）；再配置 HTTP 客户端，将上报 URL 填为本页回调地址，客户端 Token 填入与本页事件签名密钥相同的内容，消息格式选 array。本机 NapCat 使用客户端 Token 生成 HMAC-SHA1 签名。两处 Token 都不等于 WebUI 登录密码。仅支持文字；未授权会话不交给模型，暂停期间的新消息不在恢复后批量补发。QQ 内容会交给当前设置的模型。')
        while len(canonical(page).encode()) > 60000:
            longest = max((records, blocked, work, memories, experiences, followups), key=lambda items: len(canonical(items).encode()))
            if not longest:
                raise ValueError('QQ 页面配置超过显示上限')
            longest.pop()
        return page

    def receive(self, event: dict[str, Any]) -> None:
        config, _ = self.settings()
        if self.stopping or not config['enabled'] or str(event.get('self_id', '')) != config['account_id']:
            return
        if event.get('post_type') not in {'message', 'message_sent'} or event.get('message_type') not in {'private', 'group'}:
            return
        kind = event['message_type']
        sender = str(event.get('user_id', ''))
        outgoing = sender == config['account_id'] or event.get('post_type') == 'message_sent'
        target = str(event.get('group_id', '') if kind == 'group' else (event.get('target_id') or (sender if sender != config['account_id'] else '')) if outgoing else sender)
        if outgoing:
            sender = config['account_id']
        raw_message_id = event.get('message_id')
        platform_id = str(raw_message_id)
        if (not re.fullmatch(r'[0-9]{5,20}', sender) or not re.fullmatch(r'[0-9]{5,20}', target)
                or type(raw_message_id) not in {str, int} or not re.fullmatch(r'-?[0-9]{1,30}', platform_id)):
            return
        message = event.get('message')
        mentioned = False
        has_text = False
        reply_to: str | None = None
        mentions: list[str] = []
        if isinstance(message, list):
            if len(message) > 100:
                return  # 不截断消息尾部，以免遗漏后面的否定或修改要求。
            chunks = []
            for segment in message:
                if not isinstance(segment, dict) or not isinstance(segment.get('data'), dict):
                    continue
                if segment.get('type') == 'text':
                    text = str(segment['data'].get('text', ''))
                    chunks.append(text)
                    has_text |= bool(text.strip())
                elif segment.get('type') == 'at':
                    who = str(segment['data'].get('qq', ''))
                    mentioned |= who == config['account_id']
                    chunks.append('@' + who)
                    has_text |= bool(re.fullmatch(r'[0-9]{5,20}', who))
                    if re.fullmatch(r'[0-9]{5,20}', who):
                        mentions.append(who)
                elif segment.get('type') == 'reply':
                    reference = str(segment['data'].get('id', ''))
                    if re.fullmatch(r'-?[0-9]{1,30}', reference):
                        reply_to = reference
                else:
                    chunks.append('[非文字内容]')
            body = ''.join(chunks)
        elif isinstance(message, str):
            mentioned = '[CQ:at,qq=' + config['account_id'] + ']' in message
            has_text = bool(re.sub(r'\[CQ:[^\]]*\]', '', message).strip()) or mentioned
            body = re.sub(r'\[CQ:[^\]]*\]', '[非文字内容]', message)
        else:
            return
        if not body.strip() or len(body.encode('utf-8')) > 8000:
            return
        # 未授权来信不落库；避免接收端变成整个账号的无边界聊天镜像。
        if not self.permitted(config, kind, target):
            return
        if kind == 'group' and reply_to:
            original = self.store.rows('SELECT direction FROM qq_message WHERE account=? AND thread=? AND platform_id=?',
                                       (config['account_id'], kind + ':' + target, reply_to))
            mentioned |= bool(original and original[0]['direction'] == 'out')
        stamp = datetime.fromtimestamp(float(event.get('time', 0)), timezone.utc).isoformat()
        nickname = event.get('sender', {})
        nickname = str(nickname.get('card') or nickname.get('nickname') or sender) if isinstance(nickname, dict) else sender
        speaker_id = 'qq:' + sender
        if kind == 'group' and (event.get('anonymous') or event.get('sub_type') == 'anonymous'):
            # 协议的匿名占位 QQ 号不代表稳定人物；每条匿名事件单独标识。
            speaker_id = 'qq:anonymous:' + target + ':' + platform_id
            nickname = '匿名发言'
        with self.lock:
            latest, _ = self.settings()
            if (self.stopping or not latest['enabled'] or latest['account_id'] != config['account_id']
                    or not self.permitted(latest, kind, target)):
                return
            eligible = not outgoing and has_text and not latest['paused'] and (kind == 'private' or latest['group_mode'] == 'all' or mentioned)
            inserted = self.store.receive(config['account_id'], kind + ':' + target, platform_id, speaker_id, nickname, body, stamp,
                                          outgoing=outgoing, eligible=eligible, reply_to=reply_to, mentions=tuple(mentions), direct_call=kind == 'group' and mentioned)
        if inserted:
            self.app._dispatcher.enqueue_internal('qq:inbox', self.poll)

    def poll(self) -> None:
        try:
            self._poll()
        except WorkflowBlocked:
            self.error = 'QQ 账号、暂停或授权状态已变化，已保留消息，未继续操作。'
        except Exception:
            self.error = 'QQ 收件处理暂不可用，消息保留在插件库；请检查连接和最近处理记录。'

    def tick(self) -> None:
        if self.stopping or not self.connected:
            return
        config, _ = self.settings()
        if config['enabled'] and not config['paused']:
            self.app._dispatcher.enqueue_internal('qq:inbox', self.poll)

    def _poll(self) -> None:
        if self.stopping:
            return
        self.social.deliver()
        with self.store.transaction() as db:
            db.execute("UPDATE qq_followup SET state='blocked',error='原工作已暂停、取消或失效，未自动补发' WHERE state='running' AND work_id IN (SELECT id FROM qq_work WHERE state='blocked')")
        for work in self.store.rows("SELECT * FROM qq_work WHERE state='running'"):
            task = self.app.tasks.get(work['task_id'])
            if task and task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.NEEDS_REVIEW, TaskStatus.UNCERTAIN}:
                self.interaction.prepared.pop(task.id, None)
                self.interaction.retrieval.pop(task.id, None)
                self.interaction.history_reads.pop(task.id, None)
                payload = task.result if isinstance(task.result, dict) else {}
                failures = [str(row.get('error') or '工具未完成') for row in payload.get('tool_results', []) if not row.get('success')]
                with self.store.transaction() as db:
                    db.execute('UPDATE qq_work SET state=?,error=? WHERE id=?',
                        ('completed' if task.status == TaskStatus.COMPLETED else 'blocked', (task.error or '; '.join(failures))[:500], work['id']))
                self.followups.settle(work['id'], task.status == TaskStatus.COMPLETED)
        config, _ = self.settings()
        if (not config['enabled'] or config['paused'] or not self.connected
                or not self.app.config.llm.ready or self.app.config.llm.provider == 'offline'):
            return
        self.error = ''
        for thread in self.store.rows('SELECT DISTINCT thread FROM qq_message WHERE account=? AND eligible=1 AND work_id IS NULL', (config['account_id'],)):
            kind, target = thread['thread'].split(':')
            if not self.permitted(config, kind, target):
                with self.store.transaction() as db:
                    db.execute('UPDATE qq_message SET eligible=0 WHERE account=? AND thread=? AND work_id IS NULL', (config['account_id'], thread['thread']))
        for queued in self.store.rows("SELECT * FROM qq_work WHERE account=? AND state='queued'", (config['account_id'],)):
            kind, target = queued['thread'].split(':')
            if (not self.permitted(config, kind, target) or queued['trigger'] == 'followup' and not config['allow_proactive']
                    or queued['trigger'] == 'review' and self.app.config.memory.mode == 'paused'):
                with self.store.transaction() as db:
                    db.execute("UPDATE qq_work SET state='blocked',error='现行授权或学习状态不允许原工作继续' WHERE id=?", (queued['id'],))
        pending = self.store.reserve_work(config['account_id'], config=config) or self.followups.next_work(config)
        if pending:
            kind = pending['thread'].split(':')[0]
            context = TaskExecutionContext('qq', 'qq', pending['account'], pending['principal'], pending['thread'], kind, pending['id'], pending['trigger'])
            request = ('处理当前 QQ 会话的实际来信。需要回复时使用 qq_send；不必回复则安静结束。' if pending['trigger'] == 'message' else
                       '处理已登记的 QQ 内部复查；不是新来信，不发送消息。根据认知及来源决定是否整理，然后安静结束。' if pending['trigger'] == 'review' else
                       '处理已登记且预授权的 QQ 后续聊天机会；不是新来信，核对当前场合后决定是否通过 qq_send 发言，也可安静结束。')
            task = self.host.submit(context, request, 'qq:' + pending['id'])
            if context.conversation_kind == 'group' and context.trigger == 'message' and not pending.get('task_id'):
                self.app.store.record_event('qq_group_opportunity', task.id,
                    dict(direct_call=bool(pending['direct_call']), group_interval_seconds=config['group_interval_seconds'],
                         direct_call_cooldown=config['direct_call_cooldown']))
            with self.store.transaction() as db:
                db.execute("UPDATE qq_work SET task_id=?,state='running' WHERE id=? AND state='queued'", (task.id, pending['id']))

    def send(self, task_id: str, call_id: str, draft: str, delivery: dict[str, Any] | None = None) -> ToolResult:
        context = self.host.context(task_id)
        self.check_context(context)
        if not call_id or not draft.strip() or len(draft.encode()) > 6000:
            return ToolResult(False, error='QQ 草稿为空或过长，未发送')
        delivery = delivery or {}
        mentions = delivery.get('mention_ids', [])
        reply_to = delivery.get('reply_to_message_id')
        if (not isinstance(mentions, list) or len(mentions) > 6 or any(not isinstance(who, str)
                or not re.fullmatch(r'[0-9]{5,20}', who) for who in mentions)
                or mentions and context.conversation_kind != 'group'):
            return ToolResult(False, error='只能在当前群中 @ 已知发言者，不支持 @ 全体或其他会话')
        for who in mentions:
            if not self.store.rows('SELECT 1 FROM qq_message WHERE account=? AND thread=? AND speaker_id=? LIMIT 1', (context.account_id, context.conversation_id, 'qq:' + who)):
                return ToolResult(False, error='被 @ 的账号不是当前群内已知发言者，未发送')
        if reply_to and (not isinstance(reply_to, str) or not re.fullmatch(r'-?[0-9]{1,30}', reply_to)
                         or not self.store.rows('SELECT 1 FROM qq_message WHERE account=? AND thread=? AND platform_id=?', (context.account_id, context.conversation_id, reply_to))):
            return ToolResult(False, error='引用消息不属于当前已知会话，未发送')
        batch = self.store.batch(task_id, call_id, context.account_id, context.conversation_id, draft, delivery=delivery)
        if batch is None:
            turn = self.interaction.current_turn(task_id)
            client = self.interaction.calls(task_id, 'plugin_expression')
            from deskpal.llm.context import role_view, memory_view
            facts = [asdict(attempt.result) for attempt in self.app.journal.attempts(task_id)
                     if attempt.result is not None and attempt.result.tool_name not in {'qq_history', 'qq_memory_search', 'qq_host_memory_search'}]
            payload = dict(draft=draft, role=role_view(json.loads(turn.role_json)), memories=[memory_view(record.content) for record in turn.memory_records],
                           qq_social=next((dict(item) for item in turn.planning_items if item.get('kind') == 'qq_social_context'), {}),
                           now=now(), timezone=self.app.config.proactive.timezone, history=list(turn.history), tool_results=facts)
            result = client.chat((ChatMessage('system', '[QQ_EXPRESSION] 将草稿组织成自然 QQ 文字。结合角色资料保留草稿中有依据的自身观点、兴趣、偏好、异议和未定打算，不为迎合联系人改成附和，不刻意反对或补造行动；想象与愿望不当作真实经历。只返回 JSON {"messages":["第一条","第二条"]}，按语义拆为1到6条，短回复一条即可；不逐句调用模型，不输出工具标记。每条至多1500 UTF-8字节，总正文至多6000字节。执行事实以 tool_results 为准，草稿不是成功回执，历史只表示当时交流；不声称已完成尚无回执的学习。'),
                ChatMessage('user', canonical(payload))))
            try:
                data = json.loads(result.content)
            except (ValueError, TypeError):
                return ToolResult(False, error='QQ 表达没有返回有效消息组，本次未发送')
            messages = data.get('messages') if isinstance(data, dict) and set(data) == {'messages'} else None
            if (not isinstance(messages, list) or not 1 <= len(messages) <= 6
                    or any(not isinstance(text, str) or not text.strip() or len(text.encode()) > 1500 for text in messages)
                    or sum(len(text.encode()) for text in messages) > 6000):
                return ToolResult(False, error='QQ 表达格式无效，本次未发送')
            batch = self.store.batch(task_id, call_id, context.account_id, context.conversation_id, draft, messages, delivery=delivery)
        assert batch is not None
        confirmed: list[dict[str, str]] = []
        def stop_remaining(reason: str) -> ToolResult:
            with self.store.transaction() as db:
                db.execute("UPDATE qq_send_command SET state='cancelled',error=? WHERE batch_id=? AND state='pending'", (reason, batch['id']))
                db.execute("UPDATE qq_send_batch SET state='blocked' WHERE id=?", (batch['id'],))
            return ToolResult(False, dict(sent=confirmed), reason)
        for command in self.store.rows('SELECT * FROM qq_send_command WHERE batch_id=? ORDER BY position', (batch['id'],)):
            if command['state'] == 'confirmed':
                confirmed.append(dict(text=command['content'], message_id=command['platform_id'])); continue
            if command['state'] in {'unknown', 'sending'}:
                return ToolResult(False, dict(sent=confirmed), '此前发送结果未知，未重发', outcome='unknown')
            if command['state'] == 'cancelled':
                return ToolResult(False, dict(sent=confirmed), '此消息组的未发部分已停止；请结合当前来信重新判断')
            try:
                self.check_context(context)
            except WorkflowBlocked as error:
                return stop_remaining(str(error))
            latest = self.store.rows("SELECT MAX(sequence) AS n FROM qq_message WHERE account=? AND thread=? AND direction='in'", (context.account_id, context.conversation_id))[0]['n'] or 0
            if confirmed and latest > batch['anchor']:
                return stop_remaining('发送期间收到新来信，后续条目未发，请结合新消息重新判断')
            with self.store.transaction() as db:
                db.execute("UPDATE qq_send_command SET state='sending' WHERE batch_id=? AND position=?", (batch['id'], command['position']))
            config, _ = self.settings()
            try:
                self.check_context(context)
                api = OneBotClient(config['endpoint'], self.secrets.get('token'))
                login = api.call('get_login_info')
                if str(login.get('user_id', '')) != context.account_id:
                    raise ProtocolError('NapCat 当前登录账号已变化，本条未发送，请重新核对绑定账号。')
                self.check_context(context)
                kind, target = context.conversation_id.split(':')
                segments = []
                if command['position'] == 0:
                    if reply_to:
                        segments.append({'type': 'reply', 'data': {'id': reply_to}})
                    segments.extend({'type': 'at', 'data': {'qq': who}} for who in mentions)
                segments.append({'type': 'text', 'data': {'text': command['content']}})
                data = api.call('send_private_msg' if kind == 'private' else 'send_group_msg',
                    {('user_id' if kind == 'private' else 'group_id'): int(target),
                     'message': segments}, effect=True)
                raw_id = data.get('message_id')
                message_id = str(raw_id)
                if type(raw_id) not in {str, int} or not re.fullmatch(r'-?[0-9]{1,30}', message_id):
                    raise ProtocolError('NapCat 没有返回消息编号，无法确认发送', unknown=True)
            except (ProtocolError, WorkflowBlocked) as error:
                unknown = isinstance(error, ProtocolError) and error.unknown
                with self.store.transaction() as db:
                    db.execute('UPDATE qq_send_command SET state=?,error=? WHERE batch_id=? AND position=?',
                        ('unknown' if unknown else 'failed', str(error), batch['id'], command['position']))
                    db.execute("UPDATE qq_send_batch SET state='blocked' WHERE id=?", (batch['id'],))
                return ToolResult(False, dict(sent=confirmed), str(error), outcome='unknown' if unknown else 'known')
            try:
                with self.store.transaction() as db:
                    db.execute("UPDATE qq_send_command SET state='confirmed',platform_id=?,error='' WHERE batch_id=? AND position=?",
                        (message_id, batch['id'], command['position']))
                    db.execute('INSERT OR IGNORE INTO qq_message(account,thread,platform_id,speaker_id,speaker_name,direction,content,created_at,eligible,work_id,reply_to,mentions) VALUES(?,?,?,?,?,?,?,?,0,?,?,?)',
                        (context.account_id, context.conversation_id, message_id, 'qq:' + context.account_id, config['nickname'] or '当前角色', 'out', command['content'], now(), context.work_id,
                         reply_to if command['position'] == 0 else None, json.dumps(mentions if command['position'] == 0 else [])))
            except Exception:
                return ToolResult(False, dict(sent=confirmed), '协议端已响应，但本机发送回执未能保存，已停止继续发送；不要重发。', outcome='unknown')
            confirmed.append(dict(text=command['content'], message_id=message_id))
        try:
            with self.store.transaction() as db:
                db.execute("UPDATE qq_send_batch SET state='completed' WHERE id=?", (batch['id'],))
        except Exception:
            pass  # 每条实际确认回执已提交，汇总标记失败不把成功发送伪装成可重发的失败。
        return ToolResult(True, dict(sent=confirmed, delivered='NapCat 已确认发送，不代表对方已读'))

    def close(self) -> None:
        if self.closed:
            return
        self.stopping = True
        self.stop_receiver()
        self.store.close()
        self.closed = True

    def validate_experience(self, context: TaskExecutionContext, payload: dict[str, Any]) -> None:
        self.social.validate_experience(context, payload)


class UnavailableQq:
    """插件库损坏或不可写时保留说明页，不拖垮主体聊天。"""
    def __init__(self, app: Any) -> None:
        self.stopping = False
        app.plugins.register_page('qq', PageSpec((), lambda *_: [dict(title='QQ 插件未加载',
            text='当前档案的 QQ 数据库无法安全打开。主体聊天仍可使用，请检查插件库后重启；未自动删除或重建数据。')]))

    def start(self) -> None:
        pass

    def poll(self) -> None:
        pass

    def stop_receiver(self) -> None:
        pass

    def close(self) -> None:
        pass
