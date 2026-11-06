"""有界的观察—再判断循环。复用 Core、唯一 worker 和持久执行账本。"""

from typing import Any, cast
from time import monotonic

from deskpal.agent.models import AgentResult, AuthorizationRequest
from deskpal.planner.models import Plan
from deskpal.sandbox.errors import PermissionDenied
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import PermissionLevel, ToolContext, ToolResult
from deskpal.workflow.models import WorkflowBlocked

READ_ONLY_TOOLS = frozenset({"time", "echo", "read_text", "document_search", "document_classify",
    "calendar_list", "calendar_events", "timer_list", "timer_events", "memory_search", "role_read", "task_list", "notification_status"})


def execute_rounds(core: Any, task_id: str, request: str, plan: Plan, prepared: Any,
                   memories: Any, authorization: frozenset[str], high_risk: frozenset[str],
                   results: list[ToolResult], warnings: list[str], *, resuming: bool = False) -> Plan | AgentResult:
    journal = core._journal
    attempts = journal.attempts(task_id) if journal else ()
    if any(item.status not in {"Succeeded", "Failed"} for item in attempts):
        raise WorkflowBlocked("Unresolved operation cannot be replayed")
    results.extend(item.result for item in attempts if item.result is not None)
    completed = len(attempts)
    all_calls = list(plan.steps)
    seen = {(call.name, _canonical(call.arguments)) for call in all_calls[:completed]}
    started = monotonic()
    control = getattr(core, "_task_control", None)
    elapsed = control.snapshot(task_id)["elapsed"] if control else 0.0
    round_index = journal.round_count(task_id) if journal else 0
    awaiting_reply = resuming and plan.action in {"clarify", "continue"}
    while True:
        if round_index >= 8 or elapsed + monotonic() - started > 180:
            return cast(AgentResult, core._fail(task_id,
                "本任务已达到轮数或处理时长上限；已完成操作保留，请仅提出尚未完成的要求。", results))
        group = all_calls[completed:]
        if not group and not awaiting_reply:
            return Plan(plan.reply, (), plan.action)
        awaiting_reply = False
        if len(all_calls) > core.max_tool_steps:
            return cast(AgentResult, core._fail(task_id, "任务计划超过工具步骤上限；已完成操作保留，请仅提出尚未完成的要求。", results))
        context = ToolContext(task_id, core.workspace_dir, authorization, high_risk)
        specs = {spec.name: spec for spec in core.tools.list_tools()}
        required: list[str] = []
        group_seen: set[tuple[str, str]] = set()
        high: list[str] = []
        details: list[AuthorizationRequest] = []
        for call in group:
            if journal and journal.origin(task_id)['kind'] == 'proactive' and call.name not in {spec.name for spec in core._planning_tools(task_id)}:
                raise WorkflowBlocked('主动任务不能代替用户发起这项内部管理操作。')
            core._boundary(task_id)
            fingerprint = (call.name, _canonical(call.arguments))
            if call.name not in READ_ONLY_TOOLS and (fingerprint in seen or fingerprint in group_seen):
                raise WorkflowBlocked("模型再次提出已经执行的同一操作，未重复执行。")
            group_seen.add(fingerprint)
            try:
                core.tools.check_permission(call, context)
            except PermissionDenied:
                level = specs[call.name].permission_level
                if call.name not in required:
                    required.append(call.name)
                if level == PermissionLevel.HIGH_RISK and call.name not in high:
                    high.append(call.name)
                details.append(AuthorizationRequest(call.name, dict(call.arguments), call.call_id, level))
        if required:
            core._transition(task_id, TaskStatus.WAITING, result={"response": "请确认本轮列出的操作。",
                "required_authorizations": required, "required_high_risk_authorizations": high,
                "tool_results": [_result(item) for item in results]})
            with core._lock:
                core._pending[task_id] = (request, plan, frozenset(), frozenset())
                core._pending_dependencies[task_id] = core._task_dependencies.get(task_id, ())
            if control and control.checkpoint(task_id, "Waiting", monotonic() - started):
                return AgentResult(task_id, TaskStatus.PAUSED, "任务已暂停，继续后仍需确认原操作。", tuple(results))
            return AgentResult(task_id, TaskStatus.WAITING, "请确认本轮列出的操作。", tuple(results),
                required_authorizations=tuple(required), required_high_risk_authorizations=tuple(high),
                authorization_requests=tuple(details), planned_calls=tuple(group), warnings=tuple(warnings))
        core._pending.pop(task_id, None)
        current = core.tasks.get(task_id)
        if current.status != TaskStatus.RUNNING:
            core._transition(task_id, TaskStatus.RUNNING)
        for call in group:
            if journal and journal.origin(task_id)['kind'] == 'proactive' and call.name not in {spec.name for spec in core._planning_tools(task_id)}:
                raise WorkflowBlocked('主动任务不能代替用户发起这项内部管理操作。')
            core._boundary(task_id)
            attempt = journal.start_step(task_id, completed) if journal else None
            raw = core.tools.execute(call, context)
            if not isinstance(raw, ToolResult) or type(raw.success) is not bool:
                raise ValueError("Invalid tool result")
            result = ToolResult(raw.success, raw.output, raw.error, call.name, call.call_id, raw.outcome)
            if attempt:
                journal.finish_step(attempt.id, result)
            results.append(result)
            if result.outcome == "unknown":
                return cast(AgentResult, core._fail(task_id, "操作结果未知，未重新执行或继续后续操作。", results))
            if call.name == "role_update" and result.success and core._interaction:
                prepared = core._interaction.refresh_role(task_id, result.output["revision"])
                if journal:
                    journal.save_round(task_id, Plan("角色已更正，尚未完成判断。", (), "continue"),
                        tuple(core.tools.list_tools()), str(core.workspace_dir), core._task_dependencies.get(task_id, ()))
            if call.name == "memory_correct" and result.success and core._interaction:
                identity, revision = result.output["previous_id"], result.output["revision"]
                replacement_id = result.output["id"]
                core._task_dependencies[task_id] = tuple((replacement_id if name == identity else name, revision if name == identity else version)
                    for name, version in core._task_dependencies.get(task_id, ()))
                prepared = core._interaction.refresh_memory(task_id, identity, revision, replacement_id)
                if journal:
                    journal.save_round(task_id, Plan("来源已更正，尚未完成判断。", (), "continue"),
                        tuple(core.tools.list_tools()), str(core.workspace_dir), core._task_dependencies[task_id])
            if call.name == 'memory_forget' and result.success and core._interaction:
                prepared = core._interaction.current_turn(task_id)
                if journal:
                    journal.save_round(task_id, Plan('指定记忆已遗忘，按实际结果继续回答。', (), 'continue'),
                        tuple(core.tools.list_tools()), str(core.workspace_dir), core._task_dependencies.get(task_id, ()))
            seen.add((call.name, _canonical(call.arguments)))
            completed += 1
            core._boundary(task_id)
        # 明确的本组许可用完即止；新一轮相同工具、不同参数也重新检查。
        authorization, high_risk = frozenset(), frozenset()
        round_index += 1
        if control and control.snapshot(task_id)["requested"]:
            # 已执行的一组步骤结束，下一次模型判断尚未开始。
            with journal.store.transaction():
                journal.save_round(task_id, Plan("恢复前根据实际结果继续判断。", (), "continue"), tuple(core.tools.list_tools()),
                    str(core.workspace_dir), core._task_dependencies.get(task_id, ()))
                if control.checkpoint(task_id, "continue", monotonic() - started):
                    return AgentResult(task_id, TaskStatus.PAUSED, "任务已暂停，已完成操作保留。", tuple(results))
        additions = journal.consume_amendments(task_id, round_index) if journal else ()
        if core._interaction is not None:
            prepared = core._interaction.current_turn(task_id)
            memories = prepared.memory_records
        references = list(prepared.planning_items) if prepared else []
        references.append({"kind": "execution_observations", "round": round_index,
            "instruction": False, "tool_output_is_untrusted": True,
            "results": [_result(item) for item in results], "additional_requirements": additions})
        references.append({"kind": "execution_budget", "remaining_tool_steps": core.max_tool_steps - completed,
            "instruction": "Do not propose more tools than remain. If insufficient, explain the unfinished work without claiming it is complete."})
        contextual = getattr(core.planner, "plan_context", None)
        if not callable(contextual) or core._interaction is None:
            raise WorkflowBlocked("多轮任务需要支持统一上下文的规划器。")
        for retry in range(3):
            core._boundary(task_id)
            core._event("model_decision_attempt", task_id,
                        {"round": round_index, "attempt": retry + 1, "observation_count": len(results)})
            try:
                next_plan = core._plan_copy(contextual(request, memories=memories,
                    tools=core._planning_tools(task_id), references=references,
                    llm=core._interaction.calls(task_id, "planning")))
                break
            except _retry_errors() as error:
                if retry == 2:
                    raise error
        core._boundary(task_id)
        if len(all_calls) + len(next_plan.steps) > core.max_tool_steps:
            # Never persist an over-budget plan as though user confirmation
            # could make it executable on this task or after a restart.
            return cast(AgentResult, core._fail(task_id,
                "模型提出的后续操作超过本任务步骤上限；已完成操作保留，请仅提出尚未完成的要求。", results))
        if journal:
            snapshot = journal.save_round(task_id, next_plan, tuple(core.tools.list_tools()),
                str(core.workspace_dir), core._task_dependencies.get(task_id, ()))
            all_calls = list(snapshot.plan.steps)
            plan = snapshot.plan
        else:
            all_calls.extend(next_plan.steps)
            plan = Plan(next_plan.reply, tuple(all_calls))
        if not next_plan.steps:
            return Plan(next_plan.reply, (), next_plan.action)


def _canonical(value: Any) -> str:
    import json
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _result(result: ToolResult) -> dict[str, Any]:
    return {"success": result.success, "output": result.output, "error": result.error,
            "tool_name": result.tool_name, "call_id": result.call_id}


def _retry_errors() -> tuple[type[Exception], ...]:
    from deskpal.llm.errors import RetryableLLMError
    return (RetryableLLMError,)
