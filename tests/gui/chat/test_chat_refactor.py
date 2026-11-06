"""Development-only prepared GUI acceptance; not run in the implementation phase."""
from typing import Any
from uuid import uuid4
import pytest
from PySide6.QtCore import QCoreApplication, QObject, Signal, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QPushButton
from deskpal.gui.chat import ChatCoordinator, ChatView
from deskpal.gui.task_card import TaskCard
from deskpal.gui.channel import Channel
from deskpal.gui.widgets import MessageInput

class FakeBridge(QObject):
    state_changed = Signal(str)
    reply = Signal(object)
    image_reply = Signal(object)
    privacy_changed = Signal(int)
    def __init__(self) -> None:
        super().__init__(); self.state = "Ready"; self.privacy_epoch = 0; self.calls: list[tuple[str, dict[str, Any], int, str]] = []
    def send(self, operation: str, parameters: dict[str, Any], *, generation: int = 0) -> str:
        identity = str(uuid4())
        if operation in {"conversation_clear", "conversation_delete"}:
            self.privacy_epoch += 1; self.privacy_changed.emit(self.privacy_epoch)
        self.calls.append((operation, dict(parameters), generation, identity)); return identity
    def send_image(self, operation: str, parameters: dict[str, Any], generation: int = 0) -> str:
        return self.send(operation, parameters, generation=generation)
    def respond(self, call: tuple[str, dict[str, Any], int, str], data: dict[str, Any]) -> None:
        operation, parameters, generation, identity = call
        self.reply.emit({"request_id": identity, "operation": operation, "generation": generation, "privacy_epoch": self.privacy_epoch, "ok": True, "data": data})

def coordinator() -> tuple[FakeBridge, ChatCoordinator]:
    bridge = FakeBridge(); state = ChatCoordinator(bridge); state.timer.stop()
    state.default_id = "daily"; state.model_ready = True
    state.conversations = {key: {"id": key, "title": key, "revision": 1, "context_epoch": 1} for key in ["daily", "project"]}
    return bridge, state

def test_redacted_history_has_explanation_and_user_author_is_on_right(qtbot: Any) -> None:
    from PySide6.QtWidgets import QFrame, QLabel
    bridge, state = coordinator(); view = ChatView(state); qtbot.addWidget(view)
    view.set_conversation('daily'); view.resize(640, 700); view.show()
    state._timeline('daily', {'items': [
        {'id': 'gone', 'kind': 'user_message', 'body': '', 'redacted_at': 'now', 'sequence': 1},
        {'id': 'new', 'kind': 'user_message', 'body': '仍然可见的新消息', 'sequence': 2},
        {'id': 'reply', 'kind': 'assistant_message', 'body': '正常回复', 'sequence': 3}]}, False)
    qtbot.waitUntil(lambda: len(view.findChildren(QFrame, 'user_message')) == 1)
    frame = view.findChild(QFrame, 'user_message')
    avatar = frame.findChild(QLabel, 'message_user_avatar'); name = frame.findChild(QLabel, 'message_author')
    qtbot.waitUntil(lambda: avatar.x() > name.x())
    assert view.findChild(QLabel, 'chat_redaction_notice') is not None
    assert any(label.text() == '仍然可见的新消息' for label in frame.findChildren(QLabel))


def test_submission_receipt_clears_stale_unknown_command_notice(qtbot: Any) -> None:
    bridge, state = coordinator(); notices = []; state.notice.connect(notices.append)
    state.edit_draft('daily', '新的要求'); state.submit('daily')
    call = next(call for call in reversed(bridge.calls) if call[0] == 'submit')
    state.notice.emit('后台尚无此命令回执')
    bridge.respond(call, {'accepted': True, 'task_id': 'new-task'})
    qtbot.waitUntil(lambda: not state.submissions)
    assert notices[-1] == ''


def test_new_dispatcher_state_releases_accepted_confirmation(qtbot: Any) -> None:
    bridge, state = coordinator()
    state.reviews['t'] = {'task_revision': 2, 'confirmation_revision': 3, 'confirmation_state': 'accepted', 'plan_hash': 'p'}
    state.accepted['t'] = (2, 'p')
    state.set_review('t', {'task_revision': 2, 'confirmation_revision': 4, 'confirmation_state': 'none',
                         'plan_hash': 'p', 'available_actions': ['confirm'], 'status': 'NeedsReview'})
    assert state.reviews['t']['confirmation_state'] == 'none'
    assert 'confirm' in state.reviews['t']['available_actions']
    assert 't' not in state.accepted


