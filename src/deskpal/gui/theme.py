"""清透工作室主题：灰白表面、紫色强调，保留 Fluent 控件的交互状态。"""
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import Theme, isDarkTheme, setTheme, setThemeColor

LIGHT = """
QWidget#chat, QWidget#arrangements, QWidget#memory, QWidget#tools, QWidget#settings { background:#ffffff; border-radius:16px; }
QFrame#studio_setting_card { background:#ffffff; border:1px solid #e9ebf2; border-radius:14px; }
QFrame#studio_divider { background:#e9ebf2; }
QWidget#studio_detail { background:#f5f6fa; border:1px solid #e9ebf2; border-radius:14px; }
QFrame#studio_composer { background:#ffffff; border:1px solid #e9ebf2; border-radius:15px; }
QPlainTextEdit#chat_input { background:transparent; border:none; padding:4px; }
QLabel#message_author { color:#242835; font-size:12px; }
QLabel#message_body { color:#242835; font-size:14px; padding:5px 0; }
QLabel#section_caption { color:#242835; font-size:18px; font-weight:600; }
QListWidget#settings_navigation { background:transparent; border:none; }
QListWidget#settings_navigation::item { padding:12px 8px; border-radius:9px; }
QListWidget#settings_navigation::item:selected { background:#f0effa; color:#242835; }
QListWidget[studioList="true"], QTreeWidget[studioList="true"] { background:#ffffff; border:1px solid #e9ebf2; border-radius:12px; padding:6px; }
QListWidget[studioList="true"]::item, QTreeWidget[studioList="true"]::item { padding:10px 8px; margin:2px; border-radius:8px; }
QPlainTextEdit[studioReading="true"] { background:transparent; border:none; padding:2px; }
QSplitter::handle { background:transparent; }

QWidget#deskpalWorkspace { background:#f5f6fa; }
QFrame#tool_results_sidebar { background:#ffffff; border-left:1px solid #e9ebf2; }
QFrame#message { background:transparent; border:none; }
QFrame#user_message { background:#f0effa; border-radius:14px; }
QLabel#heading { font-size:24px; font-weight:600; color:#242835; }
QLabel#subtle { color:#7c8191; }
QLabel#chat_avatar { background:#f0effa; border-radius:21px; color:#6261d8; font-size:18px; font-weight:600; }
QLabel#task_card_title { font-size:16px; font-weight:600; }
QFrame#tool_result_card, QFrame#tool_result_summary { background:#ffffff; border:1px solid #e9ebf2; border-radius:12px; }
QFrame#tool_result_parameters { background:#f5f5fc; border-radius:8px; }
QGroupBox { border:1px solid #e9ebf2; border-radius:10px; margin-top:12px; padding:12px 10px 8px 10px; color:#474165; font-weight:600; }
QLabel#tool_result_card_title, QLabel#tool_result_summary_title { font-size:16px; font-weight:600; }
QLabel#tool_result_status { color:#6261d8; }
QLabel#tool_result_status[state="failure"] { color:#ab4943; }
"""
DARK = """
QWidget#chat, QWidget#arrangements, QWidget#memory, QWidget#tools, QWidget#settings { background:#222532; border-radius:16px; }
QFrame#studio_setting_card { background:#222532; border:1px solid #393d51; border-radius:14px; }
QFrame#studio_divider { background:#393d51; }
QWidget#studio_detail { background:#191b24; border:1px solid #393d51; border-radius:14px; }
QFrame#studio_composer { background:#222532; border:1px solid #393d51; border-radius:15px; }
QPlainTextEdit#chat_input { background:transparent; border:none; padding:4px; }
QLabel#message_author { color:#e9eaf4; font-size:12px; }
QLabel#message_body { color:#e9eaf4; font-size:14px; padding:5px 0; }
QLabel#section_caption { color:#e9eaf4; font-size:18px; font-weight:600; }
QListWidget#settings_navigation { background:transparent; border:none; }
QListWidget#settings_navigation::item { padding:12px 8px; border-radius:9px; }
QListWidget#settings_navigation::item:selected { background:#34314d; color:#e9eaf4; }
QListWidget[studioList="true"], QTreeWidget[studioList="true"] { background:#222532; border:1px solid #393d51; border-radius:12px; padding:6px; }
QListWidget[studioList="true"]::item, QTreeWidget[studioList="true"]::item { padding:10px 8px; margin:2px; border-radius:8px; }
QPlainTextEdit[studioReading="true"] { background:transparent; border:none; padding:2px; }
QSplitter::handle { background:transparent; }

QWidget#deskpalWorkspace { background:#191b24; }
QFrame#tool_results_sidebar { background:#222532; border-left:1px solid #393d51; }
QFrame#message { background:transparent; border:none; }
QFrame#user_message { background:#34314d; border-radius:14px; }
QLabel#heading { font-size:24px; font-weight:600; color:#e9eaf4; }
QLabel#subtle { color:#a9aec1; }
QLabel#chat_avatar { background:#34314d; border-radius:21px; color:#b5b0ff; font-size:18px; font-weight:600; }
QLabel#task_card_title { font-size:16px; font-weight:600; }
QFrame#tool_result_card, QFrame#tool_result_summary { background:#222532; border:1px solid #393d51; border-radius:12px; }
QFrame#tool_result_parameters { background:#2b2b41; border-radius:8px; }
QGroupBox { border:1px solid #393d51; border-radius:10px; margin-top:12px; padding:12px 10px 8px 10px; color:#c7c9df; font-weight:600; }
QLabel#tool_result_card_title, QLabel#tool_result_summary_title { font-size:16px; font-weight:600; }
QLabel#tool_result_status { color:#b5b0ff; }
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
    setThemeColor('#6261d8', save=False, lazy=True)


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
        (QPalette.ColorRole.Window, '#222532' if dark else '#f5f6fa'),
        (QPalette.ColorRole.WindowText, '#e9eaf4' if dark else '#242835'),
        (QPalette.ColorRole.Base, '#191b24' if dark else '#ffffff'),
        (QPalette.ColorRole.Text, '#e9eaf4' if dark else '#242835'),
        (QPalette.ColorRole.Button, '#34314d' if dark else '#f0effa'),
        (QPalette.ColorRole.ButtonText, '#e9eaf4' if dark else '#242835'),
        (QPalette.ColorRole.Highlight, '#6261d8'),
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
