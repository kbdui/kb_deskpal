"""Profile switching uses synthetic services and explicit resource-settled signals."""

from threading import Event, get_ident

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QFileDialog, QLabel, QInputDialog, QMessageBox, QWidget

from deskpal.gui.launcher import DesktopLauncher
from deskpal.gui.maintenance import MaintenanceBridge
from deskpal.gui.profiles import ProfileWindow, profile_bridge


class ControlledBridge(QObject):
    reply = Signal(object)
    stopped = Signal()

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.calls = []
        self.closing = False
        self.busy = False

    def send(self, operation, parameters=None, *, generation=0):
        assert not self.closing and not self.busy
        self.busy = True
        request = str(len(self.calls))
        self.calls.append((request, operation, dict(parameters or {}), generation))
        return request

    def complete(self, data=None, *, ok=True):
        request, operation, _, generation = self.calls[-1]
        self.busy = False
        self.reply.emit(dict(request_id=request, operation=operation, generation=generation,
                             ok=ok, data=data or {}, error="synthetic failure"))

    def shutdown(self):
        self.closing = True


def profiles():
    return {"profiles": [{"id": "stable-a", "name": "甲", "selected": True},
                         {"id": "stable-b", "name": "乙", "selected": False}],
            "selected_id": "stable-a"}


def test_profile_selection_waits_for_worker_exit_and_ignores_late_result(qtbot):
    bridge = ControlledBridge()
    window = ProfileWindow(bridge)
    qtbot.addWidget(window)
    finished = []
    window.finished.connect(finished.append)
    window.open()
    assert bridge.calls[-1][1] == "profile_list"
    assert not window.action_buttons["start"].isEnabled()
    bridge.complete(profiles())
    window.profiles.setCurrentRow(1)
    window._start()
    assert bridge.calls[-1][1:3] == ("profile_select", {"profile_id": "stable-b"})
    assert not finished and not bridge.closing
    bridge.complete({"selected_id": "stable-b"})
    assert bridge.closing and not finished
    bridge.complete(profiles())
    assert window._closing and not window.action_buttons["start"].isEnabled()
    bridge.stopped.emit()
    assert finished == ["start"]


def test_profile_failure_keeps_recovery_controls_available(qtbot):
    bridge = ControlledBridge()
    window = ProfileWindow(bridge)
    qtbot.addWidget(window)
    window.open()
    bridge.complete(profiles())
    window._start()
    bridge.complete(ok=False)
    assert "synthetic failure" in window.status.text()
    assert window.action_buttons["maintenance"].isEnabled()
    assert window.action_buttons["start"].isEnabled()
    window._shutdown("maintenance")
    assert bridge.closing
    bridge.stopped.emit()


def test_profile_rename_and_delete_use_stable_id_and_refresh(qtbot, monkeypatch):
    bridge = ControlledBridge()
    window = ProfileWindow(bridge)
    qtbot.addWidget(window)
    window.open()
    bridge.complete(profiles())
    monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("改名", True))
    window._rename()
    assert bridge.calls[-1][1:3] == ("profile_rename", {"profile_id": "stable-a", "name": "改名"})
    bridge.complete()
    assert bridge.calls[-1][1] == "profile_list"
    bridge.complete(profiles())
    count = len(bridge.calls)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    window._delete()
    assert len(bridge.calls) == count
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window._delete()
    assert bridge.calls[-1][1:3] == ("profile_delete", {"profile_id": "stable-a"})
    bridge.complete()
    bridge.complete({"profiles": [], "selected_id": None})
    assert window.profiles.count() == 0 and not window.action_buttons["start"].isEnabled()
    window.close()
    bridge.stopped.emit()


