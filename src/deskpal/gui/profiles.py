"""Closed-host profile management using the bounded maintenance worker."""

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QInputDialog, QLabel, QListWidgetItem, QMessageBox, QVBoxLayout, QWidget)
from .components import (PushButton as QPushButton, ListWidget as QListWidget)

from .maintenance import MaintenanceBridge
from .components import SettingsWindow


def profile_bridge(path: Path, *, parent: Any = None,
                   provider_override: str | None = None) -> MaintenanceBridge:
    # Import and service construction both happen on the worker, never Qt's thread.
    def factory(config_path: Path, cancelled: Any) -> Any:
        from deskpal.application.profiles import ProfileService
        return ProfileService(config_path, cancelled=cancelled,
                              provider_override=provider_override)
    return MaintenanceBridge(path, factory=factory, parent=parent)


class ProfileWindow(SettingsWindow):
    finished = Signal(str)

    def __init__(self, bridge: MaintenanceBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self._request: str | None = None
        self._generation = 0
        self._closing = False
        self._pending_close = False
        self._action = "quit"
        self.startup_notice = ""
        self.setWindowTitle("Deskpal · 角色档案")
        root = QWidget()
        layout = QVBoxLayout(root)
        self.status = QLabel("角色后台已关闭。请选择要打开的完整档案。")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        scope = QLabel("每个档案独立保存聊天、记忆、安排与受管图片。模型连接、密钥、工作区与模型缓存共用。")
        scope.setWordWrap(True)
        layout.addWidget(scope)
        self.profiles = QListWidget()
        self.profiles.currentRowChanged.connect(self._controls)
        layout.addWidget(self.profiles, 1)
        self.action_buttons: dict[str, QPushButton] = {}
        for entries in (
            (("refresh", "刷新", self.refresh), ("create", "新建", self._create),
             ("rename", "改名", self._rename), ("delete", "删除", self._delete)),
            (("backup", "备份所选档案", self._backup), ("restore", "导入备份为新档案", self._restore)),
            (("start", "打开所选档案", self._start),
             ("maintenance", "共用配置与维护", lambda: self._shutdown("maintenance")),
             ("quit", "退出", self.close)),
        ):
            row = QHBoxLayout()
            for key, title, callback in entries:
                button = QPushButton(title)
                button.clicked.connect(callback)
                row.addWidget(button)
                self.action_buttons[key] = button
            layout.addLayout(row)
        self.set_settings_content(root)
        bridge.reply.connect(self._reply)
        bridge.stopped.connect(self._stopped)
        self._controls()

    def open(self) -> None:
        self.show()
        self.refresh()

    def _selected(self) -> str | None:
        item = self.profiles.currentItem()
        identity = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return identity if isinstance(identity, str) else None

    def _controls(self, *_: Any) -> None:
        available = not self._closing and self._request is None
        for key, button in self.action_buttons.items():
            button.setEnabled(available and (key not in {"rename", "delete", "backup", "start"}
                                             or self._selected() is not None))
        self.profiles.setEnabled(available)

    def _send(self, operation: str, parameters: dict[str, Any] | None = None) -> None:
        if self._closing or self._request is not None:
            return
        try:
            self._request = self.bridge.send(operation, parameters, generation=self._generation)
            self.status.setText("正在处理档案，请等待完成…")
        except (RuntimeError, ValueError) as exc:
            self.status.setText(str(exc))
        self._controls()

    def refresh(self) -> None:
        self._send("profile_list")

    def _create(self) -> None:
        name, ok = QInputDialog.getText(self, "新建角色档案", "档案名称")
        if ok and name.strip():
            self._send("profile_create", {"name": name.strip()})

    def _rename(self) -> None:
        identity = self._selected()
        if identity is None:
            return
        name, ok = QInputDialog.getText(self, "修改显示名称", "档案名称")
        if ok and name.strip():
            self._send("profile_rename", {"profile_id": identity, "name": name.strip()})

    def _delete(self) -> None:
        identity = self._selected()
        if identity is None:
            return
        name = self.profiles.currentItem().text()
        answer = QMessageBox.question(self, "删除完整角色档案", f"永久删除“{name}”的聊天、记忆、安排、执行记录和受管图片？此操作无法撤销。",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self._send("profile_delete", {"profile_id": identity})

    def _backup(self) -> None:
        identity = self._selected()
        if identity is None:
            return
        destination, _ = QFileDialog.getSaveFileName(self, "保存完整档案备份", "", "ZIP 备份 (*.zip)")
        if destination:
            self._send("profile_backup", {"profile_id": identity, "destination": destination})

    def _restore(self) -> None:
        archive, _ = QFileDialog.getOpenFileName(self, "导入完整档案备份", "", "ZIP 备份 (*.zip)")
        if not archive:
            return
        name, ok = QInputDialog.getText(self, "导入为新档案", "新档案名称（备份中的历史数据会一并恢复）")
        if ok and name.strip():
            self._send("profile_restore", {"archive": archive, "name": name.strip()})

    def _start(self) -> None:
        identity = self._selected()
        if identity is not None:
            self._send("profile_select", {"profile_id": identity})

    @Slot(object)
    def _reply(self, envelope: dict[str, Any]) -> None:
        if (self._closing or envelope.get("request_id") != self._request
                or envelope.get("generation") != self._generation):
            return
        self._request = None
        if self._pending_close:
            self._shutdown("quit")
            return
        if not envelope.get("ok"):
            error = envelope.get("error", "未知错误")
            message = error.get("message", "未知错误") if isinstance(error, dict) else str(error)
            self.status.setText("档案操作失败：" + str(message))
            self._controls()
            return
        operation = envelope.get("operation")
        if operation == "profile_select":
            self._shutdown("start")
            return
        if operation == "profile_list":
            data = envelope.get("data", {})
            previous = self._selected()
            self.profiles.clear()
            chosen = None
            for entry in data.get("profiles", []):
                identity = str(entry["id"])
                item = QListWidgetItem(str(entry["name"]) + (" · 当前选择" if entry.get("selected") else ""))
                item.setData(Qt.ItemDataRole.UserRole, identity)
                self.profiles.addItem(item)
                if identity == (previous or data.get("selected_id")):
                    chosen = item
            if chosen is not None:
                self.profiles.setCurrentItem(chosen)
            self.status.setText(self.startup_notice or "请选择要打开的档案，或新建、导入档案。")
        elif operation == "profile_backup":
            self.status.setText("完整档案备份已保存。请妥善保管其中的私人数据。")
        else:
            self.refresh()
        self._controls()

    def _shutdown(self, action: str) -> None:
        if self._closing or self._request is not None:
            return
        self._closing = True
        self._generation += 1
        self._action = action
        self._controls()
        self.status.setText("等待档案操作后台安全退出…")
        self.bridge.shutdown()

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        # An active file operation must settle; do not cancel a half-written archive.
        if self._request is not None:
            self._pending_close = True
            self.status.setText("档案操作仍在进行，请等待完成后退出。")
        self._shutdown("quit")

    @Slot()
    def _stopped(self) -> None:
        self.hide()
        self.finished.emit(self._action)
