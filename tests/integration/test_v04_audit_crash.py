"""Real process death after audit append and after local inbox reception."""

from dataclasses import replace
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys

from deskpal.config import load_config
from deskpal.proactive import LocalRuleEngine
from deskpal.tasks.models import TaskStatus


AUDIT_CHILD = r'''
import json
import os
import sys
from pathlib import Path
from threading import current_thread, main_thread
from deskpal.application.factory import create_application
from deskpal.config import load_config
from deskpal.planner.models import Plan
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolCall, ToolResult, ToolSpec

root = Path(sys.argv[1])
config = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
    "DESKPAL_DATA_DIR": str(root / "data"), "DESKPAL_WORKSPACE_DIR": str(root / "workspace")})
class Planner:
    def plan(self, request, *, memories=(), tools=()):
        return Plan("synthetic audit crash", (ToolCall("synthetic_effect", {}, "effect-0"),))
class Effect:
    spec = ToolSpec("synthetic_effect", "Synthetic fsynced effect", {"type": "object"})
    def execute(self, arguments, context):
        with (context.workspace_dir / "effect.log").open("a", encoding="utf-8") as stream:
            stream.write("effect-once\n")
            stream.flush()
            os.fsync(stream.fileno())
        return ToolResult(True, {"effect": "committed"})
app = create_application(config, planner=Planner())
app.tools.register(Effect())
original_record = app._audit_sink.record
target_event = "synthetic_completed_audit"

def append_then_die(event, task_id=None, data=None):
    # Only the explicit foreground flush may deliver. The worker's idle flush
    # fails independently of execution and cannot race to acknowledge this row.
    if current_thread() is not main_thread():
        raise OSError("synthetic temporarily unavailable audit sink")
    original_record(event, task_id, data)
    if event == target_event:
        assert app.tasks.get(task_id).status == TaskStatus.COMPLETED
        assert len(app.journal.attempts(task_id)) == 1
        assert app.journal.attempts(task_id)[0].status == "Succeeded"
        with app._audit_sink.path.open("a", encoding="utf-8") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        print("audit-appended-before-ack", flush=True)
        os._exit(79)

app._audit_sink.record = append_then_die
result = app.run("synthetic complete before audit fault")
assert result.status == TaskStatus.COMPLETED
app.store.record_event(target_event, result.task_id, {"kind": "synthetic"})
target = next(event for event in app.store.pending_audit() if event.event == target_event)
with (root / "receipt.json").open("w", encoding="utf-8") as stream:
    json.dump({"task_id": result.task_id, "event_id": target.id}, stream)
    stream.flush()
    os.fsync(stream.fileno())
for _ in range(10):
    app.flush_audit()
raise AssertionError("target audit fault not reached")
'''


INBOX_CHILD = r'''
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from deskpal.application.factory import create_application
from deskpal.config import load_config
from deskpal.proactive import LocalRuleEngine

root = Path(sys.argv[1])
config = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
    "DESKPAL_DATA_DIR": str(root / "data"), "DESKPAL_WORKSPACE_DIR": str(root / "workspace")})
app = create_application(config)
app.goals.create("Synthetic deadline", "Explicit synthetic completion criterion",
    deadline=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), command_id="deadline")
rules = LocalRuleEngine(app.store, app.goals, app.proposals, app.scheduling,
    replace(app.config.proactive, enabled=True))
def after_receive_before_process():
    with app.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM proactive_inbox WHERE status='Pending'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    print("inbox-committed-before-process", flush=True)
    os._exit(80)
rules._process = after_receive_before_process
rules.poll()
raise AssertionError("inbox fault not reached")
'''


class NeverPlanner:
    def __init__(self):
        self.calls = 0

    def plan(self, request, *, memories=(), tools=()):
        self.calls += 1
        raise AssertionError("Audit/inbox recovery must not call a planner")


def config_for(root):
    return load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
        "DESKPAL_DATA_DIR": str(root / "data"), "DESKPAL_WORKSPACE_DIR": str(root / "workspace")})


