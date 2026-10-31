"""Switch between maintenance and desktop only after the previous owner settles."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Slot
from PySide6.QtWidgets import QApplication

from .host import DesktopHost
from .maintenance import MaintenanceBridge, MaintenanceWindow
from .profiles import ProfileWindow, profile_bridge


class DesktopLauncher(QObject):
    def __init__(self, config_path: Path, factory: Callable[[], Any], *,
                 maintenance: bool = False, host_factory: Callable[..., Any] = DesktopHost,
                 maintenance_provider_override: str | None = None,
                 bridge_factory: Callable[..., Any] = MaintenanceBridge,
                 window_factory: Callable[..., Any] = MaintenanceWindow,
                 profile_bridge_factory: Callable[..., Any] = profile_bridge,
                 profile_window_factory: Callable[..., Any] = ProfileWindow,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config_path = config_path
        self._factory = factory
        self._host_factory = host_factory
        self._bridge_factory = bridge_factory
        self._window_factory = window_factory
        self._profile_bridge_factory = profile_bridge_factory
        self._profile_window_factory = profile_window_factory
        self._initial_maintenance = maintenance
        self._maintenance_provider_override = maintenance_provider_override
        self.host: Any = None
        self.maintenance_window: Any = None
        self.profile_window: Any = None
        self._maintenance_pending = False
        self._profiles_pending = False
        self._startup_failed = False
        self._startup_reason = ""
        self._settings_geometry: Any = None
        self._return_settings_tab: int | None = None

    def start(self) -> None:
        if self._initial_maintenance or not self.config_path.exists():
            self._show_maintenance()
        else:
            self._start_host()

    def _start_host(self) -> None:
        if self.host is not None or self.maintenance_window is not None or self.profile_window is not None:
            return
        self.host = self._host_factory(self._factory, quit_on_closed=False, parent=self)
        self.host.finished.connect(self._host_finished)
        self.host.window.maintenance_requested.connect(self.request_maintenance)
        profiles_requested = getattr(self.host.window, "profiles_requested", None)
        if profiles_requested is not None:
            profiles_requested.connect(self.request_profiles)
        state_changed = getattr(self.host.bridge, "state_changed", None)
        if state_changed is not None:
            state_changed.connect(self._host_state)
        self.host.start()

    @Slot(str)
    def _host_state(self, state: str) -> None:
        if self.host is None or (self.sender() is not None and self.sender() is not self.host.bridge):
            return
        if state == "StartupFailed":
            # Failed startup still owns its worker until Closed; never launch a
            # maintenance instance against resources the old Host may retain.
            self._startup_failed = True
            error = getattr(self.host.bridge, "startup_error", None)
            self._startup_reason = str(error.get("message", "")) if isinstance(error, dict) else ""
            self.request_profiles()
        elif state == 'Ready' and self._return_settings_tab is not None:
            window = self.host.window
            if self._settings_geometry is not None:
                window.restoreGeometry(self._settings_geometry)
            window.set_page(4)
            window.settings_page.tabs.setCurrentIndex(self._return_settings_tab)
            self._return_settings_tab = None

    def _remember_settings(self) -> None:
        window = self.host.window
        if hasattr(window, 'saveGeometry'):
            self._settings_geometry = window.saveGeometry()
        if hasattr(window, 'settings_page'):
            self._return_settings_tab = window.settings_page.tabs.currentIndex()

    @Slot()
    def request_profiles(self) -> None:
        if self.host is None or self._maintenance_pending or self._profiles_pending:
            return
        self._profiles_pending = True
        self._remember_settings()
        self.host.bridge.shutdown()

    @Slot()
    def request_maintenance(self) -> None:
        if self.host is None or self._maintenance_pending or self._profiles_pending:
            return
        self._maintenance_pending = True
        self._remember_settings()
        self.host.bridge.shutdown()

    @Slot()
    def _host_finished(self) -> None:
        # finished is emitted after bridge Closed AND the pet resources settle.
        previous = self.host
        self.host = None
        if previous is not None:
            previous.window.deleteLater()
            previous.deleteLater()
        if self._profiles_pending:
            self._profiles_pending = False
            self._show_profiles()
        elif self._maintenance_pending:
            self._maintenance_pending = False
            self._show_maintenance()
        else:
            self._quit()

    def _show_maintenance(self) -> None:
        if self.host is not None or self.maintenance_window is not None or self.profile_window is not None:
            return
        bridge = self._bridge_factory(self.config_path, parent=self,
                                      provider_override=self._maintenance_provider_override)
        self.maintenance_window = self._window_factory(bridge)
        if self._settings_geometry is not None:
            self.maintenance_window.restoreGeometry(self._settings_geometry)
        if self._return_settings_tab == 6 and hasattr(self.maintenance_window, 'tabs'):
            self.maintenance_window.tabs.setCurrentIndex(2)
        self.maintenance_window.finished.connect(self._maintenance_finished)
        self.maintenance_window.open()

    @Slot(str)
    def _maintenance_finished(self, action: str) -> None:
        # MaintenanceWindow emits this only after the worker thread has exited.
        previous = self.maintenance_window
        self.maintenance_window = None
        if previous is not None:
            if hasattr(previous, 'saveGeometry'):
                self._settings_geometry = previous.saveGeometry()
            previous.bridge.deleteLater()
            previous.deleteLater()
        if action == "start":
            self._start_host()
        elif action == "profiles":
            self._show_profiles()
        else:
            self._quit()

    def _show_profiles(self) -> None:
        if self.host is not None or self.maintenance_window is not None or self.profile_window is not None:
            return
        bridge = self._profile_bridge_factory(self.config_path, parent=self,
                    provider_override=self._maintenance_provider_override)
        self.profile_window = self._profile_window_factory(bridge)
        if self._settings_geometry is not None:
            self.profile_window.restoreGeometry(self._settings_geometry)
        self.profile_window.finished.connect(self._profiles_finished)
        self.profile_window.open()
        if self._startup_failed:
            self._startup_failed = False
            self.profile_window.startup_notice = "后台打开失败，已等待资源关闭。请重试打开、选择其他档案，或进入共用配置与维护。"
            if self._startup_reason:
                self.profile_window.startup_notice += "\n" + self._startup_reason
            self._startup_reason = ""
            self.profile_window.status.setText(self.profile_window.startup_notice)

    @Slot(str)
    def _profiles_finished(self, action: str) -> None:
        # ProfileWindow waits for the same bounded maintenance worker to exit.
        previous = self.profile_window
        self.profile_window = None
        if previous is not None:
            if hasattr(previous, 'saveGeometry'):
                self._settings_geometry = previous.saveGeometry()
            previous.bridge.deleteLater()
            previous.deleteLater()
        if action == "start":
            self._start_host()
        elif action == "maintenance":
            self._show_maintenance()
        else:
            self._quit()

    def _quit(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.quit()
