"""One GUI-owned pet; read-only presentation polling and bounded decoration."""

from collections import deque
from collections.abc import Callable
from math import ceil
from time import monotonic
from typing import Any

from PySide6.QtCore import QObject, QPoint, QTimer, Signal
from PySide6.QtGui import QPixmap

from deskpal.presentation.models import PresentationSnapshot
from deskpal.presentation.state import expression_for_status, status_label

from .pet_assets import AssetResult, MAX_CACHE_BYTES, PREPARED_EDGE, PetAssetLoader, expression_id
from .pet_window import PetWindow
from .preferences import GuiPreferences

FRESH_SECONDS = 3.0


class PetController(QObject):
    navigation_requested = Signal()
    stopped = Signal()

    def __init__(self, bridge: Any, preferences: GuiPreferences, *,
                 loader: PetAssetLoader | None = None,
                 clock: Callable[[], float] = monotonic,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.preferences = preferences
        self.loader = loader if loader is not None else PetAssetLoader(self)
        self._clock = clock
        self.window: PetWindow | None = None
        self._retired_windows: dict[int, PetWindow] = {}
        self._generation = 0
        self._asset_generation = 0
        self._request: str | None = None
        self._request_at = 0.0
        self._baseline = True
        self._snapshot: PresentationSnapshot | None = None
        self._received_at = 0.0
        self._seen: deque[tuple[str, int, str | None, int]] = deque(maxlen=128)
        self._cache: dict[str, QPixmap] = {}
        self.cache_bytes = 0
        self.diagnostics: tuple[str, ...] = ()
        self.lifecycle = "Disabled"
        self.expression = "neutral"
        self.status = "Unknown"
        self._expression_at = 0.0
        self._desired = "neutral"
        self._decoration_token: tuple[int, int, int, str | None, int] | None = None
        self._stopping = False
        self._render_broken = False
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(1000)
        self.poll_timer.timeout.connect(self.poll)
        self.stale_timer = QTimer(self)
        self.stale_timer.setSingleShot(True)
        self.stale_timer.timeout.connect(self._expire)
        self.decoration_timer = QTimer(self)
        self.decoration_timer.setSingleShot(True)
        self.decoration_timer.timeout.connect(self._decorate)
        self.success_timer = QTimer(self)
        self.success_timer.setSingleShot(True)
        self.success_timer.timeout.connect(self._success_done)
        self.load_timer = QTimer(self)
        self.load_timer.setSingleShot(True)
        self.load_timer.timeout.connect(self._load_timeout)
        self.loader.ready.connect(self._assets_ready)
        self.loader.idle.connect(self._loader_idle)
        self.bridge.reply.connect(self._reply)
        self.bridge.state_changed.connect(self._host_state)
        self.bridge.privacy_changed.connect(self._privacy)
        self.preferences.changed.connect(self._preference)

    @property
    def settled(self) -> bool:
        return not self.loader.busy and not self._retired_windows

    def start(self) -> None:
        if self.preferences.pet_enabled:
            self._enable()

    def _enable(self) -> None:
        if self._stopping or self.window is not None:
            return
        self._generation += 1
        self._baseline = True
        self._snapshot = None
        self.window = PetWindow()
        self.diagnostics = ()
        self._render_broken = False
        self.window.navigate_requested.connect(self.navigation_requested)
        self.window.disable_requested.connect(lambda: self.preferences.set_pet_enabled(False))
        self.window.position_changed.connect(self._save_position)
        self.window.render_failed.connect(self._paint_failed)
        self.window.move(QPoint(*self.preferences.pet_position))
        self.window.fit_to_screens()
        self.window.set_native_options(on_top=self.preferences.pet_on_top,
                                       click_through=self.preferences.pet_click_through)
        self._asset_generation += 1
        self.lifecycle = "Loading"
        self.loader.request(self._asset_generation)
        self.load_timer.start(2000)
        self._set_unknown()
        self._visibility()

    def _visibility(self) -> None:
        window = self.window
        if window is None or self._stopping:
            return
        self._generation += 1
        self._baseline = True
        self._snapshot = None
        self._cancel_decorations()
        self._set_unknown()
        if self.preferences.pet_visible:
            window.show()
            self.lifecycle = "Degraded" if not self._cache else "Visible"
            if self.bridge.state == "Ready":
                self.poll_timer.start()
                self.poll()
        else:
            self.lifecycle = "Hidden"
            window.hide()
            self.poll_timer.stop()
            self.stale_timer.stop()

    def disable(self) -> None:
        self._generation += 1
        self._asset_generation += 1
        self.poll_timer.stop()
        self.stale_timer.stop()
        self.load_timer.stop()
        self._cancel_decorations()
        self.loader.cancel()
        window, self.window = self.window, None
        if window is not None:
            window.hide()
            identity = id(window)
            self._retired_windows[identity] = window
            window.destroyed.connect(lambda _object=None, key=identity: self._window_destroyed(key))
            window.deleteLater()
        self._cache.clear()
        self.cache_bytes = 0
        self._snapshot = None
        self._baseline = True
        self.lifecycle = "Stopping" if not self.settled else "Disabled"

    def _window_destroyed(self, identity: int) -> None:
        self._retired_windows.pop(identity, None)
        if self.window is None and self.settled:
            self.lifecycle = "Disabled"
        if self._stopping and self.settled:
            self.stopped.emit()

    def shutdown(self) -> None:
        self._stopping = True
        self.disable()
        if self.settled:
            self.stopped.emit()

    def _loader_idle(self) -> None:
        if self.window is None:
            self.lifecycle = "Disabled"
        if self._stopping and self.settled:
            self.stopped.emit()

    def _load_timeout(self) -> None:
        self._asset_generation += 1
        self.loader.cancel()
        self.diagnostics = ("assets:timeout",)
        if self.window is not None:
            self.lifecycle = "Degraded" if self.window.isVisible() else "Hidden"
            self._render()

    def _assets_ready(self, result: AssetResult) -> None:
        if (result.generation != self._asset_generation or self.window is None
                or self._stopping):
            return
        self.load_timer.stop()
        if self._render_broken:
            return
        self.diagnostics = result.diagnostics
        cache: dict[str, QPixmap] = {}
        count = 0
        # One fixed bounded physical resolution. Qt scales this correctly across
        # DPR changes; no per-screen cache accumulates and hit regions stay logical.
        for name, image in result.images.items():
            if image.width() > PREPARED_EDGE or image.height() > PREPARED_EDGE:
                self.diagnostics += ("assets:unprepared_image",)
                continue
            cost = image.width() * image.height() * 4
            if count + cost > MAX_CACHE_BYTES:
                continue
            pixmap = QPixmap.fromImage(image)
            pixmap.setDevicePixelRatio(2)
            cache[name] = pixmap
            count += cost
        if "neutral" not in cache:
            cache.clear()
            count = 0
        self._cache = cache
        self.cache_bytes = count
        if "neutral" in cache:
            self.window.set_stable_mask(cache["neutral"])
        self.lifecycle = ("Visible" if cache else "Degraded") if self.window.isVisible() else "Hidden"
        self._render()

    def _paint_failed(self, code: str) -> None:
        self._render_broken = True
        self._cache.clear()
        self.cache_bytes = 0
        self.diagnostics += (code,)
        if self.window is not None:
            self.window.set_stable_mask(None)
        self.lifecycle = "Degraded" if self.window is not None and self.window.isVisible() else "Hidden"
        self._render()

    def _save_position(self, point: QPoint, screen: str) -> None:
        self.preferences.set_pet_position((point.x(), point.y()), screen)

    def _preference(self, key: str, value: object) -> None:
        if key == "pet_enabled":
            if value:
                self._enable()
            else:
                self.disable()
        elif key == "pet_visible":
            self._visibility()
        elif key in {"pet_on_top", "pet_click_through"} and self.window is not None:
            self.window.set_native_options(on_top=self.preferences.pet_on_top,
                                          click_through=self.preferences.pet_click_through)

    def _host_state(self, state: str) -> None:
        if state in {"Closing", "Closed"}:
            self.status = "Closing"
            self.shutdown()
        elif state == "Ready" and self.window is not None and self.preferences.pet_visible:
            self._visibility()
        elif state != "Ready":
            self.poll_timer.stop()
            self._set_unknown()

    def _privacy(self, epoch: int) -> None:
        self._generation += 1
        # Bridge drops these receipts itself after an epoch change.
        self._request = None
        self._snapshot = None
        self._baseline = True
        self._set_unknown()

    def poll(self) -> None:
        if (self.window is None or not self.preferences.pet_visible or self._stopping
                or self.bridge.state != "Ready" or self._request is not None):
            return
        try:
            self._request_at = self._clock()
            self._request = self.bridge.send("presentation", {}, generation=self._generation)
        except BufferError:
            # The regular one-second timer is the only capacity retry.
            self._set_unknown()

    def _reply(self, envelope: Any) -> None:
        if not isinstance(envelope, dict) or envelope.get("request_id") != self._request:
            return
        self._request = None
        if (self.window is None or not self.preferences.pet_visible or self._stopping
                or self.bridge.state != "Ready" or envelope.get("generation") != self._generation
                or envelope.get("privacy_epoch") != self.bridge.privacy_epoch):
            return
        if not envelope.get("ok") or self._clock() - self._request_at > FRESH_SECONDS:
            self._set_unknown()
            return
        try:
            snapshot = PresentationSnapshot.from_payload(envelope["data"])
        except (KeyError, TypeError, ValueError):
            self._set_unknown()
            return
        previous = self._snapshot
        if previous is not None:
            if (snapshot.session_id != previous.session_id
                    or snapshot.memory_epoch < previous.memory_epoch
                    or snapshot.sequence < previous.sequence):
                self._set_unknown()
                return
            memory_advanced = snapshot.memory_epoch > previous.memory_epoch
            if (snapshot.task_id == previous.task_id and snapshot.revision < previous.revision):
                self._set_unknown()
                return
            if (not memory_advanced and snapshot.sequence == previous.sequence
                    and snapshot != previous):
                self._set_unknown()
                return
            if memory_advanced:
                # A scrub can advance authority metadata without adding audit
                # events. Its same-sequence snapshot establishes a new baseline;
                # no old decoration survives the database privacy boundary.
                self._cancel_decorations()
                self._baseline = True
                self._set_expression("neutral")
        baseline, self._baseline = self._baseline, False
        self._snapshot = snapshot
        self._received_at = self._clock()
        self.stale_timer.start(int(FRESH_SECONDS * 1000))
        self.status = snapshot.status
        target = expression_for_status(snapshot.status)
        terminal = (snapshot.session_id, snapshot.memory_epoch, snapshot.task_id, snapshot.revision)
        if snapshot.status == "Completed":
            if baseline or terminal in self._seen:
                target = "neutral"
            if terminal not in self._seen:
                self._seen.append(terminal)
        if snapshot.status in {"Waiting", "Failed", "Unknown"}:
            self._cancel_decorations()
            self._set_expression(target)
        elif snapshot.status == "Completed" and target == "neutral":
            # Preserve an already active single feedback interval without extending it.
            if self.expression != "pleased" or not self.success_timer.isActive():
                self._schedule("neutral")
        else:
            self._schedule(target)
        self._render()  # Text is authoritative immediately, independent of image scheduling.

    def _expire(self) -> None:
        remaining = FRESH_SECONDS - (self._clock() - self._received_at)
        if remaining <= 0:
            self._set_unknown()
        else:
            # Coarse Qt timers can fire early. Never lose the freshness deadline.
            self.stale_timer.start(max(1, ceil(remaining * 1000)))

    def _set_unknown(self) -> None:
        self.stale_timer.stop()
        self._baseline = True
        self._cancel_decorations()
        self.status = "Unknown"
        self._set_expression("neutral")

    def _cancel_decorations(self) -> None:
        self.decoration_timer.stop()
        self.success_timer.stop()
        self._decoration_token = None

    def _token(self) -> tuple[int, int, int, str | None, int]:
        snapshot = self._snapshot
        return (self._generation, self.bridge.privacy_epoch,
                snapshot.memory_epoch if snapshot else 0,
                snapshot.task_id if snapshot else None, snapshot.revision if snapshot else 0)

    def _schedule(self, target: str) -> None:
        target = expression_id(target)
        self._desired = target
        self.decoration_timer.stop()
        if target == self.expression:
            return
        self.success_timer.stop()
        # Activity must immediately supersede success. Thinking alone has a short
        # delay; minimum dwell only decorates other noncritical image transitions.
        delay = 250 if target == "thinking" else 0
        if self.expression != "pleased":
            delay = max(delay, round(max(0.0, .5 - (self._clock() - self._expression_at)) * 1000))
        self._decoration_token = self._token()
        if delay:
            self.decoration_timer.start(delay)
        else:
            self._decorate()

    def _decorate(self) -> None:
        if (self._decoration_token != self._token() or self._stopping or self.window is None
                or not self.preferences.pet_visible or self.status == "Unknown"):
            return
        self._set_expression(self._desired)
        if self._desired == "pleased":
            self.success_timer.start(1500)

    def _success_done(self) -> None:
        if self.expression == "pleased" and self._decoration_token == self._token():
            self._set_expression("neutral")

    def _set_expression(self, value: str) -> None:
        value = expression_id(value)
        if value != self.expression:
            self.expression = value
            self._expression_at = self._clock()
        self._render()

    def _render(self) -> None:
        if self.window is not None:
            pixmap = self._cache.get(self.expression, self._cache.get("neutral"))
            self.window.present(pixmap, status_label(self.status), "; ".join(self.diagnostics))
