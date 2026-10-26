"""Prepared GUI acceptance against the five-page shell; not executed in D1–D5."""

from copy import deepcopy
from threading import Thread
from typing import Any

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QObject, QPoint, QRect, QSettings, QSize, QThread, Signal, Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QTreeWidget

from deskpal.gui.channel import Channel
from deskpal.gui.preferences import GuiPreferences
from deskpal.gui.task_card import TaskCard
from deskpal.gui.window import MainWindow
from deskpal.gui.views import CommandForm, PagedView
from deskpal.gui.widgets import fill_tree


class FakeBridge(QObject):
    state_changed = Signal(str)
    reply = Signal(object)
    privacy_changed = Signal(int)
    image_reply = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.state = "Initializing"
        self.privacy_epoch = 0
        self.sent: list[dict[str, Any]] = []
        self.shutdown_count = 0
        self.start_count = 0

    def set_state(self, state: str) -> None:
        self.state = state
        self.state_changed.emit(state)

    def send(self, operation: str, parameters: dict[str, Any] | None = None, *, generation: int = 0) -> str:
        if operation in {"request_forget", "conversation_clear", "conversation_delete"} or (operation == "memory" and (
                parameters or {}).get("action") in {"forget", "correct", "accept", "reject"}):
            self.privacy_epoch += 1
            self.privacy_changed.emit(self.privacy_epoch)
        request_id = "request-" + str(len(self.sent) + 1)
        self.sent.append({"request_id": request_id, "operation": operation,
                          "parameters": deepcopy(parameters or {}), "generation": generation,
                          "privacy_epoch": self.privacy_epoch})
        return request_id

    def send_image(self, operation: str, parameters: dict[str, Any], generation: int = 0) -> str:
        return self.send(operation, parameters, generation=generation)

    def start(self) -> None:
        self.start_count += 1

    def shutdown(self) -> None:
        self.shutdown_count += 1

    def requests_for(self, operation: str) -> list[dict[str, Any]]:
        return [item for item in self.sent if item["operation"] == operation]

    def answer(self, request: dict[str, Any], data: dict[str, Any] | None = None,
               *, error: str | None = None) -> None:
        response = {key: request[key] for key in ("request_id", "operation", "generation", "privacy_epoch")}
        response.update(ok=error is None, data=data or {}, error={"code": error, "message": "synthetic failure"})
        self.reply.emit(response)


def pending(widget: MainWindow, identity: str) -> bool:
    return any(identity in channel.pending for channel in widget.findChildren(Channel))


def deliver(qtbot: Any, widget: MainWindow, bridge: FakeBridge,
            request: dict[str, Any], data: dict[str, Any] | None = None, error: str | None = None) -> None:
    bridge.answer(request, data, error=error)
    qtbot.waitUntil(lambda: not pending(widget, request["request_id"]))


@pytest.fixture
def window(qtbot: Any) -> tuple[MainWindow, FakeBridge]:
    bridge = FakeBridge()
    widget = MainWindow(bridge)
    qtbot.addWidget(widget)
    bridge.set_state("Ready")
    qtbot.waitUntil(lambda: widget.lifecycle == "Ready")
    widget.poll_timer.stop()
    state = widget.chat_coordinator
    state.timer.stop()
    daily = {"id": "daily", "title": "日常聊天", "kind": "default", "revision": 1, "context_epoch": 1}
    boot = list(bridge.sent)
    for request in boot:
        op = request["operation"]
        data = ({"llm_ready": True} if op == "settings" else daily if op == "conversation_ensure_default"
                else {"items": [daily]} if op == "conversation_list" else {"profiles": []})
        deliver(qtbot, widget, bridge, request, data)
    state.default_id = "daily"
    state.conversations["daily"] = daily
    state.model_ready = True
    widget.chat_view.set_conversation("daily")
    widget.memory_page._settings({"request_use_default": False})
    return widget, bridge


def task_review(task_id: str = "task-1", status: str = "Waiting", revision: int = 1) -> dict[str, Any]:
    return {"task_id": task_id, "task_revision": revision, "confirmation_revision": revision,
            "title": "整理文件", "status": status, "conversation_id": "daily",
            "plan_hash": "a" * 64, "workspace": "synthetic/workspace", "confirmation_state": "none",
            "steps": [{"tool_name": "read", "display_name": "读取文件", "arguments": {"path": "full synthetic parameters"}},
                      {"tool_name": "delete", "display_name": "删除文件",
                       "arguments": {"path": "explicit high-risk target", "text": "<img src='https://invalid.test/private'>"}}],
            "missing_permissions": {"ordinary_tools": ["read"], "high_risk_tools": ["delete"]},
            "available_actions": ["confirm", "cancel"] if status == "Waiting" else [],
            "task": {"id": task_id, "status": status, "revision": revision, "result": None},
            "attempt_summary": {"count": 0}}


def memory_item() -> dict[str, Any]:
    return {"id": "memory-1", "revision": 3, "status": "candidate",
            "draft": {"content": "synthetic sensitive body", "quote": "synthetic quote"},
            "source": {"content": "synthetic source", "origin": "user_message", "use_policy": "local_only"}}


def show_task(qtbot: Any, widget: MainWindow, bridge: FakeBridge,
              review: dict[str, Any] | None = None) -> TaskCard:
    value = review or task_review()
    widget.set_page(1)
    widget.arrangements._task({"id": value["task_id"], "status": value["status"]})
    deliver(qtbot, widget, bridge, bridge.requests_for("task_review")[-1], value)
    return widget.arrangements.card_host.findChildren(TaskCard)[-1]


def text_in_tree(tree: QTreeWidget) -> str:
    def values(item: Any) -> list[str]:
        return [item.text(0), item.text(1), *(text for i in range(item.childCount()) for text in values(item.child(i)))]
    return "\n".join(text for i in range(tree.topLevelItemCount()) for text in values(tree.topLevelItem(i)))


def button_with_text(widget: Any, title: str) -> QPushButton:
    return next(button for button in widget.findChildren(QPushButton) if button.text() == title)


