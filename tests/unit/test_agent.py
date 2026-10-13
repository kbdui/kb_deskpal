"""Core public behavior using controlled collaborators and real safe tools."""

import copy
from dataclasses import replace
from threading import Event
from concurrent.futures import ThreadPoolExecutor

import pytest

from deskpal.agent import AgentCore
from deskpal.llm.errors import LLMError
from deskpal.llm.models import LLMResponse
from deskpal.memory import MemoryKind, MemoryRecord
from deskpal.planner import Plan
from deskpal.sandbox import WorkspaceSandbox
from deskpal.tasks import InMemoryTaskManager, TaskStatus
from deskpal.tools import BuiltinToolManager, ToolCall, ToolResult, ToolSpec


class CaptureLog:
    def __init__(self, fail_when=None):
        self.records = []
        self.fail_when = fail_when

    def record(self, event, task_id=None, data=None):
        if self.fail_when is not None and self.fail_when(event, data or {}):
            raise OSError("secondary audit unavailable")
        self.records.append((event, task_id, copy.deepcopy(data)))


class FakeMemory:
    def __init__(self, *, related=(), search_error=None, save_error=None):
        self.related = related
        self.search_error = search_error
        self.save_error = save_error
        self.saved = []
        self.queries = []

    def search(self, query, *, limit=5, kind=None):
        self.queries.append(query)
        if self.search_error:
            raise self.search_error
        return self.related

    def save(self, record):
        if self.save_error:
            raise self.save_error
        saved = replace(record, id=record.id or f"memory-{len(self.saved) + 1}")
        self.saved.append(saved)
        return saved

    def delete(self, memory_id):
        return False


class FixedPlanner:
    def __init__(self, plan, *, error=None, hook=None):
        self.plan_value = plan
        self.error = error
        self.hook = hook
        self.calls = []

    def plan(self, request, *, memories=(), tools=()):
        self.calls.append((request, tuple(memories), tuple(tools)))
        if self.hook:
            self.hook()
        if self.error:
            raise self.error
        return self.plan_value


class FakeLLM:
    def __init__(self, response=None, *, error=None):
        self.response = response or LLMResponse(content="根据实际结果完成反思")
        self.error = error
        self.calls = []

    def chat(self, messages, *, tools=()):
        self.calls.append((tuple(messages), tuple(tools)))
        if self.error:
            raise self.error
        return self.response

    def generate(self, prompt):
        raise AssertionError("Core reflection must use explicit chat messages")


class CallbackTool:
    spec = ToolSpec("callback", "trusted test callback", {"type": "object", "additionalProperties": False})

    def __init__(self, callback):
        self.callback = callback
        self.calls = []

    def execute(self, arguments, context):
        self.calls.append(context)
        self.callback(context)
        return ToolResult(True, "callback-result")


@pytest.fixture
def make_agent(tmp_path):
    count = 0

    def make(plan=None, *, llm=None, memory=None, logger=None, max_tool_steps=8, planner_error=None, on_close=None):
        nonlocal count
        count += 1
        root = tmp_path / f"workspace-{count}"
        root.mkdir()
        log = logger if logger is not None else CaptureLog()
        tools = BuiltinToolManager(WorkspaceSandbox(root), execution_log=log)
        tools.register_builtin()
        tasks = InMemoryTaskManager()
        planner = FixedPlanner(plan or Plan(reply="普通回复"), error=planner_error)
        llm = llm if llm is not None else FakeLLM()
        memory = memory if memory is not None else FakeMemory()
        core = AgentCore(llm=llm, planner=planner, tasks=tasks, memory=memory, tools=tools,
                         workspace_dir=root, max_tool_steps=max_tool_steps, execution_log=log, on_close=on_close)
        return core, planner, llm, memory, log, root

    return make


def echo(text="中文结果", call_id="echo-1"):
    return ToolCall("echo", {"text": text}, call_id)


@pytest.mark.parametrize("method", ["run", "handle_request"])
def test_plain_response_completes_without_tools_or_saving_all_chat(make_agent, method):
    core, planner, llm, memory, _, _ = make_agent()
    result = getattr(core, method)("你好")
    assert result.status == TaskStatus.COMPLETED
    assert result.response == "普通回复"
    assert result.tool_results == ()
    assert core.tasks.get(result.task_id).status == TaskStatus.COMPLETED
    assert planner.calls[0][0] == "你好"
    assert llm.calls == []
    assert memory.saved == []


