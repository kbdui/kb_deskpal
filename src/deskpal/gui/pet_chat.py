"""Focusable pet-side shell; all chat, drafts and approvals belong to ChatCoordinator."""

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QKeySequence, QScreen, QShortcut
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QVBoxLayout, QWidget)
from .components import (PushButton as QPushButton)

from .chat import ChatCoordinator, ChatView
from .pet_window import clamp_position
from .theme import apply as apply_theme


class PetChatWindow(QWidget):
    expand_requested = Signal(str, str)

    def __init__(self, coordinator: ChatCoordinator, parent: QWidget | None = None) -> None:
        # A separate native focusable window; never inherits the canvas's no-focus flags.
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.WindowCloseButtonHint)
        self.setWindowTitle("DeskPal · 聊天")
        self.setObjectName('deskpalWorkspace')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.resize(410, 520)
        self._disposing = False
        self._deactivate_timer = QTimer(self)
        self._deactivate_timer.setSingleShot(True)
        self._deactivate_timer.timeout.connect(self._hide_if_inactive)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addStretch()
        close = QPushButton("收起")
        close.clicked.connect(self.hide)
        row.addWidget(close)
        layout.addLayout(row)
        self.chat_view = ChatView(coordinator, compact=True, parent=self)
        self.chat_view.expand_requested.connect(self.expand_requested)
        layout.addWidget(self.chat_view, 1)
        apply_theme(self)
        from qfluentwidgets import qconfig
        qconfig.themeChanged.connect(self._apply_theme)
        self._escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._escape.activated.connect(self.hide)
        self._anchor: QWidget | None = None
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.installEventFilter(self)
            app.screenRemoved.connect(self.reanchor)
            app.screenAdded.connect(self._screen_added)
            for screen in app.screens():
                self._screen_added(screen)

    def _apply_theme(self) -> None:
        apply_theme(self)

    def _screen_added(self, screen: QScreen) -> None:
        screen.availableGeometryChanged.connect(self.reanchor)

    def open_for(self, anchor: QWidget) -> None:
        if self._disposing:
            return
        self._anchor = anchor
        self.chat_view.show_default()
        self.reanchor()
        self.show()
        self.raise_()
        self.activateWindow()
        self.chat_view.focus_input()

    def reanchor(self, *_: object) -> None:
        anchor = self._anchor
        if anchor is None or self._disposing:
            return
        screens = QApplication.screens()
        screen = anchor.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.setMinimumSize(0, 0)
        self.resize(min(410, area.width()), min(520, area.height()))
        right = anchor.geometry().right() + 12
        left = anchor.geometry().left() - self.width() - 12
        x = right if right + self.width() <= area.right() + 1 else left
        proposed = QPoint(x, anchor.y() + (anchor.height() - self.height()) // 2)
        self.move(clamp_position(proposed, (self.width(), self.height()),
                                 [screen.availableGeometry() for screen in screens]))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self and event.type() == QEvent.Type.WindowDeactivate and self.isVisible():
            # Defer until the canvas click/menu action has completed its toggle.
            self._deactivate_timer.start(0)
        return super().eventFilter(watched, event)

    def _hide_if_inactive(self) -> None:
        if not self.isActiveWindow() and not self._disposing:
            self.hide()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._disposing:
            event.accept()
        else:
            event.ignore()
            self.hide()

    def dispose(self) -> None:
        if self._disposing:
            return
        self._disposing = True
        self._deactivate_timer.stop()
        self.hide()
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.removeEventFilter(self)
        self._anchor = None
        self.deleteLater()
