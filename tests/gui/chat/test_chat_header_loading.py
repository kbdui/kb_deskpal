"""Offline regressions for readable conversation switching and first-page loading."""

from typing import Any

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QObject, Signal

from deskpal.gui.chat import ChatCoordinator, ChatView
from deskpal.gui.window import MainWindow


class FakeBridge(QObject):
    state_changed = Signal(str)
    reply = Signal(object)
    image_reply = Signal(object)
    privacy_changed = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.state = "Initializing"
        self.privacy_epoch = 0
        self.sent: list[dict[str, Any]] = []

    def send(self, operation: str, parameters: dict[str, Any], *, generation: int = 0) -> str:
        identity = f"synthetic-{len(self.sent)}"
        self.sent.append({"request_id": identity, "operation": operation, "parameters": dict(parameters),
                          "generation": generation, "privacy_epoch": self.privacy_epoch})
        return identity

    def send_image(self, operation: str, parameters: dict[str, Any], generation: int = 0) -> str:
        return self.send(operation, parameters, generation=generation)

    def set_state(self, state: str) -> None:
        self.state = state
        self.state_changed.emit(state)

    def answer(self, request: dict[str, Any], data: dict[str, Any]) -> None:
        envelope = {key: request[key] for key in ("request_id", "operation", "generation", "privacy_epoch")}
        self.reply.emit({**envelope, "ok": True, "data": data})


@pytest.fixture
def shell(qtbot: Any) -> Any:
    bridge = FakeBridge()
    widget = MainWindow(bridge)
    qtbot.addWidget(widget)
    yield widget, bridge
    widget.poll_timer.stop()
    widget.cleanup_timer.stop()
    widget.chat_coordinator.timer.stop()
    widget._allow_close = True


def ready(qtbot: Any, widget: MainWindow, bridge: FakeBridge) -> None:
    bridge.set_state("Ready")
    qtbot.waitUntil(lambda: widget.lifecycle == "Ready")
    widget.chat_coordinator.timer.stop()
    widget.poll_timer.stop()


def populate(state: ChatCoordinator) -> None:
    state.default_id = "daily"
    state.conversations = {
        "daily": {"id": "daily", "title": "日常聊天", "revision": 1, "context_epoch": 1},
        "project": {"id": "project", "title": "为这周的阅读和学习安排一段专项会话", "revision": 1, "context_epoch": 1},
    }
    state.conversations_changed.emit()


def test_narrow_chat_opens_session_directory_from_title(shell: Any, qtbot: Any) -> None:
    widget, bridge = shell
    ready(qtbot, widget, bridge)
    populate(widget.chat_coordinator)
    widget.chat_view.set_conversation("project")
    widget.resize(480, 700)
    widget.show()
    view = widget.chat_view
    assert view.conversation_row.isHidden()
    assert view.title.text() == state_title(widget)
    view.title.click()
    qtbot.waitUntil(widget.directory_dialog.isVisible)
    assert widget.conversations.count() == 2


def state_title(widget: MainWindow) -> str:
    return str(widget.chat_coordinator.conversations["project"]["title"])


def test_wide_and_narrow_chat_have_no_permanent_directory(shell: Any) -> None:
    widget, _ = shell
    widget.resize(1120, 760)
    widget._responsive()
    assert widget.directory_dialog.isHidden()
    assert widget.chat_view.conversation_row.isHidden()
    widget.resize(700, 600)
    widget._responsive()
    assert widget.directory_dialog.isHidden()
    assert widget.chat_view.conversation_row.isHidden()


def test_compact_chat_keeps_readable_switching_and_independent_draft(qtbot: Any) -> None:
    bridge = FakeBridge()
    bridge.state = "Ready"
    state = ChatCoordinator(bridge)
    state.timer.stop()
    populate(state)
    view = ChatView(state, compact=True)
    qtbot.addWidget(view)
    view.resize(360, 600)
    view.show()
    state.edit_draft("daily", "保留日常草稿")
    view.set_selector_visible(False)
    view.selector.setCurrentIndex(view.selector.findData("project"))
    assert view.conversation_id == "project"
    assert view.conversation_row.isVisible()
    assert view.selector.width() >= 120
    view.selector.setCurrentIndex(view.selector.findData("daily"))
    assert view.input.toPlainText() == "保留日常草稿"


@pytest.mark.parametrize(("page", "operation"), [(1, "schedule_items"), (2, "memories"), (3, "tools_catalog")])
def test_page_chosen_during_startup_loads_when_backend_becomes_ready(
    shell: Any, qtbot: Any, page: int, operation: str,
) -> None:
    widget, bridge = shell
    widget.set_page(page)
    assert not any(item["operation"] == operation for item in bridge.sent)
    ready(qtbot, widget, bridge)
    assert widget.stack.currentIndex() == page
    assert any(item["operation"] == operation for item in bridge.sent)


def test_ready_recovery_reloads_the_current_page_without_clicking_reload(shell: Any, qtbot: Any) -> None:
    widget, bridge = shell
    widget.set_page(3)
    ready(qtbot, widget, bridge)
    bridge.set_state("CleanupBlocked")
    qtbot.waitUntil(lambda: widget.lifecycle == "CleanupBlocked")
    widget.set_page(2)
    before = len([item for item in bridge.sent if item["operation"] == "memories"])
    ready(qtbot, widget, bridge)
    assert widget.stack.currentIndex() == 2
    assert len([item for item in bridge.sent if item["operation"] == "memories"]) == before + 1


def test_delayed_startup_settings_do_not_steal_user_navigation(shell: Any, qtbot: Any) -> None:
    widget, bridge = shell
    ready(qtbot, widget, bridge)
    initial = next(item for item in bridge.sent if item["request_id"] in widget.channel.pending and item["operation"] == "settings")
    widget.set_page(2)
    bridge.answer(initial, {"llm_ready": False})
    qtbot.waitUntil(lambda: initial["request_id"] not in widget.channel.pending)
    assert widget.stack.currentIndex() == 2


def test_initial_connection_guidance_still_opens_for_untouched_chat(shell: Any, qtbot: Any) -> None:
    widget, bridge = shell
    ready(qtbot, widget, bridge)
    initial = next(item for item in bridge.sent if item["request_id"] in widget.channel.pending and item["operation"] == "settings")
    bridge.answer(initial, {"llm_ready": False})
    qtbot.waitUntil(lambda: widget.stack.currentIndex() == 4)
    assert widget.settings_page.tabs.currentIndex() == 4


def test_privacy_refresh_loads_current_page_and_drops_old_receipts(shell: Any, qtbot: Any) -> None:
    widget, bridge = shell
    ready(qtbot, widget, bridge)
    widget.set_page(2)
    previous = next(item for item in reversed(bridge.sent) if item["operation"] == "memories")
    bridge.privacy_epoch += 1
    bridge.privacy_changed.emit(bridge.privacy_epoch)
    qtbot.waitUntil(lambda: any(item["operation"] == "memories" and item["privacy_epoch"] == 1 for item in bridge.sent))
    bridge.answer(previous, {"items": [{"id": "old", "content": "旧代际正文"}]})
    qtbot.wait(10)
    assert widget.memory_page.page.items == []
    current = next(item for item in reversed(bridge.sent) if item["operation"] == "memories")
    bridge.answer(current, {"items": [{"id": "current", "content": "当前可见记忆"}]})
    qtbot.waitUntil(lambda: widget.memory_page.page.items != [])
    assert widget.memory_page.page.items[0]["id"] == "current"
