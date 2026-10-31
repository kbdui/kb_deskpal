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


def dto(**values):
    return {"schema_version": 1, "session_id": "synthetic-session", "memory_epoch": 0, **values}


def interaction(*, revision=1, conversation_id="default", candidates=True, allowed=False,
                display_name="Deskpal", memory_epoch=0, interaction_epoch=0):
    return {**dto(revision=revision, memory_epoch=memory_epoch), 'schema_version': 2,
            'character_card': {'text': display_name, 'use_policy': 'cloud_allowed'},
            'habits': [], 'current_emotion': None}


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
    host.memory_page._snapshot = {'id': 'synthetic-memory', 'revision': 1}
    host.memory_page.action('forget')
    command = bridge.requests_for('request_forget')[-1]
    # Mirror ApplicationBridge's order: elevate epoch, signal barrier, then acknowledge.
    bridge.privacy_epoch = 1
    bridge.privacy_changed.emit(1)
    bridge.answer(qtbot, command, dto(privacy_invalidated=True), privacy_epoch=1)
    fresh = bridge.requests_for("interaction_snapshot")[-1]
    assert fresh["privacy_epoch"] == 1
    bridge.answer(qtbot, fresh, interaction(revision=3, interaction_epoch=1, candidates=False))
    bridge.answer(qtbot, old_history, transcript("conversation-a", "private stale history"))
    bridge.answer(qtbot, old_interaction,
                  interaction(revision=99, allowed=True, display_name="Revived"))
    assert "private stale history" not in texts(widget)
    assert coordinator.timelines == {} and controls.snapshot['habits'] == []
    assert controls.card.toPlainText() != "Revived"
    assert not controls.pending and len(bridge.requests_for('request_forget')) == 1


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
    assert coordinator.timelines == {} and controls.card.toPlainText() == ""
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
    assert controls.card.toPlainText() == "" and not controls.habits_view.text()
    assert coordinator.timelines == {} and not widget.poll_timer.isActive()
    assert not controls.channel.pending and not coordinator.channel.pending
