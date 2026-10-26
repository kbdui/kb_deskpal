"""HM06: real offscreen Qt, synthetic DTOs and deliberately reordered replies.

The bridge follows the existing companion GUI fake's queued transport contract.
It is local to this module so importing tests cannot alter pytest collection.
"""

from copy import deepcopy

from PySide6.QtCore import QObject, QSettings, QTimer, Signal
from PySide6.QtWidgets import QLabel

import pytest

from deskpal.gui.chat import ChatCoordinator, ChatView
from deskpal.gui.settings import InteractionSettings
from deskpal.gui.preferences import GuiPreferences
from deskpal.gui.window import MainWindow


PERMISSIONS = ("custom_persona", "preferences", "cue", "affect", "history")
IDENTITY = {"display_name": "Deskpal", "role_kind": "assistant", "self_reference": "neutral",
            "greeting_style": "none", "followup_style": "none"}


def dto(**values):
    return {"schema_version": 1, "session_id": "synthetic-session", "memory_epoch": 0, **values}


def interaction(*, revision=1, conversation_id="default", candidates=True, allowed=False,
                display_name="Deskpal", memory_epoch=0, interaction_epoch=0):
    return dto(revision=revision, memory_epoch=memory_epoch, interaction_epoch=interaction_epoch,
               cloud_permissions=dict.fromkeys(PERMISSIONS, allowed),
               identity={**IDENTITY, "display_name": display_name}, identity_use_policy="local_only",
               candidates=([{"id": "synthetic-candidate", "conversation_id": conversation_id,
                             "key": "verbosity", "value": "brief", "scope": "session"}]
                           if candidates else []))


def conversation(identity, *, revision=1):
    return {"id": identity, "conversation_id": identity, "title": identity,
            "revision": revision, "context_epoch": 0, "timeline_revision": revision}


def transcript(conversation_id, text, *, sequence=1, memory_epoch=0):
    return dto(conversation=conversation(conversation_id), memory_epoch=memory_epoch,
               items=[{"id": "message-" + conversation_id, "sequence": sequence,
                       "kind": "user_message", "revision": 1, "body": text}],
               has_more=False, next_cursor=None)


class Bridge(QObject):
    reply = Signal(object)
    image_reply = Signal(object)
    state_changed = Signal(str)
    privacy_changed = Signal(int)

    def __init__(self):
        super().__init__()
        self.state = "Ready"
        self.privacy_epoch = 0
        self.sent = []
        self.delivered = 0

    def send(self, operation, parameters=None, *, generation=0):
        request = {"request_id": f"request-{len(self.sent) + 1}", "operation": operation,
                   "parameters": deepcopy(parameters or {}), "generation": generation,
                   "privacy_epoch": self.privacy_epoch}
        self.sent.append(request)
        return request["request_id"]

    def send_image(self, operation, parameters, generation=0):
        return self.send(operation, parameters, generation=generation)

    def requests_for(self, operation):
        return [request for request in self.sent if request["operation"] == operation]

    def answer(self, qtbot, request, data, **overrides):
        envelope = {key: request[key] for key in ("request_id", "operation", "generation", "privacy_epoch")}
        envelope.update(ok=True, data=deepcopy(data))
        envelope.update(overrides)
        delivered = self.delivered

        def emit():
            self.reply.emit(envelope)
            self.delivered += 1

        QTimer.singleShot(0, emit)
        qtbot.waitUntil(lambda: self.delivered == delivered + 1, timeout=2000)
        # A second queued turn ensures consumers processed the queued signal.
        drained = []
        QTimer.singleShot(0, lambda: drained.append(True))
        qtbot.waitUntil(lambda: bool(drained), timeout=2000)

    def set_state(self, state):
        self.state = state
        self.state_changed.emit(state)


@pytest.fixture
def panel(qtbot):
    bridge = Bridge()
    coordinator = ChatCoordinator(bridge)
    coordinator.timer.stop()
    coordinator.default_id = "default"
    coordinator.conversations = {identity: conversation(identity) for identity in
                                 ("default", "conversation-a", "conversation-b")}
    widget = InteractionSettings(coordinator)
    qtbot.addWidget(widget)
    coordinator.conversations_changed.emit()
    yield widget, bridge, coordinator
    coordinator.timer.stop()


