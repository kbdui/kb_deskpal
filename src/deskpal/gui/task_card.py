"""Shared task review surface used by chat, pet chat and arrangements."""
from typing import Any
from PySide6.QtCore import Qt
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout
from .widgets import fill_tree, label, text_label


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


class TaskCard(QFrame):
    def __init__(self, coordinator: Any, task_id: str, compact: bool = False, parent: Any = None) -> None:
        super().__init__(parent)
        self.coordinator, self.task_id, self.compact = coordinator, task_id, compact
        self.signature: Any = None
        self.setObjectName("message")
        self.layout_box = QVBoxLayout(self)
        coordinator.task_changed.connect(self._changed)
        self.destroyed.connect(lambda _=None, token=id(self): coordinator.watch_review(token, None))
        self.render_review()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event); self.coordinator.watch_review(id(self), self.task_id)

    def hideEvent(self, event: QHideEvent) -> None:
        self.coordinator.watch_review(id(self), None); super().hideEvent(event)

    def _changed(self, task_id: str) -> None:
        if task_id == self.task_id:
            self.render_review()

    def render_review(self) -> None:
        review = self.coordinator.reviews.get(self.task_id, {})
        signature = (review, self.coordinator.grants.get(self.task_id), self.task_id in self.coordinator.task_pending)
        if self.signature == signature:
            return
        import copy
        self.signature = copy.deepcopy(signature)
        while self.layout_box.count():
            item = self.layout_box.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setEnabled(False); widget.hide(); widget.deleteLater()
        if not review:
            self.layout_box.addWidget(text_label("正在读取任务审核内容…"))
            return
        if review.get("review_unavailable"):
            self.layout_box.addWidget(text_label(str(review["review_unavailable"])))
            reload = QPushButton("重新读取审核内容"); reload.clicked.connect(lambda: self.coordinator.review(self.task_id)); self.layout_box.addWidget(reload)
        task = review.get("task", {})
        status = str(review.get("status", task.get("status", "")))
        self.layout_box.addWidget(text_label(str(review.get("title") or task.get("request") or "这项安排") + " · " + label(status), heading=True))
        if review.get("blocked_reason"):
            self.layout_box.addWidget(text_label(str(review["blocked_reason"])))
        if review.get("workspace"):
            self.layout_box.addWidget(text_label("执行工作区：" + str(review["workspace"])))
        if review.get("confirmation_state") == "accepted":
            self.layout_box.addWidget(text_label("确认已受理，正在排队执行。"))
        steps = review.get("steps", [])
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
            details.setMinimumHeight(100)
            details.setMaximumHeight(260 if self.compact else 380)
            # All argument fields remain readable; no truncated approval scope.
            fields = step.get("fields", [])
            argument_labels = {field.get("name"): field.get("label") for field in fields if isinstance(field, dict)}
            complete_scope = _literal_tree(details, {f"{argument_labels.get(key) or label(key)} ({key})": value for key, value in step.get("arguments", {}).items()}) and complete_scope
            self.layout_box.addWidget(details)
        if task.get("error"):
            self.layout_box.addWidget(text_label("未完成原因：" + str(task["error"])))
        if task.get("result"):
            details = QTreeWidget(); details.setHeaderLabels(["执行结果", "内容"])
            _literal_tree(details, task["result"]); self.layout_box.addWidget(details)
        actions = review.get("available_actions", [])
        waiting = status in {"Waiting", "NeedsReview"} and "confirm" in actions and review.get("confirmation_state") != "accepted"
        complex_review = len(steps) > 1 or any(len(str(step.get("arguments", {}))) > 250 for step in steps if isinstance(step, dict))
        row = QHBoxLayout()
        if waiting:
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
        if self.compact and complex_review:
            self.layout_box.addWidget(text_label("这项操作范围较长，请展开主窗口完整审核后确认；已选高风险项会保留。"))
            expand = QPushButton("展开完整审核")
            expand.clicked.connect(lambda: self.coordinator.expand_requested.emit(str(review.get("conversation_id") or ""), self.task_id))
            row.addWidget(expand)
        self.layout_box.addLayout(row)
        if self.task_id in self.coordinator.task_pending:
            self.layout_box.addWidget(text_label("操作已提交，等待后台确认；请勿重复操作。"))
        technical = QPushButton("技术信息 ▸")
        tree = QTreeWidget(); tree.setHeaderLabels(["字段", "内容"]); fill_tree(tree, review); tree.hide()
        nodes = [tree.topLevelItem(index) for index in range(tree.topLevelItemCount())]
        while nodes:
            node = nodes.pop()
            if node is None: continue
            if node.text(0) in {label("arguments"), label("result")}:
                _restore_literal_leaves([node])
            else:
                nodes.extend(node.child(index) for index in range(node.childCount()))
        technical.setCheckable(True); technical.toggled.connect(tree.setVisible)
        self.layout_box.addWidget(technical); self.layout_box.addWidget(tree)
