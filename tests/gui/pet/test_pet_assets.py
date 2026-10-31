"""T01/T03: fixed resources, budgets and loader ownership using temporary PNGs."""

from threading import Event, get_ident

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from deskpal.gui import pet_assets
from deskpal.gui.pet_assets import AssetResult, PetAssetLoader, expression_id, load_package_images


def png(path, width=32, height=32):
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    image.setPixelColor(width // 2, height // 2, Qt.GlobalColor.darkCyan)
    assert image.save(str(path), "PNG")


def test_t01_fixed_ids_missing_corrupt_and_budget(tmp_path, monkeypatch):
    root = tmp_path / "中文 space" / "assets" / "pet"
    root.mkdir(parents=True)
    monkeypatch.setattr(pet_assets.resources, "files", lambda package: root.parents[1])
    png(root / "neutral.png")
    (root / "focused.png").write_bytes(b"not a png")
    (root / "pleased.png").write_bytes(b"x" * (pet_assets.MAX_FILE_BYTES + 1))
    png(root / "alert.png", width=2049, height=1)
    result = load_package_images(7, Event())
    assert list(result.images) == ["neutral"]
    assert result.generation == 7
    assert "thinking:missing" in result.diagnostics
    assert "focused:image_budget_or_format" in result.diagnostics
    assert "pleased:byte_budget" in result.diagnostics
    assert "alert:image_budget_or_format" in result.diagnostics
    assert expression_id("../../private.png") == "neutral"
    assert expression_id("focused") == "focused"
    (root / "neutral.png").unlink()
    assert "neutral:missing" in load_package_images(8, Event()).diagnostics


def test_t03_one_worker_latest_merge_and_owned_late_completion(qtbot):
    release, entered = Event(), Event()
    calls, deliveries, thread_ids = [], [], []

    def slow(generation, cancel):
        calls.append(generation)
        thread_ids.append(get_ident())
        if generation == 1:
            entered.set()
            assert release.wait(5)
        return AssetResult(generation, {}, ())

    loader = PetAssetLoader(load=slow)
    gui_thread = get_ident()
    loader.ready.connect(deliveries.append)
    try:
        loader.request(1)
        qtbot.waitUntil(entered.is_set)
        first_thread = loader._thread
        loader.request(2)
        loader.request(3)
        assert loader.busy and loader._thread is first_thread
        assert loader._latest == 3
        release.set()
        qtbot.waitUntil(lambda: not loader.busy and len(deliveries) == 2)
        assert calls == [1, 3]
        assert all(thread != gui_thread for thread in thread_ids)
        assert not first_thread.is_alive()
    finally:
        release.set()
        loader.cancel()
        qtbot.waitUntil(lambda: not loader.busy)
