"""T08: real Qt delivery races and observable companion controls/decorations.

Only synthetic DTOs, prepared in-memory images and temporary QSettings are used.
The fake transport deliberately permits duplicated, stale and reordered replies.
"""

from copy import deepcopy

import pytest

pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QObject, QSettings, QTimer, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QSignalSpy

from deskpal.gui.pet_assets import AssetResult, PetAssetLoader
from deskpal.gui.pet_controller import PetController
from deskpal.gui.preferences import GuiPreferences
from deskpal.gui.window import MainWindow
from deskpal.presentation.state import status_label


STYLE = {"formality": 0.5, "warmth": 0.5, "humor": 0.3, "verbosity": 0.5,
         "expressiveness": 0.5, "curiosity": 0.5}
COLORS = {"neutral": "#222222", "thinking": "#4488aa", "focused": "#1122dd",
          "pleased": "#22bb66", "alert": "#bb2200"}


def companion(*, revision=1, enabled=True, emotions=True, preset="balanced",
              expression="focused", memory_epoch=0):
    return {"schema_version": 1, "session_id": "synthetic-session", "memory_epoch": memory_epoch,
            "revision": revision, "reset_epoch": 0, "enabled": enabled,
            "emotions_enabled": emotions,
            "persona": {"version": 3, "preset": preset, "style": dict(STYLE)},
            "persona_versions": [3, 2, 1],
            "affect": {"valence": 0.4, "arousal": 0.3, "mood": 0.05,
                       "mode": "focused", "expression": expression},
            "preferences": {}, "constraints": {"humor_allowed": True}, "cue": None,
            "warnings": []}


def presentation(status="Idle", *, sequence=0, revision=0, memory_epoch=0):
    return {"schema_version": 1, "session_id": "synthetic-session", "memory_epoch": memory_epoch,
            "sequence": sequence, "status": status, "revision": revision,
            "task_id": None if status == "Idle" else "synthetic-task"}


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
        self.outstanding = {}
        self.delivered = 0
        self.shutdown_count = 0

    def send_image(self, operation, parameters, generation=0):
        return self.send(operation, parameters, generation=generation)

    def send(self, operation, parameters=None, *, generation=0):
        request = {"request_id": f"request-{len(self.sent) + 1}", "operation": operation,
                   "parameters": deepcopy(parameters or {}), "generation": generation,
                   "privacy_epoch": self.privacy_epoch}
        self.sent.append(request)
        self.outstanding[request["request_id"]] = request
        return request["request_id"]

    def requests_for(self, operation):
        return [request for request in self.sent if request["operation"] == operation]

    def pending(self, operation):
        return [request for request in self.outstanding.values() if request["operation"] == operation]

    def set_state(self, state):
        self.state = state
        self.state_changed.emit(state)

    def shutdown(self):
        self.shutdown_count += 1

    def answer(self, qtbot, request, data=None, *, ok=True, **overrides):
        envelope = {key: request[key] for key in ("request_id", "operation", "generation", "privacy_epoch")}
        envelope.update(ok=ok, data=deepcopy(data or {}),
                        error={"code": "SyntheticFailure", "message": "synthetic failure"})
        envelope.update(overrides)
        delivered = self.delivered

        def emit():
            self.outstanding.pop(envelope["request_id"], None)
            self.reply.emit(envelope)
            self.delivered += 1

        QTimer.singleShot(0, emit)
        qtbot.waitUntil(lambda: self.delivered == delivered + 1, timeout=2000)


def prepared(generation, names=tuple(COLORS)):
    images = {}
    for name in names:
        image = QImage(32, 32, QImage.Format.Format_ARGB32)
        image.fill(QColor(COLORS[name]))
        images[name] = image
    return AssetResult(generation, images, ())


@pytest.fixture
def pet(qtbot, tmp_path):
    bridge = Bridge()
    preferences = GuiPreferences(QSettings(str(tmp_path / "pet.ini"), QSettings.Format.IniFormat))
    clock = [100.0]
    loader = PetAssetLoader(load=lambda generation, cancel: prepared(generation))
    controller = PetController(bridge, preferences, loader=loader, clock=lambda: clock[0])
    preferences.set_pet_enabled(True)
    qtbot.waitUntil(lambda: controller.settled and controller.cache_bytes > 0, timeout=3000)
    controller.poll_timer.stop()
    try:
        yield controller, bridge, preferences, clock
    finally:
        controller.shutdown()
        qtbot.waitUntil(lambda: controller.settled, timeout=3000)


