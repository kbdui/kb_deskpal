import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QInputDialog, QPushButton, QLabel
from deskpal.gui.task_card import TaskCard
from deskpal.gui.bridge import CONTROL_OPERATIONS


class Channel:
    def __init__(self):
        self.calls = []
    def send(self, operation, parameters, callback, *, mutation=False):
        self.calls.append((operation, parameters, mutation))
        return "synthetic"


class Coordinator(QObject):
    task_changed = Signal(str)
    reviews_cleared = Signal()
    def __init__(self, status):
        super().__init__()
        self.reviews = {"task": {"status": status, "task": {"status": status,
            "result": {"response": "需要处理哪一项？"}}, "steps": [], "available_actions": []}}
        self.grants = {}
        self.task_pending = set()
        self.channel = Channel()
    def watch_review(self, token, task_id):
        pass
    def review(self, task_id):
        pass


@pytest.mark.parametrize("status,button_text,operation", [
    ("Running", "追加要求", "task_append_requirement"),
    ("AwaitingInput", "补充回答并继续", "task_answer"),
])
@pytest.mark.parametrize("compact", [True, False])
def test_task_control_is_available_in_main_and_pet_views(qtbot, monkeypatch, status, button_text, operation, compact):
    coordinator = Coordinator(status)
    card = TaskCard(coordinator, "task", compact=compact)
    qtbot.addWidget(card)
    button = next(widget for widget in card.findChildren(QPushButton) if widget.text() == button_text)
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *args: ("选择乙，保留已完成结果", True))
    button.click()
    assert coordinator.channel.calls == [(operation, {"task_id": "task", "content": "选择乙，保留已完成结果"}, True)]
    assert operation in CONTROL_OPERATIONS
    if status == "AwaitingInput":
        assert any("需要处理哪一项" in widget.text() for widget in card.findChildren(QLabel))
