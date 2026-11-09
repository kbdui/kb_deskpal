"""统一 Fluent 控件与分段页签；保留 Qt 数据模型、信号及生命周期语义。"""

from typing import Any

from PySide6.QtCore import QPoint, Qt, Signal, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QSizePolicy, QStackedWidget, QVBoxLayout, QWidget, QLayoutItem
from qfluentwidgets import (
    FlowLayout as FluentFlowLayout, BodyLabel as BodyLabel, CheckBox as CheckBox, ComboBox, DoubleSpinBox as FluentDoubleSpinBox, LineEdit as FluentLineEdit,
    ListWidget as ListWidget, Pivot, PlainTextEdit as PlainTextEdit, PushButton as PushButton, ScrollArea as FluentScrollArea,
    Slider as Slider, SpinBox as FluentSpinBox, ToolButton as ToolButton, TreeWidget as TreeWidget, FluentWindow, FluentIcon,
    NavigationItemPosition, RoundMenu,
)


class ActionFlowLayout(FluentFlowLayout):
    """仅用于按钮换行；修正 1.11.3 takeAt 的 QWidget 返回值，遵守 Qt 布局析构契约。"""
    def takeAt(self, index: int) -> QLayoutItem | None:
        item = self.itemAt(index)
        if item is not None:
            super().takeAt(index)
        return item


def _fit_numeric_editor(spin: Any) -> None:
    """Qt 6.12 可能把 Fluent 自绘按钮旁的原生编辑区压到 1px，按按钮实际位置排布。"""
    field = spin.lineEdit()
    if field is not None:
        right = spin.upButton.x() if spin.upButton.isVisible() else spin.width() - 8
        field.setGeometry(12, 4, max(1, right - 18), max(1, spin.height() - 8))


class SpinBox(FluentSpinBox):
    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self, lambda: _fit_numeric_editor(self))

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self, lambda: _fit_numeric_editor(self))


class DoubleSpinBox(FluentDoubleSpinBox):
    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self, lambda: _fit_numeric_editor(self))

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self, lambda: _fit_numeric_editor(self))


class LineEdit(FluentLineEdit):
    """同时接受初始文本和父对象，避免把文本误传给 Fluent 的 parent 参数。"""

    def __init__(self, text: str | QWidget = '', parent: QWidget | None = None) -> None:
        if isinstance(text, QWidget):
            parent, text = text, ''
        super().__init__(parent)
        self.setText(text)


class ChoiceBox(ComboBox):
    """保留现有 DTO 选择值；菜单、箭头和悬浮效果由 Fluent 提供。"""

    def addItem(self, text: str, userData: Any = None, icon: Any = None) -> None:  # type: ignore[override]
        # 兼容业务旧调用的第二个 data 参数；有意与库的 icon/data 顺序不同。
        super().addItem(text, icon=icon, userData=userData)

    def wheelEvent(self, event: Any) -> None:
        event.ignore()


class Menu(RoundMenu):
    """Fluent 菜单，集中适配原有文本/回调写法，不重复绘制菜单项目。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)

    def addAction(self, action: QAction | str, callback: Any = None) -> QAction:  # type: ignore[override]
        # 只开放应用使用的两种调用，不暴露原生 QMenu 的图标/快捷键重载。
        if isinstance(action, str): action = QAction(action, self)
        if callback is not None: action.triggered.connect(callback)
        super().addAction(action)
        return action

    def popup(self, pos: QPoint, /, at: QAction | None = None) -> None:
        if at is not None: self.setDefaultAction(at)
        self.exec(pos)


class ScrollArea(FluentScrollArea):
    """沿用库的滚动委托，移除业务阅读区域无意义的原生外框。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QFrame
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.viewport().setAutoFillBackground(False)

    def setWidget(self, widget: QWidget) -> None:
        super().setWidget(widget)
        widget.setAutoFillBackground(False)


class Tabs(QWidget):
    """Pivot 与页面栈是唯一页签实现，不再同时保留一套原生页签栏。"""

    currentChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pivot = Pivot(self)
        self.pivot.setMinimumWidth(0)
        self.pages = QStackedWidget(self)
        self.pages.setMinimumWidth(0)
        self.pages.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        layout.addWidget(self.pivot)
        layout.addWidget(self.pages, 1)
        self._titles: list[str] = []
        self.pages.currentChanged.connect(self._changed)

    def addTab(self, widget: QWidget, title: str) -> int:
        index = len(self._titles)
        self._titles.append(title)
        self.pivot.addItem(str(index), title, onClick=lambda: self.setCurrentIndex(index))
        self.pages.addWidget(widget)
        if index == 0:
            self.pivot.setCurrentItem('0')
        return index

    def _changed(self, index: int) -> None:
        self.pivot.setCurrentItem(str(index))
        self.currentChanged.emit(index)

    def currentIndex(self) -> int:
        return self.pages.currentIndex()

    def currentWidget(self) -> QWidget:
        return self.widget(self.currentIndex())

    def setCurrentIndex(self, index: int) -> None:
        self.pages.setCurrentIndex(index)

    def count(self) -> int:
        return self.pages.count()

    def widget(self, index: int) -> QWidget:
        page = self.pages.widget(index)
        if page is None:
            raise IndexError(index)
        return page

    def tabText(self, index: int) -> str:
        return self._titles[index]

    def setTabText(self, index: int, title: str) -> None:
        self._titles[index] = title
        self.pivot.setItemText(str(index), title)

    def setUsesScrollButtons(self, enabled: bool) -> None:
        # Pivot 自行管理溢出菜单，不引入另一套滚动按钮。
        pass


def primary(button: PushButton) -> None:
    """同一实际操作按钮的强调，不替换对象或丢失原信号连接。"""
    from qfluentwidgets import setCustomStyleSheet
    button.setProperty('deskpalPrimary', True)
    setCustomStyleSheet(button,
        'PushButton {background:#6261d8;color:white;border:1px solid #6261d8;} PushButton:hover {background:#5554c5;} PushButton:disabled {background:#eeeef5;color:#999bad;}',
        'PushButton {background:#b5b0ff;color:#24213b;} PushButton:disabled {background:#39364f;color:#9c98b3;}')


class SettingsWindow(FluentWindow):
    """后台安全关闭期间的设置外壳，与聊天工作台共用 Fluent 窗口体系。

    此时不提供聊天导航：连接及档案服务必须独占资源，完成后重新打开后台。
    表单与凭据仍由原有维护 worker 处理，窗口只负责显示。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        from .theme import configure
        configure()
        super().__init__(parent)
        self.setMicaEffectEnabled(False)
        self.stackedWidget.setAnimationEnabled(False)
        self.resize(1180, 800)
        self.setMinimumSize(560, 420)
        self.navigationInterface.setExpandWidth(184)
        self.navigationInterface.setReturnButtonVisible(False)
        from qfluentwidgets import qconfig
        qconfig.themeChanged.connect(self._theme_changed)

    def _theme_changed(self) -> None:
        from .theme import apply
        apply(self)

    def set_settings_content(self, content: QWidget) -> None:
        from .theme import apply
        content.setObjectName('deskpalWorkspace')
        self.addSubInterface(content, FluentIcon.SETTING, '设置',
                             position=NavigationItemPosition.BOTTOM)
        apply(self)

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        screen = self.screen()
        if screen is not None:
            area = screen.availableGeometry()
            width, height = max(1, area.width() - 24), max(1, area.height() - 48)
            self.setMinimumSize(min(560, width), min(420, height))
            if self.width() > width or self.height() > height:
                self.resize(min(self.width(), width), min(self.height(), height))
