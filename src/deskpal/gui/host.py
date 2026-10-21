"""Main-thread desktop composition, notification polling and shutdown ownership."""

from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from PySide6.QtCore import QObject, QSettings, QTimer, Slot
from PySide6.QtWidgets import QApplication

from deskpal.application.service import ApplicationService

from .bridge import ApplicationBridge
from .notifications import NotificationController
from .pet_controller import PetController
from .preferences import GuiPreferences
from .tray import TrayController
from .window import MainWindow


class DesktopHost(QObject):
    """Own the only backend until its Closed receipt, including a late startup."""

    def __init__(self, factory: Callable[[], ApplicationService], *,
                 preferences: GuiPreferences | None = None, quit_on_closed: bool = True,
                 notification_settings: QSettings | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.preferences = preferences if preferences is not None else GuiPreferences()
        self.bridge = ApplicationBridge(factory, parent=self)
        self.window = MainWindow(self.bridge, self.preferences)
        self.window.defer_closed = True
        self.tray = TrayController(self.window, self.preferences, parent=self)
        self.pet = PetController(self.bridge, self.preferences, parent=self)
        self.pet.navigation_requested.connect(self.restore)
        self.pet.stopped.connect(self._finish_closed)
        self._finalized = False
        self._timezone = ZoneInfo("Asia/Shanghai")
        self.notifications = NotificationController(self.preferences, settings=notification_settings,
            show_message=self.tray.show_message,
            clock=lambda: datetime.now(self._timezone), parent=self)
        self._quit_on_closed = quit_on_closed
        self._after_sequence: int | None = None
        self._until_sequence: int | None = None
        self._feed_request: str | None = None
        self._validation_request: str | None = None
        self._validation_serial = 0
        self._notification_generation = 0
        self._hidden_to_tray = False
        self.feed_timer = QTimer(self)
        self.feed_timer.setInterval(2000)
        self.feed_timer.timeout.connect(self._poll_feed)
        self.tick_timer = QTimer(self)
        self.tick_timer.setInterval(1000)
        self.tick_timer.timeout.connect(self.notifications.tick)
        self.bridge.state_changed.connect(self._state)
        self.bridge.reply.connect(self._reply)
        self.bridge.privacy_changed.connect(self._privacy)
        self.window.close_requested.connect(self._window_close)
        self.window.privacy_cleared.connect(self.notifications.clear_pending)
        self.tray.restore_requested.connect(self.restore)
        self.tray.quit_requested.connect(self.bridge.shutdown)
        self.notifications.navigation_requested.connect(self._navigate)
        self.notifications.revalidation_requested.connect(self._validate_notifications)
        self.preferences.changed.connect(self._preference)
        self.tray.message_clicked.connect(self.notifications.activate_notification)

    def start(self, *, visible: bool = True) -> None:
        if visible:
            self.window.show()
        self.pet.start()
        self.bridge.start()

    @Slot()
    def restore(self) -> None:
        self._hidden_to_tray = False
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    @Slot()
    def _window_close(self) -> None:
        if self.bridge.state == "Ready" and self.tray.should_hide_on_close():
            self._hidden_to_tray = True
            self.window.hide()
        else:
            self.bridge.shutdown()

    @Slot(str)
    def _state(self, state: str) -> None:
        if state == "Ready":
            self.feed_timer.start()
            self.tick_timer.start()
            self._request_settings()
            self._poll_feed()
        elif state in {"Closing", "Closed", "CleanupBlocked", "StartupFailed"}:
            self.feed_timer.stop()
            self.tick_timer.stop()
            self.notifications.clear_pending()
            if state == "Closing":
                # A hidden close-to-tray window must remain available on timeout.
                self.restore()
            if state == "Closed":
                self._finish_closed()

    @Slot()
    def _finish_closed(self) -> None:
        if self.bridge.state != "Closed" or self._finalized:
            return
        if not self.pet.settled:
            self.restore()
            self.window.state_label.setText("正在关闭：后台已释放，等待桌宠图片和窗口资源释放。")
            return
        self._finalized = True
        self.tray.dispose()
        self.window.defer_closed = False
        self.window.on_state_changed("Closed")
        self.window.hide()
        if self._quit_on_closed:
            app = QApplication.instance()
            if app is not None:
                app.quit()

    @Slot(int)
    def _privacy(self, epoch: int) -> None:
        self._notification_generation += 1
        self.notifications.clear_pending()
        self._feed_request = None
        self._validation_request = None
        self._after_sequence = None
        self._until_sequence = None

    @Slot(str, object)
    def _preference(self, key: str, value: Any) -> None:
        if key == "pet_click_through" and value and not self.tray.available:
            self.restore()
        if key == "notifications_enabled":
            self._privacy(self.bridge.privacy_epoch)
            if self.bridge.state == "Ready":
                self._poll_feed()

    @Slot()
    def _request_settings(self) -> None:
        if self.bridge.state != "Ready":
            return
        try:
            self.bridge.send("settings")
        except BufferError:
            QTimer.singleShot(250, self._request_settings)

    @Slot()
    def _poll_feed(self) -> None:
        if self._hidden_to_tray and not self.tray.available:
            self.restore()
        if self.bridge.state != "Ready" or self._feed_request is not None:
            return
        if not self.preferences.notifications_enabled:
            return
        args: dict[str, Any] = {"limit": 100}
        if self._after_sequence is not None:
            args["after_sequence"] = self._after_sequence
        if self._until_sequence is not None:
            args["until_sequence"] = self._until_sequence
        try:
            self._feed_request = self.bridge.send("notification_feed", args,
                                                 generation=self._notification_generation)
        except BufferError:
            # The regular feed timer retries after the GUI has drained replies.
            return

    @Slot(object)
    def _validate_notifications(self, candidates: Any) -> None:
        if self.bridge.state != "Ready" or self._validation_request is not None:
            return
        if not isinstance(candidates, dict):
            return
        self._validation_serial = int(candidates["request_id"])
        items = [{"kind": kind, "entity_id": entity_id} for kind, entity_id in candidates["candidates"]][:20]
        generation = self._notification_generation
        try:
            self._validation_request = self.bridge.send("notification_validate", {"items": items},
                                                       generation=generation)
        except BufferError:
            # Keep the controller's current batch, but invalidate this retry if
            # privacy or the notification switch changes before capacity returns.
            QTimer.singleShot(250, lambda: self._retry_validation(candidates, generation))

    def _retry_validation(self, candidates: Any, generation: int) -> None:
        if (generation == self._notification_generation
                and candidates["request_id"] == self._validation_serial):
            self._validate_notifications(candidates)

    @Slot(object)
    def _reply(self, envelope: Any) -> None:
        if not isinstance(envelope, dict) or envelope.get("privacy_epoch") != self.bridge.privacy_epoch:
            return
        operation = envelope.get("operation")
        data = envelope.get("data", {})
        if operation == "settings" and envelope.get("ok"):
            try:
                self._timezone = ZoneInfo(data["timezone"])
            except (KeyError, TypeError, ValueError):
                pass
        if envelope.get("request_id") == self._feed_request:
            self._feed_request = None
            if not envelope.get("ok") or self.bridge.state != "Ready":
                return
            self._after_sequence = int(data["next_sequence"])
            more = bool(data["has_more"])
            self._until_sequence = int(data["until_sequence"]) if more else None
            self.notifications.ingest(data.get("items", []), baseline=bool(data.get("baseline")),
                next_sequence=self._after_sequence, until_sequence=data.get("until_sequence"), has_more=more)
            if more:
                QTimer.singleShot(10, self._poll_feed)
        if envelope.get("request_id") == self._validation_request:
            self._validation_request = None
            self.notifications.revalidated(data.get("items", []) if envelope.get("ok") else [],
                                            self._validation_serial)

    @Slot(str, str)
    def _navigate(self, kind: str, entity_id: str) -> None:
        if self.bridge.state == "Ready":
            self.restore()
            self.window.navigate(kind, entity_id)
