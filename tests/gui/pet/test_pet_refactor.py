"""D4 development cases. Written for the later authorized Qt test phase; not executed."""
from datetime import datetime, timezone

import pytest

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QPoint, QSettings, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPixmap

from deskpal.gui.chat import ChatCoordinator
from deskpal.gui.notifications import NotificationController
from deskpal.gui.pet_assets import MAX_CACHE_BYTES
from deskpal.gui.pet_chat import PetChatWindow
from deskpal.gui.pet_controller import PetController
from deskpal.gui.pet_window import PetWindow
from deskpal.gui.preferences import GuiPreferences


class Bridge(QObject):
    reply = Signal(object)
    image_reply = Signal(object)
    state_changed = Signal(str)
    privacy_changed = Signal(int)
    state = "Ready"
    privacy_epoch = 0

    def __init__(self):
        super().__init__()
        self.calls = []

    def send(self, operation, parameters=None, *, generation=0):
        request = str(len(self.calls))
        self.calls.append((request, operation, parameters or {}, generation))
        return request

    def send_image(self, operation, parameters, generation=0):
        return self.send(operation, parameters, generation=generation)


class Loader(QObject):
    ready = Signal(object)
    idle = Signal()
    busy = False

    def __init__(self):
        super().__init__()
        self.requests = []

    def request(self, generation):
        self.requests.append(generation)

    def cancel(self):
        pass


def preferences(tmp_path, name="prefs.ini"):
    return GuiPreferences(QSettings(str(tmp_path / name), QSettings.Format.IniFormat))


def image(color="#26724a", edge=64):
    result = QImage(edge, edge, QImage.Format.Format_ARGB32)
    result.fill(QColor(color))
    return result


def encoded(source):
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert source.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def test_scale_validates_full_canvas_mask_and_readable_label(qtbot, tmp_path):
    prefs = preferences(tmp_path)
    assert prefs.pet_scale == 1.0
    for invalid in (True, float("nan"), float("inf"), .59, 1.61, "1.2"):
        assert not prefs.set_pet_scale(invalid)
    window = PetWindow()
    qtbot.addWidget(window)
    transparent = QImage(100, 100, QImage.Format.Format_ARGB32)
    transparent.fill(Qt.GlobalColor.transparent)
    for y in range(25, 75):
        for x in range(25, 75):
            transparent.setPixelColor(x, y, Qt.GlobalColor.darkCyan)
    main = QPixmap.fromImage(transparent)
    window.set_stable_mask(main)
    assert prefs.set_pet_scale(1.6)
    window.set_scale(prefs.pet_scale)
    assert (window.width(), window.height()) == (384, 384)
    assert window.mask().contains(QPoint(192, 192))
    assert not window.mask().contains(QPoint(8, 8))
    assert not window.mask().contains(QPoint(24, 400))
    region = window.mask()
    window.present(QPixmap.fromImage(image("#ff0000")), "等待确认")
    assert window.mask() == region
    window.set_scale(.6)
    assert window.height() == 144 and not hasattr(window, "_label")


def test_temp_hide_preserves_preferences_and_stops_pet_reads(qtbot, tmp_path):
    bridge, loader, prefs = Bridge(), Loader(), preferences(tmp_path)
    pet = PetController(bridge, prefs, loader=loader)
    try:
        pet.start()
        assert not loader.requests and not bridge.calls
        prefs.set_pet_enabled(True)
        assert len(loader.requests) == 1 and pet.window.isVisible()
        pet.hide_temporarily()
        assert prefs.pet_enabled and prefs.pet_visible
        assert not pet.window.isVisible() and not pet.poll_timer.isActive()
        count = len(bridge.calls)
        pet.poll()
        assert len(bridge.calls) == count
        # Reaffirming true in settings restores a temporary hide even when its stored value was true.
        prefs.set_pet_visible(True)
        assert pet.window.isVisible()
    finally:
        pet.shutdown()
        qtbot.waitUntil(lambda: pet.settled)


