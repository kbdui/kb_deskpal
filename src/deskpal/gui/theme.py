"""Shared warm paper and sage visual language for native desktop surfaces."""

STYLE = """
QWidget { font-family: 'Microsoft YaHei UI', 'Noto Sans CJK SC', sans-serif; font-size: 14px; color: #2f4037; }
QMainWindow, QWidget#shell { background: #f7f6f0; }
QFrame#navigation { background: #e9eee5; border-radius: 16px; }
QFrame#conversation_sidebar, QGroupBox { background: #fcfcf8; border: 1px solid #dce3d8; border-radius: 12px; }
QGroupBox { margin-top: 16px; padding: 16px 10px 10px; }
QGroupBox::title { subcontrol-origin: margin; padding: 2px 8px; }
QPushButton { padding: 8px 12px; border: 1px solid #d1ddce; background: #edf2e8; border-radius: 9px; }
QPushButton:hover { background: #dce8d8; }
QPushButton:checked, QPushButton#primary { color: #fcfff9; background: #597a60; border-color: #597a60; }
QPushButton:disabled { color: #919b91; background: #f0f2eb; border-color: #e3e7df; }
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: #fffefa; border: 1px solid #d6dfd0; border-radius: 8px; padding: 7px; selection-background-color: #d7e5cd; }
QListWidget, QTreeWidget { background: #fcfcf8; border: 1px solid #e0e5db; border-radius: 10px; padding: 5px; }
QListWidget::item { padding: 10px 7px; border-radius: 7px; }
QListWidget::item:selected { background: #e0ead8; color: #2c4733; }
QLabel#heading { font-size: 22px; font-weight: 600; }
QLabel#subtle { color: #6d7d6f; }
QFrame#message { background: #fffefa; border: 1px solid #e0e6d9; border-radius: 14px; }
QFrame#user_message { background: #e7eedf; border-radius: 14px; }
QTabWidget::pane { border: none; }
QTabBar::tab { padding: 9px 12px; background: #eef1e9; border-radius: 7px; margin: 2px; }
QTabBar::tab:selected { background: #dce8d4; }
QScrollArea { border: none; background: transparent; }
QToolTip { background: #fffefa; border: 1px solid #cddac8; padding: 6px; }
"""