def test_initialization_and_close_use_host_lifecycle(qtbot: Any) -> None:
    bridge = FakeBridge()
    widget = MainWindow(bridge)
    qtbot.addWidget(widget)
    assert bridge.start_count == 0 and not widget.chat_view.send.isEnabled()
    widget.settings_page.read_diagnostics()
    assert bridge.sent[-1]["operation"] == "diagnostics"
    with qtbot.waitSignal(widget.close_requested):
        widget.close()
    with qtbot.waitSignal(widget.close_requested):
        widget.request_exit()
    assert bridge.shutdown_count == 0  # Only DesktopHost owns bridge shutdown.
    bridge.set_state("Closing")
    qtbot.waitUntil(lambda: widget.lifecycle == "Closing")
    assert not widget.chat_view.send.isEnabled()
    bridge.set_state("Closed")
    qtbot.waitUntil(lambda: widget.lifecycle == "Closed")
    assert widget._allow_close


def test_full_plan_plain_text_exact_hash_and_complete_split_grants(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    card = show_task(qtbot, widget, bridge)
    text = "\n".join(text_in_tree(tree) for tree in card.findChildren(QTreeWidget))
    assert "full synthetic parameters" in text and "explicit high-risk target" in text
    assert "<img src=" in text
    checks = card.findChildren(QCheckBox)
    assert len(checks) == 1 and not checks[0].isChecked()
    assert not button_with_text(card, "允许上述普通操作").isEnabled()
    checks[0].setChecked(True)
    confirm = button_with_text(card, "允许上述普通操作")
    confirm.click()
    widget.chat_coordinator.confirm("task-1")
    sent = bridge.requests_for("confirm")
    assert len(sent) == 1
    assert sent[0]["parameters"] == {"task_id": "task-1", "plan_hash": "a" * 64,
        "expected_revision": 1, "ordinary_tools": ["read"], "high_risk_tools": ["delete"],
        "command_id": sent[0]["parameters"]["command_id"]}
    assert not button_with_text(card, "允许上述普通操作").isEnabled()


def test_old_review_and_page_navigation_cannot_restore_stale_authority(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    state = widget.chat_coordinator
    state.set_review("task-1", task_review(revision=2))
    state.set_grant("task-1", "delete", True)
    newer = task_review(revision=3)
    newer["plan_hash"] = "b" * 64
    state.set_review("task-1", newer)
    state.set_review("task-1", task_review(revision=2))
    assert state.reviews["task-1"]["plan_hash"] == "b" * 64
    assert not state.grants.get("task-1")
    widget.set_page(4)
    widget.set_page(0)
    assert not state.grants.get("task-1")
    assert not bridge.requests_for("confirm")


@pytest.mark.parametrize("operation", ["confirm", "cancel"])
def test_uncertain_task_receipt_is_checked_before_any_replay(window: Any, qtbot: Any, operation: str) -> None:
    widget, bridge = window
    show_task(qtbot, widget, bridge)
    state = widget.chat_coordinator
    state.set_grant("task-1", "delete", True)
    state.confirm("task-1") if operation == "confirm" else state.task_action("task-1", operation)
    command = bridge.requests_for(operation)[-1]
    deliver(qtbot, widget, bridge, command, error="Busy")
    state.confirm("task-1") if operation == "confirm" else state.task_action("task-1", operation)
    assert len(bridge.requests_for(operation)) == 1
    assert "task-1" in state.task_pending
    state.poll()  # The fixture stops the timer; drive its normal reconciliation tick.
    lookup = bridge.requests_for("command_status")[-1]
    assert lookup["parameters"]["command_id"] == command["parameters"]["command_id"]
    state.confirm("task-1") if operation == "confirm" else state.task_action("task-1", operation)
    assert len(bridge.requests_for(operation)) == 1 and "task-1" in state.task_pending
    deliver(qtbot, widget, bridge, lookup, {"known": False})
    state.confirm("task-1") if operation == "confirm" else state.task_action("task-1", operation)
    assert len(bridge.requests_for(operation)) == 2
    assert bridge.requests_for(operation)[-1]["parameters"]["command_id"] == command["parameters"]["command_id"]


@pytest.mark.parametrize("status", ["Running", "Completed", "Failed", "Cancelled"])
def test_terminal_or_running_review_replaces_old_approval(window: Any, qtbot: Any, status: str) -> None:
    widget, bridge = window
    card = show_task(qtbot, widget, bridge)
    widget.chat_coordinator.set_review("task-1", task_review(status=status, revision=2))
    assert not [button for button in card.findChildren(QPushButton) if button.text() == "允许上述普通操作" and button.isEnabled()]
    assert not widget.chat_coordinator.grants.get("task-1")


@pytest.mark.parametrize("name", ["tasks", "memories", "proposals"])
def test_page_refresh_rechecks_changed_and_missing_selection(window: Any, name: str) -> None:
    widget, _ = window
    page = {"tasks": widget.arrangements.tasks, "memories": widget.memory_page.page,
            "proposals": widget.arrangements.proposals}[name]
    observed: list[dict[str, Any]] = []
    page.selected.connect(observed.append)
    page.set_page({"items": [{"id": "one", "revision": 1, "title": "可读名称"}]})
    page.list.setCurrentRow(0)
    page.set_page({"items": [{"id": "one", "revision": 2, "title": "新名称"}]})
    assert observed[-1]["revision"] == 2
    page.set_page({"items": []})
    assert observed[-1] == {} and not page.selected_item() and page.detail.topLevelItemCount() == 0


def test_selecting_goal_without_deadline_clears_previous_value(window: Any) -> None:
    widget, _ = window
    goals = widget.arrangements.goals
    goals._selected("goal", {"id": "goal-1", "revision": 1, "deadline": "2027-01-01T00:00:00+00:00"})
    assert goals.forms["goal"].values()["deadline"]
    goals._selected("goal", {"id": "goal-2", "revision": 2, "deadline": None})
    assert goals.forms["goal"].values()["deadline"] == ""
    assert goals.snapshots["goal"]["revision"] == 2


def test_only_chat_page_owns_conversation_sidebar(window: Any, qtbot: Any) -> None:
    widget, _ = window
    widget.resize(1120, 760)
    widget.show()
    widget.set_page(0)
    assert widget.sidebar.isVisible()
    for index in range(1, 5):
        widget.set_page(index)
        assert not widget.sidebar.isVisible()
    assert widget.stack.count() == 5
    from deskpal.gui.chat import ChatView
    assert widget.findChildren(ChatView) == [widget.chat_view]


def test_large_font_settings_scroll_without_forcing_window_off_screen(window: Any, qtbot: Any) -> None:
    widget, _ = window
    font = widget.font()
    font.setPointSize(18)
    widget.setFont(font)
    widget.show()
    widget.resize(1100, 1500)
    widget._queue_geometry_fit()
    available = widget.screen().availableGeometry()
    qtbot.waitUntil(lambda: available.contains(widget.frameGeometry()))
    widget.set_page(4)
    for index in range(widget.settings_page.tabs.count()):
        widget.settings_page.tabs.setCurrentIndex(index)
        area = widget.settings_page.tabs.widget(index)
        assert isinstance(area, QScrollArea)
        assert area.widgetResizable()
    assert widget.minimumSizeHint().height() < available.height()


def test_forget_clears_sensitive_views_and_drops_queued_old_body(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    state = widget.chat_coordinator
    state.review("old")
    old = bridge.requests_for("task_review")[-1]
    bridge.answer(old, task_review("old"))
    widget.chat_view.input.setPlainText("synthetic private draft")
    state.timelines["daily"] = [{"id": "m", "sequence": 1, "kind": "assistant_message", "body": "private history"}]
    state.timeline_changed.emit("daily")
    widget.memory_page._selected(memory_item())
    widget.memory_page.action("forget")
    qtbot.waitUntil(lambda: not state.drafts and not state.reviews)
    assert not widget.chat_view.input.toPlainText()
    assert not widget.memory_page.page.items
    assert widget.memory_page.page.detail.topLevelItemCount() == 0
    assert not state.timelines
    request = bridge.requests_for("request_forget")[-1]
    assert set(request["parameters"]) == {"memory_id", "expected_revision", "command_id"}
    assert request["privacy_epoch"] == 1 and old["privacy_epoch"] == 0


@pytest.mark.parametrize("code", ["Busy", "Conflict", "Rejected", "QueueFull"])
def test_failed_memory_mutation_keeps_no_stale_selection_and_allows_explicit_read(window: Any, qtbot: Any, code: str) -> None:
    widget, bridge = window
    widget.memory_page._selected(memory_item())
    widget.memory_page.action("forget")
    deliver(qtbot, widget, bridge, bridge.requests_for("request_forget")[-1], error=code)
    assert not widget.memory_page._snapshot
    assert not widget.memory_page._pending
    widget.memory_page.refresh()
    assert bridge.requests_for("memories")[-1]["parameters"] == {"limit": 50, "offset": 0}


def test_memory_source_policy_and_nested_draft_correction(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    widget.memory_page._selected(memory_item())
    text = text_in_tree(widget.memory_page.page.detail)
    assert "user_message" in text and "仅本地" in text
    assert widget.memory_page.correct.values()["content"] == "synthetic sensitive body"
    widget.memory_page.action("correct", {"content": "synthetic corrected body"})
    params = bridge.requests_for("memory")[-1]["parameters"]
    assert params["action"] == "correct" and params["expected_revision"] == 3
    assert params["content"] == "synthetic corrected body"


def test_page_errors_preserve_loaded_records_and_channel_has_bounded_capacity(window: Any) -> None:
    widget, bridge = window
    page = widget.arrangements.tasks
    page.set_page({"items": [{"id": "one", "title": "记录"}], "has_more": True})
    page.set_page({"_error": {"message": "read failed"}})
    assert page.items[0]["id"] == "one"
    page.next.click()
    assert bridge.requests_for("tasks")[-1]["parameters"]["offset"] == 50
    channel = Channel(bridge)
    for index in range(30):
        channel.send("tasks", {"offset": index})
    assert len(channel.pending) == 12


def test_notification_navigation_only_requeries(window: Any) -> None:
    widget, bridge = window
    widget.navigate("task", "task-notified")
    assert bridge.requests_for("task_review")[-1]["parameters"] == {"task_id": "task-notified"}
    widget.navigate("proposal", "proposal-notified")
    assert bridge.requests_for("proposal")[-1]["parameters"] == {"proposal_id": "proposal-notified"}
    assert not any(item["operation"] in {"confirm", "resume", "accept_proposal"} for item in bridge.sent)


def test_goal_milestone_binding_forms_use_real_cas_and_stable_source_ids(window: Any) -> None:
    widget, bridge = window
    goals = widget.arrangements.goals
    goal = {"id": "goal-1", "revision": 9, "title": "目标", "status": "Active"}
    goals._goals({"items": [goal]})
    goals._selected("goal", goal)
    set_form(goals.forms["goal"], status="Completed", evidence="人工签收")
    goals._status("goal")
    assert bridge.requests_for("goal_status")[-1]["parameters"]["expected_revision"] == 9
    goals._save("milestone", {"title": "步骤", "success_criteria": "标准", "deadline": ""}, True)
    assert bridge.requests_for("create_milestone")[-1]["parameters"]["goal_id"] == "goal-1"
    goals._sources_schedule({"items": [{"source": "timer", "source_id": "timer-1", "title": "同名"}], "has_more": True})
    goals._sources_tasks({"items": [{"id": "task-1", "title": "同名"}]})
    assert goals.source.currentData()["source_id"] == "timer-1"
    goals._sources_schedule({"items": [{"source": "timer", "source_id": "timer-2", "title": "同名"}]})
    assert goals.source.currentData()["source_id"] == "timer-1"
    goals._bind({"use_policy": "local_only"})
    assert bridge.requests_for("bind")[-1]["parameters"]["source_id"] == "timer-1"
    goals.refresh()
    assert bridge.requests_for("schedule_items")
    assert not bridge.requests_for("reminder_feed")


def test_gui_preferences_and_connection_metadata_stay_in_settings(qtbot: Any, tmp_path: Any) -> None:
    prefs = GuiPreferences(QSettings(str(tmp_path / "synthetic.ini"), QSettings.Format.IniFormat))
    bridge = FakeBridge()
    widget = MainWindow(bridge, prefs)
    qtbot.addWidget(widget)
    settings = widget.settings_page
    settings.notification_controls["notifications_enabled"].setChecked(True)
    settings.notification_controls["close_to_tray"].setChecked(True)
    settings.quiet_start.setValue(21)
    assert prefs.notifications_enabled and prefs.close_to_tray and prefs.quiet_start == 21
    settings._settings({"provider": "openai", "llm_ready": False, "execution_location": "cloud"})
    assert "云模型" in text_in_tree(settings.model_info)
    assert not widget.chat_view.findChildren(CommandForm)
    assert not widget.catalog_page.findChildren(CommandForm)
    widget._save_geometry()
    assert prefs.geometry


def test_queued_channel_reply_updates_only_on_qt_main_thread(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    threads: list[QThread] = []
    widget.channel.send("diagnostics", {}, lambda data: threads.append(QThread.currentThread()))
    worker = Thread(target=lambda: bridge.answer(bridge.sent[-1], {"status": "ready"}))
    worker.start()
    worker.join(timeout=1)
    qtbot.waitUntil(lambda: bool(threads))
    assert threads == [QApplication.instance().thread()]


def test_chat_is_plain_text_single_submit_with_no_grants_and_lightweight_messages(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    state = widget.chat_coordinator
    widget.chat_view.input.setPlainText("Synthetic request")
    widget.chat_view._send()
    widget.chat_view._send()
    requests = bridge.requests_for("submit")
    assert len(requests) == 1
    assert set(requests[0]["parameters"]) == {"request", "conversation_id", "expected_context_epoch", "command_id"}
    deliver(qtbot, widget, bridge, requests[0], {"accepted": True, "task_id": "chat-task"})
    state._timeline("daily", {"items": [
        {"id": "r", "sequence": 1, "kind": "assistant_message", "body": "<b>literal unsafe HTML</b>"},
        {"id": "c", "sequence": 2, "kind": "task_card", "task_id": "chat-task", "visible": False}]}, False)
    assert any(label.text() == "<b>literal unsafe HTML</b>" and label.textFormat() == Qt.TextFormat.PlainText
               for label in widget.chat_view.findChildren(QLabel))
    assert not widget.chat_view.findChildren(TaskCard)


def test_same_text_after_success_is_a_new_submission(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    widget.chat_view.input.setPlainText("Repeated synthetic intent")
    widget.chat_view._send()
    first = bridge.requests_for("submit")[-1]
    deliver(qtbot, widget, bridge, first, {"accepted": True, "task_id": "first"})
    widget.chat_view.input.setPlainText("Repeated synthetic intent")
    widget.chat_view._send()
    second = bridge.requests_for("submit")[-1]
    assert second["parameters"]["command_id"] != first["parameters"]["command_id"]


def test_remember_inherits_readonly_input_policy_and_closing_blocks_submit(window: Any) -> None:
    widget, bridge = window
    assert "use_policy" not in widget.memory_page.remember.fields
    widget.memory_page.action("remember", {"content": "synthetic new memory", "use_policy": "cloud_allowed"})
    assert bridge.requests_for("memory")[-1]["parameters"]["use_policy"] == "local_only"
    widget.chat_view.input.setPlainText("private draft")
    widget.chat_coordinator.set_state("Closing")
    widget.on_state_changed("Closing")
    before = len(bridge.requests_for("submit"))
    widget.chat_view._send()
    assert len(bridge.requests_for("submit")) == before
    assert not widget.chat_coordinator.drafts and not widget.chat_view.input.toPlainText()


def test_new_memory_waits_for_valid_settings_metadata(window: Any) -> None:
    widget, bridge = window
    memory = widget.memory_page; memory.clear_sensitive()
    before = len(bridge.requests_for("memory"))
    memory.action("remember", {"content": "synthetic new memory"})
    memory._settings({"request_use_default": "false"})
    assert not memory.remember.submit.isEnabled()
    assert len(bridge.requests_for("memory")) == before
    memory._settings({"request_use_default": True})
    memory.action("remember", {"content": "synthetic new memory", "use_policy": "local_only"})
    assert bridge.requests_for("memory")[-1]["parameters"]["use_policy"] == "cloud_allowed"


def test_cleanup_poll_is_global_until_ready_and_stops_when_closing(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    widget.hide(); bridge.set_state("CleanupBlocked")
    qtbot.waitUntil(lambda: widget.lifecycle == "CleanupBlocked")
    assert widget.cleanup_timer.isActive() and widget.cleanup_timer.interval() == 2000
    first = bridge.requests_for("cleanup_status")[-1]
    deliver(qtbot, widget, bridge, first, {"status": "Pending", "blocked": True, "pending": ["job"]})
    before = len(bridge.requests_for("cleanup_status"))
    widget.cleanup_timer.timeout.emit()
    assert len(bridge.requests_for("cleanup_status")) == before + 1
    second = bridge.requests_for("cleanup_status")[-1]
    deliver(qtbot, widget, bridge, second, {"status": "Completed", "blocked": False})
    assert widget.cleanup_timer.isActive()
    bridge.set_state("Ready"); qtbot.waitUntil(lambda: widget.lifecycle == "Ready")
    assert not widget.cleanup_timer.isActive()
    bridge.set_state("CleanupBlocked"); qtbot.waitUntil(lambda: widget.lifecycle == "CleanupBlocked")
    late = bridge.requests_for("cleanup_status")[-1]
    bridge.set_state("Closing"); qtbot.waitUntil(lambda: widget.lifecycle == "Closing")
    assert not widget.cleanup_timer.isActive()
    deliver(qtbot, widget, bridge, late, {"status": "Completed", "blocked": False})
    assert widget.lifecycle == "Closing"


def set_form(form: CommandForm, **values: Any) -> None:
    for key, value in values.items():
        field = form.fields[key]
        if isinstance(field, QComboBox):
            field.setCurrentIndex(field.findData(value))
        elif isinstance(field, QSpinBox):
            field.setValue(int(value))
        elif isinstance(field, QPlainTextEdit):
            field.setPlainText(str(value))
        else:
            field.setText(str(value))


def operation_result(qtbot: Any, widget: MainWindow, responses: list[dict[str, Any]],
                     operation: str, count_before: int) -> dict[str, Any]:
    qtbot.waitUntil(lambda: len([item for item in responses if item["operation"] == operation]) > count_before,
                    timeout=5000)
    response = [item for item in responses if item["operation"] == operation][-1]
    qtbot.waitUntil(lambda: not pending(widget, response["request_id"]), timeout=5000)
    assert response["ok"], response
    return response["data"]


def select_entity(qtbot: Any, widget: MainWindow, page: PagedView, entity_id: str) -> None:
    goals = widget.arrangements.goals
    operation = next((name + "s" for name, candidate in goals.pages.items() if candidate is page), None)
    page.refresh_requested.emit()
    # A cached ID is not proof that this refresh has returned the current revision.
    qtbot.waitUntil(lambda: (operation is None or (operation not in goals._read_active and operation not in goals._read_latest))
                    and any(item.get("id") == entity_id for item in page.items), timeout=5000)
    page.list.setCurrentRow(-1)
    page.list.setCurrentRow(next(index for index, item in enumerate(page.items) if item.get("id") == entity_id))


@pytest.fixture
def goals_page(qtbot: Any) -> tuple[Any, FakeBridge]:
    from deskpal.gui.management import GoalsPage
    bridge = FakeBridge(); bridge.state = "Ready"
    page = GoalsPage(bridge); qtbot.addWidget(page)
    return page, bridge


def test_goal_refresh_burst_reserves_capacity_and_reads_after_mutation(goals_page: Any, qtbot: Any) -> None:
    from collections import Counter
    goals, bridge = goals_page
    original = {"id": "goal-a", "title": "Original", "revision": 1}
    goals._goals({"items": [original]}); goals.pages["goal"].list.setCurrentRow(0)
    for _ in range(100): goals.refresh()
    assert Counter(item["operation"] for item in bridge.sent) == {
        "goals": 1, "tasks": 1, "schedule_items": 1, "milestones": 1, "bindings": 1}
    goals._status("goal")
    mutation = bridge.requests_for("goal_status")[-1]
    assert mutation["parameters"]["expected_revision"] == 1
    bridge.answer(mutation, {"id": "goal-a", "revision": 2})
    qtbot.waitUntil(lambda: mutation["request_id"] not in goals.channel.pending)
    old = bridge.requests_for("goals")[-1]
    bridge.answer(old, {"items": [{**original, "title": "Obsolete read"}]})
    qtbot.waitUntil(lambda: len(bridge.requests_for("goals")) == 2)
    assert goals.pages["goal"].items == [original]
    latest = bridge.requests_for("goals")[-1]
    bridge.answer(latest, {"items": [{**original, "revision": 2}]})
    qtbot.waitUntil(lambda: goals.snapshots["goal"]["revision"] == 2)
    assert len(bridge.requests_for("goal_status")) == 1


def test_goal_child_reads_follow_latest_parent_and_page(goals_page: Any, qtbot: Any) -> None:
    goals, bridge = goals_page
    goals._goals({"items": [{"id": identity, "title": identity, "revision": 1} for identity in ["a", "b"]]})
    old_milestones = bridge.requests_for("milestones")[-1]
    old_bindings = bridge.requests_for("bindings")[-1]
    goals.parent_goal.setCurrentIndex(1)
    goals.pages["milestone"]._move(50)
    bridge.answer(old_milestones, {"items": [{"id": "old-a", "title": "Old parent", "revision": 1}]})
    bridge.answer(old_bindings, {"items": [{"id": "old-binding", "revision": 1}]})
    qtbot.waitUntil(lambda: len(bridge.requests_for("milestones")) == 2 and len(bridge.requests_for("bindings")) == 2)
    assert not goals.pages["milestone"].items and not goals.pages["binding"].items
    page_50 = bridge.requests_for("milestones")[-1]
    assert page_50["parameters"] == {"goal_id": "b", "offset": 50, "limit": 50}
    goals.pages["milestone"]._move(50)
    bridge.answer(page_50, {"items": [{"id": "old-page", "title": "Old page", "revision": 1}], "offset": 50})
    qtbot.waitUntil(lambda: len(bridge.requests_for("milestones")) == 3)
    assert not goals.pages["milestone"].items
    latest = bridge.requests_for("milestones")[-1]
    assert latest["parameters"] == {"goal_id": "b", "offset": 100, "limit": 50}
    bridge.answer(latest, {"items": [{"id": "current-b", "title": "Current page", "revision": 1}], "offset": 100})
    qtbot.waitUntil(lambda: bool(goals.pages["milestone"].items))
    assert goals.pages["milestone"].items[0]["id"] == "current-b"
    assert goals.milestone.itemData(1) == "current-b"


def test_goal_clear_drops_late_reads_and_mutation_refresh(goals_page: Any, qtbot: Any) -> None:
    goals, bridge = goals_page
    goals._goals({"items": [{"id": "a", "title": "Synthetic goal", "revision": 1}]})
    goals.pages["goal"].list.setCurrentRow(0)
    goals.refresh(); goals.refresh(); goals._status("goal")
    old_requests = list(bridge.sent)
    goals.clear_sensitive()
    for request in old_requests:
        bridge.answer(request, {"items": [{"id": "late", "title": "Discarded", "revision": 2}]})
    QApplication.processEvents()
    assert len(bridge.sent) == len(old_requests)
    assert not goals.snapshots and not goals.sources and goals.parent_goal.count() == 0
    assert all(not page.items for page in goals.pages.values())


@pytest.mark.parametrize("size_argument", [False, True])
def test_explicit_resize_survives_fit_show_and_saved_geometry(
        window: Any, qtbot: Any, tmp_path: Any, size_argument: bool) -> None:
    widget, bridge = window
    widget.preferences = GuiPreferences(QSettings(str(tmp_path / "resize.ini"), QSettings.Format.IniFormat))
    widget.show()
    size = QSize(640, 480)
    if size_argument:
        widget.resize(size)
    else:
        widget.resize(size.width(), size.height())
    widget._queue_geometry_fit()
    qtbot.waitUntil(lambda: not widget._geometry_fit_pending)
    assert widget.size() == size
    widget.hide()
    widget.show()
    qtbot.waitUntil(lambda: not widget._geometry_fit_pending)
    assert widget.size() == size
    widget.showMaximized()
    qtbot.waitUntil(widget.isMaximized)
    widget.showNormal()
    qtbot.waitUntil(lambda: not widget._geometry_fit_pending)
    assert widget.size() == size
    widget._save_geometry()
    restored = MainWindow(bridge, widget.preferences)
    qtbot.addWidget(restored)
    restored.show()
    qtbot.waitUntil(lambda: not restored._geometry_fit_pending)
    assert restored.size() == size

def test_available_area_clamp_keeps_normal_size_for_same_screen_restore(
        window: Any, qtbot: Any, monkeypatch: Any) -> None:
    widget, _ = window
    widget.show()
    wanted = QSize(640, 600)
    widget.resize(wanted)
    qtbot.waitUntil(lambda: not widget._geometry_fit_pending)
    screen = widget.screen()
    original = screen.availableGeometry()
    smaller = QRect(original.x(), original.y(), original.width(), 400)
    monkeypatch.setattr(screen, "availableGeometry", lambda: smaller)
    widget._queue_geometry_fit()
    qtbot.waitUntil(lambda: not widget._geometry_fit_pending)
    assert widget.height() < wanted.height()
    assert widget._normal_client_size == wanted
    monkeypatch.setattr(screen, "availableGeometry", lambda: original)
    widget._queue_geometry_fit()
    qtbot.waitUntil(lambda: not widget._geometry_fit_pending)
    assert widget.size() == wanted

def test_channel_pending_metadata_has_no_private_body_and_is_bounded(qtbot: Any) -> None:
    bridge = FakeBridge(); channel = Channel(bridge)
    for index in range(150):
        channel.send("submit", {"request": "synthetic private text"}, mutation=True)
    assert "synthetic private text" not in repr(channel.pending)
    assert len(channel.pending) == 12
    assert len({call["parameters"]["command_id"] for call in bridge.sent}) == 12

def test_real_bridge_memory_review_correct_forget_pending_retry_and_late_read(qtbot: Any, tmp_path: Any) -> None:
    from threading import Event
    from deskpal.application.desktop import DesktopFacade
    from deskpal.application.factory import create_application
    from deskpal.config import load_config
    from deskpal.gui.bridge import ApplicationBridge
    from deskpal.memory.contracts import MemoryDraft, MemorySource

    config = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline", "DESKPAL_LLM_REQUEST_USE_DEFAULT": "false",
        "DESKPAL_WORKSPACE_DIR": str(tmp_path / "synthetic-workspace"),
        "DESKPAL_DATA_DIR": str(tmp_path / "synthetic-data")})
    state = {"fail_forget": False, "block_read": False}
    seeded: dict[str, str] = {}
    entered, release = Event(), Event()

    def factory() -> Any:
        app = create_application(config)
        runtime = app.core.memory_runtime
        for label in ("accept", "reject"):
            content = "synthetic candidate " + label
            item = runtime.repository.ingest(MemorySource(content), MemoryDraft(content, content), accepted=False)
            seeded[label] = item.id
        original = app.core.memory_command

        def controlled(action: str, **parameters: Any) -> Any:
            if action == "forget" and state["fail_forget"]:
                raise OSError("synthetic cleanup failure")
            return original(action, **parameters)

        app.core.memory_command = controlled
        return app

    class DelayedFacade(DesktopFacade):
        def execute(self, operation: str, parameters: Any) -> dict[str, Any]:
            data = super().execute(operation, parameters)
            if operation == "memories" and state["block_read"]:
                state["block_read"] = False
                entered.set()
                assert release.wait(5)
            return data

    bridge = ApplicationBridge(factory, facade_factory=DelayedFacade)
    widget = MainWindow(bridge)
    responses: list[dict[str, Any]] = []
    bridge.reply.connect(responses.append)
    qtbot.addWidget(widget)
    bridge.start()
    try:
        qtbot.waitUntil(lambda: widget.lifecycle == "Ready", timeout=5000)
        widget.poll_timer.stop()
        widget.set_page(2)
        qtbot.waitUntil(lambda: widget.memory_page._new_memory_policy is not None)
        set_form(widget.memory_page.remember, content="synthetic remembered body")
        before = len([item for item in responses if item["operation"] == "memory"])
        widget.memory_page.remember.submit.click()
        remembered = operation_result(qtbot, widget, responses, "memory", before)
        assert remembered["source"]["use_policy"] == "local_only"
        widget.memory_page.refresh()
        select_entity(qtbot, widget, widget.memory_page.page, seeded["reject"])
        before = len([item for item in responses if item["operation"] == "memory"])
        widget.memory_page.action("reject")
        rejected = operation_result(qtbot, widget, responses, "memory", before)
        assert rejected["status"] == "rejected"
        widget.memory_page.refresh()
        select_entity(qtbot, widget, widget.memory_page.page, seeded["accept"])
        before = len([item for item in responses if item["operation"] == "memory"])
        widget.memory_page.action("accept")
        accepted = operation_result(qtbot, widget, responses, "memory", before)
        assert accepted["status"] == "accepted"
        widget.memory_page.refresh()
        select_entity(qtbot, widget, widget.memory_page.page, seeded["accept"])
        set_form(widget.memory_page.correct, content="synthetic corrected private body")
        before = len([item for item in responses if item["operation"] == "memory"])
        widget.memory_page.correct.submit.click()
        corrected = operation_result(qtbot, widget, responses, "memory", before)
        assert corrected["supersedes"] == accepted["id"]
        assert corrected["id"] != accepted["id"]
        assert corrected["draft"]["content"] == "synthetic corrected private body"
        widget.memory_page.refresh()
        select_entity(qtbot, widget, widget.memory_page.page, corrected["id"])
        state["block_read"] = True
        old_request = widget.memory_page.channel.send("memories", {"limit": 50, "offset": 0}, widget.memory_page.page.set_page)
        qtbot.waitUntil(entered.is_set, timeout=5000)
        state["fail_forget"] = True
        widget.memory_page.action("forget")
        qtbot.waitUntil(lambda: widget.lifecycle == "CleanupBlocked", timeout=5000)
        assert widget.memory_page._snapshot == {}
        assert widget.lifecycle == "CleanupBlocked"
        assert widget.memory_page.page.detail.topLevelItemCount() == 0
        release.set()
        qtbot.waitUntil(lambda: not pending(widget, old_request))
        assert not any(item["request_id"] == old_request for item in responses)
        assert "synthetic corrected private body" not in text_in_tree(widget.memory_page.page.detail)
        state["fail_forget"] = False
        before = len([item for item in responses if item["operation"] == "retry_cleanup"])
        widget.settings_page.channel.send("retry_cleanup", {})
        cleanup = operation_result(qtbot, widget, responses, "retry_cleanup", before)
        assert cleanup["complete"]
        qtbot.waitUntil(lambda: widget.lifecycle == "Ready")
        widget.poll_timer.stop()
        select_entity(qtbot, widget, widget.memory_page.page, corrected["id"])
        assert widget.memory_page._snapshot["status"] == "deleted"
        assert widget.memory_page._snapshot["source"]["content"] == ""
        assert widget.memory_page._snapshot["draft"]["content"] == ""
    finally:
        release.set()
        state["fail_forget"] = False
        bridge.shutdown()
        qtbot.waitUntil(lambda: bridge.state == "Closed", timeout=5000)


def test_accept_proposal_is_revision_bound_queue_only(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    page = widget.arrangements.proposals
    page.set_page({"items": [{"id": "proposal-1", "revision": 7, "title": "建议"}]})
    page.list.setCurrentRow(0)
    widget.arrangements._proposal("accept_proposal")
    widget.arrangements._proposal("accept_proposal")
    requests = bridge.requests_for("accept_proposal")
    assert len(requests) == 1
    assert requests[0]["parameters"]["expected_revision"] == 7
    assert set(requests[0]["parameters"]) == {"proposal_id", "expected_revision", "command_id"}
    assert not bridge.requests_for("confirm")


def test_real_bridge_window_confirm_writes_only_after_explicit_grant(qtbot: Any, tmp_path: Any) -> None:
    from deskpal.application.factory import create_application
    from deskpal.config import load_config
    from deskpal.gui.bridge import ApplicationBridge
    from deskpal.planner.direct import DirectToolPlanner
    from deskpal.tools.models import ToolCall

    config = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
        "DESKPAL_WORKSPACE_DIR": str(tmp_path / "synthetic-workspace"),
        "DESKPAL_DATA_DIR": str(tmp_path / "synthetic-data")})
    planner = DirectToolPlanner(ToolCall("write_text", {"path": "synthetic.txt", "text": "synthetic output"}))
    bridge = ApplicationBridge(lambda: create_application(config, planner=planner))
    widget = MainWindow(bridge)
    qtbot.addWidget(widget)
    widget.show()
    state = widget.chat_coordinator
    bridge.start()
    try:
        qtbot.waitUntil(lambda: state.model_ready and bool(state.default_id), timeout=5000)
        state.timer.setInterval(30)
        widget.chat_view.set_conversation(state.default_id)
        widget.chat_view.input.setPlainText("Synthetic local write")
        widget.chat_view._send()
        qtbot.waitUntil(lambda: any(review.get("status") == "Waiting" for review in state.reviews.values()), timeout=5000)
        task_id = next(identity for identity, review in state.reviews.items() if review["status"] == "Waiting")
        qtbot.waitUntil(lambda: bool(widget.chat_view.findChildren(TaskCard)), timeout=5000)
        card = widget.chat_view.findChildren(TaskCard)[-1]
        assert not (config.sandbox.workspace_dir / "synthetic.txt").exists()
        assert not state.grants.get(task_id)
        assert "synthetic output" in "\n".join(text_in_tree(tree) for tree in card.findChildren(QTreeWidget))
        button_with_text(card, "允许上述普通操作").click()
        qtbot.waitUntil(lambda: state.reviews.get(task_id, {}).get("status") == "Completed", timeout=5000)
        assert (config.sandbox.workspace_dir / "synthetic.txt").read_text(encoding="utf-8") == "synthetic output"
        assert state.reviews[task_id]["attempt_summary"]["count"] == 1
        state.confirm(task_id)
        assert state.reviews[task_id]["status"] == "Completed"
    finally:
        bridge.shutdown()
        qtbot.waitUntil(lambda: bridge.state == "Closed", timeout=5000)
        qtbot.waitUntil(lambda: widget.lifecycle == "Closed", timeout=5000)


def test_real_bridge_goal_milestone_binding_and_proposal_form_contracts(qtbot: Any, tmp_path: Any) -> None:
    from collections import Counter
    from datetime import datetime, timezone
    from pytestqt.exceptions import TimeoutError as QtTimeoutError
    from deskpal.application.factory import create_application
    from deskpal.config import load_config
    from deskpal.gui.bridge import ApplicationBridge
    from deskpal.planner.direct import DirectToolPlanner
    from deskpal.tools.models import ToolCall

    config = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
        "DESKPAL_WORKSPACE_DIR": str(tmp_path / "synthetic-workspace"),
        "DESKPAL_DATA_DIR": str(tmp_path / "synthetic-data")})
    seeded: dict[str, str] = {}
    planner = DirectToolPlanner(ToolCall("write_text", {"path": "proposal.txt", "text": "must need confirmation"}))
    def factory() -> Any:
        app = create_application(config, planner=planner)
        goal = app.goals.create("Synthetic proposal owner", "Human evidence", command_id="seed-owner")
        for key in ("accept", "ignore"):
            seeded[key] = app.proposals.create(goal.id, rule_id="synthetic", root_source_key=key,
                kind="synthetic-review", due_at=datetime.now(timezone.utc).isoformat()).id
        seeded["timer"] = app.scheduling.create_timer(3600, label="Synthetic future timer").id
        return app

    class DiagnosticBridge(ApplicationBridge):
        def __init__(self) -> None:
            super().__init__(factory)
            self.sent_operations: list[str] = []

        def send(self, operation: str, parameters: dict[str, Any] | None = None, *, generation: int = 0) -> str:
            identity = super().send(operation, parameters, generation=generation)
            self.sent_operations.append(operation)
            return identity

    bridge = DiagnosticBridge()
    widget = MainWindow(bridge)
    responses: list[dict[str, Any]] = []
    bridge.reply.connect(responses.append)
    qtbot.addWidget(widget)
    goals = widget.arrangements.goals
    def diagnostic() -> dict[str, Any]:
        return {"pending_count": len(goals.channel.pending),
            "pending_operations": dict(Counter(value[0] for value in goals.channel.pending.values())),
            "snapshot_revisions": {name: item.get("revision") for name, item in goals.snapshots.items()},
            "selected_goal_revision": goals.pages["goal"].selected_item().get("revision"),
            "parent_goal_revision": (goals.parent_goal.currentData() or {}).get("revision"),
            "sent_operations": dict(Counter(bridge.sent_operations)),
            "reply_operations": dict(Counter(item["operation"] for item in responses)),
            "notice": goals.notice.text(), "bridge_state": bridge.state, "bridge_pending": bridge.pending_count}

    def perform(operation: str, action: Any) -> dict[str, Any]:
        before = len([item for item in responses if item["operation"] == operation])
        before_action = diagnostic()
        action()
        after_action = diagnostic()
        try:
            return operation_result(qtbot, widget, responses, operation, before)
        except QtTimeoutError as exc:
            raise AssertionError({"operation": operation, "before_action": before_action,
                "after_action": after_action, "timeout": diagnostic()}) from exc
    def choose_goal(identity: str) -> None:
        select_entity(qtbot, widget, goals.pages["goal"], identity)
        qtbot.waitUntil(lambda: goals.snapshots.get("goal", {}).get("id") == identity)
        index = next(index for index in range(goals.parent_goal.count()) if goals.parent_goal.itemData(index)["id"] == identity)
        goals.parent_goal.setCurrentIndex(index)
    bridge.start()
    try:
        qtbot.waitUntil(lambda: widget.lifecycle == "Ready", timeout=5000)
        widget.poll_timer.stop()
        widget.chat_coordinator.timer.stop()
        widget.set_page(1)
        widget.arrangements.tabs.setCurrentIndex(2)
        goal = perform("create_goal", lambda: goals._save("goal", {"title": "Synthetic GUI goal",
            "success_criteria": "Human verifies milestone", "deadline": "", "use_policy": "local_only"}, True))
        choose_goal(goal["id"])
        set_form(goals.forms["goal"], title="Revised GUI goal")
        updated = perform("update_goal", lambda: goals.forms["goal"].submit.click())
        assert updated["title"] == "Revised GUI goal"
        choose_goal(goal["id"])
        set_form(goals.forms["goal"], status="Paused", evidence="")
        assert perform("goal_status", lambda: goals._status("goal"))["status"] == "Paused"
        choose_goal(goal["id"])
        set_form(goals.forms["goal"], status="Active")
        assert perform("goal_status", lambda: goals._status("goal"))["status"] == "Active"
        choose_goal(goal["id"])
        milestone = perform("create_milestone", lambda: goals._save("milestone", {
            "title": "Synthetic milestone", "success_criteria": "Human signoff", "deadline": ""}, True))
        select_entity(qtbot, widget, goals.pages["milestone"], milestone["id"])
        set_form(goals.forms["milestone"], title="Revised milestone")
        revised = perform("update_milestone", lambda: goals.forms["milestone"].submit.click())
        assert revised["revision"] > milestone["revision"]
        select_entity(qtbot, widget, goals.pages["milestone"], milestone["id"])
        set_form(goals.forms["milestone"], status="Completed", evidence="Human milestone signoff")
        assert perform("milestone_status", lambda: goals._status("milestone"))["status"] == "Completed"
        choose_goal(goal["id"])
        qtbot.waitUntil(lambda: any(goals.source.itemData(index).get("source_id") == seeded["timer"] for index in range(goals.source.count())))
        goals.source.setCurrentIndex(next(index for index in range(goals.source.count()) if goals.source.itemData(index)["source_id"] == seeded["timer"]))
        binding = perform("bind", lambda: goals._bind({"use_policy": "local_only"}))
        assert binding["source_id"] == seeded["timer"]
        select_entity(qtbot, widget, goals.pages["binding"], binding["id"])
        set_form(goals.binding_form, status="Removed", use_policy="local_only")
        assert perform("update_binding", goals._update_binding)["status"] == "Removed"
        choose_goal(goal["id"])
        set_form(goals.forms["goal"], status="Completed", evidence="Human goal signoff")
        assert perform("goal_status", lambda: goals._status("goal"))["status"] == "Completed"
        widget.arrangements.tabs.setCurrentIndex(3)
        select_entity(qtbot, widget, widget.arrangements.proposals, seeded["ignore"])
        assert perform("ignore_proposal", lambda: widget.arrangements._proposal("ignore_proposal"))["status"] == "Ignored"
        select_entity(qtbot, widget, widget.arrangements.proposals, seeded["accept"])
        receipt = perform("accept_proposal", lambda: widget.arrangements._proposal("accept_proposal"))
        assert receipt["accepted"] and not (config.sandbox.workspace_dir / "proposal.txt").exists()
        widget.chat_coordinator.timer.start()
        widget.navigate("task", receipt["task_id"])
        qtbot.waitUntil(lambda: widget.chat_coordinator.reviews.get(receipt["task_id"], {}).get("status") == "Waiting", timeout=5000)
        assert widget.arrangements.tabs.currentIndex() == 0
        assert any(card.task_id == receipt["task_id"] and card.isVisible() for card in widget.arrangements.findChildren(TaskCard))
        review = widget.chat_coordinator.reviews[receipt["task_id"]]
        assert review["attempt_summary"]["count"] == 0
        assert not widget.chat_coordinator.grants.get(receipt["task_id"])
        assert not (config.sandbox.workspace_dir / "proposal.txt").exists()
    finally:
        bridge.shutdown()
        qtbot.waitUntil(lambda: bridge.state == "Closed", timeout=5000)
