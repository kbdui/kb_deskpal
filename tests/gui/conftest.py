"""GUI checks are optional; headless backend users do not need Qt installed."""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")
if os.name == 'nt':
    font_dir = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
    if font_dir.is_dir(): os.environ.setdefault('QT_QPA_FONTDIR', str(font_dir))

import pytest

pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="session")
def qapp(qapp_args, qapp_cls, pytestconfig):
    """Destroy Qt before Python tears down WebEngine's module globals."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    from shiboken6 import delete, isValid

    existing = QApplication.instance()
    if existing is not None:
        yield existing
        return
    app = qapp_cls(qapp_args)
    app.setApplicationName(pytestconfig.getini("qt_qapp_name"))
    try:
        yield app
    finally:
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        for widget in app.topLevelWidgets():
            if isValid(widget):
                delete(widget)
        delete(app)
