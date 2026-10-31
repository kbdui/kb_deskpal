"""Read-only, bounded sidebar over recorded tool facts; never an approval surface."""
from __future__ import annotations

import copy
import json
from collections import Counter, defaultdict
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget)
from .components import (PushButton as QPushButton, PlainTextEdit as QPlainTextEdit, ScrollArea as QScrollArea)

from deskpal.response.templates import tool_result_text
from .display_text import compact_path, friendly_time
from .widgets import label, text_label


TERMINAL_STATUSES = frozenset({"Completed", "Succeeded", "Failed", "Cancelled", "Uncertain"})
_PAGE_SIZE = 12
_TEXT_LIMIT = 24000


def fit_dialog(dialog: QDialog, owner: QWidget) -> None:
    """Leave room for window chrome even on a small pet-window screen."""
    screen = owner.screen()
    available = screen.availableGeometry() if screen is not None else None
    width = max(1, available.width() - 48) if available else 680
    height = max(1, available.height() - 80) if available else 500
    dialog.setMinimumSize(min(320, width), min(220, height))
    dialog.resize(min(680, width), min(500, height))
    dialog.setSizeGripEnabled(True)


def has_tool_details(review: dict[str, Any]) -> bool:
    task = review.get("task", {})
    result = task.get("result") if isinstance(task, dict) else None
    attempts = review.get("attempt_summary", {})
    return bool(review.get("steps") or (isinstance(result, dict) and result.get("tool_results"))
                or (isinstance(attempts, dict) and (attempts.get("count") or attempts.get("items"))))


def operation_records(review: dict[str, Any]) -> list[dict[str, Any]]:
    """Associate facts only by stable identity or an unambiguous legacy name.

    Planned calls without evidence stay explicitly unexecuted/unknown. An
    attempt's status is evidence of that attempt, never a fabricated result.
    """
    steps = [step for step in review.get("steps", []) if isinstance(step, dict)]
    task = review.get("task", {})
    result = task.get("result") if isinstance(task, dict) else None
    results = [item for item in result.get("tool_results", []) if isinstance(item, dict)] if isinstance(result, dict) else []
    summary = review.get("attempt_summary", {})
    attempts = [item for item in summary.get("items", []) if isinstance(item, dict)] if isinstance(summary, dict) else []
    records: list[dict[str, Any]] = []
    used: set[int] = set()
    covered_attempts: set[int] = set()
    identities = Counter(str(step.get("call_id") or "") for step in steps)
    names = Counter(str(step.get("tool_name") or "") for step in steps)
    results_by_id: dict[str, list[int]] = defaultdict(list)
    legacy_results: dict[str, list[int]] = defaultdict(list)
    for result_index, item in enumerate(results):
        if item.get("call_id"):
            results_by_id[str(item["call_id"])].append(result_index)
        else:
            legacy_results[str(item.get("tool_name", item.get("name")) or "")].append(result_index)
    attempts_by_index = {item.get("index"): (index, item) for index, item in enumerate(attempts)}
    for position, step in enumerate(steps):
        call_id = str(step.get("call_id") or "")
        name = str(step.get("tool_name") or "")
        matches = [index for index in results_by_id.get(call_id, []) if identities[call_id] == 1
                   and results[index].get("tool_name", results[index].get("name")) == name]
        if not matches and not call_id:
            if names[name] == 1:
                matches = legacy_results.get(name, [])
        index = step.get("index", position)
        found = attempts_by_index.get(index)
        attempt = found[1] if found is not None else None
        if found is not None:
            covered_attempts.add(found[0])
        if matches:
            for result_index in matches:
                used.add(result_index)
                records.append({"step": step, "result": results[result_index], "attempt": attempt})
        else:
            records.append({"step": step, "result": None, "attempt": attempt})
    # A result with an unknown identity is still a recorded fact. Do not attach
    # an unrelated planned argument set by list position.
    records.extend({"step": None, "result": item, "attempt": None}
                   for index, item in enumerate(results) if index not in used)
    records.extend({"step": None, "result": None, "attempt": item}
                   for index, item in enumerate(attempts) if index not in covered_attempts)
    return records


def _bounded(text: str) -> str:
    return text if len(text) <= _TEXT_LIMIT else text[:_TEXT_LIMIT] + "\n…本段显示已截短，完整原值可在技术记录中查看/复制。"