@pytest.fixture
def window(qtbot, tmp_path):
    bridge = Bridge()
    preferences = GuiPreferences(QSettings(str(tmp_path / "hm06.ini"), QSettings.Format.IniFormat))
    widget = MainWindow(bridge, preferences)
    qtbot.addWidget(widget)
    bridge.set_state("Ready")
    qtbot.waitUntil(lambda: widget.lifecycle == "Ready", timeout=2000)
    widget.poll_timer.stop()
    widget.chat_coordinator.timer.stop()
    yield widget, bridge
    widget.poll_timer.stop()
    widget.chat_coordinator.timer.stop()


def load_interaction(qtbot, widget, bridge, **values):
    widget.refresh()
    bridge.answer(qtbot, bridge.requests_for("interaction_snapshot")[-1], interaction(**values))


def texts(widget):
    return "\n".join(label.text() for label in widget.findChildren(QLabel))


def test_hm06_dirty_identity_and_permissions_keep_edit_revision_for_cas(panel, qtbot):
    widget, bridge, _ = panel
    load_interaction(qtbot, widget, bridge)
    widget.name.selectAll()
    qtbot.keyClicks(widget.name, "Edited")
    choice = widget.choices["role_kind"]
    choice.setCurrentIndex(choice.findData("companion"))
    widget.permissions["history"].setChecked(True)
    load_interaction(qtbot, widget, bridge, revision=8, display_name="Remote")
    assert widget.name.text() == "Edited"
    assert choice.currentData() == "companion"
    assert widget.permissions["history"].isChecked()
    assert widget.snapshot.revision == 1  # The edited baseline remains the CAS authority.
    widget.save_identity()
    command = bridge.requests_for("interaction_command")[-1]
    parameters = dict(command["parameters"])
    assert parameters.pop("command_id")
    assert parameters == {"action": "identity", "expected_revision": 1,
                          **{**IDENTITY, "display_name": "Edited", "role_kind": "companion"},
                          "use_policy": "local_only"}
    bridge.answer(qtbot, command, {}, ok=False,
                  error={"code": "RevisionConflict", "message": "synthetic revision conflict"})
    assert widget.dirty and widget.name.text() == "Edited"
    assert not widget.pending and widget.permissions["history"].isChecked()
    # An explicit reload adopts the current baseline before another save.
    widget.dirty = False
    load_interaction(qtbot, widget, bridge, revision=8, display_name="Remote")
    widget.permissions["history"].setChecked(True)
    widget.save_permissions()
    parameters = dict(bridge.requests_for("interaction_command")[-1]["parameters"])
    assert parameters.pop("command_id")
    assert parameters == {"action": "cloud_permissions", "expected_revision": 8,
                          "permissions": {key: key == "history" for key in PERMISSIONS}}


@pytest.mark.parametrize("first", ["identity", "permissions"])
def test_hm06_saving_one_settings_section_preserves_the_other_dirty_section(panel, qtbot, first):
    widget, bridge, _ = panel
    load_interaction(qtbot, widget, bridge, revision=4)
    widget.name.selectAll()
    qtbot.keyClicks(widget.name, "Edited")
    widget.permissions["history"].setChecked(True)
    getattr(widget, "save_" + first)()
    initial = bridge.requests_for("interaction_command")[-1]
    assert initial["parameters"]["expected_revision"] == 4
    saved = interaction(revision=5, display_name="Edited" if first == "identity" else "Deskpal")
    if first == "permissions": saved["cloud_permissions"]["history"] = True
    bridge.answer(qtbot, initial, saved)
    assert widget.name.text() == "Edited" and widget.permissions["history"].isChecked()
    assert widget.dirty and not widget.pending and widget.snapshot.revision == 5
    second = "permissions" if first == "identity" else "identity"
    getattr(widget, "save_" + second)()
    command = bridge.requests_for("interaction_command")[-1]
    assert command["parameters"]["expected_revision"] == 5
    if second == "permissions":
        assert command["parameters"]["permissions"] == {key: key == "history" for key in PERMISSIONS}
    else:
        assert command["parameters"]["display_name"] == "Edited"
    result = interaction(revision=6, display_name="Edited")
    result["cloud_permissions"]["history"] = True
    bridge.answer(qtbot, command, result)
    assert not widget.dirty and not widget.pending and widget.snapshot.revision == 6


