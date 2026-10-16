"""Production Host responsiveness under synthetic local faults; no cloud or user data."""

from contextlib import contextmanager
import sqlite3
from threading import Event

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QSystemTrayIcon
import pytest

from deskpal.application.factory import create_application
from deskpal.application.instance import InstanceLock, InstanceLockError
from deskpal.config import load_config
from deskpal.gui.host import DesktopHost
from deskpal.gui.preferences import GuiPreferences
from deskpal.llm.errors import LLMError
from deskpal.memory.contracts import EmbeddingManifest
from deskpal.planner.models import Plan
from deskpal.tools.models import ToolCall


@contextmanager
def production_host(qtbot, tmp_path, monkeypatch, **injections):
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: False)
    config = load_config(environ={
        "DESKPAL_LLM_PROVIDER": "offline", "DESKPAL_EMBEDDING_PROVIDER": "disabled",
        "DESKPAL_DATA_DIR": str(tmp_path / "data"),
        "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
    })
    preferences = GuiPreferences(QSettings(str(tmp_path / "prefs.ini"), QSettings.IniFormat))
    settings = QSettings(str(tmp_path / "notifications.ini"), QSettings.IniFormat)
    applications = []

    def factory():
        application = create_application(config, **injections)
        applications.append(application)
        return application

    host = DesktopHost(factory, preferences=preferences, notification_settings=settings,
                       quit_on_closed=False)
    qtbot.addWidget(host.window)
    replies, ticks = [], []
    host.bridge.reply.connect(replies.append)
    heartbeat = QTimer(host)
    heartbeat.setInterval(10)
    heartbeat.timeout.connect(lambda: ticks.append(True))
    heartbeat.start()
    host.start()
    try:
        qtbot.waitUntil(lambda: host.bridge.state == "Ready", timeout=5000)
        qtbot.waitUntil(lambda: host.bridge.pending_count == 0, timeout=5000)
        yield host, applications[0], config, replies, ticks
    finally:
        host.bridge.shutdown()
        qtbot.waitUntil(lambda: host.bridge.state == "Closed", timeout=7000)
        heartbeat.stop()
        assert all(not thread.is_alive() for thread in host.bridge._threads)
        with InstanceLock(config.storage.data_dir):
            pass


def receipt(qtbot, replies, request_id):
    qtbot.waitUntil(lambda: any(item["request_id"] == request_id for item in replies), timeout=5000)
    result = next(item for item in replies if item["request_id"] == request_id)
    assert result["ok"], result
    return result["data"]


def execute(qtbot, host, replies, operation, parameters=None):
    return receipt(qtbot, replies, host.bridge.send(operation, parameters))


def assert_responsive(qtbot, host, ticks):
    before = len(ticks)
    host.window.chat_input.setFocus()
    qtbot.keyClicks(host.window.chat_input, "responsive")
    qtbot.waitUntil(lambda: len(ticks) >= before + 5, timeout=1500)
    assert "responsive" in host.window.chat_input.toPlainText()


class BlockingEmbedding:
    manifest = EmbeddingManifest("synthetic-local", 2, "fault-test-v1")

    def __init__(self):
        self.entered, self.release = Event(), Event()

    def embed_query(self, text):
        self.entered.set()
        assert self.release.wait(8), "Test did not release local embedding"
        return (1.0, 0.0)

    def embed_documents(self, texts):
        return tuple((1.0, 0.0) for _ in texts)


def test_blocked_embedding_keeps_ui_control_and_closing_truthful(qtbot, tmp_path, monkeypatch):
    embedding = BlockingEmbedding()
    with production_host(qtbot, tmp_path, monkeypatch, embedding=embedding) as values:
        host, application, config, replies, ticks = values
        try:
            task = execute(qtbot, host, replies, "submit",
                           {"request": "synthetic retrieval", "command_id": "embedding-fault"})
            qtbot.waitUntil(embedding.entered.is_set, timeout=5000)
            assert_responsive(qtbot, host, ticks)
            cancellation = execute(qtbot, host, replies, "cancel", {"task_id": task["task_id"]})
            assert cancellation["cancel_requested"]
            # Retrieval precedes Planning, so logical cancellation can already
            # be terminal. Actual worker ownership, not status alone, proves
            # that the synchronous embedding call has not been force-stopped.
            diagnostics = execute(qtbot, host, replies, "diagnostics")
            assert diagnostics["dispatcher"]["worker_alive"]
            assert diagnostics["dispatcher"]["active_task_id"] == task["task_id"]
            host.bridge.shutdown()
            before = len(ticks)
            qtbot.waitUntil(lambda: len(ticks) >= before + 5, timeout=1500)
            assert host.bridge.state == "Closing"
            assert host.window.isVisible()
            assert not application.closed
            with pytest.raises(InstanceLockError):
                InstanceLock(config.storage.data_dir).acquire()
            assert not embedding.release.is_set()
        finally:
            embedding.release.set()


