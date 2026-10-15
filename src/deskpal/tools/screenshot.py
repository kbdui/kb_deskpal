"""Authorized local PNG capture. Importing this module never loads MSS."""

import importlib
import struct
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, Protocol, Sequence

from deskpal.sandbox.workspace import WorkspaceSandbox

from ._schema import ArgumentError
from .models import PermissionLevel, ToolContext, ToolResult, ToolSpec


@dataclass(frozen=True)
class Monitor:
    left: int
    top: int
    width: int
    height: int


class ScreenshotBackend(Protocol):
    """Monitors omit MSS's synthetic all-monitor entry. Capture returns PNG."""

    def monitors(self) -> Sequence[Monitor]: ...

    def capture(self, region: Monitor) -> bytes: ...


class ScreenshotUnavailable(RuntimeError):
    pass


class MSSScreenshotBackend:
    """MSS sessions and PNG encoding begin only during an authorized execute."""

    @staticmethod
    def _modules() -> tuple[Any, Any]:
        try:
            return importlib.import_module("mss"), importlib.import_module("mss.tools")
        except ImportError:
            raise ScreenshotUnavailable("Screenshot dependency is unavailable; install deskpal[screenshot]") from None

    def monitors(self) -> Sequence[Monitor]:
        mss, _ = self._modules()
        with mss.mss() as session:
            result = []
            for screen in session.monitors[1:]:
                def value(name: str) -> int:
                    result: object = screen[name] if isinstance(screen, Mapping) else getattr(screen, name)
                    if type(result) is not int:
                        raise ScreenshotUnavailable("Screenshot monitor metadata must contain integers")
                    return result
                result.append(Monitor(value("left"), value("top"), value("width"), value("height")))
            return tuple(result)

    def capture(self, region: Monitor) -> bytes:
        mss, encoding = self._modules()
        with mss.mss() as session:
            image = session.grab({"left": region.left, "top": region.top,
                                  "width": region.width, "height": region.height})
            # With output omitted, MSS returns PNG bytes without creating a file.
            png: object = encoding.to_png(image.rgb, image.size)
            if not isinstance(png, bytes):
                raise ScreenshotUnavailable("Screenshot encoder did not return PNG bytes")
            return png


class ScreenshotCaptureTool:
    def __init__(self, sandbox: WorkspaceSandbox, *, backend: ScreenshotBackend | None = None,
                 max_pixels: int = 16_000_000) -> None:
        if type(max_pixels) is not int or max_pixels <= 0:
            raise ValueError("max_pixels must be a positive integer")
        self._sandbox = sandbox
        self._backend = backend if backend is not None else MSSScreenshotBackend()
        self._max_pixels = max_pixels

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("screenshot_capture", "Capture an authorized screen region to a local workspace PNG.", {
            "type": "object", "properties": {
                "path": {"type": "string", "minLength": 1},
                "monitor": {"type": "integer", "minimum": 1, "default": 1},
                "region": {"type": "object", "properties": {
                    "left": {"type": "integer", "minimum": 0},
                    "top": {"type": "integer", "minimum": 0},
                    "width": {"type": "integer", "minimum": 1},
                    "height": {"type": "integer", "minimum": 1},
                }, "required": ["left", "top", "width", "height"], "additionalProperties": False},
                "overwrite": {"type": "boolean", "default": False},
            }, "required": ["path"], "additionalProperties": False,
        }, PermissionLevel.CONFIRM)

    def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        path = arguments["path"]
        target = self._sandbox.resolve_path(path, for_write=True)
        if PurePosixPath(path.replace("\\", "/")).suffix.casefold() != ".png":
            raise ArgumentError("Screenshot path must end in .png")
        overwrite = arguments.get("overwrite", False)
        if target.exists() and not overwrite:
            raise FileExistsError("Workspace file already exists")
        number = arguments.get("monitor", 1)
        try:
            monitors = self._backend.monitors()
        except ScreenshotUnavailable as error:
            return ToolResult(False, error=str(error))
        except Exception:
            return ToolResult(False, error="Screen monitor enumeration failed")
        if type(number) is not int or number < 1 or number > len(monitors):
            raise ArgumentError("Monitor index does not select an available physical screen")
        selected = monitors[number - 1]
        if not isinstance(selected, Monitor) or any(type(value) is not int for value in
                (selected.left, selected.top, selected.width, selected.height)) or min(selected.width, selected.height) <= 0:
            return ToolResult(False, error="Screen backend returned invalid monitor dimensions")
        supplied = arguments.get("region")
        if supplied is None:
            region = selected
        else:
            left, top, width, height = (supplied[key] for key in ("left", "top", "width", "height"))
            if any(type(value) is not int for value in (left, top, width, height)) or left < 0 or top < 0 or min(width, height) <= 0:
                raise ArgumentError("Screenshot region requires nonnegative offsets and positive dimensions")
            if left + width > selected.width or top + height > selected.height:
                raise ArgumentError("Screenshot region exceeds the selected monitor")
            region = Monitor(selected.left + left, selected.top + top, width, height)
        if region.width * region.height > self._max_pixels:
            raise ArgumentError("Screenshot exceeds the configured pixel limit")
        if self._sandbox.resolve_path(path, for_write=True) != target:
            raise ArgumentError("Screenshot output path changed")
        try:
            data = self._backend.capture(region)
        except ScreenshotUnavailable as error:
            return ToolResult(False, error=str(error))
        except Exception:
            return ToolResult(False, error="Screen capture failed")
        if (not isinstance(data, bytes) or len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n"
                or data[12:16] != b"IHDR" or struct.unpack(">II", data[16:24]) != (region.width, region.height)):
            return ToolResult(False, error="Screen backend returned invalid PNG dimensions")
        output = self._sandbox.write_bytes(path, data, overwrite=overwrite)
        return ToolResult(True, output={**output, "width": region.width, "height": region.height, "monitor": number})
