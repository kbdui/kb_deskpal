"""Ownership and runtime-context regression checks for the production facade."""

from dataclasses import replace
from threading import Event

import pytest

from deskpal.application.factory import create_application
from deskpal.config import load_config
from deskpal.planner.models import Plan
from deskpal.tasks import TaskStatus
from deskpal.tools.models import ToolCall


@pytest.fixture
def config(tmp_path):
    return load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
        "DESKPAL_DATA_DIR": str(tmp_path / "data"), "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace")})


def test_completed_context_is_durable_without_retaining_process_history(config):
    with create_application(config) as app:
        ids = [app.run("echo synthetic bounded context").task_id for _ in range(24)]
        assert app.core._task_dependencies == {}
        assert app.core._pending_dependencies == {}
        assert app._live_pending == set()
        assert app._reviewed == {}
        assert all(app.journal.get_plan(task_id) is not None for task_id in ids)
        assert all(app.journal.attempts(task_id)[0].status == "Succeeded" for task_id in ids)


def test_noop_cleanup_preserves_live_waiting_plan_and_grants(config):
    class Planner:
        def plan(self, request, *, memories=(), tools=()):
            return Plan("synthetic", (ToolCall("write_text", {"path": "note.txt", "text": "kept"}),))

    with create_application(config, planner=Planner()) as app:
        waiting = app.run("explicit synthetic file")
        assert waiting.status == TaskStatus.WAITING
        assert app.retry_cleanup()
        assert app.tasks.get(waiting.task_id).status == TaskStatus.WAITING
        assert app.confirm_task(waiting.task_id, authorized_tools=("write_text",)).status == TaskStatus.COMPLETED


def test_startup_pending_cleanup_restarts_rules_only_after_success(config, monkeypatch):
    config = replace(config, proactive=replace(config.proactive, enabled=True, poll_interval_seconds=.05))
    with create_application(config) as app:
        memory = app.memory_command("remember", content="synthetic retained source")
        app.journal.begin_deletion(memory.id, memory.revision)

    from deskpal.memory.coordinator import MemoryCoordinator
    original = MemoryCoordinator.forget

    def unavailable(*args, **kwargs):
        raise OSError("synthetic unavailable store")

    monkeypatch.setattr(MemoryCoordinator, "forget", unavailable)
    app = create_application(config)
    try:
        assert app.diagnostics()["cleanup_pending"]
        assert app._rule_thread is None
        assert not app.diagnostics()["dispatcher"]["worker_alive"]
        monkeypatch.setattr(MemoryCoordinator, "forget", original)
        assert app.retry_cleanup()
        assert app._rule_thread.is_alive()
        assert app.diagnostics()["dispatcher"]["worker_alive"]
        assert app.run("echo after cleanup").status == TaskStatus.COMPLETED
    finally:
        assert app.close()


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), True])
def test_invalid_shutdown_timeout_does_not_begin_closing(config, timeout):
    with create_application(config) as app:
        with pytest.raises(ValueError):
            app.close(timeout=timeout)
        assert not app.diagnostics()["closing"]
        assert app.run("echo still usable").status == TaskStatus.COMPLETED


def test_slow_close_keeps_control_diagnostics_available(config, monkeypatch):
    entered, released = Event(), Event()
    app = create_application(config)
    execute = app.tools.execute

    def blocked(call, context):
        entered.set()
        assert released.wait(10)
        return execute(call, context)

    monkeypatch.setattr(app.tools, "execute", blocked)
    task = app.submit("echo synthetic slow effect")
    try:
        assert entered.wait(5)
        assert not app.close(timeout=.01)
        assert app.diagnostics()["closing"]
        assert app.cancel_task(task.id).cancel_requested
    finally:
        released.set()
        assert app.close(timeout=5)


def test_control_receipt_during_pending_deletion_does_not_expose_body(config, monkeypatch):
    marker = "SYNTHETIC_CONTROL_PRIVATE_MARKER"
    with create_application(config) as app:
        memory = app.memory_command("remember", content=marker)
        result = app.run("echo " + marker)
        runtime = app.core.memory_runtime
        original = runtime.forget

        def unavailable(*args, **kwargs):
            raise OSError("synthetic unavailable memory")

        monkeypatch.setattr(runtime, "forget", unavailable)
        pending = app.memory_command("forget", memory_id=memory.id, expected_revision=memory.revision)
        assert pending["status"] == "Pending"
        assert marker not in repr(app.cancel_task(result.task_id))
        monkeypatch.setattr(runtime, "forget", original)
        assert app.retry_cleanup()


def test_empty_idempotency_key_is_rejected_without_submission(config):
    with create_application(config) as app:
        with pytest.raises(ValueError):
            app.submit("synthetic", command_id="")
        assert app.tasks.list_tasks() == ()


def test_blocked_scheduler_close_retains_connection_and_directory_lock(config, monkeypatch):
    from time import monotonic
    from deskpal.application.instance import InstanceLockError

    entered, released = Event(), Event()
    app = create_application(config)
    original = app.scheduling._poll_due_locked

    def blocked_poll():
        entered.set()
        assert released.wait(10)
        return original()

    monkeypatch.setattr(app.scheduling, "_poll_due_locked", blocked_poll)
    app.scheduling._wake.set()
    try:
        assert entered.wait(5)
        started = monotonic()
        assert app.close(timeout=.02) is False
        assert monotonic() - started < 1
        assert not app.scheduling._closed
        assert not app.core._closed
        assert app.diagnostics()["closing"]
        with pytest.raises(InstanceLockError):
            create_application(config)
    finally:
        released.set()
        assert app.close(timeout=5)
    with create_application(config) as reopened:
        assert reopened.run("echo lock released safely").status == TaskStatus.COMPLETED


def test_blocked_rule_shutdown_stops_dispatch_claims_first(config, monkeypatch):
    config = replace(config, proactive=replace(config.proactive, enabled=True, poll_interval_seconds=.01))
    entered, released = Event(), Event()
    app = create_application(config)

    def blocked_poll():
        entered.set()
        assert released.wait(10)
        return 0

    monkeypatch.setattr(app.rules, "poll", blocked_poll)
    try:
        assert entered.wait(5)
        assert app.close(timeout=.02) is False
        assert app.diagnostics()["dispatcher"]["stopping"]
        assert not app.core._closed
    finally:
        released.set()
        assert app.close(timeout=5)
