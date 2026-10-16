import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from deskpal.config import ProactiveConfig
from deskpal.goals import GoalManager, GoalStatus, MilestoneStatus
from deskpal.proactive import LocalRuleEngine, ProposalConflict, ProposalManager, ProposalStatus
from deskpal.scheduling import SchedulingService
from deskpal.tasks.models import TaskStatus
from deskpal.workflow import ExecutionJournal, PersistentTaskManager, WorkflowStore
from deskpal.workflow.models import QueueFull, WorkflowBlocked


class Clock:
    def __init__(self):
        self.wall = datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc)
        self.elapsed = 1000.0

    def now(self):
        return self.wall

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.elapsed += seconds


class Domain:
    def __init__(self, path, *, clock=None, config=None, capacity=100):
        self.path = Path(path)
        self.clock = clock or Clock()
        self.config = config or ProactiveConfig(enabled=True)
        self.store = WorkflowStore(self.path / "workflow.sqlite3", queue_capacity=capacity)
        self.goals = GoalManager(self.store, clock=self.clock.now)
        self.tasks = PersistentTaskManager(self.store)
        self.schedule = SchedulingService(self.path / "scheduling.sqlite3", clock=self.clock)
        self.proposals = ProposalManager(self.store, self.goals, self.tasks, self.schedule, clock=self.clock.now)
        self.rules = LocalRuleEngine(self.store, self.goals, self.proposals, self.schedule, self.config, clock=self.clock.now)

    def close(self):
        self.schedule.close()
        self.store.close()

    def bound_timer(self, *, goal=None, milestone_id=None):
        goal = goal or self.goals.create("Goal", "Explicit user review")
        timer = self.schedule.create_timer(1, label="Bound timer")
        binding = self.goals.bind_source(goal.id, self.goals.get(goal.id).revision,
                                         "timer", timer.id, milestone_id=milestone_id)
        return goal, timer, binding

    def due(self):
        self.clock.advance(2)
        self.schedule.poll_due()


@pytest.fixture
def domain(tmp_path):
    value = Domain(tmp_path)
    yield value
    value.close()


def test_disabled_and_first_enable_tail_do_not_replay_history(tmp_path):
    d = Domain(tmp_path, config=ProactiveConfig())
    try:
        d.bound_timer()
        d.due()
        assert d.rules.poll() == 0
        assert d.rules.consumer_status() == ()
        d.rules.config = replace(d.config, enabled=True)
        assert d.rules.poll() == 0
        assert d.proposals.list() == ()
        assert all(cursor["status"] == "Active" for cursor in d.rules.consumer_status())
    finally:
        d.close()


