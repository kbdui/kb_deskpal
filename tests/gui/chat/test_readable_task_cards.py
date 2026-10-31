"""Task review summaries retain exact payloads and authorization barriers."""
import copy
import json
from typing import Any

import pytest
from PySide6.QtCore import QObject, Signal, QCoreApplication, Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QLabel, QPlainTextEdit, QPushButton, QScrollArea, QTreeWidget, QVBoxLayout, QWidget

from deskpal.gui.chat import ChatCoordinator, ChatView
from deskpal.gui.task_card import TaskCard


class Bridge(QObject):
    state_changed = Signal(str)
    reply = Signal(object)
    image_reply = Signal(object)
    privacy_changed = Signal(int)
    state = "Ready"
    privacy_epoch = 0

    def __init__(self) -> None:
        super().__init__(); self.calls: list[tuple[str, dict[str, Any]]] = []

    def send(self, operation: str, parameters: dict[str, Any], *, generation: int = 0) -> str:
        self.calls.append((operation, copy.deepcopy(parameters)))
        return str(len(self.calls))

    def send_image(self, operation: str, parameters: dict[str, Any], generation: int = 0) -> str:
        return self.send(operation, parameters, generation=generation)


def state() -> tuple[Bridge, ChatCoordinator]:
    bridge = Bridge(); coordinator = ChatCoordinator(bridge); coordinator.timer.stop()
    coordinator.default_id = "daily"; coordinator.model_ready = True
    coordinator.conversations = {key: {"title": title, "id": key, "revision": 1}
                                for key, title in (("daily", "日常聊天"), ("project", "项目讨论"))}
    return bridge, coordinator


def review(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"task_id": "t", "task_revision": 2, "confirmation_revision": 1, "plan_hash": "exact-original-hash",
        "workspace": r"E:\private\projects\named-workspace", "status": "Waiting", "title": '{"raw":"内部计划"}',
        "steps": [{"tool_name": "write_text", "display_name": "写入文本", "arguments": arguments,
                   "fields": [{"name": "path", "label": "文件路径"}, {"name": "start_at", "label": "开始时间"}]}],
        "missing_permissions": {"ordinary_tools": ["write_text"], "high_risk_tools": []},
        "available_actions": ["confirm", "cancel"], "confirmation_state": "none",
        "task": {"request": "原始请求", "result": {"plan": "庞大内部结果", "status": "Waiting"}}}


def button(widget: Any, title: str) -> QPushButton:
    return next(item for item in widget.findChildren(QPushButton) if item.text() == title and item.isEnabled())


def test_default_is_readable_and_explicit_copy_preserves_exact_arguments(qtbot: Any) -> None:
    bridge, coordinator = state()
    arguments = {"path": r"E:\private\projects\named-workspace\docs\notes.txt", "start_at": "2026-10-27T08:45:59.123456+00:00",
                 "future_payload": {"status": "Completed", "flag": True, "blank": None, "text": "true"}}
    original = review(arguments); coordinator.set_review("t", original)
    card = TaskCard(coordinator, "t"); qtbot.addWidget(card); card.resize(600, 700); card.show()
    labels = "\n".join(item.text() for item in card.findChildren(QLabel) if item.isVisible())
    assert "写入文本" in labels and "named-workspace" in labels
    assert "内部计划" not in labels and "庞大内部结果" not in labels
    assert all(tree.isHidden() for tree in card.findChildren(QTreeWidget))
    summary = card.findChild(QPlainTextEdit, "task_parameters").toPlainText()
    assert "文件路径：docs\\notes.txt" in summary and "2026年10月27日" in summary
    assert "future_payload" in summary and "Completed" in summary and "true" in summary and "null" in summary
    assert "{'status'" not in summary
    button(card, "查看/复制完整原参数").click()
    dialog = next(dialog for dialog in card.findChildren(QDialog) if dialog.isVisible())
    raw = dialog.findChild(QPlainTextEdit).toPlainText()
    assert json.loads(raw) == arguments
    button(dialog, "复制完整值").click()
    assert QApplication.clipboard().text() == raw
    dialog.close(); QCoreApplication.processEvents()
    button(card, "允许上述普通操作").click()
    command = [params for operation, params in bridge.calls if operation == "confirm"][0]
    assert command["plan_hash"] == "exact-original-hash" and command["expected_revision"] == 2
    assert command["ordinary_tools"] == ["write_text"] and command["high_risk_tools"] == []
    assert coordinator.reviews["t"]["steps"][0]["arguments"] == arguments


