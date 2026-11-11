"""可信内部管理适配。仍经 Tool Manager 校验参数、权限和实际回执。"""
from collections.abc import Mapping
from dataclasses import asdict
from hashlib import sha256
from deskpal.workflow.store import canonical
from deskpal.workflow.models import WorkflowBlocked
from typing import Any

from deskpal.memory.contracts import UsePolicy
from deskpal.tools.models import PermissionLevel, ToolContext, ToolResult, ToolSpec


class ManagementTool:
    def __init__(self, app: Any, name: str, description: str, properties: dict[str, Any], required: list[str]) -> None:
        self.app, self.name = app, name
        self.spec = ToolSpec(name, description, {"type": "object", "properties": properties,
            "required": required, "additionalProperties": False},
            PermissionLevel.CONFIRM if name == "memory_forget" else PermissionLevel.AUTOMATIC)

    def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        app = self.app
        output: dict[str, Any]
        origin = app.journal.origin(context.task_id)['kind']
        if origin == 'proactive' and self.name not in {'task_list', 'notification_status', 'memory_search', 'role_read', 'attention_schedule', 'attention_end'}:
            raise WorkflowBlocked('Proactive work cannot impersonate explicit user management intent')
        command = 'tool:' + sha256(canonical([context.task_id, self.name, dict(arguments)]).encode()).hexdigest()
        if self.name == 'notification_status':
            output = app.notification_policy.snapshot()
        elif self.name == 'task_list':
            from deskpal.tasks.models import TaskStatus
            page = app.tasks.page_summaries(statuses=(TaskStatus(arguments.get('status', 'Paused')),), limit=20)
            items = []
            for row in page['items']:
                task = app.tasks.get(row['id'])
                if task is None or task.id == context.task_id:
                    continue
                policy = app.tasks.task_use_policy(task.id)
                if app.config.llm.is_cloud and policy == 'local_only':
                    continue
                items.append(dict(id=task.id, status=task.status.value, description=task.request[:160], pause_control=app.task_control.snapshot(task.id)))
            output = dict(items=items, has_more=page['has_more'])
        elif self.name == "memory_search":
            bundle = app._memory_runtime.retrieve(arguments["query"])
            app.journal.set_dependencies(context.task_id, bundle.revisions)
            app.core._task_dependencies[context.task_id] = tuple(sorted(set(
                (*app.core._task_dependencies.get(context.task_id, ()), *bundle.revisions))))
            app._interaction.admit_tool_memories(context.task_id, bundle.revisions)
            output = {"items": [{"id": item.id, "content": item.content} for item in bundle.records],
                      "revisions": [list(ref) for ref in bundle.revisions]}
        elif self.name == "memory_remember":
            item = app._memory_runtime.remember(arguments["content"], task_id=context.task_id,
                use_policy=UsePolicy(app._humanization.task_policy(context.task_id)))
            turn = app._humanization.turn(context.task_id)
            if turn:
                app.learning.store.link(item.id, [dict(kind='dialogue_turn', id=context.task_id + ':user',
                    revision=turn['sequence'], conversation_id=turn['conversation_id'], available=True)])
            output = {"id": item.id, "revision": item.revision, "content": item.draft.content, "status": "saved"}
        elif self.name == "memory_correct":
            item = app._memory_runtime.correct(arguments["memory_id"], arguments["expected_revision"], arguments["content"])
            app._conversation_repository.invalidate_memory_summaries(arguments['memory_id'])
            output = {"id": item.id, "previous_id": arguments["memory_id"], "revision": item.revision, "content": item.draft.content, "status": "saved"}
        elif self.name == "memory_forget":
            output = app.forget_during_task(arguments['memory_id'], arguments['expected_revision'], context.task_id)
        elif self.name == "role_read":
            turn = app._humanization.turn(context.task_id)
            output = app.core.companion_runtime.context(turn["conversation_id"], turn["epoch"], context.task_id,
                                                       allow_restricted=not app.config.llm.is_cloud)
        elif self.name == "role_update":
            runtime = app.core.companion_runtime
            runtime.command("character_card", text=arguments["text"], expected_revision=arguments["expected_revision"],
                            command_id="role-tool:" + context.task_id,
                            use_policy=app._humanization.task_policy(context.task_id))
            snapshot = runtime.interaction_snapshot()
            output = {"revision": snapshot["revision"], "character_card": snapshot["character_card"], "status": "saved"}
        elif self.name == 'notification_control':
            output = app.notification_control(arguments['kind'], arguments['paused'], command, arguments.get('until_at'))
        elif self.name in {'task_pause', 'task_continue'}:
            target = arguments['task_id']
            if self.name == 'task_continue' and target == context.task_id:
                raise ValueError('Current task is already executing')
            result = app.pause_task(target, command) if self.name == 'task_pause' else app.continue_task(target, command)
            output = dict(task_id=result.id, status=result.status.value, pause_control=app.task_control.snapshot(target))
        elif self.name == 'attention_schedule':
            output = dict(id=app.proactive.schedule(context.task_id, arguments['goal'], arguments['reason'], arguments['due_at'], arguments.get('mode', 'chat')), status='scheduled')
        elif self.name == 'attention_end':
            with app.store.transaction() as db:
                db.execute("UPDATE attention SET status='ended' WHERE task_id=? AND status='running'", (context.task_id,))
            output = dict(status='ended')
        elif self.name == "task_cancel":
            if arguments["task_id"] == context.task_id:
                raise ValueError("Use the task cancellation control for the current task")
            output = asdict(app.cancel_task(arguments["task_id"]))
            output["status"] = output["status"].value
            output["created_at"] = output["created_at"].isoformat()
            output["updated_at"] = output["updated_at"].isoformat()
        else:
            raise ValueError("Unsupported management operation")
        return ToolResult(True, output, tool_name=self.name)