def _parameters(step: dict[str, Any], workspace: str) -> str:
    """Readable parameter context, with exact JSON reserved for diagnostics."""
    names = {str(field.get("name")): str(field.get("label") or field.get("name"))
             for field in step.get("fields", []) if isinstance(field, dict)}
    lines: list[str] = []
    budget = 256
    def add(key: str, value: Any, depth: int) -> None:
        nonlocal budget
        if budget <= 0:
            return
        budget -= 1
        title = names.get(key) or label(key)
        prefix = "  " * min(depth, 16) + title + "："
        if isinstance(value, (dict, list, tuple)):
            lines.append(prefix + f"{len(value)} 项")
            if depth >= 16:
                lines.append("  " * depth + "层级较深，完整参数见技术记录。")
                return
            children = value.items() if isinstance(value, dict) else ((f"第 {index} 项", child) for index, child in enumerate(value, 1))
            for child_key, child in children:
                add(str(child_key), child, depth + 1)
                if budget <= 0:
                    break
        else:
            rendered = "未设置" if value is None else label(value) if type(value) is bool else str(value)
            if isinstance(value, str) and key in {"path", "source", "destination"}:
                rendered = compact_path(value, workspace)
            elif isinstance(value, str) and key in {"start_at", "end_at", "due_at", "reminder_at", "start_after", "start_before"}:
                rendered = friendly_time(value)
            lines.append(prefix + _bounded(rendered))
    for key, value in step["arguments"].items():
        add(str(key), value, 1)
        if budget <= 0:
            lines.append("其余参数见技术记录。")
            break
    return _bounded("\n".join(lines))


def _record_text(record: dict[str, Any], number: int, running: bool, workspace: str) -> str:
    step, result, attempt = record["step"], record["result"], record["attempt"]
    name = (step.get("display_name") or label(str(step.get("tool_name") or "操作"))) if step else "已记录操作"
    lines = [f"{number}. {name}"]
    if result is not None:
        lines.append(tool_result_text(result))
    elif attempt is not None:
        status = attempt.get("status")
        lines.append({"Succeeded": "执行记录标记为成功；未保留可读结果。",
                      "Failed": "执行记录标记为失败；可能已有部分效果，未保留可读结果。",
                      "Started": "已开始，尚未记录最终结果。" if running else "已开始，最终结果未知；请核查实际效果。",
                      "Uncertain": "执行结果未知；请核查实际效果。"}.get(str(status), "存在执行记录，最终结果尚无法确定。"))
    else:
        lines.append("仅有原计划，未见执行记录；不能据此认定这一步已经执行。")
    if step and isinstance(step.get("arguments"), dict) and step["arguments"]:
        # This is explicitly the frozen intent, not an assertion of actual effects.
        lines.append("原计划参数：")
        lines.append(_parameters(step, workspace))
    elif result is not None and step is None:
        lines.append("此结果未能关联原计划参数。")
    return _bounded("\n".join(lines))


def _record_state(record: dict[str, Any], running: bool) -> tuple[str, str]:
    result, attempt = record["result"], record["attempt"]
    if result is not None:
        success = result.get("success")
        if success is True:
            return "工具返回结果", "success"
        if success is False:
            return "未完成", "failure"
        return "结果待核实", "unknown"
    if attempt is not None:
        status = str(attempt.get("status") or "")
        if status == "Succeeded":
            return "标记为成功", "success"
        if status == "Failed":
            return "标记为失败", "failure"
        if status == "Started":
            return ("执行中" if running else "最终状态未知"), "unknown"
        if status == "Uncertain":
            return "结果待核实", "unknown"
    return "仅计划", "planned"


def _record_timestamp(record: dict[str, Any]) -> str:
    for source in (record.get("result"), record.get("attempt")):
        if isinstance(source, dict):
            value = source.get("timestamp", source.get("recorded_at", source.get("created_at")))
            if value:
                # Keep the stored precision and offset; this is a recorded fact,
                # not a time estimate or a value normalized for display.
                return str(value)
    return ""


