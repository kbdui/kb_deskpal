"""Prepared GUI acceptance against the five-page shell; not executed in D1–D5."""

from copy import deepcopy
from threading import Thread
from typing import Any

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QObject, QPoint, QRect, QSettings, QSize, QThread, Signal, Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QTreeWidget

from deskpal.gui.channel import Channel
from deskpal.gui.preferences import GuiPreferences
from deskpal.gui.task_card import TaskCard
from deskpal.gui.window import MainWindow
from deskpal.gui.views import CommandForm, PagedView
from deskpal.gui.widgets import fill_tree
from deskpal.gui.components import ChoiceBox as QComboBox


class FakeBridge(QObject):
    state_changed = Signal(str)
    reply = Signal(object)
    privacy_changed = Signal(int)
    image_reply = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.state = "Initializing"
        self.privacy_epoch = 0
        self.sent: list[dict[str, Any]] = []
        self.shutdown_count = 0
        self.start_count = 0

    def set_state(self, state: str) -> None:
        self.state = state
        self.state_changed.emit(state)

    def send(self, operation: str, parameters: dict[str, Any] | None = None, *, generation: int = 0) -> str:
        if operation in {"request_forget", "conversation_clear", "conversation_delete"} or (operation == "memory" and (
                parameters or {}).get("action") in {"forget", "correct", "accept", "reject"}):
            self.privacy_epoch += 1
            self.privacy_changed.emit(self.privacy_epoch)
        request_id = "request-" + str(len(self.sent) + 1)
        self.sent.append({"request_id": request_id, "operation": operation,
                          "parameters": deepcopy(parameters or {}), "generation": generation,
                          "privacy_epoch": self.privacy_epoch})
        return request_id

    def send_image(self, operation: str, parameters: dict[str, Any], generation: int = 0) -> str:
        return self.send(operation, parameters, generation=generation)

    def start(self) -> None:
        self.start_count += 1

    def shutdown(self) -> None:
        self.shutdown_count += 1

    def requests_for(self, operation: str) -> list[dict[str, Any]]:
        return [item for item in self.sent if item["operation"] == operation]

    def answer(self, request: dict[str, Any], data: dict[str, Any] | None = None,
               *, error: str | None = None) -> None:
        response = {key: request[key] for key in ("request_id", "operation", "generation", "privacy_epoch")}
        response.update(ok=error is None, data=data or {}, error={"code": error, "message": "synthetic failure"})
        self.reply.emit(response)


def pending(widget: MainWindow, identity: str) -> bool:
    return any(identity in channel.pending for channel in widget.findChildren(Channel))


def deliver(qtbot: Any, widget: MainWindow, bridge: FakeBridge,
            request: dict[str, Any], data: dict[str, Any] | None = None, error: str | None = None) -> None:
    bridge.answer(request, data, error=error)
    qtbot.waitUntil(lambda: not pending(widget, request["request_id"]))


@pytest.fixture
def window(qtbot: Any) -> tuple[MainWindow, FakeBridge]:
    bridge = FakeBridge()
    widget = MainWindow(bridge)
    qtbot.addWidget(widget)
    bridge.set_state("Ready")
    qtbot.waitUntil(lambda: widget.lifecycle == "Ready")
    widget.poll_timer.stop()
    state = widget.chat_coordinator
    state.timer.stop()
    daily = {"id": "daily", "title": "日常聊天", "kind": "default", "revision": 1, "context_epoch": 1}
    boot = list(bridge.sent)
    for request in boot:
        op = request["operation"]
        data = ({"llm_ready": True} if op == "settings" else daily if op == "conversation_ensure_default"
                else {"items": [daily]} if op == "conversation_list" else {"profiles": []})
        deliver(qtbot, widget, bridge, request, data)
    state.default_id = "daily"
    state.conversations["daily"] = daily
    state.model_ready = True
    widget.chat_view.set_conversation("daily")
    return widget, bridge


