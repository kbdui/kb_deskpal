"""UTF-8 CLI behavior in isolated subprocesses and the console main entry."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from deskpal.application.cli import main


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def run_cli(tmp_path):
    # Inspect names before selecting values; never select actual OpenAI/secret vars.
    env = {name: os.environ[name] for name in os.environ
           if not name.upper().startswith(("OPENAI_", "DESKPAL_"))
           and not any(marker in name.upper() for marker in ("API_KEY", "SECRET", "TOKEN"))}
    env.update({"PYTHONPATH": str(PROJECT_ROOT / "src"), "PYTHONIOENCODING": "utf-8",
                "DESKPAL_LLM_PROVIDER": "offline", "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
                "DESKPAL_DATA_DIR": str(tmp_path / "data")})

    def run(*arguments, input_text=None, use_config_paths=False):
        selected_env = dict(env)
        if use_config_paths:
            selected_env.pop("DESKPAL_WORKSPACE_DIR")
            selected_env.pop("DESKPAL_DATA_DIR")
        return subprocess.run([sys.executable, "-m", "deskpal", *arguments], cwd=tmp_path, env=selected_env,
                              input=input_text, capture_output=True, encoding="utf-8", timeout=15, check=False)

    return run


def test_module_cli_acceptance_and_utf8_json_result(run_cli):
    completed = run_cli("--request", "帮我执行一个任务", "--json")
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert set(result) == {"task_id", "status", "response", "tool_results", "required_authorizations", "error",
                           "required_high_risk_authorizations", "authorization_requests", "planned_calls"}
    assert result["status"] == "Completed"
    assert result["tool_results"][0]["output"] == "离线演示任务已执行"
    assert "离线演示" in result["response"]


def test_cli_waiting_authorized_write_read_and_repeat_authorize(run_cli, tmp_path):
    denied = run_cli("--request", "write note.txt 中文", "--json")
    assert denied.returncode == 2
    assert json.loads(denied.stdout)["required_authorizations"] == ["write_text"]
    assert not (tmp_path / "workspace" / "note.txt").exists()
    completed = run_cli("--request", "write note.txt 中文", "--authorize", "write_text",
                        "--authorize", "echo", "--json")
    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "workspace" / "note.txt").read_text(encoding="utf-8") == "中文"
    read = run_cli("--request", "read note.txt", "--json")
    assert read.returncode == 0
    assert json.loads(read.stdout)["tool_results"][0]["output"] == "中文"
    again = run_cli("--request", "write another.txt 中文", "--json")
    assert again.returncode == 2
    assert not (tmp_path / "workspace" / "another.txt").exists()


def test_cli_failed_exit_code_and_json_error(run_cli):
    result = run_cli("--request", "read missing.txt", "--json")
    assert result.returncode == 1
    output = json.loads(result.stdout)
    assert output["status"] == "Failed"
    assert output["error"]
    assert output["tool_results"][0]["success"] is False


def test_cli_lists_trusted_tools_in_json(run_cli):
    result = run_cli("--list-tools", "--json")
    assert result.returncode == 0, result.stderr
    specs = {spec["name"]: spec for spec in json.loads(result.stdout)}
    assert set(specs) == {"echo", "time", "read_text", "write_text", "calendar_create", "calendar_list",
                          "calendar_update", "calendar_delete", "calendar_events", "timer_create", "timer_list",
                          "timer_cancel", "timer_events", "document_search", "document_classify", "document_copy", "document_move",
                          "document_delete", "screenshot_capture"}
    assert specs["write_text"]["permission_level"] == 1
    assert specs["document_delete"]["permission_level"] == 2


@pytest.mark.parametrize("arguments", [("--request", "echo x", "--list-tools"),
                                       ("--authorize", "write_text"),
                                       ("--list-tools", "--authorize", "write_text")])
def test_cli_rejects_ambiguous_or_persistent_authorization_modes(run_cli, arguments, tmp_path):
    result = run_cli(*arguments)
    assert result.returncode == 2
    assert result.stderr
    assert not (tmp_path / "workspace").exists()
    assert not (tmp_path / "data").exists()


def test_cli_config_relative_paths_and_invalid_configuration(run_cli, tmp_path):
    settings = tmp_path / "settings"
    settings.mkdir()
    path = settings / "config.toml"
    path.write_text('[llm]\nprovider="offline"\n[sandbox]\nworkspace_dir="files"\n'
                    '[storage]\ndata_dir="db"\n', encoding="utf-8")
    result = run_cli("--config", str(path), "--request", "write note.txt configured", "--authorize", "write_text",
                     use_config_paths=True)
    assert result.returncode == 0, result.stderr
    assert (settings / "files" / "note.txt").read_text(encoding="utf-8") == "configured"
    assert (settings / "db" / "memory.sqlite3").exists()
    path.write_text("[broken", encoding="utf-8")
    failure = run_cli("--config", str(path), "--request", "echo ignored")
    assert failure.returncode == 1
    assert "Traceback" not in failure.stderr


def test_interactive_cli_handles_eof_and_does_not_authorize_write(run_cli, tmp_path):
    result = run_cli("--json", input_text="echo 交互中文\nwrite denied.txt 内容\n/quit\n")
    assert result.returncode == 2
    outputs = [json.loads(line) for line in result.stdout.splitlines()]
    assert [item["status"] for item in outputs] == ["Completed", "Waiting"]
    assert outputs[0]["tool_results"][0]["output"] == "交互中文"
    assert not (tmp_path / "workspace" / "denied.txt").exists()
    assert run_cli(input_text="").returncode == 0


def test_console_main_entry_returns_exit_code_and_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.upper().startswith(("OPENAI_", "DESKPAL_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("DESKPAL_LLM_PROVIDER", "offline")
    monkeypatch.setenv("DESKPAL_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("DESKPAL_DATA_DIR", str(tmp_path / "data"))
    assert main(["--request", "echo 控制台", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["tool_results"][0]["output"] == "控制台"


@pytest.mark.parametrize("arguments", ['{"text":NaN}', '{"text":"one","text":"two"}', '[]', 'null', '{broken'])
def test_direct_tool_malformed_json_fails_before_storage_io(run_cli, tmp_path, arguments):
    result = run_cli("--tool", "echo", "--arguments", arguments)
    assert result.returncode == 2
    assert not (tmp_path / "workspace").exists()
    assert not (tmp_path / "data").exists()


def test_direct_tool_waiting_preview_and_high_risk_flag_are_separate(run_cli, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "delete.txt"
    target.write_text("temporary", encoding="utf-8")
    arguments = json.dumps({"path": "delete.txt"})
    denied = run_cli("--tool", "document_delete", "--arguments", arguments,
                     "--authorize", "document_delete", "--json")
    waiting = json.loads(denied.stdout)
    assert denied.returncode == 2
    assert waiting["required_high_risk_authorizations"] == ["document_delete"]
    assert waiting["planned_calls"][0]["arguments"] == {"path": "delete.txt"}
    assert waiting["authorization_requests"][0]["permission_level"] == 2
    assert target.exists()
    allowed = run_cli("--tool", "document_delete", "--arguments", arguments,
                      "--authorize-high-risk", "document_delete", "--json")
    assert allowed.returncode == 0, allowed.stderr
    assert json.loads(allowed.stdout)["status"] == "Completed"
    assert not target.exists()


def test_direct_timer_create_and_list_are_persistent_without_blocking(run_cli):
    created = run_cli("--tool", "timer_create", "--arguments", '{"duration_seconds":60,"label":"测试计时"}',
                      "--authorize", "timer_create", "--json")
    assert created.returncode == 0, created.stderr
    record = json.loads(created.stdout)["tool_results"][0]["output"]
    listed = run_cli("--tool", "timer_list", "--json")
    assert listed.returncode == 0, listed.stderr
    assert any(item["id"] == record["id"] for item in json.loads(listed.stdout)["tool_results"][0]["output"])


def _interactive_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.upper().startswith(("OPENAI_", "DESKPAL_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("DESKPAL_LLM_PROVIDER", "offline")
    monkeypatch.setenv("DESKPAL_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("DESKPAL_DATA_DIR", str(tmp_path / "data"))


def test_interactive_confirmation_requires_separate_high_risk_and_never_repeats(tmp_path, monkeypatch, capsys):
    _interactive_environment(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "delete.txt"
    target.write_text("temporary", encoding="utf-8")
    outputs, errors = [], []
    task_id = None
    step = 0

    def command(prompt):
        nonlocal step, task_id
        captured = capsys.readouterr()
        errors.append(captured.err)
        outputs.extend(json.loads(line) for line in captured.out.splitlines() if line.strip())
        step += 1
        if step == 1:
            return 'tool document_delete {"path":"delete.txt"}'
        if step == 2:
            task_id = outputs[-1]["task_id"]
            assert target.exists()
            return f"/confirm {task_id}"
        if step == 3:
            assert outputs[-1]["status"] == "Waiting"
            assert target.exists()
            return f"/confirm-high-risk {task_id}"
        if step == 4:
            assert not target.exists()
            return f"/confirm-high-risk {task_id}"
        return "/quit"

    monkeypatch.setattr("builtins.input", command)
    assert main(["--json"]) == 1
    assert [item["status"] for item in outputs] == ["Waiting", "Waiting", "Completed"]
    assert {item["task_id"] for item in outputs} == {task_id}
    assert "Unknown pending task" in "".join(errors)
    entries = [json.loads(line) for line in (tmp_path / "data" / "execution.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len([entry for entry in entries if entry["event"] == "tool_started"]) == 1


def test_interactive_cancel_removes_pending_plan_without_write(tmp_path, monkeypatch, capsys):
    _interactive_environment(tmp_path, monkeypatch)
    outputs, errors = [], []
    task_id = None
    step = 0

    def command(prompt):
        nonlocal step, task_id
        captured = capsys.readouterr()
        errors.append(captured.err)
        outputs.extend(json.loads(line) for line in captured.out.splitlines() if line.strip())
        step += 1
        if step == 1:
            return "write cancelled.txt never"
        if step == 2:
            task_id = outputs[-1]["task_id"]
            return f"/cancel {task_id}"
        if step == 3:
            return f"/confirm {task_id}"
        return "/quit"

    monkeypatch.setattr("builtins.input", command)
    assert main(["--json"]) == 1
    assert len(outputs) == 1 and outputs[0]["status"] == "Waiting"
    assert not (tmp_path / "workspace" / "cancelled.txt").exists()
    assert "Task cancelled" in "".join(errors)
    assert "Unknown pending task" in "".join(errors)