def run_child(script, root, expected_code, expected_marker):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src") + os.pathsep + env.get("PYTHONPATH", "")
    child = subprocess.run([sys.executable, "-u", "-c", script, str(root)], env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert child.returncode == expected_code, child.stdout + child.stderr
    assert expected_marker in child.stdout


def audit_rows(root):
    return [json.loads(line) for line in (root / "data" / "execution.jsonl").read_text(encoding="utf-8").splitlines()]


def test_process_death_after_jsonl_append_repeats_only_same_event_id_without_execution(tmp_path, monkeypatch):
    from deskpal.application.factory import create_application
    from deskpal.llm.providers import OfflineLLMProvider
    from deskpal.tools.manager import BuiltinToolManager

    run_child(AUDIT_CHILD, tmp_path, 79, "audit-appended-before-ack")
    receipt = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    effect = tmp_path / "workspace" / "effect.log"
    assert effect.read_text(encoding="utf-8") == "effect-once\n"
    before = [row for row in audit_rows(tmp_path) if row["event"] == "synthetic_completed_audit"]
    assert len(before) == 1 and before[0]["data"]["event_id"] == receipt["event_id"]
    with closing(sqlite3.connect(tmp_path / "data" / "workflow.sqlite3")) as db:
        assert db.execute("SELECT delivered FROM audit_outbox WHERE id=?", (receipt["event_id"],)).fetchone()[0] == 0
        assert db.execute("SELECT status FROM task WHERE id=?", (receipt["task_id"],)).fetchone()[0] == "Completed"
    planner = NeverPlanner()
    tool_calls, llm_calls = [], []
    def never_execute(*args, **kwargs):
        tool_calls.append(args)
        raise AssertionError("Audit replay must not invoke a tool")
    def never_llm(*args, **kwargs):
        llm_calls.append(args)
        raise AssertionError("Audit replay must not invoke an LLM")
    # Install before construction so startup recovery is covered as well.
    monkeypatch.setattr(BuiltinToolManager, "execute", never_execute)
    monkeypatch.setattr(OfflineLLMProvider, "chat", never_llm)
    monkeypatch.setattr(OfflineLLMProvider, "generate", never_llm)
    for _ in range(2):
        with create_application(config_for(tmp_path), planner=planner) as app:
            task = app.tasks.get(receipt["task_id"])
            assert task.status == TaskStatus.COMPLETED
            assert app.journal.attempts(task.id)[0].status == "Succeeded"
            app.flush_audit()
            assert app.store.pending_audit() == ()
            with app.store.transaction() as db:
                assert db.execute("SELECT delivered FROM audit_outbox WHERE id=?", (receipt["event_id"],)).fetchone()[0] == 1
        assert effect.read_text(encoding="utf-8") == "effect-once\n"
    repeated = [row for row in audit_rows(tmp_path) if row["event"] == "synthetic_completed_audit"]
    assert len(repeated) == 2
    assert {row["data"]["event_id"] for row in repeated} == {receipt["event_id"]}
    assert {row["task_id"] for row in repeated} == {receipt["task_id"]}
    assert planner.calls == 0 and tool_calls == [] and llm_calls == []


def test_process_death_after_inbox_reception_resumes_once_before_any_task_execution(tmp_path):
    from deskpal.application.factory import create_application

    run_child(INBOX_CHILD, tmp_path, 80, "inbox-committed-before-process")
    planner = NeverPlanner()
    with create_application(config_for(tmp_path), planner=planner) as app:
        with app.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM proactive_inbox WHERE status='Pending'").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
        rules = LocalRuleEngine(app.store, app.goals, app.proposals, app.scheduling,
            replace(app.config.proactive, enabled=True))
        assert rules.poll() == 1
        proposal = app.proposals.list()[0]
        assert rules.poll() == 0
        assert [item.id for item in app.proposals.list()] == [proposal.id]
        with app.store.transaction() as db:
            row = db.execute("SELECT status,scope_offset FROM proactive_inbox").fetchone()
            assert tuple(row) == ("Done", 1)
            assert db.execute("SELECT COUNT(*) FROM proactive_inbox").fetchone()[0] == 1
        assert app.tasks.list_tasks() == ()
    assert planner.calls == 0