def show_status(qtbot, controller, bridge, status="Idle", **values):
    controller.poll()
    requests = bridge.pending("presentation")
    assert len(requests) == 1
    bridge.answer(qtbot, requests[0], presentation(status, **values))


def show_affect(qtbot, controller, bridge, **values):
    requests = bridge.pending("companion")
    assert len(requests) == 1
    bridge.answer(qtbot, requests[0], companion(**values))


def assert_pet(controller, expression, label):
    assert controller.window is not None
    assert controller.window.toolTip().split("\n")[0] == label
    image = controller.window._pixmap.toImage()
    assert image.pixelColor(0, 0) == QColor(COLORS[expression])


def test_t08_pet_idle_emotion_reaches_real_window_and_each_read_lane_is_bounded(pet, qtbot):
    controller, bridge, _, clock = pet
    show_status(qtbot, controller, bridge)
    for _ in range(20):
        controller.poll()
    assert len(bridge.pending("presentation")) == len(bridge.pending("companion")) == 1
    sent = len(bridge.sent)
    for _ in range(20):
        controller.poll()
    assert len(bridge.sent) == sent
    show_affect(qtbot, controller, bridge)
    assert_pet(controller, "focused", "空闲")
    assert not controller.success_timer.isActive()
    show_status(qtbot, controller, bridge)
    clock[0] += 1
    controller.poll()
    for _ in range(20):
        controller.poll()
    assert len(bridge.pending("presentation")) == len(bridge.pending("companion")) == 1
    assert {request["operation"] for request in bridge.sent} == {"presentation", "companion", "appearance_snapshot"}
    assert all(request["parameters"] == {} for request in bridge.sent)


def test_t08_pet_completed_baseline_allows_static_smile_without_success_timer(pet, qtbot):
    controller, bridge, preferences, clock = pet
    show_status(qtbot, controller, bridge, "Completed", sequence=1, revision=1)
    assert_pet(controller, "neutral", "已完成")
    show_affect(qtbot, controller, bridge, expression="pleased")
    assert_pet(controller, "pleased", "已完成")
    assert not controller.success_timer.isActive()
    # Restoring a visible pet establishes a new baseline, never historical feedback.
    clock[0] += 1
    preferences.set_pet_visible(False)
    preferences.set_pet_visible(True)
    controller.poll_timer.stop()
    show_status(qtbot, controller, bridge, "Completed", sequence=1, revision=1)
    assert_pet(controller, "neutral", "已完成")
    assert not controller.success_timer.isActive()
    clock[0] += 1
    show_affect(qtbot, controller, bridge, revision=2, expression="pleased")
    assert_pet(controller, "pleased", "已完成")
    assert not controller.success_timer.isActive()


def test_t08_pet_completion_feedback_once_1500ms_then_current_emotion(pet, qtbot):
    controller, bridge, _, clock = pet
    show_status(qtbot, controller, bridge, "Running", sequence=1, revision=1)
    assert_pet(controller, "focused", "执行中")
    clock[0] += 1
    show_status(qtbot, controller, bridge, "Completed", sequence=2, revision=2)
    assert_pet(controller, "pleased", "已完成")
    assert controller.success_timer.isActive() and controller.success_timer.interval() == 1500
    feedback = QSignalSpy(controller.success_timer.timeout)
    remaining = controller.success_timer.remainingTime()
    show_affect(qtbot, controller, bridge, expression="focused")
    assert_pet(controller, "pleased", "已完成")
    assert 0 < controller.success_timer.remainingTime() <= remaining
    clock[0] += 1
    show_status(qtbot, controller, bridge, "Completed", sequence=2, revision=2)
    assert controller.success_timer.remainingTime() <= remaining
    qtbot.waitUntil(lambda: feedback.count() == 1, timeout=2200)
    assert_pet(controller, "focused", "已完成")
    assert not controller.success_timer.isActive()
    clock[0] += 1
    show_affect(qtbot, controller, bridge, revision=2, expression="pleased")
    assert_pet(controller, "pleased", "已完成")
    show_status(qtbot, controller, bridge, "Completed", sequence=2, revision=2)
    assert_pet(controller, "pleased", "已完成")
    assert feedback.count() == 1 and not controller.success_timer.isActive()


