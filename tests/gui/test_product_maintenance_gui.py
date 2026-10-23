"""Synthetic maintenance lifecycle coverage; no SDK, personal config or databases."""

from threading import Event, get_ident

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QFileDialog, QInputDialog, QLineEdit, QLabel, QPushButton, QWidget

from deskpal.gui.launcher import DesktopLauncher
from deskpal.gui.maintenance import MaintenanceBridge, MaintenanceWindow


def status(**changes):
    result = {"values": {"provider": "offline", "timeout_seconds": 30}, "revision": "r1",
              "config_exists": True, "config_valid": True, "credential_present": False,
              "sources": {}, "issues": []}
    result.update(changes)
    return result


class ControlledBridge(QObject):
    reply = Signal(object)
    stopped = Signal()

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.calls = []
        self.busy = False
        self.closing = False

    def send(self, operation, parameters=None, *, generation=0):
        assert not self.busy and not self.closing
        self.busy = True
        request_id = str(len(self.calls))
        self.calls.append((request_id, operation, dict(parameters or {}), generation))
        return request_id

    def complete(self, data):
        request_id, operation, _, generation = self.calls[-1]
        self.busy = False
        self.reply.emit(dict(request_id=request_id, operation=operation, generation=generation,
                             ok=True, data=data))

    def shutdown(self):
        self.closing = True


def test_worker_stays_alive_until_current_stage_finishes(qtbot, tmp_path):
    entered, release = Event(), Event()
    calls, replies, stopped = [], [], []
    main_thread = get_ident()

    class Service:
        def execute(self, operation, parameters):
            calls.append((operation, get_ident()))
            entered.set()
            assert release.wait(5)
            return {"status": "ready"}

    bridge = MaintenanceBridge(tmp_path / "synthetic.toml", factory=lambda path, cancelled: Service())
    bridge.reply.connect(replies.append)
    bridge.stopped.connect(lambda: stopped.append(True))
    try:
        bridge.send("cache_verify")
        qtbot.waitUntil(entered.is_set)
        with pytest.raises(RuntimeError):
            bridge.send("cache_clear")
        bridge.shutdown()
        qtbot.wait(50)
        assert not stopped
        release.set()
        qtbot.waitUntil(lambda: bool(stopped))
        assert replies == []  # Closing suppresses even a successful late result.
        assert calls == [("cache_verify", calls[0][1])]
        assert calls[0][1] != main_thread
        assert bridge._thread is not None and not bridge._thread.is_alive()
    finally:
        release.set()
        bridge.shutdown()
        qtbot.waitUntil(lambda: bridge._thread is None or not bridge._thread.is_alive())


@pytest.mark.parametrize("provider_override", [None, "offline"])
def test_default_bridge_factory_passes_path_cancellation_and_startup_override(qtbot, tmp_path, monkeypatch,
                                                                           provider_override):
    import deskpal.maintenance.service as maintenance_service

    path = tmp_path / "synthetic.toml"
    constructed, replies = [], []

    class Service:
        def __init__(self, config_path, *, cancelled, provider_override):
            constructed.append((config_path, cancelled, provider_override))

        def execute(self, operation, parameters):
            return {"can_start": True}

    monkeypatch.setattr(maintenance_service, "MaintenanceService", Service)
    bridge = MaintenanceBridge(path, provider_override=provider_override)
    bridge.reply.connect(replies.append)
    try:
        bridge.send("setup_status")
        qtbot.waitUntil(lambda: bool(replies))
        assert len(constructed) == 1
        config_path, cancelled, override = constructed[0]
        assert config_path == path
        assert override == provider_override
        assert cancelled() is False
        assert replies[0]["ok"] is True
        bridge.shutdown()
        assert cancelled() is True
    finally:
        bridge.shutdown()
        qtbot.waitUntil(lambda: bridge._thread is None or not bridge._thread.is_alive())


def test_injected_bridge_factory_keeps_two_argument_contract_with_offline_override(qtbot, tmp_path):
    path = tmp_path / "synthetic.toml"
    constructed, replies = [], []

    class Service:
        def execute(self, operation, parameters):
            return {"can_start": True}

    def factory(config_path, cancelled):
        constructed.append((config_path, cancelled))
        return Service()

    bridge = MaintenanceBridge(path, factory=factory, provider_override="offline")
    bridge.reply.connect(replies.append)
    try:
        bridge.send("setup_status")
        qtbot.waitUntil(lambda: bool(replies))
        assert len(constructed) == 1
        assert constructed[0][0] == path
        assert constructed[0][1]() is False
        assert replies[0]["ok"] is True
    finally:
        bridge.shutdown()
        qtbot.waitUntil(lambda: bridge._thread is None or not bridge._thread.is_alive())
    assert constructed[0][1]() is True


