"""透明边缘、差分轮廓和宽图/高 DPI 的完整显示回归。"""
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QRegion
from PySide6.QtWidgets import QWidget

from deskpal.gui.pet_window import PetWindow


def body():
    image = QImage(240, 240, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.fillRect(80, 60, 80, 140, QColor('#26724a'))
    painter.end()
    return image


def test_low_alpha_edges_are_not_cut_by_binary_window_mask(qtbot):
    window = PetWindow()
    qtbot.addWidget(window)
    image = body()
    image.setPixelColor(25, 25, QColor(220, 40, 80, 32))
    pixmap = QPixmap.fromImage(image)
    window.set_stable_mask(pixmap)
    window.present(pixmap, '普通')
    assert window.mask().contains(QPoint(25, 25))
    assert not window.mask().contains(QPoint(2, 2))


def test_expression_outside_main_silhouette_is_visible_without_shrinking_mask(qtbot):
    window = PetWindow()
    qtbot.addWidget(window)
    neutral = QPixmap.fromImage(body())
    window.set_stable_mask(neutral)
    image = body()
    painter = QPainter(image)
    painter.fillRect(4, 100, 232, 8, QColor('#287bea'))
    painter.end()
    window.present(QPixmap.fromImage(image), '思考')
    assert window.mask().contains(QPoint(4, 104))
    assert window.mask().contains(QPoint(235, 104))
    assert not window.mask().contains(QPoint(2, 2))
    region = window.mask()
    window.set_stable_mask(neutral)
    window.present(neutral, '普通')
    assert window.mask() == region


@pytest.mark.parametrize('dpr', [1., 1.25, 2.])
@pytest.mark.parametrize('portrait', [False, True])
@pytest.mark.parametrize('scale', [1., 1.07])
def test_opaque_frame_is_fitted_with_both_edges_visible(qtbot, dpr, portrait, scale):
    window = PetWindow()
    qtbot.addWidget(window)
    window.set_scale(scale)
    width, height = (240, 480) if portrait else (480, 240)
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor('#26724a'))
    painter = QPainter(image)
    painter.fillRect(0, 0, width if portrait else 16, 16 if portrait else height, QColor('#ff0000'))
    painter.fillRect(0 if portrait else width - 16, height - 16 if portrait else 0,
                     width if portrait else 16, 16 if portrait else height, QColor('#0000ff'))
    painter.end()
    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(dpr)
    window.set_stable_mask(pixmap)
    window.present(pixmap, '普通')
    edge = window.canvas_edge
    output = QImage(edge, edge, QImage.Format.Format_ARGB32)
    output.fill(Qt.GlobalColor.transparent)
    # Qt 默认 render 会将遮罩包围盒挪到输出原点；单独检查遮罩，绘制保持窗口坐标。
    window.render(output, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren | QWidget.RenderFlag.IgnoreMask)
    start = QPoint(edge // 2, 1) if portrait else QPoint(1, edge // 2)
    end = QPoint(edge // 2, edge - 2) if portrait else QPoint(edge - 2, edge // 2)
    assert output.pixelColor(start).red() > 240
    assert output.pixelColor(end).blue() > 240
    assert window.mask().contains(start)
    assert not window.mask().contains(QPoint(1, 1))
