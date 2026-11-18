"""可信渠道上下文与执行策略；插件不能通过模型参数切换身份、工具或记忆库。"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Any

from deskpal.workflow.models import WorkflowBlocked
from deskpal.workflow.store import canonical


class ChannelDenied(ValueError):
    """渠道越界：返回未执行结果，不转入桌面审批。"""


@dataclass(frozen=True)
class TaskExecutionContext:
    plugin_id: str
    channel: str
    account_id: str
    principal_id: str
    conversation_id: str
    conversation_kind: str
    work_id: str = ''
    trigger: str = 'message'
    delivery: str = 'tools'


class ToolPolicy:
    def __init__(self, host: 'ChannelHost') -> None:
        self.host = host

    def check(self, task_id: str, call: Any) -> bool | None:
        context = self.host.context(task_id)
        if context is None:
            if call.name in self.host.channel_tools():
                raise ChannelDenied('这项能力仅供对应插件的已授权渠道使用，未执行。')
            return None  # 原本机授权机制。
        runtime = self.host.runtime(context)
        if not runtime.allowed(context, call):
            raise ChannelDenied('当前渠道或会话没有这项操作的预先授权，未执行；不会转入桌面审批。')
        return True


class ChannelHost:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.runtimes: dict[str, Any] = {}
        self.policy = ToolPolicy(self)

    def register(self, identity: str, runtime: Any) -> None:
        if identity in self.runtimes:
            raise ValueError('Duplicate channel runtime')
        self.runtimes[identity] = runtime

    def channel_tools(self) -> frozenset[str]:
        return frozenset(name for runtime in self.runtimes.values() for name in runtime.tool_names)

    def validate_task(self, task_id: str, refs: Any = ()) -> None:
        context = self.context(task_id)
        if context:
            self.runtime(context).check_context(context)

    def role_context(self, context: TaskExecutionContext, task_id: str) -> dict[str, Any]:
        """共享同一角色的人格与情绪；本机用户理解保留归属，不冒充 QQ 联系人。"""
        return dict(self.app.core.companion_runtime.context(
            'plugin:' + context.plugin_id + ':' + context.account_id + ':' + context.conversation_id,
            0, task_id, allow_restricted=self.app._interaction.allow_restricted))

    def receive_experiences(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        from .experiences import receive
        return receive(self, task_id, payload)

    def context(self, task_id: str) -> TaskExecutionContext | None:
        with self.app.store.read_connection() as db:
            row = db.execute("SELECT items,digest FROM context_snapshot WHERE task_id=? AND purpose='plugin_execution'", (task_id,)).fetchone()
        if row and sha256(row[0].encode()).hexdigest() != row[1]:
            raise WorkflowBlocked('渠道任务上下文校验失败')
        return TaskExecutionContext(**json.loads(row[0])) if row else None

    def runtime(self, context: TaskExecutionContext) -> Any:
        if context.plugin_id not in self.runtimes:
            raise WorkflowBlocked('来源插件当前不可用')
        return self.runtimes[context.plugin_id]

    def submit(self, context: TaskExecutionContext, request: str, command_id: str) -> Any:
        self.runtime(context).check_context(context)
        body = canonical(asdict(context))
        def register(task: Any) -> None:
            with self.app.store.transaction() as db:
                old = db.execute("SELECT items FROM context_snapshot WHERE task_id=? AND purpose='plugin_execution'", (task.id,)).fetchone()
                if old and old[0] != body:
                    raise WorkflowBlocked('渠道任务编号已用于其他上下文')
                db.execute("INSERT OR IGNORE INTO context_snapshot VALUES(?,?,?,?)",
                           (task.id, 'plugin_execution', sha256(body.encode()).hexdigest(), body))
        return self.app._dispatcher.submit(request, command_id=command_id, authorized_tools=frozenset(),
            high_risk_authorized_tools=frozenset(), register=register)


class ChannelInteraction:
    def __init__(self, host: ChannelHost, default: Any) -> None:
        self.host, self.default = host, default

    def __getattr__(self, key: str) -> Any:
        return getattr(self.default, key)

    def _route(self, task_id: str) -> Any:
        context = self.host.context(task_id)
        return self.host.runtime(context).interaction if context else self.default

    def prepare(self, task_id: str, request: str, bundle: Any) -> Any:
        return self._route(task_id).prepare(task_id, request, bundle)

    def current_turn(self, task_id: str) -> Any:
        return self._route(task_id).current_turn(task_id)

    def prepare_continuation(self, task_id: str) -> None:
        self._route(task_id).prepare_continuation(task_id)

    def apply_decision(self, task_id: str, decision: Any) -> bool:
        return self._route(task_id).apply_decision(task_id, decision)

    def calls(self, task_id: str, purpose: str) -> Any:
        return self._route(task_id).calls(task_id, purpose)

    def check(self, task_id: str, *, expression: bool = False) -> None:
        self._route(task_id).check(task_id, expression=expression)

    def publication(self, task_id: str) -> Any:
        return self._route(task_id).publication(task_id)

    def publish(self, turn: Any, *args: Any, **kwargs: Any) -> None:
        self._route(turn.task_id).publish(turn, *args, **kwargs)

    def close(self) -> None:
        for runtime in self.host.runtimes.values():
            runtime.close()
        self.default.close()


class ChannelFeedback:
    def __init__(self, host: ChannelHost, default: Any) -> None:
        self.host, self.default = host, default

    def _route(self, task_id: str) -> Any:
        context = self.host.context(task_id)
        return self.host.runtime(context).feedback if context else self.default

    def planning_materials(self, task_id: str) -> Any:
        return self._route(task_id).planning_materials(task_id)

    def finish(self, **kwargs: Any) -> Any:
        return self._route(kwargs['task_id']).finish(**kwargs)

    def receipt(self, task_id: str) -> Any:
        return self._route(task_id).receipt(task_id)
