"""Failure containment with real temporary workflow transactions and no models."""

from dataclasses import asdict
from threading import Event

import pytest

from deskpal.agent.models import AgentResult, AuthorizationRequest
from deskpal.application.dispatcher import DispatcherFailed, SubmissionDispatcher
from deskpal.planner.models import Plan
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import PermissionLevel, ToolCall, ToolResult, ToolSpec
from deskpal.workflow import ExecutionJournal, PersistentTaskManager, WorkflowBlocked, WorkflowStore


class Core:
    def __init__(self, tasks, journal):
        self.tasks, self.journal = tasks, journal
        self.calls = []
        self.hook = None
        self.success = True
        self.output = "synthetic effect"

    def execute_task(self, task_id, *, authorized_tools=(), high_risk_authorized_tools=()):
        self.calls.append((task_id, authorized_tools, high_risk_authorized_tools))
        if self.hook is not None:
            return self.hook(task_id)
        return self.complete(task_id)

    def confirm_task(self, task_id, **kwargs):
        return self.execute_task(task_id, **kwargs)

    def begin(self, task_id):
        self.tasks.update_status(task_id, TaskStatus.PLANNING)
        self.journal.save_plan(task_id, Plan("synthetic", (ToolCall("effect"),)),
                               (ToolSpec("effect", "synthetic", {}),), "workspace")
        self.tasks.update_status(task_id, TaskStatus.RUNNING)
        return self.journal.start_step(task_id, 0)

    def complete(self, task_id):
        attempt = self.begin(task_id)
        result = ToolResult(self.success, self.output, tool_name="effect", call_id="step-0")
        self.journal.finish_step(attempt.id, result)
        status = TaskStatus.COMPLETED if self.success else TaskStatus.FAILED
        self.tasks.update_status(task_id, status,
            result={"response": "synthetic", "tool_results": [asdict(result)]})
        return AgentResult(task_id, status, "synthetic", (result,))