class _RecordList(QFrame):
    """Scrollable card list with a plain-text view kept for callers and tests."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(9)
        self._text = ""

    def toPlainText(self) -> str:
        return self._text

    def _clear_cards(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide(); widget.deleteLater()

    def set_records(self, records: list[dict[str, Any]], start: int, running: bool, workspace: str) -> None:
        self._clear_cards()
        lines: list[str] = []
        if not records:
            empty = QFrame(); empty.setObjectName("tool_result_empty")
            empty_layout = QVBoxLayout(empty); empty_layout.setContentsMargins(16, 18, 16, 18)
            empty_label = text_label("暂时没有可展示的工具执行记录。保存的任务状态不代表操作已成功。")
            empty_label.setObjectName("tool_result_empty_text"); empty_layout.addWidget(empty_label)
            empty.setProperty("record_text", "暂时没有可展示的工具执行记录。保存的任务状态不代表操作已成功。")
            self._layout.addWidget(empty)
            lines.append(empty.property("record_text"))
        for index, record in enumerate(records):
            card = QFrame(); card.setObjectName("tool_result_card")
            card.setMinimumWidth(0); card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
            box = QVBoxLayout(card); box.setContentsMargins(13, 12, 13, 13); box.setSpacing(8)
            step = record["step"]
            name = (step.get("display_name") or label(str(step.get("tool_name") or "操作"))) if step else "已记录操作"
            state, state_class = _record_state(record, running)
            heading = QHBoxLayout(); heading.setSpacing(8)
            title = text_label(f"{start + index + 1}. {name}", heading=True)
            title.setObjectName("tool_result_card_title"); title.setMinimumWidth(0)
            badge = QLabel(state); badge.setObjectName("tool_result_status")
            badge.setProperty("state", state_class); badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
            heading.addWidget(title, 1); heading.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
            box.addLayout(heading)
            summary_text = tool_result_text(record['result']) if record['result'] is not None else _record_text(record, start + index + 1, running, workspace).split('\n', 1)[1].split('\n原计划参数：', 1)[0]
            summary = text_label(summary_text)
            summary.setObjectName("tool_result_card_summary")
            box.addWidget(summary)
            stamp = _record_timestamp(record)
            if stamp:
                time_label = text_label("记录时间：" + friendly_time(stamp)); time_label.setObjectName("tool_result_timestamp")
                box.addWidget(time_label)
            if step and isinstance(step.get("arguments"), dict) and step["arguments"]:
                details = QFrame(); details.setObjectName("tool_result_parameters")
                details_layout = QVBoxLayout(details); details_layout.setContentsMargins(10, 8, 10, 8); details_layout.setSpacing(4)
                parameters_title = text_label("原计划参数"); parameters_title.setObjectName("tool_result_parameters_title")
                parameters = text_label(_parameters(step, workspace)); parameters.setObjectName("tool_result_parameters_text")
                details_layout.addWidget(parameters_title); details_layout.addWidget(parameters)
                box.addWidget(details)
            elif record["result"] is not None and step is None:
                box.addWidget(text_label("此结果未能关联原计划参数。"))
            rendered = _record_text(record, start + index + 1, running, workspace)
            card.setProperty("record_text", rendered)
            lines.append(rendered)
            self._layout.addWidget(card)
        self._text = "\n\n".join(lines)
        self._layout.addStretch(1)

    def set_notices(self, notices: list[str]) -> None:
        if not notices:
            return
        notice = QFrame(); notice.setObjectName("tool_result_notice")
        notice_layout = QVBoxLayout(notice); notice_layout.setContentsMargins(12, 10, 12, 10)
        message = text_label("\n".join(notices)); message.setObjectName("tool_result_notice_text")
        notice_layout.addWidget(message)
        self._layout.insertWidget(0, notice)
        self._text = "\n\n".join(["\n".join(notices), self._text]).strip()

    def clear(self) -> None:
        self._clear_cards(); self._text = ""


class ToolResultsPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(0)
        self._review: dict[str, Any] = {}
        self._records: list[dict[str, Any]] = []
        self._page = 0
        self._raw = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        self.summary_card = QFrame(); self.summary_card.setObjectName("tool_result_summary")
        summary_layout = QVBoxLayout(self.summary_card); summary_layout.setContentsMargins(14, 12, 14, 12); summary_layout.setSpacing(5)
        self.summary_title = text_label("执行记录", heading=True); self.summary_title.setObjectName("tool_result_summary_title")
        self.summary = text_label(); self.summary.setObjectName("tool_result_summary_text")
        summary_layout.addWidget(self.summary_title); summary_layout.addWidget(self.summary)
        layout.addWidget(self.summary_card)
        self.records_scroll = QScrollArea(); self.records_scroll.setWidgetResizable(True)
        self.records_scroll.setObjectName("tool_result_scroll")
        self.body = _RecordList(); self.body.setObjectName("tool_result_document")
        self.records_scroll.setWidget(self.body); layout.addWidget(self.records_scroll, 1)
        pages = QHBoxLayout()
        self.previous = QPushButton("上一页"); self.previous.clicked.connect(lambda: self._turn(-1)); pages.addWidget(self.previous)
        self.page_label = text_label(); pages.addWidget(self.page_label)
        self.next = QPushButton("下一页"); self.next.clicked.connect(lambda: self._turn(1)); pages.addWidget(self.next); pages.addStretch()
        layout.addLayout(pages)
        self.diagnostic = QPushButton("查看原始技术记录"); self.diagnostic.setObjectName("tool_result_diagnostic_toggle")
        self.diagnostic.setCheckable(True); self.diagnostic.toggled.connect(self._diagnostics); layout.addWidget(self.diagnostic)
        self.raw_body = QPlainTextEdit(); self.raw_body.setObjectName("tool_result_diagnostics")
        self.raw_body.setReadOnly(True); self.raw_body.hide(); layout.addWidget(self.raw_body, 1)
        self.raw_pages = QHBoxLayout()
        self.raw_previous = QPushButton("上一段"); self.raw_previous.clicked.connect(lambda: self._raw_turn(-1)); self.raw_pages.addWidget(self.raw_previous)
        self.raw_next = QPushButton("下一段"); self.raw_next.clicked.connect(lambda: self._raw_turn(1)); self.raw_pages.addWidget(self.raw_next)
        self.copy = QPushButton("复制完整记录"); self.copy.clicked.connect(self._copy); self.raw_pages.addWidget(self.copy)
        layout.addLayout(self.raw_pages)
        self._raw_offset = 0
        self._diagnostics(False)
        self.invalidate()

    def update_review(self, review: dict[str, Any]) -> None:
        self.setEnabled(True)
        if review == self._review:
            return
        self._review = copy.deepcopy(review)
        self._records = operation_records(self._review)
        self._page = min(self._page, max(0, (len(self._records) - 1) // _PAGE_SIZE))
        self._raw = ""; self._raw_offset = 0
        self._render()
        if self.diagnostic.isChecked():
            self._diagnostics(True)

    def _render(self) -> None:
        task = self._review.get("task", {})
        status = str(self._review.get("status", task.get("status", "")))
        self.summary_title.setText("任务状态 · " + (label(status) if status else "尚无状态"))
        self.summary.setText("以下记录按原计划顺序排列；未关联的结果列在后面。执行状态只反映已保存的事实。")
        start = self._page * _PAGE_SIZE
        selected = self._records[start:start + _PAGE_SIZE]
        self.body.set_records(selected, start, status not in TERMINAL_STATUSES, str(self._review.get("workspace") or ""))
        notices: list[str] = []
        if task.get("error"):
            notices.append("任务原因：" + _bounded(str(task["error"])))
        if self._review.get("blocked_reason"):
            notices.append(_bounded(str(self._review["blocked_reason"])))
        if status == "Cancelled":
            notices.append("任务已取消。已发生的效果未被撤回，已开始的操作可能仍产生效果。")
        self.body.set_notices(notices)
        pages = max(1, (len(self._records) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        self.page_label.setText(f"第 {self._page + 1} / {pages} 页")
        self.previous.setEnabled(self._page > 0); self.next.setEnabled(self._page + 1 < pages)
        self.previous.setVisible(pages > 1); self.next.setVisible(pages > 1); self.page_label.setVisible(pages > 1)

    def _turn(self, delta: int) -> None:
        self._page += delta; self._render()

    def _diagnostics(self, checked: bool) -> None:
        self.diagnostic.setText('收起原始技术记录' if checked else '查看原始技术记录')
        self.raw_body.setVisible(checked)
        for button in (self.raw_previous, self.raw_next, self.copy):
            button.setVisible(checked)
        if checked:
            if not self._raw:
                self._raw = json.dumps(self._review, ensure_ascii=False, indent=2, allow_nan=False)
            self._raw_turn(0)

    def _raw_turn(self, delta: int) -> None:
        self._raw_offset = max(0, self._raw_offset + delta * _TEXT_LIMIT)
        self.raw_body.setPlainText(self._raw[self._raw_offset:self._raw_offset + _TEXT_LIMIT])
        self.raw_previous.setEnabled(self._raw_offset > 0)
        self.raw_next.setEnabled(self._raw_offset + _TEXT_LIMIT < len(self._raw))

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._raw)

    def invalidate(self) -> None:
        self._review.clear(); self._records.clear(); self._raw = ""
        self._page = 0; self._raw_offset = 0
        self.body.clear(); self.raw_body.clear(); self.summary.clear(); self.summary_title.setText("执行记录")
        self.page_label.clear()
        self.diagnostic.setChecked(False)
        self.setEnabled(False)