@pytest.mark.parametrize("edited", ["model", "secret"])
def test_unsaved_field_or_secret_blocks_start_without_discarding_edits_until_refresh(qtbot, edited):
    bridge = ControlledBridge()
    window = MaintenanceWindow(bridge)
    qtbot.addWidget(window)
    window.open()
    bridge.complete(status())
    assert not window._dirty and not window._populating
    field = window.secret if edited == "secret" else window.fields[edited]
    assert isinstance(field, QLineEdit)
    field.setText("synthetic-unsaved-value")
    assert window._dirty
    assert not window.start_button.isEnabled()
    count = len(bridge.calls)
    window._start()
    assert len(bridge.calls) == count
    assert not window._starting
    assert field.text() == "synthetic-unsaved-value"
    refresh = next(button for button in window.findChildren(QPushButton)
                   if "放弃未保存编辑" in button.text())
    refresh.click()
    assert bridge.calls[-1][1] == "setup_status"
    assert field.text() == "synthetic-unsaved-value"
    bridge.complete(status())
    assert field.text() == ""
    assert not window._dirty and not window._populating
    assert window.start_button.isEnabled()
    window.close()
    bridge.stopped.emit()


def test_setup_password_is_write_only_and_stale_reply_cannot_reopen(qtbot):
    bridge = ControlledBridge()
    window = MaintenanceWindow(bridge)
    qtbot.addWidget(window)
    window.open()
    bridge.complete(status())
    assert window.start_button.isEnabled()
    window.secret.setText("synthetic-test-secret")
    window._save(False)
    assert bridge.calls[-1][2]["credential_secret"] == "synthetic-test-secret"
    assert window.secret.text() == ""
    assert "synthetic-test-secret" not in window.output.toPlainText()
    previous = bridge.calls[-1]
    window.close()
    assert bridge.closing and window.isVisible()
    bridge.complete(status())
    assert window._closing
    assert not window.start_button.isEnabled()
    bridge.reply.emit(dict(request_id=previous[0], operation="setup_save", generation=previous[3],
                           ok=True, data=status()))
    bridge.stopped.emit()
    assert not window.isVisible()


def test_cache_and_restore_require_explicit_permissions(qtbot, tmp_path):
    bridge = ControlledBridge()
    window = MaintenanceWindow(bridge)
    qtbot.addWidget(window)
    window.open()
    bridge.complete(status())
    count = len(bridge.calls)
    window._prepare()
    window._clear_cache()
    window._restore()
    window._backup()
    window._save(True)
    assert len(bridge.calls) == count
    window.rollback_confirm.setChecked(True)
    target = str(tmp_path / "synthetic-new-target")
    window.target.setText(target)
    window._restore()
    assert bridge.calls[-1][2]["acknowledge_data_rollback"] is True
    bridge.complete({"status": "restored", "target_dir": target})
    window._use_restore()
    field = window.fields["data_dir"]
    assert isinstance(field, QLineEdit) and field.text() == target
    assert not window.start_button.isEnabled()
    assert not any(call[1] == "setup_save" for call in bridge.calls)
    window.close()
    bridge.stopped.emit()


@pytest.mark.parametrize("name", ["../escape", "nested/child", "nested\\child", ".", "..",
                                      "CON", "nul.txt", "bad:name", "trailing.", " space"])
def test_new_restore_directory_rejects_non_basename(qtbot, tmp_path, monkeypatch, name):
    bridge = ControlledBridge()
    window = MaintenanceWindow(bridge)
    qtbot.addWidget(window)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(tmp_path))
    monkeypatch.setattr(QInputDialog, "getText", lambda *args: (name, True))
    window._choose_restore_target()
    assert window.target.text() == ""
    assert bridge.calls == []
    window.close()
    bridge.stopped.emit()


def test_new_restore_directory_does_not_create_and_existing_requires_resume(qtbot, tmp_path, monkeypatch):
    bridge = ControlledBridge()
    window = MaintenanceWindow(bridge)
    qtbot.addWidget(window)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(tmp_path))
    monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("new-restored-data", True))
    window._choose_restore_target()
    target = tmp_path / "new-restored-data"
    assert window.target.text() == str(target)
    assert not target.exists()
    assert not window.resume_confirm.isChecked()
    target.mkdir()
    window.target.clear()
    window._choose_restore_target()
    assert window.target.text() == ""
    window.rollback_confirm.setChecked(True)
    window.target.setText(str(target))
    window._restore()
    assert bridge.calls == []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(target))
    window._choose_resume_target()
    assert window.resume_confirm.isChecked()
    window.archive.setText("synthetic-archive.zip")
    window._restore()
    assert bridge.calls[-1][1] == "backup_restore"
    assert bridge.calls[-1][2]["resume"] is True
    assert bridge.calls[-1][2]["target_dir"] == str(target)
    window.close()
    bridge.stopped.emit()


