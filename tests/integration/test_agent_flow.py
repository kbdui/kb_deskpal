"""Real offline application composition and durable local side effects."""

import json

import pytest

from deskpal.application import create_agent
from deskpal.config import load_config
from deskpal.memory import MemoryKind
from deskpal.memory.repository import SQLiteMemoryRepository
from deskpal.tasks import TaskStatus


@pytest.fixture
def config(tmp_path):
    return load_config(environ={"DESKPAL_LLM_PROVIDER": "offline", "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
                                "DESKPAL_DATA_DIR": str(tmp_path / "data")})


def test_acceptance_request_creates_plans_executes_reflects_and_audits(config):
    with create_agent(config) as core:
        result = core.run("帮我执行一个任务")
        assert result.status == TaskStatus.COMPLETED
        assert len(result.tool_results) == 1
        assert result.tool_results[0].tool_name == "echo"
        assert result.tool_results[0].output == "离线演示任务已执行"
        assert "离线演示" in result.response
        assert core.tasks.get(result.task_id).status == TaskStatus.COMPLETED
        assert any(item.draft.kind == MemoryKind.EPISODIC for item in core.memory_command("list_items"))
    entries = [json.loads(line) for line in (config.storage.data_dir / "execution.jsonl").read_text(encoding="utf-8").splitlines()]
    events = [entry for entry in entries if entry["task_id"] == result.task_id]
    states = [entry["data"]["status"] for entry in events if entry["event"] == "task_status_changed"]
    assert states == ["Created", "Planning", "Running", "Completed"]
    assert {entry["event"] for entry in events} >= {"plan_created", "permission_checked", "tool_started",
                                                    "tool_completed", "memory_saved", "request_completed"}


def test_waiting_then_authorized_write_read_and_authorization_expiry(config):
    path = config.sandbox.workspace_dir / "my note.txt"
    with create_agent(config) as core:
        waiting = core.run('write "my note.txt" 你好世界')
        assert waiting.status == TaskStatus.WAITING
        assert waiting.required_authorizations == ("write_text",)
        assert not path.exists()
        completed = core.run('write "my note.txt" 你好世界', authorized_tools=("write_text",))
        assert completed.status == TaskStatus.COMPLETED
        assert completed.task_id != waiting.task_id
        assert core.tasks.get(waiting.task_id).status == TaskStatus.WAITING
        read = core.run('read "my note.txt"')
        assert read.status == TaskStatus.COMPLETED
        assert read.tool_results[0].output == "你好世界"
        denied = core.run("write second.txt new")
        assert denied.status == TaskStatus.WAITING
        assert not (config.sandbox.workspace_dir / "second.txt").exists()
    assert path.read_bytes() == "你好世界".encode("utf-8")


def test_plain_chat_not_saved_and_explicit_fact_survives_factory_close(config):
    with create_agent(config) as core:
        assert core.run("你好").status == TaskStatus.COMPLETED
        assert not core.memory_command("list_items")
        remembered = core.run("记住：我喜欢中文")
        assert remembered.status == TaskStatus.COMPLETED
        assert core.memory.search("我喜欢中文", kind=MemoryKind.SEMANTIC)
    with SQLiteMemoryRepository(config.storage.data_dir / "memory.sqlite3") as reopened:
        items = reopened.list_items()
        assert len(items) == 1
        assert items[0].draft.content == "我喜欢中文"
        assert items[0].source.task_id == remembered.task_id
    with pytest.raises(RuntimeError):
        core.memory.list_items()


def test_file_errors_surface_failed_with_actual_tool_results_and_no_escape(config):
    with create_agent(config) as core:
        missing = core.run("read missing.txt")
        assert missing.status == TaskStatus.FAILED
        assert len(missing.tool_results) == 1
        assert missing.tool_results[0].success is False
        escape = core.run("write ../escaped.txt outside", authorized_tools=("write_text",))
        assert escape.status == TaskStatus.FAILED
        assert escape.error
    assert not (config.sandbox.workspace_dir.parent / "escaped.txt").exists()


def test_factory_close_is_idempotent_and_reopened_application_reuses_memory(config):
    first = create_agent(config)
    first.run("记住：稳定事实")
    first.close()
    first.close()
    with pytest.raises(RuntimeError):
        first.run("closed")
    with create_agent(config) as reopened:
        assert reopened.memory.search("稳定事实", kind=MemoryKind.SEMANTIC)
        assert not reopened.tasks.list_tasks()
