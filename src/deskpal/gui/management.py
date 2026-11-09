"""Readable memory, arrangement and goal workflows over the async channel."""
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget)
from .components import (PushButton as QPushButton, Tabs as QTabWidget)
from .channel import Channel
from .views import PagedView
from .widgets import ArrowComboBox, notice_label, scroll, text_label
from .studio import page_layout

class MemoryPage(QWidget):
    """记忆只读浏览；修改意图通过聊天进入统一模型管理链路。"""
    open_conversation = Signal(str, str)
    def __init__(self, bridge: Any, parent: Any = None) -> None:
        super().__init__(parent); self.channel = Channel(bridge, self)
        layout = page_layout(self)
        layout.addWidget(text_label("记忆", heading=True))
        layout.addWidget(text_label("查看记住的事实、共同经历和仍待明确的理解。需要补充、更正或遗忘时，请在聊天中告诉我。"))
        self.notice = notice_label(); layout.addWidget(self.notice)
        self.channel.notice.connect(self.notice.setText)
        from qfluentwidgets import Pivot
        from .components import LineEdit
        self.categories = Pivot()
        layout.addWidget(self.categories)
        self.query = LineEdit(); self.query.setMaxLength(200); self.query.setPlaceholderText('查找当前分类的记忆，按 Enter 搜索')
        self.query.returnPressed.connect(lambda: self._filter(0))
        layout.addWidget(self.query)
        self.page = PagedView("memories"); layout.addWidget(self.page, 1)
        self.filter = ArrowComboBox()
        for value, title in [('facts', '事实与偏好'), ('episode', '共同经历'), ('buffer', '待确认的理解'), ('all', '全部记录')]:
            self.filter.addItem(title, value)
        for index in range(self.filter.count()):
            self.categories.addItem(str(index), self.filter.itemText(index), onClick=partial(self.filter.setCurrentIndex, index))
        self.categories.setCurrentItem('0')
        self.filter.setParent(self); self.filter.hide()
        self.filter.currentIndexChanged.connect(self._filter)
        self.page.refresh_requested.connect(self.refresh)
        self.page.selected.connect(self._selected)
        self.source_button = QPushButton('查看交流来源'); self.source_button.hide()
        self.source_button.clicked.connect(self._source)
        self.page.details_layout.addWidget(self.source_button)
        self.page.action_scroll.hide()
        self.learning_button = QPushButton('学习记录')
        self.learning_button.clicked.connect(self._learning_records)
        buttons = QHBoxLayout(); buttons.addWidget(self.learning_button); buttons.addStretch()
        layout.addLayout(buttons)
        self._read_epoch = 0; self._reading = False; self._read_again = False
        if hasattr(bridge, 'privacy_changed'): bridge.privacy_changed.connect(self.clear_sensitive)

    def _selected(self, item: dict[str, Any]) -> None:
        self.page.show_detail(item)
        source = item.get('source') or {}
        self.source_button.setVisible(bool(source.get('session_id')) and item.get('status') != 'deleted')

    def _source(self) -> None:
        source = self.page.selected_item().get('source') or {}
        if source.get('session_id'):
            self.open_conversation.emit(str(source['session_id']), str(source.get('task_id') or ''))

    def _filter(self, index: int) -> None:
        self.categories.setCurrentItem(str(self.filter.currentIndex()))
        self.page.offset = 0; self.refresh()

    def refresh(self) -> None:
        if str(getattr(self.channel.bridge, 'state', '')) != 'Ready': return
        self.page.loading()
        if self._reading:
            self._read_again = True; return
        self._read_memories()

    def _read_memories(self) -> None:
        self._reading = True; self._read_again = False
        epoch = self._read_epoch
        params: dict[str, Any] = {"offset": self.page.offset, "limit": 50,
                                  'group': self.filter.currentData() or 'facts', 'query': self.query.text().strip()}
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

    def clear_sensitive(self, epoch: int = 0) -> None:
        for dialog in self.findChildren(QDialog):
            for value in dialog.findChildren(QLabel): value.clear()
            dialog.close()
        self._read_epoch += 1; self._reading = False; self._read_again = False
        self.channel.invalidate(); self.page.clear_sensitive(); self.query.clear(); self.source_button.hide()

