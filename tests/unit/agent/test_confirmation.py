"""V0.2 confirms an isolated pending plan with task-scoped risk grants."""

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from deskpal.agent import AgentCore
from deskpal.llm.models import LLMResponse
from deskpal.planner import Plan
from deskpal.sandbox import WorkspaceSandbox
from deskpal.tasks import InMemoryTaskManager, TaskStatus
from deskpal.tools import BuiltinToolManager, PermissionLevel, ToolCall, ToolResult, ToolSpec


class Audit:
    def __init__(self):
        self.records = []
        self.failure = None

    def record(self, event, task_id=None, data=None):
        if self.failure and self.failure(event, data or {}):
            raise OSError("confirmation audit unavailable")
        self.records.append((event, task_id, copy.deepcopy(data)))


class Planner:
    def __init__(self, plan):
        self.value = plan
        self.calls = []
        self.hook = None

    def plan(self, request, *, memories=(), tools=()):
        self.calls.append(request)
        if self.hook:
            self.hook()
        return self.value


class Memory:
    def __init__(self):
        self.searches = []
        self.saved = []

    def search(self, query, *, limit=5, kind=None):
        self.searches.append(query)
        return ()

    def save(self, record):
        self.saved.append(record)
        return replace(record, id="test-memory")


class Reflection:
    def __init__(self):
        self.calls = []

    def chat(self, messages, *, tools=()):
        self.calls.append((tuple(messages), tuple(tools)))
        return LLMResponse(content="Confirmed result")


class RecordingTool:
    def __init__(self, name, permission, executed):
        self.spec = ToolSpec(name, "Trusted test tool", {"type": "object", "properties": {
            "target": {"type": "string"}, "nested": {"type": "object"}}, "required": ["target"]}, permission)
        self.executed = executed
        self.fail = False

    def execute(self, arguments, context):
        self.executed.append((self.spec.name, copy.deepcopy(arguments), context))
        return ToolResult(not self.fail, output=copy.deepcopy(arguments), error="tool failed" if self.fail else None)


def call(name, marker=None):
    return ToolCall(name, {"target": marker or name}, f"{name}-call")


@pytest.fixture
def setup_confirmation(tmp_path):
    executed, tools_by_name = [], {}
    audit = Audit()
    manager = BuiltinToolManager(WorkspaceSandbox(tmp_path), execution_log=audit)
    for name, permission in (("auto", PermissionLevel.AUTOMATIC), ("first", PermissionLevel.CONFIRM),
                             ("second", PermissionLevel.CONFIRM), ("high", PermissionLevel.HIGH_RISK)):
        tool = RecordingTool(name, permission, executed)
        tools_by_name[name] = tool
        manager.register(tool)
    plan = Plan(steps=(call("auto"), call("first"), call("second"), call("high")))
    planner, memory, llm = Planner(plan), Memory(), Reflection()
    core = AgentCore(llm=llm, planner=planner, tasks=InMemoryTaskManager(), memory=memory,
                     tools=manager, workspace_dir=tmp_path, execution_log=audit)
    yield core, planner, memory, llm, audit, executed, tools_by_name
    core.close()


def test_partial_confirmation_accumulates_grants_without_replan_or_partial_execution(setup_confirmation):
    core, planner, memory, llm, _, executed, _ = setup_confirmation
    first = core.run("original request")
    assert first.status == TaskStatus.WAITING
    searches = list(memory.searches)
    second = core.confirm_task(first.task_id, authorized_tools=("first",))
    assert second.status == TaskStatus.WAITING
    assert second.required_authorizations == ("second", "high")
    assert second.required_high_risk_authorizations == ("high",)
    third = core.confirm_task(first.task_id, authorized_tools=("second", "high"))
    assert third.status == TaskStatus.WAITING
    assert third.required_authorizations == ("high",)
    assert not executed and not llm.calls
    final = core.confirm_task(first.task_id, high_risk_authorized_tools=("high",))
    assert final.task_id == first.task_id
    assert final.status == TaskStatus.COMPLETED
    assert [name for name, _, _ in executed] == ["auto", "first", "second", "high"]
    assert planner.calls == ["original request"]
    assert memory.searches == searches
    assert len(core.tasks.list_tasks()) == 1
    assert len(llm.calls) == 1
    assert not llm.calls[0][1]
    assert all(message.role != "tool" and not message.tool_calls for message in llm.calls[0][0])
    with pytest.raises(ValueError):
        core.confirm_task(first.task_id, high_risk_authorized_tools=("high",))
    assert len(executed) == 4


def test_result_preview_task_and_source_plan_mutation_cannot_change_private_arguments(setup_confirmation):
    core, planner, _, _, _, executed, _ = setup_confirmation
    planner.value = Plan(steps=(ToolCall("first", {"target": "original", "nested": {"items": ["original"]}}, "one"),))
    waiting = core.run("preview")
    waiting.authorization_requests[0].arguments["target"] = "authorization-preview-change"
    waiting.planned_calls[0].arguments["nested"]["items"].append("plan-preview-change")
    planner.value.steps[0].arguments["target"] = "planner-source-change"
    task = core.tasks.get(waiting.task_id)
    task.result["required_authorizations"].clear()
    task.result["planned_calls"] = [{"name": "auto", "arguments": {"target": "task-change"}}]
    completed = core.confirm_task(waiting.task_id, authorized_tools=("first",))
    assert completed.status == TaskStatus.COMPLETED
    assert executed[0][1] == {"target": "original", "nested": {"items": ["original"]}}


