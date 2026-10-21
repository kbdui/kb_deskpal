"""Bounded, fixed package PNG loading. No caller supplied resource paths."""

from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from threading import Event, Thread

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QTimer, Qt, Signal
from PySide6.QtGui import QImage, QImageReader

EXPRESSIONS = ("neutral", "thinking", "focused", "pleased", "alert")
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 8_000_000
MAX_CACHE_BYTES = 32 * 1024 * 1024
PREPARED_EDGE = 480


@dataclass(frozen=True)
class AssetResult:
    generation: int
    images: dict[str, QImage]
    diagnostics: tuple[str, ...]


def expression_id(value: str) -> str:
    return value if value in EXPRESSIONS else "neutral"


def load_package_images(generation: int, cancel: Event) -> AssetResult:
    """Worker only: reader/buffer/file ownership never crosses to the GUI."""
    images: dict[str, QImage] = {}
    diagnostics: list[str] = []
    total_bytes = total_pixels = 0
    source_size: tuple[int, int] | None = None
    root = resources.files("deskpal.gui").joinpath("assets", "pet")
    for name in EXPRESSIONS:
        if cancel.is_set():
            break
        try:
            with root.joinpath(name + ".png").open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            diagnostics.append(name + ":missing")
            continue
        except OSError:
            diagnostics.append(name + ":read_failed")
            continue
        if len(raw) > MAX_FILE_BYTES or total_bytes + len(raw) > MAX_TOTAL_BYTES:
            diagnostics.append(name + ":byte_budget")
            continue
        total_bytes += len(raw)
        buffer = QBuffer()
        buffer.setData(QByteArray(raw))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer, b"png")
        reader.setDecideFormatFromContent(True)
        size = reader.size()
        pixels = size.width() * size.height()
        if (reader.format() != b"png" or not size.isValid() or size.width() > 2048
                or size.height() > 2048 or pixels + total_pixels > MAX_PIXELS):
            diagnostics.append(name + ":image_budget_or_format")
            buffer.close()
            continue
        image = reader.read()
        buffer.close()
        if image.isNull():
            diagnostics.append(name + ":decode_failed")
            continue
        if not image.hasAlphaChannel():
            diagnostics.append(name + ":alpha_missing")
            continue
        if source_size is not None and (image.width(), image.height()) != source_size:
            diagnostics.append(name + ":canvas_mismatch")
            continue
        total_pixels += pixels
        source_size = (image.width(), image.height())
        # A deep, independently owned buffer is published once and never mutated.
        images[name] = image.scaled(PREPARED_EDGE, PREPARED_EDGE,
                                    Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation).copy()
    return AssetResult(generation, images, tuple(diagnostics))


class PetAssetLoader(QObject):
    """Own one worker through completion, including cancellation and timeout."""

    ready = Signal(object)
    idle = Signal()
    _completed = Signal(object)

    def __init__(self, parent: QObject | None = None, *,
                 load: Callable[[int, Event], AssetResult] = load_package_images) -> None:
        super().__init__(parent)
        self._load = load
        self._thread: Thread | None = None
        self._cancel = Event()
        self._latest: int | None = None
        self._result: AssetResult | None = None
        self._completed.connect(self._receive, Qt.ConnectionType.QueuedConnection)
        self._reap_timer = QTimer(self)
        self._reap_timer.setSingleShot(True)
        self._reap_timer.timeout.connect(self._reap)

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def request(self, generation: int) -> None:
        if self.busy:
            self._latest = generation
            self._cancel.set()
            return
        self._cancel = Event()
        cancel = self._cancel

        def work() -> None:
            try:
                result = self._load(generation, cancel)
            except Exception:
                result = AssetResult(generation, {}, ("assets:worker_failed",))
            self._completed.emit(result)

        self._thread = Thread(target=work, name="deskpal-pet-assets", daemon=False)
        self._thread.start()

    def cancel(self) -> None:
        self._latest = None
        self._cancel.set()

    def _receive(self, result: AssetResult) -> None:
        self._result = result
        self._reap()

    def _reap(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._reap_timer.start(10)
            return
        self._thread = None
        result, self._result = self._result, None
        latest, self._latest = self._latest, None
        if result is not None:
            self.ready.emit(result)
        if latest is not None:
            self.request(latest)
        else:
            self.idle.emit()
