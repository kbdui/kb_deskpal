
from tests.support.paths import REPO_ROOT
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


