"""Readable memory, arrangement and goal workflows over the async channel."""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QComboBox, QFormLayout, QHBoxLayout, QPushButton, QTabWidget, QVBoxLayout, QWidget
from .channel import Channel
from .views import PagedView, CommandForm
from .widgets import label, scroll, text_label
from .task_card import TaskCard

class MemoryPage(QWidget):
    def __init__(self, bridge: Any, parent: Any = None) -> None:
        super().__init__(parent); self.channel = Channel(bridge, self)
        layout = QVBoxLayout(self); layout.addWidget(text_label("记忆", heading=True)); layout.addWidget(text_label("查看、审核与更正你认可的信息。遗忘会清理相关正文，聊天记录清空单独在设置完成。"))
        self.notice = text_label(); layout.addWidget(self.notice); self.channel.notice.connect(self.notice.setText)
        self.page = PagedView("memories"); layout.addWidget(self.page, 1)
        self.filter = QComboBox()
        for value in ["all", "candidate", "accepted", "disputed", "rejected", "deleted"]:
            self.filter.addItem(label(value), value)
        self.page.toolbar.insertWidget(0, self.filter)
        self.filter.currentIndexChanged.connect(self._filter); self.page.refresh_requested.connect(self.refresh)
        self.page.selected.connect(self._selected)
        for action, title in [("accept", "接受候选"), ("reject", "拒绝候选"), ("forget", "遗忘这条记忆及相关正文")]:
            button = QPushButton(title); button.clicked.connect(lambda checked=False, action=action: self.action(action)); self.page.action_bar.addWidget(button)
        self.forms = QTabWidget()
        self.correct = CommandForm("更正采用当前审阅版本，发生冲突时保留编辑内容。", [("content", "更正内容", None)])
        self.remember = CommandForm("新记忆用途随设置中的新增输入策略；正在读取当前策略。", [("content", "内容", None)])
        self._new_memory_policy: str | None = None; self.remember.submit.setEnabled(False)
        self.correct.submit.setText("保存更正"); self.remember.submit.setText("保存记忆")
        self.forms.addTab(self.correct, "更正"); self.forms.addTab(self.remember, "新记忆")
        layout.addWidget(scroll(self.forms)); self.correct.submitted.connect(lambda values: self.action("correct", values)); self.remember.submitted.connect(lambda values: self.action("remember", values))
        self._snapshot: dict[str, Any] = {}; self._pending = False
        if hasattr(bridge, "privacy_changed"): bridge.privacy_changed.connect(self.clear_sensitive)

    def _filter(self, index: int) -> None:
        self.page.offset = 0; self.refresh()

    def refresh(self) -> None:
        self.channel.send("settings", {}, self._settings)
        params: dict[str, Any] = {"offset": self.page.offset, "limit": 50}
        if self.filter.currentData() != "all": params["status"] = self.filter.currentData()
        self.channel.send("memories", params, self.page.set_page)

    def _settings(self, data: dict[str, Any]) -> None:
        if "_error" in data or type(data.get("request_use_default")) is not bool: return
        self._new_memory_policy = "cloud_allowed" if data["request_use_default"] else "local_only"
        self.remember.description.setText("新记忆用途随设置中的新增输入策略：" + label(self._new_memory_policy) + "。已有记忆的用途保持不变。")
        self.remember.submit.setEnabled(True)

    def _selected(self, item: dict[str, Any]) -> None:
        if self.correct.dirty and item.get("id") == self._snapshot.get("id"):
            self.page.show_detail(item); self.notice.setText("当前记录已有新版本，未保存更正已保留。请重新审阅后保存。"); return
        self._snapshot = dict(item); self.page.show_detail(item)
        if item: self.correct.populate({"content": item.get("content", item.get("draft", {}).get("content", ""))})

    def action(self, action: str, values: dict[str, Any] | None = None) -> None:
        if self._pending: return
        params = {"action": action, **(values or {})}
        if action == "remember":
            if self._new_memory_policy is None: self.notice.setText("请先读取设置中的新增输入策略，再保存记忆。"); return
            params["use_policy"] = self._new_memory_policy
        if action != "remember":
            if not self._snapshot: self.notice.setText("请先选择并审阅一条记忆。"); return
            params.update(memory_id=self._snapshot["id"], expected_revision=self._snapshot["revision"])
        self._pending = True
        def done(data: dict[str, Any]) -> None:
            self._pending = False
            if "_error" not in data:
                self.notice.setText("操作已受理，正在读取当前记忆。"); self.correct.mark_clean(); self.remember.mark_clean(); self.refresh()
        if self.channel.send("request_forget" if action == "forget" else "memory", {key: value for key, value in params.items() if action != "forget" or key != "action"}, done, mutation=True) is None: self._pending = False

    def clear_sensitive(self, epoch: int = 0) -> None:
        self.page.clear_sensitive(); self._snapshot = {}; self.correct.clear_sensitive(); self.remember.clear_sensitive(); self._pending = False
        self._new_memory_policy = None; self.remember.submit.setEnabled(False)

