"""Plain-text presentation, safe scrolling and IME-aware message input."""

from typing import Any

from PySide6.QtCore import QObject, QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QInputMethodEvent, QKeyEvent, QWheelEvent
from PySide6.QtWidgets import (QApplication, QAbstractItemView, QAbstractScrollArea, QAbstractSlider, QAbstractSpinBox, QComboBox, QDialog, QLabel, QSizePolicy, QVBoxLayout, QScrollBar, QTreeWidgetItem, QWidget)
from .components import (PushButton as QPushButton, PlainTextEdit as QPlainTextEdit, TreeWidget as QTreeWidget, ScrollArea as QScrollArea)


from .components import ChoiceBox


class ArrowComboBox(ChoiceBox):
    """Fluent 下拉菜单，沿用已有选择值及调用方名称。"""


LABELS = {"automatic": "自动学习与模型复核", "explicit_only": "自动学习与模型复核", "candidate_review": "自动学习与模型复核", "paused": "暂停记忆写入", "pending": "等待到期", "fired": "已到期", "cancelled": "已取消", "scheduled": "已安排", "reminded": "已提醒", "openai": "OpenAI 兼容服务", "offline": "离线演示", "responses": "Responses 协议", "prompt": "提示协议", "json_object": "原生 JSON 对象", "disabled": "关闭", "fastembed": "本地语义检索", "session": "仅选定会话", "today": "今天", "persistent": "持续保存", "brief": "简短", "normal": "适中", "detailed": "详细", "true": "允许", "false": "关闭", "Removed": "已移除", "unknown": "尚无法判断", "occurred": "已发生", "not_occurred": "未发生", "due": "已到期", "missed": "错过提醒", "suppressed": "已抑制", "redacted": "已清理", "execution_location": "执行位置", "provider": "服务提供方", "model": "模型名称", "protocol": "协议", "llm_ready": "连接配置就绪", "credential_mode": "凭据模式", "credential_source": "凭据来源", "active_revision": "当前生效版本", "request_use_default": "默认允许本次请求云处理", "memory_mode": "记忆模式", "embedding_provider": "检索模型", "data_dir": "数据目录", "cloud": "云模型", "local": "本地模型", "system_vault": "系统凭据库", "environment": "环境变量", "none": "无需凭据", "calendar": "日程", "timer": "计时器", "task": "任务", "Ignored": "已忽略", "Accepted": "已接受", "Expired": "已过期", "Invalidated": "已失效", "Planning": "正在理解", "Created": "已创建", "title": "名称", "status": "状态", "content": "内容", "request": "请求", "response": "回复",
          "description": "说明", "path": "路径", "destination": "目标路径", "source": "来源", "source_id": "来源记录",
          "deadline": "截止时间", "success_criteria": "成功标准", "progress_note": "进度备注", "evidence": "人工证据",
          "use_policy": "可用范围", "revision": "版本", "created_at": "创建时间", "updated_at": "更新时间",
          "due_at": "提醒时间", "label": "名称", "arguments": "操作参数", "result": "结果", "risk": "风险",
          "local_only": "仅本地", "cloud_allowed": "允许云处理", "Active": "进行中", "Paused": "已暂停",
          "Completed": "已完成", "Cancelled": "已取消", "Pending": "待处理", "Waiting": "待我确认",
          "NeedsReview": "需要重新审核", "Running": "执行中", "Queued": "排队中", "Failed": "失败",
          "Succeeded": "已完成", "AwaitingInput": "等待补充信息", "Uncertain": "结果待核实", "accepted": "已采用", "candidate": "待复核理解",
          "rejected": "已拒绝", "deleted": "已删除", "disputed": "有争议", "all": "全部",
          "ordinary_tools": "普通工具", "high_risk_tools": "高风险工具", "workspace_dir": "工作区",
          "display_name": "显示名称", "tool_name": "工具名称", "permission_level": "所需权限", 'limit': '数量上限'}


def label(value: object) -> str:
    if type(value) is bool:
        return "是" if value else "否"
    text = str(value)
    return LABELS.get(text, text)


def text_label(text: str = "", *, heading: bool = False) -> QLabel:
    from .components import BodyLabel
    widget = BodyLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    widget.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if heading:
        widget.setObjectName("heading")
    return widget


class _NoticeLabel(QLabel):
    """A status line occupies space only while it has something to report."""

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setVisible(bool(text.strip()))

    def clear(self) -> None:
        self.setText("")


def notice_label() -> QLabel:
    widget = _NoticeLabel()
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    widget.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    widget.hide()
    return widget


