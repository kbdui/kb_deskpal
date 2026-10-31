"""Readable memory, arrangement and goal workflows over the async channel."""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (QDialog, QFormLayout, QHBoxLayout, QLabel, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)
from .components import (ChoiceBox as QComboBox, PushButton as QPushButton, Tabs as QTabWidget, Menu as QMenu)
from .channel import Channel
from .views import PagedView, CommandForm
from .widgets import ArrowComboBox, label, notice_label, scroll, text_label
from .task_card import TaskCard

class MemoryPage(QWidget):
    def __init__(self, bridge: Any, parent: Any = None) -> None:
        super().__init__(parent); self.channel = Channel(bridge, self)
        layout = QVBoxLayout(self); layout.addWidget(text_label("记忆", heading=True)); layout.addWidget(text_label("查看记住的事实、共同经历和仍待明确的理解。系统会自动整理；你可以更正或遗忘。"))
        self.notice = notice_label(); layout.addWidget(self.notice); self.channel.notice.connect(self.notice.setText)
        self.page = PagedView("memories"); layout.addWidget(self.page, 3)
        self.filter = ArrowComboBox()
        for value, title in [('facts', '记住的内容'), ('episode', '一起经历'), ('buffer', '仍待明确'), ('all', '全部记录')]:
            self.filter.addItem(title, value)
        self.page.toolbar.insertWidget(0, self.filter)
        self.filter.currentIndexChanged.connect(self._filter); self.page.refresh_requested.connect(self.refresh)
        self.page.selected.connect(self._selected)
        self.memory_actions = QPushButton('更多操作 ···')
        self.memory_actions.hide()
        self.page.details_layout.insertWidget(0, self.memory_actions)
        self.memory_actions.clicked.connect(self._more_memory_actions)
        self.page.action_scroll.hide()
        assert self.page.memory_tree is not None
        self.page.memory_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.page.memory_tree.customContextMenuRequested.connect(self._memory_context_menu)
        self.forms = QStackedWidget()
        self.correct = CommandForm("更正采用当前审阅版本，发生冲突时保留编辑内容。", [("content", "更正内容", None)])
        self.remember = CommandForm("新记忆用途随设置中的新增输入策略；正在读取当前策略。", [("content", "内容", None)])
        self._new_memory_policy: str | None = None; self.remember.submit.setEnabled(False)
        self.correct.submit.setText("保存更正"); self.remember.submit.setText("保存记忆")
        self._latest_for_edit: dict[str, Any] = {}
        self.adopt_revision = QPushButton('采用刚读取的版本，保留更正草稿')
        self.adopt_revision.clicked.connect(self._adopt_revision); self.adopt_revision.hide()
        correct_layout = self.correct.layout(); assert isinstance(correct_layout, QFormLayout)
        correct_layout.addRow(self.adopt_revision)
        self.forms.addWidget(self.correct); self.forms.addWidget(self.remember)
        editor_buttons = QHBoxLayout()
        self.create_memory = QPushButton("新建记忆")
        self.learning_button = QPushButton('学习记录')
        self.learning_button.clicked.connect(self._learning_records)
        editor_buttons.addWidget(self.create_memory); editor_buttons.addWidget(self.learning_button); editor_buttons.addStretch(); layout.addLayout(editor_buttons)
        self.correct.submitted.connect(lambda values: self.action("correct", values)); self.remember.submitted.connect(lambda values: self.action("remember", values))
        self.create_memory.clicked.connect(lambda: self._open_editor(1))
        # Keep the detail and editor in the existing right-hand panel so a
        # selected record stays visible while its correction is being made.
        self.right_tabs = QTabWidget()
        self.right_tabs.setObjectName("memory_detail_editor_tabs")
        self.right_tabs.setMinimumWidth(0)
        self.right_tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.page.detail_panel.takeWidget()
        self.right_tabs.addTab(self.page.detail_content, "记录详情")
        self.form_stack = QStackedWidget()
        self.form_placeholder = text_label("选择一条记录查看详情。")
        self.form_stack.addWidget(self.form_placeholder)
        self.form_scroll = scroll(self.forms)
        self.form_scroll.setMinimumHeight(0)
        self.form_stack.addWidget(self.form_scroll)
        self.right_tabs.addTab(self.form_stack, "编辑记忆")
        self.page.detail_panel.setWidget(self.right_tabs)
        self.right_tabs.setCurrentIndex(0)
        for index in range(layout.count()):
            entry = layout.itemAt(index)
            header = entry.widget() if entry is not None else None
            if isinstance(header, QLabel): header.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._snapshot: dict[str, Any] = {}; self._pending = False
        self._read_epoch = 0
        self._reading = False; self._read_again = False; self._settings_reading = False
        if hasattr(bridge, "privacy_changed"): bridge.privacy_changed.connect(self.clear_sensitive)

    def _open_editor(self, index: int) -> None:
        if index == 0 and not self._snapshot:
            self.notice.setText("请先选择要更正的记忆。"); return
        self.forms.setCurrentWidget(self.correct if index == 0 else self.remember)
        self.form_stack.setCurrentWidget(self.form_scroll)
        self.right_tabs.setCurrentIndex(1)
        (self.correct if index == 0 else self.remember).fields["content"].setFocus()

    def _memory_context_menu(self, position: Any) -> None:
        tree = self.page.memory_tree
        if tree is None:
            return
        row = tree.itemAt(position)
        item = row.data(0, Qt.ItemDataRole.UserRole) if row is not None else None
        if row is None or not isinstance(item, dict) or not item.get("id"):
            return
        tree.setCurrentItem(row)
        self._show_memory_actions(item, tree.viewport().mapToGlobal(position))

    def _more_memory_actions(self) -> None:
        from PySide6.QtCore import QPoint
        self._show_memory_actions(self._snapshot, self.memory_actions.mapToGlobal(QPoint(0, self.memory_actions.height())))

    def _show_memory_actions(self, item: dict[str, Any], position: Any) -> None:
        if not item or not item.get('id'): return
        tree = self.page.memory_tree
        assert tree is not None
        status = str(item.get("status", "")).lower()
        menu = QMenu(tree)
        if status != "deleted":
            menu.addAction("更正这条记忆", lambda: self._open_editor(0))
            menu.addSeparator()
            menu.addAction("遗忘这条记忆及相关正文", lambda: self.action("forget"))
        if menu.actions():
            if hasattr(menu, 'close'): tree.currentItemChanged.connect(menu.close)
            menu.exec(position)

    def _filter(self, index: int) -> None:
        self.page.offset = 0; self.refresh()

    def refresh(self) -> None:
        if str(getattr(self.channel.bridge, "state", "")) != "Ready": return
        self.page.loading()
        if not self._settings_reading:
            self._settings_reading = True
            epoch = self._read_epoch
            def settings_done(data: dict[str, Any]) -> None:
                if epoch != self._read_epoch: return
                self._settings_reading = False; self._settings(data)
            if self.channel.send("settings", {}, settings_done) is None: self._settings_reading = False
        if self._reading:
            self._read_again = True; return
        self._read_memories()

    def _read_memories(self) -> None:
        self._reading = True; self._read_again = False
        epoch = self._read_epoch
        params: dict[str, Any] = {"offset": self.page.offset, "limit": 50,
                                  'group': self.filter.currentData() or 'facts'}
        def done(data: dict[str, Any]) -> None:
            if epoch != self._read_epoch: return
            self._reading = False
            if self._read_again: self._read_memories()
            else: self.page.set_page(data)
        if self.channel.send("memories", params, done) is None:
            self._reading = False; self.page.load_failed()

    def _learning_records(self) -> None:
        """显示真实学习依据，不把缓冲理解变成人工审批待办。"""
        from .display_text import friendly_time
        from .tool_results import fit_dialog
        dialog = QDialog(self); dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle('最近的学习记录')
        active = [True]
        def finished(*args: Any) -> None: active[0] = False
        dialog.finished.connect(finished); dialog.destroyed.connect(finished)
        layout = QVBoxLayout(dialog)
        status = text_label('正在读取最近 20 条学习记录…'); layout.addWidget(status)
        content = QWidget(); records = QVBoxLayout(content); layout.addWidget(scroll(content), 1)
        close = QPushButton('关闭'); close.clicked.connect(dialog.close); layout.addWidget(close)
        def loaded(data: dict[str, Any]) -> None:
            if not active[0]: return
            if '_error' in data:
                status.setText('读取失败，请关闭后重试。'); return
            items = data.get('items', [])
            status.setText('最近的学习记录（最多 20 条）' if items else '还没有学习记录。')
            for item in items:
                title = {'create': '形成新的理解', 'update': '修订已有理解', 'delete': '移除旧理解',
                         'retain': '暂时保留', 'promote': '作为长期记忆', 'discard': '放弃这条理解'}.get(item.get('action'), '整理与学习')
                records.addWidget(text_label(title + ' · ' + friendly_time(item.get('created_at'))))
                for key, name in [('understanding', '理解'), ('reason', '依据'), ('uncertainty', '仍不确定')]:
                    if item.get(key): records.addWidget(text_label(name + '：' + str(item[key])))
                records.addSpacing(12)
            records.addStretch()
        self.channel.send('learning_records', {'limit': 20}, loaded)
        fit_dialog(dialog, self); dialog.open()

    def _settings(self, data: dict[str, Any]) -> None:
        if "_error" in data or type(data.get("request_use_default")) is not bool: return
        self._new_memory_policy = "cloud_allowed" if data["request_use_default"] else "local_only"
        self.remember.description.setText("新记忆用途随设置中的新增输入策略：" + label(self._new_memory_policy) + "。已有记忆的用途保持不变。")
        self.remember.submit.setEnabled(True)

    def _selected(self, item: dict[str, Any]) -> None:
        if self.correct.dirty and item.get("id") == self._snapshot.get("id"):
            self.page.show_detail(item)
            if item.get('revision') != self._snapshot.get('revision'):
                self._latest_for_edit = dict(item)
                self.adopt_revision.setVisible(item.get('status') != 'deleted')
                self.notice.setText('当前记录已有新版本，草稿已保留。请核对详情，再采用刚读取的版本继续更正。')
            return
        self._latest_for_edit.clear(); self.adopt_revision.hide()
        self._snapshot = dict(item); self.page.show_detail(item)
        self.memory_actions.setVisible(bool(item) and item.get('status') != 'deleted')
        if not item:
            self.form_stack.setCurrentWidget(self.form_placeholder)
            return
        if item:
            self.correct.populate({"content": item.get("content", item.get("draft", {}).get("content", ""))})
        else:
            self.correct.clear_sensitive()
        self.form_stack.setCurrentWidget(self.form_placeholder)

    def _adopt_revision(self) -> None:
        if not self._latest_for_edit or self._latest_for_edit.get('id') != self._snapshot.get('id'): return
        self._snapshot = dict(self._latest_for_edit)
        self._latest_for_edit.clear(); self.adopt_revision.hide()
        self.notice.setText('已采用刚读取的版本；更正草稿仍在，保存后才会修改记忆。')

    def action(self, action: str, values: dict[str, Any] | None = None) -> None:
        if self._pending: return
        params = {"action": action, **(values or {})}
        if action == "remember":
            if self._new_memory_policy is None: self.notice.setText("请先读取设置中的新增输入策略，再保存记忆。"); return
            params["use_policy"] = self._new_memory_policy
        if action != "remember":
            if not self._snapshot: self.notice.setText("请先选择并审阅一条记忆。"); return
            status = str(self._snapshot.get("status", "")).lower()
            if action == "correct" and status == "deleted":
                self.notice.setText("已遗忘的记忆不能更正。"); return
            if action == "forget" and status == "deleted":
                self.notice.setText("这条记忆已经遗忘。"); return
            params.update(memory_id=self._snapshot["id"], expected_revision=self._snapshot["revision"])
        self._pending = True
        def done(data: dict[str, Any]) -> None:
            self._pending = False
            if "_error" not in data:
                self.notice.setText("操作已受理，正在读取当前记忆。"); self.correct.mark_clean(); self.remember.mark_clean(); self.refresh()
        if self.channel.send("request_forget" if action == "forget" else "memory", {key: value for key, value in params.items() if action != "forget" or key != "action"}, done, mutation=True) is None: self._pending = False

    def clear_sensitive(self, epoch: int = 0) -> None:
        from .components import Menu
        for menu in self.findChildren(Menu): menu.close()
        for dialog in self.findChildren(QDialog):
            for value in dialog.findChildren(QLabel): value.clear()
            dialog.close()
        self._read_epoch += 1; self._reading = False; self._read_again = False; self._settings_reading = False
        self.channel.invalidate()
        self.page.clear_sensitive(); self._snapshot = {}; self.correct.clear_sensitive(); self.remember.clear_sensitive(); self._pending = False
        self.memory_actions.hide()
        self._latest_for_edit.clear(); self.adopt_revision.hide()
        self._new_memory_policy = None; self.remember.submit.setEnabled(False)
        self.right_tabs.setCurrentIndex(0)
        self.form_stack.setCurrentWidget(self.form_placeholder)