def test_rejected_task_command_does_not_keep_retrying_stale_parameters(qtbot: Any) -> None:
    bridge, state = coordinator()
    state.task_commands['t'] = {'command_id': 'cmd', 'action': 'confirm', 'parameters': {}, 'at': 0}
    state.task_pending.add('t')
    state._task_receipt('t', {'_error': {'code': 'Blocked', 'message': '计划已失效'}})
    state.reconcile_task('t')
    call = next(call for call in reversed(bridge.calls) if call[0] == 'command_status')
    bridge.respond(call, {'known': False, 'accepted': False})
    qtbot.waitUntil(lambda: 't' not in state.task_commands)
    assert 't' not in state.task_pending


def test_late_reconciliation_cannot_overwrite_a_new_task_command(qtbot: Any) -> None:
    bridge, state = coordinator(); notices = []; state.notice.connect(notices.append)
    state.task_commands['t'] = {'command_id': 'old', 'action': 'confirm', 'parameters': {}, 'at': 0}
    state.reconcile_task('t')
    call = next(call for call in reversed(bridge.calls) if call[0] == 'command_status')
    replacement = {'command_id': 'new', 'action': 'cancel', 'parameters': {}, 'at': 1}
    state.task_commands['t'] = replacement; state.task_pending.add('t')
    bridge.respond(call, {'known': False, 'accepted': False})
    qtbot.waitUntil(lambda: 'task-receipt:t' not in state._reads)
    assert state.task_commands['t'] is replacement
    assert 't' in state.task_pending and not notices


def test_two_views_share_revision_draft_and_submit_once(qtbot: Any) -> None:
    bridge, state = coordinator(); first = ChatView(state); second = ChatView(state, compact=True)
    qtbot.addWidget(first); qtbot.addWidget(second); first.set_conversation("daily"); second.set_conversation("daily")
    first.input.setPlainText("今天过得怎样？")
    assert second.input.toPlainText() == "今天过得怎样？"
    first._send(); second._send()
    sent = [call for call in bridge.calls if call[0] == "submit"]
    assert len(sent) == 1
    first.set_conversation("project")
    bridge.respond(sent[0], {"accepted": True, "task_id": "task-one"})
    qtbot.waitUntil(lambda: state.draft("daily").text == "")
    assert first.conversation_id == "project" and second.conversation_id == "daily"

def test_hidden_views_do_not_poll_their_conversation(qtbot: Any) -> None:
    bridge, state = coordinator(); view = ChatView(state); qtbot.addWidget(view); view.set_conversation("daily"); view.show()
    assert id(view) in state._views
    view.hide(); state._reads.clear(); bridge.calls.clear(); state.poll()
    assert not any(call[0] == "conversation_timeline" for call in bridge.calls)

def test_old_history_is_bounded_but_reachable_without_poll_reset(qtbot: Any) -> None:
    bridge, state = coordinator(); view = ChatView(state); qtbot.addWidget(view); view.set_conversation("daily")
    state._timeline("daily", {"items": [{"id": "tail", "sequence": 2000, "kind": "assistant_message", "body": "最新"}], "has_more": True, "next_cursor": 2000}, False)
    for page in range(12):
        view._load_older(); call = [call for call in bridge.calls if call[0] == "conversation_timeline"][-1]
        bridge.respond(call, {"items": [{"id": f"old-{page}", "sequence": 1000 - page, "kind": "assistant_message", "body": "旧记录"}], "has_more": True, "next_cursor": 1000 - page})
        qtbot.waitUntil(lambda: not view._page_reading)
    assert view._history_items[0]["id"] == "old-11" and len(view._history_items) == 1
    assert state.timelines["daily"][0]["id"] == "tail"
    view._latest()
    assert bridge.calls[-1][0] == "conversation_timeline" and "before" not in bridge.calls[-1][1]

def review(revision: int = 2, confirmation_revision: int = 1) -> dict[str, Any]:
    return {"task_id": "t", "task_revision": revision, "confirmation_revision": confirmation_revision, "status": "Waiting", "plan_hash": "hash", "title": "删除文件", "steps": [{"tool_name": "document_delete", "display_name": "删除文件", "arguments": {"path": "old.txt"}, "fields": [{"name": "path", "label": "文件路径"}]}], "missing_permissions": {"ordinary_tools": [], "high_risk_tools": ["document_delete"]}, "available_actions": ["confirm", "cancel"], "confirmation_state": "none"}