def scroll(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(widget)
    area.setMinimumSize(0, 0)
    return area


def _tree_value(tree: QTreeWidget, item: QTreeWidgetItem, column: int) -> None:
    value = item.data(1, Qt.ItemDataRole.UserRole)
    if value is None:
        return
    dialog = QDialog(tree); dialog.setWindowTitle(item.text(0) + " · 全文（可复制）")
    layout = QVBoxLayout(dialog); text = QPlainTextEdit(); text.setReadOnly(True); text.setPlainText(str(value)); layout.addWidget(text)
    copy = QPushButton("复制全文"); copy.clicked.connect(lambda: QApplication.clipboard().setText(str(value))); layout.addWidget(copy)
    screen = tree.screen(); available = screen.availableGeometry() if screen is not None else None
    dialog.resize(min(720, available.width() - 40) if available else 720, min(520, available.height() - 60) if available else 520); dialog.exec()


def fill_tree(tree: QTreeWidget, value: Any) -> bool:
    """Readable fields; explicit budget failure prevents incomplete approval."""
    tree.clear(); budget = [0]; complete = [True]
    if not tree.property("deskpal_copy_ready"):
        tree.itemDoubleClicked.connect(lambda item, column: _tree_value(tree, item, column))
        tree.setProperty("deskpal_copy_ready", True)
        tree.setToolTip("双击字段查看并复制完整内容")
    def add(parent: Any, key: str, item: Any, depth: int) -> None:
        budget[0] += 1
        if budget[0] > 4096 or depth > 16:
            complete[0] = False; return
        if isinstance(item, (dict, list, tuple)):
            node = QTreeWidgetItem([label(key), ""])
            parent.addChild(node) if isinstance(parent, QTreeWidgetItem) else parent.addTopLevelItem(node)
            children = item.items() if isinstance(item, dict) else enumerate(item, 1)
            for child_key, child in children:
                add(node, str(child_key), child, depth + 1)
        else:
            semantic = key in {"status", "delivery_state", "use_policy", "provider", "protocol", "execution_location", "credential_mode", "credential_source", "memory_mode", "embedding_provider", "permission_level", "状态", "可用范围"}
            rendered = label(item) if semantic or type(item) is bool else str(item) if item is not None else "未设置"
            node = QTreeWidgetItem([label(key), rendered])
            node.setData(1, Qt.ItemDataRole.UserRole, str(item))
            parent.addChild(node) if isinstance(parent, QTreeWidgetItem) else parent.addTopLevelItem(node)
    add(tree, "详情", value, 0)
    if not complete[0]:
        tree.addTopLevelItem(QTreeWidgetItem(["显示范围不完整", "内容超过 4096 项或 16 层，无法在此完整审核；请缩小请求范围。不可据此确认执行。 "]))
    tree.expandToDepth(2); tree.setProperty("deskpal_complete", complete[0]); return complete[0]


class WheelGuard(QObject):
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Scroll areas deliver viewport wheel events to their own QScrollBar.
        # Sending that scrollbar event back to the viewport recurses through
        # Qt's native wheelEvent until the process exhausts its stack.
        if isinstance(watched, QScrollBar):
            return super().eventFilter(watched, event)
        if isinstance(event, QWheelEvent) and isinstance(watched, QWidget):
            parent = watched.parentWidget()
            if isinstance(parent, QAbstractItemView) and parent.viewport() is watched:
                for widget in QApplication.allWidgets():
                    if isinstance(widget, QComboBox) and widget.view() is parent and widget.view().isVisible():
                        delta = event.angleDelta().y()
                        bar = parent.verticalScrollBar()
                        bar.setValue(bar.value() - (delta // 120 or (1 if delta > 0 else -1)) * max(1, bar.singleStep()) * 3)
                        event.accept()
                        return True
        if isinstance(event, QWheelEvent) and isinstance(watched, (QComboBox, ChoiceBox, QAbstractSpinBox, QAbstractSlider)):
            parent = watched.parentWidget()
            while parent is not None:
                if isinstance(parent, QAbstractScrollArea):
                    QApplication.sendEvent(parent.viewport(), event)
                    return True
                parent = parent.parentWidget()
            event.ignore()
            return True
        return super().eventFilter(watched, event)


def install_wheel_guard() -> None:
    app = QApplication.instance()
    if app is not None and not hasattr(app, "_deskpal_wheel_guard"):
        guard = WheelGuard(app)
        setattr(app, "_deskpal_wheel_guard", guard)
        app.installEventFilter(guard)


class MessageInput(QPlainTextEdit):
    send_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._composing = False
        self._ime_commit_guard = False
        self.setPlaceholderText("写点什么…  Enter 发送，Shift + Enter 换行")
        self.setMaximumHeight(132)
        self.setMinimumHeight(82)

    def inputMethodEvent(self, event: QInputMethodEvent) -> None:
        self._composing = bool(event.preeditString())
        self._ime_commit_guard = True
        QTimer.singleShot(0, self._release_ime_guard)
        super().inputMethodEvent(event)

    def _release_ime_guard(self) -> None:
        self._ime_commit_guard = False

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            if self._composing or self._ime_commit_guard:
                event.accept()
                return
            if not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                if not event.isAutoRepeat():
                    self.send_requested.emit()
                event.accept()
                return
        super().keyPressEvent(event)