class ArrangementsPage(QWidget):
    open_conversation = Signal(str, str)
    edit_schedule_requested = Signal(object)
    def __init__(self, coordinator: Any, parent: Any = None) -> None:
        super().__init__(parent); self.coordinator = coordinator; self.channel = Channel(coordinator.bridge, self)
        self._read_epoch = 0
        self._reads: set[str] = set()
        self._latest_reads: dict[str, tuple[dict[str, Any], Callable[[dict[str, Any]], None]]] = {}
        layout = QVBoxLayout(self); layout.addWidget(text_label("安排", heading=True)); self.notice = notice_label(); self.channel.notice.connect(self.notice.setText); layout.addWidget(self.notice)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        self.task_tabs = QTabWidget()
        self.tasks = PagedView("arrangements"); self.task_tabs.addTab(self.tasks, "执行任务")
        self.task_filter = ArrowComboBox()
        for key, title in [("active", "待处理与进行中"), ("Waiting", "待我确认"), ("Running", "进行中"), ("Paused", "已暂停"), ("all", "历史与全部")]: self.task_filter.addItem(title, key)
        self.tasks.toolbar.insertWidget(0, self.task_filter); self.tasks.refresh_requested.connect(self.refresh_tasks); self.task_filter.currentIndexChanged.connect(self._filter_tasks)
        self.tasks.selected.connect(self._task)
        self.card_host = QWidget(); self.card_layout = QVBoxLayout(self.card_host)
        self.card_area = scroll(self.card_host); self.card_area.setObjectName("arrangements_review_area")
        self.tasks.detail_panel.hide()
        self.tasks.splitter.addWidget(self.card_area)
        self.tasks.splitter.setStretchFactor(0, 1); self.tasks.splitter.setStretchFactor(2, 2)
        self.tasks.splitter.setSizes([280, 0, 560])
        self.return_button = QPushButton("返回原会话 · 定位这条消息"); self.return_button.clicked.connect(self._return); self.return_button.hide()
        self.uncertain = CommandForm("结果待核实时，请提供人工审查证据。", [("verdict", "判断", ["unknown", "occurred", "not_occurred"]), ("evidence", "人工证据", None)])
        self.uncertain.submitted.connect(self._uncertain); self.card_layout.addWidget(self.uncertain); self.uncertain.hide()
        self.schedule_tabs = QTabWidget(); self.tabs.addTab(self.schedule_tabs, "日程")
        self.tabs.addTab(self.task_tabs, "任务")
        self.schedules = PagedView("schedules"); self.schedule_tabs.addTab(self.schedules, "日程与计时"); self.schedules.refresh_requested.connect(self.refresh_schedules); self.schedules.selected.connect(self._schedule)
        self.schedule_filter = ArrowComboBox()
        self.schedule_filter.addItem('接下来', 'upcoming'); self.schedule_filter.addItem('全部与历史', 'all')
        self.schedules.toolbar.insertWidget(0, self.schedule_filter)
        self.schedule_filter.currentIndexChanged.connect(self._schedule_filter)
        self.schedule_edit = QPushButton('在聊天中修改或取消')
        self.schedule_edit.clicked.connect(lambda: self.edit_schedule_requested.emit(self.schedules.selected_item()))
        self.schedules.details_layout.addWidget(self.schedule_edit); self.schedule_edit.hide()
        self.reminders = PagedView("reminders"); self.schedule_tabs.addTab(self.reminders, "到期收件箱")
        self.reminders.refresh_requested.connect(self.refresh_reminders); self.reminders.selected.connect(self._reminder)
        self.reminders.list.currentRowChanged.connect(lambda _: self._update_reminder_actions())
        self.reminders.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.reminders.list.customContextMenuRequested.connect(self._reminder_context_menu)
        self.reminder_cursor: Any = None
        self._proposal_pending: set[str] = set()
        self.reminders.previous.clicked.disconnect(); self.reminders.next.clicked.disconnect()
        self.reminders.previous.clicked.connect(lambda: self.refresh_reminders(False)); self.reminders.next.clicked.connect(lambda: self.refresh_reminders(True))
        self.ack_button = QPushButton("标记已读"); self.ack_button.clicked.connect(self._ack); self.reminders.details_layout.addWidget(self.ack_button); self.ack_button.hide()
        self.open_reminder_button = QPushButton("查看原会话"); self.open_reminder_button.clicked.connect(self._open_reminder); self.reminders.details_layout.addWidget(self.open_reminder_button); self.open_reminder_button.hide()
        self.goals = GoalsPage(coordinator.bridge); self.task_tabs.addTab(self.goals, "目标与分步计划")
        self.task_tabs.currentChanged.connect(lambda _: self.refresh())
        self.proposals = PagedView("attentions"); self.tabs.addTab(self.proposals, "后续关注")
        self.end_attention = QPushButton('结束这项关注'); self.end_attention.clicked.connect(self._end_attention)
        self.proposals.details_layout.addWidget(self.end_attention); self.end_attention.hide()
        self.proposals.refresh_requested.connect(self.refresh_proposals); self.proposals.selected.connect(self._attention)
        self.proposals.action_scroll.hide()
        self.tabs.currentChanged.connect(lambda _: self.refresh())
        self.coordinator.task_changed.connect(self._task_changed)
        if hasattr(coordinator.bridge, "privacy_changed"): coordinator.bridge.privacy_changed.connect(self.clear_sensitive)

    def refresh(self) -> None:
        index = self.tabs.currentIndex()
        if index == 0: self.refresh_schedules(); self.refresh_reminders()
        elif index == 1:
            if self.task_tabs.currentIndex() == 0: self.refresh_tasks()
            else: self.goals.refresh()
        else: self.refresh_proposals()

    def refresh_tasks(self) -> None:
        self.tasks.loading()
        params: dict[str, Any] = {"offset": self.tasks.offset, "limit": 50}
        task_filter = self.task_filter.currentData()
        if task_filter != "all":
            params["statuses"] = {'Waiting': ['Waiting', 'NeedsReview'],
                'Running': ['Created', 'Planning', 'Running'], 'Paused': ['Paused'],
                'active': ['Created', 'Planning', 'Running', 'Waiting', 'NeedsReview', 'Paused', 'AwaitingInput']}[task_filter]
        self._read("tasks", params, self.tasks.set_page)

    def _filter_tasks(self, index: int) -> None:
        self.tasks.offset = 0; self.refresh_tasks()

    def _read(self, operation: str, parameters: dict[str, Any], callback: Callable[[dict[str, Any]], None]) -> None:
        if str(getattr(self.channel.bridge, "state", "")) != "Ready": return
        if operation in self._reads:
            self._latest_reads[operation] = (parameters, callback); return
        self._reads.add(operation)
        epoch = self._read_epoch
        def done(data: dict[str, Any]) -> None:
            if epoch != self._read_epoch: return
            self._reads.discard(operation)
            latest = self._latest_reads.pop(operation, None)
            if latest is not None: self._read(operation, *latest)
            else: callback(data)
        if self.channel.send(operation, parameters, done) is None:
            self._reads.discard(operation); callback({"_error": {"code": "NotAccepted"}})

    def _task(self, item: dict[str, Any]) -> None:
        # TaskCard owns the complete task and approval scope; a second generic
        # detail column duplicates it and leaves the review card too narrow.
        self.tasks.show_detail({})
        while self.card_layout.count():
            child = self.card_layout.takeAt(0)
            widget = child.widget() if child is not None else None
            if widget is not None and widget not in (self.uncertain, self.return_button):
                widget.setEnabled(False); widget.hide(); widget.deleteLater()
        if item:
            identity = str(item.get("id", item.get("task_id", "")))
            self.card_layout.addWidget(TaskCard(self.coordinator, identity)); self.coordinator.review(identity)
        else:
            self.card_layout.addWidget(text_label('选择一项任务查看进度与可用操作。'))
        self.card_layout.addWidget(self.return_button); self.return_button.setVisible(bool(item))
        self.card_layout.addWidget(self.uncertain); self.uncertain.setVisible(item.get("status") == "Uncertain")

    def _task_changed(self, task_id: str) -> None:
        pass  # TaskCard subscribes to the same shared revision state.

    def _return(self) -> None:
        item = self.tasks.selected_item(); identity = str(item.get("id", "")); review = self.coordinator.reviews.get(identity, {})
        if review.get("conversation_id"): self.open_conversation.emit(str(review["conversation_id"]), identity)
        else: self.notice.setText("此任务来自独立请求，没有会话记录。")

    def _uncertain(self, values: dict[str, Any]) -> None:
        item = self.tasks.selected_item()
        if item: self.channel.send("review_uncertain", {"task_id": item["id"], **values}, lambda data: self.refresh_tasks(), mutation=True)

    def refresh_schedules(self) -> None:
        self.schedules.loading()
        self._read("schedule_items", {"source": "all", "offset": self.schedules.offset, "limit": 50,
                                     "status": self.schedule_filter.currentData()}, self.schedules.set_page)

    def _schedule_filter(self, index: int) -> None:
        self.schedules.offset = 0; self.refresh_schedules()

    def _schedule(self, item: dict[str, Any]) -> None:
        self.schedules.show_detail(item)
        self.schedule_edit.setVisible(bool(item) and item.get('status') != 'cancelled')

    def refresh_reminders(self, more: bool = False) -> None:
        self.reminders.loading()
        def done(data: dict[str, Any]) -> None:
            if "_error" in data: self.reminders.load_failed(); return
            self.reminder_cursor = data.get("next_cursor"); self.reminders.set_page(data)
            self.reminders.page_label.setText(f"未读 {data.get('unread_count', 0)} 条")
        self._read("reminder_feed", {"cursor": self.reminder_cursor if more else None, "limit": 50, "unread": False}, done)

    def _reminder(self, item: dict[str, Any]) -> None:
        self.reminders.show_detail(item)
        self._update_reminder_actions()

    def _update_reminder_actions(self) -> None:
        item = self.reminders.selected_item()
        unread = bool(item and item.get("acknowledged_at") is None and item.get("delivery_state", item.get("status")) in {"due", "missed"})
        self.ack_button.setEnabled(unread)
        self.ack_button.setVisible(bool(item))
        self.open_reminder_button.setEnabled(bool(item.get("conversation_id")))
        self.open_reminder_button.setVisible(bool(item.get('conversation_id')))

    def _reminder_context_menu(self, position: Any) -> None:
        row = self.reminders.list.itemAt(position)
        index = self.reminders.list.row(row) if row is not None else -1
        if not 0 <= index < len(self.reminders.items): return
        self.reminders.list.setCurrentRow(index)
        item = self.reminders.selected_item(); menu = QMenu(self.reminders.list)
        if item.get("acknowledged_at") is None and item.get("delivery_state", item.get("status")) in {"due", "missed"}:
            menu.addAction("标记已读", self._ack)
        if item.get("conversation_id"):
            menu.addAction("查看原会话", self._open_reminder)
        if menu.actions(): menu.exec(self.reminders.list.viewport().mapToGlobal(position))

    def _ack(self) -> None:
        item = self.reminders.selected_item()
        if item: self.channel.send("reminder_ack", {"id": item["id"]}, lambda data: self.refresh_reminders(), mutation=True)

    def _open_reminder(self) -> None:
        item = self.reminders.selected_item()
        if item.get("conversation_id"): self.open_conversation.emit(str(item["conversation_id"]), str(item.get("task_id") or ""))

    def refresh_proposals(self) -> None:
        self.proposals.loading()
        self._read("attentions", {"offset": self.proposals.offset, "limit": 50}, self.proposals.set_page)

    def _attention(self, item: dict[str, Any]) -> None:
        self.proposals.show_detail(item)
        self.end_attention.setVisible(item.get('status') == 'pending')
        self.end_attention.setEnabled(bool(item) and str(item.get('id')) not in self._proposal_pending)

    def _end_attention(self) -> None:
        item = self.proposals.selected_item(); identity = str(item.get('id') or '')
        if not identity or item.get('status') != 'pending' or identity in self._proposal_pending: return
        self._proposal_pending.add(identity); self._attention(item)
        def done(data: dict[str, Any]) -> None:
            self._proposal_pending.discard(identity)
            if '_error' not in data: self.notice.setText('已结束关注；已完成的操作保留。')
            self.refresh_proposals()
        if self.channel.send('attention_end', {'id': identity, 'expected_status': 'pending'}, done, mutation=True) is None:
            self._proposal_pending.discard(identity); self._attention(item)

    def clear_sensitive(self, epoch: int = 0) -> None:
        self._read_epoch += 1; self._reads.clear(); self._latest_reads.clear(); self.channel.invalidate()
        self._proposal_pending.clear(); self.reminder_cursor = None
        self.end_attention.hide()
        self.schedule_edit.hide()
        for page in [self.tasks, self.schedules, self.reminders, self.proposals]: page.clear_sensitive()
        self._update_reminder_actions()
        self._task({}); self.goals.clear_sensitive(); self.uncertain.clear_sensitive()

