"""Isolated native Qt wheel regression; no production configuration or backend."""

import faulthandler
import json

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QApplication, QComboBox, QListWidget, QPlainTextEdit, QScrollArea, QSlider,
    QSpinBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from deskpal.gui.widgets import install_wheel_guard


def wheel(widget, delta=-120):
    point = widget.rect().center()
    event = QWheelEvent(QPointF(point), QPointF(widget.mapToGlobal(point)), QPoint(),
                        QPoint(0, delta), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(widget, event)


def main():
    faulthandler.enable()
    app = QApplication([])
    install_wheel_guard()
    views = [QListWidget(), QTreeWidget(), QPlainTextEdit()]
    views[0].addItems([f"record {i}" for i in range(100)])
    views[1].setHeaderLabels(["record"])
    views[1].addTopLevelItems([QTreeWidgetItem([f"record {i}"]) for i in range(100)])
    views[2].setPlainText("\n".join(f"line {i}" for i in range(100)))
    area = QScrollArea()
    host = QWidget(); layout = QVBoxLayout(host)
    combo = QComboBox(); combo.addItems([f"choice {i}" for i in range(100)])
    spin = QSpinBox(); spin.setValue(30)
    slider = QSlider(Qt.Orientation.Horizontal); slider.setValue(30)
    for control in (combo, spin, slider): layout.addWidget(control)
    host.setMinimumHeight(1500); area.setWidget(host); area.setWidgetResizable(True)
    views.append(area)
    results = {}
    for view in views:
        view.resize(320, 240); view.show(); app.processEvents()
        bar = view.verticalScrollBar()
        assert bar.maximum() > 0
        wheel(view.viewport()); app.processEvents()
        assert bar.value() > 0, type(view).__name__
        results[type(view).__name__] = bar.value()
        # Native scrollbars and viewport dispatch must both terminate, even at
        # an edge where Qt may ignore and propagate the wheel to a parent.
        for value, delta in ((0, 120), (bar.maximum(), -120)):
            bar.setValue(value)
            wheel(view.viewport(), delta); wheel(bar, delta); app.processEvents()
    for control in (combo, spin, slider):
        area.verticalScrollBar().setValue(0)
        before = combo.currentIndex() if control is combo else control.value()
        wheel(control); app.processEvents()
        after = combo.currentIndex() if control is combo else control.value()
        assert before == after, type(control).__name__
        assert area.verticalScrollBar().value() > 0, type(control).__name__
    combo.showPopup(); app.processEvents()
    popup = combo.view()
    before = combo.currentIndex()
    popup.verticalScrollBar().setValue(0)
    wheel(popup.viewport()); app.processEvents()
    assert popup.verticalScrollBar().value() > 0
    assert combo.currentIndex() == before
    combo.hidePopup()
    for view in views: view.close()
    app.processEvents()
    print(json.dumps({"wheel_passed": results, "controls_unchanged": True,
                      "popup_scrolls_without_selection": True}), flush=True)


if __name__ == "__main__": main()