@pytest.mark.parametrize("temporary", [False, True])
def test_hidden_pet_drops_late_images_and_profiles_without_new_queries(qtbot, tmp_path, temporary):
    bridge, prefs = Bridge(), preferences(tmp_path)
    pet = PetController(bridge, prefs, loader=Loader())
    try:
        prefs.set_pet_enabled(True)
        pet.poll_timer.stop()
        pet._package["neutral"] = image()
        pet._render()
        _, edge = pet._metrics()
        retained = image("#222222")
        pet._sources[("retained", edge)] = retained
        # Simulate a preview that became obsolete while a newer main awaits its image.
        pet._request_image("old-main", edge)
        old_image_id = next(key for key, item in pet._image_requests.items() if item[1] == "old-main")
        old_image_generation = pet._image_generation
        old_profile_id = pet._appearance_request
        old_profile_generation = pet._generation
        assert old_profile_id is not None
        pet._profiles = {"pet_main": {"asset_id": "new-main", "revision": 2}}
        before_hide = len(bridge.calls)
        if temporary:
            pet.hide_temporarily()
        else:
            prefs.set_pet_visible(False)
        assert len(bridge.calls) == before_hide
        assert pet._image_generation > old_image_generation
        assert not pet._image_requests and pet._appearance_request is None
        assert pet._sources[("retained", edge)] is retained
        count = len(bridge.calls)
        bridge.image_reply.emit({"request_id": old_image_id, "operation": "asset_preview", "ok": True,
            "generation": old_image_generation, "privacy_epoch": 0,
            "image": {"png": encoded(image("#ff0000")), "asset_id": "old-main", "width": 64, "height": 64}})
        bridge.reply.emit({"request_id": old_profile_id, "operation": "appearance_snapshot", "ok": True,
            "generation": old_profile_generation, "privacy_epoch": 0,
            "data": {"profiles": [{"slot": "pet_main", "asset_id": "old-main", "revision": 1}]}})
        pet._render()
        pet._request_image("new-main", edge)
        pet._poll_appearance(force=True)
        assert len(bridge.calls) == count
        assert ("old-main", edge) not in pet._sources
        assert pet._profiles["pet_main"]["asset_id"] == "new-main"
        prefs.set_pet_visible(True)
        pet.poll_timer.stop()
        assert pet.window.isVisible() and pet._appearance_request is not None
        assert pet._appearance_request != old_profile_id
    finally:
        pet.shutdown()
        qtbot.waitUntil(lambda: pet.settled)


def test_custom_missing_expression_uses_main_crop_and_rejects_late_image(qtbot, tmp_path):
    bridge, prefs = Bridge(), preferences(tmp_path)
    pet = PetController(bridge, prefs, loader=Loader())
    try:
        prefs.set_pet_enabled(True)
        pet.poll_timer.stop()
        pet._profiles = {
            "pet_main": {"asset_id": "main-a", "effective_asset_id": "main-a", "revision": 4,
                         "crop": [0., 0., .5, 1.], "display_mode": "contain"},
            "pet_thinking": {"asset_id": None, "effective_asset_id": None, "revision": 1},
        }
        _, edge = pet._metrics()
        pet._sources[("main-a", edge)] = image()
        pet._package["thinking"] = image("#ff0000")
        pet._set_expression("thinking")
        assert pet.window._pixmap.toImage().pixelColor(0, 0) == QColor("#26724a")
        assert pet.window._pixmap.width() * 2 == pet.window._pixmap.height()
        assert pet._profile("thinking")[1]["crop"] == [0., 0., .5, 1.]
        old_generation = pet._image_generation
        pet._image_requests["old"] = (old_generation, "main-a", edge)
        pet._profiles["pet_main"] = {"asset_id": "main-b", "effective_asset_id": "main-b", "revision": 5}
        pet._metrics_changed()
        before = dict(pet._sources)
        bridge.image_reply.emit({"request_id": "old", "operation": "asset_preview", "ok": True,
            "generation": old_generation, "privacy_epoch": 0,
            "image": {"png": encoded(image("#ff0000")), "width": 64, "height": 64, "asset_id": "main-a"}})
        assert pet._sources == before
        assert pet.window._pixmap is None  # Never switches back to the packaged red character.
    finally:
        pet.shutdown()
        qtbot.waitUntil(lambda: pet.settled)