@dataclass
class _GoalRead:
    parameters: dict[str, Any]
    callback: Callable[[dict[str, Any]], None]


class GoalsPage(QWidget):
    def __init__(self, bridge: Any, parent: Any = None) -> None:
        super().__init__(parent); self.channel = Channel(bridge, self); self.snapshots: dict[str, dict[str, Any]] = {}; self.sources: list[dict[str, Any]] = []
        self._read_epoch = 0
        self._read_active: dict[str, _GoalRead] = {}
        self._read_latest: dict[str, _GoalRead] = {}
        self._children_goal_id: str | None = None
        self._new_use_policy: str | None = None
        self._drafts: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
        layout = QVBoxLayout(self); self.notice = notice_label(); self.notice.setText("目标完成需要人工证据，任务完成不会自动完成目标。"); layout.addWidget(self.notice); self.channel.notice.connect(self.notice.setText)
        self.parent_goal = ArrowComboBox(); layout.addWidget(self.parent_goal); self.parent_goal.currentIndexChanged.connect(lambda _: self._children())
        self.tabs = QTabWidget(); layout.addWidget(self.tabs)
        self.parent_goal.hide()
        self.tabs.currentChanged.connect(lambda index: self.parent_goal.setVisible(index != 0))
        self.pages: dict[str, PagedView] = {}; self.forms: dict[str, CommandForm] = {}
        self.editor_tabs: dict[str, QTabWidget] = {}
        self.editor_hosts: dict[str, QStackedWidget] = {}
        self._creating: dict[str, bool] = {}
        self._writing: set[str] = set()
        self._created_selection: dict[str, str] = {}
        self._latest_for_edit: dict[str, dict[str, Any]] = {}
        self.adopt_buttons: dict[str, QPushButton] = {}
        self.create_buttons: dict[str, QPushButton] = {}; self.status_buttons: dict[str, QPushButton] = {}
        for name, title in [("goal", "目标"), ("milestone", "分步计划"), ("binding", "关联提醒与任务")]:
            page = PagedView(name); self.pages[name] = page; self.tabs.addTab(page, title)
            page.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            page.list.customContextMenuRequested.connect(lambda position, name=name: self._context_menu(name, position))
            page.refresh_requested.connect(lambda name=name: self.refresh() if name == "goal" else self._children())
            page.selected.connect(lambda item, name=name: self._selected(name, item))
            if name == "binding": continue
            fields: list[Any] = [("title", "名称", None), ("success_criteria", "完成标准", None), ("deadline", "截止时间（带时区，留空不设）", None), ("progress_note", "进度备注", None)]
            fields += [("status", "状态", ["Active", "Paused", "Completed", "Cancelled"] if name == "goal" else ["Pending", "Completed", "Cancelled"]), ("evidence", "完成证据", None)]
            form = CommandForm("选择记录后可修改；保存使用已审阅版本。", fields); self.forms[name] = form
            self._creating[name] = False
            form.submit.setText("保存所选修改"); form.submit.setEnabled(False); form.submitted.connect(lambda values, name=name: self._save(name, values, self._creating.get(name, False)))
            self._add_editor(name, page, form)
            create = QPushButton("新建" + title); create.clicked.connect(lambda checked=False, name=name: self._open_new(name)); page.action_bar.addWidget(create); self.create_buttons[name] = create
            status = QPushButton("保存状态与完成证据"); status.clicked.connect(lambda checked=False, name=name: self._status(name))
            form_layout = form.layout(); assert isinstance(form_layout, QFormLayout)
            form_layout.addRow(status); status.hide(); self.status_buttons[name] = status
            adopt = QPushButton('采用刚读取的版本，保留编辑草稿')
            adopt.clicked.connect(lambda checked=False, name=name: self._adopt_revision(name))
            adopt.hide(); form_layout.addRow(adopt); self.adopt_buttons[name] = adopt
        self.binding_form = CommandForm("按名称关联已知任务、提醒或日程。", [("status", "关联状态", ["Active", "Removed"])])
        binding_layout = self.binding_form.layout()
        assert isinstance(binding_layout, QFormLayout)
        self.source = ArrowComboBox(); self.milestone = ArrowComboBox(); binding_layout.insertRow(1, "关联对象", self.source); binding_layout.insertRow(2, "所属步骤", self.milestone)
        self.source_offset = 0
        self.source_more = QPushButton("更多日程 / 计时来源"); self.source_more.clicked.connect(self._next_sources); binding_layout.addRow(self.source_more)
        source_first = QPushButton("返回首批来源"); source_first.clicked.connect(lambda: self._next_sources(True)); binding_layout.addRow(source_first)
        self.binding_form.submit.setText("新增关联"); self.binding_form.submitted.connect(self._bind)
        self._add_editor("binding", self.pages["binding"], self.binding_form)
        adopt_binding = QPushButton('采用刚读取的版本，保留编辑草稿')
        adopt_binding.clicked.connect(lambda: self._adopt_revision('binding'))
        adopt_binding.hide(); binding_layout.addRow(adopt_binding); self.adopt_buttons['binding'] = adopt_binding
        self.create_binding_button = QPushButton('新增关联')
        self.create_binding_button.setEnabled(False)
        self.create_binding_button.clicked.connect(self._open_new_binding)
        self.pages['binding'].action_bar.addWidget(self.create_binding_button)
        self.create_buttons["milestone"].setEnabled(False)
        self.update_binding_button = QPushButton("保存所选关联状态"); self.update_binding_button.clicked.connect(self._update_binding); binding_layout.addRow(self.update_binding_button); self.update_binding_button.setEnabled(False)

    def _add_editor(self, name: str, page: PagedView, form: CommandForm) -> None:
        # Reading and editing share the full detail column instead of splitting
        # it into two narrow strips. Keep the public forms for command callers.
        tabs = QTabWidget(); tabs.setMinimumWidth(0)
        tabs.addTab(page.detail_panel, "记录详情")
        host = QStackedWidget(); host.addWidget(text_label('选择一条记录查看详情。'))
        host.addWidget(scroll(form)); self.editor_hosts[name] = host
        tabs.addTab(host, "编辑表单")
        self.editor_tabs[name] = tabs
        page.splitter.addWidget(tabs)
        page.splitter.setStretchFactor(0, 1); page.splitter.setStretchFactor(1, 3)
        page.splitter.setSizes([220, 660])

    def refresh(self) -> None:
        if self._new_use_policy is None: self._read('settings', {}, self._settings)
        self._read("goals", {"offset": self.pages["goal"].offset, "limit": 50}, self._goals)
        self._read("tasks", {"offset": 0, "limit": 100}, self._sources_tasks)
        self._read("schedule_items", {"source": "all", "offset": self.source_offset, "limit": 100, "status": "all"}, self._sources_schedule)
        self._children()

    def _settings(self, data: dict[str, Any]) -> None:
        if type(data.get('request_use_default')) is bool:
            self._new_use_policy = 'cloud_allowed' if data['request_use_default'] else 'local_only'

    def _read(self, operation: str, parameters: dict[str, Any], callback: Callable[[dict[str, Any]], None]) -> None:
        if str(getattr(self.channel.bridge, "state", "")) != "Ready": return
        job = _GoalRead(dict(parameters), callback)
        if operation in self._read_active:
            # Even identical parameters require a new read after a successful write.
            self._read_latest[operation] = job
        else:
            self._start_read(operation, job)

    def _start_read(self, operation: str, job: _GoalRead) -> None:
        epoch = self._read_epoch
        self._read_active[operation] = job
        def done(data: dict[str, Any]) -> None:
            if epoch != self._read_epoch or self._read_active.get(operation) is not job: return
            self._read_active.pop(operation)
            latest = self._read_latest.pop(operation, None)
            if str(getattr(self.channel.bridge, "state", "")) != "Ready": return
            if latest is not None:
                self._start_read(operation, latest)
            elif self._read_current(operation, job.parameters):
                job.callback(data)
        if self.channel.send(operation, job.parameters, done) is None:
            self._read_active.pop(operation, None)

    def _read_current(self, operation: str, parameters: dict[str, Any]) -> bool:
        if operation == "goals": return bool(parameters["offset"] == self.pages["goal"].offset)
        if operation == "schedule_items": return bool(parameters["offset"] == self.source_offset)
        if operation in {"milestones", "bindings"}:
            goal = self.parent_goal.currentData() or {}
            return bool(parameters["goal_id"] == goal.get("id") and parameters["offset"] == self.pages[operation[:-1]].offset)
        return True

    def _goals(self, data: dict[str, Any]) -> None:
        if "_error" in data: return
        self.pages["goal"].set_page(data)
        chosen = self._select_created('goal') or self.parent_goal.currentData(); self.parent_goal.blockSignals(True); self.parent_goal.clear()
        for item in data.get("items", []): self.parent_goal.addItem(str(item.get("title", "目标")), item)
        for index in range(self.parent_goal.count()):
            if chosen and self.parent_goal.itemData(index).get("id") == chosen.get("id"): self.parent_goal.setCurrentIndex(index)
        self.parent_goal.blockSignals(False); self._children()

    def _children(self) -> None:
        goal = self.parent_goal.currentData()
        identity = str(goal["id"]) if goal else None
        if identity != self._children_goal_id:
            self._children_goal_id = identity
            for name in ["milestone", "binding"]:
                self.pages[name].clear_sensitive(); self.snapshots.pop(name, None)
            self.forms["milestone"].clear_sensitive(); self.binding_form.clear_sensitive(); self.milestone.clear()
            self.create_buttons["milestone"].setEnabled(bool(goal))
        if not goal:
            self._read_latest.pop("milestones", None); self._read_latest.pop("bindings", None)
        self.create_binding_button.setEnabled(bool(goal))
        if not goal: return
        for name in ["milestone", "binding"]:
            def done(data: dict[str, Any], name: str = name) -> None:
                self._child_page(name, data)
            self._read(name + "s", {"goal_id": goal["id"], "offset": self.pages[name].offset, "limit": 50}, done)

    def _child_page(self, name: str, data: dict[str, Any]) -> None:
        if "_error" in data: return
        self.pages[name].set_page(data)
        self._select_created(name)
        if name == "milestone":
            selected = self.milestone.currentData(); self.milestone.blockSignals(True); self.milestone.clear(); self.milestone.addItem("整个目标", None)
            for item in data.get("items", []): self.milestone.addItem(str(item.get("title", "步骤")), item["id"])
            index = self.milestone.findData(selected)
            if index >= 0: self.milestone.setCurrentIndex(index)
            self.milestone.blockSignals(False)

    def _selected(self, name: str, item: dict[str, Any]) -> None:
        form = self.binding_form if name == "binding" else self.forms[name]
        old = self.snapshots.get(name, {})
        if self._creating.get(name) and form.dirty and not item: return
        if form.dirty and item.get('id') and item.get("id") == old.get("id"):
            self.pages[name].show_detail(item)
            if item.get('revision') != old.get('revision'):
                self._latest_for_edit[name] = dict(item)
                if name in self.adopt_buttons: self.adopt_buttons[name].show()
                self.notice.setText('记录版本已变化，草稿已保留。请核对详情后采用刚读取的版本。')
            return
        self._latest_for_edit.pop(name, None)
        if name in self.adopt_buttons: self.adopt_buttons[name].hide()
        if form.dirty and old.get("id"):
            self._drafts[(name, str(old["id"]))] = (dict(old), form.values())
        self.snapshots[name] = dict(item); self.pages[name].show_detail(item)
        self._creating[name] = False
        form.submit.setText('新增关联' if name == 'binding' else '保存所选修改')
        self.editor_hosts[name].setCurrentIndex(1 if item else 0)
        if name in self.status_buttons: self.status_buttons[name].setVisible(bool(item))
        if name == "binding":
            self.update_binding_button.setEnabled(bool(item) and item.get("status") == "Active")
            self.update_binding_button.setVisible(bool(item))
            form.submit.setVisible(False)
        elif name == "goal":
            selected_parent = self.parent_goal.currentData() or {}
            if item.get('id') and selected_parent.get('id') != item['id']:
                for index in range(self.parent_goal.count()):
                    if self.parent_goal.itemData(index).get('id') == item['id']:
                        self.parent_goal.setCurrentIndex(index); break
            editable = bool(item) and item.get("status") not in {"Completed", "Cancelled"}
            self.status_buttons[name].setEnabled(editable and name not in self._writing); self.forms[name].submit.setEnabled(editable and name not in self._writing)
        else:
            editable = bool(item) and item.get("status") == "Pending"
            self.status_buttons[name].setEnabled(editable and name not in self._writing); self.forms[name].submit.setEnabled(editable and name not in self._writing)
        draft = self._drafts.get((name, str(item.get("id", ""))))
        if draft is not None:
            self.snapshots[name] = dict(draft[0]); form.populate(draft[1]); form.dirty = True
            self.notice.setText("已恢复这条记录的未保存编辑，请审阅当前记录后保存。")
            if item.get('revision') != draft[0].get('revision'):
                self._latest_for_edit[name] = dict(item)
                if name in self.adopt_buttons: self.adopt_buttons[name].show()
        elif item: form.populate(item)
        else: form.clear_sensitive()

    def _open_selected_editor(self, name: str) -> None:
        if not self.snapshots.get(name): return
        if name in self.editor_tabs: self.editor_tabs[name].setCurrentIndex(1)

    def _open_new(self, name: str) -> None:
        if name == 'milestone' and not self.parent_goal.currentData(): return
        form = self.forms[name]
        old = self.snapshots.get(name, {})
        if form.dirty and old.get('id'):
            self._drafts[(name, str(old['id']))] = (dict(old), form.values())
        self.snapshots[name] = {}; self._creating[name] = True
        self._latest_for_edit.pop(name, None); self.adopt_buttons[name].hide()
        form.clear_sensitive(); form.submit.setText('新建目标' if name == 'goal' else '新建分步计划')
        form.submit.setEnabled(name not in self._writing)
        self.status_buttons[name].hide()
        self.editor_hosts[name].setCurrentIndex(1); self.editor_tabs[name].setCurrentIndex(1)
        form.fields['title'].setFocus()

    def _adopt_revision(self, name: str) -> None:
        latest = self._latest_for_edit.get(name)
        if not latest or latest.get('id') != self.snapshots.get(name, {}).get('id'): return
        self.snapshots[name] = dict(latest); self._latest_for_edit.pop(name, None)
        self._drafts.pop((name, str(latest['id'])), None)
        if name in self.adopt_buttons: self.adopt_buttons[name].hide()
        if name == 'binding':
            self.update_binding_button.setEnabled(latest.get('status') == 'Active' and name not in self._writing)
        else:
            editable = latest.get('status') in ({'Active', 'Paused'} if name == 'goal' else {'Pending'})
            self.forms[name].submit.setEnabled(editable and name not in self._writing)
            self.status_buttons[name].setEnabled(editable and name not in self._writing)
        self.notice.setText('已采用刚读取的版本，编辑草稿保留，保存后才会修改记录。')

    def _open_new_binding(self) -> None:
        if not self.parent_goal.currentData(): return
        self.snapshots['binding'] = {}; self._creating['binding'] = True
        self.binding_form.clear_sensitive(); self.binding_form.submit.show()
        self.binding_form.submit.setEnabled('binding' not in self._writing)
        self.update_binding_button.hide()
        self.editor_hosts['binding'].setCurrentIndex(1); self.editor_tabs['binding'].setCurrentIndex(1)

    def _select_created(self, name: str) -> dict[str, Any] | None:
        identity = self._created_selection.get(name)
        if not identity: return None
        page = self.pages[name]
        for index, item in enumerate(page.items):
            if item.get('id') == identity:
                self._created_selection.pop(name, None); page.list.setCurrentRow(index)
                return item
        return None

    def _context_menu(self, name: str, position: Any) -> None:
        page = self.pages[name]; row = page.list.itemAt(position)
        index = page.list.row(row) if row is not None else -1
        if not 0 <= index < len(page.items): return
        page.list.setCurrentRow(index)
        item = page.selected_item(); menu = QMenu(page.list)
        if name == "binding":
            if item.get("status") == "Active": menu.addAction("编辑所选关联", lambda: self._open_selected_editor(name))
        elif name == "goal":
            if item.get("status") not in {"Completed", "Cancelled"}:
                menu.addAction("编辑所选目标", lambda: self._open_selected_editor(name))
                menu.addAction("填写目标状态与完成证据", lambda: self._open_selected_editor(name))
        elif item.get("status") == "Pending":
            menu.addAction("编辑所选分步计划", lambda: self._open_selected_editor(name))
            menu.addAction("填写分步计划状态与完成证据", lambda: self._open_selected_editor(name))
        if menu.actions(): menu.exec(page.list.viewport().mapToGlobal(position))

    def _write(self, op: str, params: dict[str, Any]) -> None:
        epoch = self._read_epoch
        target = "milestone" if "milestone" in op else "binding" if "bind" in op else "goal"
        if target in self._writing: return
        self._writing.add(target)
        form = self.binding_form if target == "binding" else self.forms[target]
        form.submit.setEnabled(False)
        submitted_values = form.values()
        submitted_id = str(self.snapshots.get(target, {}).get("id", ""))
        def done(data: dict[str, Any]) -> None:
            if epoch != self._read_epoch: return
            self._writing.discard(target)
            form.submit.setEnabled(True)
            if "_error" in data: self.notice.setText("保存失败，编辑内容已保留；请审阅当前版本。"); return
            self._drafts.pop((target, submitted_id), None)
            if (str(self.snapshots.get(target, {}).get("id", "")) == submitted_id
                    and form.values() == submitted_values):
                form.mark_clean()
                if (op.startswith('create_') or op == 'bind') and data.get('id'):
                    self.snapshots[target] = dict(data); self._creating[target] = False
                    form.submit.setText('保存所选修改')
                    self.pages[target].offset = 0; self._created_selection[target] = str(data['id'])
            self.refresh()
        if self.channel.send(op, params, done, mutation=True) is None:
            self._writing.discard(target); form.submit.setEnabled(True)

    def _save(self, name: str, values: dict[str, Any], create: bool) -> None:
        params = {key: value for key, value in values.items() if key not in {"status", "evidence"}}
        params["deadline"] = params.get("deadline") or None
        if create:
            if name == 'goal':
                if self._new_use_policy is None:
                    self.notice.setText('正在读取设置，请稍后再新建目标。'); self.refresh(); return
                params['use_policy'] = self._new_use_policy
            params.pop("progress_note", None)
            if name == "milestone":
                goal = self.parent_goal.currentData()
                if not goal: return
                params.update(goal_id=goal["id"], expected_revision=goal["revision"])
        else:
            item = self.snapshots.get(name, {})
            if not item: self.notice.setText("请先选择要修改的记录。"); return
            params.update({name + "_id": item["id"], "expected_revision": item["revision"]})
        self._write(("create_" if create else "update_") + name, params)

    def _status(self, name: str) -> None:
        item = self.snapshots.get(name, {}); values = self.forms[name].values()
        if item: self._write(name + "_status", {name + "_id": item["id"], "expected_revision": item["revision"], "status": values["status"], "evidence": values["evidence"]})

    def _sources_tasks(self, data: dict[str, Any]) -> None:
        if "_error" in data: return
        self.sources = [item for item in self.sources if item["source"] != "task"]
        self.sources.extend({"source": "task", "source_id": item["id"], "title": item.get("title") or item.get("request") or "任务"} for item in data.get("items", [])); self._source_choices()

    def _sources_schedule(self, data: dict[str, Any]) -> None:
        if "_error" in data: return
        self.sources = [item for item in self.sources if item["source"] == "task"]
        for item in data.get("items", []):
            source = item.get("source"); source_id = item.get("source_id", item.get("id"))
            if source in {"timer", "calendar"} and source_id: self.sources.append({"source": source, "source_id": source_id, "title": item.get("title") or item.get("label") or "提醒"})
        self.source_more.setEnabled(bool(data.get("has_more"))); self._source_choices()

    def _next_sources(self, first: bool = False) -> None:
        self.source_offset = 0 if first else self.source_offset + 100
        self._read("schedule_items", {"source": "all", "offset": self.source_offset, "limit": 100, "status": "all"}, self._sources_schedule)

    def _source_choices(self) -> None:
        selected = self.source.currentData(); self.source.blockSignals(True); self.source.clear()
        choices = list(self.sources)
        if selected and not any((item["source"], item["source_id"]) == (selected["source"], selected["source_id"]) for item in choices):
            choices.insert(0, selected)
        for item in choices: self.source.addItem(str(item["title"])[:100], item)
        if selected:
            for index in range(self.source.count()):
                if (self.source.itemData(index).get("source"), self.source.itemData(index).get("source_id")) == (selected.get("source"), selected.get("source_id")): self.source.setCurrentIndex(index)
        self.source.blockSignals(False)

    def _bind(self, values: dict[str, Any]) -> None:
        goal = self.parent_goal.currentData(); source = self.source.currentData()
        if self._new_use_policy is None:
            self.notice.setText('正在读取设置，请稍后再新增关联。'); self.refresh(); return
        if goal and source: self._write("bind", {"goal_id": goal["id"], "expected_revision": goal["revision"], "source": source["source"], "source_id": source["source_id"], "milestone_id": self.milestone.currentData(), "use_policy": self._new_use_policy})
        else: self.notice.setText("请先选择目标和关联对象。")

    def _update_binding(self) -> None:
        item = self.snapshots.get("binding", {})
        if item and item.get("status") == "Active": self._write("update_binding", {"binding_id": item["id"], "expected_revision": item["revision"], **self.binding_form.values()})

    def has_unsaved_content(self) -> bool:
        return bool(self._drafts) or self.binding_form.dirty or any(form.dirty for form in self.forms.values())

    def clear_sensitive(self) -> None:
        self._read_epoch += 1; self._read_active.clear(); self._read_latest.clear(); self.channel.invalidate()
        self.parent_goal.clear(); self.source.clear(); self.milestone.clear()
        for page in self.pages.values(): page.clear_sensitive()
        for form in self.forms.values(): form.clear_sensitive()
        self.binding_form.clear_sensitive(); self.source_offset = 0
        for button in self.status_buttons.values(): button.setEnabled(False)
        for form in self.forms.values(): form.submit.setEnabled(False)
        self.update_binding_button.setEnabled(False)
        self.create_buttons["milestone"].setEnabled(False)
        # Clearing selections emits synchronous callbacks that can store empty snapshots.
        self.snapshots.clear(); self.sources.clear(); self._drafts.clear()
        self._new_use_policy = None
        self._writing.clear(); self._creating.clear()
        self._created_selection.clear()
        self._latest_for_edit.clear()
        for button in self.adopt_buttons.values(): button.hide()
        for host in self.editor_hosts.values(): host.setCurrentIndex(0)
