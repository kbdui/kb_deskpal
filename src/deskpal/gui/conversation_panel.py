"""Session controls and explicit interaction consent, separate from task approval."""

from typing import Any
from uuid import uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from .conversation_state import ConversationSnapshot, InteractionSnapshot


class ConversationPanel(QWidget):
    navigate_task = Signal(str)
    navigate_memory_candidates = Signal()
    conversation_changed = Signal()

    def __init__(self, bridge: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self._available = False
        self._generation = 2_000_000
        self._pending: dict[str, tuple[str, int, int, str]] = {}
        self._privacy_receipts: dict[str, tuple[int, int]] = {}
        self._sessions: dict[str, int] = {}
        self._session_id = ""
        self._memory_epoch = -1
        self._snapshot: ConversationSnapshot | None = None
        self._interaction: InteractionSnapshot | None = None
        self._permissions_dirty = False
        self._identity_dirty = False
        self._command_action = ""
        self._active_task_id = ""
        self._populating = False
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.sessions = QComboBox()
        self.sessions.addItem("独立请求（无会话历史）", "")
        self.new_button = QPushButton("新建会话")
        self.close_button = QPushButton("关闭并清空本会话上下文")
        row.addWidget(self.sessions)
        row.addWidget(self.new_button)
        row.addWidget(self.close_button)
        layout.addLayout(row)
        self.notice = QLabel("会话仅在本次运行保留最近 8 轮 / 16 KiB；任务记录仍按原规则保存。")
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.notice)
        self.body_tabs = QTabWidget()
        layout.addWidget(self.body_tabs)
        transcript = QWidget()
        self.body_tabs.addTab(transcript, "近期对话与任务")
        layout = QVBoxLayout(transcript)
        self.history = QPlainTextEdit()
        self.history.setReadOnly(True)
        self.history.document().setMaximumBlockCount(400)
        layout.addWidget(self.history)
        task_row = QHBoxLayout()
        self.task_choices = QComboBox()
        self.task_button = QPushButton("到任务页审阅所选任务")
        task_row.addWidget(self.task_choices)
        task_row.addWidget(self.task_button)
        layout.addLayout(task_row)
        interaction_page = QWidget()
        self.body_tabs.addTab(interaction_page, "互动设置与偏好候选")
        layout = QVBoxLayout(interaction_page)
        permissions = QGroupBox("云模型用途许可（默认关闭，仍受来源用途限制）")
        permission_layout = QVBoxLayout(permissions)
        self.permission_checks: dict[str, QCheckBox] = {}
        for key, label in (("custom_persona", "自定义人格与六轴"), ("preferences", "明确表达偏好"),
                           ("cue", "历史情境线索"), ("affect", "应用模拟情绪"), ("history", "当前会话历史")):
            box = QCheckBox(label)
            box.toggled.connect(self._mark_permissions_dirty)
            self.permission_checks[key] = box
            permission_layout.addWidget(box)
        self.permissions_save = QPushButton("保存用途许可")
        permission_layout.addWidget(self.permissions_save)
        self.permission_hint = QLabel("未获准的类别仅在本地使用；允许类别不能解除 local_only 来源限制。")
        self.permission_hint.setWordWrap(True)
        permission_layout.addWidget(self.permission_hint)
        layout.addWidget(permissions)
        candidate_row = QHBoxLayout()
        self.candidates = QComboBox()
        self.candidates.setMaximumWidth(650)
        self.confirm_candidate = QPushButton("确认保存此偏好")
        self.reject_candidate = QPushButton("拒绝此候选")
        candidate_row.addWidget(self.candidates)
        candidate_row.addWidget(self.confirm_candidate)
        candidate_row.addWidget(self.reject_candidate)
        layout.addLayout(candidate_row)
        self.candidate_detail = QLabel("候选未确认前不会保存；确认不授予任何工具权限。")
        self.candidate_detail.setWordWrap(True)
        self.candidate_detail.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.candidate_detail)
        profile_hint = QLabel("这里确认回复详略、幽默和追问偏好。称呼与回复语言候选请到记忆页查看来源，"
                              "并使用该页的接受或拒绝操作；出现候选不表示已保存。")
        profile_hint.setWordWrap(True)
        profile_hint.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(profile_hint)
        self.memory_candidates_button = QPushButton("查看称呼 / 语言候选")
        layout.addWidget(self.memory_candidates_button)
        identity_group = QGroupBox("有限角色身份（本地显式设置）")
        identity_layout = QFormLayout(identity_group)
        self.display_name = QLineEdit()
        self.display_name.setMaxLength(40)
        self.display_name.textEdited.connect(self._mark_identity_dirty)
        identity_layout.addRow("名称", self.display_name)
        self.identity_choices: dict[str, QComboBox] = {}
        for key, label, values in (("role_kind", "角色", ("assistant", "companion")),
                                   ("self_reference", "自称", ("neutral", "name", "first_person")),
                                   ("greeting_style", "问候", ("none", "brief")),
                                   ("followup_style", "追问", ("none", "optional_one"))):
            choice = QComboBox()
            choice.addItems(values)
            choice.currentIndexChanged.connect(self._mark_identity_dirty)
            self.identity_choices[key] = choice
            identity_layout.addRow(label, choice)
        self.identity_use_policy = QComboBox()
        self.identity_use_policy.addItems(["local_only", "cloud_allowed"])
        self.identity_use_policy.currentIndexChanged.connect(self._mark_identity_dirty)
        identity_layout.addRow("身份来源用途", self.identity_use_policy)
        self.identity_save = QPushButton("保存角色身份")
        identity_layout.addRow(self.identity_save)
        layout.addWidget(identity_group)
        clear_row = QHBoxLayout()
        self.clear_cue = QPushButton("清除本会话短时线索")
        self.clear_constraints = QPushButton("清除本会话表达覆盖")
        clear_row.addWidget(self.clear_cue)
        clear_row.addWidget(self.clear_constraints)
        layout.addLayout(clear_row)
        self.sessions.currentIndexChanged.connect(self._selected)
        self.new_button.clicked.connect(lambda: self._send("conversation_open", {}, mutation=True))
        self.close_button.clicked.connect(self._close)
        self.task_button.clicked.connect(self._navigate)
        self.permissions_save.clicked.connect(self._save_permissions)
        self.confirm_candidate.clicked.connect(lambda: self._candidate("confirm_candidate"))
        self.reject_candidate.clicked.connect(lambda: self._candidate("reject_candidate"))
        self.memory_candidates_button.clicked.connect(self._navigate_memory_candidates)
        self.candidates.currentIndexChanged.connect(self._candidate_selected)
        self.identity_save.clicked.connect(self._save_identity)
        self.clear_cue.clicked.connect(lambda: self._clear_interaction("clear_cue"))
        self.clear_constraints.clicked.connect(lambda: self._clear_interaction("clear_constraints"))
        bridge.reply.connect(self._reply, Qt.ConnectionType.QueuedConnection)
        if hasattr(bridge, "privacy_changed"):
            bridge.privacy_changed.connect(self.clear_sensitive, Qt.ConnectionType.QueuedConnection)
        self._enabled()

    def submission_parameters(self) -> dict[str, Any]:
        identity = str(self.sessions.currentData() or "")
        return ({"conversation_id": identity, "expected_conversation_epoch": self._sessions[identity]}
                if identity else {})

    def track_task(self, task_id: str) -> None:
        """Only a navigation handle; the task page owns the executable plan."""
        self._active_task_id = task_id
        if task_id and self.task_choices.findData(task_id) < 0:
            self.task_choices.addItem("当前请求 · " + task_id[:8], task_id)
            self.task_choices.setCurrentIndex(self.task_choices.count() - 1)
        self._enabled()

    def set_available(self, available: bool) -> None:
        changed = available != self._available
        self._available = available
        if not available and changed:
            self._generation += 1
            self._pending.clear()
        self._enabled()

    def _enabled(self) -> None:
        ready = self._available and not any(item[0] in {"conversation_open", "conversation_close", "interaction_command"}
                                            for item in self._pending.values())
        self.sessions.setEnabled(ready)
        self.new_button.setEnabled(ready and len(self._sessions) < 4)
        self.close_button.setEnabled(ready and bool(self.sessions.currentData()))
        self.task_button.setEnabled(ready and self.task_choices.count() > 0)
        self.memory_candidates_button.setEnabled(ready)
        for box in self.permission_checks.values():
            box.setEnabled(ready and self._interaction is not None)
        self.permissions_save.setEnabled(ready and self._interaction is not None and self._permissions_dirty)
        self.display_name.setEnabled(ready and self._interaction is not None)
        for choice in self.identity_choices.values():
            choice.setEnabled(ready and self._interaction is not None)
        self.identity_use_policy.setEnabled(ready and self._interaction is not None)
        self.identity_save.setEnabled(ready and self._interaction is not None and self._identity_dirty)
        for button in (self.clear_cue, self.clear_constraints):
            button.setEnabled(ready and self._interaction is not None and bool(self.sessions.currentData()))
        for button in (self.confirm_candidate, self.reject_candidate):
            button.setEnabled(ready and self._interaction is not None and self.candidates.count() > 0)

    def _send(self, operation: str, params: dict[str, Any], *, mutation: bool = False) -> None:
        if not self._available or len(self._pending) >= 4 or any(item[0] == operation for item in self._pending.values()):
            return
        parameters = dict(params)
        if operation == "interaction_command":
            self._command_action = str(parameters.get("action", ""))
        if mutation:
            parameters["command_id"] = str(uuid4())
        try:
            request_id = str(self.bridge.send(operation, parameters, generation=self._generation))
        except (RuntimeError, ValueError, BufferError):
            self.notice.setText("界面请求未受理，请刷新后重试。")
            return
        self._pending[request_id] = (operation, self._generation,
                                     int(getattr(self.bridge, "privacy_epoch", 0)), str(self.sessions.currentData() or ""))
        self._enabled()

    def poll(self) -> None:
        if self.sessions.currentData():
            self._send("conversation_snapshot", {"conversation_id": self.sessions.currentData()})
        self._send("interaction_snapshot", {})

    def _selected(self, _: int) -> None:
        self._generation += 1
        self._pending.clear()
        self._snapshot = None
        self._active_task_id = ""
        self._interaction = None
        self.candidates.clear()
        self.candidate_detail.clear()
        self.history.clear()
        self.task_choices.clear()
        self.conversation_changed.emit()
        self.poll()
        self._enabled()

    def _close(self) -> None:
        identity = str(self.sessions.currentData() or "")
        if identity:
            self._send("conversation_close", {"conversation_id": identity,
                                              "expected_epoch": self._sessions[identity]}, mutation=True)

    def _navigate(self) -> None:
        identity = self.task_choices.currentData()
        if isinstance(identity, str) and identity:
            self.navigate_task.emit(identity)

    def _navigate_memory_candidates(self) -> None:
        if self._available and self.memory_candidates_button.isEnabled():
            self.navigate_memory_candidates.emit()

    def _mark_permissions_dirty(self, _: bool) -> None:
        if not self._populating:
            self._permissions_dirty = True
        self._enabled()

    def _save_permissions(self) -> None:
        if self._interaction is not None:
            self._send("interaction_command", {"action": "cloud_permissions",
                "expected_revision": self._interaction.revision,
                "permissions": {key: box.isChecked() for key, box in self.permission_checks.items()}}, mutation=True)

    def _mark_identity_dirty(self, _: Any) -> None:
        if not self._populating:
            self._identity_dirty = True
        self._enabled()

    def _save_identity(self) -> None:
        if self._interaction is not None:
            self._send("interaction_command", {"action": "identity", "expected_revision": self._interaction.revision,
                "display_name": self.display_name.text(), "use_policy": self.identity_use_policy.currentText(),
                **{key: choice.currentText() for key, choice in self.identity_choices.items()}}, mutation=True)

    def _clear_interaction(self, action: str) -> None:
        if self._interaction is not None and self.sessions.currentData():
            parameters: dict[str, Any] = {"action": action, "expected_revision": self._interaction.revision,
                                          "conversation_id": self.sessions.currentData()}
            if action == "clear_constraints":
                parameters["scope"] = "session"
            self._send("interaction_command", parameters, mutation=True)

    def _candidate(self, action: str) -> None:
        if self._interaction is not None and self.candidates.currentData():
            self._send("interaction_command", {"action": action,
                "expected_revision": self._interaction.revision, "candidate_id": self.candidates.currentData()}, mutation=True)

    def _candidate_selected(self, _: int) -> None:
        identity = self.candidates.currentData()
        text = next((text for key, text in self._interaction.candidates if key == identity), "") if self._interaction else ""
        self.candidate_detail.setText(text or "候选未确认前不会保存；确认不授予任何工具权限。")

    def _reply(self, envelope: Any) -> None:
        if not isinstance(envelope, dict):
            return
        request_id = str(envelope.get("request_id", ""))
        pending = self._pending.pop(request_id, None)
        if pending is None:
            acknowledgement = self._privacy_receipts.pop(request_id, None)
            data = envelope.get("data")
            if (acknowledgement is not None and self._available and envelope.get("ok") is True
                    and envelope.get("operation") == "interaction_command"
                    and envelope.get("generation") == acknowledgement[0]
                    and type(envelope.get("privacy_epoch")) is int
                    and envelope["privacy_epoch"] > acknowledgement[1]
                    and envelope["privacy_epoch"] == int(getattr(self.bridge, "privacy_epoch", 0))
                    and isinstance(data, dict) and data.get("privacy_invalidated") is True
                    and (not self._session_id or data.get("session_id") == self._session_id)):
                # The privacy signal already erased editable bodies and old
                # context. Acknowledge with fixed text and fetch fresh DTOs.
                self.notice.setText("用途已撤销，旧会话上下文和候选已清空；正在加载当前设置。")
                self.poll()
                self._enabled()
            return
        self._enabled()
        operation, generation, epoch, selected = pending
        if (not self._available or generation != self._generation or envelope.get("generation") != generation
                or envelope.get("privacy_epoch") != epoch or epoch != int(getattr(self.bridge, "privacy_epoch", 0))
                or selected != str(self.sessions.currentData() or "")):
            return
        if not envelope.get("ok"):
            self.notice.setText("操作未完成；请刷新状态并审核当前版本后重试。")
            if operation == "conversation_snapshot" and envelope.get("error", {}).get("code") in {"Conflict", "NotFound"}:
                self._remove_session(selected)
            return
        data = envelope.get("data")
        if not isinstance(data, dict):
            return
        session_id, memory_epoch = data.get("session_id"), data.get("memory_epoch")
        if not isinstance(session_id, str) or type(memory_epoch) is not int:
            return
        if self._session_id and session_id != self._session_id:
            self.clear_sensitive()
            return
        if memory_epoch < self._memory_epoch:
            return
        if self._memory_epoch >= 0 and memory_epoch > self._memory_epoch:
            self.clear_sensitive()
            self._memory_epoch = memory_epoch
            return
        self._session_id, self._memory_epoch = session_id, memory_epoch
        try:
            if operation == "conversation_open":
                identity, new_epoch = data.get("conversation_id", data.get("id")), data.get("epoch")
                if not isinstance(identity, str) or type(new_epoch) is not int or len(self._sessions) >= 4:
                    raise ValueError("Invalid opened conversation")
                self._sessions[identity] = new_epoch
                self.sessions.addItem("会话 " + identity[:8], identity)
                self.sessions.setCurrentIndex(self.sessions.count() - 1)
            elif operation == "conversation_close":
                self._remove_session(selected)
            elif operation == "conversation_snapshot":
                snapshot = ConversationSnapshot.decode(data)
                if snapshot.conversation_id != selected or snapshot.epoch != self._sessions.get(selected):
                    return
                if self._snapshot and snapshot.turns and self._snapshot.turns and snapshot.turns[-1].sequence < self._snapshot.turns[-1].sequence:
                    return
                self._snapshot = snapshot
                self.history.setPlainText("\n\n".join("您：" + item.request + "\n助手：" + item.response for item in snapshot.turns))
                self.task_choices.clear()
                for item in snapshot.turns:
                    self.task_choices.addItem("第 " + str(item.sequence) + " 轮 · " + item.task_id[:8], item.task_id)
                self.track_task(self._active_task_id)
            elif operation == "interaction_snapshot":
                snapshot_i = InteractionSnapshot.decode(data, conversation_id=selected)
                if self._interaction and (snapshot_i.interaction_epoch < self._interaction.interaction_epoch
                                           or snapshot_i.revision < self._interaction.revision):
                    return
                self._interaction = snapshot_i
                if not self._permissions_dirty:
                    self._populating = True
                    for key, allowed in snapshot_i.permissions:
                        self.permission_checks[key].setChecked(allowed)
                    self._populating = False
                if not self._identity_dirty:
                    self._populating = True
                    identity_map = dict(snapshot_i.identity)
                    self.display_name.setText(identity_map["display_name"])
                    for key, choice in self.identity_choices.items():
                        choice.setCurrentText(identity_map[key])
                    self.identity_use_policy.setCurrentText(snapshot_i.identity_use_policy)
                    self._populating = False
                selected_candidate = self.candidates.currentData()
                self.candidates.clear()
                for identity, text in snapshot_i.candidates:
                    self.candidates.addItem(text[:100], identity)
                index = self.candidates.findData(selected_candidate)
                if index >= 0:
                    self.candidates.setCurrentIndex(index)
                self._candidate_selected(0)
            elif operation == "interaction_command":
                if self._command_action == "cloud_permissions":
                    self._permissions_dirty = False
                elif self._command_action == "identity":
                    self._identity_dirty = False
                self._interaction = None
                self.candidates.clear()
                self.notice.setText("本地管理操作已受理；候选保存结果以最新状态和回执为准。")
                self.poll()
        except (ValueError, TypeError, UnicodeError):
            self.notice.setText("无法识别当前状态，未启用相应操作。")
        self._enabled()

    def _remove_session(self, identity: str) -> None:
        self._sessions.pop(identity, None)
        index = self.sessions.findData(identity)
        if index >= 0:
            self.sessions.removeItem(index)

    def clear_sensitive(self, _: int = 0) -> None:
        self._generation += 1
        for request_id, pending in self._pending.items():
            if pending[0] == "interaction_command":
                self._privacy_receipts[request_id] = (pending[1], pending[2])
        while len(self._privacy_receipts) > 4:
            self._privacy_receipts.pop(next(iter(self._privacy_receipts)))
        self._pending.clear()
        self._sessions.clear()
        self._snapshot = None
        self._active_task_id = ""
        self._interaction = None
        self._permissions_dirty = False
        self._identity_dirty = False
        self._memory_epoch = -1
        self.sessions.blockSignals(True)
        self.sessions.clear()
        self.sessions.addItem("独立请求（无会话历史）", "")
        self.sessions.blockSignals(False)
        self.history.clear()
        self.task_choices.clear()
        self.candidates.clear()
        self.candidate_detail.clear()
        self.notice.clear()
        self._populating = True
        self.display_name.clear()
        self.identity_use_policy.setCurrentIndex(0)
        for choice in self.identity_choices.values():
            choice.setCurrentIndex(0)
        for box in self.permission_checks.values():
            box.setChecked(False)
        self._populating = False
        self._enabled()
