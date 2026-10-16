"""Production composition exercised with temporary local adapters and a real Qt loop."""

from threading import Event

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QSystemTrayIcon

from deskpal.application.factory import create_application
from deskpal.application.instance import InstanceLock, InstanceLockError
from deskpal.config import load_config
from deskpal.gui.bridge import ApplicationBridge
from deskpal.gui.host import DesktopHost
from deskpal.gui.preferences import GuiPreferences
from deskpal.planner.models import Plan
from deskpal.tools.models import ToolCall

import pytest


def make_host(qtbot, tmp_path, monkeypatch, *, planner=None):
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: False)
    config = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
        "DESKPAL_DATA_DIR": str(tmp_path / "data"), "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace")})
    prefs = GuiPreferences(QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat))
    notifications = QSettings(str(tmp_path / "notifications.ini"), QSettings.Format.IniFormat)
    host = DesktopHost(lambda: create_application(config, planner=planner), preferences=prefs,
                       notification_settings=notifications, quit_on_closed=False)
    qtbot.addWidget(host.window)
    replies = []
    host.bridge.reply.connect(replies.append)
    host.start(visible=False)
    qtbot.waitUntil(lambda: host.bridge.state == "Ready", timeout=5000)
    # The worker updates its property before emitting queued Ready callbacks.
    # Test doubles must be installed only after both GUI handlers and their
    # initial settings requests have finished, otherwise startup can call them.
    qtbot.waitUntil(lambda: host.window.lifecycle == "Ready" and host.feed_timer.isActive()
                   and host.bridge.pending_count == 0 and not host.window.requests.pending,
                   timeout=5000)
    return host, replies, config


def execute(qtbot, host, replies, operation, parameters):
    request_id = host.bridge.send(operation, parameters)
    qtbot.waitUntil(lambda: any(r["request_id"] == request_id for r in replies), timeout=5000)
    result = next(r for r in replies if r["request_id"] == request_id)
    assert result["ok"], result
    return result["data"]


def close(qtbot, host):
    host.bridge.shutdown()
    qtbot.waitUntil(lambda: host.bridge.state == "Closed", timeout=5000)
    qtbot.waitUntil(lambda: host.window.lifecycle == "Closed", timeout=5000)


def test_host_factory_waits_for_queued_ready_and_initial_settings(qtbot, tmp_path, monkeypatch):
    """A backend Ready property must not stand in for queued GUI initialization."""
    original_set_state = ApplicationBridge._set_state

    def delayed_ready_delivery(bridge, state):
        if state != "Ready":
            return original_set_state(bridge, state)
        # Enlarge the existing property-before-queued-signal gap without
        # replacing registered Qt slots or their native meta-object entries.
        with bridge._lock:
            if bridge._closing.is_set():
                return
            bridge._state = state
        Event().wait(0.3)
        with bridge._lock:
            if not bridge._closing.is_set():
                bridge.state_changed.emit(state)

    monkeypatch.setattr(ApplicationBridge, "_set_state", delayed_ready_delivery)
    host, replies, _ = make_host(qtbot, tmp_path, monkeypatch)
    try:
        assert host.window.lifecycle == "Ready"
        assert host.feed_timer.isActive()
        assert host.bridge.pending_count == 0
        assert not host.window.requests.pending
        assert any(reply["operation"] == "settings" and reply["ok"] for reply in replies)
    finally:
        close(qtbot, host)


def test_production_host_runs_offline_request_and_releases_instance(qtbot, tmp_path, monkeypatch):
    host, replies, config = make_host(qtbot, tmp_path, monkeypatch)
    try:
        receipt = execute(qtbot, host, replies, "submit",
            {"request": "帮我执行一个任务", "command_id": "host-submit", "use_policy": "local_only"})
        task_id = receipt["task_id"]
        for _ in range(50):
            data = execute(qtbot, host, replies, "task", {"task_id": task_id})
            if data["task"]["status"] == "Completed":
                break
            qtbot.wait(20)
        assert data["task"]["status"] == "Completed"
        assert len(data["attempts"]) == 1
        with pytest.raises(InstanceLockError):
            InstanceLock(config.storage.data_dir).acquire()
    finally:
        close(qtbot, host)
    lock = InstanceLock(config.storage.data_dir).acquire()
    lock.release()