class ArrangementsPage(QWidget):
    open_conversation = Signal(str, str)
    def __init__(self, coordinator: Any, parent: Any = None) -> None:
        super().__init__(parent); self.coordinator = coordinator; self.channel = Channel(coordinator.bridge, self)
        layout = QVBoxLayout(self); layout.addWidget(text_label("安排", heading=True)); self.notice = text_label(); self.channel.notice.connect(self.notice.setText); layout.addWidget(self.notice)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        self.tasks = PagedView("arrangements"); self.tabs.addTab(self.tasks, "任务")
        self.task_filter = QComboBox()
        for key, title in [("Waiting", "待我确认"), ("Running", "进行中"), ("all", "历史与全部")]: self.task_filter.addItem(title, key)
        self.tasks.toolbar.insertWidget(0, self.task_filter); self.tasks.refresh_requested.connect(self.refresh_tasks); self.task_filter.currentIndexChanged.connect(lambda _: self.refresh_tasks())
        self.tasks.selected.connect(self._task)
        self.card_host = QWidget(); self.card_layout = QVBoxLayout(self.card_host); self.tasks.splitter.addWidget(scroll(self.card_host))
        back = QPushButton("返回原会话 · 定位这条消息"); self.tasks.action_bar.addWidget(back); back.clicked.connect(self._return)
        self.uncertain = CommandForm("结果待核实时，请提供人工审查证据。", [("verdict", "判断", ["unknown", "occurred", "not_occurred"]), ("evidence", "人工证据", None)])
        self.uncertain.submitted.connect(self._uncertain); self.card_layout.addWidget(self.uncertain); self.uncertain.hide()
        self.schedule_tabs = QTabWidget(); self.tabs.addTab(self.schedule_tabs, "提醒与日程")
        self.schedules = PagedView("schedules"); self.schedule_tabs.addTab(self.schedules, "当前计时 / 日程"); self.schedules.refresh_requested.connect(self.refresh_schedules); self.schedules.selected.connect(self.schedules.show_detail)
        self.reminders = PagedView("reminders"); self.schedule_tabs.addTab(self.reminders, "到期收件箱")
        self.reminders.refresh_requested.connect(self.refresh_reminders); self.reminders.selected.connect(self._reminder)
        self.reminder_cursor: Any = None
        self._proposal_pending: set[str] = set()
        self.reminders.previous.clicked.disconnect(); self.reminders.next.clicked.disconnect()
        self.reminders.previous.clicked.connect(lambda: self.refresh_reminders(False)); self.reminders.next.clicked.connect(lambda: self.refresh_reminders(True))
        ack = QPushButton("标记已读"); ack.clicked.connect(self._ack); self.reminders.action_bar.addWidget(ack)
        open_chat = QPushButton("查看原会话"); open_chat.clicked.connect(self._open_reminder); self.reminders.action_bar.addWidget(open_chat)
        self.goals = GoalsPage(coordinator.bridge); self.tabs.addTab(self.goals, "目标与分步计划")
        self.proposals = PagedView("proposals"); self.tabs.addTab(self.proposals, "建议")
        self.proposals.refresh_requested.connect(self.refresh_proposals); self.proposals.selected.connect(self.proposals.show_detail)
        for op, title in [("accept_proposal", "接受建议并排队（另行审核工具）"), ("ignore_proposal", "忽略建议")]:
            button = QPushButton(title); button.clicked.connect(lambda checked=False, op=op: self._proposal(op)); self.proposals.action_bar.addWidget(button)
        self.tabs.currentChanged.connect(lambda _: self.refresh())
        self.coordinator.task_changed.connect(self._task_changed)
        if hasattr(coordinator.bridge, "privacy_changed"): coordinator.bridge.privacy_changed.connect(self.clear_sensitive)

    def refresh(self) -> None:
        index = self.tabs.currentIndex()
        if index == 0: self.refresh_tasks()
        elif index == 1: self.refresh_schedules(); self.refresh_reminders()
        elif index == 2: self.goals.refresh()
        else: self.refresh_proposals()

    def refresh_tasks(self) -> None:
        params = {"offset": self.tasks.offset, "limit": 50}
        if self.task_filter.currentData() != "all": params["status"] = self.task_filter.currentData()
        self.channel.send("tasks", params, self.tasks.set_page)

    def _task(self, item: dict[str, Any]) -> None:
        self.tasks.show_detail({key: value for key, value in item.items() if key not in {"id", "plan_hash"}})
        while self.card_layout.count():
            child = self.card_layout.takeAt(0)
            widget = child.widget() if child is not None else None
            if widget is not None and widget is not self.uncertain: widget.deleteLater()
        if item:
            identity = str(item.get("id", item.get("task_id", "")))
            self.card_layout.addWidget(TaskCard(self.coordinator, identity)); self.coordinator.review(identity)
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
        self.channel.send("schedule_items", {"source": "all", "offset": self.schedules.offset, "limit": 50, "status": "all"}, self.schedules.set_page)

    def refresh_reminders(self, more: bool = False) -> None:
        def done(data: dict[str, Any]) -> None:
            if "_error" in data: return
            self.reminder_cursor = data.get("next_cursor"); self.reminders.set_page(data)
            self.reminders.page_label.setText(f"未读 {data.get('unread_count', 0)} 条")
        self.channel.send("reminder_feed", {"cursor": self.reminder_cursor if more else None, "limit": 50, "unread": False}, done)

    def _reminder(self, item: dict[str, Any]) -> None:
        self.reminders.show_detail(item)

    def _ack(self) -> None:
        item = self.reminders.selected_item()
        if item: self.channel.send("reminder_ack", {"id": item["id"]}, lambda data: self.refresh_reminders(), mutation=True)

    def _open_reminder(self) -> None:
        item = self.reminders.selected_item()
        if item.get("conversation_id"): self.open_conversation.emit(str(item["conversation_id"]), str(item.get("task_id") or ""))

    def refresh_proposals(self) -> None:
        self.channel.send("proposals", {"offset": self.proposals.offset, "limit": 50, "status": "all"}, self.proposals.set_page)

    def _proposal(self, operation: str) -> None:
        item = self.proposals.selected_item()
        if not item or item["id"] in self._proposal_pending: return
        identity = str(item["id"]); self._proposal_pending.add(identity)
        def done(data: dict[str, Any]) -> None:
            self._proposal_pending.discard(identity)
            if "_error" not in data: self.refresh_proposals()
        if self.channel.send(operation, {"proposal_id": identity, "expected_revision": item["revision"]}, done, mutation=True) is None:
            self._proposal_pending.discard(identity)

    def clear_sensitive(self, epoch: int = 0) -> None:
        self._proposal_pending.clear(); self.reminder_cursor = None
        for page in [self.tasks, self.schedules, self.reminders, self.proposals]: page.clear_sensitive()
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
        layout = QVBoxLayout(self); self.notice = text_label("目标完成需要人工证据，任务完成不会自动完成目标。"); layout.addWidget(self.notice); self.channel.notice.connect(self.notice.setText)
        self.parent_goal = QComboBox(); layout.addWidget(self.parent_goal); self.parent_goal.currentIndexChanged.connect(lambda _: self._children())
        self.tabs = QTabWidget(); layout.addWidget(self.tabs)
        self.pages: dict[str, PagedView] = {}; self.forms: dict[str, CommandForm] = {}
        for name, title in [("goal", "目标"), ("milestone", "分步计划"), ("binding", "关联提醒与任务")]:
            page = PagedView(name); self.pages[name] = page; self.tabs.addTab(page, title)
            page.refresh_requested.connect(lambda name=name: self.refresh() if name == "goal" else self._children())
            page.selected.connect(lambda item, name=name: self._selected(name, item))
            if name == "binding": continue
            fields: list[Any] = [("title", "名称", None), ("success_criteria", "完成标准", None), ("deadline", "截止时间（带时区，留空不设）", None), ("progress_note", "进度备注", None)]
            if name == "goal": fields.append(("use_policy", "使用范围", ["local_only", "cloud_allowed"]))
            fields += [("status", "状态", ["Active", "Paused", "Completed", "Cancelled"] if name == "goal" else ["Pending", "Completed", "Cancelled"]), ("evidence", "完成证据", None)]
            form = CommandForm("选择记录后可修改；保存使用已审阅版本。", fields); self.forms[name] = form
            form.submit.setText("保存所选修改"); form.submitted.connect(lambda values, name=name: self._save(name, values, False)); page.splitter.addWidget(scroll(form))
            create = QPushButton("新建" + title); create.clicked.connect(lambda checked=False, name=name: self._save(name, self.forms[name].values(), True)); page.action_bar.addWidget(create)
            status = QPushButton("设置状态（完成需证据）"); status.clicked.connect(lambda checked=False, name=name: self._status(name)); page.action_bar.addWidget(status)
        self.binding_form = CommandForm("按名称关联已知任务、提醒或日程。", [("use_policy", "使用范围", ["local_only", "cloud_allowed"]), ("status", "关联状态", ["Active", "Removed"])])
        binding_layout = self.binding_form.layout()
        assert isinstance(binding_layout, QFormLayout)
        self.source = QComboBox(); self.milestone = QComboBox(); binding_layout.insertRow(1, "关联对象", self.source); binding_layout.insertRow(2, "所属步骤", self.milestone)
        self.source_offset = 0
        self.source_more = QPushButton("更多日程 / 计时来源"); self.source_more.clicked.connect(self._next_sources); binding_layout.addRow(self.source_more)
        source_first = QPushButton("返回首批来源"); source_first.clicked.connect(lambda: self._next_sources(True)); binding_layout.addRow(source_first)
        self.binding_form.submit.setText("新增关联"); self.binding_form.submitted.connect(self._bind); self.pages["binding"].splitter.addWidget(scroll(self.binding_form))
        update = QPushButton("保存所选关联状态 / 用途"); update.clicked.connect(self._update_binding); self.pages["binding"].action_bar.addWidget(update)

    def refresh(self) -> None:
        self._read("goals", {"offset": self.pages["goal"].offset, "limit": 50}, self._goals)
        self._read("tasks", {"offset": 0, "limit": 100}, self._sources_tasks)
        self._read("schedule_items", {"source": "all", "offset": self.source_offset, "limit": 100, "status": "all"}, self._sources_schedule)
        self._children()

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
        chosen = self.parent_goal.currentData(); self.parent_goal.blockSignals(True); self.parent_goal.clear()
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
        if not goal:
            self._read_latest.pop("milestones", None); self._read_latest.pop("bindings", None)
        if not goal: return
        for name in ["milestone", "binding"]:
            def done(data: dict[str, Any], name: str = name) -> None:
                self._child_page(name, data)
            self._read(name + "s", {"goal_id": goal["id"], "offset": self.pages[name].offset, "limit": 50}, done)

    def _child_page(self, name: str, data: dict[str, Any]) -> None:
        if "_error" in data: return
        self.pages[name].set_page(data)
        if name == "milestone":
            selected = self.milestone.currentData(); self.milestone.blockSignals(True); self.milestone.clear(); self.milestone.addItem("整个目标", None)
            for item in data.get("items", []): self.milestone.addItem(str(item.get("title", "步骤")), item["id"])
            index = self.milestone.findData(selected)
            if index >= 0: self.milestone.setCurrentIndex(index)
            self.milestone.blockSignals(False)

    def _selected(self, name: str, item: dict[str, Any]) -> None:
        form = self.binding_form if name == "binding" else self.forms[name]
        old = self.snapshots.get(name, {})
        if form.dirty and item.get("id") == old.get("id"):
            self.pages[name].show_detail(item); self.notice.setText("记录版本已变化，未保存编辑已保留。请重新审阅后保存。"); return
        self.snapshots[name] = dict(item); self.pages[name].show_detail(item)
        if item: form.populate(item)

    def _write(self, op: str, params: dict[str, Any]) -> None:
        epoch = self._read_epoch
        def done(data: dict[str, Any]) -> None:
            if epoch != self._read_epoch: return
            if "_error" in data: self.notice.setText("保存失败，编辑内容已保留；请审阅当前版本。"); return
            target = "milestone" if "milestone" in op else "binding" if "bind" in op else "goal"
            (self.binding_form if target == "binding" else self.forms[target]).mark_clean(); self.refresh()
        self.channel.send(op, params, done, mutation=True)

    def _save(self, name: str, values: dict[str, Any], create: bool) -> None:
        params = {key: value for key, value in values.items() if key not in {"status", "evidence"}}
        params["deadline"] = params.get("deadline") or None
        if create:
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
        if goal and source: self._write("bind", {"goal_id": goal["id"], "expected_revision": goal["revision"], "source": source["source"], "source_id": source["source_id"], "milestone_id": self.milestone.currentData(), "use_policy": values["use_policy"]})
        else: self.notice.setText("请先选择目标和关联对象。")

    def _update_binding(self) -> None:
        item = self.snapshots.get("binding", {})
        if item: self._write("update_binding", {"binding_id": item["id"], "expected_revision": item["revision"], **self.binding_form.values()})

    def clear_sensitive(self) -> None:
        self._read_epoch += 1; self._read_active.clear(); self._read_latest.clear(); self.channel.invalidate()
        self.parent_goal.clear(); self.source.clear(); self.milestone.clear()
        for page in self.pages.values(): page.clear_sensitive()
        for form in self.forms.values(): form.clear_sensitive()
        self.binding_form.clear_sensitive(); self.source_offset = 0
        # Clearing selections emits synchronous callbacks that can store empty snapshots.
        self.snapshots.clear(); self.sources.clear()
