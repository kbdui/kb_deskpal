"""Fluent 主题仅定制业务表面；不覆盖组件库的通用按钮和输入样式。"""
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import Theme, isDarkTheme, setTheme, setThemeColor

LIGHT = """
QWidget#deskpalWorkspace { background:#f6f6f1; }
QFrame#tool_results_sidebar { background:#fffefa; border-left:1px solid #dfe5da; }
QFrame#message { background:transparent; border:none; }
QFrame#user_message { background:#e5eee2; border-radius:14px; }
QLabel#heading { font-size:24px; font-weight:600; color:#273b31; }
QLabel#subtle { color:#647166; }
QLabel#chat_avatar { background:#e5eee2; border-radius:21px; color:#52735d; font-size:18px; font-weight:600; }
QLabel#task_card_title { font-size:16px; font-weight:600; }
QFrame#tool_result_card, QFrame#tool_result_summary { background:#fffefa; border:1px solid #dfe5da; border-radius:12px; }
QFrame#tool_result_parameters { background:#f3f5ee; border-radius:8px; }
QLabel#tool_result_card_title, QLabel#tool_result_summary_title { font-size:16px; font-weight:600; }
QLabel#tool_result_status { color:#52735d; }
QLabel#tool_result_status[state="failure"] { color:#ab4943; }
"""
DARK = """
QWidget#deskpalWorkspace { background:#181d1a; }
QFrame#tool_results_sidebar { background:#222925; border-left:1px solid #3a473d; }
QFrame#message { background:transparent; border:none; }
QFrame#user_message { background:#304536; border-radius:14px; }
QLabel#heading { font-size:24px; font-weight:600; color:#e4ebe4; }
QLabel#subtle { color:#adbaae; }
QLabel#chat_avatar { background:#304536; border-radius:21px; color:#a6c4aa; font-size:18px; font-weight:600; }
QLabel#task_card_title { font-size:16px; font-weight:600; }
QFrame#tool_result_card, QFrame#tool_result_summary { background:#222925; border:1px solid #3a473d; border-radius:12px; }
QFrame#tool_result_parameters { background:#29352d; border-radius:8px; }
QLabel#tool_result_card_title, QLabel#tool_result_summary_title { font-size:16px; font-weight:600; }
QLabel#tool_result_status { color:#a6c4aa; }
QLabel#tool_result_status[state="failure"] { color:#efaaa2; }
"""

def configure() -> None:
    app = QApplication.instance()
    if not isinstance(app, QApplication) or app.property('deskpalFluentConfigured'):
        return
    app.setProperty('deskpalFluentConfigured', True)
    app.setFont(QFont('Microsoft YaHei UI', 10))
    # 不在已构造 Fluent 控件后替换 QApplication 的原生 QStyle。
    # 官方 lazy 刷新让隐藏、退役中的控件留到下次绘制再更新。
    setTheme(Theme.AUTO, save=False, lazy=True)
    setThemeColor('#52735d', save=False, lazy=True)


def select(value: str) -> None:
    configure()
    setTheme({'light': Theme.LIGHT, 'dark': Theme.DARK}.get(value, Theme.AUTO), save=False, lazy=True)


def _palette() -> None:
    """原生对话框/菜单等必要 Qt 表面也应跟随主题，防止暗色白字落在白底。"""
    app = QApplication.instance()
    if not isinstance(app, QApplication): return
    dark = isDarkTheme()
    if app.property('deskpalPaletteIsDark') == dark: return
    app.setProperty('deskpalPaletteIsDark', dark)
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, '#222925' if dark else '#f6f6f1'),
        (QPalette.ColorRole.WindowText, '#e4ebe4' if dark else '#273b31'),
        (QPalette.ColorRole.Base, '#181d1a' if dark else '#fffefa'),
        (QPalette.ColorRole.Text, '#e4ebe4' if dark else '#273b31'),
        (QPalette.ColorRole.Button, '#304536' if dark else '#e5eee2'),
        (QPalette.ColorRole.ButtonText, '#e4ebe4' if dark else '#273b31'),
        (QPalette.ColorRole.Highlight, '#52735d'),
        (QPalette.ColorRole.HighlightedText, '#ffffff')):
        palette.setColor(role, QColor(color))
    app.setPalette(palette)


def apply(widget: QWidget) -> None:
    configure()
    _palette()
    widget.setStyleSheet(DARK if isDarkTheme() else LIGHT)
    from .components import PushButton, primary
    for button in widget.findChildren(PushButton):
        if button.objectName() == 'primary':
            primary(button)
