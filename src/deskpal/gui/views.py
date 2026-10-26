"""Reusable Qt Widgets; all displayed backend content is plain text."""

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QSpinBox, QTreeWidget,
    QSizePolicy, QSplitter, QScrollArea, QVBoxLayout, QWidget,
)

from .widgets import fill_tree, label


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
        layout = QVBoxLayout(self)
        self.toolbar = QHBoxLayout()
        self.previous = QPushButton("上一页")
        self.next = QPushButton("下一页")
        self.refresh = QPushButton("重新加载")
        self.page_label = QLabel("每页 50 条")
        self.page_label.setWordWrap(True)
        self.page_label.setMinimumWidth(0)
        self.page_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        for widget in (self.previous, self.next, self.refresh, self.page_label):
            self.toolbar.addWidget(widget)
        self.toolbar.addStretch()
        layout.addLayout(self.toolbar)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.list = QListWidget()
        self.list.setObjectName(name + "_list")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.detail = QTreeWidget()
        self.detail.setHeaderLabels(["项目", "内容"])
        self.detail.setObjectName(name + "_detail")
        self.detail.setMinimumWidth(0)
        self.splitter.addWidget(self.list)
        self.splitter.addWidget(self.detail)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        layout.addWidget(self.splitter)
        actions = QWidget(); self.action_bar = QHBoxLayout(actions); self.action_bar.setContentsMargins(0, 0, 0, 0)
        action_scroll = QScrollArea(); action_scroll.setWidgetResizable(True); action_scroll.setWidget(actions); action_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff); action_scroll.setMaximumHeight(70); action_scroll.setMinimumHeight(58)
        layout.addWidget(action_scroll)
        self.previous.clicked.connect(lambda: self._move(-50))
        self.next.clicked.connect(lambda: self._move(50))
        self.refresh.clicked.connect(self.refresh_requested.emit)
        self.list.currentRowChanged.connect(self._select)
        self._buttons()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self.splitter.setOrientation(Qt.Orientation.Vertical if self.width() < 760 else Qt.Orientation.Horizontal)

    def _move(self, delta: int) -> None:
        self.offset = max(0, self.offset + delta)
        self.refresh_requested.emit()

    def _buttons(self) -> None:
        self.previous.setEnabled(self.offset > 0)
        self.next.setEnabled(self.has_more)

    def set_page(self, data: dict[str, Any]) -> None:
        if "_error" in data:
            return
        previous = self.selected_item()
        previous_id = previous.get("id", previous.get("plugin_id"))
        raw = data.get("items", [])
        self.items = [item for item in raw[:100] if isinstance(item, dict)]
        self.offset = int(data.get("offset", self.offset))
        self.has_more = bool(data.get("has_more", False))
        self.list.blockSignals(True)
        self.list.clear()
        selected = -1
        for index, item in enumerate(self.items):
            # Readable local titles are invalidated at the privacy barrier.
            title = str(item.get("title") or item.get("label") or item.get("display_name") or item.get("content") or item.get("draft", {}).get("content") or item.get("request") or "未命名记录")
            row = QListWidgetItem(title[:100] + " · " + label(item.get("status", item.get("delivery_state", ""))))
            row.setToolTip(title)
            self.list.addItem(row)
            if previous_id is not None and item.get("id", item.get("plugin_id")) == previous_id:
                selected = index
        self.list.setCurrentRow(selected)
        self.list.blockSignals(False)
        self.page_label.setText(f"第 {self.offset + 1}–{self.offset + len(self.items)} 条 / 每页 50 条")
        self._buttons()
        if previous and self.selected_item() != previous:
            self._select(selected)

    def selected_item(self) -> dict[str, Any]:
        row = self.list.currentRow()
        return self.items[row] if 0 <= row < len(self.items) else {}

    def _select(self, row: int) -> None:
        self.detail.clear()
        self.selected.emit(self.items[row] if 0 <= row < len(self.items) else {})

    def clear_sensitive(self) -> None:
        self.list.clear()
        self.items.clear()
        self.detail.clear()
        self.offset = 0
        self.has_more = False
        self._buttons()

    def show_detail(self, data: dict[str, Any]) -> None:
        if data:
            fill_tree(self.detail, data)
        else:
            self.detail.clear()


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
                widget = QComboBox()
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
