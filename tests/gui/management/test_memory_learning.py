from PySide6.QtCore import QObject, Qt, Signal

from deskpal.gui.management import MemoryPage


class Bridge(QObject):
    reply = Signal(object)
    privacy_changed = Signal(int)
    state = 'Ready'
    privacy_epoch = 0

    def send(self, *args, **kwargs):
        return 'synthetic-request'


def test_buffer_is_readable_and_memory_page_has_no_editing_actions(qtbot):
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
    assert tree.contextMenuPolicy() == Qt.ContextMenuPolicy.DefaultContextMenu
    assert page.page.action_scroll.isHidden()
    assert page.learning_button.isVisible()
    assert not hasattr(page, 'forms')