@pytest.mark.parametrize("depth", [False, True])
def test_collapsed_arguments_cannot_bypass_node_or_depth_limit(qtbot: Any, depth: bool) -> None:
    bridge, coordinator = state()
    arguments: dict[str, Any] = {"future": ["x"] * 4100}
    if depth:
        arguments = {"leaf": "x"}
        for _ in range(18):
            arguments = {"nested": arguments}
    coordinator.set_review("t", review(arguments))
    card = TaskCard(coordinator, "t"); qtbot.addWidget(card)
    confirm = next(item for item in card.findChildren(QPushButton) if item.text() == "允许上述普通操作")
    assert not confirm.isEnabled()
    assert any(tree.property("deskpal_complete") is False for tree in card.findChildren(QTreeWidget))
    assert len(card.findChildren(QPlainTextEdit)) == 1
    assert not any(operation == "confirm" for operation, _ in bridge.calls)


def test_compact_complex_scope_requires_expansion_and_preserves_high_risk_gate(qtbot: Any) -> None:
    bridge, coordinator = state(); original = review({"path": "notes.txt"})
    original["steps"] *= 2
    original["missing_permissions"] = {"ordinary_tools": [], "high_risk_tools": ["write_text"]}
    coordinator.set_review("t", original)
    card = TaskCard(coordinator, "t", compact=True); qtbot.addWidget(card)
    confirm = next(item for item in card.findChildren(QPushButton) if item.text() == "确认所选授权")
    assert not confirm.isEnabled()
    card.findChild(QCheckBox).setChecked(True)
    assert "write_text" in coordinator.grants["t"]
    assert not any(item.isEnabled() for item in card.findChildren(QPushButton) if item.text() == "确认所选授权")
    expanded: list[tuple[str, str]] = []; coordinator.expand_requested.connect(lambda conversation, task: expanded.append((conversation, task)))
    button(card, "展开完整审核").click()
    assert expanded == [("", "t")] and not any(operation == "confirm" for operation, _ in bridge.calls)


def test_specialized_empty_chat_and_user_json_are_not_rewritten(qtbot: Any) -> None:
    _, coordinator = state(); view = ChatView(coordinator); qtbot.addWidget(view)
    view.resize(500, 500); view.show(); view.set_conversation("project")
    assert "日常" not in view.status.text()
    assert "主题" in "\n".join(item.text() for item in view.body.findChildren(QLabel))
    assert view.new_message.isHidden()
    raw = '{"start_at":"2026-10-27T08:45:59.123456Z","path":"C:/private/long/path/file.txt"}'
    coordinator.timelines["project"] = [{"id": "u", "kind": "user_message", "body": raw},
                                        {"id": "a", "kind": "assistant_message", "body": raw}]
    view._timeline("project"); QCoreApplication.processEvents()
    assert sum(item.text() == raw for item in view.body.findChildren(QLabel)) == 2
    view.new_message.show(); view.set_conversation("daily"); QCoreApplication.processEvents()
    assert view.new_message.isHidden()


def test_short_waiting_card_keeps_content_height_and_natural_buttons_in_tall_parent(qtbot: Any) -> None:
    bridge, coordinator = state()
    coordinator.set_review("t", review({"path": "notes.txt", "text": "两行参数"}))
    host = QWidget(); host.resize(1920, 1080); layout = QVBoxLayout(host)
    card = TaskCard(coordinator, "t"); layout.addWidget(card)
    qtbot.addWidget(host); host.show(); QCoreApplication.processEvents()
    assert host.height() == 1080
    assert card.height() <= 400
    confirm = button(card, "允许上述普通操作")
    cancel = button(card, "取消这个请求")
    for action in (confirm, cancel):
        assert action.isVisible() and action.width() <= 420
        assert action.width() >= action.fontMetrics().horizontalAdvance(action.text()) + 10
    location = button(card, "查看/复制完整位置")
    workspace = next(label for label in card.findChildren(QLabel) if label.text().startswith("执行工作区："))
    assert 0 <= location.x() - workspace.geometry().right() <= 15
    qtbot.mouseClick(confirm, Qt.MouseButton.LeftButton)
    assert len([operation for operation, _ in bridge.calls if operation == "confirm"]) == 1


def test_long_parameter_card_still_scrolls_to_last_value_and_authorization(qtbot: Any) -> None:
    bridge, coordinator = state()
    coordinator.set_review("t", review({f"field_{index}": f"value_{index}" for index in range(120)}))
    host = QWidget(); host.resize(720, 760); layout = QVBoxLayout(host)
    area = QScrollArea(); area.setWidgetResizable(True); layout.addWidget(area)
    content = QWidget(); content_layout = QVBoxLayout(content)
    card = TaskCard(coordinator, "t"); content_layout.addWidget(card); content_layout.addStretch(); area.setWidget(content)
    qtbot.addWidget(host); host.show(); QCoreApplication.processEvents()
    document = card.findChild(QPlainTextEdit, "task_parameters")
    assert document.verticalScrollBar().maximum() > 0
    document.verticalScrollBar().setValue(document.verticalScrollBar().maximum())
    assert document.verticalScrollBar().value() == document.verticalScrollBar().maximum()
    assert "field_119：value_119" in document.toPlainText()
    button(card, "原参数 ▸").click(); QCoreApplication.processEvents()
    confirm = button(card, "允许上述普通操作")
    area.ensureWidgetVisible(confirm); QCoreApplication.processEvents()
    center = confirm.mapTo(area.viewport(), confirm.rect().center())
    assert area.viewport().rect().contains(center) and confirm.isVisible()
    qtbot.mouseClick(confirm, Qt.MouseButton.LeftButton)
    assert len([operation for operation, _ in bridge.calls if operation == "confirm"]) == 1