@pytest.mark.parametrize("request_text", ["", "  ", "\n\t"])
def test_blank_input_rejected_before_task_creation(make_agent, request_text):
    core, planner, _, _, _, _ = make_agent()
    with pytest.raises(ValueError):
        core.run(request_text)
    assert not core.tasks.list_tasks()
    assert not planner.calls


def test_related_memory_and_tool_metadata_are_passed_to_planner(make_agent):
    memory = FakeMemory(related=(MemoryRecord("用户喜欢中文", kind=MemoryKind.SEMANTIC),))
    core, planner, _, _, _, _ = make_agent(memory=memory)
    assert core.run("偏好是什么").status == TaskStatus.COMPLETED
    assert planner.calls[0][1] == memory.related
    assert {spec.name for spec in planner.calls[0][2]} == {"echo", "time", "read_text", "write_text"}


def test_tool_plan_runs_in_order_and_reflects_independent_plain_text_request(make_agent):
    core, _, llm, memory, logger, _ = make_agent(Plan(steps=(echo("first", "one"), echo("second", "two"))))
    result = core.run("执行两个回显")
    assert result.status == TaskStatus.COMPLETED
    assert [tool.output for tool in result.tool_results] == ["first", "second"]
    assert [tool.call_id for tool in result.tool_results] == ["one", "two"]
    assert result.response == "根据实际结果完成反思"
    assert len(llm.calls) == 1
    messages, tools = llm.calls[0]
    assert tools == ()
    assert any(message.role == "system" and "[REFLECT]" in message.content for message in messages)
    assert any(message.role == "user" and "first" in message.content and "second" in message.content
               and "执行两个回显" in message.content for message in messages)
    assert all(message.role != "tool" and not message.tool_calls and message.tool_call_id is None for message in messages)
    assert memory.saved
    assert all(record.task_id == result.task_id for record in memory.saved)
    events = [event for event, task_id, _ in logger.records if task_id == result.task_id]
    assert "request_received" in events and "request_completed" in events
    states = [data["status"] for event, task_id, data in logger.records
              if task_id == result.task_id and event == "task_status_changed"]
    assert states == ["Created", "Planning", "Running", "Completed"]


def test_reflection_does_not_execute_new_tool_calls(make_agent):
    llm = FakeLLM(LLMResponse(content="总结", tool_calls=(echo("must-not-run", "new-call"),)))
    core, _, _, _, logger, _ = make_agent(Plan(steps=(echo(),)), llm=llm)
    result = core.run("echo")
    assert result.status == TaskStatus.FAILED
    assert result.error
    assert len(result.tool_results) == 1
    assert [data["call_id"] for event, _, data in logger.records if event == "tool_started"] == ["echo-1"]


def test_full_plan_permission_preflight_waits_without_partial_execution(make_agent):
    write = ToolCall("write_text", {"path": "nested/note.txt", "text": "内容"}, "write-1")
    second_write = ToolCall("write_text", {"path": "other.txt", "text": "内容"}, "write-2")
    core, _, llm, memory, logger, root = make_agent(Plan(steps=(echo(), write, second_write)))
    result = core.run("包含未授权写入")
    assert result.status == TaskStatus.WAITING
    assert result.required_authorizations == ("write_text",)
    assert result.tool_results == ()
    assert not list(root.iterdir())
    assert not any(event == "tool_started" for event, _, _ in logger.records)
    assert not llm.calls and not memory.saved
    assert core.tasks.get(result.task_id).status == TaskStatus.WAITING


def test_authorization_only_applies_to_new_request_and_creates_new_task(make_agent):
    write = ToolCall("write_text", {"path": "note.txt", "text": "内容"}, "write-1")
    core, _, _, _, _, root = make_agent(Plan(steps=(write,)))
    first = core.run("写入")
    second = core.run("写入", authorized_tools=("write_text",))
    third = core.run("写入")
    assert (first.status, second.status, third.status) == (TaskStatus.WAITING, TaskStatus.COMPLETED, TaskStatus.WAITING)
    assert len({first.task_id, second.task_id, third.task_id}) == 3
    assert (root / "note.txt").read_text(encoding="utf-8") == "内容"


