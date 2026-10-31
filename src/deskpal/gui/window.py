"""Warm five-entry shell; domain state belongs to shared coordinators."""
from typing import Any, overload
from PySide6.QtCore import QByteArray, QEvent, QSize, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QInputDialog, QMessageBox, QStackedWidget, QSizePolicy, QVBoxLayout, QWidget, QSplitter, QDialog)
from .theme import apply as apply_theme, configure
from .components import PushButton as QPushButton, ListWidget as QListWidget
from qfluentwidgets import FluentWindow, FluentIcon, NavigationItemPosition, qconfig
from .widgets import install_wheel_guard, notice_label, text_label
from .channel import Channel
from .chat import ChatCoordinator, ChatView
from .appearance import AppearanceController
from .settings import SettingsPage
from .catalog import CatalogPage
from .views import CommandForm
from .management import ArrangementsPage, MemoryPage
from .tool_results import ToolResultsPanel, has_tool_details

class MainWindow(FluentWindow):
    close_requested = Signal()
    privacy_cleared = Signal()
    maintenance_requested = Signal()
    profiles_requested = Signal()

    def __init__(self, bridge: Any, preferences: Any = None, parent: QWidget | None = None) -> None:
        configure()
        if preferences is not None:
            from .theme import select
            select(getattr(preferences, 'theme', 'auto'))
        super().__init__(parent)
        self.setMicaEffectEnabled(False)
        self.bridge = bridge; self.preferences = preferences; self.lifecycle = "Initializing"
        self._allow_close = False; self.defer_closed = False; self._ready_seen = False
        self._page_revision = 0
        self._geometry_fit_pending = False; self._screen_signals_connected = False
        self.setWindowTitle("Deskpal · 聊天与安排"); self.resize(1180, 800); self.setMinimumSize(560, 420)
        install_wheel_guard()
        self.navigationInterface.setExpandWidth(184)
        self.navigationInterface.setMinimumExpandWidth(940)
        self._wide_navigation: bool | None = None
        self.navigationInterface.setReturnButtonVisible(False)
        self.channel = Channel(bridge, self)
        self.chat_coordinator = ChatCoordinator(bridge, self)
        self.stack = self.stackedWidget
        # 页面状态与数据读取立即同步，避免切页动画尚未结束就刷新旧页。
        self.stack.setAnimationEnabled(False)
        self.chat_view = ChatView(self.chat_coordinator)
        self.arrangements = ArrangementsPage(self.chat_coordinator)
        self.memory_page = MemoryPage(bridge)
        self.catalog_page = CatalogPage(bridge)
        self.settings_page = SettingsPage(self.chat_coordinator, preferences, self.request_maintenance,
                                          profiles=self.request_profiles)
        self.profiles_button = self.settings_page.profiles_button
        self.nav_buttons = []
        for page, title, route, icon, position in (
            (self.chat_view, "聊天", "chat", FluentIcon.CHAT, NavigationItemPosition.TOP),
            (self.arrangements, "安排", "arrangements", FluentIcon.CALENDAR, NavigationItemPosition.TOP),
            (self.memory_page, "记忆", "memory", FluentIcon.BOOK_SHELF, NavigationItemPosition.TOP),
            (self.catalog_page, "工具插件", "tools", FluentIcon.APPLICATION, NavigationItemPosition.TOP),
            (self.settings_page, "设置", "settings", FluentIcon.SETTING, NavigationItemPosition.BOTTOM)):
            page.setObjectName(route)
            self.nav_buttons.append(self.addSubInterface(page, icon, title, position=position))
        # 内部详情与页面共享工作区，不再使用 QMainWindow 的停靠窗或第二列常驻目录。
        self.widgetLayout.removeWidget(self.stack)
        self.workspace = QWidget(self); self.workspace.setObjectName("deskpalWorkspace")
        outer = QVBoxLayout(self.workspace); outer.setContentsMargins(12, 8, 12, 12)
        self.state_label = text_label("正在准备本地数据…"); self.state_label.setObjectName("subtle")
        outer.addWidget(self.state_label)
        self.content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.addWidget(self.stack)
        outer.addWidget(self.content_splitter, 1)
        self._tool_results_task = ""
        self.tool_results = ToolResultsPanel(self)
        self.tool_results_dock = QFrame()
        self.tool_results_dock.setObjectName("tool_results_sidebar")
        self.tool_results_dock.setMinimumWidth(0)
        record_layout = QVBoxLayout(self.tool_results_dock)
        record_header = QHBoxLayout(); record_header.addWidget(text_label("执行记录", heading=True), 1)
        close_records = QPushButton("收起"); close_records.clicked.connect(self._clear_tool_results)
        record_header.addWidget(close_records); record_layout.addLayout(record_header)
        record_layout.addWidget(self.tool_results, 1)
        self.content_splitter.addWidget(self.tool_results_dock)
        self.content_splitter.setStretchFactor(0, 1)
        self.content_splitter.setStretchFactor(1, 0)
        self.tool_results_dock.hide()
        self.notice = notice_label(); outer.addWidget(self.notice)
        self.widgetLayout.addWidget(self.workspace)
        self.channel.notice.connect(self.notice.setText)
        self.chat_coordinator.notice.connect(self.notice.setText)
        self.chat_coordinator.conversations_changed.connect(self._directory)
        self.chat_coordinator.expand_requested.connect(lambda identity, task_id: self.open_conversation(identity, task_id=task_id or None))
        self.arrangements.open_conversation.connect(lambda identity, task_id: self.open_conversation(identity, task_id=task_id or None))
        self.arrangements.edit_schedule_requested.connect(self._edit_schedule)
        self.chat_view.conversation_selected.connect(lambda identity: self._directory())
        self.chat_coordinator.tool_results_requested.connect(self._open_tool_results)
        self.chat_coordinator.task_changed.connect(self._update_tool_results)
        self.chat_coordinator.reviews_cleared.connect(self._clear_tool_results)
        self.chat_view.conversation_selected.connect(self._tool_results_conversation)
        self.chat_view.directory_requested.connect(self.open_conversations)
        self.chat_view.execution_requested.connect(self._latest_tool_results)
        self._build_directory()
        self.stack.currentChanged.connect(self._page_changed)
        bridge.state_changed.connect(self.on_state_changed, Qt.ConnectionType.QueuedConnection)
        if hasattr(bridge, "privacy_changed"): bridge.privacy_changed.connect(self._privacy_changed, Qt.ConnectionType.QueuedConnection)
        self.poll_timer = QTimer(self); self.poll_timer.setInterval(3000); self.poll_timer.timeout.connect(self._poll)
        self.cleanup_timer = QTimer(self); self.cleanup_timer.setInterval(2000); self.cleanup_timer.timeout.connect(self._poll_cleanup)
        self._cleanup_request: str | None = None
        self._restore_geometry(); self._normal_client_size = self.size(); self._geometry_screen = self.screen(); self._geometry_dpr = self.devicePixelRatioF()
        apply_theme(self)
        qconfig.themeChanged.connect(self._apply_theme)
        self.set_page(0); self.on_state_changed(str(getattr(bridge, "state", "Initializing")))

    def _apply_theme(self) -> None:
        apply_theme(self)

    def _edit_schedule(self, item: dict[str, Any]) -> None:
        """先填入可编辑草稿，仍经用户发送、模型查找与原有工具权限执行。"""
        from .display_text import friendly_time
        identity = self.chat_view.conversation_id or self.chat_coordinator.default_id
        if not identity or not item: return
        stamp = item.get('start_at') or item.get('due_at')
        text = '我想调整“' + str(item.get('title') or item.get('label') or '这项安排') + '”'
        if stamp: text += '（' + friendly_time(stamp) + '）'
        text += '：'
        existing = self.chat_coordinator.draft(identity).text
        if self.chat_coordinator.edit_draft(identity, existing + ('\n' if existing else '') + text):
            self.open_conversation(identity); self.chat_view.focus_input()

    def _build_directory(self) -> None:
        self.directory_dialog = QDialog(self)
        self.directory_dialog.setWindowTitle("当前角色的会话")
        self.directory_dialog.resize(440, 480)
        layout = QVBoxLayout(self.directory_dialog)
        layout.addWidget(text_label("会话", heading=True))
        create = QPushButton("＋ 新建专项会话"); create.clicked.connect(self._new_conversation); layout.addWidget(create)
        self.conversations = QListWidget(); self.conversations.currentRowChanged.connect(self._conversation_selected)
        layout.addWidget(self.conversations, 1)
        self.archived = QPushButton("查看归档会话"); self.archived.setCheckable(True); self.archived.toggled.connect(self._archives)
        layout.addWidget(self.archived)
        row = QHBoxLayout()
        for title, callback in (("重命名", self._rename), ("归档 / 恢复", self._archive), ("删除", self._delete)):
            button = QPushButton(title); button.clicked.connect(callback); row.addWidget(button)
        layout.addLayout(row)
        self.directory_more = QPushButton("更多会话"); self.directory_more.clicked.connect(lambda: self.chat_coordinator.refresh_directory(True)); layout.addWidget(self.directory_more)
        done = QPushButton("完成"); done.clicked.connect(self.directory_dialog.accept); layout.addWidget(done)

    def open_conversations(self) -> None:
        if self.lifecycle != "Ready": return
        self._directory(); self.chat_coordinator.refresh_directory()
        self.directory_dialog.open()

    def _page_changed(self, index: int) -> None:
        self._page_revision += 1
        if hasattr(self, 'directory_dialog'): self.directory_dialog.hide()
        if index > 1: self._clear_tool_results()
        self._responsive(); self._refresh_current_page()

    def _latest_tool_results(self) -> None:
        items = self.chat_coordinator.timelines.get(self.chat_view.conversation_id, [])
        identity = next((str(item.get('task_id')) for item in reversed(items) if item.get('task_id')), '') or self.chat_view.task_id
        if not identity:
            self.notice.setText("这个会话还没有执行记录。")
            return
        self._tool_results_task = identity
        self.chat_coordinator.watch_review(id(self.tool_results), identity)
        self.chat_coordinator.review(identity)
        self._open_tool_results(identity)

    def set_page(self, index: int) -> None:
        previous = self.stack.currentIndex()
        self.stack.setCurrentIndex(index)
        page = self.stack.widget(index)
        if page is not None: self.navigationInterface.setCurrentItem(page.objectName())
        if previous == index: self._page_changed(index)

    def _refresh_current_page(self) -> None:
        if self.lifecycle != "Ready": return
        index = self.stack.currentIndex()
        if index == 0: self.chat_coordinator.load(self.chat_view.conversation_id)
        elif index == 1: self.arrangements.refresh()
        elif index == 2: self.memory_page.refresh()
        elif index == 3: self.catalog_page.refresh()
        else: self.settings_page.refresh()

    def _responsive(self) -> None:
        if not hasattr(self, 'tool_results_dock'): return
        wide = self.width() >= 940
        if wide != self._wide_navigation:
            self._wide_navigation = wide
            if wide: self.navigationInterface.expand(useAni=False)
        details_open = not self.tool_results_dock.isHidden()
        # 窄窗以详情作为当前视图，收起后回到原页面，不把输入区挤成窄条。
        self.stack.setVisible(not (details_open and self.width() < 940))
        self.chat_view.set_selector_visible(False)
        if details_open and self.width() >= 940:
            self.content_splitter.setSizes([max(350, self.content_splitter.width() - 340), 340])

    def _open_tool_results(self, task_id: str) -> None:
        review = self.chat_coordinator.reviews.get(task_id, {})
        if not self.chat_coordinator.available or review.get("review_unavailable") or not has_tool_details(review):
            return
        self._tool_results_task = task_id
        self.chat_coordinator.watch_review(id(self.tool_results), task_id)
        self.tool_results.update_review(review)
        self.tool_results_dock.show()
        self._responsive()

    def _update_tool_results(self, task_id: str) -> None:
        if task_id != self._tool_results_task:
            return
        review = self.chat_coordinator.reviews.get(task_id, {})
        if not review:
            return  # 首次读取尚未返回，不能释放正在等待的任务订阅。
        if review.get("review_unavailable") or not has_tool_details(review):
            self._clear_tool_results()
        else:
            self.tool_results.update_review(review)
            self.tool_results_dock.show()
            self._responsive()

    def _clear_tool_results(self) -> None:
        self._tool_results_task = ""
        self.chat_coordinator.watch_review(id(self.tool_results), None)
        self.tool_results.invalidate()
        self.tool_results_dock.hide()
        self._responsive()

    def _tool_results_conversation(self, identity: str) -> None:
        review = self.chat_coordinator.reviews.get(self._tool_results_task, {})
        if self._tool_results_task and review.get("conversation_id") != identity:
            self._clear_tool_results()

    def _directory(self) -> None:
        self.conversations.blockSignals(True); self.conversations.clear(); self._conversation_ids = []
        for identity, item in self.chat_coordinator.conversations.items():
            if self.chat_coordinator.archived != bool(item.get("archived_at") or item.get("archived", False)) and identity != self.chat_coordinator.default_id: continue
            self._conversation_ids.append(identity); self.conversations.addItem(str(item.get('title', '专项会话')) + (' · 固定' if identity == self.chat_coordinator.default_id else ''))
        if self.chat_view.conversation_id in self._conversation_ids: self.conversations.setCurrentRow(self._conversation_ids.index(self.chat_view.conversation_id))
        self.conversations.blockSignals(False); self.directory_more.setVisible(self.chat_coordinator.directory_more)

    def _conversation_selected(self, row: int) -> None:
        if 0 <= row < len(self._conversation_ids): self.chat_view.set_conversation(self._conversation_ids[row])
    def _new_conversation(self) -> None:
        text, ok = QInputDialog.getText(self, "新建专项会话", "给这段话题起个名字")
        if ok and text.strip(): self.chat_coordinator.create(text.strip())
    def _rename(self) -> None:
        identity = self.chat_view.conversation_id
        if identity == self.chat_coordinator.default_id: self.notice.setText("日常聊天名称固定，专项会话可以重命名。"); return
        item = self.chat_coordinator.conversations.get(identity, {}); text, ok = QInputDialog.getText(self, "重命名会话", "会话名称", text=str(item.get("title", "")))
        if ok and text.strip(): self.chat_coordinator.update_conversation(identity, title=text.strip())
    def _archive(self) -> None:
        identity = self.chat_view.conversation_id
        if identity == self.chat_coordinator.default_id: self.notice.setText("日常聊天是固定入口，无需归档。"); return
        item = self.chat_coordinator.conversations.get(identity, {}); self.chat_coordinator.update_conversation(identity, archived=not bool(item.get("archived_at") or item.get("archived", False)))
    def _archives(self, archived: bool) -> None:
        self.chat_coordinator.archived = archived; self.archived.setText("返回当前会话" if archived else "查看归档会话"); self.chat_coordinator.refresh_directory()
    def _delete(self) -> None:
        identity = self.chat_view.conversation_id
        if identity == self.chat_coordinator.default_id: self.notice.setText("日常聊天保留固定入口，可在设置中清空记录。"); return
        if identity and QMessageBox.question(self, "删除专项会话", "删除这段会话及其相关任务正文？此操作无法撤销。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes: self.chat_coordinator.remove_conversation(identity, True)

    def open_conversation(self, id: str, task_id: str | None = None) -> None:
        self.set_page(0); self.chat_view.set_conversation(id or self.chat_coordinator.default_id, task_id); self._directory(); self.chat_view.focus_input()

    def navigate(self, kind: str, entity_id: str) -> None:
        self.showNormal(); self.raise_(); self.activateWindow()
        if kind == "reminder":
            self.channel.send("reminder_detail", {"id": entity_id}, self._reminder_navigation)
        elif kind == "task":
            self.channel.send("task_review", {"task_id": entity_id}, lambda data: self._task_navigation(entity_id, data))
        elif kind == "proposal":
            self.set_page(1); self.arrangements.tabs.setCurrentIndex(2)
            self.channel.send("proposal", {"proposal_id": entity_id}, self._proposal_navigation)
        elif kind in {"conversation", "chat"}: self.open_conversation(entity_id)

    def navigate_to(self, kind: str, entity_id: str) -> None:
        self.navigate(kind, entity_id)
    def _reminder_navigation(self, data: dict[str, Any]) -> None:
        if "_error" in data: return
        item = data.get("item", data)
        if item.get("conversation_id"): self.open_conversation(str(item["conversation_id"]), str(item.get("task_id") or "") or None)
        else: self.set_page(1); self.arrangements.tabs.setCurrentIndex(0); self.arrangements.schedule_tabs.setCurrentIndex(1); self.arrangements.reminders.show_detail(item)
    def _task_navigation(self, identity: str, data: dict[str, Any]) -> None:
        if "_error" in data: return
        self.chat_coordinator.set_review(identity, data)
        if data.get("conversation_id"): self.open_conversation(str(data["conversation_id"]), identity)
        else:
            self.set_page(1); self.arrangements.tabs.setCurrentIndex(1); self.arrangements.task_tabs.setCurrentIndex(0)
            self.arrangements._task({"id": identity, **data.get("task", {})})
    def _proposal_navigation(self, data: dict[str, Any]) -> None:
        if "_error" not in data: self.arrangements.proposals.set_page({"items": [data]}); self.arrangements.proposals.list.setCurrentRow(0)

    def _restore_geometry(self) -> None:
        if self.preferences is not None and self.preferences.geometry:
            if self.restoreGeometry(QByteArray(self.preferences.geometry)):
                # Qt restores the saved frame position. Moving again before
                # show would apply the native title-bar offset twice.
                return
        screens = QApplication.screens()
        frame = self.frameGeometry()
        if screens:
            screen = next((item for item in screens if item.availableGeometry().intersects(frame)), screens[0])
            available = screen.availableGeometry()
            self.resize(min(self.width(), available.width()), min(self.height(), available.height()))
            self.move(max(available.left(), min(self.x(), available.right() - self.width() + 1)),
                      max(available.top(), min(self.y(), available.bottom() - self.height() + 1)))

    def _save_geometry(self) -> None:
        if self.preferences is not None:
            geometry = self.saveGeometry().data()
            setter = getattr(self.preferences, "set_geometry", None)
            if setter is not None:
                setter(geometry)
            else:
                self.preferences.geometry = geometry
            if self.screen() is not None:
                setter = getattr(self.preferences, "set_screen_name", None)
                if setter is not None:
                    setter(self.screen().name())
                else:
                    self.preferences.screen_name = self.screen().name()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None and not self._screen_signals_connected:
            self._screen_signals_connected = True
            handle.screenChanged.connect(self._queue_geometry_fit)
            for screen in QApplication.screens():
                screen.availableGeometryChanged.connect(self._queue_geometry_fit)
        self._queue_geometry_fit()

    def event(self, event: QEvent) -> bool:
        result = super().event(event)
        if event.type() in {QEvent.Type.DevicePixelRatioChange, QEvent.Type.FontChange}:
            self._queue_geometry_fit()
        return bool(result)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._responsive()
        # Preserve the user's logical client size, not Windows' DPI-transition
        # resize (whose decoration rounding can add pixels on each round trip).
        if (getattr(self, "_screen_signals_connected", False)
                and not self._geometry_fit_pending
                and self.screen() == self._geometry_screen
                and self.devicePixelRatioF() == self._geometry_dpr
                and not self.isMinimized() and not self.isMaximized()):
            self._normal_client_size = event.size()

    @overload
    def resize(self, arg__1: QSize, /) -> None: ...

    @overload
    def resize(self, w: int, h: int, /) -> None: ...

    def resize(self, w: QSize | int, h: int | None = None, /) -> None:
        if isinstance(w, QSize):
            size = w
        elif h is not None:
            size = QSize(w, h)
        else:
            raise TypeError("resize requires a QSize or width and height")
        if hasattr(self, "_normal_client_size"):
            self._normal_client_size = size
        super().resize(size)

    def _queue_geometry_fit(self, _: object = None) -> None:
        if getattr(self, "_geometry_fit_pending", True):
            return
        self._geometry_fit_pending = True
        QTimer.singleShot(0, self, self._fit_screen)

    def _fit_screen(self) -> None:
        self._geometry_fit_pending = True
        try:
            screen = self.screen()
            if screen is None or not self.isVisible() or self.isMinimized() or self.isMaximized():
                return
            available = screen.availableGeometry()
            frame = self.frameGeometry()
            # Keep the guard throughout resize: a temporary same-screen clamp
            # must not become the user's saved normal client size.
            super().resize(min(self._normal_client_size.width(), max(1, available.width() - (frame.width() - self.width()))),
                           min(self._normal_client_size.height(), max(1, available.height() - (frame.height() - self.height()))))
            frame = self.frameGeometry()
            self.move(max(available.left(), min(frame.left(), available.right() - frame.width() + 1)),
                      max(available.top(), min(frame.top(), available.bottom() - frame.height() + 1)))
            self._geometry_screen = screen
            self._geometry_dpr = self.devicePixelRatioF()
        finally:
            self._geometry_fit_pending = False

    @Slot(str)
    def on_state_changed(self, state: str) -> None:
        if state == "Closed" and self.defer_closed: state = "Closing"
        if self.lifecycle == "Closed": return
        first_ready = state == "Ready" and not self._ready_seen
        became_ready = state == "Ready" and self.lifecycle != "Ready"
        self.lifecycle = state
        self.state_label.setVisible(state != "Ready")
        self.state_label.setText({"Initializing": "正在准备本地数据…", "Ready": "Deskpal · 聊天与安排", "CleanupBlocked": "隐私清理未完成：相关正文已遮蔽，请在设置重试清理。", "StartupFailed": "启动失败，请从设置进入配置与数据维护。", "Closing": "正在关闭，等待后台与图片资源释放…", "Closed": "已关闭"}.get(state, state))
        self.settings_page.companion.set_available(state == "Ready")
        if state == "Ready":
            self._ready_seen = True; self.poll_timer.start(); self.chat_coordinator.appearance.refresh()
            if first_ready:
                if not self.chat_view.conversation_id: self.chat_view.show_default()
                revision = self._page_revision
                self.channel.send("settings", {}, lambda data: self._initial_settings(data, revision))
            if became_ready: self._refresh_current_page()
        else: self.poll_timer.stop()
        if state != "CleanupBlocked": self.cleanup_timer.stop()
        if state in {"CleanupBlocked", "Closing", "Closed", "StartupFailed"}: self.clear_sensitive()
        if state == "CleanupBlocked":
            self.set_page(4); self.settings_page.tabs.setCurrentIndex(6); self._cleanup_request = None
            self.cleanup_timer.start(); self._poll_cleanup()
        if state == "Closed": self._allow_close = True; self.close()

    def _initial_settings(self, data: dict[str, Any], page_revision: int) -> None:
        if (self.lifecycle == "Ready" and self._page_revision == page_revision
                and self.stack.currentIndex() == 0 and data.get("llm_ready") is False):
            self.set_page(4); self.settings_page.tabs.setCurrentIndex(4)
    def _poll(self) -> None:
        if not self.isVisible(): return
        if self.stack.currentIndex() == 1: self.arrangements.refresh()
        elif self.stack.currentIndex() == 4: self.settings_page.companion.poll()
    def _privacy_changed(self, epoch: int) -> None:
        self.notice.clear(); self.privacy_cleared.emit()
        QTimer.singleShot(0, self, self._refresh_after_privacy)
    def _refresh_after_privacy(self) -> None:
        if self.lifecycle == "Ready" and getattr(self.bridge, "state", "") == "Ready":
            if self.stack.currentIndex() != 4: self.settings_page.interaction.refresh()
            self._refresh_current_page()
    def _poll_cleanup(self) -> None:
        if self.lifecycle != "CleanupBlocked": return
        if self._cleanup_request in self.settings_page.channel.pending: return
        self._cleanup_request = self.settings_page.channel.send("cleanup_status", {}, self._cleanup_progress)
    def _cleanup_progress(self, data: dict[str, Any]) -> None:
        self._cleanup_request = None
        if self.lifecycle in {"Closing", "Closed", "StartupFailed"}: return
        self.settings_page._cleanup_status(data)
        if "_error" in data: return
        if data.get("status") == "Completed" and not data.get("blocked"):
            self.notice.setText("隐私清理已完成，原清理范围的正文不会恢复。")
        else:
            self.notice.setText(f"相关正文已遮蔽，隐私清理进行中；剩余 {len(data.get('pending', []))} 项工作。")
    def clear_sensitive(self) -> None:
        self._clear_tool_results()
        self.directory_dialog.hide()
        for channel in self.findChildren(Channel): channel.invalidate()
        self._cleanup_request = None
        self.notice.clear(); self.chat_coordinator.clear_sensitive(); self.memory_page.clear_sensitive(); self.arrangements.clear_sensitive(); self.catalog_page.clear_sensitive(); self.settings_page.interaction.clear_sensitive(); self.settings_page._privacy()
    def has_unsaved_content(self) -> bool:
        return (any(draft.text.strip() for draft in self.chat_coordinator.drafts.values())
                or any(form.dirty for form in self.findChildren(CommandForm))
                or self.arrangements.goals.has_unsaved_content()
                or self.settings_page.interaction.dirty
                or self.settings_page.companion._config_dirty
                or bool(self.settings_page.appearance.token))

    def confirm_exit(self) -> bool:
        if self.lifecycle in {"Closing", "Closed", "StartupFailed"} or not self.has_unsaved_content(): return True
        return QMessageBox.question(self, "仍有未发送或未保存内容", "聊天草稿、未保存的设置或表单只保留在本次运行。放弃这些内容并退出？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    def request_maintenance(self) -> None:
        if self.has_unsaved_content():
            if QMessageBox.question(self, "进入维护前处理草稿", "进入维护会关闭本次生产会话。未发送聊天草稿、未保存设置和表单不会转移到重开的应用。放弃这些内容并进入维护？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes: return
        self.maintenance_requested.emit()

    def request_profiles(self) -> None:
        if self.has_unsaved_content():
            if QMessageBox.question(self, "进入角色档案", "切换前会关闭当前后台，未发送草稿和未保存设置不会转移。放弃这些内容并进入档案？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes: return
        self.profiles_requested.emit()

    def request_exit(self) -> None:
        self.close_requested.emit()
    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_geometry()
        if self._allow_close: event.accept()
        else: event.ignore(); self.close_requested.emit()
