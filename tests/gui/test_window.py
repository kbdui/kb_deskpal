"""Offline widget interactions using detached synthetic DTOs only."""

from copy import deepcopy
from threading import Thread
from typing import Any

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QObject, QPoint, QRect, QSettings, QSize, QThread, Signal, Qt
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QPlainTextEdit, QScrollArea, QSpinBox

from deskpal.gui.preferences import GuiPreferences
from deskpal.gui.viewmodels import RequestTracker
from deskpal.gui.window import MainWindow
from deskpal.gui.views import CommandForm, PagedView


class FakeBridge(QObject):
    state_changed = Signal(str)
    reply = Signal(object)
    privacy_changed = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.privacy_epoch = 0
        self.sent: list[dict[str, Any]] = []
        self.shutdown_count = 0
        self.start_count = 0

    def send(self, operation: str, parameters: dict[str, Any] | None = None, *, generation: int = 0) -> str:
        if operation == "request_forget" or (operation == "memory" and (
                parameters or {}).get("action") in {"forget", "correct", "accept", "reject"}):
            self.privacy_epoch += 1
            self.privacy_changed.emit(self.privacy_epoch)
        request_id = "request-" + str(len(self.sent) + 1)
        self.sent.append({"request_id": request_id, "operation": operation,
                          "parameters": deepcopy(parameters or {}), "generation": generation,
                          "privacy_epoch": self.privacy_epoch})
        return request_id

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


@pytest.fixture
def window(qtbot: Any) -> tuple[MainWindow, FakeBridge]:
    bridge = FakeBridge()
    widget = MainWindow(bridge)
    qtbot.addWidget(widget)
    bridge.state_changed.emit("Ready")
    qtbot.waitUntil(lambda: widget.lifecycle == "Ready")
    widget.poll_timer.stop()
    return widget, bridge


def deliver(qtbot: Any, widget: MainWindow, bridge: FakeBridge,
            request: dict[str, Any], data: dict[str, Any] | None = None, error: str | None = None) -> None:
    bridge.answer(request, data, error=error)
    qtbot.waitUntil(lambda: request["request_id"] not in widget.requests.pending)


def task_review(task_id: str = "task-1", status: str = "Waiting", revision: int = 1) -> dict[str, Any]:
    return {"task": {"id": task_id, "status": status, "revision": revision,
                     "request": "synthetic request", "result": None, "cancel_requested": False},
            "plan": {"hash": "a" * 64, "workspace_dir": "synthetic/workspace", "revision": 1,
                     "memory_refs": [["memory-1", 1]],
                     "plan": {"reply": "<img src='https://invalid.test/private'>", "steps": [
                         {"name": "read", "arguments": {"path": "full synthetic parameters"}},
                         {"name": "delete", "arguments": {"path": "explicit high-risk target"}}]},
                     "tool_specs": [{"name": "read", "permission_level": 1, "contract_version": "1"},
                                    {"name": "delete", "permission_level": 2, "contract_version": "1"}]},
            "attempts": [], "memory_epoch": 0, "session_id": "synthetic-session"}


def memory_item() -> dict[str, Any]:
    return {"id": "memory-1", "revision": 3, "status": "candidate",
            "draft": {"content": "synthetic sensitive body", "quote": "synthetic quote"},
            "source": {"content": "synthetic source", "origin": "user_message", "use_policy": "local_only"}}


def show_task(qtbot: Any, widget: MainWindow, bridge: FakeBridge,
              review: dict[str, Any] | None = None) -> None:
    widget.tabs.setCurrentIndex(1)
    widget._task_selected({"id": (review or task_review())["task"]["id"]})
    deliver(qtbot, widget, bridge, bridge.requests_for("task")[-1], review or task_review())


def test_initialization_and_close_use_host_lifecycle(qtbot: Any) -> None:
    bridge = FakeBridge()
    widget = MainWindow(bridge)
    qtbot.addWidget(widget)
    assert bridge.start_count == 0
    assert not widget.submit_button.isEnabled()
    widget.show_diagnostics()
    assert bridge.sent[-1]["operation"] == "diagnostics"
    with qtbot.waitSignal(widget.close_requested):
        widget.close()
    assert bridge.shutdown_count == 0
    widget.request_exit()
    assert widget.lifecycle == "Closing"
    assert bridge.shutdown_count == 1
    assert not widget.submit_button.isEnabled()
    bridge.state_changed.emit("Closed")
    qtbot.waitUntil(lambda: widget.lifecycle == "Closed")
    assert widget._allow_close