def test_raster_cache_is_bounded_across_scale_revision_and_dpr(qtbot, tmp_path):
    bridge, prefs = Bridge(), preferences(tmp_path)
    pet = PetController(bridge, prefs, loader=Loader())
    try:
        prefs.set_pet_enabled(True)
        pet.poll_timer.stop()
        pet._package = {name: image(edge=480) for name in ("neutral", "thinking", "focused", "pleased", "alert")}
        for index in range(30):
            scale = .6 + index / 29
            prefs._settings.setValue("pet/scale", scale)
            for name in pet._package:
                pet._profiles["pet_main" if name == "neutral" else "pet_" + name] = {"revision": index}
                pet._metrics = lambda scale=scale: (3., round(240 * scale * 3))
                assert pet._pixmap(name) is not None
                assert pet.cache_bytes <= MAX_CACHE_BYTES
        assert len(pet._rasters) < 150
    finally:
        pet.shutdown()
        qtbot.waitUntil(lambda: pet.settled)


def test_pet_chat_reopens_default_with_shared_drafts_and_no_new_submission(qtbot, tmp_path):
    bridge = Bridge()
    coordinator = ChatCoordinator(bridge)
    coordinator.timer.stop()
    coordinator.default_id = "daily"
    coordinator.conversations = {"daily": {"title": "日常聊天"}, "project": {"title": "专项"}}
    coordinator.edit_draft("daily", "还没有发送")
    anchor = PetWindow()
    chat = PetChatWindow(coordinator)
    qtbot.addWidget(anchor)
    qtbot.addWidget(chat)
    anchor.show()
    chat.open_for(anchor)
    assert chat.chat_view.coordinator is coordinator
    assert chat.chat_view.conversation_id == "daily"
    chat.chat_view.set_conversation("project")
    chat.hide()
    assert id(chat.chat_view) not in coordinator._views
    chat.open_for(anchor)
    assert chat.chat_view.conversation_id == "daily"
    assert chat.chat_view.input.toPlainText() == "还没有发送"
    assert not any(call[1] == "submit" for call in bridge.calls)
    chat.hide()
    anchor.hide()


def test_reminder_notification_requires_same_occurrence_revalidation(qtbot, tmp_path):
    prefs = preferences(tmp_path)
    prefs.set_notifications_enabled(True)
    shown, requests, navigated = [], [], []
    controller = NotificationController(prefs, QSettings(str(tmp_path / "notifications.ini"), QSettings.Format.IniFormat),
        shown.append, lambda: datetime(2026, 10, 25, 12, tzinfo=timezone.utc))
    controller.update_policy({'quiet':{'quiet_start':0, 'quiet_end':0, 'reminder_bypass':False, 'timezone':'UTC'}})
    controller.revalidation_requested.connect(requests.append)
    controller.navigation_requested.connect(lambda kind, identity: navigated.append((kind, identity)))
    controller.ingest([], baseline=True, next_sequence=10)
    reminder = {"kind": "reminder", "entity_id": "r-1", "revision": 3, "status": "due", "label": "私人正文"}
    controller.ingest([reminder], next_sequence=11)
    controller.revalidated([{**reminder, "revision": 4}], requests[-1]["request_id"])
    assert shown == []
    controller.ingest([{**reminder, "revision": 4, "status": "missed"}], next_sequence=12)
    controller.revalidated([{**reminder, "revision": 4, "status": "missed"}], requests[-1]["request_id"])
    assert shown == ["DeskPal 有 1 项更新，请打开应用查看。"]
    controller.activate_notification()
    assert navigated == [("reminder", "r-1")]
    controller.clear_pending()
    controller.activate_notification()
    assert len(navigated) == 1