def test_hm06_two_sessions_keep_reordered_history_and_candidates_separate(panel, qtbot):
    interaction_widget, bridge, coordinator = panel
    widget = ChatView(coordinator)
    qtbot.addWidget(widget)
    widget.set_conversation("conversation-a")
    coordinator.load("conversation-a")
    old_history = bridge.requests_for("conversation_timeline")[-1]
    load_interaction(qtbot, interaction_widget, bridge, conversation_id="conversation-a")
    interaction_widget.conversation.setCurrentIndex(
        interaction_widget.conversation.findData("conversation-a"))
    assert interaction_widget.candidates.currentData() == "synthetic-candidate"
    interaction_widget.refresh()
    old_interaction = bridge.requests_for("interaction_snapshot")[-1]
    widget.set_conversation("conversation-b")
    coordinator.load("conversation-b")
    current = bridge.requests_for("conversation_timeline")[-1]
    interaction_widget.conversation.setCurrentIndex(
        interaction_widget.conversation.findData("conversation-b"))
    assert interaction_widget.candidates.count() == 0
    bridge.answer(qtbot, current, transcript("conversation-b", "B current"))
    bridge.answer(qtbot, old_history, transcript("conversation-a", "A stale", sequence=99))
    bridge.answer(qtbot, old_interaction, interaction(revision=8, conversation_id="conversation-a"))
    assert widget.conversation_id == "conversation-b"
    assert "B current" in texts(widget) and "A stale" not in texts(widget)
    assert coordinator.timelines["conversation-a"][0]["body"] == "A stale"
    assert interaction_widget.candidates.count() == 0
    assert not bridge.requests_for("interaction_command")


@pytest.mark.parametrize("scope", ["session", "today", "persistent"])
def test_hm06_explicit_preferences_use_selected_scope_and_false_values(panel, qtbot, scope):
    # These controls replace the retired hidden preferences form.
    widget, bridge, _ = panel
    load_interaction(qtbot, widget, bridge, revision=4)
    widget.conversation.setCurrentIndex(widget.conversation.findData("conversation-b"))
    widget.save_preference({"scope": scope, "verbosity": "brief", "humor_allowed": "false",
                            "followup_allowed": "false", "use_policy": "local_only"})
    params = dict(bridge.requests_for("interaction_command")[-1]["parameters"])
    assert params.pop("command_id")
    assert params == {"action": "preference", "expected_revision": 4,
                      "scope": scope, "verbosity": "brief", "humor_allowed": False,
                      "followup_allowed": False, "use_policy": "local_only",
                      "conversation_id": "conversation-b"}


def test_hm06_candidate_requires_explicit_confirmation_and_memory_review_entry(window, qtbot):
    widget, bridge = window
    coordinator = widget.chat_coordinator
    coordinator.default_id = "default"
    coordinator.conversations = {"default": conversation("default")}
    coordinator.conversations_changed.emit()
    controls = widget.settings_page.interaction
    load_interaction(qtbot, controls, bridge, revision=4)
    assert "确认后才会保存" in controls.candidate_text.text()
    assert not bridge.requests_for("interaction_command")
    controls.candidate("confirm_candidate")
    request = bridge.requests_for("interaction_command")[-1]
    params = dict(request["parameters"])
    assert params.pop("command_id")
    assert params == {"action": "confirm_candidate", "expected_revision": 4,
                      "candidate_id": "synthetic-candidate"}
    controls.candidate("confirm_candidate")
    assert len(bridge.requests_for("interaction_command")) == 1
    bridge.answer(qtbot, request, interaction(revision=5, candidates=False))
    assert not controls.pending and controls.candidates.count() == 0
    # Long-term profile candidates now use the dedicated memory navigation entry.
    widget.set_page(2)
    widget.memory_page.filter.setCurrentIndex(widget.memory_page.filter.findData("candidate"))
    assert widget.stack.currentIndex() == 2
    assert bridge.requests_for("memories")[-1]["parameters"] == {
        "limit": 50, "offset": 0, "status": "candidate"}
    assert not bridge.requests_for("confirm") and not bridge.requests_for("memory")


