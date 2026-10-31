"""Reusable Qt Widgets; all displayed backend content is plain text."""

from collections.abc import Sequence
import json
from datetime import datetime
from typing import Any

from PySide6.QtCore import QTimer, Signal, Qt, QModelIndex
from PySide6.QtWidgets import (QAbstractItemView, QFormLayout, QHBoxLayout, QLabel, QListWidgetItem, QSizePolicy, QSplitter, QTreeWidgetItem, QVBoxLayout, QWidget)
from .components import (ChoiceBox as QComboBox, PushButton as QPushButton, LineEdit as QLineEdit, PlainTextEdit as QPlainTextEdit, ListWidget as QListWidget, TreeWidget as QTreeWidget, ScrollArea as QScrollArea, SpinBox as QSpinBox)

from .widgets import ArrowComboBox, fill_tree, label, text_label
from .display_text import friendly_time

MEMORY_TOOL_NAMES = {
    "echo": "回显文本", "time": "当前时间", "read_text": "读取文本",
    "write_text": "写入文本", "calendar_create": "创建日程",
    "calendar_list": "查看日程", "calendar_update": "更新日程",
    "calendar_delete": "删除日程", "calendar_events": "查看日程提醒记录",
    "timer_create": "创建计时器", "timer_list": "查看计时器",
    "timer_cancel": "取消计时器", "timer_events": "查看计时记录",
    "document_search": "搜索文件", "document_classify": "分类文件",
    "document_copy": "复制文件", "document_move": "移动文件",
    "document_delete": "删除文件", "screenshot_capture": "截取屏幕",
}


