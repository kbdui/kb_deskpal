"""HM06: real offscreen Qt, synthetic DTOs and deliberately reordered replies.

The bridge follows the existing companion GUI fake's queued transport contract.
It is local to this module so importing tests cannot alter pytest collection.
"""

from copy import deepcopy

from PySide6.QtCore import QObject, QSettings, QTimer, Signal

import pytest

from deskpal.gui.conversation_panel import ConversationPanel
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


def transcript(conversation_id, text, *, sequence=1, memory_epoch=0):
    return dto(conversation_id=conversation_id, epoch=0, memory_epoch=memory_epoch,
               turns=[{"sequence": sequence, "task_id": "task-" + conversation_id,
                       "request": text, "response": "synthetic reply"}])


class Bridge(QObject):
    reply = Signal(object)
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
    widget = ConversationPanel(bridge)
    qtbot.addWidget(widget)
    widget.set_available(True)
    return widget, bridge


@pytest.fixture
def window(qtbot, tmp_path):
    bridge = Bridge()
    preferences = GuiPreferences(QSettings(str(tmp_path / "hm06.ini"), QSettings.Format.IniFormat))
    widget = MainWindow(bridge, preferences)
    qtbot.addWidget(widget)
    bridge.set_state("Ready")
    qtbot.waitUntil(lambda: widget.lifecycle == "Ready", timeout=2000)
    widget.poll_timer.stop()
    return widget, bridge


def load_interaction(qtbot, widget, bridge, **values):
    widget.poll()
    bridge.answer(qtbot, bridge.requests_for("interaction_snapshot")[-1], interaction(**values))


def open_conversation(qtbot, widget, bridge, identity):
    widget.new_button.click()
    bridge.answer(qtbot, bridge.requests_for("conversation_open")[-1],
                  dto(conversation_id=identity, epoch=0))
    assert widget.sessions.currentData() == identity


def test_hm06_dirty_identity_and_permissions_survive_refresh_with_current_revision(panel, qtbot):
    widget, bridge = panel
    load_interaction(qtbot, widget, bridge)
    widget.display_name.selectAll()
    qtbot.keyClicks(widget.display_name, "Edited")
    widget.identity_choices["role_kind"].setCurrentText("companion")
    widget.permission_checks["history"].setChecked(True)
    load_interaction(qtbot, widget, bridge, revision=8, display_name="Remote")
    assert widget.display_name.text() == "Edited"
    assert widget.identity_choices["role_kind"].currentText() == "companion"
    assert widget.permission_checks["history"].isChecked()
    assert widget.identity_save.isEnabled() and widget.permissions_save.isEnabled()
    widget.identity_save.click()
    identity_command = bridge.requests_for("interaction_command")[-1]
    parameters = dict(identity_command["parameters"])
    assert parameters.pop("command_id")
    assert parameters == {"action": "identity", "expected_revision": 8,
                          **{**IDENTITY, "display_name": "Edited", "role_kind": "companion"},
                          "use_policy": "local_only"}
    bridge.answer(qtbot, identity_command, dto(revision=9))
    bridge.answer(qtbot, bridge.requests_for("interaction_snapshot")[-1],
                  interaction(revision=9, display_name="Edited"))
    assert widget.permission_checks["history"].isChecked()
    widget.permissions_save.click()
    parameters = dict(bridge.requests_for("interaction_command")[-1]["parameters"])
    assert parameters.pop("command_id")
    assert parameters == {"action": "cloud_permissions", "expected_revision": 9,
                          "permissions": {key: key == "history" for key in PERMISSIONS}}


def test_hm06_session_switch_rejects_old_history_and_invalidates_candidate(panel, qtbot):
    widget, bridge = panel
    open_conversation(qtbot, widget, bridge, "conversation-a")
    old_history = bridge.requests_for("conversation_snapshot")[-1]
    old_interaction = bridge.requests_for("interaction_snapshot")[-1]
    bridge.answer(qtbot, old_interaction, interaction(conversation_id="conversation-a"))
    assert widget.candidates.currentData() == "synthetic-candidate"
    open_conversation(qtbot, widget, bridge, "conversation-b")
    assert widget.candidates.count() == 0 and not widget.confirm_candidate.isEnabled()
    fresh_history = bridge.requests_for("conversation_snapshot")[-1]
    fresh_interaction = bridge.requests_for("interaction_snapshot")[-1]
    bridge.answer(qtbot, fresh_history, transcript("conversation-b", "B current"))
    bridge.answer(qtbot, fresh_interaction, interaction(conversation_id="conversation-b", candidates=False))
    bridge.answer(qtbot, old_history, transcript("conversation-a", "A stale", sequence=99))
    bridge.answer(qtbot, old_interaction, interaction(revision=99, conversation_id="conversation-a"))
    assert "B current" in widget.history.toPlainText() and "A stale" not in widget.history.toPlainText()
    assert widget.submission_parameters() == {"conversation_id": "conversation-b", "expected_conversation_epoch": 0}
    assert widget.candidates.count() == 0 and not widget.confirm_candidate.isEnabled()
    assert not bridge.requests_for("interaction_command")