def test_shared_grants_and_accepted_confirmation_never_reappear(qtbot: Any) -> None:
    bridge, state = coordinator(); state.set_review("t", review()); first = TaskCard(state, "t"); second = TaskCard(state, "t")
    qtbot.addWidget(first); qtbot.addWidget(second); first.show(); second.show()
    confirm = [button for button in first.findChildren(QPushButton) if button.text() == "确认所选授权"][0]
    assert not confirm.isEnabled()
    state.set_grant("t", "document_delete", True); state.set_review("t", review())
    assert state.grants["t"] == {"document_delete"}
    accepted = review(confirmation_revision=2); accepted["confirmation_state"] = "accepted"; accepted["available_actions"] = ["cancel"]
    state.set_review("t", accepted); state.set_review("t", review())
    assert state.reviews["t"]["confirmation_state"] == "accepted"
    qtbot.waitUntil(lambda: not [button for button in second.findChildren(QPushButton) if button.text() == "确认所选授权" and button.isVisible()])

def test_clear_keeps_its_own_new_epoch_receipt(qtbot: Any) -> None:
    bridge = FakeBridge(); channel = Channel(bridge); replies: list[dict[str, Any]] = []
    channel.send("conversation_clear", {"id": "daily", "expected_revision": 1}, replies.append, mutation=True)
    call = bridge.calls[-1]
    qtbot.waitUntil(lambda: len(channel.pending) == 1)
    bridge.respond(call, {"accepted": True, "status": "Completed"})
    qtbot.waitUntil(lambda: len(replies) == 1)
    assert replies[0]["status"] == "Completed"

