"""Real Qt event loop + controlled background calls; no network or user storage."""

from threading import Event, current_thread

from PySide6.QtCore import QTimer
import pytest

from deskpal.gui.bridge import ApplicationBridge, MAX_PENDING


class FakeApplication:
    def __init__(self):
        self.closed = False
        self.close_allowed = True
        self.close_attempts = 0

    def diagnostics(self):
        return {"cleanup_pending": False, "closed": self.closed}

    def close(self, timeout=None):
        self.close_attempts += 1
        if self.close_allowed:
            self.closed = True
        return self.closed

    def begin_shutdown(self):
        pass


class FakeFacade:
    def __init__(self, app):
        self.app = app
        self.entered = Event()
        self.release = Event()
        self.block = None
        self.calls = []

    def execute(self, operation, parameters):
        self.calls.append((operation, current_thread().name))
        if operation == self.block:
            self.entered.set()
            assert self.release.wait(8)
        if operation == "fail":
            raise ValueError("sk-this-must-never-be-shown")
        return {"operation": operation, "parameters": dict(parameters)}


def running(qtbot):
    app = FakeApplication()
    facade = FakeFacade(app)
    bridge = ApplicationBridge(lambda: app, facade_factory=lambda _: facade)
    results = []
    bridge.reply.connect(results.append)
    bridge.start()
    qtbot.waitUntil(lambda: bridge.state == "Ready")
    return app, facade, bridge, results


def finish(qtbot, bridge, facade=None):
    if facade:
        facade.release.set()
    bridge.shutdown()
    qtbot.waitUntil(lambda: bridge.state == "Closed", timeout=5000)


def test_blocked_startup_can_close_without_losing_late_application(qtbot):
    app = FakeApplication()
    entered, release = Event(), Event()

    def factory():
        entered.set()
        assert release.wait(5)
        return app

    bridge = ApplicationBridge(factory, facade_factory=FakeFacade)
    ticks = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    bridge.start()
    try:
        qtbot.waitUntil(entered.is_set)
        bridge.shutdown()
        qtbot.waitUntil(lambda: len(ticks) >= 5)
        assert bridge.state == "Closing"
        assert not app.closed
    finally:
        release.set()
        qtbot.waitUntil(lambda: bridge.state == "Closed")
        timer.stop()
    assert app.closed


def test_command_wait_does_not_block_cancel_or_qt_timer(qtbot):
    app, facade, bridge, results = running(qtbot)
    facade.block = "long_operation"
    try:
        bridge.send("long_operation")
        qtbot.waitUntil(facade.entered.is_set)
        bridge.send("cancel", {"task_id": "synthetic"})
        qtbot.waitUntil(lambda: any(r["operation"] == "cancel" for r in results))
        assert not facade.release.is_set()
        assert ("cancel", "deskpal-gui-control") in facade.calls
    finally:
        finish(qtbot, bridge, facade)
    assert app.closed


@pytest.mark.parametrize("forget_operation", ["memory", "request_forget"])
def test_privacy_barrier_drops_old_query_and_copies_parameters(qtbot, forget_operation):
    _, facade, bridge, results = running(qtbot)
    facade.block = "task"
    values = {"nested": {"value": "original"}}
    try:
        old = bridge.send("task", values)
        qtbot.waitUntil(facade.entered.is_set)
        values["nested"]["value"] = "mutated"
        parameters = {"memory_id": "synthetic", "expected_revision": 1}
        if forget_operation == "memory":
            parameters["action"] = "forget"
        bridge.send(forget_operation, parameters)
        qtbot.waitUntil(lambda: any(r["operation"] == forget_operation for r in results))
        assert bridge.privacy_epoch == 1
        facade.release.set()
        qtbot.waitUntil(lambda: bridge.pending_count == 0)
        assert not any(r["request_id"] == old for r in results)
        assert all(r["privacy_epoch"] == 1 for r in results)
    finally:
        finish(qtbot, bridge, facade)


def test_backpressure_and_query_coalescing(qtbot):
    _, facade, bridge, results = running(qtbot)
    facade.block = "task"
    try:
        first = bridge.send("task", {"task_id": "same"})
        qtbot.waitUntil(facade.entered.is_set)
        assert bridge.send("task", {"task_id": "same"}) == first
        for index in range(MAX_PENDING + 5):
            bridge.send("task", {"task_id": str(index)})
        assert bridge.pending_count <= MAX_PENDING
        qtbot.waitUntil(lambda: any(r.get("error", {}).get("code") == "QueueFull" for r in results))
    finally:
        finish(qtbot, bridge, facade)


def test_close_timeout_keeps_closing_and_diagnostics_available(qtbot):
    app, facade, bridge, results = running(qtbot)
    app.close_allowed = False
    try:
        bridge.shutdown()
        qtbot.waitUntil(lambda: app.close_attempts > 0)
        assert bridge.state == "Closing"
        assert not app.closed
        bridge.send("diagnostics")
        qtbot.waitUntil(lambda: any(r["operation"] == "diagnostics" for r in results))
        assert bridge.state == "Closing"
    finally:
        app.close_allowed = True
        finish(qtbot, bridge, facade)


def test_error_does_not_echo_sensitive_exception(qtbot):
    _, facade, bridge, results = running(qtbot)
    try:
        bridge.send("fail")
        qtbot.waitUntil(lambda: bool(results))
        assert not results[0]["ok"]
        assert results[0]["error"]["code"] == "Rejected"
        assert "sk-this" not in str(results)
    finally:
        finish(qtbot, bridge, facade)


def test_delivery_backpressure_without_gui_consuming_callbacks(qtbot):
    _, facade, bridge, results = running(qtbot)
    facade.block = "task"
    try:
        bridge.send("task", {"task_id": "blocking"})
        qtbot.waitUntil(facade.entered.is_set)
        rejected_synchronously = False
        # The Qt loop deliberately does not drain replies during this burst.
        for index in range(10000):
            try:
                bridge.send("task", {"task_id": str(index)})
            except BufferError:
                rejected_synchronously = True
                break
        assert rejected_synchronously
        assert bridge.pending_count <= MAX_PENDING
        assert len(bridge._outbox) <= MAX_PENDING * 3
        assert bridge._outbox_bytes <= 8 * 1024 * 1024
    finally:
        finish(qtbot, bridge, facade)


def test_initializing_diagnostics_do_not_wait_for_factory(qtbot):
    app = FakeApplication()
    release = Event()

    def factory():
        assert release.wait(5)
        return app

    bridge = ApplicationBridge(factory, facade_factory=FakeFacade)
    results = []
    bridge.reply.connect(results.append)
    bridge.start()
    try:
        bridge.send("diagnostics")
        qtbot.waitUntil(lambda: bool(results))
        assert results[0]["ok"]
        assert results[0]["data"]["lifecycle"] == "Initializing"
        assert not release.is_set()
    finally:
        release.set()
        finish(qtbot, bridge)