def test_same_tool_multiple_targets_are_visible_and_remain_original(setup_confirmation):
    core, planner, _, _, _, executed, _ = setup_confirmation
    planner.value = Plan(steps=(ToolCall("first", {"target": "one"}, "one"),
                                ToolCall("first", {"target": "two"}, "two")))
    waiting = core.run("two targets")
    assert waiting.required_authorizations == ("first",)
    assert [item.arguments["target"] for item in waiting.authorization_requests] == ["one", "two"]
    assert len(waiting.planned_calls) == 2
    assert core.confirm_task(waiting.task_id, authorized_tools=("first",)).status == TaskStatus.COMPLETED
    assert [arguments["target"] for _, arguments, _ in executed] == ["one", "two"]


def test_high_risk_grants_do_not_license_level_one_or_other_task(setup_confirmation):
    core, planner, _, _, _, executed, _ = setup_confirmation
    planner.value = Plan(steps=(call("first"), call("high")))
    waiting = core.run("one", high_risk_authorized_tools=("first", "high"))
    assert waiting.required_authorizations == ("first",)
    assert not executed
    assert core.confirm_task(waiting.task_id, authorized_tools=("first",)).status == TaskStatus.COMPLETED
    another = core.run("two")
    assert another.status == TaskStatus.WAITING
    assert another.required_authorizations == ("first", "high")
    assert another.required_high_risk_authorizations == ("high",)
    assert len(executed) == 2


@pytest.mark.parametrize("condition", ["cancelled", "closed", "failed", "unknown", "no-cache"])
def test_confirmation_rejects_invalid_lifecycle_states_without_execution(setup_confirmation, condition):
    core, _, _, _, _, executed, _ = setup_confirmation
    waiting = core.run("waiting")
    task_id = waiting.task_id
    expected = ValueError
    if condition == "cancelled":
        core.cancel_task(task_id)
    elif condition == "closed":
        core.close()
        expected = RuntimeError
    elif condition == "failed":
        core.tasks.update_status(task_id, TaskStatus.FAILED)
    elif condition == "unknown":
        task_id, expected = "not-a-task", KeyError
    else:
        task = core.tasks.create("orphan")
        core.tasks.update_status(task.id, TaskStatus.PLANNING)
        core.tasks.update_status(task.id, TaskStatus.WAITING)
        task_id = task.id
    with pytest.raises(expected):
        core.confirm_task(task_id, authorized_tools=("first", "second"), high_risk_authorized_tools=("high",))
    assert not executed


@pytest.mark.parametrize("failed_event", ["plan_confirmation_received", "task_status_changed"])
def test_confirmation_audit_failure_is_failed_and_has_no_tool_side_effect(setup_confirmation, failed_event):
    core, _, _, _, audit, executed, _ = setup_confirmation
    waiting = core.run("waiting")
    audit.failure = lambda event, data: event == failed_event and (
        event != "task_status_changed" or data.get("status") == "Running")
    result = core.confirm_task(waiting.task_id, authorized_tools=("first", "second"),
                               high_risk_authorized_tools=("high",))
    assert result.status == TaskStatus.FAILED
    assert result.error
    assert core.tasks.get(waiting.task_id).status == TaskStatus.FAILED
    assert not executed
    with pytest.raises(ValueError):
        core.confirm_task(waiting.task_id)


def test_failed_confirmation_is_not_reexecuted_and_stops_later_steps(setup_confirmation):
    core, planner, _, _, _, executed, tools = setup_confirmation
    planner.value = Plan(steps=(call("first"), call("auto")))
    waiting = core.run("fail")
    tools["first"].fail = True
    failed = core.confirm_task(waiting.task_id, authorized_tools=("first",))
    assert failed.status == TaskStatus.FAILED
    assert [name for name, _, _ in executed] == ["first"]
    with pytest.raises(ValueError):
        core.confirm_task(waiting.task_id, authorized_tools=("first",))
    assert len(executed) == 1


def test_confirmation_rejects_concurrent_run_and_does_not_lose_pending_plan(setup_confirmation):
    core, planner, _, _, _, executed, _ = setup_confirmation
    waiting = core.run("pending")
    planner.value = Plan(reply="other request")
    started, release = Event(), Event()

    def block():
        started.set()
        assert release.wait(5)

    planner.hook = block
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(core.run, "other")
        try:
            assert started.wait(5)
            with pytest.raises(RuntimeError):
                core.confirm_task(waiting.task_id)
        finally:
            release.set()
        assert running.result(timeout=5).status == TaskStatus.COMPLETED
    assert not executed
    assert core.confirm_task(waiting.task_id, authorized_tools=("first", "second"),
                             high_risk_authorized_tools=("high",)).status == TaskStatus.COMPLETED


def test_cancel_hook_after_waiting_storage_returns_cancelled_not_waiting(setup_confirmation, monkeypatch):
    core, _, _, _, _, executed, _ = setup_confirmation
    original = core.tasks.update_status

    def cancel_after_waiting(task_id, status, **kwargs):
        changed = original(task_id, status, **kwargs)
        if status == TaskStatus.WAITING:
            core.cancel_task(task_id)
        return changed

    monkeypatch.setattr(core.tasks, "update_status", cancel_after_waiting)
    result = core.run("cancel boundary")
    assert result.status == TaskStatus.CANCELLED
    assert core.tasks.get(result.task_id).status == TaskStatus.CANCELLED
    with pytest.raises(ValueError):
        core.confirm_task(result.task_id, authorized_tools=("first", "second"),
                          high_risk_authorized_tools=("high",))
    assert not executed
