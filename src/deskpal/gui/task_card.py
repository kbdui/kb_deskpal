"""Shared task review surface used by chat, pet chat and arrangements."""
import json
from typing import Any
from PySide6.QtCore import Qt
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout, QInputDialog, QSizePolicy, QTreeWidgetItem, QVBoxLayout)
from .components import (PushButton as QPushButton, CheckBox as QCheckBox, PlainTextEdit as QPlainTextEdit, TreeWidget as QTreeWidget)
from .display_text import compact_path, friendly_time
from .widgets import fill_tree, label, text_label
from .tool_results import TERMINAL_STATUSES, fit_dialog, has_tool_details


def _restore_literal_leaves(pending: list[QTreeWidgetItem | None]) -> None:
    while pending:
        node = pending.pop()
        if node is None: continue
        raw = node.data(1, Qt.ItemDataRole.UserRole)
        if raw is not None:
            node.setText(1, str(raw))
        pending.extend(node.child(index) for index in range(node.childCount()))


def _literal_tree(tree: QTreeWidget, value: Any) -> bool:
    """Plan arguments and tool results are payload, including nested enum-like names."""
    complete = fill_tree(tree, value)
    _restore_literal_leaves([tree.topLevelItem(index) for index in range(tree.topLevelItemCount())])
    return complete


_PATH_FIELDS = {"path", "source", "destination", "workspace", "workspace_dir"}
_TIME_FIELDS = {"due_at", "start_at", "end_at", "reminder_at", "start_after", "start_before", "created_at", "updated_at", "deadline"}
_STATUS_TEXT = {
    "Paused": "任务已暂停。继续后将结合最新状态处理，已完成操作保留。",
    "AwaitingInput": "需要补充信息，请回答后继续这项任务。",
    "Waiting": "请核对下面的操作范围，再决定是否授权。",
    "NeedsReview": "这项请求需要重新审核，请核对原计划与当前条件。",
    "Running": "正在执行，完成后会在这里更新结果。",
    "Queued": "已进入队列，等待执行。",
    "Completed": "这项请求已处理完成，执行记录可在下方查看。",
    "Succeeded": "这项请求已处理完成，执行记录可在下方查看。",
    "Failed": "这项请求未能完成，请查看原因。",
    "Uncertain": "执行结果尚待核实，请先查看记录。",
    "Cancelled": "这项请求已取消。",
}


def _literal(value: Any) -> str:
    if isinstance(value, str):
        return value if value else '""'
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _parameter_text(arguments: dict[str, Any], fields: list[Any], workspace: str) -> str:
    """Bounded document, not thousands of widgets; every field keeps its name."""
    names: dict[str, str] = {}
    pending = list(fields)
    metadata_count = 0
    while pending and metadata_count < 4096:
        metadata_count += 1
        field = pending.pop()
        if isinstance(field, dict):
            names[str(field.get("name"))] = str(field.get("label") or field.get("name"))
            pending.extend(field.get("children", []))
    lines: list[str] = []
    count = 0
    truncated = False
    def add(key: str, value: Any, depth: int) -> None:
        nonlocal count, truncated
        count += 1
        if count > 4096 or depth > 16:
            truncated = True
            return
        title = names.get(key) or label(key)
        prefix = "  " * depth + title + "："
        if isinstance(value, (dict, list, tuple)):
            lines.append(prefix + (f"共 {len(value)} 项" if value else ("{}" if isinstance(value, dict) else "[]")))
            children = value.items() if isinstance(value, dict) else enumerate(value, 1)
            for child_key, child in children:
                add(str(child_key), child, depth + 1)
        else:
            rendered = _literal(value)
            if isinstance(value, str) and key in _PATH_FIELDS:
                rendered = compact_path(value, workspace)
            elif isinstance(value, str) and key in _TIME_FIELDS:
                rendered = friendly_time(value)
            rendered = rendered.replace("\r", "\\r").replace("\n", "\\n")
            lines.append(prefix + (rendered[:180] + "…（完整值见下方）" if len(rendered) > 180 else rendered))
    for key, value in arguments.items():
        add(str(key), value, 0)
    if truncated:
        lines.append("范围过大，无法完整审核；请缩小请求。")
    return "\n".join(lines) or "这一步无需参数。"


