"""T02/T03: read-only polling, freshness, decoration and cancellation races."""

from threading import Event

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtGui import QImage

from deskpal.gui.pet_assets import AssetResult, PetAssetLoader
from deskpal.gui.pet_controller import PetController
from deskpal.gui.preferences import GuiPreferences


class Bridge(QObject):
    reply = Signal(object)
    state_changed = Signal(str)
    privacy_changed = Signal(int)
    state = "Ready"
    privacy_epoch = 0

    def __init__(self):
        super().__init__()
        self.calls = []
        self.full = False

    def send(self, operation, parameters, *, generation=0):
        if self.full:
            raise BufferError("synthetic")
        receipt = str(len(self.calls))
        self.calls.append((receipt, operation, parameters, generation))
        return receipt


def make_pet(tmp_path, *, load=None):
    bridge = Bridge()
    preferences = GuiPreferences(QSettings(str(tmp_path / "pet.ini"), QSettings.Format.IniFormat))
    clock = [100.0]
    loader = PetAssetLoader(load=load or (lambda generation, cancel: AssetResult(generation, {}, ())))
    pet = PetController(bridge, preferences, loader=loader, clock=lambda: clock[0])
    return pet, bridge, preferences, clock


def deliver(pet, bridge, status, *, sequence=1, revision=1, task="t", epoch=None, generation=None,
            memory_epoch=0):
    pet.poll()
    receipt = pet._request
    assert receipt is not None
    request = next(call for call in bridge.calls if call[0] == receipt)
    assert request[1] == "presentation"
    request_generation = request[3]
    bridge.reply.emit({"request_id": receipt, "operation": "presentation", "ok": True,
                       "generation": request_generation if generation is None else generation,
                       "privacy_epoch": bridge.privacy_epoch if epoch is None else epoch,
                       "data": {"schema_version": 1, "sequence": sequence, "status": status,
                                "revision": revision, "task_id": task, "session_id": "s",
                                "memory_epoch": memory_epoch}})


def dispose(qtbot, pet):
    pet.shutdown()
    qtbot.waitUntil(lambda: pet.settled)


def test_t02_baseline_dedup_restore_freshness_and_priority(qtbot, tmp_path):
    pet, bridge, prefs, clock = make_pet(tmp_path)
    try:
        pet.start()
        assert not pet.loader.busy and not bridge.calls  # Disabled means no asset/status work.
        prefs.set_pet_enabled(True)
        qtbot.waitUntil(lambda: pet.settled)
        pet.poll_timer.stop()
        deliver(pet, bridge, "Completed")
        clock[0] += 1
        deliver(pet, bridge, "Completed")
        assert pet.expression == "neutral"  # First snapshot never replays history.
        deliver(pet, bridge, "Planning", sequence=2, revision=2)
        assert pet.status == "Planning" and pet.decoration_timer.isActive()
        deliver(pet, bridge, "Waiting", sequence=3, revision=3)
        assert pet.status == "Waiting" and pet.expression == "neutral"
        assert not pet.decoration_timer.isActive()
        deliver(pet, bridge, "Running", sequence=4, revision=4)
        pet.decoration_timer.stop()
        pet._decorate()
        assert pet.expression == "focused"
        clock[0] += 1
        deliver(pet, bridge, "Completed", sequence=5, revision=5)
        assert pet.expression == "pleased" and pet.success_timer.isActive()
        remaining = pet.success_timer.remainingTime()
        deliver(pet, bridge, "Completed", sequence=5, revision=5)
        assert pet.success_timer.remainingTime() <= remaining
        deliver(pet, bridge, "Failed", sequence=6, revision=6)
        assert pet.expression == "alert" and not pet.success_timer.isActive()
        deliver(pet, bridge, "Running", sequence=4, revision=4)
        assert pet.status == "Unknown"
        prefs.set_pet_visible(False)
        calls = len(bridge.calls)
        pet.poll()
        assert len(bridge.calls) == calls and not pet.poll_timer.isActive()
        prefs.set_pet_visible(True)
        pet.poll_timer.stop()
        deliver(pet, bridge, "Completed", sequence=7, revision=7)
        assert pet.expression == "neutral"
        clock[0] += 4
        pet._expire()
        assert pet.status == "Unknown"
        # Both v0.8 reads are permitted; submit, confirm and tool execution are not.
        assert {item[1] for item in bridge.calls} == {"presentation", "companion"}
        assert all(parameters == {} for _, _, parameters, _ in bridge.calls)
    finally:
        dispose(qtbot, pet)


def test_t02_single_inflight_generation_privacy_and_bounded_retry(qtbot, tmp_path):
    pet, bridge, prefs, clock = make_pet(tmp_path)
    try:
        prefs.set_pet_enabled(True)
        qtbot.waitUntil(lambda: pet.settled)
        pet.poll_timer.stop()
        calls = len(bridge.calls)
        pet.poll()
        pet.poll()
        assert len(bridge.calls) == calls
        deliver(pet, bridge, "Running", generation=-1)
        assert pet.status == "Unknown"
        bridge.privacy_epoch += 1
        bridge.privacy_changed.emit(bridge.privacy_epoch)
        deliver(pet, bridge, "Completed", epoch=0)
        assert pet.status == "Unknown" and not pet.success_timer.isActive()
        bridge.full = True
        pet.poll()
        assert pet._request is None and not pet.decoration_timer.isActive()
        assert pet.status == "Unknown"
    finally:
        dispose(qtbot, pet)