def test_profile_service_factory_runs_off_qt_thread_and_preserves_offline(qtbot, tmp_path, monkeypatch):
    import deskpal.application.profiles as profile_service
    entered, release = Event(), Event()
    calls, replies, stopped = [], [], []
    main_thread = get_ident()

    class Service:
        def __init__(self, path, *, cancelled, provider_override):
            calls.append((path, get_ident(), provider_override, cancelled))

        def execute(self, operation, parameters):
            entered.set()
            assert release.wait(5)
            return profiles()

    monkeypatch.setattr(profile_service, "ProfileService", Service)
    path = tmp_path / "synthetic.toml"
    bridge = profile_bridge(path, provider_override="offline")
    assert isinstance(bridge, MaintenanceBridge)
    bridge.reply.connect(replies.append)
    bridge.stopped.connect(lambda: stopped.append(True))
    try:
        bridge.send("profile_list")
        qtbot.waitUntil(entered.is_set)
        bridge.shutdown()
        qtbot.wait(30)
        assert not stopped
        release.set()
        qtbot.waitUntil(lambda: bool(stopped))
        assert not replies
        assert calls[0][0] == path and calls[0][1] != main_thread
        assert calls[0][2] == "offline" and calls[0][3]() is True
    finally:
        release.set()
        bridge.shutdown()
        qtbot.waitUntil(lambda: bridge._thread is None or not bridge._thread.is_alive())


def test_backup_and_import_send_paths_only_to_worker(qtbot, tmp_path, monkeypatch):
    bridge = ControlledBridge()
    window = ProfileWindow(bridge)
    qtbot.addWidget(window)
    window.open()
    bridge.complete(profiles())
    archive = str(tmp_path / "synthetic.zip")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (archive, ""))
    window._backup()
    assert bridge.calls[-1][1:3] == ("profile_backup", {"profile_id": "stable-a", "destination": archive})
    bridge.complete({"status": "backed_up"})
    assert "备份已保存" in window.status.text()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (archive, ""))
    monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("导入角色", True))
    window._restore()
    assert bridge.calls[-1][1:3] == ("profile_restore", {"archive": archive, "name": "导入角色"})
    window.close()
    assert not bridge.closing  # An active import must settle before navigation.
    bridge.complete({"status": "restored"})
    assert bridge.closing
    bridge.stopped.emit()


def test_launcher_waits_for_host_and_profile_worker_before_new_host(qtbot, tmp_path):
    path = tmp_path / "synthetic.toml"
    path.write_text("# synthetic", encoding="utf-8")
    events = []

    class Host(QObject):
        finished = Signal()
        state_changed = Signal(str)

        def __init__(self, factory, **kwargs):
            super().__init__()
            self.bridge = self
            self.window = type("HostWindow", (QWidget,), {
                "maintenance_requested": Signal(), "profiles_requested": Signal()})()
            qtbot.addWidget(self.window)

        def start(self):
            events.append("start-host")

        def shutdown(self):
            events.append("close-host")

    class Window(QWidget):
        finished = Signal(str)

        def __init__(self, bridge):
            super().__init__()
            self.bridge = bridge
            self.status = QLabel()
            qtbot.addWidget(self)

        def open(self):
            events.append("profiles-open")

    launcher = DesktopLauncher(path, lambda: None, host_factory=Host,
        profile_bridge_factory=ControlledBridge, profile_window_factory=Window)
    launcher.start()
    old = launcher.host
    launcher.request_profiles()
    launcher.request_maintenance()  # Repeated navigation cannot replace the pending target.
    assert events == ["start-host", "close-host"]
    old.state_changed.emit("Closed")
    assert launcher.profile_window is None
    old.finished.emit()
    assert events == ["start-host", "close-host", "profiles-open"]
    assert launcher.host is None
    launcher.profile_window.finished.emit("start")
    assert events[-1] == "start-host" and launcher.profile_window is None
    # A failed new open returns to selection only after full Host settlement.
    failed = launcher.host
    failed.state_changed.emit("StartupFailed")
    assert launcher.profile_window is None
    failed.finished.emit()
    assert launcher.host is None and launcher.profile_window is not None
    assert "后台打开失败" in launcher.profile_window.status.text()
