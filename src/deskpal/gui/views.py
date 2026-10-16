"""Reusable Qt Widgets; all displayed backend content is plain text."""

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QSpinBox,
    QSizePolicy, QSplitter, QVBoxLayout, QWidget,
)

from .viewmodels import plain_json


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
        for widget in (self.previous, self.next, self.refresh, self.page_label):
            self.toolbar.addWidget(widget)
        self.toolbar.addStretch()
        layout.addLayout(self.toolbar)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.list = QListWidget()
        self.list.setObjectName(name + "_list")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.detail = QPlainTextEdit()
        self.detail.setObjectName(name + "_detail")
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("选择条目查看完整详情。")
        self.splitter.addWidget(self.list)
        self.splitter.addWidget(self.detail)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        layout.addWidget(self.splitter)
        self.action_bar = QHBoxLayout()
        layout.addLayout(self.action_bar)
        self.previous.clicked.connect(lambda: self._move(-50))
        self.next.clicked.connect(lambda: self._move(50))
        self.refresh.clicked.connect(self.refresh_requested.emit)
        self.list.currentRowChanged.connect(self._select)
        self._buttons()

    def _move(self, delta: int) -> None:
        self.offset = max(0, self.offset + delta)
        self.refresh_requested.emit()

    def _buttons(self) -> None:
        self.previous.setEnabled(self.offset > 0)
        self.next.setEnabled(self.has_more)

    def set_page(self, data: dict[str, Any]) -> None:
        previous = self.selected_item()
        previous_id = previous.get("id")
        raw = data.get("items", [])
        self.items = [item for item in raw[:100] if isinstance(item, dict)]
        self.offset = int(data.get("offset", self.offset))
        self.has_more = bool(data.get("has_more", False))
        self.list.blockSignals(True)
        self.list.clear()
        selected = -1
        for index, item in enumerate(self.items):
            # No private request/content/title is copied into navigation labels.
            label = f"{item.get('id', '')} · {item.get('status', '')} · v{item.get('revision', '')}"
            row = QListWidgetItem(label)
            self.list.addItem(row)
            if item.get("id") == previous_id:
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

    def show_detail(self, data: dict[str, Any]) -> None:
        self.detail.setPlainText(plain_json(data))


class CommandForm(QWidget):
    """A labelled management form with typed enum/revision fields."""

    submitted = Signal(object)

    def __init__(self, title: str, fields: Sequence[tuple[str, str, Sequence[str] | None]],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.fields: dict[str, QLineEdit | QComboBox | QSpinBox | QPlainTextEdit] = {}
        layout = QFormLayout(self)
        self.description = QLabel(title)
        self.description.setWordWrap(True)
        layout.addRow(self.description)
        for key, label, choices in fields:
            widget: QLineEdit | QComboBox | QSpinBox | QPlainTextEdit
            if key == "expected_revision":
                widget = QSpinBox()
                widget.setRange(1, 2**31 - 1)
            elif choices is not None:
                widget = QComboBox()
                widget.addItems(list(choices))
            elif key in {"success_criteria", "content", "evidence", "progress_note"}:
                widget = QPlainTextEdit()
                widget.setMaximumHeight(85)
            else:
                widget = QLineEdit()
            widget.setObjectName(key)
            self.fields[key] = widget
            layout.addRow(label, widget)
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
                result[key] = widget.currentText()
            elif isinstance(widget, QPlainTextEdit):
                result[key] = widget.toPlainText()
            else:
                result[key] = widget.text()
        return result

    def populate(self, values: dict[str, Any]) -> None:
        for key, widget in self.fields.items():
            source = "revision" if key == "expected_revision" else key
            if source not in values:
                continue
            value = values[source]
            if value is None:
                if isinstance(widget, (QLineEdit, QPlainTextEdit)):
                    widget.clear()
                continue
            if isinstance(widget, QSpinBox):
                widget.setValue(int(value))
            elif isinstance(widget, QComboBox):
                widget.setCurrentText(str(value))
            elif isinstance(widget, QPlainTextEdit):
                widget.setPlainText(str(value))
            else:
                widget.setText(str(value))

    def clear_sensitive(self) -> None:
        for widget in self.fields.values():
            if isinstance(widget, (QLineEdit, QPlainTextEdit)):
                widget.clear()
