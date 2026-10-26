"""The sole settings center. Secrets remain in the independent maintenance UI."""
from typing import Any
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSlider, QSpinBox, QTabWidget, QTreeWidget, QVBoxLayout, QWidget
from .channel import Channel
from .persona_settings import PersonaSettings
from .interaction_state import InteractionSnapshot
from .views import CommandForm
from .widgets import fill_tree, scroll, text_label

class ImagePreview(QLabel):
    def __init__(self) -> None:
        super().__init__(); self.pixmap_value = QPixmap(); self.mode = "contain"; self.crop = [0., 0., 1., 1.]; self.setMinimumHeight(180); self.setMaximumHeight(320)
    def paintEvent(self, event: Any) -> None:
        if self.pixmap_value.isNull():
            super().paintEvent(event); return
        painter = QPainter(self); painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        target = QRectF(self.rect()).adjusted(8, 8, -8, -8)
        if self.mode in {"square", "circle"}:
            side = min(target.width(), target.height()); target = QRectF(target.center().x() - side / 2, target.center().y() - side / 2, side, side)
        if self.mode == "circle":
            path = QPainterPath(); path.addEllipse(target); painter.setClipPath(path)
        x, y, w, h = self.crop; image = self.pixmap_value
        source = QRectF(x * image.width(), y * image.height(), w * image.width(), h * image.height())
        if self.mode == "contain":
            scale = min(target.width() / source.width(), target.height() / source.height()); width = source.width() * scale; height = source.height() * scale
            target = QRectF(target.center().x() - width / 2, target.center().y() - height / 2, width, height)
        painter.drawPixmap(target, image, source)

