"""共享任务卡的控制在主窗、小窗一致；离屏不代表原生实机验收。"""
import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QLabel, QPushButton
from deskpal.gui.task_card import TaskCard
from deskpal.gui.bridge import CONTROL_OPERATIONS


class Coordinator(QObject):
    task_changed = Signal(str)
    reviews_cleared = Signal()

    def __init__(self, status, actions, requested=0):
        super().__init__()
        self.reviews = {'task': dict(status=status, task={'status':status}, steps=[],
            available_actions=actions, pause_control={'requested':requested})}
        self.grants = {}
        self.task_pending = set()
        self.calls = []

    def watch_review(self, token, task_id):
        pass

    def task_action(self, task_id, action):
        self.calls.append((task_id, action))


@pytest.mark.parametrize('compact', [False, True])
@pytest.mark.parametrize('status,action,title', [('Running','task_pause','暂停任务'), ('Paused','task_continue','继续任务')])
def test_pause_continue_controls_shared(qtbot, compact, status, action, title):
    coordinator = Coordinator(status, [action])
    card = TaskCard(coordinator, 'task', compact=compact)
    qtbot.addWidget(card)
    button = next(item for item in card.findChildren(QPushButton) if item.text()==title)
    button.click()
    assert coordinator.calls == [('task', action)]
    assert action in CONTROL_OPERATIONS
    if compact:
        assert not [item for item in card.findChildren(QPushButton) if '查看工具结果' in item.text()]


def test_pause_requested_is_not_already_paused(qtbot):
    coordinator = Coordinator('Running', [], requested=1)
    card = TaskCard(coordinator, 'task')
    qtbot.addWidget(card)
    assert any('正在暂停' in item.text() for item in card.findChildren(QLabel))
    assert not [item for item in card.findChildren(QPushButton) if item.text() in {'暂停任务','继续任务'}]


def test_attention_detail_is_readable_without_approval(qtbot):
    from deskpal.gui.views import PagedView
    page=PagedView('attentions')
    qtbot.addWidget(page)
    page.show_detail(dict(id='internal-only',title='面试准备',description='用户提到准备面试，回应后结束关注',
        status='pending',method='review',due_at='2026-10-30T10:00:00+08:00',expires_at='2026-10-31T10:00:00+08:00'))
    assert page.summary_body.toPlainText()=='用户提到准备面试，回应后结束关注'
    assert '等待合适时机' in page.summary_meta.text()
    assert '模型安排复查' in page.summary_meta.text()
    assert not [button for button in page.findChildren(QPushButton) if '接受' in button.text()]