def test_enter_shift_enter_and_ime_commit_guard(qtbot: Any) -> None:
    widget = MessageInput(); qtbot.addWidget(widget); sent: list[bool] = []; widget.send_requested.connect(lambda: sent.append(True)); widget.show()
    widget.setPlainText("你好"); qtbot.keyClick(widget, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert not sent and "\n" in widget.toPlainText()
    widget.inputMethodEvent(QInputMethodEvent("候选", [])); qtbot.keyClick(widget, Qt.Key.Key_Return)
    assert not sent
    widget.inputMethodEvent(QInputMethodEvent("", [])); qtbot.keyClick(widget, Qt.Key.Key_Return)
    assert not sent
    qtbot.waitUntil(lambda: not widget._ime_commit_guard); qtbot.keyClick(widget, Qt.Key.Key_Return)
    assert sent == [True]


def test_appearance_rejects_old_profile_image_and_reset_clears(qtbot: Any) -> None:
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice
    from PySide6.QtGui import QPixmap, QColor
    from deskpal.gui.appearance import AppearanceController
    bridge = FakeBridge(); controller = AppearanceController(bridge)
    pixmap = QPixmap(8, 8); pixmap.fill(QColor("red")); data = QByteArray(); buffer = QBuffer(data); buffer.open(QIODevice.OpenModeFlag.WriteOnly); assert pixmap.save(buffer, "PNG"); buffer.close()
    controller.profiles = {"character_avatar": {"slot": "character_avatar", "effective_asset_id": "old", "revision": 1, "display_mode": "contain", "crop": [0, 0, 1, 1]}}
    controller.preview("character_avatar", "old"); old_call = bridge.calls[-1]
    controller.profiles["character_avatar"].update(effective_asset_id="new", revision=2)
    controller.preview("character_avatar", "new"); new_call = bridge.calls[-1]
    def response(call: Any) -> None:
        bridge.image_reply.emit({"request_id": call[3], "generation": call[2], "privacy_epoch": 0, "ok": True, "image": {"png": bytes(data)}})
    response(old_call); assert "character_avatar" not in controller.images
    response(new_call); assert not controller.images["character_avatar"].isNull()
    controller.refresh(); metadata = [call for call in bridge.calls if call[0] == "appearance_snapshot"][-1]
    bridge.respond(metadata, {"profiles": [{"slot": "character_avatar", "effective_asset_id": None, "revision": 3}]})
    qtbot.waitUntil(lambda: "character_avatar" not in controller.images)


@pytest.mark.parametrize("slot", ["user_avatar", "character_avatar"])
def test_two_chat_views_sync_message_avatar_apply_replace_reset_and_clear(qtbot: Any, slot: str) -> None:
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice
    from PySide6.QtGui import QColor, QPixmap
    from PySide6.QtWidgets import QLabel
    from shiboken6 import delete, isValid
    bridge, state = coordinator(); first = ChatView(state); second = ChatView(state, compact=True)
    # This test destroys first explicitly; keep its ownership out of qtbot.
    qtbot.addWidget(second)
    try:
        items = [{"id": "user", "kind": "user_message", "body": "我的消息"},
                 {"id": "assistant", "kind": "assistant_message", "body": "角色消息"}]
        state._timeline("daily", {"items": items}, False)
        controller = state.appearance
        index = 0 if slot == "user_avatar" else 1
        marker = '你' if slot == 'user_avatar' else 'D'

        def avatar(view: ChatView) -> QLabel:
            label = view.messages.itemAt(index).widget().findChild(QLabel, "message_" + slot)
            assert label is not None
            return label

        def read_profile(asset_id: str | None, revision: int) -> Any:
            controller.refresh(); metadata = [call for call in bridge.calls if call[0] == "appearance_snapshot"][-1]
            bridge.respond(metadata, {"profiles": [{"slot": slot, "effective_asset_id": asset_id,
                "revision": revision, "display_mode": "contain", "crop": [0, 0, 1, 1]}]})
            qtbot.waitUntil(lambda: not controller._reading)
            return [call for call in bridge.calls if call[0] == "asset_preview"][-1] if asset_id else None

        def image(call: Any, color: str) -> None:
            pixmap = QPixmap(8, 4); pixmap.fill(QColor(color))
            data = QByteArray(); buffer = QBuffer(data); buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            assert pixmap.save(buffer, "PNG"); buffer.close()
            bridge.image_reply.emit({"request_id": call[3], "generation": call[2], "privacy_epoch": 0,
                                     "ok": True, "image": {"png": bytes(data)}})

        def assert_color(color: str) -> None:
            for view in (first, second):
                pixmap = avatar(view).pixmap()
                assert not pixmap.isNull() and pixmap.width() <= 24 and pixmap.height() <= 24
                rendered = pixmap.toImage()
                assert rendered.pixelColor(rendered.width() // 2, rendered.height() // 2) == QColor(color)
                # The rectangular source remains contained instead of stretching.
                assert rendered.pixelColor(0, 0).alpha() == 0
            if slot == "character_avatar":
                assert not first.avatar.pixmap().isNull() and not second.avatar.pixmap().isNull()

        assert avatar(first).text() == avatar(second).text() == marker
        image(read_profile("first", 1), "red"); assert_color("red")
        stale = read_profile("stale", 2); replacement = read_profile("replacement", 3)
        image(replacement, "blue"); image(stale, "red"); assert_color("blue")
        # Rebuild both pages from shared images, then delete a retired frame before
        # another image notification to exercise the active-widget traversal.
        retired = first.messages.itemAt(index).widget()
        items[0]["body"] = "重绘后的消息"
        state._timeline("daily", {"items": items}, False); delete(retired)
        assert_color("blue")
        stale = read_profile("reset-pending", 4); read_profile(None, 5); image(stale, "red")
        assert avatar(first).text() == avatar(second).text() == marker and slot not in controller.images
        image(read_profile("restored", 6), "red"); assert_color("red")
        stale = read_profile("clear-pending", 7)
        controller.refresh(); stale_metadata = [call for call in bridge.calls if call[0] == "appearance_snapshot"][-1]
        controller.clear(); image(stale, "blue")
        bridge.respond(stale_metadata, {"profiles": [{"slot": slot, "effective_asset_id": "revived", "revision": 8}]})
        QCoreApplication.processEvents()
        assert not controller.images and not controller.pending and not controller.profiles
        assert avatar(first).text() == avatar(second).text() == marker
        delete(first); controller.clear()
        assert avatar(second).text() == marker
        if slot == "character_avatar": assert second.avatar.text() == 'D'
    finally:
        if isValid(first): delete(first)


def test_literal_review_values_and_full_copy_do_not_translate_payload(qtbot: Any) -> None:
    from PySide6.QtWidgets import QTreeWidget
    from deskpal.gui.widgets import fill_tree
    tree = QTreeWidget(); tree.setColumnCount(2); qtbot.addWidget(tree)
    assert fill_tree(tree, {"text": "Completed", "future_flag": "true", "status": "Completed"})
    root = tree.topLevelItem(0)
    assert root.child(0).text(1) == "Completed"
    assert root.child(1).text(1) == "true"
    assert root.child(2).text(1) == "已完成"
    assert root.child(0).data(1, Qt.ItemDataRole.UserRole) == "Completed"
    assert root.child(1).data(1, Qt.ItemDataRole.UserRole) == "true"
    assert root.child(2).data(1, Qt.ItemDataRole.UserRole) == "Completed"


def test_l0_only_needs_review_can_explicitly_continue_without_grants(qtbot: Any) -> None:
    bridge, state = coordinator(); value = review()
    value.update(status="NeedsReview", steps=[{"tool_name": "time", "arguments": {}}],
                 missing_permissions={"ordinary_tools": [], "high_risk_tools": []})
    state.set_review("t", value); card = TaskCard(state, "t"); qtbot.addWidget(card)
    button = next(button for button in card.findChildren(QPushButton) if button.text() == "确认并继续原计划")
    assert button.isEnabled(); button.click()
    sent = [call for call in bridge.calls if call[0] == "confirm"]
    assert len(sent) == 1 and sent[0][1]["ordinary_tools"] == sent[0][1]["high_risk_tools"] == []
    assert sent[0][1]["expected_revision"] == value["task_revision"]


def test_incomplete_argument_scope_blocks_confirmation_and_unknown_values_survive(qtbot: Any) -> None:
    from PySide6.QtWidgets import QTreeWidget
    bridge, state = coordinator(); value = review(); value["steps"][0]["arguments"] = {"future_field": ["x"] * 4100}
    state.set_review("t", value); state.set_grant("t", "document_delete", True)
    card = TaskCard(state, "t"); qtbot.addWidget(card)
    assert not any(button.isEnabled() for button in card.findChildren(QPushButton) if button.text() == "确认所选授权")
    assert any(tree.property("deskpal_complete") is False for tree in card.findChildren(QTreeWidget))
    assert not any(call[0] == "confirm" for call in bridge.calls)


def test_review_failure_reloads_and_clears_previous_authority(qtbot: Any) -> None:
    bridge, state = coordinator(); state.set_review("t", review()); state.set_grant("t", "document_delete", True)
    card = TaskCard(state, "t"); qtbot.addWidget(card)
    state._review_failed("t", {"_error": {"message": "请重新读取"}})
    reload = next(button for button in card.findChildren(QPushButton) if button.text() == "重新读取审核内容")
    reload.click()
    assert any(call[0] == "task_review" and call[1]["task_id"] == "t" for call in bridge.calls)
    assert not state.reviews["t"]["available_actions"]


def test_task_navigation_uses_indexed_history_page_without_showing_hidden_card(qtbot: Any) -> None:
    bridge, state = coordinator(); view = ChatView(state); qtbot.addWidget(view)
    view.set_conversation("daily", "old-task")
    location = next(call for call in bridge.calls if call[0] == "conversation_task_location")
    assert location[1] == {"id": "daily", "task_id": "old-task"}
    bridge.respond(location, {"before": 122, "conversation_id": "daily", "task_id": "old-task"})
    qtbot.waitUntil(lambda: any(call[0] == "conversation_timeline" for call in bridge.calls))
    page = [call for call in bridge.calls if call[0] == "conversation_timeline"][-1]
    assert page[1] == {"id": "daily", "before": 122, "limit": 50}
    bridge.respond(page, {"items": [{"id": "old-task:assistant", "sequence": 121, "kind": "assistant_message", "body": "原消息"},
                                   {"id": "old-task:task_card", "sequence": 120, "kind": "task_card", "task_id": "old-task", "visible": False}], "next_cursor": 100, "has_more": True})
    qtbot.waitUntil(lambda: bool(view._history_items))
    assert view.latest_button.isHidden() is False
    assert not view.findChildren(TaskCard)
    assert not state.timelines.get("daily")


def test_appearance_busy_refusal_recovers_without_losing_preview(qtbot: Any, monkeypatch: Any) -> None:
    from deskpal.gui.settings import AppearanceSettings
    bridge, state = coordinator(); widget = AppearanceSettings(state, None); qtbot.addWidget(widget)
    state.appearance.profiles = {"user_avatar": {"slot": "user_avatar", "revision": 1, "crop": [0, 0, 1, 1]}}
    widget.token = "preview-token"
    with monkeypatch.context() as patch:
        patch.setattr(widget.channel, "send", lambda *args, **kwargs: None)
        widget._apply()
        assert not widget._busy and widget.token == "preview-token" and widget.slot.isEnabled()
        widget._reset()
        assert not widget._busy and widget.token == "preview-token"


def test_appearance_reset_clears_old_preview_and_reload_restores_default_crop(qtbot: Any) -> None:
    from PySide6.QtGui import QPixmap
    from deskpal.gui.settings import AppearanceSettings
    bridge, state = coordinator(); widget = AppearanceSettings(state, None); qtbot.addWidget(widget)
    state.appearance.profiles = {"user_avatar": {"slot": "user_avatar", "revision": 1, "crop": [0, 0, 1, 1]}}
    widget.preview.pixmap_value = QPixmap(8, 8); widget.token = "preview-token"
    widget.crop[2].setValue(80); widget._reset()
    reset = [call for call in bridge.calls if call[0] == "appearance_reset"][-1]
    assert not widget.slot.isEnabled() and not widget.choose.isEnabled()
    bridge.respond(reset, {"slot": "user_avatar", "revision": 2})
    qtbot.waitUntil(lambda: not widget._busy)
    assert widget.preview.pixmap_value.isNull() and not widget.token
    metadata = [call for call in bridge.calls if call[0] == "appearance_snapshot"][-1]
    bridge.respond(metadata, {"profiles": [{"slot": "user_avatar", "revision": 2, "effective_asset_id": None, "display_mode": "contain", "crop": [0, 0, 1, 1]}]})
    qtbot.waitUntil(lambda: widget.crop[2].value() == 100)
    assert [spin.value() for spin in widget.crop] == [0, 0, 100, 100]


def test_review_lru_evicts_unselected_state_but_keeps_unsent_grants(qtbot: Any) -> None:
    from deskpal.gui.chat import MAX_REVIEWS
    bridge, state = coordinator()
    for index in range(MAX_REVIEWS): state.set_review(str(index), review())
    state.set_grant("0", "document_delete", True)
    state.accepted["1"] = (2, "hash"); state.grants["1"] = set()
    state.set_review("new", review())
    assert len(state.reviews) == MAX_REVIEWS and "0" in state.reviews and "new" in state.reviews
    assert "1" not in state.reviews and "1" not in state.accepted and "1" not in state.grants


def test_review_capacity_preserves_all_pinned_selections_and_bounds_queue(qtbot: Any) -> None:
    from deskpal.gui.chat import MAX_REVIEWS
    bridge, state = coordinator(); notices: list[str] = []; state.notice.connect(notices.append)
    for index in range(MAX_REVIEWS):
        state.set_review(str(index), review()); state.set_grant(str(index), "document_delete", True)
    state.set_review("extra", review()); state.review("extra")
    assert len(state.reviews) == MAX_REVIEWS and "extra" not in state.reviews and notices
    assert all(state.grants[str(index)] == {"document_delete"} for index in range(MAX_REVIEWS))
    state.clear_sensitive(); state.available = False
    for index in range(MAX_REVIEWS + 20): state.review(str(index))
    assert len(state._review_queue) == MAX_REVIEWS


def test_draft_budget_rejection_never_sends_old_text_behind_new_composer(qtbot: Any) -> None:
    from deskpal.gui.chat import MAX_DRAFTS
    bridge, state = coordinator()
    for index in range(MAX_DRAFTS): assert state.edit_draft(str(index), "x" * 4000)
    assert not state.edit_draft("extra", "new")
    assert len(state.drafts) == MAX_DRAFTS
    state.conversations["0"] = {"id": "0", "title": "会话", "context_epoch": 1}
    view = ChatView(state); qtbot.addWidget(view); view.set_conversation("0")
    view.input.setPlainText("中" * 10000)
    assert view._draft_rejected and not view.send.isEnabled()
    assert view.input.toPlainText() == state.draft("0").text == "x" * 4000
    view._send()
    assert not any(call[0] == "submit" for call in bridge.calls)
    assert state.edit_draft("1", "")
    assert state.edit_draft("extra", "new")
    assert all(state.drafts[str(index)].text == "x" * 4000 for index in range(2, MAX_DRAFTS))


def test_archived_conversation_preserves_draft_and_blocks_enter_send(qtbot: Any) -> None:
    bridge, state = coordinator(); state.conversations["project"]["archived_at"] = "2026-10-25"
    view = ChatView(state); qtbot.addWidget(view); view.set_conversation("project"); view.input.setPlainText("归档草稿")
    assert view.input.isReadOnly() and not view.send.isEnabled() and "已归档" in view.status.text()
    view._send(); state.submit("project")
    assert state.draft("project").text == "归档草稿" and not any(call[0] == "submit" for call in bridge.calls)


@pytest.mark.parametrize("lifecycle", ["Initializing", "CleanupBlocked", "Closing", "Closed", "StartupFailed"])
def test_unavailable_chat_cannot_change_or_recreate_drafts(qtbot: Any, lifecycle: str) -> None:
    bridge, state = coordinator(); view = ChatView(state); qtbot.addWidget(view)
    view.input.setPlainText("已有草稿")
    state.set_state(lifecycle)
    before = {key: (draft.text, draft.revision) for key, draft in state.drafts.items()}
    assert view.input.isReadOnly() and not view.send.isEnabled()
    assert not state.edit_draft("daily", "不可写入")
    assert not state.edit_draft("new", "不可新建")
    assert {key: (draft.text, draft.revision) for key, draft in state.drafts.items()} == before
    state.clear_sensitive()
    assert not state.edit_draft("daily", "清理后不可恢复")
    view.input.setPlainText("程序性迟到编辑")
    state.submit("daily")
    assert not state.drafts and not any(call[0] == "submit" for call in bridge.calls)


def test_ready_chat_without_model_still_accepts_drafts(qtbot: Any) -> None:
    _, state = coordinator(); state.model_ready = False
    view = ChatView(state); qtbot.addWidget(view)
    assert not view.input.isReadOnly() and not view.send.isEnabled()
    view.input.setPlainText("设置模型前保留草稿")
    assert state.drafts["daily"].text == "设置模型前保留草稿"


def test_same_conversation_two_windows_keep_independent_history_pages(qtbot: Any) -> None:
    from PySide6.QtWidgets import QLabel
    bridge, state = coordinator(); first = ChatView(state); second = ChatView(state, compact=True)
    qtbot.addWidget(first); qtbot.addWidget(second); first.set_conversation("daily"); second.set_conversation("daily")
    state._timeline("daily", {"items": [{"id": "tail", "sequence": 200, "kind": "assistant_message", "body": "尾页"}], "has_more": True, "next_cursor": 200}, False)
    first._load_older(); call = [call for call in bridge.calls if call[0] == "conversation_timeline"][-1]
    bridge.respond(call, {"items": [{"id": "old", "sequence": 150, "kind": "assistant_message", "body": "早期页"}], "has_more": True, "next_cursor": 150})
    qtbot.waitUntil(lambda: bool(first._history_items))
    assert second._history_items is None
    state._timeline("daily", {"items": [{"id": "new", "sequence": 201, "kind": "assistant_message", "body": "新尾页"}]}, False)
    assert first._history_items[0]["body"] == "早期页" and second._history_items is None
    assert any(label.text() == "新尾页" and not label.isHidden() for label in second.findChildren(QLabel))
    assert first.new_message.isHidden() is False
    first._latest(); assert first._history_items is None
    assert state.timelines["daily"][0]["body"] == "新尾页"


def test_closed_invalidates_same_epoch_history_and_page_callbacks(qtbot: Any) -> None:
    bridge, state = coordinator(); view = ChatView(state); qtbot.addWidget(view); view.set_conversation("daily")
    state._timeline("daily", {"items": [], "has_more": True, "next_cursor": 100}, False)
    view._load_older(); stale = [call for call in bridge.calls if call[0] == "conversation_timeline"][-1]
    bridge.state = "Closed"; bridge.state_changed.emit("Closed"); state.clear_sensitive()
    bridge.respond(stale, {"items": [{"id": "stale", "sequence": 50, "kind": "assistant_message", "body": "不可恢复"}]})
    qtbot.waitUntil(lambda: not state.channel.pending)
    assert view._history_items is None and not state.timelines


@pytest.mark.parametrize("mode", ["bottom", "restore", "preserve", "card"])
def test_destroyed_chat_drops_queued_scroll_callbacks(qtbot: Any, mode: str) -> None:
    from shiboken6 import delete, isValid
    _, state = coordinator(); view = ChatView(state)
    try:
        QCoreApplication.processEvents()
        area = view.area
        bar = area.verticalScrollBar(); bar.setRange(0, 1000); bar.setValue(100 if mode == "preserve" else 1000)
        if mode == "restore": view._restore_position = 275
        if mode == "card":
            view.task_id = "t"
            state.timelines["daily"] = [{"id": "card", "kind": "task_card", "task_id": "t"}]
        else:
            state.timelines["daily"] = [{"id": "message", "kind": "assistant_message", "body": "排队滚动"}]
        view._timeline("daily")
        delete(view)
        assert not isValid(view) and not isValid(area)
        # Run the real queued callbacks; pytest-qt reports any stale QObject access.
        QCoreApplication.processEvents()
    finally:
        if isValid(view): delete(view)


def test_destroyed_task_card_drops_queued_location_with_live_chat(qtbot: Any, monkeypatch: Any) -> None:
    from shiboken6 import delete, isValid
    _, state = coordinator(); view = ChatView(state); qtbot.addWidget(view)
    QCoreApplication.processEvents()
    located: list[TaskCard] = []
    # Restore native methods while the owning chat is still alive. qtbot's
    # teardown may destroy its children before the monkeypatch fixture ends.
    with monkeypatch.context() as patch:
        patch.setattr(view.area, "ensureWidgetVisible", located.append)
        view.task_id = "t"
        state.timelines["daily"] = [{"id": "card", "kind": "task_card", "task_id": "t"}]
        view._timeline("daily"); card = view.findChildren(TaskCard)[0]
        delete(card)
        assert isValid(view) and not isValid(card)
        QCoreApplication.processEvents()
        assert located == []


@pytest.mark.parametrize("navigation", ["conversation", "history", "redraw"])
def test_queued_scroll_never_restores_retired_page(qtbot: Any, monkeypatch: Any, navigation: str) -> None:
    _, state = coordinator(); view = ChatView(state); qtbot.addWidget(view)
    QCoreApplication.processEvents()
    state.timelines["daily"] = [{"id": "old", "kind": "assistant_message", "body": "旧位置"}]
    state.cursors["daily"] = 100
    bar = view.area.verticalScrollBar(); bar.setRange(0, 1000); bar.setValue(125)
    positions: list[int] = []
    with monkeypatch.context() as patch:
        patch.setattr(bar, "setValue", positions.append)
        view._restore_position = 600; view._timeline("daily")
        if navigation == "conversation":
            view.positions["project"] = 200; view.set_conversation("project")
        elif navigation == "history":
            view._load_older()
        else:
            state.timelines["daily"] = [{"id": "new", "kind": "assistant_message", "body": "新位置"}]
            view._restore_position = 200; view._timeline("daily")
        QCoreApplication.processEvents()
        assert positions == ([] if navigation == "history" else [200])


def test_task_payload_enum_named_leaves_keep_literal_display_and_copy(qtbot: Any) -> None:
    from PySide6.QtWidgets import QTreeWidget
    _, state = coordinator(); value = review()
    value["steps"][0]["arguments"] = {"nested": {"status": "Completed", "use_policy": "local_only", "flag": True}}
    value["task"] = {"result": {"status": "Completed", "text": "true"}}
    state.set_review("t", value); card = TaskCard(state, "t"); qtbot.addWidget(card)
    trees = card.findChildren(QTreeWidget)
    for tree in trees[:2]:
        nodes = [tree.topLevelItem(index) for index in range(tree.topLevelItemCount())]
        while nodes:
            node = nodes.pop(); raw = node.data(1, Qt.ItemDataRole.UserRole)
            if raw is not None: assert node.text(1) == raw
            nodes.extend(node.child(index) for index in range(node.childCount()))
    # 参数字面值仍可审阅；任务/结果原始字段只在统一执行侧栏中查看。
    from deskpal.gui.tool_results import ToolResultsPanel
    import json
    panel = ToolResultsPanel(); qtbot.addWidget(panel); panel.update_review(value)
    panel.diagnostic.click()
    raw = json.loads(panel.raw_body.toPlainText())
    assert raw['status'] == 'Waiting' and raw['task']['result']['status'] == 'Completed'


def test_archived_conversation_blocks_original_submission_retry(qtbot: Any) -> None:
    bridge, state = coordinator(); state.edit_draft("project", "保留原命令")
    state.submit("project"); command = next(iter(state.submissions))
    state.submissions[command]["retryable"] = True
    state.conversations["project"]["archived_at"] = "2026-10-26"
    before = len([call for call in bridge.calls if call[0] == "submit"])
    state.retry_submission(command)
    assert len([call for call in bridge.calls if call[0] == "submit"]) == before
    assert state.submissions[command]["retryable"] and state.draft("project").text == "保留原命令"


def test_channel_closed_guard_precedes_queued_lifecycle_invalidation(qtbot: Any) -> None:
    bridge = FakeBridge(); channel = Channel(bridge); received: list[dict[str, Any]] = []
    channel.send("settings", {}, received.append); call = bridge.calls[-1]
    bridge.state = "Closed"
    # Simulate a same-epoch reply reaching the handler before its queued state slot.
    channel._reply({"request_id": call[3], "generation": call[2], "privacy_epoch": 0, "ok": True, "data": {"llm_ready": True}})
    assert not received and not channel.pending
    assert channel.send("settings", {}, received.append) is None
