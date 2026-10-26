"""Prepared, not executed in D1-D5: synthetic local appearance contracts."""

import os
from pathlib import Path
import struct
import zlib

import pytest

from deskpal.assets import AssetError, AssetService, NormalizedImage
from deskpal.workflow import WorkflowStore


def png(pixel=b"\x12\x34\x56\xff"):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0" + pixel)) + chunk(b"IEND", b"")


class Decoder:
    def __init__(self, visible=True):
        self.visible = visible

    def normalize(self, raw):
        return NormalizedImage(png(), 1, 1, self.visible)

    def thumbnail(self, raw, max_edge):
        return self.normalize(raw)


@pytest.mark.parametrize("format_name,alpha", [("png", 127), ("png", 0), ("jpg", 255)])
def test_qt_decoder_normalizes_static_images_and_preserves_preview_pixels(tmp_path, format_name, alpha):
    qtgui = pytest.importorskip("PySide6.QtGui")
    from deskpal.gui.asset_decoder import QtAssetDecoder

    source = qtgui.QImage(8, 4, qtgui.QImage.Format.Format_RGBA8888)
    expected = qtgui.QColor(0, 0, 255, alpha)
    source.fill(expected)
    source.setText("private-source", "synthetic metadata must be discarded")
    selected = tmp_path / ("selected." + format_name)
    assert source.save(str(selected))
    decoder = QtAssetDecoder()
    normalized = decoder.normalize(selected.read_bytes())
    assert isinstance(normalized.png, bytes) and normalized.png.startswith(b"\x89PNG\r\n\x1a\n")
    assert (normalized.width, normalized.height) == (8, 4)
    assert normalized.has_visible_pixels is (alpha > 0)
    preview = decoder.thumbnail(normalized.png, max_edge=4)
    assert (preview.width, preview.height) == (4, 2)
    assert preview.has_visible_pixels is normalized.has_visible_pixels
    for result in (normalized, preview):
        image = qtgui.QImage.fromData(result.png)
        assert not image.isNull() and not image.textKeys()
        actual = image.pixelColor(image.width() // 2, image.height() // 2)
        assert actual.alpha() == alpha
        if alpha:
            # JPEG is lossy; PNG and its preview retain the exact RGBA color.
            tolerance = 3 if format_name == "jpg" else 0
            assert all(abs(left - right) <= tolerance for left, right in zip(actual.getRgb()[:3], expected.getRgb()[:3]))


@pytest.fixture
def appearance(tmp_path):
    directory = tmp_path / "synthetic-data"
    store = WorkflowStore(directory / "workflow.sqlite3")
    source = tmp_path / "selected.png"
    source.write_bytes(png())
    service = AssetService(store, directory, decoder=Decoder())
    try:
        yield service, store, source, directory
    finally:
        store.close()


def test_apply_is_cas_and_slots_fall_back_to_current_user_main(appearance):
    service, store, source, directory = appearance
    initial = source.read_bytes()
    selected = service.prepare(source, "pet_main", generation=1)
    profile = service.apply("pet_main", selected.token, 1, generation=1)
    snapshot = {value.slot: value for value in service.snapshot()}
    assert snapshot["pet_thinking"].effective_asset_id == profile.asset_id
    assert snapshot["pet_alert"].builtin is False
    assert snapshot["user_avatar"].builtin is True
    assert source.read_bytes() == initial
    assert service.preview(asset_id=profile.asset_id).png == png()
    replacement = service.prepare(source, "pet_main", generation=2)
    with pytest.raises(AssetError) as conflict:
        service.apply("pet_main", replacement.token, 1, generation=2)
    assert conflict.value.code == "asset_revision_conflict"
    assert {value.slot: value for value in service.snapshot()}["pet_main"].asset_id == profile.asset_id
    service.reset("user_avatar", 1)
    assert {value.slot: value for value in service.snapshot()}["pet_main"].asset_id == profile.asset_id
    assert sorted(path.suffix for path in (directory / "assets").iterdir()) == [".png"]


def test_latest_selection_invalidates_prior_token_without_switching_profile(appearance):
    service, store, source, directory = appearance
    old = service.prepare(source, "character_avatar", generation=10)
    latest = service.prepare(source, "character_avatar", generation=11)
    with pytest.raises(AssetError) as expired:
        service.apply("character_avatar", old.token, 1, generation=10)
    assert expired.value.code == "asset_import_expired"
    with pytest.raises(AssetError) as stale:
        service.prepare(source, "character_avatar", generation=10)
    assert stale.value.code == "asset_generation_stale"
    profile = service.apply("character_avatar", latest.token, 1, generation=11, display_mode="circle")
    assert profile.display_mode == "circle"
    assert profile.revision == 2
    assert service.discard(latest.token) is False


def test_transparent_pet_is_rejected_and_cli_decoder_is_explicitly_optional(appearance):
    service, store, source, directory = appearance
    transparent = AssetService(store, directory, decoder=Decoder(visible=False))
    with pytest.raises(AssetError) as rejected:
        transparent.prepare(source, "pet_main", generation=1)
    assert rejected.value.code == "asset_pet_transparent"
    no_codec = AssetService(store, directory)
    assert all(profile.builtin for profile in no_codec.snapshot())
    with pytest.raises(AssetError) as unavailable:
        no_codec.prepare(source, "user_avatar", generation=1)
    assert unavailable.value.code == "asset_decoder_unavailable"
    assert "不可用" in str(unavailable.value)


def test_exact_interrupted_publication_is_repaired_but_external_links_rejected(appearance):
    service, store, source, directory = appearance
    root = directory / "assets"
    root.mkdir()
    asset_id = "a" * 32
    temporary = root / (".import-" + asset_id + ".tmp")
    temporary.write_bytes(png())
    destination = root / (asset_id + ".png")
    os.link(temporary, destination)
    recovered = AssetService(store, directory, decoder=Decoder())
    assert not temporary.exists()
    assert destination.stat().st_nlink == 1
    assert recovered.collect().removed_files == 1
    outside = source.parent / "unowned-hardlink.png"
    outside.write_bytes(png())
    os.link(outside, destination)
    with pytest.raises(AssetError) as rejected:
        AssetService(store, directory)
    assert rejected.value.code == "asset_path_unsafe"
    assert outside.exists() and destination.exists()


def test_normal_replacements_collect_old_copies_and_preserve_live_reference(appearance):
    service, store, source, directory = appearance
    revision = 1
    for generation in range(1, 5):
        prepared = service.prepare(source, "user_avatar", generation=generation)
        profile = service.apply("user_avatar", prepared.token, revision, generation=generation)
        revision = profile.revision
    result = service.collect()
    assert result.removed_assets == 1
    assert len(tuple((directory / "assets").iterdir())) == 1
    assert service.preview(asset_id=profile.asset_id).png == png()