class PagedView(QWidget):
    """Emit the selected DTO, or an empty dict when selection is lost."""

    refresh_requested = Signal()
    selected = Signal(object)

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.offset = 0
        self.has_more = False
        self.items: list[dict[str, Any]] = []
        self._orientation_pending = False
        self.keep_columns = False
        self._list_only = False
        self.display_timezone: str | None = None
        self._memory_date_groups: dict[str, QTreeWidgetItem] = {}
        layout = QVBoxLayout(self)
        self.toolbar_host = QWidget()
        self.toolbar_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.toolbar = QHBoxLayout(self.toolbar_host)
        self.toolbar.setContentsMargins(0, 0, 0, 0)
        self.previous = QPushButton("上一页")
        self.next = QPushButton("下一页")
        self.refresh = QPushButton("重新加载")
        self.back_to_list = QPushButton('返回列表')
        self.back_to_list.clicked.connect(self._back_to_list); self.back_to_list.hide()
        self.page_label = QLabel("每页 50 条")
        # A narrow toolbar may clip this secondary count, but must never turn
        # it into a tall column that takes space from the actual record body.
        self.page_label.setWordWrap(False)
        self.page_label.setMinimumWidth(0)
        self.page_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        for widget in (self.previous, self.next, self.refresh, self.page_label):
            self.toolbar.addWidget(widget)
        self.toolbar.insertWidget(0, self.back_to_list)
        self.toolbar.addStretch()
        layout.addWidget(self.toolbar_host)
        self.status_label = text_label("尚未加载，请读取当前记录。")
        self.status_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.addWidget(self.status_label)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.list = QListWidget()
        self.list.setObjectName(name + "_list")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.detail = QTreeWidget()
        self.detail.setHeaderLabels(["项目", "内容"])
        self.detail.setObjectName(name + "_detail")
        self.detail.setMinimumWidth(0)
        self.detail_panel = QScrollArea()
        self.detail_panel.setWidgetResizable(True)
        self.detail_panel.setMinimumWidth(0)
        self.detail_content = QWidget()
        self.detail_panel.setWidget(self.detail_content)
        details = QVBoxLayout(self.detail_content)
        self.details_layout = details
        details.setContentsMargins(0, 0, 0, 0)
        # 纯标题和操作按钮按内容顶端排列，避免无正文时均摊整页高度。
        details.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.summary_title = text_label("选择一条记录查看详情。", heading=True)
        self.summary_meta = text_label()
        for summary_label in (self.summary_title, self.summary_meta):
            summary_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.summary_body = QPlainTextEdit()
        self.summary_body.setReadOnly(True)
        self.summary_body.setObjectName(name + "_body")
        self.summary_body.setMinimumSize(0, 140)
        self.summary_body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.summary_context = QPlainTextEdit()
        self.summary_context.setReadOnly(True)
        self.summary_context.setObjectName(name + "_context")
        self.summary_context.setMinimumSize(0, 80)
        self.summary_context.setMaximumHeight(180)
        self.technical = QPushButton("查看完整记录字段")
        self.technical.setCheckable(True)
        self.technical.toggled.connect(self.detail.setVisible)
        for detail_widget in (self.summary_title, self.summary_meta, self.summary_body,
                              self.summary_context, self.technical, self.detail):
            details.addWidget(detail_widget)
        self.detail.hide(); self.summary_meta.hide(); self.summary_body.hide(); self.summary_context.hide(); self.technical.hide()
        self.detail.setMinimumHeight(180)
        self.splitter.addWidget(self.list)
        self.memory_tree: QTreeWidget | None = None
        if name == "memories":
            # A date tree keeps the existing DTO/page contract while ensuring
            # each expandable task still points to exactly one memory record.
            self.memory_tree = QTreeWidget()
            self.memory_tree.setObjectName("memories_grouped_list")
            self.memory_tree.setHeaderHidden(True)
            self.memory_tree.setMinimumWidth(0)
            self.splitter.replaceWidget(0, self.memory_tree)
            self.list.hide()
            self.memory_tree.currentItemChanged.connect(self._memory_tree_selection)
            self.memory_tree.itemClicked.connect(self._reopen_detail)
        self.splitter.addWidget(self.detail_panel)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        layout.addWidget(self.splitter, 1)
        actions = QWidget(); self.action_bar = QHBoxLayout(actions); self.action_bar.setContentsMargins(0, 0, 0, 0)
        self.action_scroll = QScrollArea(); self.action_scroll.setWidgetResizable(True); self.action_scroll.setWidget(actions); self.action_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff); self.action_scroll.setMaximumHeight(64); self.action_scroll.setMinimumHeight(54)
        layout.addWidget(self.action_scroll)
        self.action_scroll.hide()
        self.previous.clicked.connect(lambda: self._move(-50))
        self.next.clicked.connect(lambda: self._move(50))
        self.refresh.clicked.connect(self.refresh_requested.emit)
        self.list.currentRowChanged.connect(self._select)
        self.list.itemClicked.connect(self._reopen_detail)
        self._buttons()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._queue_orientation()

    def _queue_orientation(self) -> None:
        # Changing splitter geometry from its parent's resize callback can cause
        # another resize before Qt has finished the current layout operation.
        if not self._orientation_pending:
            self._orientation_pending = True
            QTimer.singleShot(0, self, self._apply_orientation)

    def _apply_orientation(self) -> None:
        self._orientation_pending = False
        self.action_scroll.setVisible(self.action_bar.count() > 0)
        orientation = Qt.Orientation.Vertical if self.width() < 760 and not self.keep_columns else Qt.Orientation.Horizontal
        narrow = orientation == Qt.Orientation.Vertical
        details_only = narrow and bool(self.selected_item()) and not self._list_only
        self.back_to_list.setVisible(details_only)
        self.previous.setVisible(not details_only); self.next.setVisible(not details_only)
        self.list.setVisible(self.memory_tree is None and not details_only)
        self.list.setMaximumHeight(16777215)
        if self.memory_tree is not None:
            self.memory_tree.setVisible(not details_only)
            self.memory_tree.setMaximumHeight(16777215)
        for index in range(1, self.splitter.count()):
            pane = self.splitter.widget(index)
            if pane is not None:
                unused_generic = pane is self.detail_panel and self.splitter.count() > 2
                pane.setVisible(not unused_generic and (not narrow or details_only))
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)
            if orientation == Qt.Orientation.Vertical:
                sizes = [112] + [max(180, self.splitter.height() - 112)] * (self.splitter.count() - 1)
                self.splitter.setSizes(sizes)
        if self.keep_columns:
            self.list.setMinimumWidth(120)
            self.list.setMaximumWidth(230 if self.width() >= 760 else 150)

    def _back_to_list(self) -> None:
        self._list_only = True; self._queue_orientation()

    def _reopen_detail(self, row: Any, *args: Any) -> None:
        value = row.data(0, Qt.ItemDataRole.UserRole) if isinstance(row, QTreeWidgetItem) else row.data(Qt.ItemDataRole.UserRole)
        if isinstance(value, dict) and value.get('id', value.get('plugin_id')):
            self._list_only = False; self._queue_orientation()

    def loading(self) -> None:
        self.status_label.setText("正在读取当前记录…")

    def load_failed(self) -> None:
        self.status_label.setText("读取失败，请重新加载。已有记录可能不是最新版本。")

    def _move(self, delta: int) -> None:
        self.offset = max(0, self.offset + delta)
        self.refresh_requested.emit()

    def _buttons(self) -> None:
        self.previous.setEnabled(self.offset > 0)
        self.next.setEnabled(self.has_more)

    def set_page(self, data: dict[str, Any]) -> None:
        if "_error" in data:
            self.load_failed()
            return
        previous = self.selected_item()
        previous_id = previous.get("id", previous.get("plugin_id"))
        raw = data.get("items", [])
        self.items = [item for item in raw[:100] if isinstance(item, dict)]
        self.offset = int(data.get("offset", self.offset))
        self.has_more = bool(data.get("has_more", False))
        self._memory_date_groups.clear()
        self.list.blockSignals(True)
        self.list.clear()
        if self.memory_tree is not None:
            self.memory_tree.blockSignals(True)
            self.memory_tree.clear()
        selected = -1
        schedule_day = ''
        for index, item in enumerate(self.items):
            # Readable local titles are invalidated at the privacy barrier.
            title = self._title(item)
            state = label(item.get("status", item.get("delivery_state", "")))
            if self.memory_tree is not None:
                self._add_memory_tree_item(item, title, state)
                row = None
            else:
                display = title.replace('\n', ' ')[:100]
                if self.name == 'schedules':
                    stamp = item.get('start_at') or item.get('due_at')
                    if stamp:
                        readable_time = friendly_time(stamp, timezone=self.display_timezone)
                        day = readable_time.split(' ')[0]
                        if day != schedule_day:
                            header = QListWidgetItem('今天' if day == datetime.now().astimezone().strftime('%Y年%m月%d日') else day)
                            header.setFlags(header.flags() & ~Qt.ItemFlag.ItemIsSelectable & ~Qt.ItemFlag.ItemIsEnabled)
                            self.list.addItem(header); schedule_day = day
                        display = readable_time + '\n' + display
                row = QListWidgetItem(display + (" · " + state if state else ""))
                row.setData(Qt.ItemDataRole.UserRole, item)
                self.list.addItem(row)
            if row is not None:
                row.setToolTip(title)
            if previous_id is not None and item.get("id", item.get("plugin_id")) == previous_id:
                selected = self.list.row(row) if row is not None else index
        if self.memory_tree is not None:
            self.memory_tree.blockSignals(False)
            if previous_id is not None:
                self._select_memory_id(previous_id)
        else:
            self.list.setCurrentRow(selected)
        self.list.blockSignals(False)
        self.page_label.setText(f"第 {self.offset + 1}–{self.offset + len(self.items)} 条 / 每页 50 条" if self.items else "每页 50 条")
        self.page_label.setToolTip(self.page_label.text())
        self.status_label.setText("当前条件下没有记录。" if not self.items else f"已读取 {len(self.items)} 条，选择记录查看详情。")
        self._buttons()
        if previous and self.selected_item() != previous and self.memory_tree is None:
            self._select(selected)

    def selected_item(self) -> dict[str, Any]:
        if self.memory_tree is not None:
            current = self.memory_tree.currentItem()
            value = current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None
            return value if isinstance(value, dict) else {}
        row = self.list.currentRow()
        current_row = self.list.item(row)
        item = current_row.data(Qt.ItemDataRole.UserRole) if current_row is not None else None
        return item if isinstance(item, dict) else {}

    def _memory_tree_selection(self, current: QTreeWidgetItem | None, previous: QTreeWidgetItem | None) -> None:
        value = current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None
        self.show_detail(value if isinstance(value, dict) else {})
        self.selected.emit(value if isinstance(value, dict) else {})
        self._list_only = False; self._queue_orientation()

    def _select_memory_id(self, memory_id: Any) -> None:
        if self.memory_tree is None:
            return
        for group_index in range(self.memory_tree.topLevelItemCount()):
            group = self.memory_tree.topLevelItem(group_index)
            if group is None:
                continue
            candidates = [group, *(group.child(index) for index in range(group.childCount()))]
            for row in candidates:
                if row is None:
                    continue
                item = row.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(item, dict) and item.get("id") == memory_id:
                    self.memory_tree.blockSignals(True)
                    self.memory_tree.setCurrentItem(row)
                    self.memory_tree.blockSignals(False)
                    # Emit the latest DTO even when the same ID remains
                    # selected across a refresh and its revision has changed.
                    self._memory_tree_selection(row, None)
                    return
        self.memory_tree.blockSignals(True)
        self.memory_tree.setCurrentIndex(QModelIndex())
        self.memory_tree.blockSignals(False)
        self._memory_tree_selection(None, None)

    def _add_memory_tree_item(self, item: dict[str, Any], title: str, state: str) -> None:
        assert self.memory_tree is not None
        candidate_source = item.get('source')
        source = candidate_source if isinstance(candidate_source, dict) else {}
        stamp = source.get("created_at") or item.get("recorded_at")
        local_stamp = friendly_time(stamp, timezone=self.display_timezone) if stamp else "日期未记录"
        day, separator, _ = local_stamp.partition(" ")
        group_name = day if stamp and separator and local_stamp != str(stamp) else "日期未记录"
        execution = self._execution_steps(item)
        if execution is not None:
            row_title = f"工具执行记录 · {len(execution)} 次工具调用"
        elif self._is_tool_result_origin(item):
            row_title = "工具执行记录 · 摘要格式未识别"
        else:
            row_title = title.replace("\n", " ")[:100]
        if state:
            row_title += " · " + state
        row = QTreeWidgetItem([row_title])
        row.setData(0, Qt.ItemDataRole.UserRole, item)
        row.setToolTip(0, row_title)
        if execution is None:
            # Semantic and other records stay flat. Only verified execution
            # records are nested beneath date headings.
            self.memory_tree.addTopLevelItem(row)
        else:
            group = self._memory_date_groups.get(group_name)
            if group is None:
                group = QTreeWidgetItem()
                group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                self.memory_tree.addTopLevelItem(group)
                group.setExpanded(False)
                self._memory_date_groups[group_name] = group
            group.addChild(row)
            count = group.childCount()
            group.setText(0, f"{group_name} · {count} 条工具记录")
            group.setToolTip(0, f"本页 {count} 条工具执行记录")
        if execution is not None:
            for step in execution:
                raw_tool_name = str(step.get("tool_name") or "")
                tool_name = MEMORY_TOOL_NAMES.get(raw_tool_name, raw_tool_name or "未命名工具")
                result = "工具调用成功" if step.get("success") is True else (
                    "工具调用未成功" if step.get("success") is False else "需核实")
                detail = QTreeWidgetItem([f"{tool_name} · {result}"])
                # A tool-call row is another entry point to the same memory
                # record, so selecting it opens that task's complete details.
                detail.setData(0, Qt.ItemDataRole.UserRole, item)
                row.addChild(detail)

    @staticmethod
    def _is_tool_result_origin(item: dict[str, Any]) -> bool:
        source = item.get("source")
        return isinstance(source, dict) and source.get("origin") == "tool_result"

    @staticmethod
    def _typed_execution_marker(item: dict[str, Any]) -> bool:
        source = item.get("source")
        draft = item.get("draft")
        return (isinstance(source, dict) and isinstance(draft, dict)
                and source.get("origin") == "tool_result"
                and draft.get("claim_type") == "observed_event"
                and draft.get("kind") == "episodic"
                and draft.get("predicate") == "execution")

    @staticmethod
    def _execution_steps(item: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Accept only the persisted tool-result execution-memory signature."""
        draft = item.get("draft")
        if not isinstance(draft, dict) or not PagedView._typed_execution_marker(item):
            return None
        content = draft.get("content")
        if not isinstance(content, str):
            return None
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            return None
        steps = parsed.get("steps") if isinstance(parsed, dict) and set(parsed) == {"steps"} else None
        if not isinstance(steps, list) or not steps:
            return None
        if any(not isinstance(step, dict) or not isinstance(step.get("tool_name"), str)
               or not isinstance(step.get("call_id"), str)
               or (step.get("success") is not None and type(step.get("success")) is not bool)
               or set(step) != {"tool_name", "call_id", "success"} for step in steps):
            return None
        return steps

    def _select(self, row: int) -> None:
        if self.memory_tree is not None:
            return
        self.show_detail({})
        self.selected.emit(self.selected_item())
        self._list_only = False; self._queue_orientation()

    def clear_sensitive(self) -> None:
        self.list.clear()
        if self.memory_tree is not None:
            self.memory_tree.clear()
            self._memory_date_groups.clear()
        self.items.clear()
        self.show_detail({})
        self.offset = 0
        self.has_more = False
        self.status_label.setText("记录已清空，请重新读取当前数据。")
        self._buttons()

    def show_detail(self, data: dict[str, Any]) -> None:
        if data:
            fill_tree(self.detail, data)
            item = data.get("item", data)
            execution = self._execution_steps(item) if self.name == "memories" else None
            self.summary_title.setText(("工具执行记录" if self._is_tool_result_origin(item) else "记住的内容")
                                       if self.name == "memories" else self._title(item)[:200])
            body, metadata, context = self._readable(item)
            self.summary_body.setPlainText(body)
            self.summary_meta.setText(metadata)
            self.summary_meta.setVisible(bool(metadata))
            self.summary_context.setPlainText(context)
            self.summary_body.setVisible(bool(body))
            self.summary_context.setVisible(bool(context))
            self.technical.show()
        else:
            self.detail.clear()
            self.summary_title.setText("选择一条记录查看详情。")
            self.summary_meta.clear(); self.summary_body.clear(); self.summary_context.clear()
            self.summary_meta.hide(); self.summary_body.hide(); self.summary_context.hide(); self.technical.hide()
        self.technical.setChecked(False); self.detail.hide()

    @staticmethod
    def _title(item: dict[str, Any]) -> str:
        draft = item.get("draft") or {}
        return str(item.get("title") or item.get("label") or item.get("display_name")
                   or item.get("name") or item.get("content") or draft.get("content")
                   or item.get("request") or "未命名记录")

    def _readable(self, item: dict[str, Any]) -> tuple[str, str, str]:
        """Project domain fields for reading; the exact DTO stays available below."""
        draft = item.get("draft") or {}
        source = item.get("source") or {}
        if not isinstance(source, dict): source = {}
        body = str(item.get("content") or draft.get("content") or item.get("description")
                   or item.get("summary") or item.get("success_criteria") or item.get("request") or "")
        fields: list[tuple[str, Any]] = [
            ("状态", label(item.get("status", item.get("delivery_state", "")))),
            ("使用范围", label(source.get("use_policy", item.get("use_policy", "")))),
        ]
        date_fields = [("deadline", "截止时间"), ("start_at", "开始时间"), ("end_at", "结束时间"),
                       ("reminder_at", "提醒时间"), ("due_at", "到期时间"), ("updated_at", "更新时间")]
        if self.name != "memories":
            date_fields.append(("recorded_at", "记录时间"))
        for key, title in date_fields:
            value = item.get(key)
            if value:
                fields.append((title, friendly_time(value, timezone=self.display_timezone)))
        context: list[str] = []
        if self.name == "memories":
            execution = self._execution_steps(item)
            if execution is not None:
                body = f"此任务记录了 {len(execution)} 次工具调用。展开左侧任务可查看每次调用摘要。"
            elif self._is_tool_result_origin(item):
                body = "这条工具执行记录的摘要格式无法识别，请展开原始技术字段查看。"
            origins = {"user_message": "用户明确输入", "tool_result": "工具执行记录", "execution": "任务执行记录", "legacy": "历史导入"}
            origin = str(source.get("origin") or "")
            fields.append(("来源", origins.get(origin, origin)))
            kinds = {"semantic": "语义记忆", "episodic": "经历记录", "working": "工作记忆",
                     "goal": "目标记忆", "relationship": "关系记忆"}
            kind = draft.get("kind")
            if kind:
                fields.append(("类别", kinds.get(str(kind), str(kind))))
            scope_labels = {"global": "长期有效"}
            if draft.get("scope"):
                fields.append(("用途", scope_labels.get(str(draft["scope"]), label(draft["scope"]))))
            if draft.get('destination'):
                fields.append(('预期去向', {'facts': '事实与知识', 'episode': '共同经历', 'habit': '相处习惯'}.get(draft['destination'], draft['destination'])))
            if draft.get('reason'): context.append('理解依据\n' + str(draft['reason']))
            if draft.get('uncertainty'): context.append('不确定性与适用范围\n' + str(draft['uncertainty']))
            if draft.get('review_after'): fields.append(('后续复查', friendly_time(draft['review_after'], timezone=self.display_timezone)))
            stamp = source.get("created_at") or item.get("recorded_at")
            if stamp:
                fields.append(("日期", friendly_time(stamp, timezone=self.display_timezone)))
            if draft.get("quote"): context.append("原文依据\n" + str(draft["quote"]))
            if source.get("content") and source["content"] != draft.get("quote"):
                context.append("来源原文\n" + str(source["content"]))
        elif self.name == 'attentions':
            states = {'pending':'等待合适时机','running':'正在检查','done':'已检查','ended':'已结束关注','cancelled':'已停止','invalidated':'来源已失效','expired':'已过期'}
            fields[0] = ('状态', states.get(str(item.get('status')), '等待核对'))
            fields.append(('检查方式', {'event':'事件触发','review':'模型安排复查','opportunity':'交流机会','fallback':'低频整理'}.get(str(item.get('method')), '内部检查')))
            fields.append(('有效期至', friendly_time(item.get('expires_at'), timezone=self.display_timezone)))
            context.append('关注由系统结合最新交流自动复查，无需逐条接受；实际操作仍按工具权限确认。')
        elif self.name == "tools":
            body = str(item.get("summary") or "查看输入参数和执行权限；完整工具契约可在记录字段中查看。")
            risk = {0: "自动执行", 1: "需要普通授权", 2: "需要高风险授权"}
            permission = item.get("permission_level")
            permission_text = risk.get(permission, "由工具契约确定") if isinstance(permission, int) else "由工具契约确定"
            fields.extend([("执行权限", permission_text),
                           ("当前条件", item.get("reason"))])
            parameters = item.get("parameter_fields", [])
            if parameters:
                context.append("输入参数\n" + "\n".join(
                    str(field.get("label") or field.get("name")) + ("（必填）" if field.get("required") else "（可选）")
                    for field in parameters if isinstance(field, dict)))
        elif self.name == "plugins":
            body = f"当前提供方注册 {len(item.get('tool_ids', []))} 项工具，可在能力目录查看说明和权限。"
        for key, title in [("progress_note", "进度备注"), ("evidence", "人工证据"),
                           ("blocked_reason", "当前限制"), ("change_reason", "变更原因")]:
            if item.get(key): context.append(title + "\n" + str(item[key]))
        metadata = " · ".join(title + "：" + str(value) for title, value in fields if value is not None and value != "")
        return body, metadata, "\n\n".join(context)


class CommandForm(QWidget):
    """A labelled management form with typed enum/revision fields."""

    submitted = Signal(object)

    def __init__(self, title: str, fields: Sequence[tuple[str, str, Sequence[str] | None]],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.dirty = False; self._populating = False
        self.fields: dict[str, QLineEdit | QComboBox | QSpinBox | QPlainTextEdit] = {}
        layout = QFormLayout(self)
        layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.description = QLabel(title)
        self.description.setWordWrap(True)
        layout.addRow(self.description)
        for key, field_label, choices in fields:
            widget: QLineEdit | QComboBox | QSpinBox | QPlainTextEdit
            if key == "expected_revision":
                widget = QSpinBox()
                widget.setRange(1, 2**31 - 1)
            elif choices is not None:
                widget = ArrowComboBox()
                for choice in choices:
                    widget.addItem(label(choice), choice)
            elif key in {"success_criteria", "content", "evidence", "progress_note"}:
                widget = QPlainTextEdit()
                widget.setMaximumHeight(85)
            else:
                widget = QLineEdit()
            widget.setObjectName(key)
            self.fields[key] = widget
            if isinstance(widget, QComboBox): widget.currentIndexChanged.connect(self._edited)
            elif isinstance(widget, QSpinBox): widget.valueChanged.connect(self._edited)
            else: widget.textChanged.connect(self._edited)
            layout.addRow(field_label, widget)
        self.submit = QPushButton("提交此操作")
        self.submit.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        layout.addRow(self.submit)
        self.submit.clicked.connect(lambda: self.submitted.emit(self.values()))

    def values(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, widget in self.fields.items():
            if isinstance(widget, QSpinBox):
                result[key] = widget.value()
            elif isinstance(widget, QComboBox):
                result[key] = widget.currentData()
            elif isinstance(widget, QPlainTextEdit):
                result[key] = widget.toPlainText()
            else:
                result[key] = widget.text()
        return result

    def _edited(self, *args: Any) -> None:
        if not self._populating: self.dirty = True

    def mark_clean(self) -> None:
        self.dirty = False

    def populate(self, values: dict[str, Any]) -> None:
        self._populating = True
        try:
            for key, widget in self.fields.items():
                source = "revision" if key == "expected_revision" else key
                if source not in values: continue
                value = values[source]
                if value is None:
                    if isinstance(widget, (QLineEdit, QPlainTextEdit)): widget.clear()
                elif isinstance(widget, QSpinBox): widget.setValue(int(value))
                elif isinstance(widget, QComboBox):
                    index = widget.findData(value)
                    if index >= 0: widget.setCurrentIndex(index)
                elif isinstance(widget, QPlainTextEdit): widget.setPlainText(str(value))
                else: widget.setText(str(value))
        finally:
            self._populating = False; self.dirty = False

    def clear_sensitive(self) -> None:
        self._populating = True
        for widget in self.fields.values():
            if isinstance(widget, (QLineEdit, QPlainTextEdit)): widget.clear()
        self._populating = False; self.dirty = False
