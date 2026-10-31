"""Offline application-boundary coverage with temporary synthetic stores only."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Event

import pytest

from deskpal.application.desktop import DesktopFacade
from deskpal.application.factory import create_application
from deskpal.config import load_config
from deskpal.planner.models import Plan
from deskpal.presentation import PresentationSnapshot
from deskpal.tasks.models import TaskStatus
from deskpal.workflow.models import WorkflowBlocked


@pytest.fixture
def config(tmp_path):
    # Explicit environ prevents loading any real .env or process credentials.
    return load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
        "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "DESKPAL_DATA_DIR": str(tmp_path / "data")})


def test_snapshot_reads_live_status_while_only_core_worker_is_busy(config, monkeypatch):
    entered, release = Event(), Event()
    calls = []

    class BlockedPlanner:
        def plan(self, request, *, memories=(), tools=()):
            calls.append(request)
            entered.set()
            assert release.wait(5)
            return Plan("synthetic private reply", ())

    app = create_application(config, planner=BlockedPlanner())
    try:
        facade = DesktopFacade(app)
        assert PresentationSnapshot.from_payload(facade.execute("presentation", {})).status == "Idle"
        active = app.submit("synthetic private request", command_id="active")
        assert entered.wait(5)
        queued = app.submit("synthetic queued request", command_id="queued")
        monkeypatch.setattr(app.tools, "execute", lambda *_: pytest.fail("snapshot must not execute tools"))
        for _ in range(3):
            snapshot = PresentationSnapshot.from_payload(facade.execute("presentation", {}))
            assert (snapshot.task_id, snapshot.status) == (active.id, "Planning")
            assert "synthetic private" not in str(snapshot)
        assert calls == ["synthetic private request"]
        assert not app.journal.attempts(active.id) and not app.journal.attempts(queued.id)
        app.cancel_task(queued.id)
        release.set()
        assert app.wait(active.id, 5).status == TaskStatus.COMPLETED
        completed = facade.execute("presentation", {})
        assert completed["task_id"] == active.id and completed["status"] == "Completed"
        assert facade.execute("presentation", {}) == completed
        assert calls == ["synthetic private request"]
    finally:
        release.set()
        assert app.close(5)


def test_snapshot_retains_read_epoch_if_forget_finishes_before_response(config, monkeypatch):
    with create_application(config) as app:
        facade = DesktopFacade(app)
        item = facade.execute("memory", {"action": "remember", "content": "synthetic memory",
            "command_id": "remember"})
        old_epoch = facade.execute("presentation", {})["memory_epoch"]
        reading, release = Event(), Event()
        original = app.presentation_snapshot

        def paused_read():
            snapshot = original()
            reading.set()
            assert release.wait(5)
            return snapshot

        monkeypatch.setattr(app, "presentation_snapshot", paused_read)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(facade.execute, "presentation", {})
            try:
                assert reading.wait(5)
                forgotten = facade.execute("memory", {"action": "forget", "memory_id": item["id"],
                    "expected_revision": item["revision"], "command_id": "forget"})
            finally:
                release.set()
            stale = future.result(5)
        assert stale["memory_epoch"] == old_epoch < forgotten["memory_epoch"]
        fresh = facade.execute("presentation", {})
        assert fresh["memory_epoch"] == forgotten["memory_epoch"]
        assert fresh["session_id"] == stale["session_id"]
        assert fresh["sequence"] >= stale["sequence"]
        assert "synthetic memory" not in str(fresh)
        PresentationSnapshot.from_payload(fresh)


def test_snapshot_uses_cleanup_and_closing_gates_and_propagates_read_failures(config, monkeypatch):
    app = create_application(config)
    try:
        facade = DesktopFacade(app)
        with pytest.raises(ValueError, match="does not accept parameters"):
            facade.execute("presentation", {"task_id": "ignored"})
        with monkeypatch.context() as patch:
            def failed_query():
                raise sqlite3.OperationalError("synthetic read failure")
            patch.setattr(app.tasks, "presentation_snapshot", failed_query)
            with pytest.raises(sqlite3.OperationalError):
                facade.execute("presentation", {})
        item = facade.execute("memory", {"action": "remember", "content": "synthetic memory",
            "command_id": "remember"})
        original = app.core.memory_command

        def pending_forget(action, **parameters):
            if action == "forget":
                raise OSError("synthetic cleanup failure")
            return original(action, **parameters)

        with monkeypatch.context() as patch:
            patch.setattr(app.core, "memory_command", pending_forget)
            pending = facade.execute("memory", {"action": "forget", "memory_id": item["id"],
                "expected_revision": item["revision"], "command_id": "forget"})
            assert pending["status"] == "Pending"
            with pytest.raises(WorkflowBlocked):
                facade.execute("presentation", {})
        assert facade.execute("retry_cleanup", {})["complete"]
        assert facade.execute("presentation", {})["status"] == "Idle"
        app.begin_shutdown()
        with pytest.raises(RuntimeError):
            facade.execute("presentation", {})
    finally:
        assert app.close(5)
