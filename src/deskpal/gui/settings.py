"""The sole settings center. Secrets remain in the independent maintenance UI."""
from typing import Any
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPalette, QPixmap
from PySide6.QtWidgets import (QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QSizePolicy, QVBoxLayout, QWidget)
from .components import (ChoiceBox as QComboBox, PushButton as QPushButton, CheckBox as QCheckBox, LineEdit as QLineEdit, PlainTextEdit as QPlainTextEdit, TreeWidget as QTreeWidget, ScrollArea as QScrollArea, SpinBox as QSpinBox, DoubleSpinBox as QDoubleSpinBox, Slider as QSlider, ToolButton as QToolButton, Tabs as QTabWidget)
from .channel import Channel
from .studio import SettingsSections, page_layout, group_setting_rows
from .views import CommandForm
from .widgets import ArrowComboBox, fill_tree, notice_label, scroll, text_label
from .display_text import compact_path
from qfluentwidgets import qconfig
from .components import ActionFlowLayout as FlowLayout


def _style_numeric_field(spin: Any) -> None:
    """Keep Fluent spin-box values readable in both themes and align them as controls."""
    from qfluentwidgets import isDarkTheme
    color = QPalette.ColorRole.Text
    palette = spin.palette()
    text_color = "#f1f3f1" if isDarkTheme() else "#202522"
    palette.setColor(color, QColor(text_color))
    spin.setPalette(palette)
    spin.setMinimumWidth(150); spin.setMaximumWidth(210)
    spin.setAlignment(Qt.AlignmentFlag.AlignLeft)
    field = spin.lineEdit()
    if field is not None:
        field_palette = field.palette(); field_palette.setColor(QPalette.ColorRole.Text, palette.color(color))
        field.setPalette(field_palette); field.setAlignment(Qt.AlignmentFlag.AlignLeft)
        field.setStyleSheet(f"color:{text_color}; background:transparent;")


def _summary(text: str = "尚未读取当前设置。") -> QLabel:
    value = text_label(text)
    value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
    return value


def _details(layout: QVBoxLayout, tree: QTreeWidget, title: str = "查看技术详情") -> None:
    """Keep compatibility trees available, but out of the normal reading flow."""
    toggle = QToolButton()
    toggle.setText(title)
    toggle.setCheckable(True)
    panel = QWidget()
    content = QVBoxLayout(panel)
    content.setContentsMargins(0, 0, 0, 0)
    tree.setMinimumHeight(180)
    tree.setMaximumHeight(320)
    tree.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    content.addWidget(tree)
    copy = QPushButton("复制所选完整值（未选时复制全部）")
    def copy_values() -> None:
        roots = (tree.topLevelItem(i) for i in range(tree.topLevelItemCount()))
        selected = tree.selectedItems() or [item for item in roots if item is not None]
        lines: list[str] = []
        def visit(item: Any) -> None:
            raw = item.data(1, Qt.ItemDataRole.UserRole)
            lines.append(item.text(0) + "：" + (str(raw) if raw is not None else item.text(1)))
            for index in range(item.childCount()): visit(item.child(index))
        for item in selected: visit(item)
        QGuiApplication.clipboard().setText("\n".join(lines))
    copy.clicked.connect(copy_values)
    content.addWidget(copy)
    panel.hide()
    toggle.toggled.connect(panel.setVisible)
    layout.addWidget(toggle)
    layout.addWidget(panel)


