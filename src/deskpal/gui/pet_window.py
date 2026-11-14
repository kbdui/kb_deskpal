"""GUI-thread pet canvas; fitted frames and a non-clipping stable visible region."""

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import (QCloseEvent, QContextMenuEvent, QMouseEvent, QMoveEvent,
                          QPainter, QPaintEvent, QPixmap, QRegion, QScreen, QShowEvent, QWheelEvent)
from PySide6.QtWidgets import QApplication, QWidget

CANVAS = 240


def clamp_position(position: QPoint, size: tuple[int, int], areas: list[QRect]) -> QPoint:
    """Keep logical coordinates on an available work area, including negative origins."""
    if not areas:
        return position
    center = position + QPoint(size[0] // 2, size[1] // 2)
    area = min(areas, key=lambda a: (a.center().x() - center.x()) ** 2
               + (a.center().y() - center.y()) ** 2)
    return QPoint(max(area.left(), min(position.x(), area.right() - size[0] + 1)),
                  max(area.top(), min(position.y(), area.bottom() - size[1] + 1)))


class PetWindow(QWidget):
    navigate_requested = Signal()
    disable_requested = Signal()  # Compatibility: native close now means temporary hide.
    context_requested = Signal(object)
    position_changed = Signal(object, str)
    anchor_changed = Signal()
    display_metrics_changed = Signal()
    render_failed = Signal(str)

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("DeskPal 桌宠")
        self._canvas = CANVAS
        self._scale = 1.0
        self._dpr = self.devicePixelRatioF()
        self._screen_connected = False
        self._neutral: QPixmap | None = None
        self._mask_base_key: tuple[int, int] | None = None
        self._frame_mask_key: tuple[int, int] | None = None
        self._pixmap: QPixmap | None = None
        self._status = "状态未知"
        self._diagnostic = ""
        self._press: QPoint | None = None
        self._origin = QPoint()
        self._dragged = False
        self._paint_failed = False
        self._metrics_timer = QTimer(self)
        self._metrics_timer.setSingleShot(True)
        self._metrics_timer.timeout.connect(self._metrics_changed)
        self.setFixedSize(CANVAS, CANVAS)
        self.set_stable_mask(None)
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.screenAdded.connect(self._screen_added)
            app.screenRemoved.connect(self.fit_to_screens)
            for screen in app.screens():
                self._screen_added(screen)

    @property
    def canvas_edge(self) -> int:
        return self._canvas

    def set_scale(self, scale: float) -> None:
        self._scale = min(1.6, max(.6, scale))
        self._canvas = round(CANVAS * self._scale)
        # Keep the native window bounded to the visible pet canvas.
        self.setFixedSize(self._canvas, self._canvas)
        self.set_stable_mask(self._neutral)
        self._include_frame(self._pixmap)
        self.fit_to_screens()
        self.display_metrics_changed.emit()
        self.update()

    def _screen_added(self, screen: QScreen) -> None:
        screen.availableGeometryChanged.connect(self.fit_to_screens)
        screen.logicalDotsPerInchChanged.connect(self._metrics_changed)
        self.fit_to_screens()

    def _metrics_changed(self, *_: object) -> None:
        self.fit_to_screens()
        self._dpr = self.devicePixelRatioF()
        self.display_metrics_changed.emit()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None and not self._screen_connected:
            handle.screenChanged.connect(self._metrics_changed)
            self._screen_connected = True
        self._metrics_timer.start(0)

    def event(self, event: QEvent) -> bool:
        result = super().event(event)
        if (event.type() == getattr(QEvent.Type, "DevicePixelRatioChange", None)
                and hasattr(self, "_metrics_timer")):
            self._metrics_timer.start(0)
        return result

    def fit_to_screens(self, *_: object) -> None:
        self.move(clamp_position(self.pos(), (self.width(), self.height()),
                                 [screen.availableGeometry() for screen in QApplication.screens()]))
        self.anchor_changed.emit()

    def moveEvent(self, event: QMoveEvent) -> None:
        super().moveEvent(event)
        self.anchor_changed.emit()
        dpr = self.devicePixelRatioF()
        if dpr != self._dpr:
            self._dpr = dpr
            self.display_metrics_changed.emit()

    def set_native_options(self, *, on_top: bool, click_through: bool) -> None:
        visible, position = self.isVisible(), self.pos()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on_top)
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, click_through)
        self.move(position)
        if visible:
            self.show()

    def _fallback_rect(self) -> QRect:
        return QRect(round(65 * self._scale), round(65 * self._scale),
                     round(110 * self._scale), round(120 * self._scale))

    def set_stable_mask(self, neutral: QPixmap | None) -> None:
        self._neutral = neutral
        key = (neutral.cacheKey() if neutral is not None else 0, self._canvas)
        if key == self._mask_base_key:
            return
        self._mask_base_key = key
        self._frame_mask_key = None
        if neutral is None or neutral.isNull():
            region = QRegion(self._fallback_rect(), QRegion.RegionType.Ellipse)
        else:
            region = self._visible_region(neutral)
        self._mask_region = region
        self.setMask(self._mask_region)

    def _visible_region(self, pixmap: QPixmap) -> QRegion:
        logical = pixmap.scaled(self._canvas, self._canvas, Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
        logical.setDevicePixelRatio(1)
        mask = logical.mask()
        # JPEG 等不带透明通道的图片没有位图遮罩，但整张图仍须可见。
        region = QRegion(logical.rect()) if mask.isNull() else QRegion(mask)
        region.translate(round((self._canvas - logical.width()) / 2),
                         round((self._canvas - logical.height()) / 2))
        return region

    def _include_frame(self, pixmap: QPixmap | None) -> None:
        if pixmap is None or pixmap.isNull():
            return
        key = (pixmap.cacheKey(), self._canvas)
        if key == self._frame_mask_key:
            return
        self._frame_mask_key = key
        # 保留主轮廓和本组已显示轮廓，避免表情外缘被主图切掉或命中区反复缩小。
        region = self._mask_region.united(self._visible_region(pixmap))
        if region != self._mask_region:
            self._mask_region = region
            self.setMask(region)

    def present(self, pixmap: QPixmap | None, status: str, diagnostic: str = "") -> None:
        self._include_frame(pixmap)
        if pixmap is self._pixmap and status == self._status and diagnostic == self._diagnostic:
            return
        self._pixmap, self._status, self._diagnostic = pixmap, status, diagnostic
        self.setToolTip(status + ("\n" + diagnostic if diagnostic else ""))
        if self.isVisible():
            self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            if self._pixmap is not None and not self._pixmap.isNull():
                size = self._pixmap.size().scaled(self._canvas, self._canvas, Qt.AspectRatioMode.KeepAspectRatio)
                target = QRectF(round((self._canvas - size.width()) / 2),
                                round((self._canvas - size.height()) / 2), size.width(), size.height())
                painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
            else:
                painter.setPen(Qt.GlobalColor.darkCyan)
                painter.setBrush(Qt.GlobalColor.lightGray)
                painter.drawEllipse(self._fallback_rect())
                painter.drawText(self._fallback_rect(), Qt.AlignmentFlag.AlignCenter, "◈")
        except Exception:
            self._pixmap = None
            if not self._paint_failed:
                self._paint_failed = True
                self.render_failed.emit("paint:failed")
        finally:
            painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press, self._origin = event.globalPosition().toPoint(), self.pos()
            self._dragged = False
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press is not None:
            delta = event.globalPosition().toPoint() - self._press
            if delta.manhattanLength() >= QApplication.startDragDistance():
                self._dragged = True
            if self._dragged:
                self.move(self._origin + delta)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._press is not None:
            dragged, self._press = self._dragged, None
            if dragged:
                self.fit_to_screens()
                screen = self.screen()
                self.position_changed.emit(self.pos(), screen.name() if screen else "")
            else:
                self.navigate_requested.emit()

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        self._press = None
        self.context_requested.emit(event.globalPos())
        event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.disable_requested.emit()