def task_review(task_id: str = "task-1", status: str = "Waiting", revision: int = 1) -> dict[str, Any]:
    return {"task_id": task_id, "task_revision": revision, "confirmation_revision": revision,
            "title": "整理文件", "status": status, "conversation_id": "daily",
            "plan_hash": "a" * 64, "workspace": "synthetic/workspace", "confirmation_state": "none",
            "steps": [{"tool_name": "read", "display_name": "读取文件", "arguments": {"path": "full synthetic parameters"}},
                      {"tool_name": "delete", "display_name": "删除文件",
                       "arguments": {"path": "explicit high-risk target", "text": "<img src='https://invalid.test/private'>"}}],
            "missing_permissions": {"ordinary_tools": ["read"], "high_risk_tools": ["delete"]},
            "available_actions": ["confirm", "cancel"] if status == "Waiting" else [],
            "task": {"id": task_id, "status": status, "revision": revision, "result": None},
            "attempt_summary": {"count": 0}}


def memory_item() -> dict[str, Any]:
    return {"id": "memory-1", "revision": 3, "status": "candidate",
            "draft": {"content": "synthetic sensitive body", "quote": "synthetic quote"},
            "source": {"content": "synthetic source", "origin": "user_message", "use_policy": "local_only"}}


def show_task(qtbot: Any, widget: MainWindow, bridge: FakeBridge,
              review: dict[str, Any] | None = None) -> TaskCard:
    value = review or task_review()
    widget.set_page(0)
    state = widget.chat_coordinator
    state.set_review(value['task_id'], value)
    state._timeline('daily', {'items': [{'id':'review-card', 'sequence':1, 'kind':'task_card',
        'task_id':value['task_id'], 'visible':True}]}, False)
    return widget.chat_view.findChildren(TaskCard)[-1]


def text_in_tree(tree: QTreeWidget) -> str:
    def values(item: Any) -> list[str]:
        return [item.text(0), item.text(1), *(text for i in range(item.childCount()) for text in values(item.child(i)))]
    return "\n".join(text for i in range(tree.topLevelItemCount()) for text in values(tree.topLevelItem(i)))


def button_with_text(widget: Any, title: str) -> QPushButton:
    return next(button for button in widget.findChildren(QPushButton) if button.text() == title)


from deskpal.planner.direct import DirectToolPlanner
from deskpal.planner.models import Plan


class OneToolPlanner(DirectToolPlanner):
    """合成多轮规划器：第一次调用工具，看到真实观察后完成，不重复副作用。"""

    def plan_context(self, request, *, memories=(), tools=(), references=(), llm):
        if any(ref.get('kind') == 'execution_observations' for ref in references):
            return Plan('已读取实际执行结果。')
        return super().plan_context(request, memories=memories, tools=tools, references=references, llm=llm)


def set_form(form: CommandForm, **values: Any) -> None:
    for key, value in values.items():
        field = form.fields[key]
        if isinstance(field, QComboBox):
            field.setCurrentIndex(field.findData(value))
        elif isinstance(field, QSpinBox):
            field.setValue(int(value))
        elif isinstance(field, QPlainTextEdit):
            field.setPlainText(str(value))
        else:
            field.setText(str(value))


def operation_result(qtbot: Any, widget: MainWindow, responses: list[dict[str, Any]],
                     operation: str, count_before: int) -> dict[str, Any]:
    qtbot.waitUntil(lambda: len([item for item in responses if item["operation"] == operation]) > count_before,
                    timeout=5000)
    response = [item for item in responses if item["operation"] == operation][-1]
    qtbot.waitUntil(lambda: not pending(widget, response["request_id"]), timeout=5000)
    assert response["ok"], response
    return response["data"]


def select_entity(qtbot: Any, widget: MainWindow, page: PagedView, entity_id: str) -> None:
    goals = widget.arrangements.goals
    operation = next((name + "s" for name, candidate in goals.pages.items() if candidate is page), None)
    page.refresh_requested.emit()
    # A cached ID is not proof that this refresh has returned the current revision.
    qtbot.waitUntil(lambda: (operation is None or (operation not in goals._read_active and operation not in goals._read_latest))
                    and any(item.get("id") == entity_id for item in page.items), timeout=5000)
    if page.memory_tree is not None:
        page._select_memory_id(entity_id)
    else:
        page.list.setCurrentRow(-1)
        page.list.setCurrentRow(next(index for index in range(page.list.count())
            if (page.list.item(index).data(Qt.ItemDataRole.UserRole) or {}).get('id') == entity_id))


@pytest.fixture
def goals_page(qtbot: Any) -> tuple[Any, FakeBridge]:
    from deskpal.gui.management import GoalsPage
    bridge = FakeBridge(); bridge.state = "Ready"
    page = GoalsPage(bridge); qtbot.addWidget(page)
    page._settings({'request_use_default': False})
    return page, bridge