@pytest.mark.parametrize("extra", [ToolCall("missing", {}, "unknown"),
                                   ToolCall("write_text", {"path": "note.txt", "text": "x"}, "write")])
def test_unknown_tool_fails_entire_plan_even_when_permission_missing(make_agent, extra):
    core, _, _, _, logger, root = make_agent(Plan(steps=(echo(), extra, ToolCall("missing", {}, "unknown-2"))))
    result = core.run("invalid plan")
    assert result.status == TaskStatus.FAILED
    assert result.error
    assert result.tool_results == ()
    assert not list(root.iterdir())
    assert not any(event == "tool_started" for event, _, _ in logger.records)


def test_oversized_plan_fails_before_execution(make_agent):
    core, _, llm, _, logger, _ = make_agent(Plan(steps=(echo(), echo(call_id="two"))), max_tool_steps=1)
    result = core.run("over budget")
    assert result.status == TaskStatus.FAILED
    assert result.error
    assert not result.tool_results and not llm.calls
    assert not any(event == "tool_started" for event, _, _ in logger.records)


def test_failed_tool_stops_following_steps_and_keeps_prior_results(make_agent):
    invalid = ToolCall("echo", {}, "invalid")
    core, _, llm, memory, logger, _ = make_agent(Plan(steps=(echo("before", "first"), invalid, echo("after", "last"))))
    result = core.run("tool failure")
    assert result.status == TaskStatus.FAILED
    assert result.error
    assert len(result.tool_results) == 2
    assert result.tool_results[0].output == "before"
    assert result.tool_results[1].success is False
    assert not any(data.get("call_id") == "last" for event, _, data in logger.records if event == "tool_started")
    assert not llm.calls and not memory.saved


def test_path_error_is_failed_not_waiting_and_does_not_escape(make_agent):
    core, _, _, _, _, root = make_agent(Plan(steps=(ToolCall("write_text", {"path": "../escaped.txt", "text": "x"}),)))
    result = core.run("bad path", authorized_tools=("write_text",))
    assert result.status == TaskStatus.FAILED
    assert result.error
    assert not (root.parent / "escaped.txt").exists()


@pytest.mark.parametrize("phase", ["plan", "search", "reflect", "save"])
def test_dependency_failures_end_failed_and_keep_executed_tool_results(make_agent, phase):
    memory = FakeMemory(search_error=OSError("search unavailable") if phase == "search" else None,
                        save_error=OSError("save unavailable") if phase == "save" else None)
    llm = FakeLLM(error=LLMError("reflection unavailable") if phase == "reflect" else None)
    core, _, _, _, _, _ = make_agent(Plan(steps=(echo(),)), memory=memory, llm=llm,
                                      planner_error=LLMError("planning unavailable") if phase == "plan" else None)
    result = core.run("dependency failure")
    assert result.status == TaskStatus.FAILED
    assert result.error
    assert core.tasks.get(result.task_id).status == TaskStatus.FAILED
    assert len(result.tool_results) == (1 if phase in {"reflect", "save"} else 0)


def test_cancel_after_planning_prevents_execution_and_preserves_cancelled_state(make_agent):
    core, planner, llm, memory, logger, _ = make_agent(Plan(steps=(echo(),)))

    def cancel():
        task = core.tasks.list_tasks()[-1]
        core.tasks.update_status(task.id, TaskStatus.CANCELLED, result={"cancel-marker": True})

    planner.hook = cancel
    result = core.run("cancel")
    assert result.status == TaskStatus.CANCELLED
    assert core.tasks.get(result.task_id).result == {"cancel-marker": True}
    assert not result.tool_results and not llm.calls and not memory.saved
    assert not any(event == "tool_started" for event, _, _ in logger.records)


def test_cancel_during_tool_boundary_keeps_result_and_stops_later_tools(make_agent):
    core, _, llm, memory, _, _ = make_agent(Plan(steps=(ToolCall("callback", {}, "cancel-call"), echo())))
    tool = CallbackTool(lambda context: core.tasks.update_status(context.task_id, TaskStatus.CANCELLED,
                                                                 result={"cancel-marker": True}))
    core.tools.register(tool)
    result = core.run("cancel in tool")
    assert result.status == TaskStatus.CANCELLED
    assert len(tool.calls) == 1
    assert len(result.tool_results) == 1
    assert result.tool_results[0].output == "callback-result"
    assert core.tasks.get(result.task_id).result == {"cancel-marker": True}
    assert not llm.calls and not memory.saved


