"""Host rendering of the supported plugin page controls."""

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget

from .channel import Channel
from .components import CheckBox, PushButton
from .display_text import friendly_time
from .widgets import notice_label, scroll, text_label


class PluginPanel(QDialog):
    def __init__(self, bridge: Any, plugin_id: str, title: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(title)
        self.resize(560, 480)
        self.plugin_id = plugin_id
        self.channel = Channel(bridge, self)
        layout = QVBoxLayout(self)
        layout.addWidget(text_label(title, heading=True))
        self.notice = notice_label()
        self.channel.notice.connect(self.notice.setText)
        layout.addWidget(self.notice)
        self.reload_button = PushButton("刷新")
        self.reload_button.clicked.connect(self.refresh)
        layout.addWidget(self.reload_button)
        self.content = QWidget()
        self.body = QVBoxLayout(self.content)
        layout.addWidget(scroll(self.content), 1)

    def refresh(self) -> None:
        self.content.setEnabled(False)
        self.reload_button.setEnabled(False)
        if self.channel.send("plugin_page", {"plugin_id": self.plugin_id}, self._show_page) is None:
            self._show_page({"_error": True})

    def _show_page(self, data: dict[str, Any]) -> None:
        self.reload_button.setEnabled(True)
        if "_error" in data or data.get("plugin_id") != self.plugin_id:
            self.notice.setText("读取插件页面失败，请刷新后重试。")
            return
        while self.body.count():
            item = self.body.takeAt(0)
            if item is not None and item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        for option in data.get("switches", []):
            control = CheckBox(option["label"])
            control.setChecked(option["value"])
            control.toggled.connect(lambda checked, row=option: self._save(row, checked))
            self.body.addWidget(control)
        rows = data.get("rows", [])
        if not rows:
            self.body.addWidget(text_label("还没有符合条件的记录。"))
        for row in rows:
            parts = [row["title"]]
            if row.get("occurred_at"):
                parts.append(friendly_time(row["occurred_at"]))
            if row.get("text"):
                parts.append(row["text"])
            self.body.addWidget(text_label(" · ".join(parts)))
        self.body.addStretch()
        self.content.setEnabled(True)

    def _save(self, option: dict[str, Any], checked: bool) -> None:
        self.notice.clear()
        self.content.setEnabled(False)
        self.reload_button.setEnabled(False)
        args = dict(plugin_id=self.plugin_id, key=option["key"], value=checked,
                    expected_revision=option["revision"])
        if self.channel.send("plugin_set_option", args, lambda _: self.refresh()) is None:
            self.refresh()