def _credential(source: Any) -> str:
    return {"system_vault": "Windows 系统凭据库", "environment": "环境变量", "dotenv": "本地环境文件",
            "none": "无需凭据", "missing": "尚未提供凭据"}.get(str(source), "来源待确认")

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
    """唯一角色卡编辑入口，保留编辑、版本冲突与生命周期屏障。"""
    def __init__(self, coordinator: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self.coordinator = coordinator
        self.channel = Channel(coordinator.bridge, self)
        self.snapshot: dict[str, Any] | None = None
        self.dirty = False
        self.pending = False
        self.loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24); layout.setSpacing(16)
        layout.addWidget(text_label("角色卡", heading=True))
        self.card = QPlainTextEdit()
        self.card.setPlaceholderText("例如：沉稳、好奇，喜欢先了解实际条件；熟悉之后偶尔开玩笑。")
        self.card.setMinimumHeight(240)
        self.card.textChanged.connect(self._edited)
        layout.addWidget(self.card)
        self.notice = notice_label()
        layout.addWidget(self.notice)
        row = QHBoxLayout()
        self.save_button = QPushButton("保存角色卡")
        self.save_button.clicked.connect(self.save)
        refresh = QPushButton("重新读取")
        refresh.clicked.connect(self._reload_inputs)
        row.addStretch(1); row.addWidget(self.save_button); row.addWidget(refresh)
        layout.addLayout(row)
        habits_box = QGroupBox('逐渐形成的相处方式'); habits_layout = QVBoxLayout(habits_box)
        self.habits_view = text_label('还没有形成长期相处习惯。'); habits_layout.addWidget(self.habits_view); layout.addWidget(habits_box)
        emotion_box = QGroupBox('当前情绪'); emotion_layout = QVBoxLayout(emotion_box)
        self.emotion_view = text_label('目前没有需要保留的情绪状态。'); emotion_layout.addWidget(self.emotion_view); layout.addWidget(emotion_box)
        if hasattr(coordinator.bridge, "privacy_changed"):
            coordinator.bridge.privacy_changed.connect(self.clear_sensitive)

    @property
    def _config_dirty(self) -> bool:
        return self.dirty

    def _edited(self) -> None:
        if not self.loading: self.dirty = True

    def set_available(self, available: bool) -> None:
        self.save_button.setEnabled(available and not self.pending)

    def poll(self) -> None:
        if self.snapshot is None and not self.pending: self.refresh()

    def refresh(self) -> None:
        if not self.coordinator.available or self.pending: return
        self.channel.send("interaction_snapshot", {}, self._snapshot)

    def _snapshot(self, data: dict[str, Any]) -> None:
        if "_error" in data: return
        card = data.get("character_card")
        if data.get("schema_version") != 2 or not isinstance(card, dict) or not isinstance(card.get("text"), str):
            self.notice.setText("角色资料读取失败，请稍后重试。")
            return
        self.snapshot = data
        habits = data.get('habits', [])
        self.habits_view.setText('\n'.join('• ' + str(item.get('content', '')) for item in habits if isinstance(item, dict)) or '还没有形成长期相处习惯。')
        emotion = data.get('current_emotion')
        self.emotion_view.setText('\n'.join(title + '：' + str(emotion[key]) for key, title in (
            ('description_intensity', '感受'), ('reason_sources', '原因'),
            ('time_scope', '持续范围'), ('uncertainty', '仍不确定')) if emotion.get(key))
            if isinstance(emotion, dict) else '目前没有需要保留的情绪状态。')
        if not self.dirty:
            self.loading = True
            self.card.setPlainText(card["text"])
            self.loading = False
        self.notice.setText("旧角色卡保留仅本地限制。查看后重新保存，即按当前模型连接使用。" if card.get("use_policy") == "local_only" else "角色资料已读取。")

    def save(self) -> None:
        if self.snapshot is None or self.pending: return
        text = self.card.toPlainText().strip()
        if not text or len(text.encode("utf-8")) > 8192:
            self.notice.setText("请填写角色卡，内容最多约 8 KiB。")
            return
        self.pending = True
        self.save_button.setEnabled(False)
        def done(data: dict[str, Any]) -> None:
            self.pending = False
            self.set_available(self.coordinator.available)
            if "_error" in data:
                self.notice.setText("保存未完成，编辑已保留；请重新读取当前版本后重试。")
                return
            self.dirty = self.card.toPlainText().strip() != text
            self._snapshot(data)
            self.notice.setText("角色卡已保存。")
        sent = self.channel.send("interaction_command", {"action": "character_card", "text": text,
            "expected_revision": self.snapshot["revision"]}, done, mutation=True)
        if sent is None:
            self.pending = False
            self.set_available(self.coordinator.available)

    def _reload_inputs(self) -> None:
        self.dirty = False
        self.refresh()

    def clear_sensitive(self, epoch: int = 0) -> None:
        self.channel.invalidate()
        self.snapshot = None; self.pending = False
        self.loading = True; self.card.clear(); self.loading = False
        self.dirty = False; self.notice.clear()
        self.habits_view.clear(); self.emotion_view.clear()