class TaskCard(QFrame):
    def __init__(self, coordinator: Any, task_id: str, compact: bool = False, parent: Any = None) -> None:
        super().__init__(parent)
        self.coordinator, self.task_id, self.compact = coordinator, task_id, compact
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.signature: Any = None
        self._retired = False
        self._suppressed = False
        self.setObjectName("message")
        self.layout_box = QVBoxLayout(self)
        self.layout_box.setAlignment(Qt.AlignmentFlag.AlignTop)
        coordinator.task_changed.connect(self._changed)
        coordinator.reviews_cleared.connect(self._invalidate)
        self.destroyed.connect(lambda _=None, token=id(self): coordinator.watch_review(token, None))
        self.render_review()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event); self.coordinator.watch_review(id(self), self.task_id)

    def hideEvent(self, event: QHideEvent) -> None:
        self._close_dialogs()
        if not self.isEnabled(): self._retired = True
        self.coordinator.watch_review(id(self), None); super().hideEvent(event)

    def retire(self) -> None:
        """Retire synchronously before Qt's deferred widget deletion."""
        self._close_dialogs()
        self._retired = True
        self.coordinator.watch_review(id(self), None)
        self.setEnabled(False); self.hide()

    def _invalidate(self) -> None:
        self.signature = None
        self._close_dialogs()
        self.render_review()

    def _close_dialogs(self) -> None:
        for dialog in self.findChildren(QDialog):
            for body in dialog.findChildren(QPlainTextEdit):
                body.clear()
            dialog.setEnabled(False); dialog.close()

    def _show_results(self) -> None:
        review = self.coordinator.reviews.get(self.task_id, {})
        if self.compact or not review or review.get("review_unavailable") or not has_tool_details(review):
            return
        self.coordinator.tool_results_requested.emit(self.task_id)

    def _changed(self, task_id: str) -> None:
        if task_id == self.task_id:
            self.render_review()

    def _full_value(self, title: str, value: Any) -> None:
        raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
        dialog = QDialog(self); dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle(title + " · 完整值")
        layout = QVBoxLayout(dialog)
        body = QPlainTextEdit(); body.setReadOnly(True); body.setPlainText(raw); layout.addWidget(body)
        copy = QPushButton("复制完整值"); copy.clicked.connect(lambda: QApplication.clipboard().setText(raw)); layout.addWidget(copy)
        fit_dialog(dialog, self)
        dialog.open()

    def _details(self, title: str, tree: QTreeWidget, value: Any) -> None:
        if self.compact:
            tree.deleteLater()
            return
        row = QHBoxLayout()
        toggle = QPushButton(title + " ▸"); toggle.setCheckable(True)
        tree.setMinimumHeight(100); tree.setMaximumHeight(260 if self.compact else 320); tree.hide()
        toggle.toggled.connect(tree.setVisible)
        toggle.toggled.connect(lambda checked: toggle.setText(title + (" ▾" if checked else " ▸")))
        copy = QPushButton("查看/复制完整" + title)
        copy.clicked.connect(lambda: self._full_value(title, value))
        row.addWidget(toggle); row.addWidget(copy); row.addStretch()
        self.layout_box.addLayout(row); self.layout_box.addWidget(tree)

    def _supplement(self, status: str, row: QHBoxLayout) -> None:
        if status in {"Paused", "Created", "Planning", "Running", "Waiting", "AwaitingInput"}:
            supplement = QPushButton("补充回答并继续" if status == "AwaitingInput" else "追加要求")
            def add_requirement() -> None:
                content, accepted = QInputDialog.getMultiLineText(self, "补充当前任务", "新增要求在下一次模型判断时采用；已经完成的操作保留。")
                if accepted and content.strip():
                    operation = "task_answer" if status == "AwaitingInput" else "task_append_requirement"
                    self.coordinator.channel.send(operation, {"task_id": self.task_id, "content": content.strip()},
                        lambda _: self.coordinator.review(self.task_id), mutation=True)
            supplement.clicked.connect(add_requirement)
            row.addWidget(supplement)
        review = self.coordinator.reviews.get(self.task_id, {})
        if review.get("pause_control", {}).get("requested") and status != "Paused":
            self.layout_box.addWidget(text_label("正在暂停：当前调用或已决定的步骤组结束后生效。"))
        for action, title in (("task_pause", "暂停任务"), ("task_continue", "继续任务")):
            if action in review.get("available_actions", []):
                button = QPushButton(title)
                button.setEnabled(self.task_id not in self.coordinator.task_pending)
                button.clicked.connect(lambda checked=False, action=action: self.coordinator.task_action(self.task_id, action))
                row.addWidget(button)

    def render_review(self) -> None:
        if self._retired:
            return
        review = self.coordinator.reviews.get(self.task_id, {})
        signature = (review, self.coordinator.grants.get(self.task_id), self.task_id in self.coordinator.task_pending)
        if self.signature == signature:
            return
        previous_review = self.signature[0] if self.signature is not None else None
        import copy
        self.signature = copy.deepcopy(signature)
        def clear(layout: Any) -> None:
            while layout.count():
                item = layout.takeAt(0)
                widget = item.widget() if item is not None else None
                nested = item.layout() if item is not None else None
                if widget is not None:
                    widget.setEnabled(False); widget.hide(); widget.deleteLater()
                elif nested is not None:
                    clear(nested); nested.deleteLater()
        if review != previous_review:
            # Retire obsolete copies of approval scope when facts change.
            for dialog in self.findChildren(QDialog):
                for body in dialog.findChildren(QPlainTextEdit):
                    body.clear()
                dialog.setEnabled(False); dialog.close()
        clear(self.layout_box)
        if not review:
            self.layout_box.addWidget(text_label("正在读取任务审核内容…"))
            return
        if review.get("review_unavailable"):
            self.layout_box.addWidget(text_label(str(review["review_unavailable"])))
            reload = QPushButton("重新读取审核内容"); reload.clicked.connect(lambda: self.coordinator.review(self.task_id)); self.layout_box.addWidget(reload)
            return
        task = review.get("task", {})
        status = str(review.get("status", task.get("status", "")))
        steps = review.get("steps", [])
        if self._suppressed and has_tool_details(review):
            self._suppressed = False
            self.show()
        if status in TERMINAL_STATUSES:
            # The coherent assistant answer is its own timeline message. A
            # Main-window receipts link to the sidebar; pet chat shows no details.
            if has_tool_details(review) and not self.compact:
                results = QPushButton("查看工具结果"); results.clicked.connect(self._show_results)
                row = QHBoxLayout(); row.addWidget(results); row.addStretch(); self.layout_box.addLayout(row)
            else:
                self._suppressed = True; self.hide()
            return
        if status not in {"Waiting", "NeedsReview"} or review.get("confirmation_state") == "accepted":
            self.layout_box.addWidget(text_label("确认已受理，正在排队执行。" if review.get("confirmation_state") == "accepted" and status in {"Waiting", "NeedsReview"}
                                                else _STATUS_TEXT.get(status, "正在处理这项请求…")))
            if status == "AwaitingInput" and isinstance(task.get("result"), dict):
                self.layout_box.addWidget(text_label(str(task["result"].get("response", ""))))
            if task.get("cancel_requested"):
                self.layout_box.addWidget(text_label("正在请求取消；已开始的操作可能仍产生效果。"))
            row = QHBoxLayout()
            if has_tool_details(review) and not self.compact:
                details_button = QPushButton("查看工具结果"); details_button.clicked.connect(self._show_results); row.addWidget(details_button)
            if "cancel" in review.get("available_actions", []):
                cancel = QPushButton("取消这个请求"); cancel.setEnabled(self.task_id not in self.coordinator.task_pending)
                cancel.clicked.connect(lambda: self.coordinator.task_action(self.task_id, "cancel")); row.addWidget(cancel)
            self._supplement(status, row)
            row.addStretch(); self.layout_box.addLayout(row)
            return
        titles = [str(step.get("display_name") or step.get("tool_name") or "操作") for step in steps if isinstance(step, dict)]
        title = "、".join(titles[:2]) + (f"等 {len(titles)} 步操作" if len(titles) > 2 else "") if titles else str(review.get("title") or "这项安排")
        title_label = text_label(title + ' · ' + label(status)); title_label.setObjectName('task_card_title')
        self.layout_box.addWidget(title_label)
        if status in _STATUS_TEXT:
            self.layout_box.addWidget(text_label(_STATUS_TEXT[status]))
        if review.get("blocked_reason"):
            self.layout_box.addWidget(text_label(str(review["blocked_reason"])))
        if review.get("workspace"):
            workspace_row = QHBoxLayout()
            workspace_title = text_label("执行工作区：" + str(review.get("workspace_name") or compact_path(review["workspace"], str(review["workspace"]))))
            workspace_title.setMinimumWidth(0); workspace_title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
            workspace_row.addWidget(workspace_title)
            if not self.compact:
                location = QPushButton("查看/复制完整位置"); location.clicked.connect(lambda: self._full_value("执行工作区", review["workspace"]))
                workspace_row.addWidget(location)
            workspace_row.addStretch(); self.layout_box.addLayout(workspace_row)
        if review.get("confirmation_state") == "accepted":
            self.layout_box.addWidget(text_label("确认已受理，正在排队执行。"))
        tool_names = {step.get("tool_name"): str(step.get("display_name") or step.get("tool_name")) for step in steps if isinstance(step, dict)}
        complete_scope = True
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            self.layout_box.addWidget(text_label(f"{index + 1}. {step.get('display_name') or step.get('tool_name', '操作')}"))
            target = step.get("target_summary")
            if isinstance(target, dict):
                self.layout_box.addWidget(text_label("当前对象名称：" + str(target.get("name") or "当前不可查") + "（当前来源信息，不属于原计划冻结参数）"))
            details = QTreeWidget()
            details.setHeaderLabels(["审核范围", "内容"])
            # All argument fields remain readable; no truncated approval scope.
            fields = step.get("fields", [])
            argument_labels = {field.get("name"): field.get("label") for field in fields if isinstance(field, dict)}
            complete_scope = _literal_tree(details, {f"{argument_labels.get(key) or label(key)} ({key})": value for key, value in step.get("arguments", {}).items()}) and complete_scope
            arguments = step.get("arguments", {})
            document = QPlainTextEdit(); document.setObjectName("task_parameters"); document.setReadOnly(True)
            document.setMinimumWidth(0)
            parameter_text = _parameter_text(arguments, fields, str(review.get("workspace") or ""))
            document.setPlainText(parameter_text)
            document.setMinimumHeight(50)
            document.setMaximumHeight(min(220 if self.compact else 280, max(58, (min(10, parameter_text.count("\n") + 1) + 1) * document.fontMetrics().lineSpacing() + 12)))
            self.layout_box.addWidget(document)
            self._details("原参数", details, arguments)
        if not complete_scope:
            self.layout_box.addWidget(text_label("操作范围超过 4096 项或 16 层，无法完整审核，不能确认执行；请缩小请求范围。"))
        if task.get("error"):
            self.layout_box.addWidget(text_label("未完成原因：" + str(task["error"])))
        actions = review.get("available_actions", [])
        waiting = status in {"Waiting", "NeedsReview"} and "confirm" in actions and review.get("confirmation_state") != "accepted"
        complex_review = len(steps) > 1 or any(len(str(step.get("arguments", {}))) > 180 for step in steps if isinstance(step, dict))
        row = QHBoxLayout()
        if not self.compact and has_tool_details(review):
            records_button = QPushButton('执行记录'); records_button.clicked.connect(self._show_results); row.addWidget(records_button)
        if waiting:
            if any(step.get("tool_name") == "memory_forget" for step in steps):
                self.layout_box.addWidget(text_label("遗忘会清理这条记忆关联的聊天记录和任务正文，也可能中止当前请求。仅标记事情已完成时，请取消并要求更正记忆。"))
            permissions = review.get("missing_permissions", {})
            ordinary = list(permissions.get("ordinary_tools", []))
            high = list(permissions.get("high_risk_tools", []))
            grant = self.coordinator.grants.setdefault(self.task_id, set())
            if ordinary:
                self.layout_box.addWidget(text_label("以下按钮将明确允许本计划全部普通工具：" + "、".join(tool_names.get(tool, label(tool)) for tool in ordinary)))
            for tool in high:
                check = QCheckBox("额外允许高风险工具：" + tool_names.get(tool, label(tool)))
                check.setChecked(tool in grant)
                check.toggled.connect(lambda checked, tool=tool: self.coordinator.set_grant(self.task_id, tool, checked))
                self.layout_box.addWidget(check)
            confirm = QPushButton("允许上述普通操作" if ordinary else "确认所选授权" if high else "确认并继续原计划")
            confirm.setObjectName("primary")
            from .components import primary
            primary(confirm)
            confirm.setEnabled(self.task_id not in self.coordinator.task_pending and set(high).issubset(grant) and not (self.compact and complex_review) and complete_scope)
            missing = set(high) - grant
            if missing:
                self.layout_box.addWidget(text_label("请分别审核并勾选这些高风险操作：" + "、".join(tool_names.get(tool, label(tool)) for tool in sorted(missing))))
            confirm.clicked.connect(lambda: self.coordinator.confirm(self.task_id))
            row.addWidget(confirm)
        for action, title in (("resume", "重新审核"), ("cancel", "取消这个请求")):
            if action in actions:
                button = QPushButton(title)
                button.setEnabled(self.task_id not in self.coordinator.task_pending)
                button.clicked.connect(lambda checked=False, action=action: self.coordinator.task_action(self.task_id, action))
                row.addWidget(button)
        self._supplement(status, row)
        if self.compact and complex_review and status in {"Waiting", "NeedsReview"}:
            self.layout_box.addWidget(text_label("这项操作范围较长，请展开主窗口完整审核后确认；已选高风险项会保留。"))
            expand = QPushButton("展开完整审核")
            expand.clicked.connect(lambda: self.coordinator.expand_requested.emit(str(review.get("conversation_id") or ""), self.task_id))
            row.addWidget(expand)
        row.addStretch()
        self.layout_box.addLayout(row)
        if self.task_id in self.coordinator.task_pending:
            self.layout_box.addWidget(text_label("操作已提交，等待后台确认；请勿重复操作。"))