def test_t03_timeout_disable_and_late_result_never_revive_window(qtbot, tmp_path):
    release, entered = Event(), Event()

    def slow(generation, cancel):
        entered.set()
        assert release.wait(5)
        return AssetResult(generation, {}, ())

    pet, bridge, prefs, clock = make_pet(tmp_path, load=slow)
    try:
        prefs.set_pet_enabled(True)
        qtbot.waitUntil(entered.is_set)
        thread = pet.loader._thread
        pet._load_timeout()
        assert pet.diagnostics == ("assets:timeout",)
        assert pet.loader._thread is thread and thread.is_alive()
        prefs.set_pet_enabled(False)
        assert pet.window is None and not pet.poll_timer.isActive()
        assert not pet.settled
        pet.shutdown()
        assert pet.loader._thread is thread
        release.set()
        qtbot.waitUntil(lambda: pet.settled)
        assert pet.window is None and pet.cache_bytes == 0
    finally:
        release.set()
        dispose(qtbot, pet)


def test_t02_hidden_inflight_receipt_clears_without_reviving_old_generation(qtbot, tmp_path):
    pet, bridge, prefs, clock = make_pet(tmp_path)
    try:
        prefs.set_pet_enabled(True)
        qtbot.waitUntil(lambda: pet.settled)
        pet.poll_timer.stop()
        old_receipt, _, _, old_generation = bridge.calls[-1]
        prefs.set_pet_visible(False)
        prefs.set_pet_visible(True)
        pet.poll_timer.stop()
        assert pet._request == old_receipt  # No second simultaneous read.
        deliver(pet, bridge, "Completed", generation=old_generation)
        assert pet._request is None and pet.status == "Unknown"
        pet.poll()
        assert pet._request != old_receipt
        assert bridge.calls[-1][3] != old_generation
        deliver(pet, bridge, "Running", sequence=2, revision=2)
        assert pet.status == "Running"
    finally:
        dispose(qtbot, pet)


def test_t02_advanced_memory_epoch_accepts_same_sequence_without_replaying_success(qtbot, tmp_path):
    pet, bridge, prefs, clock = make_pet(tmp_path)
    try:
        prefs.set_pet_enabled(True)
        qtbot.waitUntil(lambda: pet.settled)
        pet.poll_timer.stop()
        deliver(pet, bridge, "Running", sequence=1, revision=1)
        clock[0] += 1
        deliver(pet, bridge, "Completed", sequence=2, revision=2)
        assert pet.expression == "pleased" and pet.success_timer.isActive()
        token_before = pet._token()
        # No bridge privacy_changed signal and no new audit high-water mark.
        deliver(pet, bridge, "Completed", sequence=2, revision=2, memory_epoch=1)
        assert bridge.privacy_epoch == 0
        assert pet.status == "Completed" and pet.expression == "neutral"
        assert pet._snapshot.memory_epoch == 1 and pet._token() != token_before
        assert not pet.success_timer.isActive() and not pet.decoration_timer.isActive()
        deliver(pet, bridge, "Completed", sequence=2, revision=2, memory_epoch=1)
        assert pet.status == "Completed" and pet.expression == "neutral"
        deliver(pet, bridge, "Completed", sequence=2, revision=3, memory_epoch=2)
        assert pet.status == "Completed" and pet._snapshot.revision == 3
        assert pet._snapshot.memory_epoch == 2 and pet.expression == "neutral"
        # The exception is only for an advancing authority epoch, not conflicts.
        deliver(pet, bridge, "Running", sequence=2, revision=4, memory_epoch=2)
        assert pet.status == "Unknown" and pet._snapshot.revision == 3
        deliver(pet, bridge, "Running", sequence=3, revision=4, memory_epoch=2)
        assert pet.status == "Running" and pet._snapshot.sequence == 3
        assert not pet.success_timer.isActive()
    finally:
        dispose(qtbot, pet)


def test_t01_paint_failure_signal_clears_cache_and_rejects_late_assets(qtbot, tmp_path):
    pet, bridge, prefs, clock = make_pet(tmp_path)
    try:
        prefs.set_pet_enabled(True)
        qtbot.waitUntil(lambda: pet.settled)
        pet.poll_timer.stop()
        image = QImage(32, 32, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.darkCyan)
        ready = AssetResult(pet._asset_generation, {"neutral": image}, ())
        pet.loader.ready.emit(ready)
        assert pet.cache_bytes > 0 and pet._cache and pet.window._pixmap is not None
        pet.window.render_failed.emit("paint:failed")
        assert pet._render_broken and pet.lifecycle == "Degraded"
        assert pet.cache_bytes == 0 and not pet._cache and pet.window._pixmap is None
        assert pet.diagnostics == ("paint:failed",)
        pet.loader.ready.emit(ready)
        assert pet.cache_bytes == 0 and not pet._cache and pet.window._pixmap is None
        assert pet.diagnostics == ("paint:failed",)
        deliver(pet, bridge, "Running")
        assert pet.status == "Running" and pet.window._pixmap is None
    finally:
        dispose(qtbot, pet)
