"""Trusted review projection and exact permission sets over persisted plans."""

from dataclasses import asdict
import json
from typing import TYPE_CHECKING, Any

from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import PermissionLevel
from deskpal.workflow.models import PlanSnapshot, WorkflowBlocked

from .tool_catalog import argument_fields, display_name

if TYPE_CHECKING:
    from .service import ApplicationService


def required_permissions(snapshot: PlanSnapshot, completed: int = 0) -> tuple[frozenset[str], frozenset[str]]:
    specs = {spec.name: spec for spec in snapshot.tool_specs}
    ordinary: set[str] = set()
    high: set[str] = set()
    for call in snapshot.plan.steps[completed:]:
        spec = specs.get(call.name)
        if spec is None:
            raise WorkflowBlocked("Persisted plan has an unknown tool contract")
        if spec.permission_level == PermissionLevel.CONFIRM:
            ordinary.add(call.name)
        elif spec.permission_level == PermissionLevel.HIGH_RISK:
            high.add(call.name)
    return frozenset(ordinary), frozenset(high)


def permissions(ordinary: frozenset[str], high: frozenset[str]) -> dict[str, list[str]]:
    return {"ordinary_tools": sorted(ordinary), "high_risk_tools": sorted(high)}


class TaskReviewService:
    def __init__(self, app: "ApplicationService") -> None:
        self._app = app

    def _target_summary(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        source = {"timer_cancel": "timer", "calendar_update": "calendar", "calendar_delete": "calendar"}.get(tool_name)
        identity = arguments.get("id")
        if source is None or not isinstance(identity, str) or not identity:
            return None
        unavailable = {"label": "当前对象名称", "source": source, "source_id": identity,
            "covered_by_plan_hash": False, "unavailable": True}
        try:
            snapshot = self._app.scheduling.source_snapshot(source, identity)
            if snapshot is None:
                return unavailable
            payload = json.loads(snapshot.payload_json)
            return {**unavailable, "unavailable": False, "name": payload.get("label" if source == "timer" else "title", ""),
                "due_at": payload.get("due_at" if source == "timer" else "reminder_at"),
                "start_at": payload.get("start_at"), "note": "当前来源信息，不属于原计划哈希冻结参数。"}
        except (RuntimeError, ValueError, OSError):
            return unavailable

    def review(self, task_id: str, *, _retry: bool = True) -> dict[str, Any]:
        if not isinstance(task_id, str) or not task_id or len(task_id) > 200:
            raise ValueError("Invalid task identity")
        app = self._app
        with app._dispatcher.snapshot_control():
            state = app._dispatcher.confirmation_state(task_id)
            grants = app.core.pending_grants(task_id)
            raw = app.journal.review(task_id)
        task, snapshot, attempts = raw["task"], raw["plan"], raw["attempts"]
        with app.store.read_connection() as connection:
            # Historical viewing uses the current privacy barrier, not the old
            # execution admission epoch saved when this task was submitted.
            app.store.check_privacy(connection)
            view_epoch = connection.execute("SELECT epoch FROM privacy_control WHERE id=1").fetchone()[0]
            turn = connection.execute("SELECT r.conversation_id FROM turn_ref r JOIN conversation c ON c.id=r.conversation_id WHERE r.task_id=? AND c.deleted_at IS NULL", (task_id,)).fetchone()
            source = connection.execute("SELECT use_policy,scrubbed FROM task WHERE id=?", (task_id,)).fetchone()
        ordinary: frozenset[str] = frozenset()
        high: frozenset[str] = frozenset()
        steps: list[dict[str, Any]] = []
        blocked: str | None = None
        pause_control = app.task_control.snapshot(task_id)
        execution_candidate = task.status in {TaskStatus.WAITING, TaskStatus.NEEDS_REVIEW}
        if snapshot is not None:
            try:
                ordinary, high = required_permissions(snapshot, len(attempts))
            except WorkflowBlocked:
                blocked = "持久计划缺少可信工具契约，请重新提出意图。"
            specs = {spec.name: spec for spec in snapshot.tool_specs}
            for index, call in enumerate(snapshot.plan.steps):
                if index < len(attempts):
                    continue
                spec = specs.get(call.name)
                steps.append({"call_id": call.call_id, "tool_name": call.name, "display_name": display_name(call.name),
                    "index": index, "permission_level": None if spec is None else int(spec.permission_level),
                    "permission_label": "契约缺失" if spec is None else {0: "自动执行", 1: "需要普通授权", 2: "需要高风险授权"}[int(spec.permission_level)],
                    "contract_version": None if spec is None else spec.contract_version,
                    "arguments": dict(call.arguments), "fields": argument_fields(call.arguments),
                    "target_summary": self._target_summary(call.name, dict(call.arguments))})
            if execution_candidate:
                try:
                    app._verify_snapshot(snapshot)
                    app.store.validate_external(task_id)
                    with app.store.read_connection() as connection:
                        app.store.check_privacy(connection, task_id)
                        app.store.check_dependencies(connection, task_id)
                except WorkflowBlocked:
                    blocked = "任务来源、工作区或工具契约已变化，请重新提出意图。"
        else:
            blocked = "没有完整的持久计划。" if execution_candidate else None
        if snapshot is not None and len(snapshot.plan.steps) > app.core.max_tool_steps and execution_candidate:
            blocked = (f"本计划共有 {len(snapshot.plan.steps)} 步，超过本任务 {app.core.max_tool_steps} 步上限。"
                       f"已完成 {len(attempts)} 步且不会重做；请取消此请求，并仅提出尚未完成的要求。")
        if source is not None and source["scrubbed"]:
            blocked = "任务内容已清理。"
            steps = []
            ordinary, high = frozenset(), frozenset()
            snapshot = None
        if any(item.status not in {"Succeeded", "Failed"} for item in attempts) and execution_candidate:
            blocked = "该计划已有执行记录，不能整份重放；请仅创建尚未完成的新意图。"
        if task.cancel_requested or task.status == TaskStatus.CANCELLED:
            blocked = "任务已取消或正在取消。"
        can_resume = False
        if snapshot is None and task.status == TaskStatus.NEEDS_REVIEW and not attempts and not task.cancel_requested and source is not None and not source["scrubbed"]:
            try:
                app.execution_guard(task_id)
                app.store.validate_external(task_id)
                with app.store.read_connection() as connection:
                    app.store.check_privacy(connection, task_id)
                    app.store.check_dependencies(connection, task_id)
            except WorkflowBlocked:
                blocked = "任务来源或执行条件已变化，请重新提出意图。"
            else:
                can_resume = True
                blocked = None
        with app._dispatcher.snapshot_control():
            with app.store.read_connection() as connection:
                app.store.check_privacy(connection)
                privacy_changed = connection.execute("SELECT epoch FROM privacy_control WHERE id=1").fetchone()[0] != view_epoch
            current = app.tasks.get(task_id)
            current_state = app._dispatcher.confirmation_state(task_id)
            changed = privacy_changed or current is None or current.revision != task.revision or current_state != state
        if changed:
            if _retry:
                return self.review(task_id, _retry=False)
            raise WorkflowBlocked("Task review changed while reading; refresh the selected task")
        granted = (grants[0] | frozenset(state["ordinary_tools"]), grants[1] | frozenset(state["high_risk_tools"]))
        actions: list[str] = []
        if task.status in {TaskStatus.PAUSED, TaskStatus.CREATED, TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.WAITING, TaskStatus.AWAITING_INPUT, TaskStatus.NEEDS_REVIEW} and not task.cancel_requested:
            actions.append("cancel")
        if not task.cancel_requested:
            if task.status == TaskStatus.PAUSED and app._dispatcher.diagnostics()["active_task_id"] != task_id:
                actions.append("task_continue")
            elif task.status in {TaskStatus.CREATED, TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.WAITING, TaskStatus.AWAITING_INPUT} and not pause_control["requested"]:
                actions.append("task_pause")
        if task.status == TaskStatus.UNCERTAIN:
            actions.append("review_uncertain")
        if can_resume:
            actions.append("resume")
        if snapshot is not None and blocked is None and task.status in {TaskStatus.WAITING, TaskStatus.NEEDS_REVIEW} and state["state"] != "accepted":
            actions.append("confirm")
            if ordinary or high:
                actions = [action for action in actions if action != "cancel"]
                actions.append("reject_tools")
        rejections = app.journal.rejected_authorizations(task_id) if not (source and source["scrubbed"]) else ()
        rejected_operations = []
        for decision in rejections:
            for operation in decision.get("operations", []):
                name = operation["tool_name"]
                arguments = operation["arguments"]
                target = next((arguments[key] for key in ("path", "destination", "id", "memory_id", "task_id")
                               if isinstance(arguments.get(key), str)), "")
                short_target = target if len(target) <= 90 else target[:35] + "…" + target[-45:]
                summary = display_name(name) + ("：" + short_target if target else "（此前提议）")
                rejected_operations.append({"tool_name": name, "display_name": display_name(name), "summary": summary})
        cleared = source is not None and bool(source["scrubbed"])
        completed_steps = []
        if not cleared:
            for attempt in attempts:
                if attempt.status != "Succeeded" or attempt.result is None:
                    continue
                result = attempt.result
                unchanged = isinstance(result.output, dict) and result.output.get("status") == "unchanged"
                completed_steps.append({"index": attempt.index, "tool_name": result.tool_name,
                    "display_name": display_name(result.tool_name), "status": "unchanged" if unchanged else "completed"})
        cancellation_notice = "取消只停止后续步骤，不会撤销已经完成的操作。"
        if task.status == TaskStatus.CANCELLED:
            cancellation_notice = ("后续步骤已取消；此前已完成的操作仍然保留。" if completed_steps
                                   else "请求已取消，没有已完成的工具操作。")
        elif task.cancel_requested:
            cancellation_notice = "正在停止后续步骤；已经开始的操作可能仍在完成，不会自动撤销已完成的操作。"
        task_value = asdict(task)
        if cleared:
            task_value.update(request="", result=None, error=None)
        return {"schema_version": 1, "task": task_value, "task_id": task.id, "task_revision": task.revision,
            "status": task.status.value, "pause_control": pause_control, "title": "已清理任务" if cleared else task.request[:120], "conversation_id": None if turn is None else turn[0],
            "plan_hash": None if snapshot is None else snapshot.hash, "workspace": None if snapshot is None else snapshot.workspace_dir,
            "steps": steps, "required_permissions": permissions(ordinary, high),
            "granted_in_current_process": permissions(granted[0] & ordinary, granted[1] & high),
            "missing_permissions": permissions(ordinary - granted[0], high - granted[1]),
            "available_actions": actions, "blocked_reason": blocked, "confirmation_state": state["state"],
            "execution_progress": {"completed": completed_steps, "remaining_count": len(steps)},
            "rejected_operations": rejected_operations,
            "continuing_after_rejection": bool(rejections) and snapshot is not None and snapshot.plan.action == "continue",
            "cancellation_notice": cancellation_notice,
            "confirmation": state, "confirmation_revision": state["revision"], "attempt_summary": {"count": len(attempts),
                "items": [{"index": attempt.index, "status": attempt.status} for attempt in attempts]},
            "source_use_summary": {"use_policy": None if source is None else source["use_policy"],
                "dependency_count": len(app.store.dependencies(task_id))}}

    def updates(self, task_ids: list[str], known_revisions: dict[str, int] | None = None) -> dict[str, Any]:
        if not isinstance(task_ids, list) or len(task_ids) > 100 or any(not isinstance(value, str) or not value or len(value) > 200 for value in task_ids) or len(set(task_ids)) != len(task_ids):
            raise ValueError("task_ids must be at most 100 distinct task identities")
        known = known_revisions or {}
        if not isinstance(known, dict) or any(key not in task_ids or type(value) is not int or value < 1 for key, value in known.items()):
            raise ValueError("Invalid known task revisions")
        app = self._app
        items: list[dict[str, Any]] = []
        missing: list[str] = []
        for identity in task_ids:
            with app._dispatcher.snapshot_control():
                task = app.tasks.get(identity)
                state = app._dispatcher.confirmation_state(identity)
            if task is None:
                missing.append(identity)
                continue
            if known.get(identity) == task.revision and state["state"] != "accepted":
                continue
            # Incremental polling carries bounded metadata; selected-card detail
            # uses task_review and preserves the user's unchanged grant choices.
            items.append({"task_id": identity, "task_revision": task.revision, "status": task.status.value,
                "pause_control": app.task_control.snapshot(identity), "cancel_requested": task.cancel_requested, "confirmation_state": state["state"], "confirmation": state,
                "confirmation_revision": state["revision"]})
        return {"schema_version": 1, "items": items, "missing_ids": missing}