class InteractionSettings(QWidget):
    def __init__(self, coordinator: Any, parent: Any = None) -> None:
        super().__init__(parent); self.coordinator = coordinator; self.channel = Channel(coordinator.bridge, self)
        self.snapshot: InteractionSnapshot | None = None; self.dirty = False; self.loading = False; self.pending = False
        self._identity_dirty = False; self._permissions_dirty = False
        layout = QVBoxLayout(self); self.notice = text_label(); layout.addWidget(self.notice); self.channel.notice.connect(self.notice.setText)
        refresh = QPushButton("读取当前设置（保留未保存编辑）"); refresh.clicked.connect(self.refresh); layout.addWidget(refresh)
        identity = QGroupBox("角色身份"); form = QFormLayout(identity); self.name = QLineEdit(); self.name.setMaxLength(40); form.addRow("显示名称", self.name)
        self.choices: dict[str, QComboBox] = {}
        enums = {"role_kind": ("角色", [("assistant", "助手"), ("companion", "陪伴伙伴")]), "self_reference": ("自称", [("neutral", "中性"), ("name", "使用名称"), ("first_person", "第一人称")]), "greeting_style": ("问候", [("none", "不主动问候"), ("brief", "简短问候")]), "followup_style": ("追问", [("none", "不主动追问"), ("optional_one", "可追问一次")]), "use_policy": ("身份使用范围", [("local_only", "仅本地"), ("cloud_allowed", "允许云处理")])}
        for key, (title, values) in enums.items():
            choice = QComboBox()
            for value, display in values: choice.addItem(display, value)
            self.choices[key] = choice; form.addRow(title, choice); choice.currentIndexChanged.connect(self._dirty)
        self.name.textEdited.connect(self._dirty); save = QPushButton("保存角色身份"); save.clicked.connect(self.save_identity); form.addRow(save); layout.addWidget(identity)
        cloud = QGroupBox("云模型类别许可"); box = QVBoxLayout(cloud); box.addWidget(text_label("每个类别单独允许；来源本身的仅本地限制仍然有效。")); self.permissions: dict[str, QCheckBox] = {}
        for key, title in [("custom_persona", "自定义人格与六轴"), ("preferences", "明确表达偏好"), ("cue", "历史情境线索"), ("affect", "应用模拟情绪"), ("history", "当前会话历史")]:
            check = QCheckBox(title); self.permissions[key] = check; box.addWidget(check); check.toggled.connect(self._permission_edited)
        save_cloud = QPushButton("保存类别许可"); save_cloud.clicked.connect(self.save_permissions); box.addWidget(save_cloud); layout.addWidget(cloud)
        override = QGroupBox("表达偏好与会话临时覆盖"); vbox = QVBoxLayout(override); self.conversation = QComboBox(); vbox.addWidget(self.conversation)
        self.preference = CommandForm("会话范围只影响下面选定会话；今天或持续保存为共享偏好。", [("scope", "生效范围", ["session", "today", "persistent"]), ("verbosity", "回复详略", ["brief", "normal", "detailed"]), ("humor_allowed", "幽默", ["true", "false"]), ("followup_allowed", "追问", ["true", "false"]), ("use_policy", "偏好使用范围", ["local_only", "cloud_allowed"])])
        self.preference.submitted.connect(self.save_preference); vbox.addWidget(self.preference)
        for action, title in [("clear_cue", "清除选定会话短时线索"), ("clear_constraints", "清除选定会话表达覆盖")]:
            button = QPushButton(title); button.clicked.connect(lambda checked=False, action=action: self.clear_scope(action)); vbox.addWidget(button)
        layout.addWidget(override)
        candidate = QGroupBox("偏好候选（确认后才保存）"); vbox = QVBoxLayout(candidate); self.candidates = QComboBox(); vbox.addWidget(self.candidates); self.candidate_text = text_label(); vbox.addWidget(self.candidate_text)
        self.candidates.currentIndexChanged.connect(lambda _: self.candidate_text.setText(self.candidates.currentText()))
        for action, title in [("confirm_candidate", "确认这条偏好"), ("reject_candidate", "拒绝这条候选")]:
            button = QPushButton(title); button.clicked.connect(lambda checked=False, action=action: self.candidate(action)); vbox.addWidget(button)
        layout.addWidget(candidate); coordinator.conversations_changed.connect(self._directory); self.conversation.currentIndexChanged.connect(lambda _: self._candidates())
        if hasattr(coordinator.bridge, "privacy_changed"): coordinator.bridge.privacy_changed.connect(self.clear_sensitive)

    def _dirty(self, *args: Any) -> None:
        if not self.loading: self.dirty = True; self._identity_dirty = True
    def _permission_edited(self, *args: Any) -> None:
        if not self.loading: self.dirty = True; self._permissions_dirty = True
    def _directory(self) -> None:
        selected = self.conversation.currentData(); self.conversation.blockSignals(True); self.conversation.clear()
        for identity, value in self.coordinator.conversations.items(): self.conversation.addItem(str(value.get("title", "会话")), identity)
        index = self.conversation.findData(selected or self.coordinator.default_id)
        if index >= 0: self.conversation.setCurrentIndex(index)
        self.conversation.blockSignals(False); self._candidates()
    def refresh(self) -> None:
        if not self.coordinator.available: return
        self.channel.send("interaction_snapshot", {}, self._snapshot)
    def _snapshot(self, data: dict[str, Any], *, accepted: bool = False) -> None:
        if "_error" in data: return
        try: value = InteractionSnapshot.decode(data, conversation_id=str(self.conversation.currentData() or ""))
        except (ValueError, TypeError, KeyError): self.notice.setText("互动设置暂不可用，请稍后读取。"); return
        if self.dirty and self.snapshot and self.snapshot.revision != value.revision and not accepted:
            self.notice.setText("后台设置已变化，未保存编辑已保留。保存可能冲突；重新载入可采用最新版本。"); return
        self.snapshot = value
        if not self.dirty: self._identity_dirty = False; self._permissions_dirty = False
        self.loading = True
        if not self._identity_dirty:
            self.name.setText(dict(value.identity).get("display_name", ""))
            for key, choice in self.choices.items():
                choice.setCurrentIndex(choice.findData(value.identity_use_policy if key == "use_policy" else dict(value.identity).get(key)))
        if not self._permissions_dirty:
            for key, enabled in value.permissions: self.permissions[key].setChecked(enabled)
        self.loading = False
        self._raw = data; self._candidates()
    def _candidates(self) -> None:
        self.candidates.clear()
        try:
            value = InteractionSnapshot.decode(getattr(self, "_raw", {}), conversation_id=str(self.conversation.currentData() or ""))
            for identity, description in value.candidates: self.candidates.addItem(description, identity)
        except (TypeError, ValueError, KeyError): pass
    def _write(self, action: str, params: dict[str, Any]) -> None:
        if self.snapshot is None or self.pending: self.notice.setText("请先读取当前互动设置。"); return
        self.pending = True
        def done(data: dict[str, Any]) -> None:
            self.pending = False
            if "_error" in data: self.notice.setText("保存未完成，编辑已保留。请读取当前版本后重试。"); return
            if action == "identity": self._identity_dirty = params != {"display_name": self.name.text(), **{key: choice.currentData() for key, choice in self.choices.items()}}
            elif action == "cloud_permissions": self._permissions_dirty = params.get("permissions") != {key: box.isChecked() for key, box in self.permissions.items()}
            elif action == "preference": self.preference.mark_clean()
            self.dirty = self._identity_dirty or self._permissions_dirty
            self._snapshot(data, accepted=True); self.notice.setText("设置已保存。")
        if self.channel.send("interaction_command", {"action": action, "expected_revision": self.snapshot.revision, **params}, done, mutation=True) is None: self.pending = False
    def save_identity(self) -> None:
        self._write("identity", {"display_name": self.name.text(), **{key: choice.currentData() for key, choice in self.choices.items()}})
    def save_permissions(self) -> None:
        self._write("cloud_permissions", {"permissions": {key: box.isChecked() for key, box in self.permissions.items()}})
    def save_preference(self, values: dict[str, Any]) -> None:
        self._write("preference", {**values, "humor_allowed": values["humor_allowed"] == "true", "followup_allowed": values["followup_allowed"] == "true", "conversation_id": self.conversation.currentData()})
    def clear_scope(self, action: str) -> None:
        self._write(action, {"conversation_id": self.conversation.currentData(), **({"scope": "session"} if action == "clear_constraints" else {})})
    def candidate(self, action: str) -> None:
        if self.candidates.currentData(): self._write(action, {"candidate_id": self.candidates.currentData()})
    def clear_sensitive(self, epoch: int = 0) -> None:
        self.channel.invalidate()
        self.loading = True
        for check in self.permissions.values(): check.setChecked(False)
        self.loading = False
        self.snapshot = None; self.dirty = False; self._identity_dirty = False; self._permissions_dirty = False; self.pending = False; self.name.clear(); self.candidates.clear(); self.candidate_text.clear(); self._raw = {}; self.preference.clear_sensitive()