@pytest.mark.parametrize("failed_event", ["task_status_changed", "request_completed"])
def test_completed_audit_failure_never_commits_success(make_agent, failed_event):
    logger = CaptureLog(lambda event, data: event == failed_event and
                        (event != "task_status_changed" or data.get("status") == "Completed"))
    core, _, _, _, _, _ = make_agent(Plan(steps=(echo(),)), logger=logger)
    try:
        result = core.run("audit failure")
    except OSError:
        pass
    else:
        assert result.status == TaskStatus.FAILED
        assert result.error
    assert core.tasks.list_tasks()[-1].status != TaskStatus.COMPLETED


def test_authorization_is_snapshotted_before_planner_can_modify_caller_list(make_agent):
    write = ToolCall("write_text", {"path": "note.txt", "text": "x"}, "write-1")
    core, planner, _, _, _, root = make_agent(Plan(steps=(write,)))
    authorization = ["write_text"]
    planner.hook = authorization.clear
    result = core.run("write", authorized_tools=authorization)
    assert result.status == TaskStatus.COMPLETED
    assert (root / "note.txt").read_text(encoding="utf-8") == "x"


def test_secondary_audit_failure_does_not_replace_primary_error(make_agent):
    logger = CaptureLog(lambda event, data: event == "request_failed")
    core, _, _, _, _, _ = make_agent(planner_error=LLMError("primary planning failure"), logger=logger)
    try:
        result = core.run("two failures")
    except Exception as error:
        assert isinstance(error, LLMError)
        assert "primary planning failure" in str(error)
    else:
        assert result.status == TaskStatus.FAILED
        assert "primary planning failure" in result.error or "LLMError" in result.error
        assert "secondary audit unavailable" not in result.error


def test_close_context_and_callback_are_idempotent_and_closed_run_is_rejected(make_agent):
    closed = []
    core, _, _, _, _, _ = make_agent(on_close=lambda: closed.append(True))
    with core as entered:
        assert entered is core
        assert core.run("hello").status == TaskStatus.COMPLETED
    core.close()
    assert closed == [True]
    before = len(core.tasks.list_tasks())
    with pytest.raises(RuntimeError):
        core.run("closed")
    assert len(core.tasks.list_tasks()) == before


def test_concurrent_and_reentrant_requests_are_rejected_without_extra_task(make_agent):
    core, planner, _, _, _, _ = make_agent()
    started, release = Event(), Event()

    def wait_in_plan():
        with pytest.raises(RuntimeError):
            core.run("reentrant")
        started.set()
        assert release.wait(5)

    planner.hook = wait_in_plan
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(core.run, "outer request")
        try:
            assert started.wait(5)
            with pytest.raises(RuntimeError):
                core.run("concurrent")
            with pytest.raises(RuntimeError):
                core.close()
        finally:
            release.set()
        assert future.result(timeout=5).status == TaskStatus.COMPLETED
    assert len(core.tasks.list_tasks()) == 1


@pytest.mark.parametrize("request_text,content", [("记住：我喜欢中文", "我喜欢中文"),
                                                 ("记住: preferred editor is Vim", "preferred editor is Vim"),
                                                 ("记住 用户事实", "用户事实")])
def test_explicit_fact_is_saved_as_semantic_memory(make_agent, request_text, content):
    core, _, _, memory, _, _ = make_agent()
    result = core.run(request_text)
    assert result.status == TaskStatus.COMPLETED
    assert len(memory.saved) == 1
    assert memory.saved[0].kind == MemoryKind.SEMANTIC
    assert memory.saved[0].content == content
    assert memory.saved[0].task_id == result.task_id


@pytest.mark.parametrize("phase", ["plan", "reflect"])
def test_empty_plan_or_reflection_is_failed(make_agent, phase):
    plan = Plan() if phase == "plan" else Plan(steps=(echo(),))
    llm = FakeLLM(LLMResponse(content=""))
    core, _, _, memory, _, _ = make_agent(plan, llm=llm)
    result = core.run("empty result")
    assert result.status == TaskStatus.FAILED
    assert result.error
    assert len(result.tool_results) == (1 if phase == "reflect" else 0)
    assert not memory.saved


