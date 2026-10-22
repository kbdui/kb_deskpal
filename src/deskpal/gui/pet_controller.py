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
from .companion_state import CompanionSnapshot

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
        self._companion_request: str | None = None
        self._companion_request_at = 0.0
        self._companion_polled = False
        self._companion: CompanionSnapshot | None = None
        self._companion_authority: CompanionSnapshot | None = None
        self._companion_received_at = 0.0
        self._seen: deque[tuple[str, int, str | None, int]] = deque(maxlen=128)
        self._cache: dict[str, QPixmap] = {}
        self.cache_bytes = 0
        self.diagnostics: tuple[str, ...] = ()
        self.lifecycle = "Disabled"
        self.expression = "neutral"
        self.status = "Unknown"
        self._expression_at = 0.0
        self._desired = "neutral"
        self._desired_completion = False
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
        self._clear_companion()
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
        self._clear_companion()
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
        self._clear_companion()
        self._companion_authority = None
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
        self._companion_request = None
        self._clear_companion()
        self._companion_authority = None
        self._snapshot = None
        self._baseline = True
        self._set_unknown()

    def poll(self) -> None:
        if (self.window is None or not self.preferences.pet_visible or self._stopping
                or self.bridge.state != "Ready"):
            return
        if self._request is None:
            try:
                self._request_at = self._clock()
                self._request = self.bridge.send("presentation", {}, generation=self._generation)
            except BufferError:
                # The regular one-second timer is the only capacity retry.
                self._set_unknown()
        self._poll_companion()

    def _reply(self, envelope: Any) -> None:
        if isinstance(envelope, dict) and envelope.get("request_id") == self._companion_request:
            self._companion_reply(envelope)
            return
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
                self._clear_companion()
                self._companion_authority = None
                self._baseline = True
                self._set_expression("neutral")
        baseline, self._baseline = self._baseline, False
        self._snapshot = snapshot
        self._received_at = self._clock()
        self._arm_stale()
        self.status = snapshot.status
        if snapshot.status not in {"Idle", "Completed"}:
            self._clear_companion()
        target = expression_for_status(snapshot.status)
        if snapshot.status == "Idle":
            target = self._companion_expression()
        terminal = (snapshot.session_id, snapshot.memory_epoch, snapshot.task_id, snapshot.revision)
        if snapshot.status == "Completed":
            if baseline or terminal in self._seen:
                target = "neutral"
            if terminal not in self._seen:
                self._seen.append(terminal)
        if snapshot.status in {"Waiting", "Failed", "Unknown", "NeedsReview", "Uncertain"}:
            self._cancel_decorations()
            self._set_expression(target)
        elif snapshot.status == "Completed" and target == "neutral":
            # Preserve an already active single feedback interval without extending it.
            if self._resting():
                self._schedule(self._companion_expression())
        else:
            self._schedule(target, completion=snapshot.status == "Completed" and target == "pleased")
        self._render()  # Text is authoritative immediately, independent of image scheduling.
        self._poll_companion()

    def _poll_companion(self) -> None:
        """Reuse the existing one-second timer; a slow decoration read cannot block status."""
        if (self.window is None or not self.preferences.pet_visible or self._stopping
                or self.bridge.state != "Ready" or self.status not in {"Idle", "Completed"}):
            return
        if self._companion is not None and self._clock() - self._companion_received_at >= FRESH_SECONDS:
            self._clear_companion(restore=True)
        if self._companion_request is not None:
            return
        if self._companion_polled and self._clock() - self._companion_request_at < 1.0:
            return
        try:
            self._companion_request_at = self._clock()
            self._companion_polled = True
            self._companion_request = self.bridge.send("companion", {}, generation=self._generation)
        except BufferError:
            self._clear_companion(restore=True)

    def _companion_reply(self, envelope: dict[str, Any]) -> None:
        self._companion_request = None
        if (self.window is None or not self.preferences.pet_visible or self._stopping
                or self.bridge.state != "Ready" or envelope.get("generation") != self._generation
                or envelope.get("privacy_epoch") != self.bridge.privacy_epoch):
            return
        if not envelope.get("ok") or self._clock() - self._companion_request_at > FRESH_SECONDS:
            self._clear_companion(restore=True)
            return
        try:
            snapshot = CompanionSnapshot.from_payload(envelope.get("data"))
            authority = self._snapshot
            if (authority is None or snapshot.session_id != authority.session_id
                    or snapshot.memory_epoch != authority.memory_epoch):
                raise ValueError("Companion is outside the presentation privacy boundary")
            previous = self._companion_authority
            if previous is not None and not snapshot.follows(previous):
                raise ValueError("Companion state moved backwards")
        except (KeyError, TypeError, ValueError):
            self._clear_companion(restore=True)
            return
        self._companion_authority = snapshot
        # A late response can never decorate an intervening authoritative state.
        if self.status not in {"Idle", "Completed"} or self._clock() - self._received_at >= FRESH_SECONDS:
            self._clear_companion()
            return
        self._companion = snapshot
        self._companion_received_at = self._clock()
        if self._resting():
            target = self._companion_expression()
            if target == "neutral":
                self._clear_companion(restore=True)
            else:
                self._schedule(target)
        self._arm_stale()

    def _resting(self) -> bool:
        return self.status == "Idle" or (
            self.status == "Completed" and not self.success_timer.isActive()
            and not (self.decoration_timer.isActive() and self._desired_completion))

    def _companion_expression(self) -> str:
        value, authority = self._companion, self._snapshot
        if (value is None or authority is None or not self._resting()
                or self._clock() - self._companion_received_at >= FRESH_SECONDS
                or self._clock() - self._received_at >= FRESH_SECONDS
                or not value.enabled or not value.emotions_enabled or value.has_warnings
                or value.session_id != authority.session_id
                or value.memory_epoch != authority.memory_epoch):
            return "neutral"
        # Missing optional images keep the original neutral fallback.
        return value.expression if value.expression in self._cache else "neutral"

    def _clear_companion(self, *, restore: bool = False) -> None:
        self._companion = None
        self._companion_received_at = 0.0
        if restore and self._resting():
            self._cancel_decorations()
            self._set_expression("neutral")

    def _arm_stale(self) -> None:
        deadlines = [self._received_at + FRESH_SECONDS]
        if self._companion is not None:
            deadlines.append(self._companion_received_at + FRESH_SECONDS)
        self.stale_timer.start(max(1, ceil((min(deadlines) - self._clock()) * 1000)))

    def _expire(self) -> None:
        if self._companion is not None and self._clock() - self._companion_received_at >= FRESH_SECONDS:
            self._clear_companion(restore=True)
        remaining = FRESH_SECONDS - (self._clock() - self._received_at)
        if remaining <= 0:
            self._set_unknown()
        else:
            # Coarse Qt timers can fire early. Never lose the freshness deadline.
            self._arm_stale()

    def _set_unknown(self) -> None:
        self.stale_timer.stop()
        self._baseline = True
        self._clear_companion()
        self._cancel_decorations()
        self.status = "Unknown"
        self._set_expression("neutral")

    def _cancel_decorations(self) -> None:
        self.decoration_timer.stop()
        self.success_timer.stop()
        self._decoration_token = None
        self._desired_completion = False

    def _token(self) -> tuple[int, int, int, str | None, int]:
        snapshot = self._snapshot
        return (self._generation, self.bridge.privacy_epoch,
                snapshot.memory_epoch if snapshot else 0,
                snapshot.task_id if snapshot else None, snapshot.revision if snapshot else 0)

    def _schedule(self, target: str, *, completion: bool = False) -> None:
        target = expression_id(target)
        self._desired = target
        self._desired_completion = completion
        self.decoration_timer.stop()
        if target == self.expression:
            # An idle simulated smile cannot consume a new authoritative
            # completion's one finite feedback interval.
            if (completion and target == "pleased"
                    and (not self.success_timer.isActive() or self._decoration_token != self._token())):
                self._decoration_token = self._token()
                self.success_timer.start(1500)
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
        if self._desired_completion and self._desired == "pleased" and self.status == "Completed":
            self.success_timer.start(1500)

    def _success_done(self) -> None:
        if self.expression == "pleased" and self._decoration_token == self._token():
            self._desired_completion = False
            self._set_expression(self._companion_expression())

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
