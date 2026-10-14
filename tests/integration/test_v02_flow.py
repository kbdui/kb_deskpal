"""Real V0.2 assembly with local SQLite, a fake clock and fake PNG backend."""

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import deskpal.application.factory as factory
from deskpal.config import load_config
from deskpal.planner import Plan
from deskpal.tasks import TaskStatus
from deskpal.tools import ToolCall
from deskpal.tools.screenshot import Monitor


TOOL_NAMES = {"echo", "time", "read_text", "write_text", "calendar_create", "calendar_list", "calendar_update",
              "calendar_delete", "calendar_events", "timer_create", "timer_list", "timer_cancel", "timer_events",
              "document_search", "document_classify", "document_copy", "document_move", "document_delete", "screenshot_capture"}


class Clock:
    def __init__(self):
        self.wall = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.elapsed = 0.0

    def now(self):
        return self.wall

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.elapsed += seconds


class StaticPlanner:
    def __init__(self, steps=()):
        self.steps = steps
        self.calls = 0

    def plan(self, request, *, memories=(), tools=()):
        self.calls += 1
        return Plan(reply="local test", steps=self.steps)


class FakeScreen:
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/gLsAAAAASUVORK5CYII=")

    def __init__(self):
        self.enumerated = 0
        self.captured = []

    def monitors(self):
        self.enumerated += 1
        return (Monitor(0, 0, 1, 1),)

    def capture(self, region):
        self.captured.append(region)
        return self.png


@pytest.fixture
def composition(tmp_path, monkeypatch):
    config = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline", "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
                                 "DESKPAL_DATA_DIR": str(tmp_path / "data"),
                                 "DESKPAL_SCHEDULING_POLL_INTERVAL_SECONDS": "60"})
    clock, services, memories, agents = Clock(), [], [], []
    close_order, state = [], {"raise_on_service_close": False}
    real_service, real_memory = factory.SchedulingService, factory.SQLiteMemoryManager

    class TrackedService(real_service):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, clock=clock, **kwargs)
            services.append(self)

        def close(self):
            close_order.append("scheduling")
            super().close()
            if state["raise_on_service_close"]:
                raise OSError("test scheduling cleanup failure")

    class TrackedMemory(real_memory):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            memories.append(self)

        def close(self):
            close_order.append("memory")
            super().close()

    monkeypatch.setattr(factory, "SchedulingService", TrackedService)
    monkeypatch.setattr(factory, "SQLiteMemoryManager", TrackedMemory)

    def create(planner=None, backend=None):
        agent = factory.create_agent(config, planner=planner, screenshot_backend=backend or FakeScreen())
        agents.append(agent)
        return agent

    yield config, create, clock, services, memories, close_order, state
    state["raise_on_service_close"] = False
    for agent in agents:
        agent.close()


def test_factory_registers_all_tools_and_closes_scheduling_before_memory(composition):
    _, create, _, services, memories, close_order, _ = composition
    core = create()
    assert {spec.name for spec in core.tools.list_tools()} == TOOL_NAMES
    assert core.run("帮我执行一个任务").status == TaskStatus.COMPLETED
    core.close()
    assert close_order == ["scheduling", "memory"]
    core.close()
    assert close_order == ["scheduling", "memory"]
    with pytest.raises(RuntimeError):
        services[0].list_timers()
    with pytest.raises(RuntimeError):
        memories[0].search("")


def test_factory_failure_during_registration_releases_both_resources(composition, monkeypatch):
    _, create, _, services, memories, close_order, _ = composition
    original = factory.BuiltinToolManager.register

    def fail_registration(manager, tool):
        if tool.spec.name == "calendar_list":
            raise OSError("test registration failure")
        return original(manager, tool)

    monkeypatch.setattr(factory.BuiltinToolManager, "register", fail_registration)
    with pytest.raises(OSError):
        create()
    assert close_order == ["scheduling", "memory"]
    with pytest.raises(RuntimeError):
        services[0].list_timers()
    with pytest.raises(RuntimeError):
        memories[0].search("")


def test_factory_failure_after_scheduler_start_releases_running_service(composition, monkeypatch):
    _, create, _, services, memories, close_order, _ = composition

    def fail_core_construction(*args, **kwargs):
        raise RuntimeError("test core constructor failure after start")

    monkeypatch.setattr(factory, "AgentCore", fail_core_construction)
    with pytest.raises(RuntimeError):
        create()
    assert close_order == ["scheduling", "memory"]
    with pytest.raises(RuntimeError):
        services[0].poll_due()
    with pytest.raises(RuntimeError):
        memories[0].search("")


def test_resource_close_error_still_closes_memory_and_permanently_seals_core(composition):
    _, create, _, services, memories, close_order, state = composition
    core = create()
    state["raise_on_service_close"] = True
    with pytest.raises(OSError):
        core.close()
    assert close_order == ["scheduling", "memory"]
    with pytest.raises(RuntimeError):
        memories[0].search("")
    with pytest.raises(RuntimeError):
        services[0].list_timers()
    with pytest.raises(RuntimeError):
        core.run("closed after cleanup error")
    core.close()


