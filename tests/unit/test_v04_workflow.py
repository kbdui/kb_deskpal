from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

from deskpal.planner.models import Plan
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolCall, ToolResult, ToolSpec
from deskpal.workflow import (ExecutionJournal, PersistentTaskManager, QueueFull,
                              TaskCancellationRequested, WorkflowBlocked, WorkflowStore)


@pytest.fixture
def workflow(tmp_path: Path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    yield store, PersistentTaskManager(store), ExecutionJournal(store)
    store.close()


def running(tasks, journal, *, steps=1, memory_refs=()):
    task = tasks.submit("synthetic request", command_id="run")
    tasks.claim_next()
    tasks.update_status(task.id, TaskStatus.PLANNING)
    plan = Plan("synthetic reply", tuple(ToolCall("test", {"index": i}) for i in range(steps)))
    journal.save_plan(task.id, plan, (ToolSpec("test", "synthetic", {"type": "object"}),), "workspace", memory_refs)
    tasks.update_status(task.id, TaskStatus.RUNNING)
    return task


def test_nested_transaction_rolls_back_entire_submission(workflow):
    store, tasks, _ = workflow
    with pytest.raises(RuntimeError, match="abort"):
        with store.transaction():
            tasks.submit("must disappear", command_id="rollback")
            raise RuntimeError("abort")
    assert tasks.list_tasks() == ()
    assert store.pending_audit() == ()


def test_inner_failure_cannot_be_swallowed_to_commit(workflow):
    store, tasks, _ = workflow
    with pytest.raises(RuntimeError, match="Nested"):
        with store.transaction():
            tasks.create("first")
            try:
                tasks.create("")
            except ValueError:
                pass
    assert tasks.list_tasks() == ()


def test_unknown_schema_refused(tmp_path):
    path = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_version(version INTEGER)")
        conn.execute("INSERT INTO schema_version VALUES(42)")
    with pytest.raises(ValueError, match="schema"):
        WorkflowStore(path)


def test_submit_idempotent_concurrent_and_bounded(tmp_path):
    store = WorkflowStore(tmp_path / "queue.sqlite3", queue_capacity=1)
    tasks = PersistentTaskManager(store)
    with ThreadPoolExecutor(max_workers=4) as pool:
        submitted = tuple(pool.map(lambda _: tasks.submit("same", command_id="one"), range(8)))
    assert len({task.id for task in submitted}) == 1
    with pytest.raises(ValueError, match="different payload"):
        tasks.submit("different", command_id="one")
    with pytest.raises(QueueFull):
        tasks.submit("another", command_id="two")
    assert len(tasks.list_tasks()) == 1
    assert tasks.claim_next().id == submitted[0].id
    assert tasks.claim_next() is None
    store.close()


def test_dependency_check_and_source_policy_are_atomic(workflow):
    store, tasks, _ = workflow
    store.set_dependency_version("goal", "one", 1)
    with pytest.raises(WorkflowBlocked, match="policy"):
        tasks.submit("private", command_id="bad", dependencies=(("goal", "one", 1),))
    task = tasks.submit("private", command_id="good", dependencies=(("goal", "one", 1),), use_policy="local_only")
    store.set_dependency_version("goal", "one", 2)
    assert tasks.claim_next() is None
    assert tasks.get(task.id).status == TaskStatus.NEEDS_REVIEW


def test_plan_snapshot_is_independent_and_exact(workflow):
    _, tasks, journal = workflow
    task = tasks.create("plan")
    args = {"nested": {"value": 1}}
    plan = Plan("reply", (ToolCall("tool", args),))
    snapshot = journal.save_plan(task.id, plan, (ToolSpec("tool", "description", {}),), "workspace")
    args["nested"]["value"] = 99
    snapshot.plan.steps[0].arguments["nested"]["value"] = 88
    saved = journal.get_plan(task.id)
    assert saved.plan.steps[0].arguments == {"nested": {"value": 1}}
    assert saved.plan.steps[0].call_id == "step-0"
    assert saved.hash == snapshot.hash
    with pytest.raises(WorkflowBlocked, match="immutable"):
        journal.save_plan(task.id, plan, (ToolSpec("tool", "description", {}),), "workspace")


def test_byte_limits_reject_inputs_preserve_output_outcome(tmp_path):
    store = WorkflowStore(tmp_path / "limits.sqlite3", request_max_bytes=30, plan_max_bytes=3000, result_max_bytes=50)
    tasks, journal = PersistentTaskManager(store), ExecutionJournal(store)
    with pytest.raises(ValueError, match="byte limit"):
        tasks.create("过" * 11)
    task = running(tasks, journal)
    attempt = journal.start_step(task.id, 0)
    journal.finish_step(attempt.id, ToolResult(True, "large" * 1000))
    saved = journal.attempts(task.id)[0]
    assert saved.status == "Succeeded"
    assert saved.result.success
    assert saved.result.output == {"output_omitted": True}
    store.close()


def test_attempt_is_committed_before_tool_and_cannot_replay(workflow):
    store, tasks, journal = workflow
    task = running(tasks, journal)
    attempt = journal.start_step(task.id, 0)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT status FROM attempt WHERE id=?", (attempt.id,)).fetchone()[0] == "Started"
    with pytest.raises(WorkflowBlocked):
        journal.start_step(task.id, 0)
    journal.finish_step(attempt.id, ToolResult(True, "done"))
    with pytest.raises(WorkflowBlocked):
        journal.finish_step(attempt.id, ToolResult(True, "overwritten"))


def test_running_cancel_is_flag_then_blocks_next_step(workflow):
    _, tasks, journal = workflow
    task = running(tasks, journal, steps=2)
    attempt = journal.start_step(task.id, 0)
    cancelled = tasks.request_cancel(task.id)
    assert cancelled.status == TaskStatus.RUNNING
    assert cancelled.cancel_requested
    journal.finish_step(attempt.id, ToolResult(True, "effect retained"))
    with pytest.raises(TaskCancellationRequested):
        journal.start_step(task.id, 1)


def test_recovery_unknown_is_never_executable(workflow):
    _, tasks, journal = workflow
    task = running(tasks, journal)
    journal.start_step(task.id, 0)
    tasks.request_cancel(task.id)
    journal.recover()
    assert tasks.get(task.id).status == TaskStatus.UNCERTAIN
    assert tasks.get(task.id).cancel_requested
    journal.review_uncertain(task.id, "not_occurred", "reviewed synthetic artifact")
    with pytest.raises(WorkflowBlocked):
        journal.resume_unstarted(task.id)
    assert journal.attempts(task.id)[0].status == "Uncertain"


@pytest.mark.parametrize("cancel", [False, True])
def test_recovery_all_results_finalizes_without_execution(workflow, cancel):
    _, tasks, journal = workflow
    task = running(tasks, journal)
    attempt = journal.start_step(task.id, 0)
    journal.finish_step(attempt.id, ToolResult(True, {"effect": 1}))
    if cancel:
        tasks.request_cancel(task.id)
    journal.recover()
    assert tasks.get(task.id).status == (TaskStatus.CANCELLED if cancel else TaskStatus.COMPLETED)
    assert len(journal.attempts(task.id)) == 1


def test_partial_recovery_requires_new_workflow(workflow):
    _, tasks, journal = workflow
    task = running(tasks, journal, steps=2)
    journal.finish_step(journal.start_step(task.id, 0).id, ToolResult(True))
    journal.recover()
    assert tasks.get(task.id).status == TaskStatus.NEEDS_REVIEW
    with pytest.raises(WorkflowBlocked, match="previous attempts"):
        journal.resume_unstarted(task.id)


def test_unstarted_recovery_plan_waits_for_fresh_confirmation(workflow):
    _, tasks, journal = workflow
    task = running(tasks, journal)
    journal.recover()
    assert journal.resume_unstarted(task.id).status == TaskStatus.WAITING
    assert tasks.claim_next() is None
    tasks.requeue(task.id)
    assert tasks.claim_next().status == TaskStatus.WAITING


def test_forgetting_blocks_permanently_and_late_results_cannot_restore_body(workflow):
    store, tasks, journal = workflow
    task = running(tasks, journal, memory_refs=(("memory", 1),))
    attempt = journal.start_step(task.id, 0)
    job = journal.begin_deletion("memory", 1)
    with pytest.raises(WorkflowBlocked):
        journal.check_task(task.id)
    journal.complete_deletion(job)
    journal.finish_step(attempt.id, ToolResult(True, "forgotten sensitive body", error="secret error"))
    assert tasks.get(task.id).request == "[forgotten]"
    assert journal.get_plan(task.id) is None
    assert journal.attempts(task.id)[0].result.output == {"output_omitted": True}
    assert journal.pending_deletions() == ()
    with pytest.raises(WorkflowBlocked):
        tasks.submit("new", command_id="deleted", dependencies=(("memory", 1),))
    assert "forgotten sensitive body" not in str(store.pending_audit())


def test_reopen_and_online_backup_preserve_queue(tmp_path):
    path = tmp_path / "workflow.sqlite3"
    store = WorkflowStore(path)
    task = PersistentTaskManager(store).submit("persist", command_id="persist")
    backup = tmp_path / "backup.sqlite3"
    store.backup(backup)
    store.close()
    reopened = WorkflowStore(backup)
    assert PersistentTaskManager(reopened).get(task.id).request == "persist"
    reopened.close()


def test_external_validator_does_not_hold_write_transaction(workflow):
    store, tasks, journal = workflow
    task = running(tasks, journal)
    checks = []
    def validate(task_id, refs):
        with sqlite3.connect(store.path, timeout=0.1) as other:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        checks.append(task_id)
    journal.add_validator(validate)
    journal.start_step(task.id, 0)
    assert checks == [task.id]


@pytest.mark.parametrize("damage", ["plan", "outcome", "index"])
def test_corrupt_ledger_cannot_claim_reliable_success(workflow, damage):
    store, tasks, journal = workflow
    task = running(tasks, journal)
    journal.finish_step(journal.start_step(task.id, 0).id, ToolResult(True, "effect"))
    with store.transaction() as conn:
        if damage == "plan":
            conn.execute("UPDATE immutableplan SET canonical_json='{}' WHERE task_id=?", (task.id,))
        elif damage == "outcome":
            conn.execute("UPDATE attempt SET result='broken-json' WHERE task_id=?", (task.id,))
        else:
            conn.execute("UPDATE attempt SET step_index=2 WHERE task_id=?", (task.id,))
    journal.recover()
    assert tasks.get(task.id).status == TaskStatus.UNCERTAIN
    with pytest.raises(WorkflowBlocked):
        journal.resume_unstarted(task.id)


def test_forget_preserves_known_terminal_outcome_on_restart(workflow):
    _, tasks, journal = workflow
    task = running(tasks, journal, memory_refs=(("memory", 1),))
    journal.finish_step(journal.start_step(task.id, 0).id, ToolResult(True, "known effect"))
    tasks.update_status(task.id, TaskStatus.COMPLETED, result={"reply": "done"})
    journal.complete_deletion(journal.begin_deletion("memory", 1))
    journal.recover()
    assert tasks.get(task.id).status == TaskStatus.COMPLETED
    assert journal.attempts(task.id)[0].status == "Succeeded"
