"""Explicit local companion controls; every operation uses the background bridge."""

from time import monotonic
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from .companion_state import CompanionSnapshot, PRESETS, STYLE_AXES

FRESH_SECONDS = 3.0
_MODES = {"neutral": "中性", "supportive": "支持", "focused": "专注"}


class CompanionPanel(QWidget):
    """One bounded in-flight request, with a fresh CAS read before each command."""

    def __init__(self, bridge: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self._available = False
        self._generation = 1_000_000
        self._request: str | None = None
        self._request_kind = ""
        self._request_at = 0.0
        self._request_epoch = 0
        self._snapshot: CompanionSnapshot | None = None
        self._authority: CompanionSnapshot | None = None
        self._received_at = 0.0
        self._intent: dict[str, Any] | None = None
        self._config_dirty = False
        self._preset_dirty = False
        self._populating = False
        self._command_action = ""
        layout = QVBoxLayout(self)
        hint = QLabel("人格风格与情绪是应用在本地维护的模拟状态，不代表真实感受。"
                      "这些选项只影响表达与静止桌宠装饰；任务状态仍以任务页为准。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.summary = QLabel("模拟状态尚未加载。")
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.notice = QLabel("")
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        refresh_row = QHBoxLayout()
        self.refresh_button = QPushButton("刷新状态（保留编辑）")
        self.reload_button = QPushButton("重新载入配置")
        refresh_row.addWidget(self.refresh_button)
        refresh_row.addWidget(self.reload_button)
        refresh_row.addStretch()
        layout.addLayout(refresh_row)

        self.config_group = QGroupBox("显式人格配置")
        config = QFormLayout(self.config_group)
        self.enabled = QCheckBox("启用人格风格（默认关闭）")
        self.emotions_enabled = QCheckBox("启用模拟情绪")
        self.preset = QComboBox()
        for key, label in PRESETS.items():
            self.preset.addItem(label, key)
        self.custom_style = QCheckBox("本次保存使用以下自定义风格")
        config.addRow(self.enabled)
        config.addRow(self.emotions_enabled)
        config.addRow("人格预设", self.preset)
        config.addRow(self.custom_style)
        self.style_inputs: dict[str, QDoubleSpinBox] = {}
        for key, label in STYLE_AXES.items():
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 1.0)
            spin.setSingleStep(0.05)
            spin.setDecimals(2)
            spin.setEnabled(False)
            self.style_inputs[key] = spin
            config.addRow(label, spin)
            spin.valueChanged.connect(self._mark_dirty)
        self.configure_button = QPushButton("保存人格配置")
        config.addRow(self.configure_button)
        layout.addWidget(self.config_group)

        self.preference_group = QGroupBox("显式互动偏好")
        preference = QFormLayout(self.preference_group)
        self.scope = QComboBox()
        for scope_label, scope_value in (("本次会话", "session"), ("今天", "today"), ("持续保存", "persistent")):
            self.scope.addItem(scope_label, scope_value)
        self.verbosity = QComboBox()
        for verbosity_label, verbosity_value in (("本次不修改", ""), ("简短", "brief"), ("适中", "normal"), ("详细", "detailed")):
            self.verbosity.addItem(verbosity_label, verbosity_value)
        self.humor = QComboBox()
        humor_options: tuple[tuple[str, bool | None], ...] = (
            ("本次不修改", None), ("允许幽默", True), ("关闭幽默", False))
        for humor_label, humor_value in humor_options:
            self.humor.addItem(humor_label, humor_value)
        preference.addRow("有效范围", self.scope)
        preference.addRow("回复详略", self.verbosity)
        preference.addRow("幽默偏好", self.humor)
        self.preference_button = QPushButton("保存显式偏好")
        preference.addRow(self.preference_button)
        layout.addWidget(self.preference_group)

        self.cue_group = QGroupBox("短时互动模式（应用模拟）")
        cue = QHBoxLayout(self.cue_group)
        self.cue_seconds = QSpinBox()
        self.cue_seconds.setRange(1, 1800)
        self.cue_seconds.setValue(300)
        self.cue_seconds.setSuffix(" 秒")
        cue.addWidget(QLabel("持续时间"))
        cue.addWidget(self.cue_seconds)
        self.support_button = QPushButton("请求支持模式")
        self.focus_button = QPushButton("请求专注模式")
        self.clear_cue_button = QPushButton("清除短时模式")
        for button in (self.support_button, self.focus_button, self.clear_cue_button):
            cue.addWidget(button)
        layout.addWidget(self.cue_group)

        self.reset_group = QGroupBox("人格历史与重置")
        reset = QVBoxLayout(self.reset_group)
        rollback_row = QHBoxLayout()
        self.versions = QComboBox()
        self.rollback_button = QPushButton("回退到所选人格版本")
        rollback_row.addWidget(QLabel("已保留人格版本"))
        rollback_row.addWidget(self.versions)
        rollback_row.addWidget(self.rollback_button)
        reset.addLayout(rollback_row)
        reset_row = QHBoxLayout()
        self.affect_reset = QPushButton("重置模拟情绪")
        self.persona_reset = QPushButton("重置人格")
        self.all_reset = QPushButton("重置全部陪伴设置")
        for button in (self.affect_reset, self.persona_reset, self.all_reset):
            reset_row.addWidget(button)
        reset.addLayout(reset_row)
        layout.addWidget(self.reset_group)
        layout.addStretch()

        self.refresh_button.clicked.connect(self.poll)
        self.reload_button.clicked.connect(self._reload_inputs)
        self.enabled.toggled.connect(self._mark_dirty)
        self.emotions_enabled.toggled.connect(self._mark_dirty)
        self.preset.currentIndexChanged.connect(self._preset_changed)
        self.custom_style.toggled.connect(self._style_changed)
        self.configure_button.clicked.connect(self._configure)
        self.preference_button.clicked.connect(self._preference)
        self.support_button.clicked.connect(lambda: self._cue("supportive"))
        self.focus_button.clicked.connect(lambda: self._cue("focused"))
        self.clear_cue_button.clicked.connect(lambda: self._begin({"action": "cue", "mode": "neutral"}))
        self.rollback_button.clicked.connect(self._rollback)
        self.affect_reset.clicked.connect(lambda: self._begin({"action": "reset", "target": "affect"}))
        self.persona_reset.clicked.connect(lambda: self._begin({"action": "reset", "target": "persona"}))
        self.all_reset.clicked.connect(lambda: self._begin({"action": "reset", "target": "all"}))
        bridge.reply.connect(self._reply)
        self._update_enabled()

    def set_available(self, available: bool) -> None:
        if self._available and not available:
            self.clear_sensitive()
        self._available = available
        self._update_enabled()

    def _mark_dirty(self, *_: object) -> None:
        if not self._populating:
            self._config_dirty = True

    def _style_changed(self, checked: bool) -> None:
        self._mark_dirty()
        for spin in self.style_inputs.values():
            spin.setEnabled(checked)

    def _preset_changed(self, *_: object) -> None:
        self._mark_dirty()
        if not self._populating:
            self._preset_dirty = True

    def _reload_inputs(self) -> None:
        self._config_dirty = False
        self._preset_dirty = False
        self.poll()

    def _configure(self) -> None:
        parameters: dict[str, Any] = {"action": "configure", "enabled": self.enabled.isChecked(),
                                      "emotions_enabled": self.emotions_enabled.isChecked()}
        if self._preset_dirty:
            parameters["preset"] = self.preset.currentData()
        if self.custom_style.isChecked():
            parameters["style"] = {key: spin.value() for key, spin in self.style_inputs.items()}
        self._begin(parameters)

    def _preference(self) -> None:
        parameters: dict[str, Any] = {"action": "preference", "scope": self.scope.currentData()}
        if self.verbosity.currentData():
            parameters["verbosity"] = self.verbosity.currentData()
        if self.humor.currentData() is not None:
            parameters["humor_allowed"] = self.humor.currentData()
        if len(parameters) == 2:
            self.notice.setText("请选择至少一项要保存的显式偏好。")
            return
        self._begin(parameters)

    def _cue(self, mode: str) -> None:
        self._begin({"action": "cue", "mode": mode, "ttl_seconds": self.cue_seconds.value()})

    def _rollback(self) -> None:
        version = self.versions.currentData()
        if type(version) is int:
            self._begin({"action": "rollback", "persona_version": version})

    def _begin(self, parameters: dict[str, Any]) -> None:
        if not self._available or self._request is not None:
            return
        self._intent = parameters
        self.notice.setText("正在读取当前版本；收到新鲜快照后提交本次显式操作。")
        self._send("companion", {}, "preflight")

    def _send(self, operation: str, parameters: dict[str, Any], kind: str) -> None:
        self._generation += 1
        self._request_kind = kind
        self._request_at = monotonic()
        self._request_epoch = self.bridge.privacy_epoch
        try:
            self._request = str(self.bridge.send(operation, parameters, generation=self._generation))
        except (BufferError, RuntimeError, ValueError):
            self._request = None
            self._intent = None
            self._degrade("后台请求暂不可用；请稍后主动重试。")
        self._update_enabled()

    def poll(self) -> None:
        if not self._available:
            return
        now = monotonic()
        if self._snapshot is not None and now - self._received_at > FRESH_SECONDS:
            self._snapshot = None
            self.summary.setText("模拟状态已过期；正在读取当前状态。")
        if self._request is not None:
            if now - self._request_at > FRESH_SECONDS:
                self._intent = None
                self._degrade("正在等待后台回执，模拟状态暂不可用；请勿重复提交。")
            return
        self._send("companion", {}, "refresh")

    def _reply(self, envelope: Any) -> None:
        if not isinstance(envelope, dict) or envelope.get("request_id") != self._request:
            return
        kind, self._request = self._request_kind, None
        if (not self._available or self.bridge.state != "Ready"
                or envelope.get("generation") != self._generation
                or envelope.get("privacy_epoch") != self._request_epoch
                or self._request_epoch != self.bridge.privacy_epoch):
            self._intent = None
            self._update_enabled()
            return
        if not envelope.get("ok"):
            self._intent = None
            self._degrade("操作未确认或状态读取失败；请刷新当前版本后主动重试。")
            self._update_enabled()
            return
        if kind == "command":
            # Never replay a command, including one whose receipt arrived late.
            if self._command_action in {"configure", "reset", "rollback"}:
                self._config_dirty = False
                self._preset_dirty = False
            self.notice.setText("本次操作已受理；正在读取当前模拟状态。")
            self._send("companion", {}, "refresh")
            return
        if monotonic() - self._request_at > FRESH_SECONDS:
            self._intent = None
            self._degrade("回执已过期，本次操作未自动提交；请刷新后重试。")
            self._update_enabled()
            return
        try:
            snapshot = CompanionSnapshot.from_payload(envelope.get("data"))
            if self._authority is not None and not snapshot.follows(self._authority):
                raise ValueError("Companion state moved backwards")
        except (KeyError, TypeError, ValueError):
            self._intent = None
            self._degrade("模拟状态暂不可用；当前使用中性表现。请稍后刷新。")
            self._update_enabled()
            return
        self._snapshot = snapshot
        self._authority = snapshot
        self._received_at = monotonic()
        self._show(snapshot)
        if kind == "preflight" and self._intent is not None:
            if not snapshot.storage_available:
                self._intent = None
                self._degrade("陪伴存储暂不可用，本次操作未提交；请稍后刷新。")
                self._update_enabled()
                return
            intent, self._intent = self._intent, None
            intent["expected_revision"] = snapshot.revision
            self._command_action = str(intent["action"])
            self._send("companion_command", intent, "command")
            return
        self._update_enabled()

    def _show(self, snapshot: CompanionSnapshot) -> None:
        enabled = "已启用" if snapshot.enabled else "已关闭"
        emotions = "开启" if snapshot.emotions_enabled else "关闭"
        self.summary.setText(
            f"人格模拟{enabled} · 情绪模拟{emotions} · {PRESETS[snapshot.preset]} · 人格版本 {snapshot.persona_version}\n"
            f"模拟心境 {snapshot.mood:.2f} · 模式：{_MODES.get(snapshot.mode, '未知')} · "
            f"效价 {snapshot.valence:.2f} / 唤醒 {snapshot.arousal:.2f}\n"
            f"配置修订 {snapshot.revision} · 重置代际 {snapshot.reset_epoch}"
            + ("\n部分陪伴设置暂不可用；使用中性降级。" if snapshot.has_warnings else ""))
        if not self._config_dirty:
            self._populating = True
            try:
                self.enabled.setChecked(snapshot.enabled)
                self.emotions_enabled.setChecked(snapshot.emotions_enabled)
                self.preset.setCurrentIndex(self.preset.findData(snapshot.preset))
                self.custom_style.setChecked(False)
                for key, value in snapshot.style:
                    self.style_inputs[key].setValue(value)
            finally:
                self._populating = False
        selected = self.versions.currentData()
        self.versions.clear()
        for version in snapshot.persona_versions:
            if version != snapshot.persona_version:
                self.versions.addItem(str(version), version)
        index = self.versions.findData(selected)
        if index >= 0:
            self.versions.setCurrentIndex(index)

    def _degrade(self, message: str) -> None:
        self._snapshot = None
        self.summary.setText("模拟状态暂不可用；当前使用中性表现。")
        self.notice.setText(message)

    def _update_enabled(self) -> None:
        ready = self._available and self._request is None
        editing = self._available and (self._request is None or self._request_kind == "refresh")
        self.refresh_button.setEnabled(ready)
        self.reload_button.setEnabled(ready)
        for group in (self.config_group, self.preference_group, self.cue_group, self.reset_group):
            group.setEnabled(editing)
        for button in (self.configure_button, self.preference_button, self.support_button,
                       self.focus_button, self.clear_cue_button, self.affect_reset,
                       self.persona_reset, self.all_reset):
            button.setEnabled(ready)
        self.rollback_button.setEnabled(ready and self.versions.count() > 0)

    def clear_sensitive(self) -> None:
        self._generation += 1
        self._request = None
        self._intent = None
        self._snapshot = None
        self._received_at = 0.0
        self._authority = None
        self._command_action = ""
        self._populating = True
        try:
            self.enabled.setChecked(False)
            self.emotions_enabled.setChecked(False)
            self.preset.setCurrentIndex(0)
            self.custom_style.setChecked(False)
            for spin in self.style_inputs.values():
                spin.setValue(0.0)
            self.scope.setCurrentIndex(0)
            self.verbosity.setCurrentIndex(0)
            self.humor.setCurrentIndex(0)
            self.cue_seconds.setValue(300)
            self.versions.clear()
        finally:
            self._populating = False
        self._config_dirty = False
        self._preset_dirty = False
        self.summary.setText("模拟状态已清空；等待重新加载。")
        self.notice.clear()
        self._update_enabled()