def test_full_plan_plain_text_exact_hash_and_split_empty_grants(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    show_task(qtbot, widget, bridge)
    assert "full synthetic parameters" in widget.tasks.detail.toPlainText()
    assert "explicit high-risk target" in widget.tasks.detail.toPlainText()
    assert "<img src=" in widget.tasks.detail.toPlainText()
    assert all(not item.isChecked() for item in [*widget._ordinary.values(), *widget._high.values()])
    assert set(widget._ordinary) == {"read"}
    assert set(widget._high) == {"delete"}
    widget._ordinary["read"].setChecked(True)
    widget.confirm_plan()
    widget.confirm_plan()
    sent = bridge.requests_for("confirm")
    assert len(sent) == 1
    assert sent[0]["parameters"]["plan_hash"] == "a" * 64
    assert sent[0]["parameters"]["ordinary_tools"] == ["read"]
    assert sent[0]["parameters"]["high_risk_tools"] == []
    assert sent[0]["parameters"]["command_id"]
    assert not widget.confirm_button.isEnabled()


def test_high_risk_requires_its_own_explicit_selection(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    show_task(qtbot, widget, bridge)
    widget._high["delete"].setChecked(True)
    widget.confirm_plan()
    params = bridge.requests_for("confirm")[-1]["parameters"]
    assert params["high_risk_tools"] == ["delete"]
    assert params["ordinary_tools"] == []


def test_old_detail_and_page_navigation_cannot_restore_authority(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    widget.tabs.setCurrentIndex(1)
    widget._task_selected({"id": "old"})
    old = bridge.requests_for("task")[-1]
    widget._task_selected({"id": "new"})
    new = bridge.requests_for("task")[-1]
    deliver(qtbot, widget, bridge, new, task_review("new"))
    deliver(qtbot, widget, bridge, old, task_review("old"))
    assert widget.task_snapshot["task"]["id"] == "new"
    widget._task_selected({"id": "late"})
    late = bridge.requests_for("task")[-1]
    widget.tabs.setCurrentIndex(0)
    deliver(qtbot, widget, bridge, late, task_review("late"))
    assert not widget.task_snapshot
    assert not widget.confirm_button.isEnabled()


@pytest.mark.parametrize("operation", ["confirm", "cancel"])
def test_task_command_immediately_invalidates_task_and_chat_details(
        window: Any, qtbot: Any, operation: str) -> None:
    widget, bridge = window
    show_task(qtbot, widget, bridge)
    widget._send("task", {"task_id": "task-1"}, route="tasks:detail")
    old_task = bridge.requests_for("task")[-1]
    widget._send("task", {"task_id": "task-1"}, route="chat:detail")
    old_chat = bridge.requests_for("task")[-1]
    if operation == "confirm":
        widget.confirm_plan()
    else:
        widget._task_command(operation)
    assert not widget.task_snapshot
    assert not widget._ordinary and not widget._high
    assert widget.tasks.detail.toPlainText() == ""
    deliver(qtbot, widget, bridge, old_task, task_review(revision=7))
    deliver(qtbot, widget, bridge, old_chat, task_review(revision=7))
    assert not widget.task_snapshot
    assert widget._chat_result_revision == 0
    deliver(qtbot, widget, bridge, bridge.requests_for(operation)[-1], error="Busy")
    assert not widget.confirm_button.isEnabled()
    assert not widget.cancel_button.isEnabled()


def test_mutation_success_invalidates_queries_started_while_command_pending(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    show_task(qtbot, widget, bridge)
    widget._task_command("cancel")
    widget._send("task", {"task_id": "task-1"}, route="tasks:detail")
    late_task = bridge.requests_for("task")[-1]
    old_list = bridge.requests_for("tasks")[-1]
    deliver(qtbot, widget, bridge, bridge.requests_for("cancel")[-1], {"cancel_requested": True})
    deliver(qtbot, widget, bridge, late_task, task_review())
    deliver(qtbot, widget, bridge, old_list, {"items": [task_review()["task"]]})
    assert not widget.task_snapshot
    assert not widget.tasks.items
    assert not widget.confirm_button.isEnabled()


@pytest.mark.parametrize("name", ["tasks", "memories", "proposals"])
def test_page_refresh_rechecks_changed_selection_and_clears_missing_selection(
        window: Any, qtbot: Any, name: str) -> None:
    widget, bridge = window
    widget.tabs.setCurrentIndex({"tasks": 1, "memories": 2, "proposals": 4}[name])
    page = widget.pages[name]
    item = {"tasks": task_review()["task"], "memories": memory_item(),
            "proposals": {"id": "proposal-1", "revision": 1, "status": "Pending"}}[name]
    page.set_page({"items": [item]})
    page.list.setCurrentRow(0)
    if name == "tasks":
        deliver(qtbot, widget, bridge, bridge.requests_for("task")[-1], task_review())
        widget._ordinary["read"].setChecked(True)
    elif name == "proposals":
        deliver(qtbot, widget, bridge, bridge.requests_for("proposal")[-1], item)
    button = {"tasks": widget.confirm_button, "memories": widget.memory_accept,
              "proposals": widget.proposal_accept}[name]
    assert button.isEnabled()
    updated = {**item, "revision": 8,
               "status": {"tasks": "Running", "memories": "accepted", "proposals": "Ignored"}[name]}
    page.set_page({"items": [updated]})
    assert not button.isEnabled()
    if name == "tasks":
        assert not widget._ordinary
        late = bridge.requests_for("task")[-1]
    elif name == "proposals":
        late = bridge.requests_for("proposal")[-1]
    else:
        assert widget.memory_snapshot == updated
    page.set_page({"items": []})
    if name == "tasks":
        deliver(qtbot, widget, bridge, late, task_review())
    elif name == "proposals":
        deliver(qtbot, widget, bridge, late, item)
    assert not {"tasks": widget.task_snapshot, "memories": widget.memory_snapshot,
                "proposals": widget.proposal_snapshot}[name]
    assert page.detail.toPlainText() == ""
    assert not button.isEnabled()


def test_selecting_goal_without_deadline_clears_previous_value(window: Any) -> None:
    widget, _ = window
    widget.tabs.setCurrentIndex(3)
    widget._management_selected("goals", {"id": "goal-1", "revision": 1,
                                         "deadline": "2027-01-01T00:00:00+00:00"})
    assert widget.goal_form.values()["deadline"]
    widget._management_selected("goals", {"id": "goal-2", "revision": 2, "deadline": None})
    assert widget.goal_form.values()["deadline"] == ""
    assert widget.goal_form.values()["expected_revision"] == 2


def test_large_font_pages_scroll_without_forcing_window_off_screen(window: Any, qtbot: Any) -> None:
    widget, _ = window
    widget.show()
    font = widget.font()
    font.setPointSize(18)
    widget.setFont(font)
    widget.resize(1100, 1500)
    widget._queue_geometry_fit()
    available = widget.screen().availableGeometry()
    qtbot.waitUntil(lambda: available.contains(widget.frameGeometry()))
    assert widget.minimumSizeHint().height() < available.height()
    buttons = [widget.submit_button, widget.uncertain_form.submit, widget.memory_remember.submit,
               widget.goal_form.submit, widget.proposal_ignore]
    for index, button in enumerate(buttons):
        widget.tabs.setCurrentIndex(index)
        if index == 2:
            widget.memory_forms.setCurrentIndex(1)
        qtbot.wait(50)
        ancestor = button.parentWidget()
        while ancestor is not None:
            if isinstance(ancestor, QScrollArea):
                ancestor.ensureWidgetVisible(button, 0, 0)
            ancestor = ancestor.parentWidget()
        assert available.contains(QRect(button.mapToGlobal(QPoint(0, 0)), button.size())), (
            index, button.geometry(), button.mapToGlobal(QPoint(0, 0)), available)
        scroll = widget._page_scrolls[index]
        viewport = QRect(scroll.viewport().mapToGlobal(QPoint(0, 0)), scroll.viewport().size())
        assert viewport.contains(QRect(button.mapToGlobal(QPoint(0, 0)), button.size()))


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


def test_forget_clears_all_sensitive_views_before_send_and_drops_queued_body(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    widget.tabs.setCurrentIndex(1)
    widget._task_selected({"id": "old"})
    old = bridge.requests_for("task")[-1]
    bridge.answer(old, task_review("old"))  # Queued but not delivered yet.
    widget.chat_history.setPlainText("synthetic private history")
    widget.chat_input.setPlainText("synthetic private draft")
    widget.goals.detail.setPlainText("synthetic goal body")
    widget.diagnostics.setPlainText("synthetic diagnosis body")
    widget._memory_selected(memory_item())
    widget.memory_action("forget")
    for editor in (widget.chat_history, widget.chat_input, widget.tasks.detail,
                   widget.goals.detail, widget.memories.detail, widget.diagnostics):
        assert editor.toPlainText() == ""
    assert widget.privacy_barrier
    assert bridge.privacy_epoch == 1
    forget = bridge.requests_for("request_forget")[-1]
    parameters = dict(forget["parameters"])
    assert parameters.pop("command_id")
    assert parameters == {"memory_id": "memory-1", "expected_revision": 3}
    assert forget["privacy_epoch"] == 1 and old["privacy_epoch"] == 0
    deliver(qtbot, widget, bridge, forget, {"status": "Pending", "blocked": True})
    assert widget.forget_state == "pending"
    assert widget.tasks.detail.toPlainText() == ""
    before = len(bridge.sent)
    widget.reload_page("memories", explicit=True)
    assert len(bridge.sent) == before
    assert "禁止加载" in widget.notice.text()
    widget.retry_cleanup()
    deliver(qtbot, widget, bridge, bridge.requests_for("retry_cleanup")[-1], {"complete": True})
    assert not widget.privacy_barrier
    assert widget.tasks.detail.toPlainText() == ""


@pytest.mark.parametrize("code", ["Busy", "Conflict", "Rejected", "QueueFull", "ValueError"])
def test_unsubmitted_forget_requires_explicit_reload(window: Any, qtbot: Any, code: str) -> None:
    widget, bridge = window
    widget._memory_selected(memory_item())
    widget.memory_action("forget")
    deliver(qtbot, widget, bridge, bridge.requests_for("request_forget")[-1], error=code)
    assert widget.forget_state == "unsubmitted"
    assert "遗忘未提交" in widget.notice.text()
    widget._poll()
    assert not bridge.requests_for("memories")
    widget.reload_page("memories", explicit=True)
    assert not widget.privacy_barrier
    assert bridge.requests_for("memories")[-1]["parameters"] == {"limit": 50, "offset": 0}


def test_memory_source_policy_review_and_correction(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    widget._memory_selected(memory_item())
    text = widget.memories.detail.toPlainText()
    assert "user_message" in text and "local_only" in text
    assert widget.memory_accept.isEnabled()
    widget.memory_action("correct", {"content": "synthetic corrected body"})
    params = bridge.requests_for("memory")[-1]["parameters"]
    assert params["action"] == "correct"
    assert params["expected_revision"] == 3
    assert params["content"] == "synthetic corrected body"
    deliver(qtbot, widget, bridge, bridge.requests_for("memory")[-1], {"id": "memory-1", "revision": 4})
    assert widget.privacy_barrier
    assert widget.forget_state == "complete"
    assert not widget.memory_snapshot


def test_queries_coalesce_and_real_page_size_stays_bounded(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    widget.tabs.setCurrentIndex(1)
    for _ in range(20):
        widget._poll()
    assert len(bridge.requests_for("tasks")) == 1
    page = {"items": [{"id": "task-1", "status": "Waiting", "revision": 1}], "offset": 0, "has_more": True}
    deliver(qtbot, widget, bridge, bridge.requests_for("tasks")[-1], page)
    qtbot.mouseClick(widget.tasks.next, Qt.MouseButton.LeftButton)
    params = bridge.requests_for("tasks")[-1]["parameters"]
    assert params == {"limit": 50, "offset": 50}
    for index in range(100):
        widget._send("tasks", {"limit": 50, "offset": index}, route="synthetic-" + str(index))
    assert len(widget.requests.pending) == widget.requests.limit
    assert "后台请求已满" in widget.notice.text()


def test_accept_proposal_is_revision_bound_queue_only(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    widget.tabs.setCurrentIndex(4)
    widget._proposal_selected({"id": "proposal-1"})
    deliver(qtbot, widget, bridge, bridge.requests_for("proposal")[-1],
            {"id": "proposal-1", "revision": 7, "status": "Pending", "request": "synthetic request"})
    widget._proposal_command("accept_proposal")
    widget._proposal_command("accept_proposal")
    requests = bridge.requests_for("accept_proposal")
    assert len(requests) == 1
    assert requests[0]["parameters"]["expected_revision"] == 7
    assert set(requests[0]["parameters"]) == {"proposal_id", "expected_revision", "command_id"}
    assert not widget.proposal_accept.isEnabled()


def test_notification_navigation_only_requeries(window: Any) -> None:
    widget, bridge = window
    widget.navigate("task", "task-notified")
    assert bridge.requests_for("task")[-1]["parameters"] == {"task_id": "task-notified"}
    widget.navigate("proposal", "proposal-notified")
    assert bridge.requests_for("proposal")[-1]["parameters"] == {"proposal_id": "proposal-notified"}
    assert not any(item["operation"] in {"confirm", "resume", "accept_proposal"} for item in bridge.sent)


def test_goal_milestone_and_binding_forms_send_real_cas_operations(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    widget.tabs.setCurrentIndex(3)
    goal = {"id": "goal-1", "revision": 9, "status": "Active", "title": "Synthetic goal",
            "success_criteria": "Human evidence", "deadline": None, "use_policy": "local_only"}
    widget.goals.set_page({"items": [goal], "offset": 0, "has_more": False})
    widget.goals.list.setCurrentRow(0)
    deliver(qtbot, widget, bridge, bridge.requests_for("goal")[-1], goal)
    values = widget.goal_form.values()
    values.update(action="goal_status", status="Completed", evidence="")
    widget._management_command("goal", values)
    assert not bridge.requests_for("goal_status")
    values["evidence"] = "Synthetic human signoff"
    widget._management_command("goal", values)
    params = bridge.requests_for("goal_status")[-1]["parameters"]
    assert params["goal_id"] == "goal-1"
    assert params["expected_revision"] == 9
    assert params["evidence"] == "Synthetic human signoff"
    deliver(qtbot, widget, bridge, bridge.requests_for("goal_status")[-1], {"id": "goal-1", "revision": 10})
    widget.goal_filter.setCurrentText("goal-1")
    milestone_values = widget.milestone_form.values()
    milestone_values.update(title="Synthetic step", success_criteria="Explicit standard", expected_revision=10)
    widget._management_command("milestone", milestone_values)
    params = bridge.requests_for("create_milestone")[-1]["parameters"]
    assert params["goal_id"] == "goal-1" and params["expected_revision"] == 10
    deliver(qtbot, widget, bridge, bridge.requests_for("create_milestone")[-1], {"id": "milestone-1"})
    binding_values = widget.binding_form.values()
    binding_values.update(source="timer", source_id="timer-1", expected_revision=11)
    widget._binding_command(binding_values)
    params = bridge.requests_for("bind")[-1]["parameters"]
    assert params["goal_id"] == "goal-1"
    assert params["source"] == "timer"
    assert params["expected_revision"] == 11


def test_gui_preferences_apply_and_backend_settings_are_read_only(qtbot: Any, tmp_path: Any) -> None:
    preferences = GuiPreferences(QSettings(str(tmp_path / "synthetic.ini"), QSettings.Format.IniFormat))
    bridge = FakeBridge()
    widget = MainWindow(bridge, preferences)
    qtbot.addWidget(widget)
    widget.on_state_changed("Ready")
    widget.poll_timer.stop()
    assert not preferences.notifications_enabled
    widget.notifications_toggle.setChecked(True)
    widget.tray_toggle.setChecked(True)
    widget.quiet_start.setValue(21)
    assert preferences.notifications_enabled
    assert preferences.close_to_tray
    assert preferences.quiet_start == 21
    assert widget.settings_text.isReadOnly()
    assert all(editor.objectName() == "qt_spinbox_lineedit"
               for editor in widget.settings_page.findChildren(QLineEdit))
    widget._save_geometry()
    assert preferences.geometry


def test_cloud_provider_explains_request_policy_without_upgrading(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    assert widget.use_policy.currentText() == "local_only"
    deliver(qtbot, widget, bridge, bridge.requests_for("settings")[-1], {"provider": "openai", "model": "synthetic"})
    assert "整次云规划" in widget.policy_hint.text()
    assert "主动选择 cloud_allowed" in widget.policy_hint.text()
    assert widget.use_policy.currentText() == "local_only"


def test_queued_signal_updates_only_on_qt_main_thread(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    widget.tabs.setCurrentIndex(1)
    widget._task_selected({"id": "worker-task"})
    request = bridge.requests_for("task")[-1]
    threads: list[QThread] = []
    original = widget._show_task

    def record(data: dict[str, Any]) -> None:
        threads.append(QThread.currentThread())
        original(data)

    widget._show_task = record
    worker = Thread(target=lambda: bridge.answer(request, task_review("worker-task")))
    worker.start()
    worker.join(timeout=1)
    qtbot.waitUntil(lambda: bool(threads))
    assert threads == [QApplication.instance().thread()]


def test_chat_is_bounded_plain_text_and_submit_has_no_grants(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    widget.tabs.setCurrentIndex(0)
    widget.chat_input.setPlainText("Synthetic request")
    widget.submit_request()
    widget.submit_request()
    requests = bridge.requests_for("submit")
    assert len(requests) == 1
    assert set(requests[0]["parameters"]) == {"request", "use_policy", "command_id"}
    deliver(qtbot, widget, bridge, requests[0], {"accepted": True, "task_id": "chat-task", "status": "Created"})
    widget._poll()
    review = task_review("chat-task", "Completed", 2)
    review["task"]["result"] = {"response": "<b>literal unsafe HTML</b>"}
    deliver(qtbot, widget, bridge, bridge.requests_for("task")[-1], review)
    assert "<b>literal unsafe HTML</b>" in widget.chat_history.toPlainText()
    for _ in range(10):
        widget._append_chat("x" * 64000)
    assert len(widget.chat_history.toPlainText()) <= 128000


def test_request_command_cache_has_no_private_body_and_is_bounded() -> None:
    tracker = RequestTracker()
    first = tracker.command_id("submit", {"request": "synthetic private text"})
    assert first == tracker.command_id("submit", {"request": "synthetic private text"})
    assert "synthetic private text" not in repr(tracker._commands)
    for index in range(150):
        tracker.command_id("submit", {"request": str(index)})
    assert len(tracker._commands) == 128


def test_same_text_after_success_is_a_new_submission(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    widget.tabs.setCurrentIndex(0)
    widget.chat_input.setPlainText("Repeated synthetic intent")
    widget.submit_request()
    first = bridge.requests_for("submit")[-1]
    deliver(qtbot, widget, bridge, first, {"accepted": True, "task_id": "first"})
    widget.chat_input.setPlainText("Repeated synthetic intent")
    widget.submit_request()
    second = bridge.requests_for("submit")[-1]
    assert second["parameters"]["command_id"] != first["parameters"]["command_id"]


def test_remember_uses_explicit_policy_and_clears_command_cache(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    widget.requests.command_id("submit", {"request": "synthetic erased intent"})
    widget.remember_memory({"content": "synthetic new memory", "use_policy": "local_only"})
    request = bridge.requests_for("memory")[-1]
    assert request["parameters"]["action"] == "remember"
    assert request["parameters"]["use_policy"] == "local_only"
    assert "synthetic erased intent" not in repr(widget.requests._commands)
    deliver(qtbot, widget, bridge, request, {"id": "synthetic-memory", "revision": 1})
    assert widget.forget_state == "complete"


def test_cleanup_and_closing_disable_business_and_clear_body(window: Any, qtbot: Any) -> None:
    widget, bridge = window
    show_task(qtbot, widget, bridge)
    widget.on_state_changed("CleanupBlocked")
    assert widget.privacy_barrier
    assert not widget.confirm_button.isEnabled()
    assert widget.tasks.detail.toPlainText() == ""
    before = len(bridge.sent)
    widget.submit_request()
    widget.navigate("task", "old")
    assert len(bridge.sent) == before
    widget.show_diagnostics()
    assert bridge.sent[-1]["operation"] == "diagnostics"
    widget.request_exit()
    widget.retry_cleanup()
    assert bridge.shutdown_count == 2


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
    bridge.start()
    try:
        qtbot.waitUntil(lambda: widget.lifecycle == "Ready", timeout=5000)
        widget.tabs.setCurrentIndex(0)
        widget.chat_input.setPlainText("Synthetic local write")
        widget.submit_request()
        qtbot.waitUntil(lambda: bool(widget._chat_task_id), timeout=5000)
        task_id = widget._chat_task_id
        widget.tabs.setCurrentIndex(1)
        widget._task_selected({"id": task_id})
        widget.poll_timer.setInterval(30)
        widget.poll_timer.start()
        qtbot.waitUntil(lambda: widget.confirm_button.isEnabled(), timeout=5000)
        assert not (config.sandbox.workspace_dir / "synthetic.txt").exists()
        assert not widget._ordinary["write_text"].isChecked()
        assert "synthetic output" in widget.tasks.detail.toPlainText()
        widget._ordinary["write_text"].setChecked(True)
        widget.confirm_plan()
        qtbot.waitUntil(lambda: widget.requests.existing("mutation:task") is None, timeout=5000)
        widget._task_selected({"id": task_id})
        qtbot.waitUntil(lambda: widget.task_snapshot.get("task", {}).get("status") == "Completed", timeout=5000)
        assert (config.sandbox.workspace_dir / "synthetic.txt").read_text(encoding="utf-8") == "synthetic output"
        assert len(widget.task_snapshot["attempts"]) == 1
        assert not widget.confirm_button.isEnabled()
    finally:
        widget.request_exit()
        qtbot.waitUntil(lambda: bridge.state == "Closed", timeout=5000)
        qtbot.waitUntil(lambda: widget.lifecycle == "Closed", timeout=5000)


def set_form(form: CommandForm, **values: Any) -> None:
    for key, value in values.items():
        field = form.fields[key]
        if isinstance(field, QComboBox):
            field.setCurrentText(str(value))
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
    qtbot.waitUntil(lambda: response["request_id"] not in widget.requests.pending, timeout=5000)
    assert response["ok"], response
    return response["data"]


def select_entity(qtbot: Any, widget: MainWindow, page: PagedView, entity_id: str) -> None:
    widget.reload_page(page.name, explicit=True)
    qtbot.waitUntil(lambda: any(item.get("id") == entity_id for item in page.items), timeout=5000)
    page.list.setCurrentRow(-1)
    page.list.setCurrentRow(next(index for index, item in enumerate(page.items) if item.get("id") == entity_id))


def test_real_bridge_memory_review_correct_forget_pending_retry_and_late_read(qtbot: Any, tmp_path: Any) -> None:
    from threading import Event
    from deskpal.application.desktop import DesktopFacade
    from deskpal.application.factory import create_application
    from deskpal.config import load_config
    from deskpal.gui.bridge import ApplicationBridge
    from deskpal.memory.contracts import MemoryDraft, MemorySource

    config = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
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
        widget.tabs.setCurrentIndex(2)
        set_form(widget.memory_remember, content="synthetic remembered body", use_policy="local_only")
        before = len([item for item in responses if item["operation"] == "memory"])
        widget.memory_remember.submit.click()
        remembered = operation_result(qtbot, widget, responses, "memory", before)
        assert remembered["source"]["use_policy"] == "local_only"
        widget.reload_page("memories", explicit=True)
        select_entity(qtbot, widget, widget.memories, seeded["reject"])
        before = len([item for item in responses if item["operation"] == "memory"])
        widget.memory_reject.click()
        rejected = operation_result(qtbot, widget, responses, "memory", before)
        assert rejected["status"] == "rejected"
        widget.reload_page("memories", explicit=True)
        select_entity(qtbot, widget, widget.memories, seeded["accept"])
        before = len([item for item in responses if item["operation"] == "memory"])
        widget.memory_accept.click()
        accepted = operation_result(qtbot, widget, responses, "memory", before)
        assert accepted["status"] == "accepted"
        widget.reload_page("memories", explicit=True)
        select_entity(qtbot, widget, widget.memories, seeded["accept"])
        set_form(widget.memory_edit, content="synthetic corrected private body")
        before = len([item for item in responses if item["operation"] == "memory"])
        widget.memory_edit.submit.click()
        corrected = operation_result(qtbot, widget, responses, "memory", before)
        assert corrected["supersedes"] == accepted["id"]
        assert corrected["id"] != accepted["id"]
        assert corrected["draft"]["content"] == "synthetic corrected private body"
        widget.reload_page("memories", explicit=True)
        select_entity(qtbot, widget, widget.memories, corrected["id"])
        state["block_read"] = True
        old_request = widget._send("memories", {"limit": 50, "offset": 0}, route="memories:list")
        qtbot.waitUntil(entered.is_set, timeout=5000)
        state["fail_forget"] = True
        widget.memory_forget.click()
        qtbot.waitUntil(lambda: widget.lifecycle == "CleanupBlocked", timeout=5000)
        assert widget.forget_state == "pending"
        assert widget.privacy_barrier
        assert widget.memories.detail.toPlainText() == ""
        release.set()
        qtbot.waitUntil(lambda: old_request not in widget.requests.pending)
        assert not any(item["request_id"] == old_request for item in responses)
        assert "synthetic corrected private body" not in widget.memories.detail.toPlainText()
        state["fail_forget"] = False
        before = len([item for item in responses if item["operation"] == "retry_cleanup"])
        widget.retry_cleanup()
        cleanup = operation_result(qtbot, widget, responses, "retry_cleanup", before)
        assert cleanup["complete"]
        qtbot.waitUntil(lambda: widget.lifecycle == "Ready" and not widget.privacy_barrier)
        widget.poll_timer.stop()
        select_entity(qtbot, widget, widget.memories, corrected["id"])
        assert widget.memory_snapshot["status"] == "deleted"
        assert widget.memory_snapshot["source"]["content"] == ""
        assert widget.memory_snapshot["draft"]["content"] == ""
    finally:
        release.set()
        state["fail_forget"] = False
        widget.request_exit()
        qtbot.waitUntil(lambda: bridge.state == "Closed", timeout=5000)


def test_real_bridge_goal_milestone_binding_and_proposal_form_contracts(qtbot: Any, tmp_path: Any) -> None:
    from datetime import datetime, timezone
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
        goal = app.goals.create("Synthetic proposal owner", "Synthetic human evidence", command_id="seed-owner")
        for label in ("accept", "ignore"):
            proposal = app.proposals.create(goal.id, rule_id="synthetic", root_source_key=label,
                kind="synthetic-review", due_at=datetime.now(timezone.utc).isoformat())
            seeded[label] = proposal.id
        return app

    bridge = ApplicationBridge(factory)
    widget = MainWindow(bridge)
    responses: list[dict[str, Any]] = []
    bridge.reply.connect(responses.append)
    qtbot.addWidget(widget)

    def click_operation(form: CommandForm, operation: str, **values: Any) -> dict[str, Any]:
        before = len([item for item in responses if item["operation"] == operation])
        set_form(form, action=operation, **values)
        form.submit.click()
        return operation_result(qtbot, widget, responses, operation, before)

    def choose_goal(goal_id: str) -> None:
        widget.management.setCurrentIndex(0)
        before = len([item for item in responses if item["operation"] == "goal"])
        select_entity(qtbot, widget, widget.goals, goal_id)
        operation_result(qtbot, widget, responses, "goal", before)

    bridge.start()
    try:
        qtbot.waitUntil(lambda: widget.lifecycle == "Ready", timeout=5000)
        widget.poll_timer.stop()
        widget.tabs.setCurrentIndex(3)
        goal = click_operation(widget.goal_form, "create_goal", title="Synthetic GUI goal",
            success_criteria="Human verifies milestone", deadline="", use_policy="local_only")
        choose_goal(goal["id"])
        updated = click_operation(widget.goal_form, "update_goal", title="Synthetic GUI revised goal")
        assert updated["title"] == "Synthetic GUI revised goal"
        choose_goal(goal["id"])
        paused = click_operation(widget.goal_form, "goal_status", status="Paused", evidence="")
        assert paused["status"] == "Paused"
        choose_goal(goal["id"])
        active = click_operation(widget.goal_form, "goal_status", status="Active", evidence="")
        assert active["status"] == "Active"
        widget.goal_filter.setCurrentText(goal["id"])
        widget.management.setCurrentIndex(1)
        milestone = click_operation(widget.milestone_form, "create_milestone", expected_revision=active["revision"],
            title="Synthetic GUI milestone", success_criteria="Human signoff", deadline="")
        select_entity(qtbot, widget, widget.milestones, milestone["id"])
        revised_milestone = click_operation(widget.milestone_form, "update_milestone", title="Revised synthetic milestone")
        assert revised_milestone["revision"] > milestone["revision"]
        select_entity(qtbot, widget, widget.milestones, milestone["id"])
        completed_milestone = click_operation(widget.milestone_form, "milestone_status",
            status="Completed", evidence="Synthetic human milestone signoff")
        assert completed_milestone["status"] == "Completed"
        choose_goal(goal["id"])
        goal_revision = widget.goal_form.values()["expected_revision"]
        widget.management.setCurrentIndex(2)
        binding = click_operation(widget.binding_form, "bind", expected_revision=goal_revision,
            source="timer", source_id="synthetic-timer", milestone_id="", use_policy="local_only")
        assert binding["source_id"] == "synthetic-timer"
        select_entity(qtbot, widget, widget.bindings, binding["id"])
        removed = click_operation(widget.binding_form, "update_binding", status="Removed", use_policy="local_only")
        assert removed["status"] == "Removed"
        choose_goal(goal["id"])
        completed_goal = click_operation(widget.goal_form, "goal_status", status="Completed",
            evidence="Synthetic human goal signoff")
        assert completed_goal["status"] == "Completed"
        widget.tabs.setCurrentIndex(4)
        select_entity(qtbot, widget, widget.proposals, seeded["ignore"])
        qtbot.waitUntil(lambda: widget.proposal_snapshot.get("id") == seeded["ignore"], timeout=5000)
        before = len([item for item in responses if item["operation"] == "ignore_proposal"])
        widget.proposal_ignore.click()
        ignored = operation_result(qtbot, widget, responses, "ignore_proposal", before)
        assert ignored["status"] == "Ignored"
        select_entity(qtbot, widget, widget.proposals, seeded["accept"])
        qtbot.waitUntil(lambda: widget.proposal_snapshot.get("id") == seeded["accept"], timeout=5000)
        before = len([item for item in responses if item["operation"] == "accept_proposal"])
        widget.proposal_accept.click()
        receipt = operation_result(qtbot, widget, responses, "accept_proposal", before)
        assert receipt["accepted"]
        assert not (config.sandbox.workspace_dir / "proposal.txt").exists()
        widget.navigate("task", receipt["task_id"])
        widget.poll_timer.setInterval(30)
        widget.poll_timer.start()
        qtbot.waitUntil(lambda: widget.task_snapshot.get("task", {}).get("status") == "Waiting", timeout=5000)
        assert not widget.task_snapshot["attempts"]
        assert not widget._ordinary["write_text"].isChecked()
        assert not (config.sandbox.workspace_dir / "proposal.txt").exists()
    finally:
        widget.request_exit()
        qtbot.waitUntil(lambda: bridge.state == "Closed", timeout=5000)
