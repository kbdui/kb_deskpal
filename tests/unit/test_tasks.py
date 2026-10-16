"""Synchronous task state contracts, defensive snapshots and audit failures."""

from dataclasses import FrozenInstanceError
from datetime import timezone

import pytest

from deskpal.tasks import InMemoryTaskManager, TaskStatus


ALLOWED = {
    TaskStatus.CREATED: {TaskStatus.PLANNING, TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.NEEDS_REVIEW},
    TaskStatus.PLANNING: {TaskStatus.RUNNING, TaskStatus.WAITING, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.NEEDS_REVIEW, TaskStatus.UNCERTAIN},
    TaskStatus.RUNNING: {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.NEEDS_REVIEW, TaskStatus.UNCERTAIN},
    TaskStatus.WAITING: {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.NEEDS_REVIEW},
    TaskStatus.NEEDS_REVIEW: {TaskStatus.CREATED, TaskStatus.PLANNING, TaskStatus.WAITING, TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.UNCERTAIN},
    TaskStatus.UNCERTAIN: set(),
    TaskStatus.COMPLETED: set(), TaskStatus.FAILED: set(), TaskStatus.CANCELLED: set(),
}
PATHS = {
    TaskStatus.CREATED: (), TaskStatus.PLANNING: (TaskStatus.PLANNING,),
    TaskStatus.RUNNING: (TaskStatus.PLANNING, TaskStatus.RUNNING),
    TaskStatus.WAITING: (TaskStatus.PLANNING, TaskStatus.WAITING),
    TaskStatus.COMPLETED: (TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.COMPLETED),
    TaskStatus.FAILED: (TaskStatus.FAILED,), TaskStatus.CANCELLED: (TaskStatus.CANCELLED,),
    TaskStatus.NEEDS_REVIEW: (TaskStatus.NEEDS_REVIEW,),
    TaskStatus.UNCERTAIN: (TaskStatus.PLANNING, TaskStatus.UNCERTAIN),
}


def task_at(manager, status):
    task = manager.create("测试任务")
    for step in PATHS[status]:
        task = manager.update_status(task.id, step, result={"source": status.value}, error="previous-error")
    return task


@pytest.mark.parametrize("source", tuple(TaskStatus))
def test_state_transition_matrix_and_same_state_idempotence(source):
    # Seven source cases cover every pair without creating 49 repetitive tests.
    for destination in TaskStatus:
        manager = InMemoryTaskManager()
        previous = task_at(manager, source)
        if destination == source:
            current = manager.update_status(previous.id, destination, result={"new": True}, error="new-error")
            assert current == previous
        elif destination in ALLOWED[source]:
            current = manager.update_status(previous.id, destination, result={"new": True}, error="new-error")
            assert current.status == destination
            assert current.result == {"new": True}
            assert current.error == "new-error"
            assert current.id == previous.id
            assert current.request == previous.request
            assert current.created_at == previous.created_at
            assert current.updated_at >= previous.updated_at
        else:
            with pytest.raises(ValueError):
                manager.update_status(previous.id, destination, result={"new": True}, error="new-error")
            assert manager.get(previous.id) == previous


def test_created_tasks_have_unique_ids_utc_timestamps_and_creation_order():
    manager = InMemoryTaskManager()
    tasks = [manager.create(request) for request in ("first", "第二个", "third")]
    assert len({task.id for task in tasks}) == 3
    assert all(isinstance(task.id, str) and task.id for task in tasks)
    assert [task.id for task in manager.list_tasks()] == [task.id for task in tasks]
    for task in tasks:
        assert task.status == TaskStatus.CREATED
        assert task.result is None
        assert task.error is None
        assert task.created_at.utcoffset() == timezone.utc.utcoffset(None)
        assert task.updated_at.utcoffset() == timezone.utc.utcoffset(None)
        assert manager.get(task.id) == task
    manager.update_status(tasks[1].id, TaskStatus.PLANNING)
    assert [task.id for task in manager.list_tasks()] == [task.id for task in tasks]


def test_missing_id_behavior():
    manager = InMemoryTaskManager()
    assert manager.get("missing") is None
    with pytest.raises(KeyError):
        manager.update_status("missing", TaskStatus.PLANNING)
    assert not manager.list_tasks()


@pytest.mark.parametrize("request_text", ["", "   ", "\n\t", None, True])
def test_invalid_request_is_rejected_without_creating_task(request_text):
    manager = InMemoryTaskManager()
    with pytest.raises(ValueError):
        manager.create(request_text)
    assert not manager.list_tasks()


def test_new_state_payload_replaces_previous_and_defaults_clear_values():
    manager = InMemoryTaskManager()
    task = manager.create("task")
    planning = manager.update_status(task.id, TaskStatus.PLANNING, result={"old": [1]}, error="old-error")
    running = manager.update_status(task.id, TaskStatus.RUNNING)
    assert running.result is None
    assert running.error is None
    assert planning.result == {"old": [1]}
    assert planning.error == "old-error"


