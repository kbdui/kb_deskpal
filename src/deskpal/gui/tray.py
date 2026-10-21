"""Qt system tray lifecycle adapter."""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from .preferences import GuiPreferences


def _tray_icon() -> QIcon:
    """Draw a small static fallback mark without depending on external assets."""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(Qt.GlobalColor.darkCyan)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(2, 2, 28, 28)
    painter.setBrush(Qt.GlobalColor.white)
    painter.drawEllipse(9, 8, 14, 14)
    painter.drawRoundedRect(7, 20, 18, 7, 3, 3)
    painter.end()
    return QIcon(pixmap)


class TrayController(QObject):
    restore_requested = Signal()
    quit_requested = Signal()
    message_clicked = Signal()

    def __init__(self, window: QWidget, preferences: GuiPreferences,
                 parent: QObject | None = None) -> None:
        super().__init__(parent or window)
        self._window = window
        self._preferences = preferences
        initially_available = bool(QSystemTrayIcon.isSystemTrayAvailable())
        self.supports_messages = bool(QSystemTrayIcon.supportsMessages())
        self._tray: QSystemTrayIcon | None = None
        self._menu: QMenu | None = None
        if initially_available:
            self._tray = QSystemTrayIcon(_tray_icon(), self)
            self._tray.setToolTip("DeskPal")
            self._menu = QMenu()
            restore = QAction("恢复窗口", self._menu)
            restore.triggered.connect(self.restore_requested)
            quit_action = QAction("退出", self._menu)
            quit_action.triggered.connect(self.quit_requested)
            self._menu.addAction(restore)
            for key, title in (("pet_enabled", "启用桌宠"), ("pet_visible", "显示桌宠"),
                               ("pet_on_top", "桌宠置顶"), ("pet_click_through", "桌宠鼠标穿透")):
                action = QAction(title, self._menu)
                action.setCheckable(True)
                action.setChecked(bool(getattr(preferences, key, False)))
                action.toggled.connect(lambda value, name=key: getattr(preferences, "set_" + name)(value))
                preferences.changed.connect(
                    lambda name, value, name_key=key, item=action: self._sync_pet_action(item, value)
                    if name == name_key else None)
                self._menu.addAction(action)
            self._menu.addSeparator()
            self._menu.addAction(quit_action)
            self._tray.setContextMenu(self._menu)
            self._tray.activated.connect(self._on_activated)
            self._tray.messageClicked.connect(self.message_clicked)
            self._tray.show()

    @property
    def available(self) -> bool:
        """Report current OS tray availability, which can change at runtime."""
        return self._tray is not None and bool(QSystemTrayIcon.isSystemTrayAvailable())

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick}:
            self.restore_requested.emit()

    @staticmethod
    def _sync_pet_action(action: QAction, value: object) -> None:
        action.blockSignals(True)
        action.setChecked(bool(value))
        action.blockSignals(False)

    def should_hide_on_close(self) -> bool:
        """Only permit close-to-tray when a working tray can restore the window."""
        return (self._tray is not None and QSystemTrayIcon.isSystemTrayAvailable()
                and bool(self._preferences.close_to_tray))

    def show_message(self, message: str) -> None:
        if self.available and self._tray is not None and self.supports_messages:
            self._tray.showMessage("DeskPal", message, QSystemTrayIcon.MessageIcon.Information)

    def dispose(self) -> None:
        if self._tray is not None:
            self._tray.hide()