def test_cancellation_after_first_memory_save_prevents_remaining_save_and_completion(make_agent):
    class CancelMemory(FakeMemory):
        def save(self, record):
            saved = super().save(record)
            core.tasks.update_status(record.task_id, TaskStatus.CANCELLED, result={"cancel-marker": True})
            return saved

    memory = CancelMemory()
    core, _, _, _, _, _ = make_agent(Plan(steps=(echo(),)), memory=memory)
    result = core.run("记住：用户事实")
    assert result.status == TaskStatus.CANCELLED
    assert len(result.tool_results) == 1
    assert len(memory.saved) == 1
    assert memory.saved[0].kind == MemoryKind.SEMANTIC
    assert core.tasks.get(result.task_id).result == {"cancel-marker": True}


def test_formal_waiting_cancel_audits_once_and_preserves_cancelled_task(make_agent):
    write = ToolCall("write_text", {"path": "note.txt", "text": "x"}, "write-1")
    core, _, _, _, logger, root = make_agent(Plan(steps=(write,)))
    waiting = core.run("write")
    cancelled = core.cancel_task(waiting.task_id)
    assert cancelled.status == TaskStatus.CANCELLED
    transitions = [data for event, task_id, data in logger.records
                   if event == "task_status_changed" and task_id == waiting.task_id and data["status"] == "Cancelled"]
    assert len(transitions) == 1
    assert transitions[0]["previous_status"] == "Waiting"
    count = len(logger.records)
    assert core.cancel_task(waiting.task_id) == cancelled
    assert len(logger.records) == count
    assert not list(root.iterdir())


def test_formal_cancel_at_tool_boundary_audits_observation_and_stops_next_step(make_agent):
    core, _, llm, memory, logger, _ = make_agent(Plan(steps=(ToolCall("callback", {}, "cancel"), echo())))
    tool = CallbackTool(lambda context: core.cancel_task(context.task_id))
    core.tools.register(tool)
    result = core.run("cancel during tool")
    assert result.status == TaskStatus.CANCELLED
    assert len(result.tool_results) == 1
    assert not llm.calls and not memory.saved
    observed = [data for event, task_id, data in logger.records
                if event == "request_cancelled" and task_id == result.task_id]
    assert len(observed) == 1
    assert observed[0]["status"] == "Cancelled"
    assert len(observed[0]["tool_results"]) == 1


def test_cancel_observation_audit_failure_is_visible_without_overriding_cancelled(make_agent):
    logger = CaptureLog(lambda event, data: event == "request_cancelled")
    core, planner, _, _, _, _ = make_agent(Plan(steps=(echo(),)), logger=logger)
    planner.hook = lambda: core.tasks.update_status(core.tasks.list_tasks()[-1].id, TaskStatus.CANCELLED,
                                                  result={"cancel-marker": True})
    result = core.run("cancel audit failure")
    assert result.status == TaskStatus.CANCELLED
    assert result.error
    assert result.response
    assert core.tasks.get(result.task_id).status == TaskStatus.CANCELLED
    assert core.tasks.get(result.task_id).result == {"cancel-marker": True}


def test_formal_cancel_audit_failure_does_not_change_waiting_state(make_agent):
    logger = CaptureLog(lambda event, data: event == "task_status_changed" and data.get("status") == "Cancelled")
    write = ToolCall("write_text", {"path": "note.txt", "text": "x"}, "write-1")
    core, _, _, _, _, _ = make_agent(Plan(steps=(write,)), logger=logger)
    waiting = core.run("write")
    previous = core.tasks.get(waiting.task_id)
    with pytest.raises(OSError):
        core.cancel_task(waiting.task_id)
    assert core.tasks.get(waiting.task_id) == previous


def test_formal_cancel_rejects_unknown_terminal_and_closed_tasks(make_agent):
    core, _, _, _, _, _ = make_agent()
    completed = core.run("hello")
    with pytest.raises(KeyError):
        core.cancel_task("missing")
    with pytest.raises(ValueError):
        core.cancel_task(completed.task_id)
    assert core.tasks.get(completed.task_id).status == TaskStatus.COMPLETED
    core.close()
    with pytest.raises(RuntimeError):
        core.cancel_task(completed.task_id)
