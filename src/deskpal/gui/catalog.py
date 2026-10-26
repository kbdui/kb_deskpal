"""Read-only capability catalog, driven exclusively by host metadata."""
from typing import Any
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLineEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget
from .channel import Channel
from .views import PagedView
from .widgets import text_label

class CatalogPage(QWidget):
    def __init__(self, bridge: Any, parent: Any = None) -> None:
        super().__init__(parent); self.channel = Channel(bridge, self); self.revision: Any = None; self.cursor: Any = None
        layout = QVBoxLayout(self); layout.addWidget(text_label("工具插件", heading=True)); layout.addWidget(text_label("这里展示应用实际注册的能力、使用条件与权限。配置统一在设置完成，查看目录不会执行工具。"))
        self.notice = text_label(); self.channel.notice.connect(self.notice.setText); layout.addWidget(self.notice)
        row = QHBoxLayout(); self.query = QLineEdit(); self.query.setPlaceholderText("搜索名称或说明")
        self.category = QComboBox()
        for title, key in [("全部分组", ""), ("时间与调试", "general"), ("文件与文档", "files"), ("提醒与日程", "schedule"), ("截图", "screen")]: self.category.addItem(title, key)
        self.origin = QComboBox(); self.origin.addItem("全部来源", ""); self.origin.addItem("内置", "builtin")
        search = QPushButton("搜索"); row.addWidget(self.query, 1); row.addWidget(self.category); row.addWidget(self.origin); row.addWidget(search); layout.addLayout(row)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1); self.tools = PagedView("tools"); self.plugins = PagedView("plugins")
        self.tabs.addTab(self.tools, "能力目录"); self.tabs.addTab(self.plugins, "能力提供方")
        search.clicked.connect(lambda: self.refresh(False)); self.query.returnPressed.connect(lambda: self.refresh(False))
        self.tools.refresh_requested.connect(lambda: self.refresh(False)); self.plugins.refresh_requested.connect(self.refresh_plugins)
        self.tools.previous.clicked.disconnect(); self.tools.next.clicked.disconnect(); self.tools.previous.clicked.connect(lambda: self.refresh(False)); self.tools.next.clicked.connect(lambda: self.refresh(True))
        self.tools.selected.connect(self._tool); self.plugins.selected.connect(self.plugins.show_detail)
        if hasattr(bridge, "privacy_changed"): bridge.privacy_changed.connect(self.clear_sensitive)

    def refresh(self, more: bool = False) -> None:
        params = {"q": self.query.text(), "cursor": self.cursor if more else None, "limit": 50}
        if self.category.currentData(): params["category"] = self.category.currentData()
        if self.origin.currentData(): params["origin"] = self.origin.currentData()
        self.channel.send("tools_catalog", params, self._catalog); self.refresh_plugins()

    def _catalog(self, data: dict[str, Any]) -> None:
        if "_error" in data or data.get("unchanged"): return
        self.revision, self.cursor = data.get("catalog_revision"), data.get("next_cursor"); self.tools.set_page(data)

    def _tool(self, item: dict[str, Any]) -> None:
        if item: self.channel.send("tool_detail", {"id": item.get("id", item.get("name")), "catalog_revision": self.revision}, lambda data: self.tools.show_detail(data) if "_error" not in data else None)

    def refresh_plugins(self) -> None:
        self.channel.send("plugins_catalog", {}, self.plugins.set_page)

    def clear_sensitive(self, epoch: int = 0) -> None:
        self.tools.clear_sensitive(); self.plugins.clear_sensitive(); self.query.clear(); self.cursor = None; self.revision = None
