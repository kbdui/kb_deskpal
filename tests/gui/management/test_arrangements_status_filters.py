"""Real GUI/bridge/facade route for the initial arrangement status filter."""

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import QPushButton, QScrollArea

from deskpal.application.factory import create_application
from deskpal.config import load_config
from deskpal.gui.bridge import ApplicationBridge
from deskpal.gui.chat import ChatCoordinator
from deskpal.gui.management import ArrangementsPage, GoalsPage, MemoryPage
from deskpal.gui.task_card import TaskCard
from deskpal.tasks.models import TaskStatus


class Coordinator(QObject):
    task_changed = Signal(str)

    def __init__(self, bridge):
        super().__init__(); self.bridge = bridge; self.reviews = {}


def test_initial_and_active_task_filters_complete_through_real_bridge(qtbot, tmp_path):
    config = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
        "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "DESKPAL_DATA_DIR": str(tmp_path / "data"),
        "DESKPAL_MEMORY_EMBEDDING_PROVIDER": "disabled"})
    def factory():
        app = create_application(config)
        for state in (TaskStatus.CREATED, TaskStatus.WAITING, TaskStatus.NEEDS_REVIEW,
                      TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.CANCELLED):
            task = app.tasks.create("synthetic " + state.value)
            if state in {TaskStatus.WAITING, TaskStatus.RUNNING}: app.tasks.update_status(task.id, TaskStatus.PLANNING)
            if state != TaskStatus.CREATED: app.tasks.update_status(task.id, state)
        return app
    bridge = ApplicationBridge(factory); responses = []; bridge.reply.connect(responses.append)
    coordinator = Coordinator(bridge); page = ArrangementsPage(coordinator); qtbot.addWidget(page)
    bridge.start()
    try:
        qtbot.waitUntil(lambda: bridge.state == "Ready", timeout=5000)
        page.tabs.setCurrentIndex(1)
        page.task_filter.setCurrentIndex(page.task_filter.findData('Waiting'))
        page.refresh()
        qtbot.waitUntil(lambda: len(page.tasks.items) == 2, timeout=5000)
        assert {item["status"] for item in page.tasks.items} == {"Waiting", "NeedsReview"}
        page.task_filter.setCurrentIndex(page.task_filter.findData('Running'))
        qtbot.waitUntil(lambda: len(page.tasks.items) == 3, timeout=5000)
        assert {item["status"] for item in page.tasks.items} == {"Created", "Planning", "Running"}
        bad = bridge.send("tasks", {"statuses": ["not-a-state"]})
        qtbot.waitUntil(lambda: any(reply["request_id"] == bad for reply in responses), timeout=5000)
        rejected = next(reply for reply in responses if reply["request_id"] == bad)
        assert not rejected["ok"] and rejected["error"]["code"] == "Rejected"
        assert all(reply["ok"] for reply in responses if reply["operation"] == "tasks" and reply["request_id"] != bad)
    finally:
        bridge.shutdown(); qtbot.waitUntil(lambda: bridge.state == "Closed", timeout=5000)


def test_memory_layout_reserves_readable_body_and_correction_form(qtbot):
    class Bridge(QObject):
        reply = Signal(object); privacy_changed = Signal(int)
        state = "Ready"; privacy_epoch = 0
        def send(self, *args, **kwargs): return "synthetic"
    bridge = Bridge(); page = MemoryPage(bridge); qtbot.addWidget(page)
    page.resize(940, 760); page.show()
    item = {"id": "memory", "revision": 1, "status": "accepted",
            "draft": {"content": "中文正文\n" * 50, "quote": "原文依据"},
            "source": {"content": "来源", "use_policy": "local_only"}}
    page.page.set_page({"items": [item]}); page.page._select_memory_id(item['id'])
    qtbot.waitUntil(lambda: not page.page._orientation_pending)
    page._open_editor(0)
    qtbot.waitUntil(lambda: page.form_scroll.isVisible())
    assert page.form_scroll.height() >= 170
    assert page.page.summary_body.height() >= 100
    assert page.page.splitter.height() > 250
    assert page.page.status_label.height() <= page.page.status_label.fontMetrics().lineSpacing() * 2
    assert page.page.page_label.height() <= page.page.page_label.fontMetrics().lineSpacing() * 2


