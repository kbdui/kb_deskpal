"""Production workbench and pet chat built from the approved Studio design."""

import base64
from collections.abc import Callable
from importlib import resources
import json
from typing import Any

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QPixmap
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from deskpal.response.templates import legacy_chat_reply

from .channel import Channel
from .chat import ChatCoordinator
from .pet_window import clamp_position
from .preferences import GuiPreferences
from .studio_surface import StudioSurface
from .tool_results import operation_records

PAGES = ("chat", "plans", "memory", "plugins", "settings")
SETTINGS = ("role", "appearance", "notifications", "data", "model", "mobile")
SLOTS = {"role": "character_avatar", "user": "user_avatar", "pet": "pet_main",
         "pet_thinking": "pet_thinking", "pet_focused": "pet_focused",
         "pet_pleased": "pet_pleased", "pet_alert": "pet_alert"}
PET_EXPRESSIONS = {"pet": "neutral", "pet_thinking": "thinking", "pet_focused": "focused",
                   "pet_pleased": "pleased", "pet_alert": "alert"}
PREFERENCES = (
    "theme", "pet_visible", "pet_enabled", "pet_scale", "pet_on_top", "close_to_tray",
    "notifications_enabled", "user_display_name", "character_display_name",
)
ACTIVE_TASKS = ["Created", "Planning", "Running", "Waiting", "NeedsReview", "AwaitingInput", "Paused", "Uncertain"]


