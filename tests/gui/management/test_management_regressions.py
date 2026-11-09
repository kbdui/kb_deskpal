"""Management UI behavior and isolated native-event crash regressions."""

from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys

from PySide6.QtCore import QObject, QTimer, Signal, Qt

from deskpal.gui.catalog import CatalogPage
from deskpal.gui.components import CheckBox
from deskpal.gui.management import MemoryPage
from deskpal.gui.plugin_panel import PluginPanel
from deskpal.gui.views import PagedView


class Bridge(QObject):
    reply = Signal(object)
    state_changed = Signal(str)
    privacy_changed = Signal(int)

    def __init__(self):
        super().__init__()
        self.state = "Ready"; self.privacy_epoch = 0; self.sent = []

    def send(self, operation, parameters=None, *, generation=0):
        request = {"request_id": str(len(self.sent) + 1), "operation": operation,
                   "generation": generation, "privacy_epoch": self.privacy_epoch,
                   "parameters": deepcopy(parameters or {})}
        self.sent.append(request)
        return request["request_id"]

    def requests(self, operation):
        return [request for request in self.sent if request["operation"] == operation]

    def answer(self, qtbot, page, request, data):
        self.reply.emit({**request, "ok": True, "data": data})
        qtbot.waitUntil(lambda: request["request_id"] not in page.channel.pending)


def memory(content="喜欢清楚的中文说明"):
    return {"id": "memory", "revision": 3, "status": "candidate",
            "draft": {"content": content, "quote": "这是明确的原文依据"},
            "source": {"content": "完整来源原文", "origin": "user_message", "use_policy": "local_only"}}


def test_plugin_catalog_opens_host_page_and_saves_declared_option(qtbot):
    bridge = Bridge()
    page = CatalogPage(bridge)
    qtbot.addWidget(page)
    page.show()
    page.refresh_plugins()
    bridge.answer(qtbot, page, bridge.requests("plugins_catalog")[-1], {"items": [
        {"id": "schedule", "name": "日程与计时器", "page_title": "日程与计时器",
         "tool_ids": ["timer_list"], "event_sources": ["timer_due"]},
    ]})
    page.tabs.setCurrentIndex(1)
    page.plugins.list.setCurrentRow(0)
    assert page.open_plugin.isVisible()
    page.open_plugin.click()
    panel = page.findChild(PluginPanel)
    assert panel is not None
    data = {"plugin_id": "schedule", "switches": [
        {"key": "timer_only", "label": "仅显示计时器到期记录", "value": False, "revision": 0},
    ], "rows": [{"title": "计时样例"}]}
    bridge.answer(qtbot, panel, bridge.requests("plugin_page")[-1], data)
    panel.findChild(CheckBox).click()
    command = bridge.requests("plugin_set_option")[-1]
    assert command["parameters"] == {"plugin_id": "schedule", "key": "timer_only",
                                      "value": True, "expected_revision": 0}
    bridge.answer(qtbot, panel, command, {"revision": 1})
    assert len(bridge.requests("plugin_page")) == 2
    page.clear_sensitive()
    assert not panel.isVisible()