class WaitingPlanner:
    def plan(self, request, *, memories=(), tools=()):
        return Plan("synthetic permission", (ToolCall("write_text", {"path": "never.txt", "text": "no grant"}),))


def test_external_sqlite_writer_keeps_pending_controls_and_close_responsive(qtbot, tmp_path, monkeypatch):
    with production_host(qtbot, tmp_path, monkeypatch, planner=WaitingPlanner()) as values:
        host, application, config, replies, ticks = values
        task = execute(qtbot, host, replies, "submit",
                       {"request": "synthetic waiting", "command_id": "sqlite-fault"})
        task_id = task["task_id"]

        def waiting():
            return application.tasks.get(task_id).status.value == "Waiting"

        qtbot.waitUntil(waiting, timeout=5000)
        qtbot.waitUntil(lambda: host.bridge.pending_count == 0, timeout=5000)
        entered = Event()
        cancel_entered = Event()
        original_cancel = application.cancel_task

        def observed_cancel(task_id):
            # Record entry only; the real service performs the cancellation.
            cancel_entered.set()
            return original_cancel(task_id)

        monkeypatch.setattr(application, "cancel_task", observed_cancel)
        # Observe actual SQLite entry, without replacing transactions or locks.
        application.store._conn.set_trace_callback(
            lambda statement: entered.set() if statement == "BEGIN IMMEDIATE" else None)
        writer = sqlite3.connect(config.storage.data_dir / "workflow.sqlite3", timeout=1)
        try:
            writer.execute("BEGIN IMMEDIATE")
            entered.clear()
            cancellation = host.bridge.send("cancel", {"task_id": task_id})
            qtbot.waitUntil(cancel_entered.is_set, timeout=2000)
            qtbot.waitUntil(entered.is_set, timeout=2000)
            diagnostics = host.bridge.send("diagnostics")
            assert_responsive(qtbot, host, ticks)
            assert not any(item["request_id"] in {cancellation, diagnostics} for item in replies)
            assert host.bridge.pending_count >= 2
            host.bridge.shutdown()
            before = len(ticks)
            qtbot.waitUntil(lambda: len(ticks) >= before + 5, timeout=1500)
            assert host.bridge.state == "Closing"
            assert host.window.isVisible()
            assert not application.closed
            with pytest.raises(InstanceLockError):
                InstanceLock(config.storage.data_dir).acquire()
        finally:
            writer.rollback()
            writer.close()
        qtbot.waitUntil(lambda: host.bridge.state == "Closed", timeout=7000)
    # The pending cancellation was a real write, not a simulated acknowledgement.
    with create_application(config) as reopened:
        assert reopened.tasks.get(task_id).status.value == "Cancelled"
        assert not (config.sandbox.workspace_dir / "never.txt").exists()


class FailingPlanner:
    def __init__(self):
        self.entered, self.release = Event(), Event()
        self.calls = 0

    def plan(self, request, *, memories=(), tools=()):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            assert self.release.wait(8), "Test did not release synthetic provider failure"
            raise LLMError("Synthetic provider unavailable")
        return Plan("Recovered synthetic planner")


def test_synthetic_provider_failure_is_visible_and_host_recovers(qtbot, tmp_path, monkeypatch):
    planner = FailingPlanner()
    with production_host(qtbot, tmp_path, monkeypatch, planner=planner) as values:
        host, application, _config, replies, ticks = values
        try:
            task = execute(qtbot, host, replies, "submit",
                           {"request": "synthetic API failure", "command_id": "provider-fault"})
            qtbot.waitUntil(planner.entered.is_set, timeout=5000)
            assert_responsive(qtbot, host, ticks)
            diagnostics = execute(qtbot, host, replies, "diagnostics")
            assert diagnostics["dispatcher"]["worker_alive"]
            planner.release.set()
            qtbot.waitUntil(lambda: application.tasks.get(task["task_id"]).status.value == "Failed", timeout=5000)
            failed = execute(qtbot, host, replies, "task", {"task_id": task["task_id"]})
            assert failed["task"]["status"] == "Failed"
            assert "Synthetic provider unavailable" in failed["task"]["error"]
            assert failed["attempts"] == []
            host.window.navigate("task", task["task_id"])
            qtbot.waitUntil(lambda: "Synthetic provider unavailable" in host.window.tasks.detail.toPlainText(),
                            timeout=5000)
            # Failure must leave the Host usable, without retrying the failed task.
            recovered = execute(qtbot, host, replies, "submit",
                                {"request": "synthetic recovery", "command_id": "provider-recovery"})
            qtbot.waitUntil(lambda: application.tasks.get(recovered["task_id"]).status.value == "Completed", timeout=5000)
            assert planner.calls == 2
            assert host.bridge.state == "Ready"
            assert_responsive(qtbot, host, ticks)
        finally:
            planner.release.set()