def test_explicit_bindings_only_and_atomic_idempotent_accept(domain):
    d = domain
    d.rules.poll()
    goal, _, _ = d.bound_timer()
    d.schedule.create_timer(1, label="Private unbound timer")
    d.due()
    assert d.rules.poll() == 1
    proposal, = d.proposals.list()
    assert proposal.use_policy == "local_only"
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = list(pool.map(lambda key: d.proposals.accept(proposal.id, 1, command_id=key), ("accept-a", "accept-b")))
    assert tasks[0].id == tasks[1].id
    assert len(d.tasks.list_tasks()) == 1
    assert d.tasks.task_use_policy(tasks[0].id) == "local_only"
    assert d.proposals.get(proposal.id).status == ProposalStatus.ACCEPTED
    assert d.goals.get(goal.id).status == GoalStatus.ACTIVE
    assert d.proposals.accept(proposal.id, 1, command_id="accept-a").id == tasks[0].id
    with pytest.raises(ProposalConflict):
        d.proposals.accept(proposal.id, 2, command_id="accept-a")
    with d.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM submission").fetchone()[0] == 1
        assert not any("grant" in row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'"))


def test_accept_backpressure_rolls_back_then_retries(tmp_path):
    d = Domain(tmp_path, capacity=1)
    try:
        d.rules.poll()
        d.bound_timer()
        d.due()
        d.rules.poll()
        proposal, = d.proposals.list()
        busy = d.tasks.submit("Existing queue item", command_id="busy")
        with pytest.raises(QueueFull):
            d.proposals.accept(proposal.id, 1, command_id="accept")
        assert d.proposals.get(proposal.id).status == ProposalStatus.PENDING
        assert len(d.tasks.list_tasks()) == 1
        d.tasks.request_cancel(busy.id)
        accepted = d.proposals.accept(proposal.id, 1, command_id="accept")
        assert accepted.id != busy.id
    finally:
        d.close()


def test_calendar_full_current_fingerprint_detects_title_and_deletion(domain):
    d = domain
    d.rules.poll()
    goal = d.goals.create("Goal", "Criterion")
    entry = d.schedule.create_calendar("Old title", "2026-10-15T12:00:01+08:00", "Asia/Shanghai",
                                        reminder_at="2026-10-15T12:00:01+08:00")
    d.goals.bind_source(goal.id, 1, "calendar", entry.id)
    d.due()
    d.rules.poll()
    proposal, = d.proposals.list()
    event, = d.schedule.list_events("calendar")
    before = d.schedule.source_snapshot("calendar", entry.id)
    d.schedule.update_calendar(entry.id, title="New title")
    after = d.schedule.source_snapshot("calendar", entry.id)
    assert before.occurrence_revision == after.occurrence_revision == event.occurrence_revision
    assert before.fingerprint != after.fingerprint
    with pytest.raises(ProposalConflict):
        d.proposals.accept(proposal.id, 1, command_id="stale")
    assert d.proposals.get(proposal.id).status == ProposalStatus.INVALIDATED
    d.schedule.delete_calendar(entry.id)
    assert d.schedule.source_snapshot("calendar", entry.id) is None
    assert d.schedule.list_events("calendar") == (event,)


def test_dequeue_and_step_preflight_rechecks_source_and_ttl(domain):
    d = domain
    d.rules.poll()
    d.bound_timer()
    d.due()
    d.rules.poll()
    proposal, = d.proposals.list()
    task = d.proposals.accept(proposal.id, 1, command_id="accept")
    journal = ExecutionJournal(d.store)
    journal.add_validator(d.proposals.validate_task)
    d.store.validate_external(task.id)
    d.clock.advance(86400)
    with pytest.raises(WorkflowBlocked):
        d.store.validate_external(task.id)
    # A receipt retry remains idempotent even after the work is no longer eligible.
    assert d.proposals.accept(proposal.id, 1, command_id="accept").id == task.id


def test_ttl_uses_due_instead_of_reception_time(domain):
    d = domain
    d.rules.poll()
    d.bound_timer()
    d.clock.advance(2 * 86400)
    d.schedule.poll_due()
    assert d.rules.poll() == 0
    assert d.proposals.list(status=None) == ()
    assert "expired" in str(d.rules.inbox_status())


def test_100_interleaved_events_survive_budget_and_restart(tmp_path):
    d = Domain(tmp_path)
    clock = d.clock
    d.rules.poll()
    goal = d.goals.create("Goal", "Criterion")
    for index in range(50):
        d.bound_timer(goal=goal)
        entry = d.schedule.create_calendar(str(index), "2026-10-15T12:00:01+08:00", "Asia/Shanghai",
                                           reminder_at="2026-10-15T12:00:01+08:00")
        d.goals.bind_source(goal.id, d.goals.get(goal.id).revision, "calendar", entry.id)
    d.due()
    assert d.rules.poll() == 10
    assert any(row["status"] == "Pending" for row in d.rules.inbox_status())
    d.close()
    d = Domain(tmp_path, clock=clock)
    try:
        assert sum(d.rules.poll() for _ in range(10)) == 90
        proposals = d.proposals.list(status=None)
        assert len(proposals) == len({proposal.id for proposal in proposals}) == 100
        assert d.rules.poll() == 0
        assert all(row["status"] == "Done" for row in d.rules.inbox_status())
        cursors = {row["stream"]: row["received_id"] for row in d.rules.consumer_status()}
        assert cursors["timer"] > 0 and cursors["calendar"] > 0
    finally:
        d.close()


def test_single_event_fanout_continuation_survives_restart_and_scope_cancel(tmp_path):
    d = Domain(tmp_path)
    clock = d.clock
    d.rules.poll()
    goal = d.goals.create("Goal", "Criterion")
    timer = d.schedule.create_timer(1)
    milestones = []
    for index in range(15):
        milestone = d.goals.add_milestone(goal.id, d.goals.get(goal.id).revision, str(index), "Criterion")
        milestones.append(milestone)
        d.goals.bind_source(goal.id, d.goals.get(goal.id).revision, "timer", timer.id, milestone_id=milestone.id)
    d.due()
    assert d.rules.poll() == 10
    first_ten = {item.milestone_id for item in d.proposals.list()}
    remaining = next(item for item in milestones if item.id not in first_ten)
    d.goals.set_milestone_status(remaining.id, 1, MilestoneStatus.CANCELLED)
    d.close()
    d = Domain(tmp_path, clock=clock)
    try:
        assert d.rules.poll() == 4
        proposals = d.proposals.list()
        assert len(proposals) == 14
        assert remaining.id not in {item.milestone_id for item in proposals}
        assert "scope_version_changed" in str(d.rules.inbox_status())
    finally:
        d.close()


@pytest.mark.parametrize("change", ["identity", "anchor", "rollback"])
def test_source_identity_anchor_and_rollback_stop_without_reset(domain, change):
    d = domain
    d.rules.poll()
    d.bound_timer()
    d.due()
    d.rules.poll()
    before = d.rules.consumer_status()
    # A test-only independent connection models a restored/replaced source database.
    import sqlite3
    with sqlite3.connect(d.path / "scheduling.sqlite3") as source:
        if change == "identity":
            source.execute("UPDATE scheduling_identity SET value='replacement' WHERE key='generation'")
        elif change == "anchor":
            source.execute("UPDATE events SET label='restored different anchor'")
        else:
            source.execute("DELETE FROM events")
    assert d.rules.poll() == 0
    after = d.rules.consumer_status()
    assert any(row["status"] == "NeedsReview" for row in after)
    assert [row["received_id"] for row in after] == [row["received_id"] for row in before]
    assert d.rules.poll() == 0


def test_task_completion_only_invites_user_milestone_review(domain):
    d = domain
    # Task timestamps come from the host clock; align this test's injected clock.
    d.clock.wall = datetime.now(timezone.utc)
    d.rules.poll()
    goal = d.goals.create("Goal", "Criterion", use_policy="cloud_allowed")
    milestone = d.goals.add_milestone(goal.id, 1, "Milestone", "User verifies")
    task = d.tasks.submit("Task", command_id="task", use_policy="local_only")
    d.goals.bind_source(goal.id, d.goals.get(goal.id).revision, "task", task.id,
                        milestone_id=milestone.id, use_policy="cloud_allowed")
    for state in (TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.COMPLETED):
        d.tasks.update_status(task.id, state)
    assert d.rules.poll() == 1
    proposal, = d.proposals.list()
    assert proposal.kind == "milestone_review" and proposal.use_policy == "local_only"
    assert d.goals.get_milestone(milestone.id).status == MilestoneStatus.PENDING
    assert d.goals.get(goal.id).status == GoalStatus.ACTIVE


def test_deadline_window_quiet_and_expired_summary(domain):
    d = domain
    goal = d.goals.create("Goal", "Criterion", deadline="2026-10-16T12:00:00+08:00")
    assert d.rules.poll() == 1
    proposal, = d.rules.summary()
    assert proposal.goal_id == goal.id
    d.clock.wall = datetime(2026, 10, 15, 15, tzinfo=timezone.utc)  # 23:00 Shanghai
    assert d.rules.summary() == ()
    assert len(d.proposals.list()) == 1
    d.clock.wall = datetime(2026, 10, 17, 4, tzinfo=timezone.utc)
    assert d.rules.summary() == ()
    assert d.proposals.get(proposal.id).status == ProposalStatus.EXPIRED


def test_quiet_cross_midnight_dst_and_ignore_idempotency(domain):
    d = domain
    rules = LocalRuleEngine(d.store, d.goals, d.proposals, d.schedule,
        replace(d.config, timezone="America/New_York"), clock=d.clock.now)
    # Both repeated 01:30 times during DST fall-back are quiet.
    assert rules.is_quiet(datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
    assert rules.is_quiet(datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))
    d.rules.poll()
    d.bound_timer()
    d.due()
    d.rules.poll()
    proposal, = d.proposals.list()
    ignored = d.proposals.ignore(proposal.id, 1, command_id="ignore")
    assert ignored.status == ProposalStatus.IGNORED
    assert d.proposals.ignore(proposal.id, 1, command_id="ignore") == ignored
    with pytest.raises(ProposalConflict):
        d.proposals.accept(proposal.id, 1, command_id="ignore")


def test_memory_category_cannot_create_goal_and_notes_keep_proposals(domain):
    d = domain
    d.rules.poll()
    goal, _, _ = d.bound_timer()
    d.due()
    d.rules.poll()
    proposal, = d.proposals.list()
    d.goals.revise(goal.id, d.goals.get(goal.id).revision, progress_note="Progress note")
    assert d.proposals.list() == (proposal,)
    d.goals.set_status(goal.id, d.goals.get(goal.id).revision, GoalStatus.PAUSED)
    assert d.proposals.list() == ()
    assert d.proposals.get(proposal.id).status == ProposalStatus.INVALIDATED


@pytest.mark.parametrize("window", ["accept_before_commit", "accept_after_commit", "inbox_before_commit", "proposal_before_progress_commit"])
def test_actual_subprocess_crash_preserves_atomic_accept_and_inbox(tmp_path, window):
    d = Domain(tmp_path)
    d.rules.poll()
    d.bound_timer()
    d.due()
    if window.startswith("accept"):
        d.rules.poll()
    d.close()
    # Child terminates at real product transaction boundaries, with no Python unwinding.
    script = r'''
import os, sys
from pathlib import Path
from test_v04_proactive import Domain
d = Domain(Path(sys.argv[1]))
d.clock.advance(2)
window = sys.argv[2]
if window == "accept_before_commit":
    original = d.proposals._save
    def crash(db, proposal):
        original(db, proposal)
        os._exit(71)
    d.proposals._save = crash
    proposal, = d.proposals.list()
    d.proposals.accept(proposal.id, 1, command_id="crash-accept")
elif window == "accept_after_commit":
    proposal, = d.proposals.list()
    d.proposals.accept(proposal.id, 1, command_id="crash-accept")
    os._exit(71)
elif window == "inbox_before_commit":
    original = d.rules._enqueue
    def crash(*args):
        original(*args)
        os._exit(71)
    d.rules._enqueue = crash
    d.rules.poll()
else:
    original = d.proposals.create
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(71)
    d.proposals.create = crash
    d.rules.poll()
raise RuntimeError("failure point did not execute")
'''
    env = dict(os.environ)
    root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(Path(__file__).parent)))
    child = subprocess.run([sys.executable, "-c", script, str(tmp_path), window], env=env,
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 71, child.stdout + child.stderr
    d = Domain(tmp_path)
    d.clock.advance(2)
    try:
        d.rules.poll()
        proposals = d.proposals.list(status=None)
        assert len(proposals) == 1
        proposal, = proposals
        if window == "accept_after_commit":
            assert proposal.status == ProposalStatus.ACCEPTED
            task = d.proposals.accept(proposal.id, 1, command_id="crash-accept")
            assert task.id == proposal.accepted_task_id
        else:
            assert proposal.status == ProposalStatus.PENDING
            task = d.proposals.accept(proposal.id, 1, command_id="crash-accept")
            assert task.id == d.proposals.accept(proposal.id, 1, command_id="crash-accept").id
        assert len(d.tasks.list_tasks()) == 1
        assert all(row["status"] == "Done" for row in d.rules.inbox_status())
    finally:
        d.close()


def test_explicit_consumer_review_requires_identity_and_is_idempotent(domain):
    d = domain
    d.rules.poll()
    identity = d.schedule.store_identity
    with pytest.raises(ValueError):
        d.rules.review_consumer("timer", expected_store_id="old", expected_generation=identity.generation)
    result = d.rules.review_consumer("timer", expected_store_id=identity.store_id,
        expected_generation=identity.generation, command_id="review")
    assert result["strategy"] == "tail" and result["status"] == "Active"
    assert d.rules.review_consumer("timer", expected_store_id=identity.store_id,
        expected_generation=identity.generation, command_id="review") == result
    with pytest.raises(ValueError):
        d.rules.review_consumer("calendar", expected_store_id=identity.store_id,
            expected_generation=identity.generation, command_id="review")
    with pytest.raises(ValueError):
        d.rules.review_consumer("timer", expected_store_id=identity.store_id,
            expected_generation=identity.generation, strategy="bounded_history")


def test_explicit_bounded_history_budget_survives_restart_and_does_not_skip_new_events(tmp_path):
    d = Domain(tmp_path)
    clock = d.clock
    goal = d.goals.create("Goal", "Criterion")
    for _ in range(12):
        d.bound_timer(goal=goal)
    d.due()
    d.rules.poll()  # First enable tails the old twelve events.
    identity = d.schedule.store_identity
    d.rules.review_consumer("timer", expected_store_id=identity.store_id,
        expected_generation=identity.generation, strategy="bounded_history", after_id=0,
        max_events=5, command_id="bounded")
    d.close()
    d = Domain(tmp_path, clock=clock)
    try:
        assert d.rules.poll() == 5
        assert d.rules.poll() == 0
        assert len(d.proposals.list()) == 5
        d.bound_timer(goal=d.goals.get(goal.id))
        d.due()
        assert d.rules.poll() == 1
        assert len(d.proposals.list()) == 6
    finally:
        d.close()


def test_goal_execution_change_reissues_deadline_with_new_scope(domain):
    d = domain
    goal = d.goals.create("Goal", "Criterion", deadline="2026-10-16T12:00:00+08:00")
    milestone = d.goals.add_milestone(goal.id, 1, "Milestone", "Criterion", deadline="2026-10-16T12:00:00+08:00")
    assert d.rules.poll() == 2
    revised = d.goals.revise(goal.id, d.goals.get(goal.id).revision, use_policy="cloud_allowed")
    assert d.rules.poll() == 2
    pending = d.proposals.list()
    assert len(pending) == 2
    assert all(item.goal_execution_version == revised.execution_version for item in pending)
    assert {item.milestone_id for item in pending} == {None, milestone.id}


def test_explicit_cloud_use_requires_goal_and_binding_both_to_allow(domain):
    d = domain
    d.rules.poll()
    local = d.goals.create("Local goal", "Criterion")
    timer = d.schedule.create_timer(1)
    d.goals.bind_source(local.id, 1, "timer", timer.id, use_policy="cloud_allowed")
    cloud = d.goals.create("Permitted goal", "Criterion", use_policy="cloud_allowed")
    d.goals.bind_source(cloud.id, 1, "timer", timer.id, use_policy="cloud_allowed")
    d.due()
    assert d.rules.poll() == 2
    by_goal = {item.goal_id: item for item in d.proposals.list()}
    assert by_goal[local.id].use_policy == "local_only"
    assert by_goal[cloud.id].use_policy == "cloud_allowed"
    accepted = d.proposals.accept(by_goal[cloud.id].id, 1, command_id="cloud")
    assert d.tasks.task_use_policy(accepted.id) == "cloud_allowed"
    # Changing the original source restriction invalidates the accepted intent.
    binding, = d.goals.list_bindings(cloud.id)
    d.goals.revise_binding(binding.id, 1, use_policy="local_only")
    with pytest.raises(WorkflowBlocked):
        d.proposals.validate_task(accepted.id)
