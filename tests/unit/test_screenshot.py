"""Screenshots use an injected backend; tests never access a real screen."""

import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from deskpal.sandbox import PathViolation, PermissionDenied, WorkspaceSandbox
from deskpal.tools import BuiltinToolManager, ToolCall, ToolContext
from deskpal.tools.screenshot import MSSScreenshotBackend, Monitor, ScreenshotCaptureTool


def png_header(width, height):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    header = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    pixels = (b"\x00" + b"\x00\x00\x00" * width) * height
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


class FakeBackend:
    def __init__(self):
        self.enumerations = 0
        self.captures = []
        self.screens = (Monitor(0, 0, 8, 6), Monitor(-10, -5, 10, 5))
        self.output = None

    def monitors(self):
        self.enumerations += 1
        return self.screens

    def capture(self, region):
        self.captures.append(region)
        return self.output if self.output is not None else png_header(region.width, region.height)


@pytest.fixture
def screenshots(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    backend = FakeBackend()
    sandbox = WorkspaceSandbox(root, max_file_bytes=128)
    manager = BuiltinToolManager(sandbox)
    manager.register(ScreenshotCaptureTool(sandbox, backend=backend, max_pixels=48))
    return root, backend, manager


def authorized(root):
    return ToolContext("task", root, frozenset({"screenshot_capture"}))


def test_permission_preflight_and_denial_never_call_backend_or_write(screenshots):
    root, backend, manager = screenshots
    call = ToolCall("screenshot_capture", {"path": "new/capture.png"})
    assert manager.check_permission(call, authorized(root)) is None
    with pytest.raises(PermissionDenied):
        manager.execute(call, ToolContext("task", root))
    assert backend.enumerations == 0 and backend.captures == []
    assert list(root.iterdir()) == []


def test_default_dependency_import_happens_only_after_authorization(tmp_path, monkeypatch):
    from deskpal.tools import screenshot
    imports = []
    def unavailable(name):
        imports.append(name)
        raise ImportError("missing")
    monkeypatch.setattr(screenshot.importlib, "import_module", unavailable)
    root = tmp_path / "workspace"
    root.mkdir()
    manager = BuiltinToolManager(WorkspaceSandbox(root))
    manager.register(ScreenshotCaptureTool(WorkspaceSandbox(root)))
    assert imports == []
    call = ToolCall("screenshot_capture", {"path": "capture.png"})
    with pytest.raises(PermissionDenied):
        manager.execute(call, ToolContext("task", root))
    assert imports == []
    result = manager.execute(call, authorized(root))
    assert not result.success and "deskpal[screenshot]" in result.error
    assert imports == ["mss"] and list(root.iterdir()) == []


def test_mss_adapter_omits_virtual_monitor_and_encodes_in_memory(monkeypatch):
    from deskpal.tools import screenshot
    opened = []
    captured = []
    encoded = []
    class Session:
        monitors = [{"left": -10, "top": -5, "width": 18, "height": 11},
                    {"left": 0, "top": 0, "width": 8, "height": 6},
                    SimpleNamespace(left=-10, top=-5, width=10, height=5)]
        def __enter__(self):
            opened.append(True)
            return self
        def __exit__(self, *args):
            pass
        def grab(self, region):
            captured.append(region)
            return SimpleNamespace(rgb=b"fake RGB", size=(4, 3))
    def to_png(rgb, size):
        encoded.append((rgb, size))
        return png_header(*size)
    modules = {"mss": SimpleNamespace(mss=Session), "mss.tools": SimpleNamespace(to_png=to_png)}
    monkeypatch.setattr(screenshot.importlib, "import_module", lambda name: modules[name])
    backend = MSSScreenshotBackend()
    assert backend.monitors() == (Monitor(0, 0, 8, 6), Monitor(-10, -5, 10, 5))
    assert backend.capture(Monitor(-4, -3, 4, 3)) == png_header(4, 3)
    assert captured == [{"left": -4, "top": -3, "width": 4, "height": 3}]
    assert encoded == [(b"fake RGB", (4, 3))] and len(opened) == 2


def test_default_capture_writes_only_local_metadata(screenshots):
    root, backend, manager = screenshots
    result = manager.execute(ToolCall("screenshot_capture", {"path": "nested/capture.png"}), authorized(root))
    assert result.success
    assert result.output == {"path": "nested/capture.png", "bytes_written": len(png_header(8, 6)), "width": 8, "height": 6, "monitor": 1}
    assert backend.captures == [Monitor(0, 0, 8, 6)]
    assert (root / "nested/capture.png").read_bytes() == png_header(8, 6)
    assert all(not isinstance(value, bytes) for value in result.output.values())


def test_region_is_relative_to_negative_monitor_origin(screenshots):
    root, backend, manager = screenshots
    result = manager.execute(ToolCall("screenshot_capture", {"path": "capture.png", "monitor": 2,
        "region": {"left": 6, "top": 2, "width": 4, "height": 3}}), authorized(root))
    assert result.success
    assert backend.captures == [Monitor(-4, -3, 4, 3)]
    assert result.output["width"] == 4 and result.output["height"] == 3


@pytest.mark.parametrize("extra", [{"monitor": 0}, {"monitor": True}, {"monitor": 3},
    {"region": {"left": -1, "top": 0, "width": 1, "height": 1}},
    {"region": {"left": 0, "top": 0, "width": 0, "height": 1}},
    {"region": {"left": 7, "top": 0, "width": 2, "height": 1}},
    {"region": {"left": 0, "top": 5, "width": 1, "height": 2}},
    {"region": {"left": 0, "top": 0, "width": True, "height": 1}}, {"monitor": 2}])
def test_invalid_region_monitor_and_pixel_limit_prevent_capture(screenshots, extra):
    root, backend, manager = screenshots
    result = manager.execute(ToolCall("screenshot_capture", {"path": "new/capture.png", **extra}), authorized(root))
    assert not result.success
    assert backend.captures == [] and list(root.iterdir()) == []


@pytest.mark.parametrize("path", ["../escape.png", "NUL.png", "new/file.png:stream", "folder"])
def test_unsafe_path_does_not_even_enumerate_screen(screenshots, path):
    root, backend, manager = screenshots
    (root / "folder").mkdir()
    with pytest.raises(PathViolation):
        manager.execute(ToolCall("screenshot_capture", {"path": path}), authorized(root))
    assert backend.enumerations == 0 and backend.captures == []
    assert not (root / "new").exists()


def test_non_png_suffix_and_existing_output_fail_before_backend(screenshots):
    root, backend, manager = screenshots
    (root / "capture.png").write_bytes(b"old")
    for path in ("new/capture.jpg", "capture.png"):
        result = manager.execute(ToolCall("screenshot_capture", {"path": path}), authorized(root))
        assert not result.success
    assert backend.enumerations == 0 and backend.captures == []
    assert (root / "capture.png").read_bytes() == b"old"
    assert not (root / "new").exists()
    result = manager.execute(ToolCall("screenshot_capture", {"path": "capture.png", "overwrite": True}), authorized(root))
    assert result.success and (root / "capture.png").read_bytes() == png_header(8, 6)


def test_png_byte_limit_failure_preserves_old_output_and_creates_no_parents(screenshots):
    root, backend, manager = screenshots
    (root / "capture.png").write_bytes(b"old")
    backend.output = png_header(8, 6) + b"x" * 129
    for path in ("capture.png", "new/capture.png"):
        result = manager.execute(ToolCall("screenshot_capture", {"path": path, "overwrite": True}), authorized(root))
        assert not result.success
    assert (root / "capture.png").read_bytes() == b"old" and not (root / "new").exists()


@pytest.mark.parametrize("data", [b"not a PNG", png_header(1, 1)])
def test_invalid_backend_output_does_not_create_files(screenshots, data):
    root, backend, manager = screenshots
    backend.output = data
    result = manager.execute(ToolCall("screenshot_capture", {"path": "new/capture.png"}), authorized(root))
    assert not result.success and "invalid PNG" in result.error
    assert list(root.iterdir()) == []


def test_failed_atomic_capture_overwrite_keeps_prior_bytes(screenshots, monkeypatch):
    root, _, manager = screenshots
    (root / "capture.png").write_bytes(b"old")
    def fail_replace(*args, **kwargs):
        raise OSError("replacement failed")
    monkeypatch.setattr(Path, "replace", fail_replace)
    result = manager.execute(ToolCall("screenshot_capture", {"path": "capture.png", "overwrite": True}), authorized(root))
    assert not result.success and (root / "capture.png").read_bytes() == b"old"
    assert list(root.iterdir()) == [root / "capture.png"]


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_max_pixels_rejected(tmp_path, value):
    with pytest.raises(ValueError):
        ScreenshotCaptureTool(WorkspaceSandbox(tmp_path), max_pixels=value)
