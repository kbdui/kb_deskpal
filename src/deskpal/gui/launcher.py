"""Switch between maintenance and desktop only after the previous owner settles."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Slot
from PySide6.QtWidgets import QApplication

from .host import DesktopHost
from .maintenance import MaintenanceBridge, MaintenanceWindow


class DesktopLauncher(QObject):
    def __init__(self, config_path: Path, factory: Callable[[], Any], *,
                 maintenance: bool = False, host_factory: Callable[..., Any] = DesktopHost,
                 maintenance_provider_override: str | None = None,
                 bridge_factory: Callable[..., Any] = MaintenanceBridge,
                 window_factory: Callable[..., Any] = MaintenanceWindow,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config_path = config_path
        self._factory = factory
        self._host_factory = host_factory
        self._bridge_factory = bridge_factory
        self._window_factory = window_factory
        self._initial_maintenance = maintenance
        self._maintenance_provider_override = maintenance_provider_override
        self.host: Any = None
        self.maintenance_window: Any = None
        self._maintenance_pending = False

    def start(self) -> None:
        if self._initial_maintenance or not self.config_path.exists():
            self._show_maintenance()
        else:
            self._start_host()

    def _start_host(self) -> None:
        if self.host is not None or self.maintenance_window is not None:
            return
        self.host = self._host_factory(self._factory, quit_on_closed=False, parent=self)
        self.host.finished.connect(self._host_finished)
        self.host.window.maintenance_requested.connect(self.request_maintenance)
        self.host.start()

    @Slot()
    def request_maintenance(self) -> None:
        if self.host is None or self._maintenance_pending:
            return
        self._maintenance_pending = True
        self.host.bridge.shutdown()

    @Slot()
    def _host_finished(self) -> None:
        # finished is emitted after bridge Closed AND the pet resources settle.
        previous = self.host
        self.host = None
        if previous is not None:
            previous.window.deleteLater()
            previous.deleteLater()
        if self._maintenance_pending:
            self._maintenance_pending = False
            self._show_maintenance()
        else:
            self._quit()

    def _show_maintenance(self) -> None:
        if self.host is not None or self.maintenance_window is not None:
            return
        bridge = self._bridge_factory(self.config_path, parent=self,
                                      provider_override=self._maintenance_provider_override)
        self.maintenance_window = self._window_factory(bridge)
        self.maintenance_window.finished.connect(self._maintenance_finished)
        self.maintenance_window.open()

    @Slot(str)
    def _maintenance_finished(self, action: str) -> None:
        # MaintenanceWindow emits this only after the worker thread has exited.
        previous = self.maintenance_window
        self.maintenance_window = None
        if previous is not None:
            previous.bridge.deleteLater()
            previous.deleteLater()
        if action == "start":
            self._start_host()
        else:
            self._quit()

    def _quit(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.quit()
