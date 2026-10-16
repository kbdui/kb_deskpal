"""Synthetic, offline deletion-saga and persisted privacy regression evidence."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from deskpal.config import load_config
from deskpal.memory.contracts import MemoryDraft, MemorySource, MemoryStatus
from deskpal.planner.models import Plan
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolCall, ToolResult, ToolSpec
from deskpal.workflow import ExecutionJournal, PersistentTaskManager, WorkflowBlocked, WorkflowStore


MARKER = "SYNTHETIC_FORGOTTEN_BODY_8419"


def config_for(root):
    return load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
        "DESKPAL_DATA_DIR": str(root / "data"), "DESKPAL_WORKSPACE_DIR": str(root / "workspace")})


CHILD = r'''
import json
import os
import sys
from pathlib import Path
from deskpal.application.factory import create_application
from deskpal.config import load_config
from deskpal.planner.models import Plan
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolCall, ToolSpec

root, phase = Path(sys.argv[1]), sys.argv[2]
config = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
    "DESKPAL_DATA_DIR": str(root / "data"), "DESKPAL_WORKSPACE_DIR": str(root / "workspace")})
app = create_application(config)
marker = "SYNTHETIC_FORGOTTEN_BODY_8419"
item = app.memory_command("remember", content=marker)
task = app.tasks.create(marker)
app.tasks.update_status(task.id, TaskStatus.PLANNING)
app.journal.save_plan(task.id, Plan(marker, (ToolCall("synthetic", {"text": marker}),)),
    (ToolSpec("synthetic", "offline", {}),), str(app.core.workspace_dir), ((item.id, item.revision),))
app.tasks.update_status(task.id, TaskStatus.RUNNING)
attempt = app.journal.start_step(task.id, 0)
app.store.record_event("synthetic_pending_payload", task.id, {"text": marker})
(root / "ids.json").write_text(json.dumps({"task": task.id, "memory": item.id, "attempt": attempt.id}), encoding="utf-8")

def crash(*args, **kwargs):
    print("privacy-fault:" + phase, flush=True)
    os._exit(73)

if phase == "job_before_memory":
    app.journal.begin_deletion(item.id, item.revision)
    crash()
elif phase == "memory_before_workflow":
    app.journal.scrub_memory = crash
elif phase == "workflow_before_job_done":
    app.journal.complete_deletion = crash
app.memory_command("forget", memory_id=item.id, expected_revision=item.revision)
raise AssertionError("fault was not reached")
'''


@pytest.mark.parametrize("phase", ["job_before_memory", "memory_before_workflow", "workflow_before_job_done"])
def test_startup_resumes_cross_database_deletion_before_exposing_copies(tmp_path, phase):
    from deskpal.application.factory import create_application

    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src") + os.pathsep + env.get("PYTHONPATH", "")
    child = subprocess.run([sys.executable, "-u", "-c", CHILD, str(tmp_path), phase],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert child.returncode == 73, child.stdout + child.stderr
    ids = json.loads((tmp_path / "ids.json").read_text(encoding="utf-8"))
    with create_application(config_for(tmp_path)) as app:
        assert not app.diagnostics()["cleanup_pending"]
        assert app.journal.pending_deletions() == ()
        task = app.tasks.get(ids["task"])
        assert task.status == TaskStatus.UNCERTAIN
        assert task.request == "[forgotten]" and task.result is None
        assert app.journal.get_plan(task.id) is None
        assert app.journal.attempts(task.id)[0].result is None
        assert all(item.status == MemoryStatus.DELETED for item in app.memory.list_items(include_history=True))
        app.flush_audit()
        log = tmp_path / "data" / "execution.jsonl"
        assert MARKER not in log.read_text(encoding="utf-8")
        with pytest.raises(WorkflowBlocked):
            app.journal.finish_step(ids["attempt"], ToolResult(True, MARKER))
        with app.store.transaction() as db:
            for table, columns in (("task", "request,result,error"), ("immutableplan", "canonical_json"),
                                   ("attempt", "result,review_evidence"), ("audit_outbox", "data")):
                assert MARKER not in str(tuple(tuple(row) for row in db.execute(f"SELECT {columns} FROM {table}")))


def test_forget_shared_source_erases_all_derived_work_but_retains_independent_goal(tmp_path):
    from deskpal.application.factory import create_application

    with create_application(config_for(tmp_path)) as app:
        source = MemorySource(MARKER, id="synthetic-shared-source")
        repository = app.memory.repository
        first = repository.ingest(source, MemoryDraft(MARKER + " first", MARKER), accepted=True)
        second = repository.ingest(source, MemoryDraft(MARKER + " second", MARKER), accepted=True)
        tasks = []
        for index, item in enumerate((first, second)):
            task = app.tasks.create(MARKER + str(index))
            app.journal.set_dependencies(task.id, ((item.id, item.revision),))
            tasks.append(task)
        # This user-created object has no memory derivation link.
        goal = app.goals.create(MARKER, "explicit independent criterion", command_id="independent")
        receipt = app.memory_command("forget", memory_id=first.id, expected_revision=first.revision)
        assert set(receipt.affected_task_ids) >= {task.id for task in tasks}
        for task in tasks:
            assert app.tasks.get(task.id).request == "[forgotten]"
        assert app.goals.get(goal.id).title == MARKER
        assert not app.memory.valid(((second.id, second.revision),))
        for item in (first, second):
            with pytest.raises(WorkflowBlocked):
                app.tasks.submit("rejected derived copy", command_id=item.id, dependencies=((item.id, item.revision),))


def test_forget_removes_pending_payload_and_uncertain_evidence(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    tasks, journal = PersistentTaskManager(store), ExecutionJournal(store)
    task = tasks.create(MARKER)
    tasks.update_status(task.id, TaskStatus.PLANNING)
    journal.save_plan(task.id, Plan(MARKER, (ToolCall("synthetic", {"body": MARKER}),)),
        (ToolSpec("synthetic", "offline", {}),), "workspace", (("memory", 1),))
    tasks.update_status(task.id, TaskStatus.RUNNING)
    attempt = journal.start_step(task.id, 0)
    journal.mark_uncertain(task.id, "unknown")
    journal.review_uncertain(task.id, "unknown", MARKER)
    store.record_event("synthetic_payload", task.id, {"body": MARKER})
    job = journal.begin_deletion("memory", 1)
    journal.complete_deletion(job)
    assert tasks.get(task.id).status == TaskStatus.UNCERTAIN
    assert journal.attempts(task.id)[0].status == "Uncertain"
    assert MARKER not in str(store.pending_audit())
    with pytest.raises(WorkflowBlocked):
        journal.finish_step(attempt.id, ToolResult(True, MARKER))
    store.close()


def test_changed_external_source_blocks_all_success_recovery(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    tasks, journal = PersistentTaskManager(store), ExecutionJournal(store)
    task = tasks.create("synthetic")
    tasks.update_status(task.id, TaskStatus.PLANNING)
    journal.save_plan(task.id, Plan("reply", (ToolCall("synthetic"),)),
        (ToolSpec("synthetic", "offline", {}),), "workspace")
    tasks.update_status(task.id, TaskStatus.RUNNING)
    journal.finish_step(journal.start_step(task.id, 0).id, ToolResult(True, "effect"))
    def invalid_source(task_id, refs):
        raise WorkflowBlocked("source was edited")
    journal.add_validator(invalid_source)
    journal.recover()
    assert tasks.get(task.id).status == TaskStatus.NEEDS_REVIEW
    assert journal.attempts(task.id)[0].status == "Succeeded"
    store.close()


def test_stale_forget_revision_rejects_before_creating_permanent_job(tmp_path):
    from deskpal.application.factory import create_application

    with create_application(config_for(tmp_path)) as app:
        item = app.memory_command("remember", content="synthetic original")
        app.memory_command("correct", memory_id=item.id, expected_revision=item.revision,
                           content="synthetic updated")
        with pytest.raises(ValueError):
            app.memory_command("forget", memory_id=item.id, expected_revision=item.revision)
        assert app.journal.pending_deletions() == ()
        assert not app.diagnostics()["cleanup_pending"]


def test_failed_workflow_scrub_keeps_cleanup_block_and_no_new_sensitive_log(tmp_path):
    from deskpal.application.factory import create_application

    app = create_application(config_for(tmp_path))
    item = app.memory_command("remember", content=MARKER)
    task = app.tasks.create(MARKER)
    app.journal.set_dependencies(task.id, ((item.id, item.revision),))
    app.store.record_event("pending_private_payload", task.id, {"body": MARKER})
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic workflow scrub failure")
    app.journal.scrub_memory = fail
    receipt = app.memory_command("forget", memory_id=item.id, expected_revision=item.revision)
    assert receipt["status"] == "Pending"
    assert app.diagnostics()["cleanup_pending"]
    with pytest.raises(WorkflowBlocked):
        app.review_task(task.id)
    assert app.close()
    log = tmp_path / "data" / "execution.jsonl"
    assert not log.exists() or MARKER not in log.read_text(encoding="utf-8")
    with create_application(config_for(tmp_path)) as reopened:
        assert not reopened.diagnostics()["cleanup_pending"]
        assert reopened.tasks.get(task.id).request == "[forgotten]"