class AppearanceSettings(QWidget):
    def __init__(self, coordinator: Any, preferences: Any) -> None:
        super().__init__(); self.coordinator = coordinator; self.controller = coordinator.appearance; self.preferences = preferences; self.channel = Channel(coordinator.bridge, self)
        self.generation = 0; self.token = ""; self.preview_request = ""; self._busy = False
        layout = QVBoxLayout(self); self.notice = text_label("PNG/JPEG 静态图片，最大 5 MiB、单边 2048 像素。图片仅在本机处理。"); layout.addWidget(self.notice); self.channel.notice.connect(self.notice.setText)
        self.toggles: dict[str, QCheckBox] = {}
        for key, title in [("pet_enabled", "启用桌宠"), ("pet_visible", "显示桌宠"), ("pet_on_top", "桌宠置顶"), ("pet_click_through", "桌宠鼠标穿透（可回到这里关闭）")]:
            check = QCheckBox(title); check.setChecked(bool(getattr(preferences, key, False))); check.toggled.connect(lambda value, key=key: self._pref(key, value)); layout.addWidget(check); self.toggles[key] = check
        scale_row = QHBoxLayout(); self.scale = QSlider(Qt.Orientation.Horizontal); self.scale.setRange(60, 160); self.scale.setValue(round(float(getattr(preferences, "pet_scale", 1.0)) * 100)); self.scale_label = text_label(f"{self.scale.value()}%")
        self.scale.valueChanged.connect(lambda value: self.scale_label.setText(f"{value}%")); self.scale.sliderReleased.connect(lambda: self._pref("pet_scale", self.scale.value() / 100)); self.scale.actionTriggered.connect(lambda _: self._scale_keyboard())
        scale_row.addWidget(text_label("桌宠大小")); scale_row.addWidget(self.scale, 1); scale_row.addWidget(self.scale_label); layout.addLayout(scale_row)
        presets = QHBoxLayout()
        for scale_percent in [80, 100, 120, 140]:
            button = QPushButton(f"{scale_percent}%"); button.clicked.connect(lambda checked=False, value=scale_percent: self._scale(value)); presets.addWidget(button)
        reset_position = QPushButton("恢复大小与位置"); reset_position.clicked.connect(self._reset_position); presets.addWidget(reset_position); layout.addLayout(presets)
        self.slot = QComboBox()
        for key, title in [("user_avatar", "用户头像"), ("character_avatar", "角色聊天头像"), ("pet_main", "桌宠主形象"), ("pet_thinking", "思考表情"), ("pet_focused", "专注表情"), ("pet_pleased", "愉悦表情"), ("pet_alert", "提示表情")]: self.slot.addItem(title, key)
        layout.addWidget(self.slot); self.preview = ImagePreview(); self.preview.setText("选择图片以预览显示区域"); layout.addWidget(self.preview)
        self.mode = QComboBox()
        for mode_key, title in [("contain", "完整显示，保持比例"), ("square", "方形头像"), ("circle", "圆形头像")]: self.mode.addItem(title, mode_key)
        layout.addWidget(self.mode); self.mode.currentIndexChanged.connect(self._preview_update)
        crop_box = QGroupBox("显示区域（0–100%，不会修改源文件）"); form = QFormLayout(crop_box); self.crop: list[QDoubleSpinBox] = []
        for title, value in [("左侧", 0), ("顶部", 0), ("宽度", 100), ("高度", 100)]:
            spin = QDoubleSpinBox(); spin.setRange(0, 100); spin.setValue(value); spin.setSuffix(" %"); spin.valueChanged.connect(self._preview_update); self.crop.append(spin); form.addRow(title, spin)
        layout.addWidget(crop_box)
        row = QHBoxLayout(); self.choose = QPushButton("选择本地图片"); self.apply = QPushButton("保存应用"); self.reset = QPushButton("此槽位恢复默认"); discard = QPushButton("丢弃本次预览")
        for button in [self.choose, self.apply, self.reset, discard]: row.addWidget(button)
        layout.addLayout(row); self.choose.clicked.connect(self._choose); self.apply.clicked.connect(self._apply); self.reset.clicked.connect(self._reset); discard.clicked.connect(self._discard); self.slot.currentIndexChanged.connect(self._slot)
        collect = QPushButton("回收未使用的托管图片"); collect.clicked.connect(self._collect); layout.addWidget(collect)
        self.controller.changed.connect(self._metadata_changed); coordinator.bridge.image_reply.connect(self._image)
        if preferences is not None and hasattr(preferences, "changed"): preferences.changed.connect(self._preference_changed)
        if hasattr(coordinator.bridge, "privacy_changed"): coordinator.bridge.privacy_changed.connect(self._clear)
        self._profiles()
    def _pref(self, key: str, value: Any) -> None:
        setter = getattr(self.preferences, "set_" + key, None)
        if setter is not None and setter(value) is False: self.notice.setText("偏好保存失败，请重试。"); self._preference_changed(key, getattr(self.preferences, key, value))
    def _scale(self, value: int) -> None:
        self.scale.setValue(value); self._pref("pet_scale", value / 100)
    def _scale_keyboard(self) -> None:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self, lambda: self._pref("pet_scale", self.scale.value() / 100))
    def _reset_position(self) -> None:
        self._scale(100)
        setter = getattr(self.preferences, "set_pet_position", None)
        if setter is not None: setter((80, 80), "")
    def _preference_changed(self, key: str, value: Any) -> None:
        widget = self.toggles.get(key)
        if widget: widget.blockSignals(True); widget.setChecked(bool(value)); widget.blockSignals(False)
        if key == "pet_scale": self.scale.blockSignals(True); self.scale.setValue(round(float(value) * 100)); self.scale.blockSignals(False); self.scale_label.setText(f"{self.scale.value()}%")
    def _profiles(self) -> None:
        values = [spin.value() / 100 for spin in self.crop]
        valid = values[2] > 0 and values[3] > 0 and values[0] + values[2] <= 1 and values[1] + values[3] <= 1
        self.apply.setEnabled(bool(self.token) and not self._busy and valid); self.reset.setEnabled(self.slot.currentData() in self.controller.profiles and not self._busy)
        self.slot.setEnabled(not self._busy); self.choose.setEnabled(not self._busy)
        self.mode.setEnabled(not self._busy and str(self.slot.currentData()).endswith("avatar"))
        for spin in self.crop: spin.setEnabled(not self._busy)
    def _metadata_changed(self) -> None:
        self._profiles()
        if not self.token and not self._busy: self._slot(self.slot.currentIndex())
    def _slot(self, index: int) -> None:
        self._discard(); profile = self.controller.profiles.get(str(self.slot.currentData()), {})
        avatar = str(self.slot.currentData()).endswith("avatar"); self.mode.setEnabled(avatar); self.mode.setCurrentIndex(self.mode.findData(profile.get("display_mode", "contain")) if avatar else 0)
        for spin, value in zip(self.crop, profile.get("crop", [0, 0, 1, 1])): spin.setValue(float(value) * 100)
        asset_id = profile.get("effective_asset_id")
        if asset_id: self._request_preview({"asset_id": asset_id})
    def _choose(self) -> None:
        if self._busy: return
        path, _ = QFileDialog.getOpenFileName(self, "选择静态图片", "", "图片 (*.png *.jpg *.jpeg)")
        if not path: return
        self._discard(); self.generation += 1; generation = self.generation; slot = self.slot.currentData()
        def done(data: dict[str, Any]) -> None:
            if "_error" in data: return
            token = str(data.get("token", ""))
            if generation != self.generation or slot != self.slot.currentData():
                if token: self.channel.send("asset_import_discard", {"token": token})
                return
            self.token = token; self._request_preview({"token": token}); self._profiles()
        self.channel.send("asset_import_prepare", {"path": path, "slot": slot, "generation": generation}, done)
    def _request_preview(self, params: dict[str, Any]) -> None:
        try: self.preview_request = str(self.coordinator.bridge.send_image("asset_preview", {**params, "max_edge": 480}, self.generation))
        except (ValueError, RuntimeError, BufferError): self.notice.setText("图片预览通道暂忙，请重新选择。")
    def _image(self, envelope: Any) -> None:
        if not isinstance(envelope, dict) or envelope.get("request_id") != self.preview_request: return
        if envelope.get("generation") != self.generation or envelope.get("privacy_epoch") != int(getattr(self.coordinator.bridge, "privacy_epoch", 0)): return
        if not envelope.get("ok"): self.notice.setText(str(envelope.get("error", {}).get("message", "无法预览"))); return
        pixmap = QPixmap(); png = envelope.get("image", {}).get("png")
        if isinstance(png, bytes) and pixmap.loadFromData(png, "PNG"): self.preview.pixmap_value = pixmap; self._preview_update()
    def _preview_update(self, *args: Any) -> None:
        self.preview.mode = str(self.mode.currentData()); values = [spin.value() / 100 for spin in self.crop]
        if values[2] <= 0 or values[3] <= 0 or values[0] + values[2] > 1.00001 or values[1] + values[3] > 1.00001:
            self.notice.setText("显示区域必须有宽高，且不能超出图片边界。"); self.apply.setEnabled(False); return
        self.preview.crop = values; self.preview.update(); self._profiles()
    def _apply(self) -> None:
        profile = self.controller.profiles.get(str(self.slot.currentData()), {})
        if not self.token or not profile or self._busy: return
        crop = [spin.value() / 100 for spin in self.crop]
        if crop[2] <= 0 or crop[3] <= 0 or crop[0] + crop[2] > 1 or crop[1] + crop[3] > 1: self.notice.setText("显示区域超出图片，请先调整。"); return
        self._busy = True; self._profiles()
        if self.channel.send("appearance_apply", {"slot": self.slot.currentData(), "token": self.token, "expected_revision": profile["revision"], "generation": self.generation, "display_mode": self.mode.currentData(), "crop": crop}, self._saved) is None:
            self._busy = False; self._profiles()
    def _saved(self, data: dict[str, Any]) -> None:
        self._busy = False
        if "_error" not in data: self._discard(); self.notice.setText("外观已保存。"); self.controller.refresh()
        else: self.notice.setText("外观未保存，当前预览已保留。请刷新后重试。"); self.controller.refresh()
        self._profiles()
    def _reset(self) -> None:
        profile = self.controller.profiles.get(str(self.slot.currentData()), {})
        if profile and not self._busy:
            self._busy = True; self._profiles()
            if self.channel.send("appearance_reset", {"slot": self.slot.currentData(), "expected_revision": profile["revision"]}, self._saved) is None:
                self._busy = False; self._profiles()
    def _discard(self) -> None:
        if self._busy: return
        if self.token: self.channel.send("asset_import_discard", {"token": self.token})
        self.token = ""; self.preview_request = ""; self.generation += 1; self.preview.pixmap_value = QPixmap(); self.preview.update(); self._profiles()
    def _collect(self) -> None:
        self.channel.send("asset_collect", {"limit": 32}, lambda data: self.notice.setText("未使用素材已回收；旧备份副本不受影响。" if "_error" not in data else "素材回收未完成。"))
    def _clear(self, epoch: int = 0) -> None:
        self.token = ""; self.preview_request = ""; self.generation += 1; self.preview.pixmap_value = QPixmap(); self.preview.update(); self._busy = False; self._profiles()