class AppearanceSettings(QWidget):
    def __init__(self, coordinator: Any, preferences: Any, *, avatars: bool = False) -> None:
        super().__init__(); self.avatars = avatars; self.coordinator = coordinator; self.controller = coordinator.appearance; self.preferences = preferences; self.channel = Channel(coordinator.bridge, self)
        self.generation = 0; self.token = ""; self.preview_request = ""; self._busy = False
        layout = QVBoxLayout(self); layout.setContentsMargins(24, 24, 24, 24); layout.setSpacing(16)
        self.notice = notice_label(); layout.addWidget(self.notice); self.channel.notice.connect(self.notice.setText)
        if not avatars:
            self.theme_choice = ArrowComboBox()
            for title, value in [('跟随系统', 'auto'), ('浅色', 'light'), ('深色', 'dark')]: self.theme_choice.addItem(title, value)
            self.theme_choice.setCurrentIndex(max(0, self.theme_choice.findData(getattr(preferences, 'theme', 'auto'))))
            def theme_changed(index: int) -> None:
                from .theme import select
                value = str(self.theme_choice.currentData())
                self._pref('theme', value); select(value)
            self.theme_choice.currentIndexChanged.connect(theme_changed)
            self.theme_choice.setMinimumWidth(120); self.theme_choice.setMaximumWidth(240)
            theme_row = QHBoxLayout(); theme_row.addWidget(text_label('应用主题')); theme_row.addStretch(1); theme_row.addWidget(self.theme_choice); layout.addLayout(theme_row)
        if avatars:
            self.user_display_name = QLineEdit(getattr(preferences, "user_display_name", "你")); self.user_display_name.setMaxLength(32)
            self.character_display_name = QLineEdit(getattr(preferences, "character_display_name", "Deskpal")); self.character_display_name.setMaxLength(32)
            self.user_display_name.setPlaceholderText("你")
            self.character_display_name.setPlaceholderText("Deskpal")
            for field in (self.user_display_name, self.character_display_name): field.setMaximumWidth(320)
            user_row = QHBoxLayout(); user_row.addWidget(text_label("聊天中的你的称呼")); user_row.addStretch(1); user_row.addWidget(self.user_display_name); layout.addLayout(user_row)
            character_row = QHBoxLayout(); character_row.addWidget(text_label("聊天中的角色称呼")); character_row.addStretch(1); character_row.addWidget(self.character_display_name); layout.addLayout(character_row)
            self.user_display_name.editingFinished.connect(lambda: self._pref("user_display_name", self.user_display_name.text()))
            self.character_display_name.editingFinished.connect(lambda: self._pref("character_display_name", self.character_display_name.text()))
        self.toggles: dict[str, QCheckBox] = {}
        if not avatars:
            for key, title in [("pet_enabled", "启用桌宠"), ("pet_visible", "显示桌宠"), ("pet_on_top", "桌宠置顶"), ("pet_click_through", "桌宠鼠标穿透（可回到这里关闭）")]:
                row = QHBoxLayout(); row.addWidget(text_label(title)); row.addStretch(1)
                check = QCheckBox(); check.setAccessibleName(title); check.setChecked(bool(getattr(preferences, key, False))); check.toggled.connect(lambda value, key=key: self._pref(key, value)); row.addWidget(check); layout.addLayout(row); self.toggles[key] = check
            scale_row = QHBoxLayout(); self.scale = QSlider(Qt.Orientation.Horizontal); self.scale.setRange(60, 160); self.scale.setValue(round(float(getattr(preferences, "pet_scale", 1.0)) * 100)); self.scale_label = text_label(f"{self.scale.value()}%")
            self.scale.valueChanged.connect(lambda value: self.scale_label.setText(f"{value}%")); self.scale.sliderReleased.connect(lambda: self._pref("pet_scale", self.scale.value() / 100)); self.scale.actionTriggered.connect(lambda _: self._scale_keyboard())
            scale_row.addWidget(text_label("桌宠大小")); scale_row.addWidget(self.scale, 1); scale_row.addWidget(self.scale_label); layout.addLayout(scale_row)
            presets = FlowLayout()
            for scale_percent in [80, 100, 120, 140]:
                button = QPushButton(f"{scale_percent}%"); button.clicked.connect(lambda checked=False, value=scale_percent: self._scale(value)); presets.addWidget(button)
            reset_position = QPushButton("恢复大小与位置"); reset_position.clicked.connect(self._reset_position); presets.addWidget(reset_position); layout.addLayout(presets)
            layout.addWidget(text_label("桌宠形象与表情", heading=True))
        self.slot = ArrowComboBox()
        self.slot.setMinimumWidth(120); self.slot.setMaximumWidth(240)
        slots = [("user_avatar", "你的头像"), ("character_avatar", "角色头像")] if avatars else [("pet_main", "桌宠主形象"), ("pet_thinking", "思考表情"), ("pet_focused", "专注表情"), ("pet_pleased", "愉悦表情"), ("pet_alert", "提示表情")]
        for key, title in slots: self.slot.addItem(title, key)
        slot_row = QHBoxLayout(); slot_row.addWidget(text_label("形象槽位")); slot_row.addStretch(1); slot_row.addWidget(self.slot); layout.addLayout(slot_row)
        self.preview = ImagePreview(); self.preview.setText("选择图片，预览后保存应用"); self.preview.setFixedHeight(112 if avatars else 180); layout.addWidget(self.preview)
        crop_box = QGroupBox("显示区域（0–100%，不会修改源文件）"); form = QVBoxLayout(crop_box); self.crop: list[QDoubleSpinBox] = []
        for title, percentage in [("左侧", 0), ("顶部", 0), ("宽度", 100), ("高度", 100)]:
            row = QHBoxLayout(); row.addWidget(text_label(title)); row.addStretch(1)
            spin = QDoubleSpinBox(); spin.setRange(0, 100); spin.setValue(percentage); spin.setSuffix(" %"); _style_numeric_field(spin)
            spin.valueChanged.connect(self._preview_update); self.crop.append(spin); row.addWidget(spin); form.addLayout(row)
        crop_toggle = QPushButton('调整图片显示区域'); crop_toggle.setCheckable(True)
        crop_toggle.toggled.connect(crop_box.setVisible)
        layout.addWidget(crop_toggle); layout.addWidget(crop_box); crop_box.hide()
        asset_actions = FlowLayout(); self.choose = QPushButton("选择本地图片"); self.apply = QPushButton("保存应用"); self.reset = QPushButton("恢复默认"); discard = QPushButton("取消预览")
        for button in [self.choose, self.apply, self.reset, discard]: asset_actions.addWidget(button)
        layout.addLayout(asset_actions); self.choose.clicked.connect(self._choose); self.apply.clicked.connect(self._apply); self.reset.clicked.connect(self._reset); discard.clicked.connect(self._discard); self.slot.currentIndexChanged.connect(self._slot)
        collect = QPushButton("回收未使用的托管图片"); collect.clicked.connect(self._collect); layout.addWidget(collect)
        self.controller.changed.connect(self._metadata_changed); coordinator.bridge.image_reply.connect(self._image)
        if preferences is not None and hasattr(preferences, "changed"): preferences.changed.connect(self._preference_changed)
        if hasattr(coordinator.bridge, "privacy_changed"): coordinator.bridge.privacy_changed.connect(self._clear)
        group_setting_rows(layout)
        self._profiles(); self._slot(0)
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
        if key == "pet_scale" and not self.avatars: self.scale.blockSignals(True); self.scale.setValue(round(float(value) * 100)); self.scale.blockSignals(False); self.scale_label.setText(f"{self.scale.value()}%")
        if key == 'theme' and not self.avatars:
            self.theme_choice.blockSignals(True); self.theme_choice.setCurrentIndex(self.theme_choice.findData(value)); self.theme_choice.blockSignals(False)
        if key == "user_display_name" and self.avatars: self.user_display_name.setText(str(value))
        if key == "character_display_name" and self.avatars: self.character_display_name.setText(str(value))
    def _profiles(self) -> None:
        values = [spin.value() / 100 for spin in self.crop]
        valid = values[2] > 0 and values[3] > 0 and values[0] + values[2] <= 1 and values[1] + values[3] <= 1
        self.apply.setEnabled(bool(self.token) and not self._busy and valid); self.reset.setEnabled(self.slot.currentData() in self.controller.profiles and not self._busy)
        self.slot.setEnabled(not self._busy); self.choose.setEnabled(not self._busy)
        for spin in self.crop: spin.setEnabled(not self._busy)
    def _metadata_changed(self) -> None:
        self._profiles()
        if not self.token and not self._busy: self._slot(self.slot.currentIndex())
    def _slot(self, index: int) -> None:
        self._discard(); profile = self.controller.profiles.get(str(self.slot.currentData()), {})
        for spin, value in zip(self.crop, profile.get("crop", [0, 0, 1, 1])): spin.setValue(float(value) * 100)
        asset_id = profile.get("effective_asset_id")
        if asset_id: self._request_preview({"asset_id": asset_id})
        elif self.avatars:
            from .chat import ChatView
            preview_label = QLabel()
            ChatView._set_avatar(preview_label, str(self.slot.currentData()), None, 160)
            self.preview.pixmap_value = preview_label.pixmap()
            self._preview_update()
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
        self.preview.mode = "circle" if self.avatars else "contain"; values = [spin.value() / 100 for spin in self.crop]
        if values[2] <= 0 or values[3] <= 0 or values[0] + values[2] > 1.00001 or values[1] + values[3] > 1.00001:
            self.notice.setText("显示区域必须有宽高，且不能超出图片边界。"); self.apply.setEnabled(False); return
        self.preview.crop = values; self.preview.update(); self._profiles()
    def _apply(self) -> None:
        profile = self.controller.profiles.get(str(self.slot.currentData()), {})
        if not self.token or not profile or self._busy: return
        crop = [spin.value() / 100 for spin in self.crop]
        if crop[2] <= 0 or crop[3] <= 0 or crop[0] + crop[2] > 1 or crop[1] + crop[3] > 1: self.notice.setText("显示区域超出图片，请先调整。"); return
        self._busy = True; self._profiles()
        if self.channel.send("appearance_apply", {"slot": self.slot.currentData(), "token": self.token, "expected_revision": profile["revision"], "generation": self.generation, "display_mode": "circle" if self.avatars else "contain", "crop": crop}, self._saved) is None:
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
    def __init__(self, coordinator: Any, preferences: Any, maintenance: Any, parent: Any = None, *, profiles: Any = None) -> None:
        super().__init__(parent); self.coordinator = coordinator; self.preferences = preferences; self.channel = Channel(coordinator.bridge, self); self.maintenance = maintenance
        layout = page_layout(self); layout.addWidget(text_label("设置", heading=True)); self.tabs = SettingsSections(); layout.addWidget(self.tabs, 1)
        self.notice = notice_label(); layout.insertWidget(1, self.notice); self.channel.notice.connect(self.notice.setText)
        self.tabs.setUsesScrollButtons(True)
        persona = QWidget(); vbox = QVBoxLayout(persona)
        vbox.setContentsMargins(0, 0, 0, 0)
        self.avatars = AppearanceSettings(coordinator, preferences, avatars=True)
        vbox.addWidget(self.avatars)
        self.interaction = InteractionSettings(coordinator); vbox.addWidget(self.interaction)
        self.companion = self.interaction
        self.tabs.addTab(scroll(persona), "角色与称呼")
        self.appearance = AppearanceSettings(coordinator, preferences); self.tabs.addTab(scroll(self.appearance), "桌宠与外观")
        notification = QWidget(); vbox = QVBoxLayout(notification); vbox.setContentsMargins(24, 24, 24, 24); vbox.setSpacing(16)
        vbox.addWidget(text_label("通知仅显示通用状态，提醒详情需主动打开查看。"))
        self.notification_controls: dict[str, QCheckBox] = {}
        for key, title in [("notifications_enabled", "启用系统通知"), ("close_to_tray", "关闭主窗口后留在托盘")]:
            control_row = QHBoxLayout(); control_row.addWidget(text_label(title)); control_row.addStretch(1)
            check = QCheckBox(); check.setAccessibleName(title); check.setChecked(bool(getattr(preferences, key, False)))
            check.toggled.connect(lambda value, key=key: self.appearance._pref(key, value)); self.notification_controls[key] = check
            control_row.addWidget(check); vbox.addLayout(control_row)
        self.chat_pause = QPushButton("暂停主动聊天")
        self.reminder_pause = QPushButton("暂停约定提醒")
        self._notification_policy: dict[str, Any] = {}
        self.chat_pause.setEnabled(False); self.reminder_pause.setEnabled(False)
        self.chat_pause.clicked.connect(lambda: self._toggle_policy('chat'))
        self.reminder_pause.clicked.connect(lambda: self._toggle_policy('reminder'))
        for title, button in [("主动聊天", self.chat_pause), ("约定提醒", self.reminder_pause)]:
            control_row = QHBoxLayout(); control_row.addWidget(text_label(title)); control_row.addStretch(1); control_row.addWidget(button); vbox.addLayout(control_row)
        self.quiet_start = QSpinBox(); self.quiet_end = QSpinBox()
        for key, spin, title, default in [("quiet_start", self.quiet_start, "免打扰开始小时", 22), ("quiet_end", self.quiet_end, "免打扰结束小时", 8)]:
            spin.setRange(0, 23); spin.setValue(default); spin.setEnabled(False); _style_numeric_field(spin)
            control_row = QHBoxLayout(); control_row.addWidget(text_label(title)); control_row.addStretch(1); control_row.addWidget(spin); vbox.addLayout(control_row)
            spin.valueChanged.connect(self._quiet)
        self.reminder_bypass = QCheckBox()
        self.reminder_bypass.setAccessibleName('定时提醒可突破免打扰')
        self.reminder_bypass.setEnabled(False)
        self.reminder_bypass.toggled.connect(self._quiet)
        row = QHBoxLayout(); row.addWidget(text_label('定时提醒可突破免打扰')); row.addStretch(1); row.addWidget(self.reminder_bypass); vbox.addLayout(row)
        vbox.addWidget(text_label('关闭时，到期提醒保留在安排页，免打扰结束后汇总通知。主动聊天始终遵守免打扰。'))
        vbox.addStretch(); self.tabs.addTab(scroll(notification), "通知与打扰")
        privacy = QWidget(); vbox = QVBoxLayout(privacy); vbox.setContentsMargins(24, 24, 24, 24); vbox.setSpacing(16)
        vbox.addWidget(text_label("清空聊天与遗忘长期记忆是独立操作。清空默认聊天保留固定入口。运行中的外部效果不能撤回，旧备份与云副本需另行处理。"))
        self.profiles_button = QPushButton("切换、备份与恢复")
        if profiles is not None: self.profiles_button.clicked.connect(profiles)
        else: self.profiles_button.setEnabled(False)
        profile_row = QHBoxLayout(); profile_row.addWidget(text_label("角色档案")); profile_row.addStretch(1); profile_row.addWidget(self.profiles_button); vbox.addLayout(profile_row)
        self.clear_target = ArrowComboBox(); self.clear_target.setMinimumWidth(100); self.clear_target.setMaximumWidth(240)
        clear = QPushButton("清空聊天记录"); clear.clicked.connect(self._clear_conversation)
        clear_row = QHBoxLayout(); clear_row.addWidget(text_label("聊天记录")); clear_row.addStretch(1); clear_row.addWidget(self.clear_target); vbox.addLayout(clear_row)
        clear_actions = QHBoxLayout(); clear_actions.addStretch(1); clear_actions.addWidget(clear); vbox.addLayout(clear_actions)
        self.memory_info = QTreeWidget(); self.memory_info.setHeaderLabels(["记忆与隐私配置", "当前值"]); self.memory_summary = _summary(); vbox.addWidget(self.memory_summary); _details(vbox, self.memory_info, "查看 / 复制完整位置与配置")
        configure_privacy = QPushButton("调整记忆与隐私配置"); configure_privacy.clicked.connect(maintenance)
        privacy_config_row = QHBoxLayout(); privacy_config_row.addWidget(text_label("记忆与隐私设置")); privacy_config_row.addStretch(1); privacy_config_row.addWidget(configure_privacy); vbox.addLayout(privacy_config_row)
        vbox.addStretch(); self.tabs.addTab(scroll(privacy), "数据与隐私")
        model = QWidget(); vbox = QVBoxLayout(model); vbox.setContentsMargins(24, 24, 24, 24); vbox.setSpacing(16)
        vbox.addWidget(text_label("修改连接前会安全关闭角色后台，再进入设置表单。密钥写入系统凭据库且不回显，重新打开后台后生效。")); self.model_info = QTreeWidget(); self.model_info.setHeaderLabels(["连接", "当前生效值"]); self.model_summary = _summary(); vbox.addWidget(self.model_summary); _details(vbox, self.model_info, "查看当前连接技术详情")
        self.model_page = model
        open_model = QPushButton("编辑连接与密钥"); open_model.setObjectName("primary"); open_model.clicked.connect(maintenance)
        model_row = QHBoxLayout(); model_row.addStretch(1); model_row.addWidget(open_model); vbox.addLayout(model_row); vbox.addStretch(); self.tabs.addTab(scroll(model), "模型连接")
        advanced = QWidget(); vbox = QVBoxLayout(advanced); maintenance_button = QPushButton("打开配置、备份与数据维护"); maintenance_button.clicked.connect(maintenance); vbox.addWidget(maintenance_button)
        self.diagnostics = QTreeWidget(); self.diagnostics.setHeaderLabels(["诊断", "内容"]); self.diagnostic_summary = _summary("读取诊断可查看后台运行和清理状态。"); vbox.addWidget(self.diagnostic_summary); _details(vbox, self.diagnostics, "查看 / 复制完整诊断")
        diagnostic = QPushButton("读取诊断"); diagnostic.clicked.connect(self.read_diagnostics); vbox.addWidget(diagnostic)
        cleanup = QPushButton("重试未完成的隐私清理"); cleanup.clicked.connect(self.retry_cleanup); vbox.addWidget(cleanup); self.tabs.addTab(scroll(advanced), "数据与高级")
        self.cleanup_state = _summary(""); vbox.addWidget(self.cleanup_state)
        cleanup_status = QPushButton("查看隐私清理进度"); cleanup_status.clicked.connect(self.read_cleanup_status); vbox.addWidget(cleanup_status); vbox.addStretch()
        for index in range(self.tabs.count()):
            area = self.tabs.widget(index)
            if isinstance(area, QScrollArea):
                content = area.widget()
                if content is not None:
                    content.setMaximumWidth(800)
                    content_layout = content.layout()
                    if isinstance(content_layout, QVBoxLayout) and content is not self.appearance:
                        group_setting_rows(content_layout)
                area.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        for button in self.findChildren(QPushButton):
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        qconfig.themeChanged.connect(self._style_numeric_fields)
        self._style_numeric_fields()
        coordinator.conversations_changed.connect(self._directory)
        if preferences is not None and hasattr(preferences, "changed"): preferences.changed.connect(self._preference_changed)
        if hasattr(coordinator.bridge, "privacy_changed"): coordinator.bridge.privacy_changed.connect(self._privacy)

    def _directory(self) -> None:
        selected = self.clear_target.currentData(); self.clear_target.clear()
        for identity, item in self.coordinator.conversations.items(): self.clear_target.addItem(str(item.get("title", "会话")), identity)
        index = self.clear_target.findData(selected or self.coordinator.default_id)
        if index >= 0: self.clear_target.setCurrentIndex(index)

    def _style_numeric_fields(self) -> None:
        for spin in [*self.appearance.crop, *self.avatars.crop, self.quiet_start, self.quiet_end]:
            _style_numeric_field(spin)

    def _quiet(self, *args: Any) -> None:
        if not self._notification_policy: return
        for widget in (self.quiet_start, self.quiet_end, self.reminder_bypass): widget.setEnabled(False)
        def done(data: dict[str, Any]) -> None:
            if '_error' in data:
                self.channel.send('notification_policy', {}, self._policy_loaded)
            else:
                self._policy_loaded(data)
        parameters: dict[str, Any] = dict(quiet_start=self.quiet_start.value() * 60, quiet_end=self.quiet_end.value() * 60,
                    reminder_bypass=self.reminder_bypass.isChecked())
        if self.channel.send('notification_quiet', parameters, done, mutation=True) is None:
            self._policy_loaded(self._notification_policy)
    def _preference_changed(self, key: str, value: Any) -> None:
        widget = self.notification_controls.get(key)
        if widget:
            widget.blockSignals(True)
            if isinstance(widget, QCheckBox): widget.setChecked(bool(value))
            else: widget.setValue(int(value))
            widget.blockSignals(False)
    def _clear_conversation(self) -> None:
        identity = str(self.clear_target.currentData() or "")
        if identity and QMessageBox.question(self, "清空聊天记录", "清空这个会话的聊天记录和工作上下文？正在处理的相关工作会停止；长期记忆、人格、执行记录和日程提醒保留。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes: self.coordinator.remove_conversation(identity)
    def refresh(self) -> None:
        self.channel.send("settings", {}, self._settings); self.interaction.refresh(); self.companion.set_available(self.coordinator.available); self.companion.poll(); self.coordinator.appearance.refresh()
        self.channel.send('notification_policy', {}, self._policy_loaded)

    def _policy_loaded(self, data: dict[str, Any]) -> None:
        valid = isinstance(data.get('chat'), dict) and isinstance(data.get('reminder'), dict)
        self.chat_pause.setEnabled(valid); self.reminder_pause.setEnabled(valid)
        if '_error' in data or not valid: return
        self._notification_policy = data
        quiet = data.get('quiet', {})
        for key, widget in [('quiet_start', self.quiet_start), ('quiet_end', self.quiet_end), ('reminder_bypass', self.reminder_bypass)]:
            widget.setEnabled(key in quiet)
            widget.blockSignals(True)
            if key in quiet:
                if isinstance(widget, QCheckBox): widget.setChecked(bool(quiet[key]))
                elif isinstance(widget, QSpinBox): widget.setValue(int(quiet[key]) // 60)
            widget.blockSignals(False)
        self.chat_pause.setText('恢复主动聊天' if data.get('chat', {}).get('paused') else '暂停主动聊天')
        self.reminder_pause.setText('恢复约定提醒' if data.get('reminder', {}).get('paused') else '暂停约定提醒')

    def _toggle_policy(self, kind: str) -> None:
        if kind not in self._notification_policy: return
        self.chat_pause.setEnabled(False); self.reminder_pause.setEnabled(False)
        def done(data: dict[str, Any]) -> None:
            self._policy_loaded(data)
            if '_error' in data: self.channel.send('notification_policy', {}, self._policy_loaded)
        if self.channel.send('notification_control', {'kind': kind,
            'paused': not bool(self._notification_policy[kind].get('paused'))}, done, mutation=True) is None:
            self._policy_loaded(self._notification_policy)
    def _settings(self, data: dict[str, Any]) -> None:
        if "_error" in data: return
        fill_tree(self.model_info, {key: data.get(key) for key in ["execution_location", "provider", "model", "protocol", "llm_ready", "credential_mode", "credential_source", "active_revision", "request_use_default"]})
        fill_tree(self.memory_info, {key: data.get(key) for key in ["memory_mode", "embedding_provider", "workspace_dir", "data_dir"]})
        location = "离线演示" if data.get("provider") == "offline" else "本机模型" if data.get("execution_location") == "local" else "云模型"
        self.model_summary.setText(
            ("聊天模型已就绪" if data.get("llm_ready") else "聊天模型尚未配置就绪") + " · " + location
            + "\n模型：" + str(data.get("model") or "尚未选择")
            + "\n凭据：" + _credential(data.get("credential_source"))
            + ("\n回答所需的角色资料、相关记忆和有限历史将交给当前模型。" if data.get("execution_location") == "cloud" and data.get("request_use_default") else "\n聊天数据仍按当前许可处理。"))
        memory = {"automatic": "自动学习与模型复核", "explicit_only": "自动学习与模型复核", "candidate_review": "自动学习与模型复核", "paused": "已暂停自动学习"}.get(str(data.get("memory_mode")), "记忆设置待确认")
        embedding = "已启用本地语义检索" if data.get("embedding_provider") == "fastembed" else "使用关键词检索"
        self.memory_summary.setText(memory + " · " + embedding + "\n工作区：" + compact_path(data.get("workspace_dir", ""))
            + "\n数据目录：" + compact_path(data.get("data_dir", "")) + "\n展开下方可查看或复制完整位置。")
    def read_diagnostics(self) -> None:
        def done(data: dict[str, Any]) -> None:
            if "_error" in data:
                self.diagnostic_summary.setText("诊断读取失败，请稍后重试。")
                return
            fill_tree(self.diagnostics, data)
            dispatcher = data.get("dispatcher", {})
            self.diagnostic_summary.setText(("应用正在关闭。" if data.get("closing") else "应用正在运行。")
                + (" 后台工作线程正常。" if isinstance(dispatcher, dict) and dispatcher.get("worker_alive") else " 后台工作线程状态待确认。")
                + ("\n仍有隐私清理等待完成。" if data.get("cleanup_pending") else "\n当前没有等待中的隐私清理。"))
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
        if data.get("status") not in {"Pending", "Completed"} and not data.get("blocked"):
            self.cleanup_state.setText("清理状态尚未确认，请重新读取；不表示清理完成。")
            fill_tree(self.diagnostics, data)
            return
        pending = data.get("status") == "Pending" or bool(data.get("blocked"))
        self.cleanup_state.setText(("指定记忆已停止使用，仍有清理工作等待完成。" if pending else "隐私清理已完成。") + (" 正在等待运行中的工作安全结束。" if data.get("waiting_for_worker") else ""))
        fill_tree(self.diagnostics, data)
    def _privacy(self, epoch: int = 0) -> None:
        self.notice.clear(); self._notification_policy.clear()
        for widget in (self.quiet_start, self.quiet_end, self.reminder_bypass): widget.setEnabled(False)
        self.chat_pause.setEnabled(False); self.reminder_pause.setEnabled(False)
        self.model_summary.clear(); self.memory_summary.clear(); self.diagnostic_summary.clear(); self.model_info.clear(); self.memory_info.clear(); self.diagnostics.clear(); self.cleanup_state.clear(); self.companion.clear_sensitive()