def completed_review() -> dict[str, Any]:
    value = review({"limit": 100})
    value.update(status="Completed", available_actions=[], conversation_id="daily")
    value["steps"] = [{"call_id": "calendar-call", "tool_name": "calendar_list", "display_name": "查看日程", "arguments": {"limit": 100}}]
    value["task"] = {"result": {"response": "本次查询范围内暂无日程。", "tool_results": [
        {"call_id": "calendar-call", "tool_name": "calendar_list", "success": True, "output": [], "error": None}]}}
    return value


@pytest.mark.parametrize("compact", [False, True])
def test_finished_chat_has_only_main_window_results_entry(qtbot: Any, compact: bool) -> None:
    _, coordinator = state(); coordinator.set_review("t", completed_review())
    card = TaskCard(coordinator, "t", compact=compact); qtbot.addWidget(card)
    requests: list[str] = []; coordinator.tool_results_requested.connect(requests.append)
    entries = card.findChildren(QPushButton)
    assert [entry.text() for entry in entries] == ([] if compact else ["查看工具结果"])
    assert card.findChildren(QPlainTextEdit) == [] and card.findChildren(QTreeWidget) == []
    if not compact:
        entries[0].click()
        assert requests == ["t"] and card.findChildren(QDialog) == []


def test_main_sidebar_tracks_facts_and_wipes_on_privacy_or_navigation(qtbot: Any) -> None:
    from deskpal.gui.window import MainWindow

    bridge = Bridge(); window = MainWindow(bridge); qtbot.addWidget(window)
    window._allow_close = True
    window.chat_coordinator.timer.stop(); window.poll_timer.stop(); window.cleanup_timer.stop()
    coordinator = window.chat_coordinator
    original = completed_review(); coordinator.set_review("t", original)
    coordinator.tool_results_requested.emit("t")
    assert not window.tool_results_dock.isHidden()
    assert "0 项日程" in window.tool_results.body.toPlainText()
    window.tool_results.diagnostic.setChecked(True)
    raw = window.tool_results.raw_body.toPlainText()
    assert json.loads(raw)["task"]["result"] == original["task"]["result"]
    updated = copy.deepcopy(original); updated["task_revision"] += 1
    updated["task"]["result"]["tool_results"][0]["output"] = [{"title": "会议", "start_at": "2026-10-28T10:00:00+08:00"}]
    coordinator.set_review("t", updated)
    assert "会议" in window.tool_results.body.toPlainText()
    window.set_page(4)
    assert window.tool_results_dock.isHidden() and not window.tool_results.body.toPlainText()
    assert not window.tool_results.raw_body.toPlainText()
    window.set_page(0); coordinator.tool_results_requested.emit("t")
    coordinator.clear_sensitive()
    assert window.tool_results_dock.isHidden()
    assert not window.tool_results.body.toPlainText() and not window.tool_results.raw_body.toPlainText()
    assert id(window.tool_results) not in coordinator._review_views


def test_unmatched_and_unstarted_calls_do_not_acquire_success(qtbot: Any) -> None:
    from deskpal.gui.tool_results import ToolResultsPanel

    value = completed_review()
    value["task"]["result"]["tool_results"][0]["call_id"] = "different-call"
    panel = ToolResultsPanel(); qtbot.addWidget(panel); panel.update_review(value)
    text = panel.body.toPlainText()
    assert "仅有原计划，未见执行记录" in text
    assert "此结果未能关联原计划参数" in text


def test_pet_waiting_scope_has_no_detail_buttons_and_still_requires_high_risk_grant(qtbot: Any) -> None:
    bridge, coordinator = state()
    value = review({"path": "notes.txt"})
    value["missing_permissions"] = {"ordinary_tools": [], "high_risk_tools": ["write_text"]}
    coordinator.set_review("t", value)
    card = TaskCard(coordinator, "t", compact=True); qtbot.addWidget(card)
    titles = [item.text() for item in card.findChildren(QPushButton)]
    assert not any(any(word in title for word in ("详情", "查看", "复制", "原参数", "原结果", "技术信息")) for title in titles)
    confirm = next(item for item in card.findChildren(QPushButton) if item.text() == "确认所选授权")
    assert not confirm.isEnabled() and not any(operation == "confirm" for operation, _ in bridge.calls)