@pytest.fixture
def rig(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3", queue_capacity=3, result_max_bytes=1024)
    tasks, journal = PersistentTaskManager(store), ExecutionJournal(store)
    core = Core(tasks, journal)
    dispatcher = SubmissionDispatcher(core, tasks, journal, guard=lambda _: None, on_idle=lambda: None)
    yield dispatcher, core, tasks, journal
    assert dispatcher.shutdown(5)
    store.close()


def submit(dispatcher, command="one", grants=frozenset()):
    return dispatcher.submit("synthetic " + command, command_id=command,
        authorized_tools=grants, high_risk_authorized_tools=frozenset())


def test_claim_failure_stops_worker_notifies_waiter_and_rejects_new_work(rig, monkeypatch):
    dispatcher, core, tasks, _ = rig
    task = submit(dispatcher)
    def fail():
        raise OSError("synthetic private database detail")
    monkeypatch.setattr(tasks, "claim_next", fail)
    dispatcher.start()
    with pytest.raises(DispatcherFailed, match="claim"):
        dispatcher.wait(task.id, timeout=2)
    info = dispatcher.diagnostics()
    assert info["failed"] and info["error_type"] == "OSError"
    assert "private" not in str(info)
    assert core.calls == []
    with pytest.raises(DispatcherFailed):
        submit(dispatcher, "later")


def test_submission_finish_failure_cannot_leave_wait_permanently_active(rig, monkeypatch):
    dispatcher, core, tasks, journal = rig
    task = submit(dispatcher)
    original = tasks.finish_submission
    writes = []
    def fail_second(task_id, status="Finished"):
        writes.append(task_id)
        if len(writes) == 2:
            raise OSError("synthetic submission write failure")
        return original(task_id, status)
    monkeypatch.setattr(tasks, "finish_submission", fail_second)
    dispatcher.start()
    with pytest.raises(DispatcherFailed, match="finish_submission"):
        dispatcher.wait(task.id, timeout=2)
    assert tasks.get(task.id).status == TaskStatus.COMPLETED
    assert journal.attempts(task.id)[0].status == "Succeeded"
    assert len(core.calls) == 1
    assert dispatcher.shutdown(2)
    assert dispatcher.diagnostics()["active_task_id"] is None


@pytest.mark.parametrize("result_committed", [False, True])
def test_journal_outcome_failure_never_retries_effect_or_claims_next(rig, monkeypatch, result_committed):
    dispatcher, core, tasks, journal = rig
    first, second = submit(dispatcher), submit(dispatcher, "two")
    original = journal.finish_step
    def fail(attempt_id, result):
        if result_committed:
            original(attempt_id, result)
        raise OSError("synthetic result commit interruption")
    monkeypatch.setattr(journal, "finish_step", fail)
    dispatcher.start()
    with pytest.raises(DispatcherFailed):
        dispatcher.wait(first.id, timeout=2)
    assert tasks.get(first.id).status == (TaskStatus.NEEDS_REVIEW if result_committed else TaskStatus.UNCERTAIN)
    assert journal.attempts(first.id)[0].status == ("Succeeded" if result_committed else "Uncertain")
    assert tasks.get(second.id).status == TaskStatus.CREATED
    assert [call[0] for call in core.calls] == [first.id]


def test_failure_while_persisting_unknown_state_is_also_contained(rig, monkeypatch):
    dispatcher, core, tasks, journal = rig
    task = submit(dispatcher)
    def interrupted(task_id):
        core.begin(task_id)
        raise RuntimeError("synthetic tool returned no outcome")
    def failed_mark(*args):
        raise OSError("synthetic uncertainty commit failure")
    core.hook = interrupted
    monkeypatch.setattr(journal, "mark_uncertain", failed_mark)
    dispatcher.start()
    with pytest.raises(DispatcherFailed, match="execution_outcome"):
        dispatcher.wait(task.id, timeout=2)
    assert journal.attempts(task.id)[0].status == "Started"
    assert dispatcher.shutdown(2)


@pytest.mark.parametrize("status", [TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.COMPLETED])
def test_terminal_state_wins_race_with_policy_block(rig, status):
    dispatcher, core, tasks, _ = rig
    task = submit(dispatcher)
    def guard(task_id):
        if status == TaskStatus.CANCELLED:
            tasks.request_cancel(task_id)
        elif status == TaskStatus.COMPLETED:
            tasks.update_status(task_id, TaskStatus.PLANNING)
            tasks.update_status(task_id, TaskStatus.RUNNING)
            tasks.update_status(task_id, status)
        else:
            tasks.update_status(task_id, status)
        raise WorkflowBlocked("synthetic simultaneous source change")
    dispatcher._guard = guard
    dispatcher.start()
    assert dispatcher.wait(task.id, timeout=2).status == status
    assert tasks.get(task.id).status == status
    assert not dispatcher.diagnostics()["failed"]
    assert core.calls == []


def test_idempotent_retry_during_claimed_created_window_cannot_add_grants(rig):
    dispatcher, core, tasks, _ = rig
    entered, release = Event(), Event()
    def guard(task_id):
        entered.set()
        assert release.wait(5)
    dispatcher._guard = guard
    task = submit(dispatcher)
    dispatcher.start()
    try:
        assert entered.wait(2)
        assert tasks.get(task.id).status == TaskStatus.CREATED
        assert submit(dispatcher, grants=frozenset({"write_text"})).id == task.id
        assert dispatcher.diagnostics()["pending_operations"] == 0
    finally:
        release.set()
    assert dispatcher.wait(task.id, timeout=2).status == TaskStatus.COMPLETED
    assert core.calls[0][1] == frozenset()


def test_cancelled_queued_operations_are_bounded_and_cache_clear_is_scoped(rig):
    dispatcher, _, tasks, _ = rig
    for index in range(12):
        task = submit(dispatcher, str(index))
        tasks.request_cancel(task.id)
        assert dispatcher.diagnostics()["pending_operations"] <= 1
    first, second = submit(dispatcher, "remaining-one"), submit(dispatcher, "remaining-two")
    assert dispatcher.diagnostics()["pending_operations"] == 2
    dispatcher.clear_cached()
    assert dispatcher.diagnostics()["pending_operations"] == 2
    dispatcher.clear_cached((first.id,))
    assert dispatcher.diagnostics()["pending_operations"] == 1
    assert second.id in dispatcher._operations


def test_failed_cancel_during_shutdown_still_joins_active_call(rig, monkeypatch):
    dispatcher, core, tasks, _ = rig
    entered, release = Event(), Event()
    def guard(task_id):
        entered.set()
        assert release.wait(5)
    dispatcher._guard = guard
    task = submit(dispatcher)
    dispatcher.start()
    assert entered.wait(2)
    def fail_cancel(task_id):
        raise OSError("synthetic cancellation database failure")
    monkeypatch.setattr(tasks, "request_cancel", fail_cancel)
    try:
        assert not dispatcher.shutdown(0.01)
        assert dispatcher.diagnostics()["worker_alive"]
        assert tasks.get(task.id) is not None
    finally:
        release.set()
    assert dispatcher.shutdown(2)
    assert dispatcher.diagnostics()["error_stage"] == "shutdown_cancel"


def test_thread_start_failure_can_be_closed_without_joining_unstarted_thread(rig, monkeypatch):
    dispatcher, _, _, _ = rig
    def fail():
        raise RuntimeError("synthetic thread startup failure")
    monkeypatch.setattr(dispatcher._thread, "start", fail)
    with pytest.raises(RuntimeError):
        dispatcher.start()
    assert not dispatcher.diagnostics()["started"]
    assert dispatcher.shutdown(1)
    assert dispatcher.diagnostics()["error_stage"] == "start"


def test_shutdown_from_worker_reports_live_call_instead_of_self_join(rig):
    dispatcher, core, tasks, _ = rig
    closed = []
    def stop_from_worker(task_id):
        closed.append(dispatcher.shutdown(0))
        return AgentResult(task_id, tasks.get(task_id).status, "cooperative shutdown")
    core.hook = stop_from_worker
    task = submit(dispatcher)
    dispatcher.start()
    assert dispatcher.wait(task.id, timeout=2).status == TaskStatus.CANCELLED
    assert closed == [False]
    assert dispatcher.shutdown(2)


@pytest.mark.parametrize("success", [True, False])
def test_oversized_core_result_is_not_cached_and_retains_reliable_outcome(rig, success):
    dispatcher, core, _, _ = rig
    core.output, core.success = "SYNTHETIC_LARGE_OUTPUT" * 5000, success
    task = submit(dispatcher)
    dispatcher.start()
    result = dispatcher.wait(task.id, timeout=2)
    assert result.status == (TaskStatus.COMPLETED if success else TaskStatus.FAILED)
    assert len(result.tool_results) == 1
    assert result.tool_results[0].success == success
    assert result.tool_results[0].output == {"output_omitted": True}
    assert "omitted" in " ".join(result.warnings)
    assert dispatcher.diagnostics()["cached_results"] == 0


def test_cached_tool_result_does_not_share_mutable_output_with_callers_or_core(rig):
    dispatcher, core, tasks, _ = rig
    core.output = {"nested": {"value": "trusted"}}
    task = submit(dispatcher)
    dispatcher.start()
    result = dispatcher.wait(task.id, timeout=2)
    result.tool_results[0].output["nested"]["value"] = "caller forgery"
    core.output["nested"]["value"] = "core mutation"
    assert dispatcher.wait(task.id).tool_results[0].output == {"nested": {"value": "trusted"}}
    assert tasks.get(task.id).result["tool_results"][0]["output"] == {"nested": {"value": "trusted"}}


def test_cached_confirmation_parameters_remain_exact_after_caller_mutation(rig):
    dispatcher, core, tasks, journal = rig
    arguments = {"nested": {"value": "trusted"}}
    def wait_for_confirmation(task_id):
        tasks.update_status(task_id, TaskStatus.PLANNING)
        journal.save_plan(task_id, Plan("review", (ToolCall("effect", arguments, "step-0"),)),
                          (ToolSpec("effect", "synthetic", {}),), "workspace")
        tasks.update_status(task_id, TaskStatus.WAITING)
        return AgentResult(task_id, TaskStatus.WAITING, "review",
            authorization_requests=(AuthorizationRequest("effect", arguments, "step-0", PermissionLevel.CONFIRM),),
            planned_calls=(ToolCall("effect", arguments, "step-0"),))
    core.hook = wait_for_confirmation
    task = submit(dispatcher)
    dispatcher.start()
    result = dispatcher.wait(task.id, timeout=2)
    result.planned_calls[0].arguments["nested"]["value"] = "forged plan"
    result.authorization_requests[0].arguments["nested"]["value"] = "forged permission"
    next_result = dispatcher.wait(task.id)
    assert next_result.planned_calls[0].arguments == {"nested": {"value": "trusted"}}
    assert next_result.authorization_requests[0].arguments == {"nested": {"value": "trusted"}}
    assert journal.get_plan(task.id).plan.steps[0].arguments == {"nested": {"value": "trusted"}}


@pytest.mark.parametrize("validator_raises", [False, True])
def test_claim_cancellation_wins_over_concurrent_dependency_invalidation(rig, validator_raises):
    dispatcher, core, tasks, journal = rig
    store = tasks.store
    store.set_dependency_version("goal", "goal", 1, use_policy="cloud_allowed")
    task = tasks.submit("synthetic", command_id="race", dependencies=(("goal", "goal", 1),))
    def invalidate(task_id, refs):
        tasks.request_cancel(task_id)
        store.set_dependency_version("goal", "goal", 2, use_policy="cloud_allowed")
        if validator_raises:
            raise WorkflowBlocked("synthetic concurrent source deletion")
    journal.add_validator(invalidate)
    dispatcher.start()
    assert dispatcher.wait(task.id, timeout=2).status == TaskStatus.CANCELLED
    assert not dispatcher.diagnostics()["failed"]
    assert core.calls == []
    with store.transaction() as db:
        assert db.execute("SELECT status FROM submission WHERE task_id=?", (task.id,)).fetchone()[0] == "Finished"


def test_confirmation_source_failure_becomes_reviewable_instead_of_hanging_wait(rig):
    dispatcher, _, tasks, journal = rig
    task = tasks.submit("synthetic", command_id="review")
    tasks.claim_next()
    tasks.update_status(task.id, TaskStatus.PLANNING)
    journal.save_plan(task.id, Plan("review", (ToolCall("effect"),)),
                      (ToolSpec("effect", "synthetic", {}),), "workspace")
    tasks.update_status(task.id, TaskStatus.WAITING)
    tasks.finish_submission(task.id, "Blocked")
    dispatcher.confirm(task.id, frozenset(), frozenset())
    def invalid_source(task_id, refs):
        raise WorkflowBlocked("synthetic changed source")
    journal.add_validator(invalid_source)
    dispatcher.start()
    assert dispatcher.wait(task.id, timeout=2).status == TaskStatus.NEEDS_REVIEW
    assert not dispatcher.diagnostics()["failed"]
    assert dispatcher.diagnostics()["pending_operations"] == 0
