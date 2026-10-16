"""Production workflow CLI with synthetic local stores and isolated processes."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import pytest

from deskpal.application.cli import main
from deskpal.goals import GoalManager
from deskpal.proactive import ProposalManager
from deskpal.workflow import PersistentTaskManager, WorkflowStore


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cli(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.upper().startswith(("OPENAI_", "DESKPAL_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("DESKPAL_LLM_PROVIDER", "offline")
    monkeypatch.setenv("DESKPAL_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("DESKPAL_DATA_DIR", str(tmp_path / "data"))

    def invoke(*args):
        code = main([*args, "--json"])
        captured = capsys.readouterr()
        value = json.loads(captured.out) if captured.out.strip() else None
        return code, value, captured.err
    return invoke


def success(cli, *args):
    code, value, error = cli(*args)
    assert code == 0, error
    return value


def create_goal(cli, *, command="goal-command"):
    return success(cli, "--workflow", "goal-create", "--title", "中文目标", "--criteria", "用户检查证据",
                   "--command-id", command)


def test_goal_cli_defaults_cas_idempotency_and_explicit_completion(cli):
    created = create_goal(cli)
    assert created["status"] == "Active" and created["use_policy"] == "local_only"
    assert create_goal(cli) == created
    code, _, error = cli("--workflow", "goal-create", "--title", "Different", "--criteria", "Criterion",
                         "--command-id", "goal-command")
    assert code == 1 and "Traceback" not in error
    updated = success(cli, "--workflow", "goal-update", "--id", created["id"], "--expected-revision", "1",
                      "--progress-note", "已写一页", "--command-id", "note")
    assert updated["revision"] == 2 and updated["execution_version"] == 1
    # Retrying creation returns its original successful snapshot; get returns current state.
    assert create_goal(cli)["revision"] == 1
    assert success(cli, "--workflow", "goal", "--id", created["id"])["revision"] == 2
    code, _, error = cli("--workflow", "goal-update", "--id", created["id"], "--expected-revision", "1", "--title", "Stale")
    assert code == 1 and "Traceback" not in error
    completed = success(cli, "--workflow", "goal-status", "--id", created["id"], "--expected-revision", "2",
                        "--state", "Completed", "--evidence", "用户已逐项确认")
    assert completed["completion_evidence"] == "用户已逐项确认"
    assert completed["execution_version"] == 2
    assert success(cli, "--workflow", "goals", "--state", "Completed") == [completed]


def test_milestone_binding_and_independent_scope_commands(cli):
    goal = create_goal(cli)
    milestone = success(cli, "--workflow", "milestone-create", "--goal-id", goal["id"], "--expected-revision", "1",
                        "--title", "First milestone", "--criteria", "User approved", "--command-id", "milestone")
    assert milestone["status"] == "Pending"
    binding = success(cli, "--workflow", "bind", "--goal-id", goal["id"], "--expected-revision", "2",
                      "--milestone-id", milestone["id"], "--source", "timer", "--source-id", "explicit-timer",
                      "--command-id", "binding")
    assert binding["use_policy"] == "local_only" and binding["revision"] == 1
    assert success(cli, "--workflow", "bindings", "--goal-id", goal["id"]) == [binding]
    revised = success(cli, "--workflow", "milestone-update", "--id", milestone["id"], "--expected-revision", "1",
                      "--progress-note", "Progress")
    assert revised["execution_version"] == 1 and revised["revision"] == 2
    completed = success(cli, "--workflow", "milestone-status", "--id", milestone["id"], "--expected-revision", "2",
                        "--state", "Completed", "--evidence", "Explicit human evidence")
    assert completed["status"] == "Completed"
    removed = success(cli, "--workflow", "binding-update", "--id", binding["id"], "--expected-revision", "1", "--state", "Removed")
    assert removed["status"] == "Removed"
    assert success(cli, "--workflow", "goal", "--id", goal["id"])["status"] == "Active"
    assert success(cli, "--workflow", "milestones", "--goal-id", goal["id"]) == [completed]


def seed_proposal(tmp_path, *, key="one"):
    store = WorkflowStore(tmp_path / "data" / "workflow.sqlite3")
    try:
        goals, tasks = GoalManager(store), PersistentTaskManager(store)
        manager = ProposalManager(store, goals, tasks)
        goal = goals.create("Review synthetic goal", "Only the user confirms completion")
        item = manager.create(goal.id, rule_id="goal_deadline_window", root_source_key=key,
            kind="deadline_window", due_at=datetime.now(timezone.utc).isoformat())
        return goal, item
    finally:
        store.close()


def test_proposal_accept_ignore_history_and_task_inspection(cli, tmp_path):
    goal, proposal = seed_proposal(tmp_path)
    pending = success(cli, "--workflow", "proposals")
    assert [item["id"] for item in pending] == [proposal.id]
    accepted = success(cli, "--workflow", "accept-proposal", "--id", proposal.id,
                       "--expected-revision", "1", "--command-id", "accept")
    task_id = accepted["task_id"]
    assert accepted["status"] == "Completed"
    assert success(cli, "--workflow", "accept-proposal", "--id", proposal.id,
                   "--expected-revision", "1", "--command-id", "accept")["task_id"] == task_id
    reviewed = success(cli, "--workflow", "task", "--id", task_id)
    assert reviewed["task"]["status"] == "Completed"
    assert len(success(cli, "--workflow", "tasks")) == 1
    assert success(cli, "--workflow", "goal", "--id", goal.id)["status"] == "Active"
    assert success(cli, "--workflow", "proposal", "--id", proposal.id)["accepted_task_id"] == task_id
    _, ignored_proposal = seed_proposal(tmp_path, key="two")
    ignored = success(cli, "--workflow", "ignore-proposal", "--id", ignored_proposal.id,
                      "--expected-revision", "1", "--command-id", "ignore")
    assert ignored["status"] == "Ignored"
    assert success(cli, "--workflow", "proposal", "--id", ignored_proposal.id) == ignored
    assert len(success(cli, "--workflow", "proposals", "--state", "all")) == 2
    assert success(cli, "--workflow", "ignore-proposal", "--id", ignored_proposal.id,
                   "--expected-revision", "1", "--command-id", "ignore") == ignored


def test_recovery_exact_plan_hash_and_ordinary_grants_are_not_persistent(cli, tmp_path):
    code, waiting, error = cli("--tool", "write_text", "--arguments", '{"path":"note.txt","text":"精确参数"}')
    assert code == 2, error
    task_id = waiting["task_id"]
    reviewed = success(cli, "--workflow", "task", "--id", task_id)
    assert reviewed["task"]["status"] == "NeedsReview"
    snapshot = reviewed["plan"]
    assert snapshot["plan"]["steps"][0]["arguments"] == {"path": "note.txt", "text": "精确参数"}
    resumed = success(cli, "--workflow", "resume", "--id", task_id)
    assert resumed["hash"] == snapshot["hash"]
    code, _, error = cli("--workflow", "confirm", "--id", task_id, "--plan-hash", "0" * 64, "--authorize", "write_text")
    assert code == 1 and "Traceback" not in error
    code, denied, _ = cli("--workflow", "confirm", "--id", task_id, "--plan-hash", snapshot["hash"])
    assert code == 2 and denied["required_authorizations"] == ["write_text"]
    assert not (tmp_path / "workspace" / "note.txt").exists()
    completed = success(cli, "--workflow", "confirm", "--id", task_id, "--plan-hash", snapshot["hash"], "--authorize", "write_text")
    assert completed["status"] == "Completed"
    assert (tmp_path / "workspace" / "note.txt").read_text(encoding="utf-8") == "精确参数"


def test_recovery_high_risk_confirmation_stays_separate(cli, tmp_path):
    target = tmp_path / "workspace" / "synthetic.txt"
    target.parent.mkdir()
    target.write_text("synthetic only", encoding="utf-8")
    code, waiting, _ = cli("--tool", "document_delete", "--arguments", '{"path":"synthetic.txt"}',
                            "--authorize", "document_delete")
    assert code == 2 and waiting["required_high_risk_authorizations"] == ["document_delete"]
    reviewed = success(cli, "--workflow", "task", "--id", waiting["task_id"])
    plan_hash = reviewed["plan"]["hash"]
    code, _, _ = cli("--workflow", "confirm", "--id", waiting["task_id"], "--plan-hash", plan_hash,
                      "--authorize", "document_delete")
    assert code == 2 and target.exists()
    result = success(cli, "--workflow", "confirm", "--id", waiting["task_id"], "--plan-hash", plan_hash,
                     "--authorize-high-risk", "document_delete")
    assert result["status"] == "Completed" and not target.exists()


def test_submission_command_is_durable_and_different_payload_is_rejected(cli):
    first = success(cli, "--workflow", "submit", "--text", "echo once", "--command-id", "submission")
    assert success(cli, "--workflow", "submit", "--text", "echo once", "--command-id", "submission")["task_id"] == first["task_id"]
    code, _, error = cli("--workflow", "submit", "--text", "echo different", "--command-id", "submission")
    assert code == 1 and "Traceback" not in error
    assert len(success(cli, "--workflow", "tasks")) == 1


def test_source_identity_and_explicit_consumer_review_cli(cli):
    identity = success(cli, "--workflow", "source-identity")
    assert {item["source"] for item in identity["streams"]} == {"timer", "calendar"}
    common = ("--expected-store-id", identity["identity"]["store_id"],
              "--expected-generation", identity["identity"]["generation"])
    reviewed = success(cli, "--workflow", "review-consumer", "--stream", "timer", *common, "--command-id", "tail-review")
    assert reviewed["strategy"] == "tail" and reviewed["status"] == "Active"
    assert success(cli, "--workflow", "review-consumer", "--stream", "timer", *common,
                   "--command-id", "tail-review") == reviewed
    bounded = success(cli, "--workflow", "review-consumer", "--stream", "calendar", *common,
                      "--strategy", "bounded_history", "--after-id", "0", "--max-events", "5")
    assert bounded["strategy"] == "bounded_history" and bounded["max_events"] == 5
    code, _, error = cli("--workflow", "review-consumer", "--stream", "timer", "--expected-store-id", "old-store",
                         "--expected-generation", identity["identity"]["generation"])
    assert code == 1 and "Traceback" not in error
    selected = success(cli, "--workflow", "source-identity", "--stream", "timer")
    assert len(selected["streams"]) == 1 and selected["streams"][0]["source"] == "timer"


@pytest.mark.parametrize("arguments", [
    ("--workflow", "goal-create", "--title", "No criteria"),
    ("--workflow", "goal-update", "--id", "x", "--expected-revision", "1"),
    ("--workflow", "goal-update", "--id", "x", "--expected-revision", "0", "--title", "x"),
    ("--workflow", "goal-status", "--id", "x", "--expected-revision", "1", "--state", "Completed"),
    ("--workflow", "milestone-status", "--id", "x", "--expected-revision", "1", "--state", "Active"),
    ("--workflow", "binding-update", "--id", "x", "--expected-revision", "1"),
    ("--workflow", "confirm", "--id", "x"),
    ("--workflow", "confirm", "--id", "x", "--plan-hash", "truncated"),
    ("--workflow", "tasks", "--command-id", ""),
    ("--workflow", "goals", "--limit", "101"),
    ("--stream", "timer"),
    ("--workflow", "goals", "--stream", "timer"),
    ("--workflow", "review-consumer", "--stream", "timer"),
    ("--workflow", "review-consumer", "--stream", "timer", "--expected-store-id", "s", "--expected-generation", "g", "--after-id", "0"),
    ("--workflow", "review-consumer", "--stream", "timer", "--expected-store-id", "s", "--expected-generation", "g", "--strategy", "bounded_history"),
    ("--workflow", "review-consumer", "--stream", "timer", "--expected-store-id", "s", "--expected-generation", "g", "--strategy", "bounded_history", "--after-id", "-1", "--max-events", "10"),
    ("--workflow", "review-consumer", "--stream", "timer", "--expected-store-id", "s", "--expected-generation", "g", "--strategy", "bounded_history", "--after-id", "0", "--max-events", "10001"),
    ("--workflow", "source-identity", "--strategy", "tail"),
])
def test_invalid_workflow_arguments_fail_before_factory(cli, tmp_path, monkeypatch, arguments):
    def forbidden(*args, **kwargs):
        raise AssertionError("factory called for invalid command")
    monkeypatch.setattr("deskpal.application.cli.create_application", forbidden)
    with pytest.raises(SystemExit) as error:
        cli(*arguments)
    assert error.value.code == 2
    assert not (tmp_path / "data").exists()


def test_local_management_overrides_cloud_and_never_dispatches_recovered_work(cli, tmp_path, monkeypatch):
    store = WorkflowStore(tmp_path / "data" / "workflow.sqlite3")
    try:
        task = PersistentTaskManager(store).submit("write forbidden.txt unexpected", command_id="old-queued")
    finally:
        store.close()
    monkeypatch.setenv("DESKPAL_LLM_PROVIDER", "openai")
    def forbidden(*args, **kwargs):
        raise AssertionError("cloud provider initialized during local management")
    monkeypatch.setattr("deskpal.application.factory.OpenAILLMProvider", forbidden)
    listed = success(cli, "--workflow", "tasks")
    assert listed[0]["id"] == task.id and listed[0]["status"] == "NeedsReview"
    create_goal(cli)
    success(cli, "--workflow", "source-identity")
    assert not (tmp_path / "workspace" / "forbidden.txt").exists()


def test_accepting_default_local_goal_in_cloud_mode_makes_no_provider_calls(cli, tmp_path, monkeypatch):
    goal, proposal = seed_proposal(tmp_path)
    calls = []
    class SyntheticCloud:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, *args, **kwargs):
            calls.append("chat")
            raise AssertionError("local_only text reached cloud")

        def generate(self, *args, **kwargs):
            calls.append("generate")
            raise AssertionError("local_only text reached cloud")
    monkeypatch.setattr("deskpal.application.factory.OpenAILLMProvider", SyntheticCloud)
    monkeypatch.setenv("DESKPAL_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-not-a-real-key")
    monkeypatch.setenv("DESKPAL_LLM_MODEL", "synthetic-never-called")
    code, result, error = cli("--workflow", "accept-proposal", "--id", proposal.id,
                              "--expected-revision", "1", "--command-id", "local-accept")
    assert code == 2, error
    assert result["status"] == "NeedsReview" and calls == []
    # The acceptance remains durable while cloud execution is safely blocked.
    accepted = success(cli, "--workflow", "proposal", "--id", proposal.id)
    assert accepted["status"] == "Accepted" and accepted["accepted_task_id"] == result["task_id"]
    assert success(cli, "--workflow", "goal", "--id", goal.id)["status"] == "Active"


def test_subprocess_goal_and_source_identity_are_durable_without_personal_config(tmp_path):
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("OPENAI_", "DESKPAL_"))
           and not any(marker in key.upper() for marker in ("API_KEY", "SECRET", "TOKEN"))}
    env.update({"PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8",
                "DESKPAL_LLM_PROVIDER": "offline", "DESKPAL_DATA_DIR": str(tmp_path / "db"),
                "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace")})
    def run(*args):
        completed = subprocess.run([sys.executable, "-m", "deskpal", *args], cwd=tmp_path, env=env,
                                   capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)
    goal = run("--workflow", "goal-create", "--title", "子进程目标", "--criteria", "明确证据", "--command-id", "create")
    assert run("--workflow", "goal", "--id", goal["id"]) == goal
    identity = run("--workflow", "source-identity")["identity"]
    reviewed = run("--workflow", "review-consumer", "--stream", "timer", "--expected-store-id", identity["store_id"],
                   "--expected-generation", identity["generation"], "--command-id", "review")
    current = run("--workflow", "source-identity", "--stream", "timer")
    assert current["consumers"][0]["received_id"] == reviewed["received_id"]