def test_cache_evidence_and_repair_reset_are_explicit_before_action(qtbot):
    bridge = ControlledBridge()
    window = MaintenanceWindow(bridge)
    qtbot.addWidget(window)
    window.open()
    bridge.complete(status())
    hints = "\n".join(label.text() for label in window.findChildren(QLabel))
    assert "重建受支持的配置" in hints and "其他设置会重置" in hints
    assert "误填的秘密" in hints
    assert any("放弃未保存编辑" in button.text() for button in window.findChildren(QPushButton))
    window.request("cache_status")
    bridge.complete({"status": "ready", "verification": "previously_verified", "dependency_available": False})
    text = window.output.toPlainText()
    assert "verification: previously_verified" in text
    assert "dependency_available: False" in text
    assert "ready 仅表示曾成功验证" in text
    window.close()
    bridge.stopped.emit()


def test_start_waits_for_status_validation_and_worker_stop(qtbot):
    bridge = ControlledBridge()
    window = MaintenanceWindow(bridge)
    qtbot.addWidget(window)
    finished = []
    window.finished.connect(finished.append)
    window.open()
    bridge.complete(status())
    window._start()
    assert bridge.calls[-1][1] == "setup_status"
    bridge.complete(status())
    assert bridge.closing and not finished
    bridge.stopped.emit()
    assert finished == ["start"]


def test_environment_override_warns_and_pending_restore_blocks_start(qtbot):
    bridge = ControlledBridge()
    window = MaintenanceWindow(bridge)
    qtbot.addWidget(window)
    window.open()
    bridge.complete(status(can_start=True, issues=[{"code": "environment_override", "message": "环境优先"}]))
    assert window.start_button.isEnabled()
    window._start()
    bridge.complete(status(can_start=False, issues=[{"code": "restore_pending", "message": "请继续恢复"}]))
    assert not window.start_button.isEnabled()
    assert not bridge.closing
    assert "请继续恢复" in window.output.toPlainText()
    window.close()
    bridge.stopped.emit()


def test_authoritative_start_gate_can_reject_otherwise_valid_config(qtbot):
    bridge = ControlledBridge()
    window = MaintenanceWindow(bridge)
    qtbot.addWidget(window)
    window.open()
    bridge.complete(status(can_start=False))
    assert not window.start_button.isEnabled()
    window._start()
    assert len(bridge.calls) == 1
    window.close()
    bridge.stopped.emit()


@pytest.mark.parametrize("provider_override", [None, "offline"])
def test_launcher_waits_for_host_final_finish_and_maintenance_worker(qtbot, tmp_path, provider_override):
    config = tmp_path / "synthetic.toml"
    config.write_text("# synthetic", encoding="utf-8")
    events, bridges = [], []

    def bridge_factory(path, *, parent, provider_override):
        bridges.append((path, parent, provider_override))
        return ControlledBridge()

    class Host(QObject):
        finished = Signal()

        def __init__(self, factory, **kwargs):
            super().__init__()
            self.window = type("HostWindow", (QWidget,), {"maintenance_requested": Signal()})()
            qtbot.addWidget(self.window)
            self.bridge = self

        def start(self):
            events.append("host-start")

        def shutdown(self):
            events.append("host-shutdown")

    class Window(QWidget):
        finished = Signal(str)

        def __init__(self, bridge):
            super().__init__()
            self.bridge = bridge
            qtbot.addWidget(self)

        def open(self):
            events.append("maintenance-open")

    launcher = DesktopLauncher(config, lambda: None, host_factory=Host,
                               bridge_factory=bridge_factory, window_factory=Window,
                               maintenance_provider_override=provider_override)
    launcher.start()
    old_host = launcher.host
    launcher.request_maintenance()
    assert events == ["host-start", "host-shutdown"]
    assert launcher.maintenance_window is None
    old_host.finished.emit()
    assert events[-1] == "maintenance-open" and launcher.host is None
    assert bridges == [(config, launcher, provider_override)]
    launcher.maintenance_window.finished.emit("start")
    assert events[-1] == "host-start"
    assert launcher.maintenance_window is None