def test_result_input_return_get_and_list_are_independent_nested_snapshots():
    logger = ControlledLog()
    manager = InMemoryTaskManager(execution_log=logger)
    task = task_at(manager, TaskStatus.RUNNING)
    result = {"steps": [{"arguments": {"items": ["原始"]}}]}
    completed = manager.update_status(task.id, TaskStatus.COMPLETED, result=result)
    result["steps"][0]["arguments"]["items"].append("caller-input")
    completed.result["steps"][0]["arguments"]["items"].append("returned-object")
    logger.records[-1][2]["result"]["steps"][0]["arguments"]["items"].append("audit-object")
    queried = manager.get(task.id)
    assert queried.result == {"steps": [{"arguments": {"items": ["原始"]}}]}
    queried.result["steps"][0]["arguments"]["items"].append("queried-object")
    listed = manager.list_tasks()[0]
    assert listed.result == {"steps": [{"arguments": {"items": ["原始"]}}]}
    listed.result["steps"][0]["arguments"]["items"].clear()
    assert manager.get(task.id).result == {"steps": [{"arguments": {"items": ["原始"]}}]}
    with pytest.raises(FrozenInstanceError):
        queried.status = TaskStatus.FAILED


@pytest.mark.parametrize("result", [object(), (1, 2), float("nan"), float("inf"), {"nested": float("-inf")},
                                    {1: "invalid-key"}, {"nested": object()}])
def test_invalid_json_result_does_not_change_task(result):
    manager = InMemoryTaskManager()
    previous = task_at(manager, TaskStatus.PLANNING)
    with pytest.raises(ValueError):
        manager.update_status(previous.id, TaskStatus.RUNNING, result=result)
    assert manager.get(previous.id) == previous


def test_cyclic_result_is_rejected_without_changing_task():
    manager = InMemoryTaskManager()
    previous = task_at(manager, TaskStatus.PLANNING)
    result = {"items": []}
    result["items"].append(result)
    with pytest.raises(ValueError):
        manager.update_status(previous.id, TaskStatus.RUNNING, result=result)
    assert manager.get(previous.id) == previous


@pytest.mark.parametrize("status", [True, False, 1, None, "UnknownStatus"])
def test_invalid_status_is_rejected_without_change(status):
    manager = InMemoryTaskManager()
    previous = manager.create("task")
    with pytest.raises(ValueError):
        manager.update_status(previous.id, status)
    assert manager.get(previous.id) == previous


def test_waiting_can_resume_same_task_or_fail_or_cancel():
    manager = InMemoryTaskManager()
    waiting = task_at(manager, TaskStatus.WAITING)
    running = manager.update_status(waiting.id, TaskStatus.RUNNING)
    assert running.id == waiting.id
    assert running.status == TaskStatus.RUNNING
    manager.update_status(running.id, TaskStatus.COMPLETED)
    waiting = task_at(manager, TaskStatus.WAITING)
    cancelled = manager.update_status(waiting.id, TaskStatus.CANCELLED)
    assert cancelled.status == TaskStatus.CANCELLED
    assert manager.get(waiting.id).status == TaskStatus.CANCELLED


class ControlledLog:
    def __init__(self):
        self.records = []
        self.fail = False

    def record(self, event, task_id=None, data=None):
        if self.fail:
            raise OSError("audit unavailable")
        self.records.append((event, task_id, data))


def test_audit_failure_during_create_does_not_commit_task():
    logger = ControlledLog()
    logger.fail = True
    manager = InMemoryTaskManager(execution_log=logger)
    with pytest.raises(OSError):
        manager.create("task")
    assert not manager.list_tasks()


def test_audit_failure_during_transition_preserves_state_and_payload():
    logger = ControlledLog()
    manager = InMemoryTaskManager(execution_log=logger)
    previous = task_at(manager, TaskStatus.RUNNING)
    logger.fail = True
    with pytest.raises(OSError):
        manager.update_status(previous.id, TaskStatus.COMPLETED, result={"completed": True})
    assert manager.get(previous.id) == previous
    logger.fail = False
    completed = manager.update_status(previous.id, TaskStatus.COMPLETED, result={"completed": True})
    assert completed.status == TaskStatus.COMPLETED


def test_status_changes_are_audited_and_idempotent_update_keeps_payload_and_timestamp():
    logger = ControlledLog()
    manager = InMemoryTaskManager(execution_log=logger)
    completed = task_at(manager, TaskStatus.COMPLETED)
    assert logger.records
    assert all(task_id == completed.id for _, task_id, _ in logger.records)
    assert all(event == "task_status_changed" for event, _, _ in logger.records)
    assert [data["status"] for _, _, data in logger.records] == ["Created", "Planning", "Running", "Completed"]
    assert logger.records[0][2]["previous_status"] is None
    assert logger.records[-1][2]["previous_status"] == "Running"
    count = len(logger.records)
    repeated = manager.update_status(completed.id, TaskStatus.COMPLETED)
    assert repeated == completed
    assert repeated.updated_at == completed.updated_at
    assert repeated.result == completed.result
    assert repeated.error == completed.error
    assert len(logger.records) == count
