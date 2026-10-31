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
