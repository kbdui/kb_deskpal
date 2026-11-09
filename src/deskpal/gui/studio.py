"""清透工作室的页面布局组件，不持有业务数据或后台状态。"""
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget

from .components import ChoiceBox, ListWidget


def page_layout(widget: QWidget) -> QVBoxLayout:
    """统一页内留白，内容随窗口缩放，避免固定宽度挤掉详情。"""
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(28, 24, 28, 24)
    layout.setSpacing(18)
    return layout


def group_setting_rows(layout: QVBoxLayout) -> None:
    """把相邻的字段行归入同一张卡片，保留原控件及其信号连接。"""
    index = 0
    while index < layout.count():
        entry = layout.itemAt(index)
        if entry is None or not isinstance(entry.layout(), QHBoxLayout):
            index += 1
            continue
        card = QFrame()
        card.setObjectName('studio_setting_card')
        rows = QVBoxLayout(card)
        rows.setContentsMargins(18, 6, 18, 6)
        rows.setSpacing(0)
        while index < layout.count():
            entry = layout.itemAt(index)
            if entry is None or not isinstance(entry.layout(), QHBoxLayout):
                break
            item = layout.takeAt(index)
            assert item is not None
            row = item.layout()
            assert isinstance(row, QHBoxLayout)
            row.setContentsMargins(0, 12, 0, 12)
            if rows.count():
                divider = QFrame(); divider.setObjectName('studio_divider'); divider.setFixedHeight(1)
                rows.addWidget(divider)
            rows.addLayout(row)
        layout.insertWidget(index, card)
        index += 1


class SettingsSections(QWidget):
    """宽窗使用侧目录，窄窗使用同一目录的选择器，页面实例始终保留。"""

    currentChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pages = QStackedWidget()
        self.pages.setMinimumWidth(0)
        self.navigation = ListWidget()
        self.navigation.setObjectName('settings_navigation')
        self.navigation.setFixedWidth(144)
        self.selector = ChoiceBox()
        self.selector.hide()
        self._titles: list[str] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.selector)
        row = QHBoxLayout()
        row.setSpacing(24)
        row.addWidget(self.navigation)
        row.addWidget(self.pages, 1)
        layout.addLayout(row, 1)
        self.navigation.currentRowChanged.connect(self.setCurrentIndex)
        self.selector.currentIndexChanged.connect(self.setCurrentIndex)
        self.pages.currentChanged.connect(self._changed)

    def addTab(self, page: QWidget, title: str) -> int:
        self._titles.append(title)
        self.navigation.addItem(title)
        self.selector.addItem(title)
        index = self.pages.addWidget(page)
        if index == 0:
            self._changed(0)
        return index

    def _changed(self, index: int) -> None:
        for control in (self.navigation, self.selector):
            control.blockSignals(True)
        self.navigation.setCurrentRow(index)
        self.selector.setCurrentIndex(index)
        for control in (self.navigation, self.selector):
            control.blockSignals(False)
        self.currentChanged.emit(index)

    def setCurrentIndex(self, index: int) -> None:
        if 0 <= index < self.count():
            self.pages.setCurrentIndex(index)

    def currentIndex(self) -> int:
        return self.pages.currentIndex()

    def count(self) -> int:
        return self.pages.count()

    def widget(self, index: int) -> QWidget:
        page = self.pages.widget(index)
        if page is None: raise IndexError(index)
        return page

    def tabText(self, index: int) -> str:
        return self._titles[index]

    def setUsesScrollButtons(self, enabled: bool) -> None:
        pass

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        narrow = self.width() < 720
        self.navigation.setVisible(not narrow)
        self.selector.setVisible(narrow)