@pytest.mark.parametrize("status,expression", [
    ("Planning", "thinking"), ("Running", "focused"), ("Waiting", "neutral"),
    ("Failed", "alert"), ("NeedsReview", "neutral"), ("Uncertain", "neutral"),
    ("Cancelled", "neutral"), ("Unknown", "neutral"),
])
def test_t08_pet_critical_state_label_preempts_late_emotion(pet, qtbot, status, expression):
    controller, bridge, _, clock = pet
    show_status(qtbot, controller, bridge)
    show_affect(qtbot, controller, bridge, expression="pleased")
    assert_pet(controller, "pleased", "空闲")
    clock[0] += 1
    controller.poll()
    late_affect = bridge.pending("companion")[0]
    show_status(qtbot, controller, bridge, status, sequence=2, revision=1)
    assert controller.window.toolTip().split("\n")[0] == status_label(status)
    bridge.answer(qtbot, late_affect, companion(revision=2, expression="pleased"))
    qtbot.waitUntil(lambda: controller.expression == expression, timeout=1000)
    assert_pet(controller, expression, status_label(status))
    assert not controller.success_timer.isActive()


@pytest.mark.parametrize("failure", ["disabled", "emotions_off", "error", "warning", "missing_image", "stale", "privacy"])
def test_t08_pet_invalid_emotion_returns_to_authoritative_mapping(pet, qtbot, failure):
    controller, bridge, _, clock = pet
    show_status(qtbot, controller, bridge)
    show_affect(qtbot, controller, bridge, expression="pleased")
    assert_pet(controller, "pleased", "空闲")
    clock[0] += 1
    controller.poll()
    late = bridge.pending("companion")[0]
    if failure == "privacy":
        bridge.privacy_epoch += 1
        bridge.privacy_changed.emit(bridge.privacy_epoch)
        bridge.answer(qtbot, late, companion(revision=2, expression="pleased"))
        assert_pet(controller, "neutral", "状态未知")
    elif failure == "stale":
        show_status(qtbot, controller, bridge)
        clock[0] += 4
        show_status(qtbot, controller, bridge)
        bridge.answer(qtbot, late, companion(revision=2, expression="pleased"))
        assert_pet(controller, "neutral", "空闲")
    else:
        value = companion(revision=2, expression="focused")
        if failure == "disabled":
            value["enabled"] = False
        elif failure == "emotions_off":
            value["emotions_enabled"] = False
        elif failure == "warning":
            value["warnings"] = ["companion_storage_unavailable"]
        elif failure == "missing_image":
            controller.loader.ready.emit(prepared(controller._asset_generation, ("neutral", "pleased")))
        bridge.answer(qtbot, late, value, ok=failure != "error")
        assert_pet(controller, "neutral", "空闲")
    assert not controller.success_timer.isActive()


@pytest.mark.parametrize("stop", ["disable", "shutdown", "Closing", "Closed"])
def test_t08_pet_stop_and_old_reply_never_recreate_window_or_polling(pet, qtbot, stop):
    controller, bridge, preferences, clock = pet
    show_status(qtbot, controller, bridge)
    show_affect(qtbot, controller, bridge)
    assert_pet(controller, "focused", "空闲")
    clock[0] += 1
    controller.poll()
    old_status, old_affect = bridge.pending("presentation")[0], bridge.pending("companion")[0]
    old_window = controller.window
    if stop == "disable":
        preferences.set_pet_enabled(False)
    elif stop == "shutdown":
        controller.shutdown()
    else:
        bridge.set_state(stop)
    assert not old_window.isVisible()
    bridge.answer(qtbot, old_affect, companion(revision=2, expression="pleased"))
    bridge.answer(qtbot, old_status, presentation("Completed", sequence=2, revision=1))
    sent = len(bridge.sent)
    controller.poll()
    qtbot.waitUntil(lambda: controller.settled, timeout=3000)
    assert controller.window is None and controller.cache_bytes == 0
    assert len(bridge.sent) == sent and not controller.poll_timer.isActive()
