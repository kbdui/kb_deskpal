"""GUI-thread-only static pet canvas and native bounded input region."""

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import (QBitmap, QCloseEvent, QMouseEvent, QPainter,
                          QPaintEvent, QPixmap, QRegion, QScreen)
from PySide6.QtWidgets import QApplication, QWidget

CANVAS = 240
LABEL = QRect(8, CANVAS, CANVAS - 16, 36)


def clamp_position(position: QPoint, size: tuple[int, int], areas: list[QRect]) -> QPoint:
    """Logical coordinates, including negative monitor positions."""
    if not areas:
        return position
    center = position + QPoint(size[0] // 2, size[1] // 2)
    area = min(areas, key=lambda a: (a.center().x() - center.x()) ** 2
               + (a.center().y() - center.y()) ** 2)
    return QPoint(max(area.left(), min(position.x(), area.right() - size[0] + 1)),
                  max(area.top(), min(position.y(), area.bottom() - size[1] + 1)))


class PetWindow(QWidget):
    navigate_requested = Signal()
    disable_requested = Signal()
    position_changed = Signal(object, str)
    render_failed = Signal(str)

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(CANVAS, CANVAS + 44)
        self.setWindowTitle("Deskpal 桌宠")
        self._pixmap: QPixmap | None = None
        self._status = "状态未知"
        self._diagnostic = ""
        self._press: QPoint | None = None
        self._origin = QPoint()
        self._dragged = False
        self._paint_failed = False
        self._mask_region = QRegion(QRect(60, 52, 120, 150), QRegion.RegionType.Ellipse)
        self._mask_region |= QRegion(LABEL)
        self.setMask(self._mask_region)
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.screenAdded.connect(self._screen_added)
            app.screenRemoved.connect(self.fit_to_screens)
            for screen in app.screens():
                self._screen_added(screen)

    def _screen_added(self, screen: QScreen) -> None:
        # QScreen ownership remains with Qt; connections die with this receiver.
        screen.availableGeometryChanged.connect(self.fit_to_screens)
        screen.logicalDotsPerInchChanged.connect(self.fit_to_screens)
        self.fit_to_screens()

    def fit_to_screens(self, *_: object) -> None:
        self.move(clamp_position(self.pos(), (self.width(), self.height()),
                                 [screen.availableGeometry() for screen in QApplication.screens()]))

    def set_native_options(self, *, on_top: bool, click_through: bool) -> None:
        visible = self.isVisible()
        position = self.pos()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on_top)
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, click_through)
        self.move(position)
        if visible:
            self.show()

    def set_stable_mask(self, neutral: QPixmap | None) -> None:
        # Native window region, rather than ignored mouse events: transparent
        # pixels really reach underlying windows. Every expression keeps this mask.
        if neutral is None:
            self._mask_region = (QRegion(QRect(60, 52, 120, 150), QRegion.RegionType.Ellipse)
                                 | QRegion(LABEL))
            self.setMask(self._mask_region)
            return
        logical = neutral.scaled(CANVAS, CANVAS, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        logical.setDevicePixelRatio(1)
        mask: QBitmap = logical.mask()
        region = QRegion(mask)
        region.translate((CANVAS - logical.width()) // 2, (CANVAS - logical.height()) // 2)
        self._mask_region = region | QRegion(LABEL)
        self.setMask(self._mask_region)

    def present(self, pixmap: QPixmap | None, status: str, diagnostic: str = "") -> None:
        if pixmap is self._pixmap and status == self._status and diagnostic == self._diagnostic:
            return
        self._pixmap = pixmap
        self._status = status
        self._diagnostic = diagnostic
        self.setToolTip(status + ("\n" + diagnostic if diagnostic else ""))
        if self.isVisible():
            self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            if self._pixmap is not None:
                size = self._pixmap.deviceIndependentSize()
                painter.drawPixmap(QPoint(round((CANVAS - size.width()) / 2),
                                          round((CANVAS - size.height()) / 2)), self._pixmap)
            else:
                # Explicit project-owned degraded mark, never a final art asset.
                painter.setPen(Qt.GlobalColor.darkCyan)
                painter.setBrush(Qt.GlobalColor.lightGray)
                painter.drawEllipse(65, 65, 110, 120)
                painter.drawText(QRect(65, 65, 110, 120), Qt.AlignmentFlag.AlignCenter, "◈")
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(Qt.GlobalColor.white)
            painter.drawRoundedRect(LABEL, 8, 8)
            painter.setPen(Qt.GlobalColor.black)
            painter.drawText(LABEL, Qt.AlignmentFlag.AlignCenter, self._status)
        except Exception:
            self._pixmap = None
            self._diagnostic = "paint:failed"
            if not self._paint_failed:
                self._paint_failed = True
                self.render_failed.emit("paint:failed")
        finally:
            painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.globalPosition().toPoint()
            self._origin = self.pos()
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
            dragged = self._dragged
            self._press = None
            if dragged:
                self.fit_to_screens()
                screen = self.screen()
                self.position_changed.emit(self.pos(), screen.name() if screen else "")
            else:
                self.navigate_requested.emit()

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.disable_requested.emit()
