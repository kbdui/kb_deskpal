from PySide6.QtCore import QObject, Signal
from types import SimpleNamespace
from deskpal.gui import management

from deskpal.gui.management import MemoryPage


class Bridge(QObject):
    reply = Signal(object)
    privacy_changed = Signal(int)
    state = 'Ready'
    privacy_epoch = 0

    def send(self, *args, **kwargs):
        return 'synthetic-request'


def test_buffer_is_readable_and_has_no_manual_approval_actions(qtbot, monkeypatch):
    page = MemoryPage(Bridge())
    qtbot.addWidget(page)
    page.resize(1000, 760)
    page.show()
    item = dict(id='buffer', revision=1, status='candidate', draft=dict(content='倾诉时可能希望先被倾听',
        quote='先听我说', destination='habit', reason='今天明确希望先倾诉', uncertainty='暂时推测，仅适用于倾诉情境'),
        source=dict(origin='user_message', use_policy='cloud_allowed'))
    page.page.set_page({'items': [item]})
    tree = page.page.memory_tree
    row = tree.topLevelItem(0)
    tree.setCurrentItem(row)
    assert '待复核理解' in page.page.summary_meta.text()
    assert '相处习惯' in page.page.summary_meta.text()
    assert '今天明确希望先倾诉' in page.page.summary_context.toPlainText()
    assert '暂时推测' in page.page.summary_context.toPlainText()
    actions = []
    class Menu:
        def __init__(self, *args):
            self.entries = []
        def addAction(self, text, callback):
            self.entries.append(SimpleNamespace(text=lambda: text))
        def addSeparator(self):
            pass
        def actions(self):
            return self.entries
        def exec(self, *args):
            actions.extend(action.text() for action in self.entries)
    # Qt 的原生弹出菜单会进入嵌套事件循环；替换菜单工厂，仅验证真实页面生成的可用动作。
    monkeypatch.setattr(management, 'QMenu', Menu)
    page._memory_context_menu(tree.visualItemRect(row).center())
    assert '更正这条记忆' in actions and '遗忘这条记忆及相关正文' in actions
    assert not any('接受' in action or '拒绝' in action for action in actions)