def test_hm06_privacy_invalidated_epoch_blocks_old_settings_and_timeline(window, qtbot):
    host, bridge = window
    coordinator = host.chat_coordinator
    coordinator.default_id = "default"
    coordinator.conversations = {identity: conversation(identity) for identity in
                                 ("default", "conversation-a")}
    coordinator.conversations_changed.emit()
    controls = host.settings_page.interaction
    widget = host.chat_view
    widget.set_conversation("conversation-a")
    coordinator.load("conversation-a")
    old_history = bridge.requests_for("conversation_timeline")[-1]
    load_interaction(qtbot, controls, bridge, allowed=True, conversation_id="conversation-a")
    controls.refresh()
    old_interaction = bridge.requests_for("interaction_snapshot")[-1]
    controls.permissions["history"].setChecked(False)
    controls.save_permissions()
    command = bridge.requests_for("interaction_command")[-1]
    # Mirror ApplicationBridge's order: elevate epoch, signal barrier, then acknowledge.
    bridge.privacy_epoch = 1
    bridge.privacy_changed.emit(1)
    bridge.answer(qtbot, command, dto(privacy_invalidated=True), privacy_epoch=1)
    fresh = bridge.requests_for("interaction_snapshot")[-1]
    assert fresh["privacy_epoch"] == 1 and fresh["generation"] > command["generation"]
    bridge.answer(qtbot, fresh, interaction(revision=3, interaction_epoch=1, candidates=False))
    bridge.answer(qtbot, old_history, transcript("conversation-a", "private stale history"))
    bridge.answer(qtbot, old_interaction,
                  interaction(revision=99, allowed=True, display_name="Revived"))
    assert "private stale history" not in texts(widget)
    assert coordinator.timelines == {} and controls.candidates.count() == 0
    assert controls.name.text() != "Revived"
    assert not any(box.isChecked() for box in controls.permissions.values())
    assert not controls.pending and len(bridge.requests_for("interaction_command")) == 1


@pytest.mark.parametrize("completion_before_receipt", [False, True])
def test_hm06_cleanup_pending_completed_and_closed_callbacks_cannot_revive(window, qtbot,
                                                                           completion_before_receipt):
    widget, bridge = window
    coordinator = widget.chat_coordinator
    coordinator.default_id = "default"
    coordinator.conversations = {"default": conversation("default")}
    coordinator.conversations_changed.emit()
    controls = widget.settings_page.interaction
    load_interaction(qtbot, controls, bridge)
    controls.refresh()
    old_settings = bridge.requests_for("interaction_snapshot")[-1]
    coordinator.load("default")
    old_history = bridge.requests_for("conversation_timeline")[-1]
    # Exercise the host's global memory-cleanup lifecycle, independent of per-chat jobs.
    widget.memory_page._snapshot = {"id": "synthetic-memory", "revision": 1}
    widget.memory_page.action("forget")
    forget = bridge.requests_for("request_forget")[-1]
    bridge.set_state("CleanupBlocked")
    qtbot.waitUntil(lambda: widget.lifecycle == "CleanupBlocked", timeout=2000)
    widget.poll_timer.stop()
    coordinator.timer.stop()
    if not completion_before_receipt:
        bridge.answer(qtbot, forget, dto(status="AcceptedPending", blocked=True,
                                        cleanup_pending=True))
    # The dedicated cleanup status query must expose real completion.
    status = bridge.requests_for("cleanup_status")[-1]
    if not completion_before_receipt:
        bridge.answer(qtbot, status, dto(status="Pending", complete=False, blocked=True,
                                        cleanup_pending=True, pending=[{"id": "cleanup-work"}]))
        assert "隐私清理进行中" in widget.notice.text()
        widget._poll_cleanup()
        status = bridge.requests_for("cleanup_status")[-1]
    bridge.answer(qtbot, status, dto(status="Completed", complete=True, blocked=False,
                                    cleanup_pending=False))
    assert "清理已完成" in widget.notice.text()
    if completion_before_receipt:
        bridge.answer(qtbot, forget, dto(status="AcceptedPending", blocked=True,
                                        cleanup_pending=True))
        assert "清理已完成" in widget.notice.text()
    bridge.answer(qtbot, old_settings, interaction(revision=99, allowed=True, display_name="Revived"))
    bridge.answer(qtbot, old_history, transcript("default", "private stale history"))
    assert coordinator.timelines == {} and controls.name.text() == ""
    bridge.set_state("Ready")
    qtbot.waitUntil(lambda: widget.lifecycle == "Ready", timeout=2000)
    widget.poll_timer.stop()
    coordinator.timer.stop()
    controls.refresh()
    late = bridge.requests_for("interaction_snapshot")[-1]
    bridge.set_state("Closed")
    qtbot.waitUntil(lambda: widget.lifecycle == "Closed", timeout=2000)
    before = len(bridge.sent)
    bridge.answer(qtbot, late, interaction(revision=100, allowed=True, display_name="Revived"))
    bridge.answer(qtbot, forget, dto(status="AcceptedPending", blocked=True))
    assert len(bridge.sent) == before and not widget.isVisible()
    assert controls.name.text() == "" and controls.candidates.count() == 0
    assert coordinator.timelines == {} and not widget.poll_timer.isActive()
    assert not controls.channel.pending and not coordinator.channel.pending
