"""Management reading surfaces measured inside the complete desktop shell."""

from copy import deepcopy

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QScrollArea

from deskpal.gui.views import PagedView
from deskpal.gui.widgets import notice_label, text_label
from tests.support.window import deliver, window, task_review


def visible_rect(widget, shell):
    """Clip to every real ancestor, including nested scroll viewports."""
    if not widget.isVisible():
        return QRect()
    result = QRect(widget.mapTo(shell, QPoint()), widget.size())
    ancestor = widget.parentWidget()
    while ancestor is not None:
        result = result.intersected(QRect(ancestor.mapTo(shell, QPoint()), ancestor.size()))
        if ancestor is shell:
            break
        ancestor = ancestor.parentWidget()
    return result.intersected(shell.rect())


def memory():
    return {"id": "memory", "revision": 5, "status": "accepted",
            "recorded_at": "2026-10-27T04:05:06.123456+00:00",
            "draft": {"content": "原文 path C:/user/private/file.json\n2026-10-27T04:05:06.123456+00:00\n" * 50},
            "source": {"use_policy": "local_only", "origin": "user_message"}}


def show_at(shell, qtbot, monkeypatch, width, page):
    # Give production's screen-fit policy enough desktop space for both sizes;
    # Qt's default offscreen screen is only 800px wide.
    monkeypatch.setattr(shell.screen(), "availableGeometry", lambda: QRect(0, 0, 1600, 1000))
    shell.resize(width, 760); shell.show(); shell.set_page(page)
    qtbot.waitUntil(lambda: not shell._geometry_fit_pending)


@pytest.mark.parametrize("width", [720, 1280])
def test_readonly_memory_detail_fits_complete_window(window, qtbot, monkeypatch, width):
    shell, _ = window
    show_at(shell, qtbot, monkeypatch, width, 2)
    page = shell.memory_page; item = memory()
    page.page.set_page({"items": [item]})
    page.page._select_memory_id(item["id"])
    qtbot.waitUntil(lambda: not page.page._orientation_pending)
    assert shell.width() == width and shell.height() == 760
    assert page.page.action_scroll.isHidden()
    assert visible_rect(page.page.summary_body, shell).height() >= 140
    assert page.page.summary_body.toPlainText() == item["draft"]["content"]
    assert "2026年10月27日" in page.page.summary_meta.text()
    assert "版本" not in page.page.summary_meta.text()
    assert page.notice.isHidden()
    assert page.page.toolbar_host.height() <= 60
    shell.resize(1280 if width == 720 else 720, 760)
    qtbot.waitUntil(lambda: not page.page._orientation_pending)
    assert page.page.summary_body.toPlainText() == item["draft"]["content"]
    assert page.learning_button.isVisible()


@pytest.mark.parametrize("width", [720, 1280])
def test_goal_reading_and_editor_use_full_detail_column_in_complete_window(window, qtbot, monkeypatch, width):
    shell, _ = window
    show_at(shell, qtbot, monkeypatch, width, 1)
    goals = shell.arrangements.goals; shell.arrangements.tabs.setCurrentIndex(1); shell.arrangements.task_tabs.setCurrentIndex(1)
    item = {"id": "goal", "title": "清楚的目标", "revision": 1, "status": "Active",
            "success_criteria": "清晰的完成标准\n" * 20}
    view = goals.pages["goal"]
    view.set_page({"items": [item]}); view.list.setCurrentRow(0)
    qtbot.waitUntil(lambda: not view._orientation_pending)
    assert shell.width() == width and shell.height() == 760
    assert visible_rect(view.summary_body, shell).height() >= 140
    tabs = goals.editor_tabs["goal"]
    assert tabs.currentIndex() == 0
    tabs.setCurrentIndex(1)
    area = goals.editor_hosts['goal'].widget(1)
    assert isinstance(area, QScrollArea)
    qtbot.waitUntil(lambda: area.isVisible())
    assert visible_rect(area.viewport(), shell).width() >= 300
    title = goals.forms["goal"].fields["title"]
    area.ensureWidgetVisible(title)
    assert visible_rect(title, shell).width() >= 280
    title.setText("未保存草稿")
    goals._selected("goal", {**item, "id": "other", "title": "另一目标"})
    goals._selected("goal", {**item, "revision": 2})
    assert title.text() == "未保存草稿"
    assert goals.snapshots["goal"]["revision"] == 1


