"""T04/T05 logical checks. Native transparent input still needs Windows evidence."""

from PySide6.QtCore import QPoint, QRect, QSettings, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtTest import QTest

from deskpal.gui.pet_window import PetWindow, clamp_position
from deskpal.gui.preferences import GuiPreferences


def test_t05_negative_monitors_missing_screen_and_bad_preferences(tmp_path):
    areas = [QRect(-1920, 0, 1920, 1080), QRect(0, 0, 1920, 1040)]
    assert clamp_position(QPoint(-1800, 30), (240, 284), areas) == QPoint(-1800, 30)
    assert clamp_position(QPoint(-4000, 3000), (240, 284), areas) == QPoint(-1920, 796)
    assert clamp_position(QPoint(-1800, 30), (240, 284), areas[1:]) == QPoint(0, 30)
    settings = QSettings(str(tmp_path / "pet.ini"), QSettings.Format.IniFormat)
    settings.setValue("pet/x", "bad")
    settings.setValue("pet/y", 10 ** 15)
    settings.setValue("pet/enabled", "unexpected")
    prefs = GuiPreferences(settings)
    assert prefs.pet_position == (80, 80) and not prefs.pet_enabled
    assert prefs.set_pet_position((-1200, 60), "left")
    assert prefs.pet_position == (-1200, 60)


def test_t04_stable_region_and_drag_does_not_navigate(qtbot):
    window = PetWindow()
    qtbot.addWidget(window)
    image = QImage(240, 240, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    for y in range(70, 170):
        for x in range(70, 170):
            image.setPixelColor(x, y, Qt.GlobalColor.darkCyan)
    neutral = QPixmap.fromImage(image)
    window.set_stable_mask(neutral)
    region = window.mask()
    assert not region.contains(QPoint(2, 2))
    assert region.contains(QPoint(100, 100))
    window.present(neutral, "等待授权")
    assert window.mask() == region
    window.show()
    clicks = []
    window.navigate_requested.connect(lambda: clicks.append(True))
    qtbot.mouseClick(window, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
    assert len(clicks) == 1
    QTest.mousePress(window, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
    QTest.mouseMove(window, QPoint(140, 130))
    QTest.mouseRelease(window, Qt.MouseButton.LeftButton, pos=QPoint(140, 130))
    assert len(clicks) == 1
    position = window.pos()
    window.set_native_options(on_top=False, click_through=True)
    assert window.isVisible() and window.pos() == position
    assert window.windowFlags() & Qt.WindowType.WindowTransparentForInput
    window.set_native_options(on_top=True, click_through=False)
    assert window.mask() == region