def test_hm06_candidate_confirmation_uses_receipt_and_memory_review_navigation(window, qtbot):
    widget, bridge = window
    panel_widget = widget.conversation_panel
    load_interaction(qtbot, panel_widget, bridge, revision=4)
    assert "确认后才会保存" in panel_widget.candidate_detail.text()
    panel_widget.confirm_candidate.click()
    request = bridge.requests_for("interaction_command")[-1]
    parameters = dict(request["parameters"])
    assert parameters.pop("command_id")
    assert parameters == {"action": "confirm_candidate", "expected_revision": 4,
                          "candidate_id": "synthetic-candidate"}
    panel_widget.confirm_candidate.click()
    assert len(bridge.requests_for("interaction_command")) == 1
    bridge.answer(qtbot, request, dto(revision=5))
    assert panel_widget.candidates.count() == 0 and not panel_widget.confirm_candidate.isEnabled()
    bridge.answer(qtbot, bridge.requests_for("interaction_snapshot")[-1], interaction(revision=5, candidates=False))
    panel_widget.memory_candidates_button.click()
    assert widget.tabs.currentIndex() == 2 and widget.memory_filter.currentText() == "candidate"
    assert bridge.requests_for("memories")[-1]["parameters"] == {"limit": 50, "offset": 0, "status": "candidate"}
    assert not bridge.requests_for("confirm") and not bridge.requests_for("memory")


def test_hm06_privacy_invalidated_receipt_advances_epoch_and_old_dtos_cannot_revive(panel, qtbot):
    widget, bridge = panel
    open_conversation(qtbot, widget, bridge, "conversation-private")
    old_history = bridge.requests_for("conversation_snapshot")[-1]
    old_interaction = bridge.requests_for("interaction_snapshot")[-1]
    bridge.answer(qtbot, old_interaction, interaction(conversation_id="conversation-private", allowed=True))
    widget.permission_checks["history"].setChecked(False)
    widget.permissions_save.click()
    command = bridge.requests_for("interaction_command")[-1]
    bridge.privacy_epoch = 1
    bridge.privacy_changed.emit(1)
    qtbot.waitUntil(lambda: widget.sessions.count() == 1 and widget.candidates.count() == 0, timeout=2000)
    bridge.answer(qtbot, command, dto(privacy_invalidated=True), privacy_epoch=1)
    fresh = bridge.requests_for("interaction_snapshot")[-1]
    assert fresh["privacy_epoch"] == 1 and fresh["generation"] > command["generation"]
    assert "用途已撤销" in widget.notice.text()
    bridge.answer(qtbot, fresh, interaction(revision=3, memory_epoch=1, interaction_epoch=1, candidates=False))
    bridge.answer(qtbot, old_history, transcript("conversation-private", "private stale history"))
    bridge.answer(qtbot, old_interaction, interaction(revision=99, conversation_id="conversation-private", allowed=True))
    assert widget.history.toPlainText() == "" and widget.candidates.count() == 0
    assert widget.sessions.count() == 1 and not any(box.isChecked() for box in widget.permission_checks.values())
    assert len(bridge.requests_for("interaction_command")) == 1


@pytest.mark.parametrize("completion_before_receipt", [False, True])
def test_hm06_cleanup_pending_completed_and_closed_callbacks_cannot_revive(window, qtbot, completion_before_receipt):
    widget, bridge = window
    load_interaction(qtbot, widget.conversation_panel, bridge)
    widget.memory_snapshot = {"id": "synthetic-memory", "revision": 1}
    widget.memory_action("forget")
    forget = bridge.requests_for("request_forget")[-1]
    assert widget.privacy_barrier and not widget.conversation_panel.new_button.isEnabled()
    if not completion_before_receipt:
        bridge.answer(qtbot, forget, dto(status="AcceptedPending", blocked=True, cleanup_pending=True))
        assert widget.forget_state == "pending" and widget.privacy_barrier
    widget.diagnostics_button.click()
    # Polling cleanup is exposed by the window's CleanupBlocked lifecycle.
    bridge.set_state("CleanupBlocked")
    qtbot.waitUntil(lambda: bool(bridge.requests_for("cleanup_status")), timeout=2000)
    widget.poll_timer.stop()
    bridge.answer(qtbot, bridge.requests_for("cleanup_status")[-1],
                  dto(status="Completed", complete=True, blocked=False, cleanup_pending=False))
    assert widget.forget_state == "complete" and "清理已完成" in widget.notice.text()
    if completion_before_receipt:
        bridge.answer(qtbot, forget, dto(status="AcceptedPending", blocked=True, cleanup_pending=True))
        assert widget.forget_state == "complete" and "清理已完成" in widget.notice.text()
    bridge.set_state("Ready")
    qtbot.waitUntil(lambda: widget.lifecycle == "Ready", timeout=2000)
    widget.poll_timer.stop()
    widget.reload_page("memories", explicit=True)
    assert not widget.privacy_barrier
    widget.tabs.setCurrentIndex(0)
    widget.conversation_panel.poll()
    late = bridge.requests_for("interaction_snapshot")[-1]
    bridge.set_state("Closed")
    qtbot.waitUntil(lambda: widget.lifecycle == "Closed", timeout=2000)
    before = len(bridge.sent)
    bridge.answer(qtbot, late, interaction(revision=99, allowed=True, display_name="Revived"))
    bridge.answer(qtbot, forget, dto(status="AcceptedPending", blocked=True))
    assert len(bridge.sent) == before and not widget.isVisible()
    assert widget.conversation_panel.display_name.text() == ""
    assert widget.conversation_panel.candidates.count() == 0
    assert not widget.conversation_panel.new_button.isEnabled() and not widget.poll_timer.isActive()
    assert not widget.requests.pending