def test_slow_planner_cancel_remains_responsive_and_does_not_claim_forced_stop(qtbot, tmp_path, monkeypatch):
    entered, release = Event(), Event()

    class SlowPlanner:
        def plan(self, request, *, memories=(), tools=()):
            entered.set()
            assert release.wait(8)
            return Plan("synthetic", (ToolCall("echo", {"text": "synthetic"}),))

    host, replies, _ = make_host(qtbot, tmp_path, monkeypatch, planner=SlowPlanner())
    try:
        receipt = execute(qtbot, host, replies, "submit", {"request": "slow", "command_id": "slow"})
        qtbot.waitUntil(entered.is_set)
        cancel = execute(qtbot, host, replies, "cancel", {"task_id": receipt["task_id"]})
        assert cancel["cancel_requested"]
        assert not release.is_set()
        assert cancel["status"] != "Cancelled"
        host.bridge.shutdown()
        qtbot.wait(300)
        assert host.bridge.state == "Closing"
        release.set()
    finally:
        release.set()
        close(qtbot, host)


def test_window_close_with_no_tray_never_hides_the_only_window(qtbot, tmp_path, monkeypatch):
    host, _, _ = make_host(qtbot, tmp_path, monkeypatch)
    host.preferences.set_close_to_tray(True)
    host.window.show()
    host.window.close()
    qtbot.waitUntil(lambda: host.bridge.state in {"Closing", "Closed"})
    close(qtbot, host)


def test_notification_toggle_does_not_coalesce_with_old_feed(qtbot, tmp_path, monkeypatch):
    host, _, _ = make_host(qtbot, tmp_path, monkeypatch)
    entered, release = Event(), Event()
    original = host.bridge._facade.execute
    feed_calls = []

    def execute_feed(operation, parameters):
        if operation != "notification_feed":
            return original(operation, parameters)
        feed_calls.append(dict(parameters))
        if len(feed_calls) == 1:
            entered.set()
            assert release.wait(5)
        sequence = 10 if len(feed_calls) == 1 else 20
        return {"items": [], "next_sequence": sequence, "until_sequence": sequence,
                "has_more": False, "baseline": True}

    monkeypatch.setattr(host.bridge._facade, "execute", execute_feed)
    try:
        host.preferences.set_notifications_enabled(True)
        qtbot.waitUntil(entered.is_set)
        stale_request = host._feed_request
        host.preferences.set_notifications_enabled(False)
        host.preferences.set_notifications_enabled(True)
        assert host._feed_request != stale_request
        release.set()
        qtbot.waitUntil(lambda: host._after_sequence == 20)
        assert len(feed_calls) == 2
    finally:
        release.set()
        close(qtbot, host)


def test_host_callbacks_retry_bounded_delivery_saturation(qtbot, tmp_path, monkeypatch):
    host, _, _ = make_host(qtbot, tmp_path, monkeypatch)
    host.feed_timer.stop()
    attempts = {}
    generations = []

    def saturated_send(operation, parameters=None, *, generation=0):
        attempts[operation] = attempts.get(operation, 0) + 1
        if attempts[operation] == 1:
            raise BufferError("full")
        generations.append(generation)
        return operation + "-receipt"

    monkeypatch.setattr(host.bridge, "send", saturated_send)
    try:
        host._request_settings()
        host.preferences.set_notifications_enabled(True)
        assert host._feed_request is None
        host._poll_feed()
        assert host._feed_request == "notification_feed-receipt"
        payload = {"request_id": 19, "candidates": (("task", "synthetic"),)}
        host._validate_notifications(payload)
        assert host._validation_request is None
        qtbot.waitUntil(lambda: host._validation_request == "notification_validate-receipt")
        qtbot.waitUntil(lambda: attempts.get("settings", 0) == 2)
        assert generations[-1] == host._notification_generation
    finally:
        close(qtbot, host)


def test_saturated_validation_retry_is_invalidated_by_notification_toggle(qtbot, tmp_path, monkeypatch):
    host, _, _ = make_host(qtbot, tmp_path, monkeypatch)
    host.feed_timer.stop()
    calls = []

    def saturated_send(operation, parameters=None, *, generation=0):
        calls.append(operation)
        raise BufferError("full")

    monkeypatch.setattr(host.bridge, "send", saturated_send)
    try:
        host._validate_notifications({"request_id": 7, "candidates": (("task", "synthetic"),)})
        host.preferences.set_notifications_enabled(True)
        qtbot.wait(350)
        assert calls.count("notification_validate") == 1
        assert host._validation_request is None
    finally:
        close(qtbot, host)


def test_hidden_window_restores_when_tray_disappears(qtbot, tmp_path, monkeypatch):
    host, _, _ = make_host(qtbot, tmp_path, monkeypatch)
    monkeypatch.setattr(host.tray, "should_hide_on_close", lambda: True)
    host.window.show()
    try:
        host._window_close()
        assert not host.window.isVisible()
        assert host._hidden_to_tray
        host._poll_feed()
        assert host.window.isVisible()
        assert not host._hidden_to_tray
        assert host.bridge.state == "Ready"
    finally:
        close(qtbot, host)
