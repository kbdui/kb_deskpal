"""Shared GUI-owned image projections and reversible import previews."""
from typing import Any
from PySide6.QtCore import QObject, Qt, Signal, QRectF
from PySide6.QtGui import QPixmap, QPainter, QPainterPath
from .channel import Channel

class AppearanceController(QObject):
    changed = Signal()
    image_changed = Signal(str, object)
    def __init__(self, bridge: Any, parent: Any = None) -> None:
        super().__init__(parent); self.bridge = bridge; self.channel = Channel(bridge, self)
        self.profiles: dict[str, dict[str, Any]] = {}; self.images: dict[str, QPixmap] = {}; self.pending: dict[str, tuple[str, int, int, tuple[Any, ...]]] = {}; self.latest: dict[str, int] = {}; self.generation = 0; self._reading = False
        if hasattr(bridge, "image_reply"): bridge.image_reply.connect(self._image)
        if hasattr(bridge, "privacy_changed"): bridge.privacy_changed.connect(self.clear)
    def refresh(self) -> None:
        if self._reading: return
        self._reading = True
        def done(data: dict[str, Any]) -> None:
            self._reading = False
            if "_error" in data: return
            self.profiles = {item["slot"]: item for item in data.get("profiles", [])}
            self.changed.emit()
            for slot in ["character_avatar", "user_avatar"]:
                profile = self.profiles.get(slot, {}); asset_id = profile.get("effective_asset_id", profile.get("asset_id"))
                if asset_id: self.preview(slot, str(asset_id))
                else:
                    self.latest[slot] = self.latest.get(slot, 0) + 1
                    self.images.pop(slot, None); self.image_changed.emit(slot, QPixmap())
        if self.channel.send("appearance_snapshot", {}, done) is None: self._reading = False
    def preview(self, slot: str, asset_id: str) -> None:
        self.generation += 1
        self.latest[slot] = self.generation
        profile = self.profiles.get(slot, {})
        signature = (asset_id, profile.get("revision"), profile.get("display_mode"), tuple(profile.get("crop", [])))
        try:
            identity = self.bridge.send_image("asset_preview", {"asset_id": asset_id, "max_edge": 256}, self.generation)
            self.pending[str(identity)] = (slot, self.generation, int(getattr(self.bridge, "privacy_epoch", 0)), signature)
        except (ValueError, RuntimeError, BufferError): pass
    def _image(self, envelope: Any) -> None:
        if not isinstance(envelope, dict): return
        item = self.pending.pop(str(envelope.get("request_id", "")), None)
        if item is None: return
        slot, generation, epoch, signature = item
        profile = self.profiles.get(slot, {})
        current = (str(profile.get("effective_asset_id", profile.get("asset_id"))), profile.get("revision"), profile.get("display_mode"), tuple(profile.get("crop", [])))
        if generation != self.latest.get(slot) or signature != current: return
        if envelope.get("generation") != generation or envelope.get("privacy_epoch") != epoch or epoch != int(getattr(self.bridge, "privacy_epoch", 0)) or not envelope.get("ok"): return
        pixmap = QPixmap(); png = envelope.get("image", {}).get("png")
        if isinstance(png, bytes) and pixmap.loadFromData(png, "PNG"):
            rendered = QPixmap(256, 256); rendered.fill(Qt.GlobalColor.transparent)
            painter = QPainter(rendered); painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            crop = profile.get("crop", [0, 0, 1, 1]); x, y, width, height = crop
            source = QRectF(x * pixmap.width(), y * pixmap.height(), width * pixmap.width(), height * pixmap.height())
            target = QRectF(0, 0, 256, 256); mode = profile.get("display_mode", "contain")
            if mode == "circle":
                path = QPainterPath(); path.addEllipse(target); painter.setClipPath(path)
            if mode == "contain":
                ratio = min(256 / source.width(), 256 / source.height()); w, h = source.width() * ratio, source.height() * ratio
                target = QRectF((256 - w) / 2, (256 - h) / 2, w, h)
            painter.drawPixmap(target, pixmap, source); painter.end()
            self.images[slot] = rendered; self.image_changed.emit(slot, rendered)
    def clear(self, epoch: int = 0) -> None:
        self.channel.invalidate()
        self.profiles.clear(); self.images.clear(); self.pending.clear(); self.latest.clear(); self._reading = False; self.changed.emit()
        for slot in ["character_avatar", "user_avatar"]: self.image_changed.emit(slot, QPixmap())