def management_tools(app: Any) -> tuple[ManagementTool, ...]:
    text = {"type": "string", "minLength": 1, "maxLength": 8192}
    identity = {"type": "string", "minLength": 1, "maxLength": 200}
    revision = {"type": "integer", "minimum": 1}
    return (
        ManagementTool(app, 'task_list', '查找当前档案的实际任务编号与状态，用于理解用户要求暂停或继续哪项任务；默认列已暂停，目标不明确时询问用户', {'status': {'type':'string','enum':['Created','Planning','Running','Waiting','AwaitingInput','Paused']}}, []),
        ManagementTool(app, 'notification_status', '查看主动聊天与提醒各自的暂停状态及恢复时间', {}, []),
        ManagementTool(app, 'notification_control', '按用户明确要求暂停或恢复主动聊天(chat)或提醒(reminder)，until_at为带时区的恢复时间；两种范围分别设置，不修改日程', {'kind': {'type':'string','enum':['chat','reminder']}, 'paused': {'type':'boolean'}, 'until_at': {'type':'string'}}, ['kind','paused']),
        ManagementTool(app, 'task_pause', '按用户要求暂停指定任务；正在暂停不代表立即中断，当前任务也可请求暂停', {'task_id': identity}, ['task_id']),
        ManagementTool(app, 'task_continue', '按用户明确要求继续指定已暂停任务，不代表授权或重做已完成操作', {'task_id': identity}, ['task_id']),
        ManagementTool(app, 'attention_schedule', '统一安排后续复查，包括记忆、习惯、未决事项和情绪；mode=internal内部维护，mode=chat考虑主动交流（默认）。不是提醒或操作授权，最短间隔15分钟', {'goal':text, 'reason':text, 'due_at':identity, 'mode':{'type':'string','enum':['internal','chat']}}, ['goal','reason','due_at']),
        ManagementTool(app, 'attention_end', '结束当前主动关注，不再追问；随后安静结束', {}, []),
        ManagementTool(app, "memory_search", "检索当前角色记忆；更正或遗忘前读取真实id和revision", {"query": text}, ["query"]),
        ManagementTool(app, "memory_remember", "保存用户明确要求记住的内容；不要将推测直接当事实", {"content": text}, ["content"]),
        ManagementTool(app, "memory_correct", "更正一条当前记忆，必须使用实际revision", {"memory_id": identity, "expected_revision": revision, "content": text}, ["memory_id", "expected_revision", "content"]),
        ManagementTool(app, "memory_forget", "按真实 id 和 revision 遗忘指定提炼记忆及其修订、索引；保留聊天、其他记忆、人格、执行记录和安排。仅用于明确遗忘请求，事项完成应更新状态。Pending 仅表示受理，不能声称清理完成。", {"memory_id": identity, "expected_revision": revision}, ["memory_id", "expected_revision"]),
        ManagementTool(app, "role_read", "读取当前角色卡及其修订", {}, []),
        ManagementTool(app, "role_update", "按用户明确意图修改角色卡；先读取当前修订", {"text": text, "expected_revision": revision}, ["text", "expected_revision"]),
        ManagementTool(app, "task_cancel", "取消指定其它任务的后续步骤，不撤销已经完成的操作", {"task_id": identity}, ["task_id"]),
    )
