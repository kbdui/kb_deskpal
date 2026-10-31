from dataclasses import FrozenInstanceError

import pytest

from deskpal.goals import (BindingStatus, GoalConflict, GoalManager, GoalStatus,
                           MilestoneStatus)
from deskpal.workflow import PersistentTaskManager, WorkflowStore
from deskpal.workflow.models import WorkflowBlocked


@pytest.fixture
def domain(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    yield store, GoalManager(store), PersistentTaskManager(store)
    store.close()


def test_explicit_defaults_snapshot_and_reopen(domain):
    store, goals, _ = domain
    goal = goals.create("Write chapter", "User reviewed complete chapter", command_id="create")
    assert goal.status == GoalStatus.ACTIVE
    assert goal.use_policy == "local_only"
    assert (goal.revision, goal.execution_version) == (1, 1)
    with pytest.raises(FrozenInstanceError):
        goal.title = "changed"
    assert GoalManager(store).get(goal.id) == goal
    assert goals.create("Write chapter", "User reviewed complete chapter", command_id="create") == goal
    with pytest.raises(GoalConflict):
        goals.create("Other title", "Other criterion", command_id="create")


def test_progress_note_does_not_invalidate_tasks_but_execution_conditions_do(domain):
    store, goals, tasks = domain
    goal = goals.create("Goal", "Criterion")
    task = tasks.submit("work", command_id="task", dependencies=(("goal", goal.id, 1),), use_policy="local_only")
    noted = goals.revise(goal.id, 1, progress_note="Some progress", command_id="note")
    assert (noted.revision, noted.execution_version) == (2, 1)
    with store.transaction() as db:
        store.check_dependencies(db, task.id)
    changed = goals.revise(goal.id, 2, success_criteria="Different criterion")
    assert (changed.revision, changed.execution_version) == (3, 2)
    with pytest.raises(WorkflowBlocked), store.transaction() as db:
        store.check_dependencies(db, task.id)


def test_cas_terminal_and_user_completion_evidence(domain):
    _, goals, _ = domain
    goal = goals.create("Goal", "Criterion")
    paused = goals.set_status(goal.id, 1, GoalStatus.PAUSED)
    with pytest.raises(GoalConflict):
        goals.revise(goal.id, 1, title="stale")
    active = goals.set_status(goal.id, paused.revision, "Active")
    with pytest.raises(ValueError):
        goals.set_status(goal.id, active.revision, "Completed")
    completed = goals.set_status(goal.id, active.revision, "Completed", evidence="User verified output", command_id="done")
    assert completed.completion_evidence == "User verified output"
    with pytest.raises(GoalConflict):
        goals.revise(goal.id, completed.revision, progress_note="overwrite terminal")
    assert goals.set_status(goal.id, active.revision, "Completed", evidence="User verified output", command_id="done") == completed


def test_milestone_and_binding_version_scope_isolation(domain):
    store, goals, tasks = domain
    goal = goals.create("Goal", "Criterion")
    first = goals.add_milestone(goal.id, goal.revision, "First", "Done first")
    second = goals.add_milestone(goal.id, goals.get(goal.id).revision, "Second", "Done second")
    one = tasks.submit("one", command_id="one", dependencies=(("goal", goal.id, 1), ("milestone", first.id, 1)), use_policy="local_only")
    two = tasks.submit("two", command_id="two", dependencies=(("goal", goal.id, 1), ("milestone", second.id, 1)), use_policy="local_only")
    goals.set_milestone_status(first.id, 1, MilestoneStatus.CANCELLED)
    with pytest.raises(WorkflowBlocked), store.transaction() as db:
        store.check_dependencies(db, one.id)
    with store.transaction() as db:
        store.check_dependencies(db, two.id)
    binding = goals.bind_source(goal.id, goals.get(goal.id).revision, "timer", "timer-id", milestone_id=second.id)
    assert binding.use_policy == "local_only"
    bound = tasks.submit("bound", command_id="bound", dependencies=(("binding", binding.id, 1),), use_policy="local_only")
    removed = goals.remove_binding(binding.id, 1)
    assert removed.status == BindingStatus.REMOVED and removed.revision == 2
    with pytest.raises(WorkflowBlocked), store.transaction() as db:
        store.check_dependencies(db, bound.id)
    assert goals.get(goal.id).execution_version == 1


def test_milestone_notes_conditions_evidence_and_independent_goal(domain):
    _, goals, _ = domain
    goal = goals.create("Goal", "Criterion")
    milestone = goals.add_milestone(goal.id, 1, "First", "User approves")
    note = goals.revise_milestone(milestone.id, 1, progress_note="note")
    assert note.execution_version == 1
    revised = goals.revise_milestone(milestone.id, 2, deadline="2027-01-01T08:00:00+08:00")
    assert revised.execution_version == 2
    with pytest.raises(ValueError):
        goals.complete_milestone(milestone.id, revised.revision, evidence="")
    complete = goals.complete_milestone(milestone.id, revised.revision, evidence="User accepted evidence")
    assert complete.status == MilestoneStatus.COMPLETED
    assert goals.get(goal.id).status == GoalStatus.ACTIVE


def test_transactions_rollback_goal_and_audit_on_failure(domain, monkeypatch):
    store, goals, _ = domain
    original = store.record_event
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("audit failure")
    monkeypatch.setattr(store, "record_event", fail)
    with pytest.raises(RuntimeError):
        goals.create("Goal", "Criterion", command_id="retry")
    assert goals.list() == ()
    monkeypatch.setattr(store, "record_event", original)
    assert goals.create("Goal", "Criterion", command_id="retry").revision == 1


@pytest.mark.parametrize("deadline", ["2026-01-01", "2026-01-01T09:00:00", "tomorrow"])
def test_deadline_requires_explicit_offset(domain, deadline):
    _, goals, _ = domain
    with pytest.raises(ValueError):
        goals.create("Goal", "Criterion", deadline=deadline)


def test_bounded_ordered_listing_and_foreign_milestone(domain):
    _, goals, _ = domain
    first = goals.create("One", "Criterion")
    second = goals.create("Two", "Criterion")
    milestone = goals.add_milestone(first.id, 1, "First", "Criterion")
    with pytest.raises(ValueError):
        goals.bind_source(second.id, 1, "timer", "timer", milestone_id=milestone.id)
    assert goals.list(limit=1) == (goals.get(first.id),)
    assert goals.list(limit=1, offset=1) == (second,)
    with pytest.raises(ValueError):
        goals.list(limit=101)

