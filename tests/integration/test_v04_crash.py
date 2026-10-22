"""Real Application/Core process death at committed execution boundaries.

Every effect is synthetic and confined to pytest's temporary workspace. No model,
external API, user configuration, or user data participates in these tests.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from deskpal.config import load_config
from deskpal.tasks import TaskStatus


CHILD = r'''
import os
import sys
from pathlib import Path
from deskpal.application.factory import create_application
from deskpal.config import load_config
from deskpal.planner.models import Plan
from deskpal.tools.models import ToolCall, ToolResult, ToolSpec

root, phase = Path(sys.argv[1]), sys.argv[2]
config = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
    "DESKPAL_DATA_DIR": str(root / "data"), "DESKPAL_WORKSPACE_DIR": str(root / "workspace")})

def crash():
    print("fault:" + phase, flush=True)
    os._exit(71)

class Planner:
    def plan(self, request, *, memories=(), tools=()):
        return Plan("synthetic two-step plan", (
            ToolCall("synthetic_effect", {"index": 0}, "effect-0"),
            ToolCall("synthetic_effect", {"index": 1}, "effect-1")))

class Effect:
    spec = ToolSpec("synthetic_effect", "Append a synthetic effect in a temporary workspace",
        {"type": "object", "properties": {"index": {"type": "integer"}},
         "required": ["index"], "additionalProperties": False})
    def execute(self, arguments, context):
        with (context.workspace_dir / "effects.log").open("a", encoding="utf-8") as stream:
            stream.write(str(arguments["index"]) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if phase == "effect_before_result":
            crash()
        if phase == "cancel_unknown":
            app.cancel_task(context.task_id)
            crash()
        return ToolResult(True, output={"effect": arguments["index"]})

app = create_application(config, planner=Planner())
app.tools.register(Effect())
original_start = app.journal.start_step
original_finish = app.journal.finish_step
finished = 0

def start(task_id, index):
    if phase == "before_started":
        crash()
    attempt = original_start(task_id, index)
    if phase == "after_started":
        crash()
    return attempt

def finish(attempt_id, result):
    global finished
    original_finish(attempt_id, result)
    finished += 1
    if phase == "partial" and finished == 1:
        crash()
    if phase in {"all_results", "cancel_all_results"} and finished == 2:
        if phase == "cancel_all_results":
            task = app.tasks.list_tasks()[0]
            app.cancel_task(task.id)
        crash()

app.journal.start_step = start
app.journal.finish_step = finish
app.run("synthetic crash acceptance")
assert app.close(timeout=5)
'''


DELETION_CHILD = r'''
import os
import sys
from pathlib import Path
from deskpal.application.factory import create_application
from deskpal.config import load_config
from deskpal.planner.models import Plan
from deskpal.tools.models import ToolCall

root, phase = Path(sys.argv[1]), sys.argv[2]
marker = "SYNTHETIC_CRASH_DELETION_MARKER"
config = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
    "DESKPAL_DATA_DIR": str(root / "data"), "DESKPAL_WORKSPACE_DIR": str(root / "workspace")})

class Planner:
    def plan(self, request, *, memories=(), tools=()):
        return Plan(marker, (ToolCall("write_text", {"path": "never-written.txt", "text": marker}),))

def crash():
    print("fault:" + phase, flush=True)
    os._exit(72)

app = create_application(config, planner=Planner())
def no_audit(*args, **kwargs):
    raise OSError("synthetic sink unavailable")
app._audit_sink.record = no_audit
memory = app.memory_command("remember", content=marker)
result = app.run(marker)
assert result.status.value == "Waiting"
app.store.record_event("pending_private_copy", result.task_id, {"body": marker})
assert any(marker in repr(event.data) for event in app.store.pending_audit())

if phase == "deletion_job_committed":
    # begin_deletion is nested in the privacy-control transaction now. Crash
    # only after that outer transaction commits, otherwise no forget was accepted.
    original = app._privacy.begin
    def begin(memory_id, expected_revision, command_id):
        original(memory_id, expected_revision, command_id)
        crash()
    app._privacy.begin = begin
elif phase == "memory_deleted":
    runtime = app.core.memory_runtime
    original = runtime.forget
    def forget(memory_id, expected_revision):
        original(memory_id, expected_revision)
        crash()
    runtime.forget = forget
elif phase == "workflow_scrubbed":
    original = app.journal.scrub_memory
    def scrub(memory_id):
        original(memory_id)
        crash()
    app.journal.scrub_memory = scrub

app.memory_command("forget", memory_id=memory.id, expected_revision=memory.revision)
raise AssertionError("Fault point was not reached")
'''


class NeverPlanner:
    def __init__(self):
        self.calls = 0

    def plan(self, request, *, memories=(), tools=()):
        self.calls += 1
        raise AssertionError("Restart must not re-plan or execute historical work")


@pytest.mark.parametrize("phase,status,effects,attempt_states,cancelled", [
    ("before_started", TaskStatus.NEEDS_REVIEW, [], [], False),
    ("after_started", TaskStatus.UNCERTAIN, [], ["Uncertain"], False),
    ("effect_before_result", TaskStatus.UNCERTAIN, ["0"], ["Uncertain"], False),
    ("partial", TaskStatus.NEEDS_REVIEW, ["0"], ["Succeeded"], False),
    ("all_results", TaskStatus.COMPLETED, ["0", "1"], ["Succeeded", "Succeeded"], False),
    ("cancel_unknown", TaskStatus.UNCERTAIN, ["0"], ["Uncertain"], True),
    ("cancel_all_results", TaskStatus.CANCELLED, ["0", "1"], ["Succeeded", "Succeeded"], True),
])
def test_application_recovers_process_death_without_replaying_effects(
        tmp_path, phase, status, effects, attempt_states, cancelled):
    from deskpal.application.factory import create_application

    env = dict(os.environ)
    source = Path(__file__).resolve().parents[2] / "src"
    env["PYTHONPATH"] = str(source) + os.pathsep + env.get("PYTHONPATH", "")
    child = subprocess.run(
        [sys.executable, "-u", "-c", CHILD, str(tmp_path), phase],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=30,
    )
    assert child.returncode == 71, child.stdout + child.stderr
    assert "fault:" + phase in child.stdout
    effect_path = tmp_path / "workspace" / "effects.log"
    before = effect_path.read_text(encoding="utf-8").splitlines() if effect_path.exists() else []
    assert before == effects
    config = load_config(environ={
        "DESKPAL_LLM_PROVIDER": "offline", "DESKPAL_DATA_DIR": str(tmp_path / "data"),
        "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
    })
    planner = NeverPlanner()
    # Opening twice verifies that recovery itself is idempotent and releases the
    # process-death instance lock before any new application starts its databases.
    task_id = None
    for _ in range(2):
        with create_application(config, planner=planner) as reopened:
            tasks = reopened.tasks.list_tasks()
            assert len(tasks) == 1
            task = tasks[0]
            task_id = task.id
            assert task.status == status
            assert task.cancel_requested == cancelled
            attempts = reopened.journal.attempts(task.id)
            assert [attempt.status for attempt in attempts] == attempt_states
            assert planner.calls == 0
            if status == TaskStatus.UNCERTAIN:
                reviewed = reopened.review_uncertain(task.id, "unknown", "Synthetic effect requires manual review")
                assert reviewed.status == TaskStatus.UNCERTAIN
                with pytest.raises(ValueError):
                    reopened.resume(task.id)
            if phase == "partial":
                with pytest.raises(ValueError):
                    reopened.resume(task.id)
            if phase in {"all_results", "cancel_all_results"}:
                assert "warning" in task.result
                assert len(task.result["tool_results"]) == 2
        after = effect_path.read_text(encoding="utf-8").splitlines() if effect_path.exists() else []
        assert after == effects
    assert task_id is not None
    assert planner.calls == 0


@pytest.mark.parametrize("phase", ["deletion_job_committed", "memory_deleted", "workflow_scrubbed"])
def test_startup_finishes_crashed_deletion_before_exposing_or_republishing_copies(tmp_path, phase):
    from deskpal.application.factory import create_application

    marker = "SYNTHETIC_CRASH_DELETION_MARKER"
    env = dict(os.environ)
    source = Path(__file__).resolve().parents[2] / "src"
    env["PYTHONPATH"] = str(source) + os.pathsep + env.get("PYTHONPATH", "")
    child = subprocess.run(
        [sys.executable, "-u", "-c", DELETION_CHILD, str(tmp_path), phase],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=30,
    )
    assert child.returncode == 72, child.stdout + child.stderr
    assert "fault:" + phase in child.stdout
    config = load_config(environ={
        "DESKPAL_LLM_PROVIDER": "offline", "DESKPAL_DATA_DIR": str(tmp_path / "data"),
        "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
    })
    planner = NeverPlanner()
    for _ in range(2):
        with create_application(config, planner=planner) as app:
            assert app.journal.pending_deletions() == ()
            tasks = app.tasks.list_tasks()
            assert len(tasks) == 1
            task = tasks[0]
            assert task.request == "[forgotten]"
            assert task.result is None
            assert app.journal.get_plan(task.id) is None
            assert marker not in repr(app.review_task(task.id))
            assert marker not in repr(app.memory_command("export", include_history=True))
            assert all(marker not in repr(event.data) for event in app.store.pending_audit())
            app.flush_audit()
            logfile = config.storage.data_dir / "execution.jsonl"
            if logfile.exists():
                assert marker not in logfile.read_text(encoding="utf-8")
            assert not (config.sandbox.workspace_dir / "never-written.txt").exists()
            assert planner.calls == 0