class ArrangementsPage(QWidget):
    """只读安排工作台。授权、暂停与修改统一在聊天内处理。"""
    open_conversation = Signal(str, str)
    edit_schedule_requested = Signal(object)

    def __init__(self, coordinator: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self.coordinator = coordinator
        self.channel = Channel(coordinator.bridge, self)
        self._read_epoch = 0
        self._reads: set[str] = set()
        self._latest_reads: dict[str, tuple[dict[str, Any], Callable[[dict[str, Any]], None]]] = {}
        layout = page_layout(self)
        layout.addWidget(text_label("安排", heading=True))
        caption = text_label("查看日程和任务进度。新增、调整或取消时，在聊天中提出。")
        caption.setObjectName('subtle'); layout.addWidget(caption)
        self.notice = notice_label(); layout.addWidget(self.notice)
        self.channel.notice.connect(self.notice.setText)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        self.schedule_tabs = QTabWidget(); self.tabs.addTab(self.schedule_tabs, "日程与提醒")
        self.task_tabs = QTabWidget(); self.tabs.addTab(self.task_tabs, "任务进度")
        self.schedules = PagedView('schedules'); self.schedule_tabs.addTab(self.schedules, '即将到来与历史')
        self.schedule_filter = ArrowComboBox()
        self.schedule_filter.addItem('即将到来', 'upcoming'); self.schedule_filter.addItem('全部与历史', 'all')
        self.schedules.toolbar.insertWidget(0, self.schedule_filter)
        self.schedule_filter.currentIndexChanged.connect(self._schedule_filter)
        self.schedules.refresh_requested.connect(self.refresh_schedules)
        self.schedules.selected.connect(self._schedule)
        self.schedule_edit = QPushButton('前往聊天处理'); self.schedule_edit.hide()
        self.schedule_edit.clicked.connect(lambda: self.edit_schedule_requested.emit(self.schedules.selected_item()))
        self.schedules.details_layout.addWidget(self.schedule_edit)
        self.reminders = PagedView('reminders'); self.schedule_tabs.addTab(self.reminders, '到期记录')
        self.reminder_cursor: Any = None
        self.reminders.refresh_requested.connect(self.refresh_reminders)
        self.reminders.previous.clicked.disconnect(); self.reminders.next.clicked.disconnect()
        self.reminders.previous.clicked.connect(lambda: self.refresh_reminders(False))
        self.reminders.next.clicked.connect(lambda: self.refresh_reminders(True))
        self.reminders.selected.connect(self._reminder)
        self.open_reminder_button = QPushButton('查看来源会话'); self.open_reminder_button.hide()
        self.open_reminder_button.clicked.connect(self._open_reminder)
        self.reminders.details_layout.addWidget(self.open_reminder_button)
        self.tasks = PagedView('arrangements'); self.task_tabs.addTab(self.tasks, '执行任务')
        self.task_filter = ArrowComboBox()
        for key, title in [('active','进行中的任务'), ('Paused','已暂停'), ('Completed','已完成'), ('all','全部与历史')]:
            self.task_filter.addItem(title, key)
        self.tasks.toolbar.insertWidget(0, self.task_filter)
        self.task_filter.currentIndexChanged.connect(self._filter_tasks)
        self.tasks.refresh_requested.connect(self.refresh_tasks)
        self.tasks.selected.connect(self._task)
        self.return_button = QPushButton('前往聊天处理'); self.return_button.hide()
        self.return_button.clicked.connect(self._return)
        self.tasks.details_layout.addWidget(self.return_button)
        self._task_origin: dict[str, Any] = {}
        self.goals = GoalsPage(coordinator.bridge); self.task_tabs.addTab(self.goals, '目标与分步计划')
        self.proposals = PagedView('attentions'); self.tabs.addTab(self.proposals, '后续关注')
        self.proposals.refresh_requested.connect(self.refresh_proposals)
        self.proposals.selected.connect(self._attention)
        self.attention_chat = QPushButton('前往聊天处理'); self.attention_chat.hide()
        self.attention_chat.clicked.connect(self._open_attention)
        self.proposals.details_layout.addWidget(self.attention_chat)
        self.tabs.currentChanged.connect(lambda _: self.refresh())
        self.task_tabs.currentChanged.connect(lambda _: self.refresh())
        self.schedule_tabs.currentChanged.connect(lambda _: self.refresh())
        if hasattr(coordinator.bridge, 'privacy_changed'):
            coordinator.bridge.privacy_changed.connect(self.clear_sensitive)

    def refresh(self) -> None:
        if self.tabs.currentIndex() == 0:
            if self.schedule_tabs.currentIndex() == 0: self.refresh_schedules()
            else: self.refresh_reminders()
        elif self.tabs.currentIndex() == 1:
            if self.task_tabs.currentIndex() == 0: self.refresh_tasks()
            else: self.goals.refresh()
        else: self.refresh_proposals()

    def refresh_tasks(self) -> None:
        self.tasks.loading()
        params: dict[str, Any] = {'offset': self.tasks.offset, 'limit': 50}
        selected = self.task_filter.currentData()
        if selected != 'all':
            params['statuses'] = (['Created','Planning','Running','Waiting','NeedsReview','Paused','AwaitingInput','Uncertain']
                                  if selected == 'active' else [selected])
        self._read('tasks', params, self.tasks.set_page)

    def _filter_tasks(self, index: int) -> None:
        self.tasks.offset = 0; self.refresh_tasks()

    def _task(self, item: dict[str, Any]) -> None:
        self.tasks.show_detail(item); self._task_origin = dict(item)
        self.return_button.setVisible(bool(item))
        if not item: return
        identity = str(item.get('id', item.get('task_id', '')))
        def done(data: dict[str, Any]) -> None:
            if '_error' in data or str(self.tasks.selected_item().get('id')) != identity: return
            self._task_origin = {**item, **data}
            task = data.get('task', {})
            self.tasks.show_detail({**item, **task, 'response': data.get('response', task.get('response', ''))})
        self._read('task_review', {'task_id': identity}, done)

    def _return(self) -> None:
        item = self._task_origin
        if item.get('conversation_id'):
            self.open_conversation.emit(str(item['conversation_id']), str(item.get('id', item.get('task_id', ''))))
        elif self.coordinator.default_id:
            self.open_conversation.emit(self.coordinator.default_id, '')

    def refresh_schedules(self) -> None:
        self.schedules.loading()
        self._read('schedule_items', {'source':'all', 'offset':self.schedules.offset, 'limit':50,
                                     'status':self.schedule_filter.currentData()}, self.schedules.set_page)

    def _schedule_filter(self, index: int) -> None:
        self.schedules.offset = 0; self.refresh_schedules()

    def _schedule(self, item: dict[str, Any]) -> None:
        self.schedules.show_detail(item); self.schedule_edit.setVisible(bool(item))

    def refresh_reminders(self, more: bool = False) -> None:
        self.reminders.loading()
        def done(data: dict[str, Any]) -> None:
            if '_error' in data: self.reminders.load_failed(); return
            self.reminder_cursor = data.get('next_cursor'); self.reminders.set_page(data)
        self._read('reminder_feed', {'cursor': self.reminder_cursor if more else None, 'limit':50, 'unread':False}, done)

    def _reminder(self, item: dict[str, Any]) -> None:
        self.reminders.show_detail(item)
        self.open_reminder_button.setVisible(bool(item.get('conversation_id')))

    def _open_reminder(self) -> None:
        item = self.reminders.selected_item()
        if item.get('conversation_id'):
            self.open_conversation.emit(str(item['conversation_id']), str(item.get('task_id') or ''))

    def refresh_proposals(self) -> None:
        self.proposals.loading()
        self._read('attentions', {'offset':self.proposals.offset, 'limit':50}, self.proposals.set_page)

    def _attention(self, item: dict[str, Any]) -> None:
        self.proposals.show_detail(item); self.attention_chat.setVisible(bool(item))

    def _open_attention(self) -> None:
        item = self.proposals.selected_item()
        identity = item.get('conversation_id') or self.coordinator.default_id
        if identity: self.open_conversation.emit(str(identity), '')

    def clear_sensitive(self, epoch: int = 0) -> None:
        self._read_epoch += 1; self._reads.clear(); self._latest_reads.clear(); self.channel.invalidate()
        self.reminder_cursor = None; self._task_origin.clear()
        for button in (self.schedule_edit, self.return_button, self.open_reminder_button, self.attention_chat): button.hide()
        for page in (self.tasks, self.schedules, self.reminders, self.proposals): page.clear_sensitive()
        self.goals.clear_sensitive()

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


@dataclass
class _GoalRead:
    parameters: dict[str, Any]
    callback: Callable[[dict[str, Any]], None]


class GoalsPage(QWidget):
    """浏览目标、步骤和关联；不创建编辑器，也不保留旧的写入通路。"""
    def __init__(self, bridge: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self.channel = Channel(bridge, self)
        self._read_epoch = 0
        self._read_active: dict[str, _GoalRead] = {}
        self._read_latest: dict[str, _GoalRead] = {}
        self._children_goal_id: str | None = None
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        self.notice = notice_label(); layout.addWidget(self.notice)
        self.channel.notice.connect(self.notice.setText)
        self.parent_goal = ArrowComboBox(); layout.addWidget(self.parent_goal)
        self.parent_goal.hide(); self.parent_goal.currentIndexChanged.connect(self._children)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        self.pages: dict[str, PagedView] = {}
        for name, title in [('goal','目标'), ('milestone','分步计划'), ('binding','关联安排')]:
            page = PagedView(name); self.pages[name] = page; self.tabs.addTab(page, title)
            page.selected.connect(page.show_detail)
            page.refresh_requested.connect(self.refresh if name == 'goal' else self._children)
        self.tabs.currentChanged.connect(self._tab)

    def _tab(self, index: int) -> None:
        self.parent_goal.setVisible(index != 0)
        if index != 0: self._children()

    def refresh(self) -> None:
        self._read('goals', {'offset':self.pages['goal'].offset, 'limit':50}, self._goals)

    def _goals(self, data: dict[str, Any]) -> None:
        if '_error' in data: self.pages['goal'].load_failed(); return
        self.pages['goal'].set_page(data)
        chosen = self.parent_goal.currentData() or {}
        self.parent_goal.blockSignals(True); self.parent_goal.clear()
        for item in data.get('items', []): self.parent_goal.addItem(str(item.get('title', '目标')), item)
        for index in range(self.parent_goal.count()):
            if self.parent_goal.itemData(index).get('id') == chosen.get('id'): self.parent_goal.setCurrentIndex(index)
        self.parent_goal.blockSignals(False)
        self._children()

    def _children(self, *args: Any) -> None:
        goal = self.parent_goal.currentData() or {}
        identity = goal.get('id')
        if identity != self._children_goal_id:
            self._children_goal_id = identity
            for name in ('milestone', 'binding'): self.pages[name].clear_sensitive()
        if not identity: return
        for name in ('milestone', 'binding'):
            page = self.pages[name]
            self._read(name + 's', {'goal_id':identity, 'offset':page.offset, 'limit':50}, page.set_page)

    def has_unsaved_content(self) -> bool:
        return False

    def clear_sensitive(self) -> None:
        self._read_epoch += 1; self.channel.invalidate(); self._read_active.clear(); self._read_latest.clear()
        self._children_goal_id = None; self.parent_goal.clear(); self.notice.clear()
        for page in self.pages.values(): page.clear_sensitive()

    def _read(self, operation: str, parameters: dict[str, Any], callback: Callable[[dict[str, Any]], None]) -> None:
        if str(getattr(self.channel.bridge, "state", "")) != "Ready": return
        job = _GoalRead(dict(parameters), callback)
        if operation in self._read_active:
            # 合并读取突发，旧页响应不得覆盖最新选择。
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
        if operation in {"milestones", "bindings"}:
            goal = self.parent_goal.currentData() or {}
            return bool(parameters["goal_id"] == goal.get("id") and parameters["offset"] == self.pages[operation[:-1]].offset)
        return True