def png_url(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii") if data else ""


def pixmap_url(pixmap: QPixmap) -> str:
    if pixmap.isNull():
        return ""
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    return png_url(bytes(data.data()))


def scheduled_from_review(review: dict[str, Any]) -> list[dict[str, Any]]:
    """Cards cite successful tool output, never planned parameters."""
    events = []
    for record in operation_records(review):
        result = record.get("result") or {}
        if result.get("success") is not True or result.get("tool_name") not in {"calendar_create", "calendar_update", "timer_create"}:
            continue
        output = result.get("output")
        if not isinstance(output, dict):
            continue
        item = output.get("entry", output.get("timer", output))
        if not isinstance(item, dict) or not item.get("id") or not (item.get("start_at") or item.get("due_at")):
            continue
        events.append({**item, "title": item.get("title") or item.get("label") or "安排"})
    return events


class StudioChatSurface(StudioSurface):
    """Shared chat presentation with independently selected session and scroll history."""
    expand_requested = Signal(str, str)

    def __init__(self, coordinator: ChatCoordinator, mode: str) -> None:
        super().__init__()
        self.chat_coordinator = coordinator
        self.mode = mode
        self.identity = ""
        self.route = "chat"
        self.history: list[dict[str, Any]] = []
        self.history_cursor: int | None = None
        self.history_reading = False
        self.generation = 0
        self.selected_task = ""
        self.attached = False
        self._signature = ""
        self._input_origin: str | None = None
        self.port.action.connect(self._action)
        self.port.attached.connect(self._attach)
        coordinator.conversations_changed.connect(self._directory)
        coordinator.timeline_changed.connect(self._timeline)
        coordinator.task_changed.connect(self._task_changed)
        coordinator.ready_changed.connect(self._push_chat)
        coordinator.draft_changed.connect(self._draft)
        coordinator.notice.connect(self._toast)
        coordinator.appearance.image_changed.connect(self._image)
        coordinator.bridge.privacy_changed.connect(self._privacy)
        if coordinator.preferences is not None:
            coordinator.preferences.changed.connect(self._prefs)

    def _attach(self) -> None:
        self.attached = True
        self._signature = ""
        self.port.publish("init", {"mode": self.mode, "prefs": self._preference_data(),
                                   "lifecycle": self.chat_coordinator.bridge.state})
        for slot, pixmap in self.chat_coordinator.appearance.images.items():
            self._image(slot, pixmap)
        self._directory()

    def _preference_data(self) -> dict[str, Any]:
        p = self.chat_coordinator.preferences
        density = str(QSettings().value("gui/studio_density", "comfortable"))
        return {**{key: getattr(p, key, None) for key in PREFERENCES}, "density": density}

    def _prefs(self, *_: Any) -> None:
        if self.attached:
            self.port.publish("prefs", self._preference_data())

    def _image(self, slot: str, pixmap: QPixmap) -> None:
        key = next((key for key, value in SLOTS.items() if value == slot), None)
        if self.attached and key:
            self.port.publish("image", {"key": key, "url": pixmap_url(pixmap)})

    def _toast(self, text: str) -> None:
        if self.attached and text:
            self.port.publish("toast", text)

    def _watch(self) -> None:
        self.chat_coordinator.watch(self, self.identity, self.isVisible() and self.route == "chat")

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._watch()

    def hideEvent(self, event: Any) -> None:
        super().hideEvent(event)
        self._watch()

    def _directory(self) -> None:
        if not self.identity and self.chat_coordinator.default_id:
            self._select(self.chat_coordinator.default_id)
        else:
            self._push_chat()

    def _select(self, identity: str) -> None:
        if identity != self.identity:
            self.identity = identity
            self._reset_history()
            self._close_records()
        self._watch()
        self._push_chat()

    def _reset_history(self) -> None:
        self.generation += 1
        self.history = []
        self.history_cursor = None
        self.history_reading = False

    def _close_records(self) -> None:
        self.selected_task = ""
        self.chat_coordinator.watch_review(id(self), None)

    def _items(self) -> list[dict[str, Any]]:
        latest = self.chat_coordinator.timelines.get(self.identity, [])
        by_id = {row.get("id"): row for row in [*self.history, *latest]}
        return sorted(by_id.values(), key=lambda row: row.get("sequence", 0))

    def _visible_tasks(self) -> set[str]:
        return {str(row["task_id"]) for row in self._items() if row.get("task_id") and row.get("visible") is not False}

    def _push_chat(self, *, prepend: bool = False, locate: str = "") -> None:
        if not self.attached:
            return
        c = self.chat_coordinator
        identity = self.identity
        selected = c.conversations.get(identity, {})
        items = self._items()
        ids = self._visible_tasks()
        if self.selected_task:
            ids.add(self.selected_task)
        reviews: dict[str, dict[str, Any]] = {}
        events: dict[str, list[dict[str, Any]]] = {}
        for task in ids:
            if task in c.reviews:
                review = c.reviews[task]
                reviews[task] = {"review": review, "grants": sorted(c.grants.get(task, [])),
                                 "pending": task in c.task_pending}
                if task == self.selected_task:
                    reviews[task]["records"] = operation_records(review)
                events[task] = scheduled_from_review(review)
        submissions = [{key: item.get(key) for key in ("request", "state", "retryable")} | {"command_id": command}
                       for command, item in c.submissions.items() if item["conversation_id"] == identity]
        display_items = []
        for item in items:
            result = c.reviews.get(str(item.get("task_id", "")), {}).get("task", {}).get("result")
            if (item.get("kind") == "assistant_message" and not item.get("body_truncated")
                    and not item.get("body_start") and isinstance(result, dict)
                    and isinstance(result.get("tool_results"), list)):
                facts = [fact for fact in result["tool_results"] if isinstance(fact, dict)]
                text = legacy_chat_reply(str(item.get("body", "")), result.get("response"), facts)
                if text is not None:
                    item = {**item, "body": text}
            display_items.append(item)
        payload = {
            "id": identity, "default": c.default_id, "title": selected.get("title"), "items": display_items,
            "sessions": [{"id": key, "title": item.get("title") or "未命名会话"}
                         for key, item in c.conversations.items()],
            "moreSessions": c.directory_more, "archived": c.archived, "submissions": submissions,
            "draft": c.draft(identity).text if identity else "",
            "reviews": reviews, "events": events, "available": c.available and bool(identity),
            "modelReady": c.model_ready, "canSend": bool(c.available and c.model_ready and identity and
                not (selected.get("archived_at") or selected.get("archived") or selected.get("deleted_at"))),
            "working": any(row["review"].get("status") in {"Created", "Planning", "Running", "AwaitingInput"}
                           for row in reviews.values()),
            "more": bool(self.history_cursor) if self.history else c.more.get(identity, False),
            "history": bool(self.history), "prepend": prepend, "locate": locate,
        }
        signature = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if signature != self._signature or locate:
            self._signature = signature
            self.port.publish("chat", payload)

    def _timeline(self, identity: str) -> None:
        if identity == self.identity:
            coordinator = self.chat_coordinator
            # A successful scheduling call may hide its task card. Keep recent
            # reply reviews available for confirmed event cards and the sidebar.
            if self.mode == "main" and self.route == "chat":
                candidates = dict.fromkeys(row.get("task_id") for row in reversed(
                    coordinator.timelines.get(identity, [])) if row.get("task_id"))
                for task in list(candidates)[:12]:
                    if task not in coordinator.reviews:
                        coordinator.review(task)
            self._push_chat()

    def _task_changed(self, identity: str) -> None:
        if identity == self.selected_task or identity in self._visible_tasks():
            self._push_chat()

    def _draft(self, identity: str, text: str, revision: int) -> None:
        if self.attached and identity == self.identity and text != self._input_origin:
            self.port.publish("draft", {"id": identity, "text": text})

    def _privacy(self, *_: Any) -> None:
        self.identity = ""
        self._watch()
        self._reset_history()
        self._close_records()
        self._signature = ""
        if self.attached:
            self.port.publish("clear", {})
        self._push_chat()

    def _read_history(self, before: int, task: str = "") -> None:
        identity = self.identity
        generation = self.generation
        self.history_reading = True

        def done(data: dict[str, Any]) -> None:
            if self.generation != generation or self.identity != identity:
                return
            self.history_reading = False
            if "_error" in data:
                self._toast("历史读取失败，请重试。")
                return
            incoming = list(data.get("items", []))
            # Keep at most 250 historical items per surface.
            self.history = (incoming + self.history)[:250]
            cursor = data.get("next_cursor")
            self.history_cursor = cursor if type(cursor) is int and data.get("has_more") and len(self.history) < 250 else None
            for row in incoming:
                if row.get("task_id") and row.get("kind") == "task_card" and row.get("visible", True):
                    self.chat_coordinator.review(str(row["task_id"]))
            self._push_chat(prepend=not task, locate=task)

        request = self.chat_coordinator.channel.send(
            "conversation_timeline", {"id": identity, "before": before, "limit": 50}, done)
        if request is None:
            self.history_reading = False

    def _locate(self, task: str) -> None:
        if task in self._visible_tasks():
            self._push_chat(locate=task)
            return
        generation = self.generation
        identity = self.identity

        def done(data: dict[str, Any]) -> None:
            if generation != self.generation or identity != self.identity:
                return
            cursor = data.get("before")
            if type(cursor) is int:
                self._read_history(cursor, task)
            else:
                self._toast("原消息暂时无法定位。")

        self.chat_coordinator.locate(identity, task, done, f"studio-locate:{id(self)}:{generation}")

    def _action(self, action: str, args: dict) -> None:
        c = self.chat_coordinator
        if action == "route":
            page = args.get("page")
            if self.mode == "main" and page in PAGES:
                self.route = page
                self._watch()
                if page != "chat":
                    self._close_records()
            return
        if action == "hide" and self.mode == "pet":
            self.hide()
            return
        if action == "expand-chat" and self.mode == "pet":
            self.expand_requested.emit(self.identity, "")
            return
        if not c.available:
            return
        if action == "session" and args.get("id") in c.conversations:
            self._select(args["id"])
        elif action == "more-sessions":
            c.refresh_directory(True)
        elif action == "archived-sessions":
            c.archived = not c.archived
            c.refresh_directory()
        elif action == "new-session":
            title = str(args.get("title", "")).strip()
            if 0 < len(title) <= 120:
                c.create(title)
        elif action in {"rename", "archive-session", "delete-session", "clear-history"}:
            self._conversation_action(action, args)
        elif action in {"submit", "draft"} and args.get("id") == self.identity and self.identity:
            value = args.get("text")
            if isinstance(value, str):
                self._input_origin = value
                try:
                    accepted = c.edit_draft(self.identity, value)
                finally:
                    self._input_origin = None
                if accepted and action == "submit":
                    c.submit(self.identity)
                    self._reset_history()
                    self._push_chat()
        elif action == "retry":
            command = args.get("id")
            if command in c.submissions and c.submissions[command]["conversation_id"] == self.identity:
                c.retry_submission(command)
        elif action == "older" and not self.history_reading:
            before = self.history_cursor if self.history else c.cursors.get(self.identity)
            if type(before) is int:
                self._read_history(before)
        elif action == "latest":
            self._reset_history()
            c.load(self.identity)
            self._push_chat()
        elif action == "message-part":
            self._load_message_part(args)
        elif action in {"records", "close-records"} and self.mode == "main":
            if action == "close-records":
                self._close_records()
            else:
                self._records(args.get("id"))
        elif action in {"task-action", "task-input", "grant"}:
            self._task_action(action, args)
        else:
            self._workbench_action(action, args)

    def _workbench_action(self, action: str, args: dict) -> None:
        pass

    def _conversation_action(self, action: str, args: dict) -> None:
        c = self.chat_coordinator
        identity = args.get("id") if action == "clear-history" else self.identity
        if identity not in c.conversations:
            return
        if action == "clear-history":
            c.remove_conversation(identity)
        elif identity != c.default_id:
            if action == "rename":
                title = str(args.get("title", "")).strip()
                if 0 < len(title) <= 120:
                    c.update_conversation(identity, title=title)
            elif action == "archive-session":
                item = c.conversations[identity]
                c.update_conversation(identity, archived=not bool(item.get("archived_at") or item.get("archived")))
            elif action == "delete-session":
                c.remove_conversation(identity, True)
                self._select(c.default_id)

    def _load_message_part(self, args: dict) -> None:
        offset = args.get("offset")
        item_id = args.get("id")
        if not isinstance(item_id, str) or type(offset) is not int or offset < 0:
            return
        identity = self.identity
        c = self.chat_coordinator
        if not any(item.get("id") == item_id for item in self.history):
            if any(item.get("id") == item_id for item in c.timelines.get(identity, [])):
                c.load_body(identity, item_id, offset)
            return
        generation = self.generation

        def done(data: dict[str, Any]) -> None:
            if self.generation != generation or "_error" in data:
                return
            item = next((row for row in self.history if row.get("id") == item_id), None)
            if item is not None:
                value = data.get("item", data)
                body = str(value.get("body", ""))
                item.update(body=body, body_start=offset, body_truncated=data.get("has_more", False),
                            body_offset=data.get("next_offset") or offset + len(body))
                self._push_chat()

        c.channel.send("conversation_item", {"id": identity, "item_id": item_id, "offset": offset,
                                             "limit": 32768}, done)

    def _records(self, selected: Any = None) -> None:
        c = self.chat_coordinator
        task = selected if selected in self._visible_tasks() else None
        if not task:
            task = next((row["task_id"] for row in reversed(self._items()) if row.get("task_id")
                         and row.get("visible") is not False and (operation_records(c.reviews.get(row["task_id"], {}))
                                                                  or row["task_id"] not in c.reviews)), "")
        self.selected_task = task
        c.watch_review(id(self), task or None)
        if task:
            c.review(task)
        self._push_chat()
        self.port.publish("record", {"id": task})

    def _task_action(self, action: str, args: dict) -> None:
        identity = args.get("id")
        c = self.chat_coordinator
        if identity not in self._visible_tasks() or identity in c.task_pending:
            return
        review = c.reviews.get(identity, {})
        if not review or review.get("task_revision") != args.get("revision"):
            self._toast("任务状态已经更新，请重新检查。")
            c.review(identity)
            return
        operation = args.get("action")
        if action == "grant":
            tool = args.get("tool")
            if (isinstance(tool, str) and tool in review.get("missing_permissions", {}).get("high_risk_tools", [])
                    and type(args.get("enabled")) is bool):
                c.set_grant(identity, tool, args["enabled"])
            return
        if action == "task-input":
            content = args.get("text")
            if not isinstance(content, str) or not content.strip():
                return
            if operation == "review_uncertain" and operation in review.get("available_actions", []):
                verdict = args.get("verdict")
                if verdict in {"unknown", "occurred", "not_occurred"}:
                    c.channel.send(operation, {"task_id": identity, "verdict": verdict, "evidence": content},
                                   lambda _: c.review(identity), mutation=True)
            elif operation in {"task_answer", "task_append_requirement"}:
                status = review.get("status")
                if (operation == "task_answer" and status == "AwaitingInput") or (
                        operation == "task_append_requirement" and status in ACTIVE_TASKS):
                    c.channel.send(operation, {"task_id": identity, "content": content},
                                   lambda _: c.review(identity), mutation=True)
            return
        if operation not in review.get("available_actions", []):
            return
        if operation == "confirm":
            c.confirm(identity)
        elif operation in {"cancel", "reject_tools", "task_pause", "task_continue", "resume"}:
            c.task_action(identity, operation)
class StudioWindow(StudioChatSurface):
    close_requested = Signal()
    privacy_cleared = Signal()
    maintenance_requested = Signal()
    profiles_requested = Signal()
    pet_requested = Signal()

    def __init__(self, bridge: Any, preferences: GuiPreferences) -> None:
        coordinator = ChatCoordinator(bridge, preferences=preferences)
        super().__init__(coordinator, "main")
        coordinator.setParent(self)
        self.bridge = bridge
        self.preferences = preferences
        self.operations = Channel(bridge, self)
        self.operations.notice.connect(self._toast)
        self.lifecycle = bridge.state
        self.defer_closed = False
        self._allow_close = False
        self._role_dirty = False
        self.setting_index = 0
        self._pending_route: tuple[str, str] | None = None
        self._data: dict[str, dict] = {}
        self._load_versions: dict[str, int] = {}
        self._asset_generation = 0
        self._asset_image: str | None = None
        self._asset_pending: dict = {}
        self._appearance_read = False
        self._pet_preview_id: str | None = None
        self._pet_preview_key = "pet"
        self._quiet_pending = False
        self._quiet_target: dict[str, Any] | None = None
        self._quiet_last = (22 * 60, 8 * 60)
        self.profile_action = ""
        self.resize(1180, 800)
        self.setMinimumSize(420, 380)
        self.setWindowTitle("Deskpal")
        geometry = preferences.geometry
        if geometry:
            self.restoreGeometry(QByteArray(geometry))
        bridge.state_changed.connect(self.on_state_changed)
        bridge.image_reply.connect(self._asset_reply)
        coordinator.appearance.changed.connect(self._appearance_changed)
        coordinator.expand_requested.connect(self.open_conversation)
        self.load_studio()

    def _attach(self) -> None:
        super()._attach()
        self._appearance_changed()
        if self._pending_route:
            self._show(*self._pending_route)

    def status_message(self, text: str) -> None:
        self.port.publish("state", {"state": self.lifecycle, "text": text})

    def on_state_changed(self, state: str) -> None:
        if state == "Closed" and self.defer_closed:
            state = "Closing"
        self.lifecycle = state
        messages = {
            "Initializing": "正在打开角色档案…", "Closing": "正在安全关闭后台…",
            "StartupFailed": "后台打开失败。", "CleanupBlocked": "仍有隐私清理等待完成。",
        }
        if state == "Ready":
            self.chat_coordinator.appearance.refresh()
            if self.route != "chat":
                self._show(self.route, SETTINGS[self.setting_index])
        elif state in {"Closing", "Closed", "StartupFailed", "CleanupBlocked"}:
            self._privacy()
            if state == "CleanupBlocked":
                self.show_settings(3)
        self.status_message(messages.get(state, ""))
        if state == "Closed":
            self._allow_close = True
            self.close()

    def _privacy(self, *_: Any) -> None:
        self._data.clear()
        self._load_versions.clear()
        self.operations.invalidate()
        self._role_dirty = False
        self._asset_generation += 1
        self._asset_image = self._pet_preview_id = None
        self._asset_pending.clear()
        self._quiet_pending = False
        self._quiet_target = None
        super()._privacy()

    def _show(self, page: str, setting: str = "") -> None:
        self.route = page
        self._watch()
        self._pending_route = (page, setting)
        if self.attached:
            self.port.publish("route", {"page": page, "setting": setting})

    def set_page(self, index: int) -> None:
        self._show(PAGES[index], SETTINGS[self.setting_index] if index == 4 else "")

    def show_settings(self, index: int) -> None:
        self.setting_index = index
        self._show("settings", SETTINGS[index])

    def open_conversation(self, identity: str, task_id: str | None = None) -> None:
        self._show("chat")
        self._select(self.chat_coordinator.default_id if identity in {"", "default"} else identity)
        if task_id:
            self._locate(task_id)

    def navigate_to(self, kind: str, identity: str) -> None:
        self.navigate(kind, identity)

    def navigate(self, kind: str, identity: str) -> None:
        if kind in {"conversation", "chat"}:
            self.open_conversation(identity)
        elif kind in {"task", "reminder"}:
            operation, key = ("task_review", "task_id") if kind == "task" else ("reminder_detail", "id")

            def done(data: dict[str, Any]) -> None:
                item = data.get("item", data)
                if item.get("conversation_id"):
                    self.open_conversation(item["conversation_id"], item.get("task_id"))
                elif "_error" not in data:
                    self._show("plans")
                    self._toast("已打开安排。此记录没有可定位的来源会话。")

            self.operations.send(operation, {key: identity}, done)

    def has_unsaved_content(self) -> bool:
        return (self._role_dirty or bool(self._asset_pending) or
                any(draft.text.strip() for draft in self.chat_coordinator.drafts.values()))

    def confirm_exit(self) -> bool:
        if not self.has_unsaved_content():
            return True
        return QMessageBox.question(self, "未保存的编辑", "放弃未发送的草稿和未保存的修改？",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    def request_exit(self) -> None:
        self.close_requested.emit()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.preferences.set_geometry(bytes(self.saveGeometry().data()))
        if self._allow_close:
            self.attached = False
            self.release_studio()
            event.accept()
        else:
            event.ignore()
            self.close_requested.emit()

    def _action(self, action: str, args: dict) -> None:
        if action == "route" and args.get("setting") in SETTINGS:
            self.setting_index = SETTINGS.index(args["setting"])
        if action == "cleanup" and self.lifecycle == "CleanupBlocked":
            self._cleanup()
            return
        super()._action(action, args)

    def _workbench_action(self, action: str, args: dict) -> None:
        if action == "load":
            self._load(args)
        elif action == "mobile":
            operation = args.get("operation")
            parameters = args.get("parameters", {})
            if operation in {"mobile_status", "mobile_enable", "mobile_disable", "mobile_pair", "mobile_revoke"} and isinstance(parameters, dict):
                # 本机访问配置不使用任务命令回执；保持参数契约，不附加 command_id。
                self.operations.send(operation, parameters, lambda data: self._data_result("mobile", data, mobileOperation=operation))
        elif action == "pet":
            self.pet_requested.emit()
        elif action in {"maintenance", "profiles", "backup", "restore"}:
            if self.confirm_exit():
                if action == "maintenance":
                    self.maintenance_requested.emit()
                else:
                    self.profile_action = action
                    self.profiles_requested.emit()
        elif action in {"role-dirty", "role-reverted"}:
            self._role_dirty = action == "role-dirty"
        elif action == "save-card":
            self._save_card(args.get("text"))
        elif action == "preference":
            self._set_preference(args)
        elif action == "cleanup":
            self._cleanup()
        elif action == "tool":
            self._tool(args.get("id"))
        elif action in {"plugin-page", "plugin-option"}:
            self._plugin(action, args)
        elif action in {"plan-chat", "memory-chat", "event"}:
            self._linked_action(action, args)
        elif action == "asset-pick":
            self._asset_pick(args.get("key"))
        elif action == "pet-slot" and args.get("key") in PET_EXPRESSIONS:
            self._asset_discard()
            self._pet_preview_key = args["key"]
            self._appearance_changed()
        elif action == "asset-reset":
            self._asset_reset(args.get("key"))
        elif action == "asset-save":
            self._asset_save()
        elif action == "asset-discard":
            self._asset_discard()

    def _data_result(self, key: str, value: dict, request: int = 0, **extra: Any) -> None:
        if "_error" not in value:
            self._data[key] = value
        self.port.publish("data", {"key": key, "value": value, "request": request, **extra})

    def _load(self, args: dict) -> None:
        key = args.get("key")
        if key not in {"plans", "memory", "tools", "plugins", "role", "settings", "policy", "mobile"}:
            return
        offset = args.get("offset", 0)
        request = args.get("request", 0)
        if type(offset) is not int or offset < 0 or type(request) is not int:
            return
        params: dict[str, Any] = {}
        operation = {"plugins": "plugins_catalog", "role": "interaction_snapshot", "settings": "settings", "mobile": "mobile_status",
                     "policy": "notification_policy"}.get(key, "")
        tab = args.get("tab", "")
        if key == "plans":
            if tab == "upcoming":
                operation = "schedule_items"
                params = {"source": "all", "status": "upcoming", "offset": offset, "limit": 50}
            elif tab in {"ongoing", "completed"}:
                operation = "tasks"
                params = {"statuses": ACTIVE_TASKS if tab == "ongoing" else ["Completed"],
                          "offset": offset, "limit": 50}
            else:
                return
        if key == "memory":
            group = {"facts": "facts", "episodes": "episode", "buffer": "buffer"}.get(tab)
            query = args.get("query", "")
            if group is None or not isinstance(query, str) or len(query) > 200:
                return
            operation = "memories"
            params = {"group": group, "query": query, "offset": offset, "limit": 50}
        if key == "tools":
            operation = "tools_catalog"
            group = args.get("group", "all")
            if not isinstance(group, str) or len(group) > 200:
                return
            previous = self._data.get("tools", {})
            cursors = previous.get("_cursors", {}) if previous.get("_group") == group else {}
            cursor = cursors.get(offset) if offset else None
            if offset and cursor is None:
                return
            params = {"plugin_id": group if group != "all" else None, "cursor": cursor, "limit": 50}
        version = self._load_versions.get(key, 0) + 1
        self._load_versions[key] = version

        def done(data: dict[str, Any]) -> None:
            if self._load_versions.get(key) != version:
                return
            if key == "tools" and "_error" not in data:
                next_cursor = data.get("next_cursor")
                data = {**data, "_group": group, "_cursors": {**cursors, offset + 50: next_cursor}}
            self._data_result(key, data, request)

        if self.operations.send(operation, params, done) is None:
            self._data_result(key, {"_error": True}, request)

    def _save_card(self, value: Any) -> None:
        snapshot = self._data.get("role", {})
        if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 8192 or "revision" not in snapshot:
            self._toast("请填写不超过约 8 KiB 的角色卡。")
            return

        def done(data: dict) -> None:
            if "_error" not in data:
                self._role_dirty = False
                self._data_result("role", data, saved=True)
                self._toast("角色卡已保存。")

        self.operations.send("interaction_command", {"action": "character_card", "text": value.strip(),
                             "expected_revision": snapshot["revision"]}, done, mutation=True)

    def _set_preference(self, args: dict) -> None:
        key, value = args.get("key"), args.get("value")
        if key in {"quiet_start", "quiet_end", "quiet_enabled", "reminder_bypass"}:
            self._quiet(key, value)
            return
        if key == "density" and value in {"comfortable", "compact"}:
            QSettings().setValue("gui/studio_density", value)
            self._prefs()
        elif key in PREFERENCES:
            if key == "pet_scale":
                if not isinstance(value, (str, int, float)) or type(value) is bool:
                    return
                try:
                    value = float(value) / 100
                except (ValueError, TypeError):
                    return
            if key == "pet_visible" and value is True and not self.preferences.pet_enabled:
                self.preferences.set_pet_enabled(True)
            if not getattr(self.preferences, "set_" + key)(value):
                self._toast("设置未保存，请重试。")
                self._prefs()

    def _quiet(self, key: str, value: Any) -> None:
        existing = self._data.get("policy", {}).get("quiet")
        if not isinstance(existing, dict):
            self._load({"key": "policy"})
            return
        quiet = dict(self._quiet_target or existing)
        if key == "quiet_enabled" and type(value) is bool:
            if value:
                quiet["quiet_start"], quiet["quiet_end"] = self._quiet_last
            else:
                if quiet["quiet_start"] != quiet["quiet_end"]:
                    self._quiet_last = (quiet["quiet_start"], quiet["quiet_end"])
                quiet["quiet_start"] = quiet["quiet_end"]
        elif key == "reminder_bypass" and type(value) is bool:
            quiet[key] = value
        elif key in {"quiet_start", "quiet_end"} and isinstance(value, str):
            try:
                hour, minute = map(int, value.split(":"))
            except ValueError:
                return
            if not 0 <= hour < 24 or not 0 <= minute < 60:
                return
            quiet[key] = hour * 60 + minute
        else:
            return
        self._quiet_target = quiet
        self._send_quiet()

    def _send_quiet(self) -> None:
        if self._quiet_pending or self._quiet_target is None:
            return
        sent = self._quiet_target
        self._quiet_pending = True

        def done(data: dict) -> None:
            self._quiet_pending = False
            if self._quiet_target is sent:
                self._quiet_target = None
                if "_error" not in data:
                    self._data_result("policy", data)
                else:
                    self._load({"key": "policy"})
            else:
                self._send_quiet()

        params: dict[str, Any] = {key: sent[key] for key in ("quiet_start", "quiet_end", "reminder_bypass") if key in sent}
        if self.operations.send("notification_quiet", params, done, mutation=True) is None:
            self._quiet_pending = False
            self._quiet_target = None

    def _cleanup(self) -> None:
        def status(data: dict) -> None:
            if "_error" not in data:
                self._toast("隐私清理已完成。" if data.get("status") == "Completed" else "有清理任务尚未完成。")

        self.operations.send("cleanup_status", {}, status)
        if self.lifecycle == "CleanupBlocked":
            def checked(_: dict[str, Any]) -> None:
                self.operations.send("cleanup_status", {}, status)
            self.operations.send("retry_cleanup", {}, checked)

    def _tool(self, identity: Any) -> None:
        catalog = self._data.get("tools", {})
        if any(item.get("id") == identity for item in catalog.get("items", [])):
            self.operations.send("tool_detail", {"id": identity, "catalog_revision": catalog.get("catalog_revision")},
                                 lambda data: self.port.publish("tool", data) if "_error" not in data else None)

    def _plugin(self, action: str, args: dict) -> None:
        identity = args.get("id")
        if not any(item.get("id") == identity and item.get("page_title") for item in self._data.get("plugins", {}).get("items", [])):
            return

        def load() -> None:
            self.operations.send("plugin_page", {"plugin_id": identity},
                                 lambda data: self.port.publish("plugin", data))

        if action == "plugin-page":
            load()
        elif type(args.get("value")) is bool and type(args.get("revision")) is int:
            self.operations.send("plugin_set_option", {"plugin_id": identity, "key": args.get("key"),
                                 "value": args["value"], "expected_revision": args["revision"]}, lambda _: load())

    def _linked_action(self, action: str, args: dict) -> None:
        identity = args.get("id")
        if not isinstance(identity, str):
            return
        if action == "memory-chat":
            item: dict[str, Any] | None = next((row for row in self._data.get("memory", {}).get("items", []) if row.get("id") == identity), {})
            source = (item or {}).get("source", {})
            if source.get("session_id"):
                self.open_conversation(source["session_id"], source.get("task_id"))
        elif action == "event":
            item = next((row for task in self._visible_tasks() for row in scheduled_from_review(
                self.chat_coordinator.reviews.get(task, {})) if row.get("id") == identity), None)
            if item:
                self.port.publish("schedule-detail", item)
        else:
            item = next((row for row in self._data.get("plans", {}).get("items", []) if row.get("id") == identity), None)
            if item is None:
                return
            if args.get("tab") == "upcoming":
                target = self.identity or self.chat_coordinator.default_id
                if target:
                    existing = self.chat_coordinator.draft(target).text
                    title = item.get("title") or item.get("label") or "这项安排"
                    text = f"我想调整「{title}」："
                    if self.chat_coordinator.edit_draft(target, (existing + "\n" if existing else "") + text):
                        self.open_conversation(target)
            else:
                self.navigate("task", identity)

    def _appearance_changed(self) -> None:
        profiles = self.chat_coordinator.appearance.profiles
        self._data_result("appearance", {key: profiles.get(slot, {}) for key, slot in SLOTS.items()})
        key = self._pet_preview_key
        asset = profiles.get(SLOTS[key], {}).get("effective_asset_id")
        if asset:
            self._pet_preview_id = self._image_request({"asset_id": asset}, self._asset_generation)
        else:
            self._pet_preview_id = None
            pixmap = QPixmap()
            try:
                raw = resources.files("deskpal.gui").joinpath("assets", "pet", PET_EXPRESSIONS[key] + ".png").read_bytes()
                pixmap.loadFromData(raw, "PNG")
            except OSError:
                pass
            self.port.publish("image", {"key": key, "url": pixmap_url(pixmap.scaled(
                320, 320, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)) if not pixmap.isNull() else ""})

    def _image_request(self, params: dict, generation: int) -> str | None:
        try:
            return self.bridge.send_image("asset_preview", {**params, "max_edge": 320}, generation)
        except (ValueError, RuntimeError, BufferError):
            self._toast("图片通道暂不可用，请重试。")
            return None

    def _asset_pick(self, key: Any) -> None:
        if key not in SLOTS:
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择图片", "", "图片 (*.png *.jpg *.jpeg)")
        if not path:
            return
        self._asset_discard()
        generation = self._asset_generation
        slot = SLOTS[key]

        def done(data: dict) -> None:
            token = data.get("token")
            if not token:
                return
            if self._asset_generation != generation:
                self.operations.send("asset_import_discard", {"token": token})
                return
            self._asset_pending = {"token": token, "slot": slot, "generation": generation,
                                   "display_mode": "contain" if slot.startswith("pet_") else "circle",
                                   "crop": [0, 0, 1, 1]}
            self._asset_image = self._image_request({"token": token}, generation)

        self.operations.send("asset_import_prepare", {"path": path, "slot": slot, "generation": generation}, done)

    def _asset_reply(self, envelope: dict) -> None:
        if envelope.get("privacy_epoch") != self.bridge.privacy_epoch:
            return
        identity = envelope.get("request_id")
        if identity not in (self._pet_preview_id, self._asset_image):
            return
        data = envelope.get("image", {}).get("png")
        if not envelope.get("ok") or not isinstance(data, bytes):
            self._toast("无法预览图片。")
            if identity == self._asset_image:
                self._asset_discard()
            return
        if identity == self._pet_preview_id:
            self.port.publish("image", {"key": self._pet_preview_key, "url": png_url(data)})
        elif envelope.get("generation") == self._asset_generation:
            self.port.publish("asset-preview", {"url": png_url(data)})

    def _asset_save(self) -> None:
        pending = self._asset_pending
        profile = self.chat_coordinator.appearance.profiles.get(pending.get("slot", ""), {})
        if not pending or "revision" not in profile:
            return

        def done(data: dict) -> None:
            if "_error" not in data:
                if self._asset_pending is pending:
                    self._asset_pending = {}
                self.chat_coordinator.appearance.refresh()
                self._toast("图片已保存。")

        if self.operations.send("appearance_apply", {**pending, "expected_revision": profile["revision"]}, done) is None:
            self._toast("图片保存尚未受理。请重新选择图片再试。")
            self._asset_discard()

    def _asset_reset(self, key: Any) -> None:
        slot = SLOTS.get(key)
        profile = self.chat_coordinator.appearance.profiles.get(slot or "", {})
        if "revision" in profile:
            self.operations.send("appearance_reset", {"slot": slot, "expected_revision": profile["revision"]},
                                 lambda _: self.chat_coordinator.appearance.refresh())

    def _asset_discard(self) -> None:
        pending = self._asset_pending
        self._asset_pending = {}
        self._asset_image = None
        self._asset_generation += 1
        if pending:
            self.operations.send("asset_import_discard", {"token": pending["token"]})


class StudioPetChat(StudioChatSurface):
    def __init__(self, coordinator: ChatCoordinator) -> None:
        super().__init__(coordinator, "pet")
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle("Deskpal · 聊天")
        self.resize(410, 520)
        self.anchor: QWidget | None = None
        self._disposing = False
        self.load_studio()

    def open_for(self, anchor: QWidget) -> None:
        self.anchor = anchor
        self._select(self.chat_coordinator.default_id)
        self.reanchor()
        self.show()
        self.raise_()
        self.activateWindow()
        self.view.setFocus()

    def reanchor(self) -> None:
        if self.anchor is None:
            return
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QApplication

        area = self.anchor.screen().availableGeometry()
        self.resize(min(410, area.width()), min(520, area.height()))
        frame = self.anchor.frameGeometry()
        x = frame.right() + 12
        if x + self.width() > area.right():
            x = frame.left() - self.width() - 12
        pos = QPoint(x, frame.center().y() - self.height() // 2)
        self.move(clamp_position(pos, (self.width(), self.height()),
                                 [screen.availableGeometry() for screen in QApplication.screens()]))

    def changeEvent(self, event: Any) -> None:
        from PySide6.QtCore import QEvent

        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow() and self.isVisible():
            QTimer.singleShot(0, self, self._hide_if_inactive)

    def _hide_if_inactive(self) -> None:
        if not self.isActiveWindow():
            self.hide()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._disposing:
            self.attached = False
            self.release_studio()
            event.accept()
        else:
            event.ignore()
            self.hide()

    def dispose(self) -> None:
        self._disposing = True
        self.close()
        self.deleteLater()
