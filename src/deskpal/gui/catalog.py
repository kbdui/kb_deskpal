"""Read-only capability catalog, driven exclusively by host metadata."""
from collections.abc import Callable
from typing import Any
from PySide6.QtWidgets import (QHBoxLayout, QVBoxLayout, QWidget)
from .components import (ChoiceBox as QComboBox, PushButton as QPushButton, LineEdit as QLineEdit, Tabs as QTabWidget)
from .channel import Channel
from .views import PagedView
from .widgets import ArrowComboBox, notice_label, text_label

class CatalogPage(QWidget):
    def __init__(self, bridge: Any, parent: Any = None) -> None:
        super().__init__(parent); self.channel = Channel(bridge, self); self.revision: Any = None; self.cursor: Any = None
        self._read_epoch = 0
        self._active: set[str] = set()
        self._latest: dict[str, tuple[dict[str, Any], Callable[[dict[str, Any]], None]]] = {}
        layout = QVBoxLayout(self); layout.addWidget(text_label("工具插件", heading=True)); layout.addWidget(text_label("这里展示应用实际注册的能力、使用条件与权限。配置统一在设置完成，查看目录不会执行工具。"))
        self.notice = notice_label(); self.channel.notice.connect(self.notice.setText); layout.addWidget(self.notice)
        row = QHBoxLayout(); self.query = QLineEdit(); self.query.setPlaceholderText("搜索名称或说明")
        self.category = ArrowComboBox()
        for title, key in [("全部分组", ""), ("记忆、角色与任务管理", "agent"), ("时间与调试", "general"), ("文件与文档", "files"), ("提醒与日程", "schedule"), ("截图", "screen")]: self.category.addItem(title, key)
        self.origin = ArrowComboBox(); self.origin.addItem("全部来源", ""); self.origin.addItem("内置", "builtin")
        search = QPushButton("搜索"); row.addWidget(self.query, 1); row.addWidget(self.category); row.addWidget(self.origin); row.addWidget(search); layout.addLayout(row)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1); self.tools = PagedView("tools"); self.plugins = PagedView("plugins")
        self.tabs.addTab(self.tools, "能力目录"); self.tabs.addTab(self.plugins, "能力提供方")
        search.clicked.connect(lambda: self.refresh(False)); self.query.returnPressed.connect(lambda: self.refresh(False))
        self.tools.refresh_requested.connect(lambda: self.refresh(False)); self.plugins.refresh_requested.connect(self.refresh_plugins)
        self.tools.previous.clicked.disconnect(); self.tools.next.clicked.disconnect(); self.tools.previous.clicked.connect(lambda: self.refresh(False)); self.tools.next.clicked.connect(lambda: self.refresh(True))
        self.tools.selected.connect(self._tool); self.plugins.selected.connect(self.plugins.show_detail)
        if hasattr(bridge, "privacy_changed"): bridge.privacy_changed.connect(self.clear_sensitive)

    def refresh(self, more: bool = False) -> None:
        self.tools.loading()
        params = {"q": self.query.text(), "cursor": self.cursor if more else None, "limit": 50}
        if self.category.currentData(): params["category"] = self.category.currentData()
        if self.origin.currentData(): params["origin"] = self.origin.currentData()
        offset = self.tools.offset + 50 if more else 0
        self._read("tools_catalog", params, lambda data: self._catalog(data, offset)); self.refresh_plugins()

    def _catalog(self, data: dict[str, Any], offset: int = 0) -> None:
        if "_error" in data: self.tools.load_failed(); return
        if data.get("unchanged"): return
        self.revision, self.cursor = data.get("catalog_revision"), data.get("next_cursor")
        self.tools.set_page({**data, "offset": offset})

    def _tool(self, item: dict[str, Any]) -> None:
        if not item:
            self.tools.show_detail({}); return
        identity = item.get("id", item.get("name")); revision = self.revision
        def done(data: dict[str, Any]) -> None:
            selected = self.tools.selected_item()
            if ("_error" not in data and selected.get("id", selected.get("name")) == identity
                    and revision == self.revision): self.tools.show_detail(data)
        self._read("tool_detail", {"id": identity, "catalog_revision": revision}, done)

    def refresh_plugins(self) -> None:
        self.plugins.loading(); self._read("plugins_catalog", {}, self.plugins.set_page)

    def _read(self, operation: str, parameters: dict[str, Any], callback: Callable[[dict[str, Any]], None]) -> None:
        if str(getattr(self.channel.bridge, "state", "")) != "Ready": return
        if operation in self._active:
            self._latest[operation] = (parameters, callback); return
        self._active.add(operation)
        epoch = self._read_epoch
        def done(data: dict[str, Any]) -> None:
            if epoch != self._read_epoch: return
            self._active.discard(operation)
            latest = self._latest.pop(operation, None)
            if latest is not None: self._read(operation, *latest)
            else: callback(data)
        if self.channel.send(operation, parameters, done) is None:
            self._active.discard(operation); callback({"_error": {"code": "NotAccepted"}})

    def clear_sensitive(self, epoch: int = 0) -> None:
        self._read_epoch += 1; self._active.clear(); self._latest.clear(); self.channel.invalidate()
        self.tools.clear_sensitive(); self.plugins.clear_sensitive(); self.query.clear(); self.cursor = None; self.revision = None