def test_overdue_calendar_and_timer_events_survive_factory_reopen_once(composition):
    _, create, clock, services, _, _, _ = composition
    planner = StaticPlanner((
        ToolCall("calendar_create", {"title": "local event", "start_at": "2030-01-01T00:00:10+00:00",
                                      "reminder_at": "2030-01-01T00:00:05+00:00", "timezone": "UTC"}, "calendar"),
        ToolCall("timer_create", {"duration_seconds": 5, "label": "local timer"}, "timer"),
    ))
    with create(planner) as core:
        waiting = core.run("create schedule")
        assert waiting.status == TaskStatus.WAITING
        assert not services[-1].list_calendar() and not services[-1].list_timers()
        created = core.confirm_task(waiting.task_id, authorized_tools=("calendar_create", "timer_create"))
        assert created.status == TaskStatus.COMPLETED
        assert len(created.tool_results) == 2
        assert planner.calls == 1
    clock.advance(30)
    with create(StaticPlanner()) as reopened:
        service = services[-1]
        service.poll_due()
        calendar = service.list_events("calendar")
        timers = service.list_events("timer")
        assert len(calendar) == len(timers) == 1
        assert calendar[0].overdue and timers[0].overdue
        assert service.list_timers()[0].status == "fired"
        ids = (calendar[0].id, timers[0].id)
        assert service.poll_due() == 0
        assert not reopened.tasks.list_tasks()
    with create() as reopened:
        services[-1].poll_due()
        assert (services[-1].list_events("calendar")[0].id, services[-1].list_events("timer")[0].id) == ids


def test_screenshot_waits_before_backend_and_returns_only_metadata(composition):
    config, create, _, _, _, _, _ = composition
    backend = FakeScreen()
    planner = StaticPlanner((ToolCall("screenshot_capture", {"path": "fake.png"}, "shot"),))
    with create(planner, backend) as core:
        waiting = core.run("fake screenshot")
        assert waiting.status == TaskStatus.WAITING
        assert backend.enumerated == 0 and not backend.captured
        assert not (config.sandbox.workspace_dir / "fake.png").exists()
        completed = core.confirm_task(waiting.task_id, authorized_tools=("screenshot_capture",))
        assert completed.status == TaskStatus.COMPLETED
        assert backend.enumerated == 1 and len(backend.captured) == 1
        assert set(completed.tool_results[0].output) == {"path", "bytes_written", "width", "height", "monitor"}
        assert (config.sandbox.workspace_dir / "fake.png").read_bytes() == backend.png
        persisted = "\n".join(record.content for record in core.memory.search(""))
    log = (config.storage.data_dir / "execution.jsonl").read_text(encoding="utf-8")
    assert base64.b64encode(backend.png).decode("ascii") not in persisted + log


def test_real_document_move_confirm_uses_original_parameters_and_high_risk_grant(composition):
    config, create, _, _, _, _, _ = composition
    planner = StaticPlanner((ToolCall("document_move", {"source": "original.txt", "destination": "moved.txt"}, "move"),))
    with create(planner) as core:
        source = config.sandbox.workspace_dir / "original.txt"
        source.write_bytes(b"exact file bytes")
        waiting = core.run("move")
        waiting.authorization_requests[0].arguments["destination"] = "changed.txt"
        ordinary = core.confirm_task(waiting.task_id, authorized_tools=("document_move",))
        assert ordinary.status == TaskStatus.WAITING
        assert source.exists()
        result = core.confirm_task(waiting.task_id, high_risk_authorized_tools=("document_move",))
        assert result.status == TaskStatus.COMPLETED
        assert result.task_id == waiting.task_id and planner.calls == 1
        assert not source.exists()
        assert (config.sandbox.workspace_dir / "moved.txt").read_bytes() == b"exact file bytes"
        assert not (config.sandbox.workspace_dir / "changed.txt").exists()


def test_real_document_classification_exposes_metadata_without_reading_or_moving(composition, monkeypatch):
    config, create, _, _, _, _, _ = composition
    planner = StaticPlanner((ToolCall("document_classify", {}, "classification"),))
    marker = b"fake-private-content-marker\xff"
    with create(planner) as core:
        document = config.sandbox.workspace_dir / "report.PDF"
        unknown = config.sandbox.workspace_dir / "unknown.bin"
        document.write_bytes(marker)
        unknown.write_bytes(marker)
        original_open = Path.open

        def reject_content_reads(path, mode="r", *args, **kwargs):
            if path in (document, unknown) and "r" in mode:
                raise AssertionError("Classification must not read file contents")
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", reject_content_reads)
        result = core.run("classify extensions")
        assert result.status == TaskStatus.COMPLETED
        files = {item["path"]: item for item in result.tool_results[0].output["files"]}
        assert files["report.PDF"]["category"] == "document"
        assert files["unknown.bin"]["category"] == "other"
        assert set(files["report.PDF"]) == {"path", "bytes", "category"}
        assert document.exists() and unknown.exists()
        assert "fake-private-content-marker" not in "\n".join(record.content for record in core.memory.search(""))
