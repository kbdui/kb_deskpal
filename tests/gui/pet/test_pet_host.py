"""T03: Application Closed does not dispose an outstanding image owner."""

from threading import Event

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QSystemTrayIcon

from deskpal.application.factory import create_application
from deskpal.config import AppConfig, SandboxConfig, StorageConfig
from deskpal.gui import host as host_module
from deskpal.gui.pet_assets import AssetResult, PetAssetLoader
from deskpal.gui.pet_controller import PetController
from deskpal.gui.preferences import GuiPreferences


def test_t03_host_waits_for_loader_after_bridge_closed(qtbot, tmp_path, monkeypatch):
    entered, release = Event(), Event()

    def slow(generation, cancel):
        entered.set()
        assert release.wait(6)
        return AssetResult(generation, {}, ())

    class SlowPet(PetController):
        def __init__(self, bridge, preferences, *, parent=None):
            super().__init__(bridge, preferences, loader=PetAssetLoader(load=slow), parent=parent)

    monkeypatch.setattr(host_module, "PetController", SlowPet)
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: False)
    prefs = GuiPreferences(QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat))
    prefs.set_pet_enabled(True)
    config = AppConfig(storage=StorageConfig(tmp_path / "data"),
                       sandbox=SandboxConfig(workspace_dir=tmp_path / "workspace"))
    host = host_module.DesktopHost(lambda: create_application(config), preferences=prefs,
        notification_settings=QSettings(str(tmp_path / "notifications.ini"), QSettings.Format.IniFormat),
        quit_on_closed=False)
    qtbot.addWidget(host.window)
    ticks = []
    heartbeat = QTimer(host)
    heartbeat.setInterval(20)
    heartbeat.timeout.connect(lambda: ticks.append(True))
    heartbeat.start()
    try:
        host.start(visible=False)
        qtbot.waitUntil(lambda: entered.is_set() and host.bridge.state == "Ready")
        host.bridge.shutdown()
        qtbot.waitUntil(lambda: host.bridge.state == "Closed", timeout=5000)
        assert host.pet.loader.busy and not host._finalized
        assert host.window.defer_closed and host.window.isVisible()
        count = len(ticks)
        qtbot.waitUntil(lambda: len(ticks) >= count + 3)
        assert host.window.lifecycle == "Closing"
        release.set()
        qtbot.waitUntil(lambda: host._finalized)
        assert host.window.lifecycle == "Closed"
        assert not host.pet.loader.busy
        assert not host.pet._retired_windows
    finally:
        release.set()
        host.bridge.shutdown()
        qtbot.waitUntil(lambda: host.bridge.state == "Closed" and host.pet.settled, timeout=5000)
        heartbeat.stop()
