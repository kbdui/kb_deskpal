"""GUI checks are optional; headless backend users do not need Qt installed."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")

import pytest

pytest.importorskip("PySide6.QtWidgets")
