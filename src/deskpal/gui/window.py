"""Chinese desktop window. All application work goes through the asynchronous bridge."""

from typing import Any, overload
import re

from PySide6.QtCore import QByteArray, QEvent, QSize, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel,
    QMainWindow, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

from .viewmodels import PendingRequest, RequestTracker, plain_json, tool_permissions
from .views import CommandForm, PagedView


class MainWindow(QMainWindow):
    close_requested = Signal()
    privacy_cleared = Signal()

    def __init__(self, bridge: Any, preferences: Any = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.preferences = preferences
        self.requests = RequestTracker()
        self.lifecycle = "Initializing"
        self.privacy_barrier = False
        self.forget_state = ""
        self.task_snapshot: dict[str, Any] = {}
        self.memory_snapshot: dict[str, Any] = {}
        self.proposal_snapshot: dict[str, Any] = {}
        self._allow_close = False
        self._ready_seen = False
        self._task_signature: tuple[Any, ...] = ()
        self._ordinary: dict[str, QCheckBox] = {}
        self._high: dict[str, QCheckBox] = {}
        self._forms: list[CommandForm] = []
        self._entity_id = ""
        self._chat_task_id = ""
        self._chat_result_revision = 0
        self._geometry_fit_pending = False
        self._screen_signals_connected = False
        self._page_scrolls: list[QScrollArea] = []
        self.setWindowTitle("Deskpal · 本地桌面助手")
        self.resize(1080, 780)
        root = QWidget()
        layout = QVBoxLayout(root)
        self.state_label = QLabel("正在初始化，您可以查看诊断或退出。")
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)
        self.notice = QLabel("")
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        controls = QHBoxLayout()
        self.diagnostics_button = QPushButton("诊断")
        self.retry_button = QPushButton("重试清理 / 关闭")
        self.exit_button = QPushButton("退出")
        controls.addWidget(self.diagnostics_button)
        controls.addWidget(self.retry_button)
        controls.addStretch()
        controls.addWidget(self.exit_button)
        layout.addLayout(controls)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.setCentralWidget(root)
        self._build_chat()
        self._build_tasks()
        self._build_memory()
        self._build_management()
        self._build_proposals()
        self._build_settings()
        self.diagnostics = QPlainTextEdit()
        self.diagnostics.setReadOnly(True)
        self.diagnostics.document().setMaximumBlockCount(500)
        self._add_page(self.diagnostics, "诊断")
        self.diagnostics_button.clicked.connect(self.show_diagnostics)
        self.retry_button.clicked.connect(self.retry_cleanup)
        self.exit_button.clicked.connect(self.request_exit)
        self.tabs.currentChanged.connect(self._tab_changed)
        bridge.state_changed.connect(self.on_state_changed, Qt.ConnectionType.QueuedConnection)
        bridge.reply.connect(self.on_reply, Qt.ConnectionType.QueuedConnection)
        if hasattr(bridge, "privacy_changed"):
            bridge.privacy_changed.connect(self._privacy_changed, Qt.ConnectionType.QueuedConnection)
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(2000)
        self.poll_timer.timeout.connect(self._poll)
        self._restore_geometry()
        self._normal_client_size = self.size()
        self._geometry_screen = self.screen()
        self._geometry_dpr = self.devicePixelRatioF()
        self._update_enabled()

    def _add_page(self, page: QWidget, title: str) -> None:
        # Hidden tabs also contribute minimumSizeHint. A viewport keeps long
        # forms accessible without forcing the native window taller than a screen.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self._page_scrolls.append(scroll)
        self.tabs.addTab(scroll, title)

    def _build_chat(self) -> None:
        self.chat_page = QWidget()
        layout = QVBoxLayout(self.chat_page)
        hint = QLabel("◈ Deskpal\n每次输入作为独立请求处理。工具操作需在任务页审核原计划后授权。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.chat_history = QPlainTextEdit()
        self.chat_history.setReadOnly(True)
        self.chat_history.document().setMaximumBlockCount(400)
        layout.addWidget(self.chat_history)
        self.chat_input = QPlainTextEdit()
        self.chat_input.setObjectName("chat_input")
        self.chat_input.setPlaceholderText("输入请求（最多 20,000 字符）")
        self.chat_input.setMaximumHeight(120)
        layout.addWidget(self.chat_input)
        row = QHBoxLayout()
        self.use_policy = QComboBox()
        self.use_policy.addItems(["local_only", "cloud_allowed"])
        self.submit_button = QPushButton("提交请求")
        self.submit_button.setObjectName("submit_request")
        row.addWidget(QLabel("本次请求数据用途"))
        row.addWidget(self.use_policy)
        row.addStretch()
        row.addWidget(self.submit_button)
        layout.addLayout(row)
        self.policy_hint = QLabel("local_only 会阻止整次云规划；cloud_allowed 允许本次请求及合规记忆用于云模型。请按本次请求主动选择。")
        self.policy_hint.setWordWrap(True)
        layout.addWidget(self.policy_hint)
        self.submit_button.clicked.connect(self.submit_request)
        self._add_page(self.chat_page, "对话")

    def _build_tasks(self) -> None:
        self.tasks = PagedView("tasks")
        self.tasks.refresh_requested.connect(lambda: self.reload_page("tasks", explicit=True))
        self.tasks.selected.connect(self._task_selected)
        self.authorization = QGroupBox("审核原计划后，分别选择本次授权（默认全部未选）")
        grant_layout = QVBoxLayout(self.authorization)
        self.hash_label = QLabel("尚未展示可确认计划")
        self.hash_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.hash_label.setTextFormat(Qt.TextFormat.PlainText)
        self.hash_label.setWordWrap(True)
        grant_layout.addWidget(self.hash_label)
        self.permission_widget = QWidget()
        self.permission_layout = QHBoxLayout(self.permission_widget)
        self.ordinary_group = QGroupBox("普通工具确认")
        self.ordinary_layout = QVBoxLayout(self.ordinary_group)
        self.high_group = QGroupBox("高风险工具：额外显式授权")
        self.high_layout = QVBoxLayout(self.high_group)
        self.permission_layout.addWidget(self.ordinary_group)
        self.permission_layout.addWidget(self.high_group)
        grant_layout.addWidget(self.permission_widget)
        self.tasks.layout().addWidget(self.authorization)  # type: ignore[union-attr]
        self.confirm_button = QPushButton("确认当前 hash 的所选授权")
        self.resume_button = QPushButton("重新审核 / 恢复")
        self.cancel_button = QPushButton("请求取消")
        for button in (self.confirm_button, self.resume_button, self.cancel_button):
            self.tasks.action_bar.addWidget(button)
        self.tasks.action_bar.addStretch()
        self.confirm_button.clicked.connect(self.confirm_plan)
        self.resume_button.clicked.connect(lambda: self._task_command("resume"))
        self.cancel_button.clicked.connect(lambda: self._task_command("cancel"))
        self.uncertain_form = CommandForm(
            "不确定结果需人工证据审查；请求取消不代表工具已强制停止。",
            [("verdict", "审查结论", ["unknown", "occurred", "not_occurred"]),
             ("evidence", "人工证据", None)])
        self.uncertain_form.submitted.connect(self._review_uncertain)
        self.tasks.layout().addWidget(self.uncertain_form)  # type: ignore[union-attr]
        self._add_page(self.tasks, "任务与授权")

    def _build_memory(self) -> None:
        self.memories = PagedView("memories")
        self.memory_filter = QComboBox()
        self.memory_filter.addItems(["all", "candidate", "accepted", "disputed", "rejected", "deleted"])
        self.memories.toolbar.insertWidget(0, self.memory_filter)
        self.memory_filter.currentTextChanged.connect(self._memory_filter_changed)
        self.memories.refresh_requested.connect(lambda: self.reload_page("memories", explicit=True))
        self.memories.selected.connect(self._memory_selected)
        self.memory_accept = QPushButton("接受候选")
        self.memory_reject = QPushButton("拒绝候选")
        self.memory_forget = QPushButton("遗忘选中记忆及相关正文")
        for button in (self.memory_accept, self.memory_reject, self.memory_forget):
            self.memories.action_bar.addWidget(button)
        self.memory_accept.clicked.connect(lambda: self.memory_action("accept"))
        self.memory_reject.clicked.connect(lambda: self.memory_action("reject"))
        self.memory_forget.clicked.connect(lambda: self.memory_action("forget"))
        self.memory_edit = CommandForm("更正将使用所选记忆的当前版本号；来源和用途见上方完整详情。",
                                       [("content", "更正内容", None)])
        self.memory_edit.submit.setText("提交更正")
        self.memory_edit.submitted.connect(lambda values: self.memory_action("correct", values))
        self.memory_remember = CommandForm("明确保存新记忆；候选审核、来源与用途仍遵循现有后端规则。", [
            ("content", "记忆内容", None), ("use_policy", "使用策略", ["local_only", "cloud_allowed"])])
        self.memory_remember.submit.setText("保存新记忆")
        self.memory_remember.submitted.connect(self.remember_memory)
        self.memory_forms = QTabWidget()
        self.memory_forms.addTab(self.memory_edit, "更正所选记忆")
        self.memory_forms.addTab(self.memory_remember, "保存新记忆")
        self.memories.layout().addWidget(self.memory_forms)  # type: ignore[union-attr]
        self._add_page(self.memories, "记忆")

    def _build_management(self) -> None:
        self.management = QTabWidget()
        self.goals = PagedView("goals")
        self.milestones = PagedView("milestones")
        self.bindings = PagedView("bindings")
        self.pages = {"tasks": self.tasks, "memories": self.memories,
                      "goals": self.goals, "milestones": self.milestones, "bindings": self.bindings}
        self.goal_filter = QComboBox()
        self.goal_filter.setEditable(True)
        self.goal_filter.setPlaceholderText("父目标 ID（里程碑 / 来源绑定）")
        for name, page, title in (("goals", self.goals, "目标"),
                                   ("milestones", self.milestones, "里程碑"),
                                   ("bindings", self.bindings, "来源绑定")):
            page.refresh_requested.connect(lambda name=name: self.reload_page(name, explicit=True))
            page.selected.connect(lambda item, name=name: self._management_selected(name, item))
            self.management.addTab(page, title)
        parent_box = QWidget()
        parent_layout = QVBoxLayout(parent_box)
        parent_layout.addWidget(QLabel("目标完成需人工证据；任务完成不会自动完成目标。"))
        parent_layout.addWidget(self.goal_filter)
        parent_layout.addWidget(self.management)
        self.goal_filter.currentTextChanged.connect(self._parent_goal_changed)
        self.management.currentChanged.connect(lambda _: self._management_tab_changed())
        common = [("title", "标题", None), ("success_criteria", "成功标准", None),
                  ("deadline", "截止时间（ISO / 空为无）", None)]
        self.goal_form = self._management_forms(self.goals, "goal", common + [
            ("use_policy", "用途", ["local_only", "cloud_allowed"]),
            ("progress_note", "进度备注", None)], ["Active", "Paused", "Completed", "Cancelled"])
        self.milestone_form = self._management_forms(self.milestones, "milestone", common + [
            ("progress_note", "进度备注", None)], ["Pending", "Completed", "Cancelled"])
        self.binding_form = CommandForm("新增绑定使用父目标版本；修改绑定使用所选绑定版本。", [
            ("action", "操作", ["bind", "update_binding"]),
            ("expected_revision", "已审阅版本（CAS）", None),
            ("source", "来源类型", ["calendar", "timer", "task"]),
            ("source_id", "来源 ID", None), ("milestone_id", "里程碑 ID（可空）", None),
            ("use_policy", "用途", ["local_only", "cloud_allowed"]),
            ("status", "绑定状态", ["Active", "Removed"])])
        self.binding_form.submitted.connect(self._binding_command)
        self._forms.append(self.binding_form)
        self._add_scroll_form(self.bindings, self.binding_form)
        self._add_page(parent_box, "目标与来源")

    def _management_forms(self, page: PagedView, entity: str,
                          fields: list[tuple[str, str, Any]], statuses: list[str]) -> CommandForm:
        form = CommandForm("新增时填写全部必填项；修改和状态操作严格使用已审阅版本。", [
            ("action", "操作", ["create_" + entity, "update_" + entity, entity + "_status"]),
            ("expected_revision", "已审阅版本（CAS）", None), *fields,
            ("status", "目标状态", statuses), ("evidence", "人工完成证据", None)])
        form.submitted.connect(lambda values, entity=entity: self._management_command(entity, values))
        self._forms.append(form)
        self._add_scroll_form(page, form)
        return form

    @staticmethod
    def _add_scroll_form(page: PagedView, form: CommandForm) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(form)
        page.splitter.addWidget(scroll)

    def _build_proposals(self) -> None:
        self.proposals = PagedView("proposals")
        self.pages["proposals"] = self.proposals
        self.proposal_filter = QComboBox()
        self.proposal_filter.addItems(["Pending", "all", "Accepted", "Ignored", "Expired", "Invalidated"])
        self.proposals.toolbar.insertWidget(0, self.proposal_filter)
        self.proposal_filter.currentTextChanged.connect(self._proposal_filter_changed)
        self.proposals.refresh_requested.connect(lambda: self.reload_page("proposals", explicit=True))
        self.proposals.selected.connect(self._proposal_selected)
        self.proposal_accept = QPushButton("接受并排队（不授予工具权限）")
        self.proposal_ignore = QPushButton("忽略提议")
        self.proposals.action_bar.addWidget(self.proposal_accept)
        self.proposals.action_bar.addWidget(self.proposal_ignore)
        self.proposal_accept.clicked.connect(lambda: self._proposal_command("accept_proposal"))
        self.proposal_ignore.clicked.connect(lambda: self._proposal_command("ignore_proposal"))
        self._add_page(self.proposals, "主动提议")

    def _build_settings(self) -> None:
        self.settings_page = QWidget()
        layout = QVBoxLayout(self.settings_page)
        self.notifications_toggle = QCheckBox("启用系统通知（仅通用状态，不含私人正文）")
        self.tray_toggle = QCheckBox("关窗留在托盘（仅托盘可用时生效）")
        row = QHBoxLayout()
        self.quiet_start = QSpinBox()
        self.quiet_end = QSpinBox()
        for spin in (self.quiet_start, self.quiet_end):
            spin.setRange(0, 23)
        row.addWidget(QLabel("免打扰起始小时"))
        row.addWidget(self.quiet_start)
        row.addWidget(QLabel("结束小时"))
        row.addWidget(self.quiet_end)
        layout.addWidget(self.notifications_toggle)
        layout.addWidget(self.tray_toggle)
        layout.addLayout(row)
        layout.addWidget(QLabel("GUI 偏好即时生效。下方后端有效配置与来源只读；修改配置文件后需重启。密钥不在界面中编辑。"))
        self.settings_text = QPlainTextEdit()
        self.settings_text.setReadOnly(True)
        layout.addWidget(self.settings_text)
        if self.preferences is not None:
            self.notifications_toggle.setChecked(bool(self.preferences.notifications_enabled))
            self.tray_toggle.setChecked(bool(self.preferences.close_to_tray))
            self.quiet_start.setValue(int(self.preferences.quiet_start))
            self.quiet_end.setValue(int(self.preferences.quiet_end))
            self.preferences.changed.connect(self._preference_changed)
        else:
            self.quiet_start.setValue(22)
            self.quiet_end.setValue(8)
        self.notifications_toggle.toggled.connect(lambda value: self._set_preference("notifications_enabled", value))
        self.tray_toggle.toggled.connect(lambda value: self._set_preference("close_to_tray", value))
        self.quiet_start.valueChanged.connect(self._set_quiet_hours)
        self.quiet_end.valueChanged.connect(self._set_quiet_hours)
        self._add_page(self.settings_page, "设置")

    def _set_preference(self, key: str, value: bool) -> None:
        if self.preferences is not None:
            setter = getattr(self.preferences, "set_" + key, None)
            if setter is not None:
                setter(value)
            else:
                setattr(self.preferences, key, value)

    def _set_quiet_hours(self, _: int) -> None:
        if self.preferences is not None:
            self.preferences.set_quiet_hours(self.quiet_start.value(), self.quiet_end.value())

    @Slot(str, object)
    def _preference_changed(self, key: str, value: Any) -> None:
        controls: dict[str, Any] = {"notifications_enabled": self.notifications_toggle,
                                    "close_to_tray": self.tray_toggle,
                                    "quiet_start": self.quiet_start, "quiet_end": self.quiet_end}
        widget = controls.get(key)
        if widget is not None:
            widget.blockSignals(True)
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            else:
                widget.setValue(int(value))
            widget.blockSignals(False)

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
        return result

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
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
        QTimer.singleShot(0, self._fit_screen)

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
        if self.lifecycle == "Closed":
            return
        self.lifecycle = state
        first_ready = state == "Ready" and not self._ready_seen
        if state == "Ready":
            self._ready_seen = True
        messages = {"Initializing": "正在初始化，您可以查看诊断或退出。",
                    "Ready": "就绪 · 所有工具授权均需审核原计划后明确选择。",
                    "CleanupBlocked": "记忆清理受阻：正文已遮蔽。仅可诊断、重试清理或退出。",
                    "StartupFailed": "启动失败。请查看诊断后退出并修正配置。",
                    "Closing": "正在关闭：停止接收操作，等待后台释放资源；可诊断或重试关闭。",
                    "Closed": "已关闭"}
        self.state_label.setText(messages.get(state, state))
        if state != "Ready":
            self.poll_timer.stop()
            self.requests.invalidate()
            self._clear_grants()
        if state in {"CleanupBlocked", "Closing", "Closed"}:
            self.clear_sensitive()
        if state == "CleanupBlocked":
            self.privacy_barrier = True
            self.forget_state = "pending"
        if state == "Ready":
            self.poll_timer.start()
            self._poll()
        if state == "Closed":
            self.requests.pending.clear()
            self._allow_close = True
            self.close()
        self._update_enabled()
        if first_ready:
            self.tabs.setCurrentIndex(0)
            self._send("settings", route="settings")

    def _send(self, operation: str, parameters: dict[str, Any] | None = None, *,
              route: str | None = None, mutation: bool = False) -> str | None:
        route = route or operation
        if self.lifecycle == "Closed":
            return None
        if self.lifecycle != "Ready" and operation not in {"diagnostics", "retry_cleanup"}:
            self.notice.setText("当前仅允许诊断、清理重试和退出。")
            return None
        if self.privacy_barrier and operation not in {"diagnostics", "retry_cleanup", "memory"}:
            self.notice.setText("隐私屏障仍在生效；请按提示处理后主动重新加载。")
            return None
        existing = self.requests.existing(route)
        if existing is not None and (existing.mutation or self.requests.current(existing)):
            return existing.request_id
        if len(self.requests.pending) >= self.requests.limit:
            self.notice.setText("后台请求已满；请等待当前请求返回后再试。")
            return None
        params = dict(parameters or {})
        command_key = ""
        if mutation:
            command_key = self.requests.command_key(operation, params)
            params["command_id"] = self.requests.command_id(operation, params)
        generation = self.requests.generation(route)
        try:
            request_id = str(self.bridge.send(operation, params, generation=generation))
        except (RuntimeError, ValueError, BufferError) as error:
            self.notice.setText(f"请求未提交：{error}")
            return None
        request = PendingRequest(request_id, operation, route, generation, mutation,
                                 int(getattr(self.bridge, "privacy_epoch", 0)), command_key)
        self.requests.pending[request_id] = request
        self._update_enabled()
        return request_id

    @Slot(object)
    def on_reply(self, reply: Any) -> None:
        if not isinstance(reply, dict):
            return
        request = self.requests.pending.pop(str(reply.get("request_id", "")), None)
        if request is None:
            return
        self._update_enabled()
        if self.lifecycle == "Closed":
            return
        if int(reply.get("generation", -1)) != request.generation:
            return
        current_epoch = int(getattr(self.bridge, "privacy_epoch", 0))
        if int(reply.get("privacy_epoch", -1)) < current_epoch:
            return
        if not request.mutation and not self.requests.current(request):
            return
        if self.lifecycle != "Ready" and request.operation not in {"diagnostics", "retry_cleanup"}:
            return
        if not reply.get("ok"):
            error = reply.get("error", {})
            code = str(error.get("code", "Error"))
            message = str(error.get("message", "操作失败"))
            if request.route == "mutation:memory":
                if self.forget_state == "awaiting":
                    if code in {"Busy", "Conflict", "Rejected", "QueueFull", "ValueError", "RevisionConflict", "MemoryNotFound"}:
                        self.forget_state = "unsubmitted"
                        self.notice.setText(f"遗忘未提交（{code}）；可主动重新加载后重试。")
                    else:
                        self.forget_state = "pending"
                        self.notice.setText("遗忘提交状态需核查；正文继续遮蔽，请诊断并重试清理。")
                else:
                    self.forget_state = "unsubmitted"
                    self.notice.setText(f"记忆操作未提交（{code}）；可主动重新加载。")
            else:
                self.notice.setText(f"{code}：{message[:2000]}。未自动重试；请重新加载当前版本后再操作。")
            self._update_enabled()
            return
        data = reply.get("data", {})
        if not isinstance(data, dict):
            return
        if request.mutation:
            self.requests.complete_command(request.command_key)
        if request.operation == "diagnostics":
            self.diagnostics.setPlainText(plain_json(data))
        elif request.operation == "retry_cleanup":
            if data.get("cleanup_complete", data.get("complete")) is True:
                self.privacy_barrier = False
                self.forget_state = ""
                self.notice.setText("清理已完成；可主动重新加载当前事实。")
            else:
                self.notice.setText("清理尚未完成，正文继续遮蔽；可查看诊断并再次重试。")
        elif request.route == "mutation:memory":
            if data.get("status") == "Pending" or data.get("blocked"):
                self.forget_state = "pending"
                self.notice.setText("遗忘已提交，清理待完成；正文持续遮蔽，请重试清理。")
            else:
                self.forget_state = "complete"
                self.notice.setText("记忆操作已完成，相关正文已清空；请主动重新加载当前事实。")
        elif self.privacy_barrier:
            return
        elif request.mutation:
            if request.operation == "submit":
                self._chat_task_id = str(data.get("task_id", ""))
                self._chat_result_revision = 0
                self._append_chat("您：" + self.chat_input.toPlainText() + "\n已排队：" + self._chat_task_id)
                self.chat_input.clear()
            elif request.operation == "cancel":
                self.notice.setText("取消请求已记录；正在执行的同步工具可能仍需等待返回。")
            elif request.operation == "accept_proposal":
                self.notice.setText("提议已接受并排队；未授予任何工具权限，请到任务页审核计划。")
            else:
                self.notice.setText("操作已受理；请查看最新状态。")
            self._invalidate_entities()
            self._poll()
        elif request.route.endswith(":list"):
            name = request.route.split(":")[0]
            self.pages[name].set_page(data)
            if name == "goals":
                old = self.goal_filter.currentText()
                self.goal_filter.blockSignals(True)
                self.goal_filter.clear()
                self.goal_filter.addItems([str(item["id"]) for item in self.goals.items])
                self.goal_filter.setCurrentText(old)
                self.goal_filter.blockSignals(False)
        elif request.route == "chat:detail":
            self._show_chat_result(data)
        elif request.operation == "task":
            self._show_task(data)
        elif request.operation == "proposal":
            self.proposal_snapshot = data
            self.proposals.show_detail(data)
        elif request.operation == "goal":
            self.goals.show_detail(data)
            self.goal_form.populate(data)
        elif request.operation == "settings":
            self.settings_text.setPlainText(plain_json(data))
            if data.get("provider") not in {None, "offline"}:
                self.policy_hint.setText("当前使用云模型。local_only 会阻止整次云规划；如允许本次请求进入云模型，请主动选择 cloud_allowed。")
        self._update_enabled()

    def _update_enabled(self) -> None:
        ready = self.lifecycle == "Ready" and not self.privacy_barrier
        for index in range(self.tabs.count() - 1):
            self.tabs.setTabEnabled(index, ready)
        # A rejected/unsubmitted memory command permits only an explicit reload.
        if self.lifecycle == "Ready" and self.privacy_barrier and self.forget_state in {"unsubmitted", "complete"}:
            self.tabs.setTabEnabled(2, True)
        submitting = self.requests.existing("mutation:submit") is not None
        self.submit_button.setEnabled(ready and not submitting)
        self.chat_input.setEnabled(ready and not submitting)
        task_pending = any(item.route == "mutation:task" for item in self.requests.pending.values())
        task = self.task_snapshot.get("task", {})
        status = task.get("status")
        self.confirm_button.setEnabled(ready and not task_pending and status == "Waiting"
                                       and bool(self._task_signature) and not self.task_snapshot.get("attempts"))
        self.resume_button.setEnabled(ready and not task_pending and status == "NeedsReview")
        self.cancel_button.setEnabled(ready and not task_pending and status in {
            "Created", "Planning", "Waiting", "Running", "NeedsReview"})
        self.uncertain_form.setEnabled(ready and not task_pending and status == "Uncertain")
        memory_ready = ready and bool(self.memory_snapshot) and self.requests.existing("mutation:memory") is None
        for button in (self.memory_accept, self.memory_reject):
            button.setEnabled(memory_ready and self.memory_snapshot.get("status") == "candidate")
        self.memory_forget.setEnabled(memory_ready)
        self.memory_edit.setEnabled(memory_ready)
        self.memory_remember.setEnabled(ready and self.requests.existing("mutation:memory") is None)
        proposal_ready = (ready and self.proposal_snapshot.get("status") == "Pending"
                          and self.requests.existing("mutation:proposal") is None)
        self.proposal_accept.setEnabled(proposal_ready)
        self.proposal_ignore.setEnabled(proposal_ready)
        for form in self._forms:
            form.setEnabled(ready and self.requests.existing("mutation:management") is None)
        for name, page in self.pages.items():
            page.list.setEnabled(ready)
            page.refresh.setEnabled(ready or (name == "memories" and self.forget_state in {"unsubmitted", "complete"}))
            if not ready:
                page.previous.setEnabled(False)
                page.next.setEnabled(False)
        for control in (self.notifications_toggle, self.tray_toggle, self.quiet_start, self.quiet_end):
            control.setEnabled(ready and self.preferences is not None)
        self.retry_button.setEnabled(self.lifecycle in {"Ready", "CleanupBlocked", "Closing"})

    def _active_page(self) -> str | None:
        current = self.tabs.currentIndex()
        if current == 1:
            return "tasks"
        if current == 2:
            return "memories"
        if current == 3:
            return ("goals", "milestones", "bindings")[self.management.currentIndex()]
        if current == 4:
            return "proposals"
        return None

    def _tab_changed(self, _: int) -> None:
        self.requests.invalidate()
        self._invalidate_entities()
        if self.lifecycle == "Ready":
            self._poll()

    def _management_tab_changed(self) -> None:
        self.requests.invalidate()
        self._poll()

    def _parent_goal_changed(self, _: str) -> None:
        for name in ("milestones", "bindings"):
            self.requests.invalidate(name + ":list")
            self.pages[name].offset = 0
            self.pages[name].clear_sensitive()
        self._poll()

    def _memory_filter_changed(self, _: str) -> None:
        self.memories.offset = 0
        self.reload_page("memories", explicit=True)

    def _proposal_filter_changed(self, _: str) -> None:
        self.proposals.offset = 0
        self.reload_page("proposals", explicit=True)

    def _poll(self) -> None:
        if self.lifecycle != "Ready" or self.privacy_barrier:
            return
        name = self._active_page()
        if name:
            self.reload_page(name)
            if name == "tasks" and self.task_snapshot:
                task_id = self.task_snapshot.get("task", {}).get("id")
                self._send("task", {"task_id": task_id}, route="tasks:detail")
        elif self.tabs.currentIndex() == 5:
            self._send("settings", route="settings")
        elif self.tabs.currentIndex() == 0 and self._chat_task_id:
            self._send("task", {"task_id": self._chat_task_id}, route="chat:detail")

    def _append_chat(self, text: str) -> None:
        combined = self.chat_history.toPlainText() + "\n" + text[:64000]
        self.chat_history.setPlainText(combined[-128000:])
        self.chat_history.verticalScrollBar().setValue(self.chat_history.verticalScrollBar().maximum())

    def _show_chat_result(self, data: dict[str, Any]) -> None:
        task = data.get("task", {})
        revision = int(task.get("revision", 0))
        if task.get("id") != self._chat_task_id or revision <= self._chat_result_revision:
            return
        self._chat_result_revision = revision
        status = str(task.get("status", ""))
        if status in {"Completed", "Failed", "Cancelled", "Uncertain", "Waiting", "NeedsReview"}:
            self._append_chat("任务状态：" + status + "\n" + plain_json(task.get("result") or task.get("error") or "请到任务页查看完整计划与结果。"))

    def reload_page(self, name: str, *, explicit: bool = False) -> None:
        if explicit and self.privacy_barrier:
            if self.forget_state not in {"unsubmitted", "complete"}:
                self.notice.setText("遗忘已提交或状态待核查；清理完成前禁止加载正文。")
                return
            self.privacy_barrier = False
            self.forget_state = ""
            self.requests.invalidate()
        if explicit:
            self.requests.invalidate(name + ":list")
            self.requests.invalidate(name + ":detail")
            self._invalidate_entities()
        page = self.pages[name]
        if explicit:
            page.clear_sensitive()
        params: dict[str, Any] = {"limit": 50, "offset": page.offset}
        if name in {"milestones", "bindings"}:
            if not self.goal_filter.currentText().strip():
                self.notice.setText("请先输入或选择父目标 ID。")
                return
            params["goal_id"] = self.goal_filter.currentText().strip()
        if name == "memories" and self.memory_filter.currentText() != "all":
            params["status"] = self.memory_filter.currentText()
        if name == "proposals":
            params["status"] = self.proposal_filter.currentText()
        self._send(name, params, route=name + ":list")

    def submit_request(self) -> None:
        request = self.chat_input.toPlainText().strip()
        if not request or len(request) > 20000:
            self.notice.setText("请输入 1–20,000 字符的请求。")
            return
        if self._send("submit", {"request": request, "use_policy": self.use_policy.currentText()},
                      route="mutation:submit", mutation=True):
            self.notice.setText("正在提交；收到入队回执后可在任务页查看。")

    def _task_selected(self, item: dict[str, Any]) -> None:
        self.requests.invalidate("tasks:detail")
        self.task_snapshot = {}
        self._task_signature = ()
        self._clear_grants()
        self.tasks.detail.clear()
        if item:
            self._send("task", {"task_id": item["id"]}, route="tasks:detail")
        self._update_enabled()

    def _show_task(self, data: dict[str, Any]) -> None:
        task = data.get("task", {})
        plan = data.get("plan") or {}
        signature = (task.get("id"), task.get("revision"), plan.get("hash"), data.get("memory_epoch"))
        if signature == self._task_signature:
            return
        self.task_snapshot = data
        self.tasks.show_detail(data)
        self._clear_grants()
        hash_value = str(plan.get("hash", ""))
        if not re.fullmatch("[a-f0-9]{64}", hash_value):
            self._task_signature = ()
            return
        self._task_signature = signature
        self.hash_label.setText("已展示完整原计划 · hash: " + hash_value + "\n工作区: " + str(plan.get("workspace_dir", "")))
        ordinary, high = tool_permissions(plan)
        for names, controls, container in ((ordinary, self._ordinary, self.ordinary_layout),
                                           (high, self._high, self.high_layout)):
            for name in names:
                checkbox = QCheckBox(name)
                checkbox.setChecked(False)
                controls[name] = checkbox
                container.addWidget(checkbox)

    def _clear_grants(self) -> None:
        self._ordinary.clear()
        self._high.clear()
        for container in (self.ordinary_layout, self.high_layout):
            while container.count():
                item = container.takeAt(0)
                widget = item.widget() if item is not None else None
                if widget is not None:
                    widget.setParent(None)
                    widget.deleteLater()
        self.hash_label.setText("尚未展示可确认计划")

    def confirm_plan(self) -> None:
        if not self.confirm_button.isEnabled():
            return
        plan = self.task_snapshot.get("plan", {})
        parameters = {"task_id": self.task_snapshot["task"]["id"], "plan_hash": plan["hash"],
                      "ordinary_tools": [name for name, box in self._ordinary.items() if box.isChecked()],
                      "high_risk_tools": [name for name, box in self._high.items() if box.isChecked()]}
        self._send("confirm", parameters, route="mutation:task", mutation=True)
        self._invalidate_task_details()

    def _task_command(self, operation: str) -> None:
        task = self.task_snapshot.get("task", {})
        if task:
            self._send(operation, {"task_id": task["id"]}, route="mutation:task", mutation=True)
            self._invalidate_task_details()

    def _invalidate_task_details(self) -> None:
        self.requests.invalidate("tasks:detail")
        self.requests.invalidate("chat:detail")
        self.task_snapshot = {}
        self._task_signature = ()
        self.tasks.detail.clear()
        self._clear_grants()
        self._update_enabled()

    def _review_uncertain(self, values: dict[str, Any]) -> None:
        task = self.task_snapshot.get("task", {})
        if not task or not values.get("evidence", "").strip():
            self.notice.setText("请提供人工审查证据。")
            return
        self._send("review_uncertain", {"task_id": task["id"], **values}, route="mutation:task", mutation=True)
        self._invalidate_task_details()

    def _memory_selected(self, item: dict[str, Any]) -> None:
        self.memory_snapshot = item
        if item:
            self.memories.show_detail(item)
        else:
            self.memories.detail.clear()
        self.memory_edit.populate({"content": item.get("draft", {}).get("content", "")})
        self._update_enabled()

    def memory_action(self, action: str, values: dict[str, Any] | None = None) -> None:
        if not self.memory_snapshot or self.privacy_barrier or self.lifecycle != "Ready":
            return
        parameters: dict[str, Any] = {"action": action, "memory_id": self.memory_snapshot["id"],
                                      "expected_revision": self.memory_snapshot["revision"]}
        if values:
            parameters.update(values)
        if action == "correct" and not str(parameters.get("content", "")).strip():
            self.notice.setText("更正内容不能为空。")
            return
        self._begin_memory(parameters)

    def remember_memory(self, values: dict[str, Any]) -> None:
        if self.privacy_barrier or self.lifecycle != "Ready":
            return
        if not str(values.get("content", "")).strip():
            self.notice.setText("新记忆内容不能为空。")
            return
        self._begin_memory({"action": "remember", **values})

    def _begin_memory(self, parameters: dict[str, Any]) -> None:
        self.privacy_barrier = True
        self.forget_state = "awaiting" if parameters["action"] == "forget" else "changing"
        self.clear_sensitive()
        self.notice.setText("正文已遮蔽，等待记忆操作回执。")
        if not self._send("memory", parameters, route="mutation:memory", mutation=True):
            self.forget_state = "unsubmitted"
            self.notice.setText("记忆操作未提交；可主动重新加载。")
        self._update_enabled()

    def clear_sensitive(self) -> None:
        self.requests.invalidate()
        self.chat_history.clear()
        self.chat_input.clear()
        self._chat_task_id = ""
        self._chat_result_revision = 0
        self.notice.clear()
        for page in self.pages.values():
            page.clear_sensitive()
        for form in self._forms:
            form.clear_sensitive()
        self.memory_edit.clear_sensitive()
        self.memory_remember.clear_sensitive()
        self.uncertain_form.clear_sensitive()
        self.settings_text.clear()
        self.diagnostics.clear()
        self._invalidate_entities()
        self.requests.clear_commands()
        self.privacy_cleared.emit()

    @Slot(int)
    def _privacy_changed(self, epoch: int) -> None:
        # The bridge intentionally drops old worker callbacks; their query
        # bookkeeping must not consume UI capacity indefinitely.
        self.requests.pending = {key: item for key, item in self.requests.pending.items()
                                 if item.privacy_epoch >= epoch}
        self.clear_sensitive()
        self._update_enabled()

    def _invalidate_entities(self) -> None:
        self.requests.invalidate()
        self.task_snapshot = {}
        self.memory_snapshot = {}
        self.proposal_snapshot = {}
        self._task_signature = ()
        self._clear_grants()
        for page in self.pages.values():
            page.list.setCurrentRow(-1)
            page.detail.clear()
        self._update_enabled()

    def _management_selected(self, name: str, item: dict[str, Any]) -> None:
        self.requests.invalidate(name + ":detail")
        if not item:
            self.pages[name].detail.clear()
            form = {"goals": self.goal_form, "milestones": self.milestone_form,
                    "bindings": self.binding_form}[name]
            form.clear_sensitive()
            return
        self.pages[name].show_detail(item)
        if name == "goals":
            self.goal_form.populate(item)
            self._send("goal", {"goal_id": item["id"]}, route="goals:detail")
        elif name == "milestones":
            self.milestone_form.populate(item)
        else:
            self.binding_form.populate(item)

    def _management_command(self, entity: str, values: dict[str, Any]) -> None:
        params = dict(values)
        operation = str(params.pop("action"))
        revision = params.pop("expected_revision")
        status = params.pop("status")
        evidence = params.pop("evidence")
        item = self.goals.selected_item() if entity == "goal" else self.milestones.selected_item()
        if operation.startswith("create_"):
            params.pop("progress_note", None)
            if not params.get("title", "").strip() or not params.get("success_criteria", "").strip():
                self.notice.setText("标题与成功标准不能为空。")
                return
            if entity == "milestone":
                params.update(goal_id=self.goal_filter.currentText(), expected_revision=revision)
        else:
            if not item:
                self.notice.setText("请先选择并审阅要修改的条目。")
                return
            if operation.endswith("_status"):
                params = {"status": status, "evidence": evidence}
                if status == "Completed" and not str(evidence).strip():
                    self.notice.setText("完成状态需要明确的人工证据。")
                    return
            params.update({entity + "_id": item["id"], "expected_revision": revision})
        if "deadline" in params and not params["deadline"]:
            params["deadline"] = None
        self._send(operation, params, route="mutation:management", mutation=True)

    def _binding_command(self, values: dict[str, Any]) -> None:
        params = dict(values)
        operation = str(params.pop("action"))
        if operation == "bind":
            params["goal_id"] = self.goal_filter.currentText()
            params.pop("status")
            if not params["milestone_id"]:
                params["milestone_id"] = None
        else:
            item = self.bindings.selected_item()
            if not item:
                self.notice.setText("请先选择并审阅来源绑定。")
                return
            params = {key: params[key] for key in ("expected_revision", "use_policy", "status")}
            params["binding_id"] = item["id"]
        self._send(operation, params, route="mutation:management", mutation=True)

    def _proposal_selected(self, item: dict[str, Any]) -> None:
        self.requests.invalidate("proposals:detail")
        self.proposal_snapshot = {}
        self.proposals.detail.clear()
        if item:
            self._send("proposal", {"proposal_id": item["id"]}, route="proposals:detail")
        self._update_enabled()

    def _proposal_command(self, operation: str) -> None:
        if not self.proposal_snapshot or self.proposal_snapshot.get("status") != "Pending":
            return
        self._send(operation, {"proposal_id": self.proposal_snapshot["id"],
                              "expected_revision": self.proposal_snapshot["revision"]},
                   route="mutation:proposal", mutation=True)
        self.proposal_snapshot = {}
        self._update_enabled()

    def navigate_to(self, kind: str, entity_id: str) -> None:
        """Notification navigation only re-queries; it never executes or grants."""
        if self.lifecycle != "Ready" or self.privacy_barrier:
            return
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if kind.lower() in {"task", "tasks"}:
            self.tabs.setCurrentIndex(1)
            self._task_selected({"id": entity_id})
        elif kind.lower() in {"proposal", "proposals"}:
            self.tabs.setCurrentIndex(4)
            self._proposal_selected({"id": entity_id})

    @Slot(str, str)
    def navigate(self, kind: str, entity_id: str) -> None:
        self.navigate_to(kind, entity_id)

    def show_diagnostics(self) -> None:
        self.tabs.setCurrentIndex(self.tabs.count() - 1)
        self._send("diagnostics", route="diagnostics")

    def retry_cleanup(self) -> None:
        if self.lifecycle == "Closing":
            self.bridge.shutdown()
        else:
            self._send("retry_cleanup", route="retry_cleanup")

    def request_exit(self) -> None:
        self.on_state_changed("Closing")
        self.bridge.shutdown()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_geometry()
        if self._allow_close:
            event.accept()
        else:
            event.ignore()
            self.close_requested.emit()