class LayoutBridge(QObject):
    reply = Signal(object); privacy_changed = Signal(int); state_changed = Signal(str)
    state = "Ready"; privacy_epoch = 0
    def __init__(self):
        super().__init__(); self.sequence = 0
    def send(self, *args, **kwargs):
        self.sequence += 1; return "layout-" + str(self.sequence)


def test_task_review_has_half_width_and_reachable_visible_authorization(qtbot):
    bridge = LayoutBridge(); coordinator = ChatCoordinator(bridge); coordinator.timer.stop()
    coordinator.reviews["task"] = {
        "task_id": "task", "status": "Waiting", "title": "审阅明确范围的写入",
        "plan_hash": "a" * 64, "task_revision": 1, "confirmation_revision": 1,
        "workspace": "synthetic/workspace", "confirmation_state": "none",
        "steps": [{"tool_name": "write_text", "display_name": "写入文本",
                   "arguments": {"path": "explicit-target.txt", "text": "完整实际参数"}}],
        "missing_permissions": {"ordinary_tools": ["write_text"], "high_risk_tools": []},
        "available_actions": ["confirm", "cancel"],
        "task": {"id": "task", "status": "Waiting", "revision": 1},
    }
    page = ArrangementsPage(coordinator); qtbot.addWidget(page)
    page.tabs.setCurrentIndex(1)
    page.resize(1100, 760); page.show()
    page.tasks.set_page({"items": [{"id": "task", "title": "审阅写入", "status": "Waiting"}]})
    page.tasks.list.setCurrentRow(0)
    qtbot.waitUntil(lambda: not page.tasks._orientation_pending)
    qtbot.waitUntil(lambda: page.tasks.splitter.orientation() == Qt.Orientation.Horizontal)
    assert page.tasks.detail_panel.isHidden()
    card = page.card_host.findChild(TaskCard)
    assert card is not None
    allow = next(button for button in card.findChildren(QPushButton) if button.text() == "允许上述普通操作")
    assert page.card_area.viewport().width() >= page.tasks.splitter.width() * 0.5
    page.card_area.ensureWidgetVisible(allow)
    qtbot.waitUntil(lambda: page.card_area.viewport().rect().intersects(
        allow.rect().translated(allow.mapTo(page.card_area.viewport(), allow.rect().topLeft()))))
    assert allow.isVisible() and allow.isEnabled()
    assert allow.width() >= allow.fontMetrics().horizontalAdvance(allow.text())


def test_goal_forms_share_the_splitter_instead_of_becoming_a_sliver(qtbot):
    page = GoalsPage(LayoutBridge()); qtbot.addWidget(page); page.resize(1100, 760); page.show()
    for index, name in enumerate(("goal", "milestone", "binding")):
        page.tabs.setCurrentIndex(index)
        view = page.pages[name]
        qtbot.waitUntil(lambda: not view._orientation_pending)
        qtbot.waitUntil(lambda: view.splitter.orientation() == Qt.Orientation.Horizontal)
        assert view.splitter.count() == 2
        page._selected(name, {'id': name, 'status': 'Active' if name in ('goal', 'binding') else 'Pending'})
        page.editor_tabs[name].setCurrentIndex(1)
        form_area = page.editor_hosts[name].widget(1)
        assert isinstance(form_area, QScrollArea)
        qtbot.waitUntil(lambda: form_area.isVisible())
        assert form_area.viewport().width() >= view.splitter.width() * 0.5
        form = page.binding_form if name == "binding" else page.forms[name]
        assert form.fields["status" if name == "binding" else "title"].isVisible()
        assert 'use_policy' not in form.fields