class SettingsPage(QWidget):
    def __init__(self, coordinator: Any, preferences: Any, maintenance: Any, parent: Any = None) -> None:
        super().__init__(parent); self.coordinator = coordinator; self.preferences = preferences; self.channel = Channel(coordinator.bridge, self); self.maintenance = maintenance
        layout = QVBoxLayout(self); layout.addWidget(text_label("设置", heading=True)); self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        self.tabs.setUsesScrollButtons(True)
        persona = QWidget(); vbox = QVBoxLayout(persona); self.interaction = InteractionSettings(coordinator); vbox.addWidget(self.interaction)
        self.companion = PersonaSettings(coordinator.bridge); self.companion.conversation_target = lambda: self.interaction.conversation.currentData(); vbox.addWidget(self.companion)
        reload = QPushButton("放弃本区未保存编辑并重新读取"); reload.clicked.connect(self._reload_persona); vbox.addWidget(reload)
        self.tabs.addTab(scroll(persona), "人格与表达")
        self.appearance = AppearanceSettings(coordinator, preferences); self.tabs.addTab(scroll(self.appearance), "桌宠与外观")
        notification = QWidget(); vbox = QVBoxLayout(notification); vbox.addWidget(text_label("通知仅显示通用状态，提醒详情需主动打开查看。"))
        self.notification_controls: dict[str, QCheckBox] = {}
        for key, title in [("notifications_enabled", "启用系统通知"), ("close_to_tray", "关闭主窗口后留在托盘")]:
            check = QCheckBox(title); check.setChecked(bool(getattr(preferences, key, False))); check.toggled.connect(lambda value, key=key: self.appearance._pref(key, value)); self.notification_controls[key] = check; vbox.addWidget(check)
        row = QFormLayout(); self.quiet_start = QSpinBox(); self.quiet_end = QSpinBox()
        for key, spin, title, default in [("quiet_start", self.quiet_start, "免打扰开始小时", 22), ("quiet_end", self.quiet_end, "结束小时", 8)]: spin.setRange(0, 23); spin.setValue(int(getattr(preferences, key, default))); row.addRow(title, spin); spin.valueChanged.connect(self._quiet)
        vbox.addLayout(row); vbox.addStretch(); self.tabs.addTab(scroll(notification), "通知与打扰")
        privacy = QWidget(); vbox = QVBoxLayout(privacy); vbox.addWidget(text_label("清空聊天与遗忘长期记忆是独立操作。清空默认聊天保留固定入口。运行中的外部效果不能撤回，旧备份与云副本需另行处理。"))
        self.clear_target = QComboBox(); vbox.addWidget(self.clear_target); clear = QPushButton("清空选定会话记录（保留入口与独立长期记忆）"); clear.clicked.connect(self._clear_conversation); vbox.addWidget(clear)
        self.memory_info = QTreeWidget(); self.memory_info.setHeaderLabels(["记忆与隐私配置", "当前值"]); vbox.addWidget(self.memory_info)
        configure_privacy = QPushButton("进入维护调整记忆与隐私配置"); configure_privacy.clicked.connect(maintenance); vbox.addWidget(configure_privacy); self.tabs.addTab(scroll(privacy), "记忆与隐私")
        model = QWidget(); vbox = QVBoxLayout(model); vbox.addWidget(text_label("连接和密钥在独立维护窗口中设置；生产服务关闭后保存，重新打开生产服务才生效。连接检查需在维护窗口单独确认。")); self.model_info = QTreeWidget(); self.model_info.setHeaderLabels(["连接", "当前生效值"]); vbox.addWidget(self.model_info)
        open_model = QPushButton("配置模型连接与密钥"); open_model.setObjectName("primary"); open_model.clicked.connect(maintenance); vbox.addWidget(open_model); self.tabs.addTab(scroll(model), "模型连接")
        tools = QWidget(); vbox = QVBoxLayout(tools); vbox.addWidget(text_label("能力提供方及真实可配置操作。当前只读能力没有启用或安装开关。")); self.plugin_info = QTreeWidget(); self.plugin_info.setHeaderLabels(["提供方与配置能力", "详情"]); vbox.addWidget(self.plugin_info); self.tabs.addTab(scroll(tools), "工具与插件")
        advanced = QWidget(); vbox = QVBoxLayout(advanced); maintenance_button = QPushButton("打开配置、备份与数据维护"); maintenance_button.clicked.connect(maintenance); vbox.addWidget(maintenance_button)
        self.diagnostics = QTreeWidget(); self.diagnostics.setHeaderLabels(["诊断", "内容"]); vbox.addWidget(self.diagnostics)
        diagnostic = QPushButton("读取诊断"); diagnostic.clicked.connect(self.read_diagnostics); vbox.addWidget(diagnostic)
        cleanup = QPushButton("重试未完成的隐私清理"); cleanup.clicked.connect(self.retry_cleanup); vbox.addWidget(cleanup); self.tabs.addTab(scroll(advanced), "数据与高级")
        self.cleanup_state = text_label(); vbox.addWidget(self.cleanup_state)
        cleanup_status = QPushButton("查看隐私清理进度"); cleanup_status.clicked.connect(self.read_cleanup_status); vbox.addWidget(cleanup_status)
        coordinator.conversations_changed.connect(self._directory)
        if preferences is not None and hasattr(preferences, "changed"): preferences.changed.connect(self._preference_changed)
        if hasattr(coordinator.bridge, "privacy_changed"): coordinator.bridge.privacy_changed.connect(self._privacy)

    def _reload_persona(self) -> None:
        self.interaction.dirty = False; self.interaction.refresh(); self.companion._reload_inputs()
    def _directory(self) -> None:
        selected = self.clear_target.currentData(); self.clear_target.clear()
        for identity, item in self.coordinator.conversations.items(): self.clear_target.addItem(str(item.get("title", "会话")), identity)
        index = self.clear_target.findData(selected or self.coordinator.default_id)
        if index >= 0: self.clear_target.setCurrentIndex(index)
    def _quiet(self, *args: Any) -> None:
        setter = getattr(self.preferences, "set_quiet_hours", None)
        if setter is not None: setter(self.quiet_start.value(), self.quiet_end.value())
    def _preference_changed(self, key: str, value: Any) -> None:
        widget = self.notification_controls.get(key) or {"quiet_start": self.quiet_start, "quiet_end": self.quiet_end}.get(key)
        if widget:
            widget.blockSignals(True)
            if isinstance(widget, QCheckBox): widget.setChecked(bool(value))
            else: widget.setValue(int(value))
            widget.blockSignals(False)
    def _clear_conversation(self) -> None:
        identity = str(self.clear_target.currentData() or "")
        if identity and QMessageBox.question(self, "清空聊天记录", "清空这个会话及其相关任务正文？独立长期记忆保留，固定日常聊天入口保留。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes: self.coordinator.remove_conversation(identity)
    def refresh(self) -> None:
        self.channel.send("settings", {}, self._settings); self.interaction.refresh(); self.companion.set_available(self.coordinator.available); self.companion.poll(); self.coordinator.appearance.refresh()
        def plugins(data: dict[str, Any]) -> None:
            if "_error" not in data: fill_tree(self.plugin_info, data)
        self.channel.send("plugins_catalog", {}, plugins)
    def _settings(self, data: dict[str, Any]) -> None:
        if "_error" in data: return
        fill_tree(self.model_info, {key: data.get(key) for key in ["execution_location", "provider", "model", "protocol", "llm_ready", "credential_mode", "credential_source", "active_revision", "request_use_default"]})
        fill_tree(self.memory_info, {key: data.get(key) for key in ["memory_mode", "embedding_provider", "workspace_dir", "data_dir"]})
    def read_diagnostics(self) -> None:
        def done(data: dict[str, Any]) -> None:
            fill_tree(self.diagnostics, data)
        self.channel.send("diagnostics", {}, done)
    def read_cleanup_status(self) -> None:
        self.channel.send("cleanup_status", {}, self._cleanup_status)
    def retry_cleanup(self) -> None:
        def done(data: dict[str, Any]) -> None:
            if "_error" in data: self.cleanup_state.setText("重试未完成，请查看当前清理进度。"); return
            self.read_cleanup_status(); self.read_diagnostics()
        self.channel.send("retry_cleanup", {}, done)
    def _cleanup_status(self, data: dict[str, Any]) -> None:
        if "_error" in data:
            self.cleanup_state.setText("清理进度读取失败，请重新查看；不表示清理完成。"); return
        pending = data.get("status") == "Pending" or bool(data.get("blocked"))
        self.cleanup_state.setText(("相关正文已遮蔽，仍有清理工作等待完成。" if pending else "隐私清理已完成。") + (" 正在等待运行中的工作安全结束。" if data.get("waiting_for_worker") else ""))
        fill_tree(self.diagnostics, data)
    def _privacy(self, epoch: int = 0) -> None:
        self.model_info.clear(); self.memory_info.clear(); self.plugin_info.clear(); self.diagnostics.clear(); self.cleanup_state.clear(); self.companion.clear_sensitive()
