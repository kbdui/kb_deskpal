"""Main-thread desktop composition, notification polling and shutdown ownership."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PySide6.QtCore import QObject, QSettings, QStandardPaths, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication, QMenu

from deskpal.application.service import ApplicationService

from .bridge import ApplicationBridge
from .notifications import NotificationController
from .pet_controller import PetController
from .preferences import GuiPreferences
from .tray import TrayController
from .studio_window import StudioPetChat, StudioWindow


class DesktopHost(QObject):
    """Own the only backend until its Closed receipt, including a late startup."""

    finished = Signal()

    def __init__(self, factory: Callable[[], ApplicationService], *,
                 preferences: GuiPreferences | None = None, quit_on_closed: bool = True,
                 notification_settings: QSettings | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.preferences = preferences if preferences is not None else GuiPreferences()
        self.bridge = ApplicationBridge(factory, parent=self)
        self.bridge.configure_mobile(
            Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)) / "mobile" / "access.sqlite3",
            {"user": self.preferences.user_display_name, "assistant": self.preferences.character_display_name})
        self.window = StudioWindow(self.bridge, self.preferences)
        self.window.defer_closed = True
        self.tray = TrayController(self.window, self.preferences, parent=self)
        self.pet = PetController(self.bridge, self.preferences, parent=self)
        self.pet_chat: StudioPetChat | None = None
        self._retired_chats: dict[int, StudioPetChat] = {}
        self._pet_menu = QMenu(self.window)
        self._pet_menu.addAction("聊一聊", self._open_pet_chat)
        self._pet_menu.addAction("打开主窗口", self.restore)
        self._pet_menu.addAction("收起小窗", self._hide_pet_chat)
        self._pet_menu.addAction("暂时隐藏桌宠", self.pet.hide_temporarily)
        self._pet_menu.addSeparator()
        self._pet_menu.addAction('调整桌宠大小…', self._resize_pet)
        self._pet_menu.addAction('通知与免打扰设置', self._notification_settings)
        self.pet.navigation_requested.connect(self._toggle_pet_chat)
        self.pet.context_requested.connect(self._show_pet_menu)
        self.pet.anchor_changed.connect(self._reanchor_pet_chat)
        self.pet.unavailable.connect(self._hide_pet_chat)
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
        self._exit_prompt_active = False
        self._exit_confirmed = False
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
        self.window.pet_requested.connect(self._open_pet_chat)
        self.window.privacy_cleared.connect(self.notifications.clear_pending)
        self.tray.restore_requested.connect(self.restore)
        self.tray.chat_requested.connect(self._open_pet_chat)
        self.tray.quit_requested.connect(self._request_quit)
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
    def _toggle_pet_chat(self) -> None:
        if self.pet_chat is not None and self.pet_chat.isVisible():
            self.pet_chat.hide()
        else:
            self._open_pet_chat()

    @Slot()
    def _open_pet_chat(self) -> None:
        if self.bridge.state != "Ready":
            return
        if self.pet_chat is None:
            self.pet_chat = StudioPetChat(self.window.chat_coordinator)
            self.pet_chat.expand_requested.connect(self._expand_pet_chat)
        self.pet_chat.open_for(self.pet.window if self.pet.window is not None else self.window)

    @Slot()
    def _notification_settings(self) -> None:
        self.restore()
        self.window.show_settings(2)

    @Slot()
    def _resize_pet(self) -> None:
        self.restore()
        self.window.show_settings(1)

    @Slot()
    def _hide_pet_chat(self) -> None:
        if self.pet_chat is not None:
            self.pet_chat.hide()
        self._pet_menu.hide()

    @Slot()
    def _reanchor_pet_chat(self) -> None:
        if self.pet_chat is not None and self.pet_chat.isVisible():
            self.pet_chat.reanchor()

    @Slot(object)
    def _show_pet_menu(self, position: Any) -> None:
        if self.bridge.state == "Ready" and self.pet.window is not None:
            self._pet_menu.popup(position)

    @Slot(str, str)
    def _expand_pet_chat(self, conversation_id: str, task_id: str) -> None:
        if self.bridge.state != "Ready":
            return
        self._hide_pet_chat()
        self.restore()
        self.window.open_conversation(conversation_id, task_id=task_id or None)

    def _dispose_pet_chat(self) -> None:
        chat, self.pet_chat = self.pet_chat, None
        if chat is None:
            return
        identity = id(chat)
        self._retired_chats[identity] = chat
        chat.destroyed.connect(lambda _object=None, key=identity: self._chat_destroyed(key))
        chat.dispose()

    def _chat_destroyed(self, identity: int) -> None:
        # QObject.destroyed is emitted before its children finish destruction.
        # Keep the ownership barrier through the next event-loop turn.
        QTimer.singleShot(0, self, lambda key=identity: self._chat_reaped(key))

    def _chat_reaped(self, identity: int) -> None:
        self._retired_chats.pop(identity, None)
        self._finish_closed()

    @Slot()
    def restore(self) -> None:
        self._hidden_to_tray = False
        if self.preferences.pet_visible:
            self.pet.restore_visibility()
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    @Slot()
    def _window_close(self) -> None:
        if self.bridge.state == "Ready" and self.tray.should_hide_on_close():
            self._hidden_to_tray = True
            self.window.hide()
        else:
            self._request_quit()

    @Slot()
    def _request_quit(self) -> None:
        state = self.bridge.state
        if state in {"Closing", "Closed"} or self._exit_prompt_active:
            return
        if state == "StartupFailed":
            self.bridge.shutdown()
            return
        if not self._exit_confirmed:
            # Native modal dialogs run a nested event loop. Repeated tray or
            # close gestures must not create another confirmation within it.
            self._exit_prompt_active = True
            try:
                confirmed = self.window.confirm_exit()
            finally:
                self._exit_prompt_active = False
            if not confirmed:
                return
            self._exit_confirmed = True
        if self.bridge.state not in {"Closing", "Closed"}:
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
            self._hide_pet_chat()
            self._dispose_pet_chat()
            if state == "Closing":
                # A hidden close-to-tray window must remain available on timeout.
                self.restore()
            if state == "Closed":
                self._finish_closed()

    @Slot()
    def _finish_closed(self) -> None:
        if self.bridge.state != "Closed" or self._finalized:
            return
        if not self.pet.settled or self.pet_chat is not None or self._retired_chats:
            self.restore()
            self.window.status_message("正在关闭：后台已释放，等待桌宠图片和聊天窗口资源释放。")
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
        self.finished.emit()

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
        if key in {"user_display_name", "character_display_name"}:
            self.bridge.update_mobile_names({"user": self.preferences.user_display_name,
                                            "assistant": self.preferences.character_display_name})
        if key == "pet_enabled" and not value:
            self._dispose_pet_chat()
        if key == "pet_visible" and not value:
            self._hide_pet_chat()
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
            QTimer.singleShot(250, self, self._request_settings)

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
            QTimer.singleShot(250, self, lambda: self._retry_validation(candidates, generation))

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
        if envelope.get('ok'):
            policy = data.get('notification_policy')
            if operation in {'notification_policy', 'notification_quiet', 'notification_control'}:
                policy = data
            if isinstance(policy, dict): self.notifications.update_policy(policy)
        if operation == "settings" and envelope.get("ok"):
            try:
                self._timezone = ZoneInfo(data["timezone"])
            except (KeyError, TypeError, ValueError):
                pass
        if envelope.get("request_id") == self._feed_request:
            self._feed_request = None
            if not envelope.get("ok") or self.bridge.state != "Ready":
                # A restored store can have a smaller audit range. Re-establish a
                # new baseline rather than repeatedly replaying or trusting its old cursor.
                self._after_sequence = None
                self._until_sequence = None
                self.notifications.clear_pending()
                return
            self._after_sequence = int(data["next_sequence"])
            more = bool(data["has_more"])
            self._until_sequence = int(data["until_sequence"]) if more else None
            self.notifications.ingest(data.get("items", []), baseline=bool(data.get("baseline")),
                next_sequence=self._after_sequence, until_sequence=data.get("until_sequence"), has_more=more)
            if more:
                QTimer.singleShot(10, self, self._poll_feed)
        if envelope.get("request_id") == self._validation_request:
            self._validation_request = None
            self.notifications.revalidated(data.get("items", []) if envelope.get("ok") else [],
                                            self._validation_serial)

    @Slot(str, str)
    def _navigate(self, kind: str, entity_id: str) -> None:
        if self.bridge.state == "Ready":
            self.restore()
            self.window.navigate(kind, entity_id)