@pytest.mark.parametrize("width", [720, 1280])
def test_arrangement_card_stays_wide_and_authorization_reachable_in_complete_window(window, qtbot, monkeypatch, width):
    shell, bridge = window
    show_at(shell, qtbot, monkeypatch, width, 1)
    page = shell.arrangements; page.tabs.setCurrentIndex(1); page.task_tabs.setCurrentIndex(0); review = task_review()
    review["steps"] = review["steps"][:1]
    review["missing_permissions"]["high_risk_tools"] = []
    shell.chat_coordinator.set_review(review["task_id"], review)
    page.tasks.set_page({"items": [{"id": review["task_id"], "title": "文件任务", "status": "Waiting"}]})
    page.tasks.list.setCurrentRow(0)
    deliver(qtbot, shell, bridge, bridge.requests_for("task_review")[-1], review)
    qtbot.waitUntil(lambda: not page.tasks._orientation_pending)
    assert shell.width() == width and shell.height() == 760
    expected = Qt.Orientation.Vertical if page.tasks.width() < 760 else Qt.Orientation.Horizontal
    qtbot.waitUntil(lambda: page.tasks.splitter.orientation() == expected)
    assert visible_rect(page.card_area.viewport(), shell).width() >= 300
    from PySide6.QtWidgets import QPushButton
    button = next(button for button in page.card_host.findChildren(QPushButton)
                  if button.text() == "允许上述普通操作" and button.isEnabled() and button.isVisible())
    page.card_area.ensureWidgetVisible(button)
    qtbot.waitUntil(lambda: visible_rect(button, shell).height() >= 30)
    assert button.isEnabled()


def test_catalog_summary_keeps_contract_in_technical_fields(qtbot):
    view = PagedView("tools"); qtbot.addWidget(view)
    item = {"display_name": "读取文本", "summary": "读取工作区中的文本文件",
            "description": "Read a UTF-8 encoded file.", "contract_version": "internal-v9",
            "parameter_fields": [{"name": "path", "label": "文件路径", "required": True,
                                  "schema": {"type": "string", "description": "Absolute file path"}}]}
    original = deepcopy(item); view.show_detail({"item": item})
    assert view.summary_body.toPlainText() == "读取工作区中的文本文件"
    assert view.summary_context.toPlainText() == "输入参数\n文件路径（必填）"
    assert "internal-v9" not in view.summary_meta.text()
    assert "Read a UTF-8" not in view.summary_body.toPlainText()
    assert item == original
    assert view.detail.isHidden()
    view.technical.click()
    assert not view.detail.isHidden()


@pytest.mark.parametrize("width", [720, 1280])
def test_catalog_reading_keeps_space_for_body_inside_complete_window(window, qtbot, monkeypatch, width):
    shell, _ = window
    show_at(shell, qtbot, monkeypatch, width, 3)
    page = shell.catalog_page
    item = {"id": "read_text", "display_name": "读取文本", "summary": "读取工作区中的文本文件\n" * 30,
            "permission_level": 0, "parameter_fields": [{"label": "文件路径", "required": True}]}
    page.tools.set_page({"items": [item]})
    page.tools._select_memory_id(item["id"])
    page.tools.show_detail(item)
    qtbot.waitUntil(lambda: not page.tools._orientation_pending)
    assert shell.width() == width and shell.height() == 760
    assert visible_rect(page.tools.summary_body, shell).height() >= 140
    assert page.tools.toolbar_host.height() <= 60
    assert page.tools.action_scroll.isHidden()
    assert page.notice.isHidden()


def test_goal_save_reply_preserves_another_records_new_draft(window, qtbot):
    shell, bridge = window
    page = shell.arrangements.goals
    first = {"id": "first", "revision": 1, "title": "第一个", "status": "Active"}
    second = {**first, "id": "second", "title": "第二个"}
    page._selected("goal", first)
    page.forms["goal"].fields["title"].setText("保存第一个")
    page._save("goal", page.forms["goal"].values(), False)
    request = bridge.requests_for("update_goal")[-1]
    page._selected("goal", second)
    page.forms["goal"].fields["title"].setText("保留第二个草稿")
    deliver(qtbot, shell, bridge, request, {"ok": True})
    assert page.forms["goal"].dirty
    assert page.forms["goal"].fields["title"].text() == "保留第二个草稿"


def test_notices_hide_only_empty_status_labels(qtbot):
    notice = notice_label(); body = text_label()
    qtbot.addWidget(notice); qtbot.addWidget(body)
    body.show()
    assert notice.isHidden() and not body.isHidden()
    notice.setText("状态已更新")
    assert not notice.isHidden()
    notice.clear()
    assert notice.isHidden()