def test_wheel_events_use_native_scrollbar_route_in_isolated_process():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    probe = Path(__file__).resolve().parents[2] / 'manual' / 'management_wheel_probe.py'
    result = subprocess.run([sys.executable, "-X", "faulthandler", str(probe)],
                            capture_output=True, text=True, timeout=25, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"controls_unchanged": true' in result.stdout
    assert '"popup_scrolls_without_selection": true' in result.stdout


def test_memory_body_metadata_and_raw_record_are_separate(qtbot):
    bridge = Bridge(); page = MemoryPage(bridge); qtbot.addWidget(page); page.show()
    item = memory("首行\n" + "可复制正文" * 400)
    page.page.set_page({"items": [item]}); page.page._select_memory_id(item["id"])
    assert page.page.summary_body.toPlainText() == item["draft"]["content"]
    assert "待复核理解" in page.page.summary_meta.text()
    assert "仅本地" in page.page.summary_meta.text()
    assert "用户明确输入" in page.page.summary_meta.text()
    assert "完整来源原文" in page.page.summary_context.toPlainText()
    assert page.page.detail.isHidden()
    page.page.technical.click()
    assert not page.page.detail.isHidden()
    page.clear_sensitive()
    assert not page.page.summary_body.toPlainText()
    assert not page.page.summary_context.toPlainText()
    assert page.page.detail.topLevelItemCount() == 0


def test_memory_refresh_coalesces_and_discards_obsolete_filter(qtbot):
    bridge = Bridge(); page = MemoryPage(bridge); qtbot.addWidget(page)
    page.refresh()
    first = bridge.requests("memories")[-1]
    for value in ('episode', 'all', 'buffer'):
        page.filter.setCurrentIndex(page.filter.findData(value))
        page.refresh()
    assert len(bridge.requests("memories")) == 1
    assert len(page.channel.pending) == 1
    assert "正在读取" in page.page.status_label.text()
    bridge.answer(qtbot, page, first, {"items": [memory("obsolete body")]})
    latest = bridge.requests("memories")[-1]
    assert latest["parameters"]['group'] == 'buffer'
    assert not page.page.items
    bridge.answer(qtbot, page, latest, {"items": []})
    assert "没有记录" in page.page.status_label.text()


def test_catalog_discards_detail_for_old_selection_and_reads_latest_search(qtbot):
    bridge = Bridge(); page = CatalogPage(bridge); qtbot.addWidget(page)
    page._catalog({"items": [{"id": "one", "display_name": "能力一"},
                            {"id": "two", "display_name": "能力二"}], "catalog_revision": "r"})
    page.tools._select_memory_id("one")
    old = bridge.requests("tool_detail")[-1]
    page.tools._select_memory_id("two")
    bridge.answer(qtbot, page, old, {"item": {"id": "one", "summary": "过期说明"}})
    assert "过期说明" not in page.tools.summary_body.toPlainText()
    new = bridge.requests("tool_detail")[-1]
    bridge.answer(qtbot, page, new, {"item": {"id": "two", "display_name": "能力二", "summary": "当前中文说明"}})
    assert page.tools.summary_body.toPlainText() == "当前中文说明"
    page.refresh(); old_catalog = bridge.requests("tools_catalog")[-1]
    for query in ("first", "second", "final"):
        page.query.setText(query); page.refresh()
    assert len(bridge.requests("tools_catalog")) == 1
    bridge.answer(qtbot, page, old_catalog, {"items": [{"id": "obsolete"}]})
    assert bridge.requests("tools_catalog")[-1]["parameters"]["q"] == "final"
    assert all(item["id"] != "obsolete" for item in page.tools.items)


def test_plain_navigation_and_deferred_splitter_layout_settle(qtbot):
    bridge = Bridge(); memory_page = MemoryPage(bridge); catalog = CatalogPage(bridge)
    qtbot.addWidget(memory_page); qtbot.addWidget(catalog)
    for page in (memory_page, catalog):
        page.resize(720, 640); page.show(); page.refresh()
    # Each step traverses actual posted layout and timer events, without wheels.
    count = []
    def navigate():
        index = len(count); page = (memory_page, catalog)[index % 2]
        page.show(); page.resize(720 if index % 3 else 960, 640)
        other = (catalog, memory_page)[index % 2]; other.hide()
        count.append(index)
        if len(count) < 30: QTimer.singleShot(0, navigate)
    QTimer.singleShot(0, navigate)
    qtbot.waitUntil(lambda: len(count) == 30)
    qtbot.waitUntil(lambda: all(not view._orientation_pending for page in (memory_page, catalog)
                             for view in page.findChildren(PagedView)))
    memory_page.show(); memory_page.page.resize(700, 300)
    qtbot.waitUntil(lambda: memory_page.page.splitter.orientation() == Qt.Orientation.Vertical)


def test_page_failure_is_visible_and_preserves_loaded_records(qtbot):
    page = PagedView("memories"); qtbot.addWidget(page)
    page.set_page({"items": [memory()]}); page.loading()
    page.set_page({"_error": {"code": "Failure"}})
    assert "读取失败" in page.status_label.text()
    assert page.items[0]["id"] == "memory"
