"""Independent maintenance UI: one bounded worker, no live Application/Core."""

from collections.abc import Callable, Mapping
import json
from pathlib import Path
import re
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any, Protocol
from uuid import uuid4

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent, QGuiApplication
from PySide6.QtWidgets import (QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QSizePolicy, QVBoxLayout, QWidget)
from .components import (ChoiceBox as QComboBox, PushButton as QPushButton, CheckBox as QCheckBox, LineEdit as QLineEdit, PlainTextEdit as QPlainTextEdit, ScrollArea as QScrollArea, ToolButton as QToolButton, Tabs as QTabWidget)

from .display_text import compact_path
from .widgets import ArrowComboBox


def _selected(data: Any, keys: Any) -> dict[str, Any]:
    """Select safe scalar fields, without serializing unknown service content."""
    if not isinstance(data, dict):
        return {}
    return {key: data[key] for key in keys if key in data
            and (data[key] is None or type(data[key]) in {str, bool, int, float})}


def _backup_receipt(data: dict[str, Any]) -> dict[str, Any]:
    result = _selected(data, ("archive_path", "archive_sha256", "target_dir", "status",
                             "format_version", "startup_review_required", "privacy_deletion_completed"))
    manifest = data.get("manifest")
    if isinstance(manifest, dict):
        safe = _selected(manifest, ("format_version", "scope", "contains_sensitive_data",
                                   "hashes_prove_authenticity", "rollback_may_restore_forgotten_data"))
        for name, keys in (("databases", ("name", "present", "bytes", "sha256", "schema_version")),
                           ("assets", ("name", "bytes", "sha256", "width", "height", "media_type"))):
            entries = manifest.get(name)
            if isinstance(entries, list):
                safe[name] = [_selected(entry, keys) for entry in entries]
        exclusions = manifest.get("exclusions")
        if isinstance(exclusions, list):
            safe["exclusions"] = [value for value in exclusions if isinstance(value, str)]
        result["manifest"] = safe
    return result


class MaintenancePort(Protocol):
    def execute(self, operation: str, parameters: Mapping[str, Any]) -> dict[str, Any]: ...


def maintenance_factory(path: Path, cancelled: Callable[[], bool], *,
                        provider_override: str | None = None) -> MaintenancePort:
    from deskpal.maintenance.service import MaintenanceService
    return MaintenanceService(path, cancelled=cancelled, provider_override=provider_override)


def _valid_directory_name(name: str) -> bool:
    """A single portable basename, including Windows device-name restrictions."""
    return (bool(name) and name not in {".", ".."} and name == name.strip()
            and not name.endswith(".")
            and re.search(r'[<>:"/\\|?*\x00-\x1f]', name) is None
            and re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name,
                             flags=re.IGNORECASE) is None)


class MaintenanceBridge(QObject):
    """At most one outstanding command/result; Qt widgets never enter the worker."""

    reply = Signal(object)
    stopped = Signal()
    _wake = Signal()

    def __init__(self, path: Path, *, factory: Callable[[Path, Callable[[], bool]], MaintenancePort]
                 = maintenance_factory, provider_override: str | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._path = path
        self._factory = factory
        self._provider_override = provider_override
        self._commands: Queue[Any] = Queue(1)
        self._results: Queue[dict[str, Any]] = Queue(1)
        self._cancel = Event()
        self._closing = False
        self._active: str | None = None
        self._thread: Thread | None = None
        self._reported_stopped = False
        self._timer = QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._drain)
        self._wake.connect(self._drain, Qt.ConnectionType.QueuedConnection)

    @property
    def busy(self) -> bool:
        return self._active is not None

    @property
    def config_path(self) -> Path:
        return self._path

    def send(self, operation: str, parameters: dict[str, Any] | None = None,
             *, generation: int = 0) -> str:
        if parameters is not None and "credential_secret" in parameters:
            raise ValueError("Use the dedicated sensitive configuration channel")
        return self._submit(operation, parameters, generation=generation)

    def send_sensitive(self, operation: str, parameters: dict[str, Any], *,
                       credential_secret: str | None = None, generation: int = 0) -> str:
        """One-shot in-memory delivery; never hash, serialize or cache the secret."""
        if operation not in {"setup_save", "setup_repair"} or "credential_secret" in parameters:
            raise ValueError("Invalid sensitive operation")
        if credential_secret is not None:
            from deskpal.credentials import WindowsCredentialStore
            WindowsCredentialStore.validate_secret(credential_secret)
        return self._submit(operation, parameters, generation=generation, credential_secret=credential_secret)

    def _submit(self, operation: str, parameters: dict[str, Any] | None = None,
                *, generation: int = 0, credential_secret: str | None = None) -> str:
        if self._closing or self.busy:
            raise RuntimeError("maintenance unavailable")
        # A JSON copy bounds input and prevents later edits to the command.
        encoded = json.dumps(parameters or {}, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 65536:
            raise ValueError("maintenance command too large")
        request_id = str(uuid4())
        self._active = request_id
        payload = json.loads(encoded)
        if credential_secret is not None:
            payload["credential_secret"] = credential_secret
        self._commands.put_nowait((request_id, operation, payload, generation))
        if self._thread is None:
            self._thread = Thread(target=self._run, name="deskpal-maintenance", daemon=False)
            self._thread.start()
        return request_id

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._cancel.set()
        if not self.busy and self._thread is not None:
            self._commands.put_nowait(None)
        self._timer.start()
        self._drain()

    def _run(self) -> None:
        service: MaintenancePort | None = None
        try:
            while not self._cancel.is_set():
                command = self._commands.get()
                if command is None or self._cancel.is_set():
                    break
                request_id, operation, parameters, generation = command
                envelope: dict[str, Any] = dict(request_id=request_id, operation=operation,
                                                generation=generation)
                try:
                    if service is None:
                        if self._factory is maintenance_factory:
                            service = maintenance_factory(self._path, self._cancel.is_set,
                                                          provider_override=self._provider_override)
                        else:
                            service = self._factory(self._path, self._cancel.is_set)
                    data = service.execute(operation, parameters)
                    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
                    if len(encoded.encode("utf-8")) > 262144:
                        raise ValueError("maintenance result too large")
                    envelope.update(ok=True, data=json.loads(encoded))
                except Exception as exc:
                    from deskpal.maintenance.service import safe_error
                    envelope.update(ok=False, error=safe_error(exc))
                finally:
                    # Do not retain submitted credentials while waiting for another command.
                    parameters.clear()
                    command = None
                self._results.put_nowait(envelope)
                self._wake.emit()
        finally:
            self._wake.emit()

    @Slot()
    def _drain(self) -> None:
        try:
            envelope = self._results.get_nowait()
        except Empty:
            envelope = None
        if envelope is not None:
            self._active = None
            if not self._closing:
                self.reply.emit(envelope)
        if self._closing:
            if self._thread is not None and self._thread.is_alive():
                return
            self._active = None
            # A stop requested before worker entry may leave an unconsumed command.
            try:
                abandoned = self._commands.get_nowait()
                if abandoned is not None:
                    abandoned[2].clear()
            except Empty:
                pass
            self._timer.stop()
            if not self._reported_stopped:
                self._reported_stopped = True
                self.stopped.emit()


from .components import SettingsWindow


class MaintenanceWindow(SettingsWindow):
    """Only safe, selected DTO fields are displayed; credential input is write-only."""

    finished = Signal(str)

    def __init__(self, bridge: MaintenanceBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from .widgets import install_wheel_guard
        install_wheel_guard()
        self.bridge = bridge
        self._generation = 0
        self._request: str | None = None
        self._closing = False
        self._action = "quit"
        self._revision = ""
        self._cache_revision = ""
        self._restore_target = ""
        self._can_start = False
        self._dirty = False
        self._populating = False
        self._starting = False
        self._setup_notice = ""
        self._config_exists = False
        self._config_valid = False
        self._status_loaded = False
        self.setWindowTitle("Deskpal · 设置")
        root = QWidget()
        layout = QVBoxLayout(root)
        self.status = QLabel("连接、密钥、工作区和模型缓存共用；备份针对当前选择档案。维护期间角色后台未启动。")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        layout.addWidget(self.status)
        config_path = str(getattr(bridge, "config_path", ""))
        self.config_label = QLabel("配置文件：" + (compact_path(config_path) or "未指定"))
        self.config_label.setWordWrap(True)
        self.config_label.setTextFormat(Qt.TextFormat.PlainText)
        self.config_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        layout.addWidget(self.config_label)
        config_toggle = QToolButton()
        config_toggle.setText("查看 / 复制完整配置文件位置")
        config_toggle.setCheckable(True)
        self.config_location = QLineEdit(config_path)
        self.config_location.setReadOnly(True)
        self.config_location.hide()
        config_toggle.toggled.connect(self.config_location.setVisible)
        layout.addWidget(config_toggle)
        layout.addWidget(self.config_location)
        self.config_copy = QPushButton("复制完整配置文件位置")
        self.config_copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(config_path))
        self.config_copy.hide()
        config_toggle.toggled.connect(self.config_copy.setVisible)
        layout.addWidget(self.config_copy)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.fields: dict[str, QLineEdit | QComboBox] = {}
        self._build_setup()
        self._build_cache()
        self._build_backups()
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.document().setMaximumBlockCount(200)
        self.output.setMinimumHeight(78)
        self.output.setMaximumHeight(144)
        layout.addWidget(self.output)
        self.details_toggle = QToolButton()
        self.details_toggle.setText("查看精确回执（完整位置、版本及归档校验值）")
        self.details_toggle.setCheckable(True)
        layout.addWidget(self.details_toggle)
        self.details_panel = QWidget()
        details_layout = QVBoxLayout(self.details_panel)
        details_layout.setContentsMargins(0, 0, 0, 0)
        self.technical_output = QPlainTextEdit()
        self.technical_output.setReadOnly(True)
        self.technical_output.setMinimumHeight(96)
        self.technical_output.setMaximumHeight(170)
        details_layout.addWidget(self.technical_output)
        self.copy_receipt = QPushButton("复制完整精确回执")
        self.copy_receipt.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.technical_output.toPlainText()))
        details_layout.addWidget(self.copy_receipt)
        self.details_panel.hide()
        self.details_toggle.toggled.connect(self.details_panel.setVisible)
        layout.addWidget(self.details_panel)
        controls = QHBoxLayout()
        self.start_button = QPushButton("启动应用")
        self.start_button.setEnabled(False)
        self.stop_button = QPushButton("退出 / 请求停止")
        self.browse_button = QPushButton("暂不配置，浏览本地内容")
        self.browse_button.setEnabled(False)
        self.profiles_button = QPushButton("角色档案 / 导入恢复")
        self.profiles_button.clicked.connect(self._profiles)
        controls.addWidget(self.start_button)
        controls.addWidget(self.browse_button)
        controls.addWidget(self.profiles_button)
        controls.addWidget(self.stop_button)
        layout.addLayout(controls)
        self.set_settings_content(root)
        self.start_button.clicked.connect(self._start)
        self.browse_button.clicked.connect(self._browse)
        self.stop_button.clicked.connect(self.close)
        bridge.reply.connect(self._reply)
        bridge.stopped.connect(self._stopped)
        self.tabs.currentChanged.connect(self._section_changed)
        for button in self.findChildren(QPushButton):
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def open(self) -> None:
        self.show()
        self.request("setup_status")

    def _page(self, name: str) -> QVBoxLayout:
        content = QWidget()
        layout = QVBoxLayout(content)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        self.tabs.addTab(scroll, name)
        return layout

    def _receipt(self, data: dict[str, Any]) -> None:
        self.technical_output.setPlainText(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))

    def _button(self, layout: QVBoxLayout, text: str, callback: Callable[[], Any]) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _build_setup(self) -> None:
        layout = self._page("模型与连接")
        hint = QLabel("默认使用云模型，密钥写入系统凭据库且不回显。保存后重新打开后台生效；连接检查由你单独发起。本地模型可在此切换，仅支持已部署的兼容 Responses 端点。离线演示仅返回固定示例。")
        hint.setWordWrap(True)
        hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        layout.addWidget(hint)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        choices = {"provider": ["openai", "offline"],
                   "execution_location": ["cloud", "local"], "protocol": ["responses"],
                   "credential_mode": ["system_vault", "environment", "none"],
                   "request_use_default": ["true", "false"],
                   "memory_mode": ["automatic", "paused"],
                   "embedding_provider": ["disabled", "fastembed"],
                   "expression_output_format": ["prompt", "json_object"]}
        labels = {"provider": "模型适配器 / 高级离线演示", "execution_location": "执行位置",
                  "protocol": "连接协议（兼容 Responses 接口）", "credential_mode": "有效凭据来源",
                  "request_use_default": "允许新聊天输入由云服务处理",
                  "model": "聊天模型名", "base_url": "API 地址",
                  "api_key_env": "密钥环境变量名", "timeout_seconds": "超时秒数",
                  "expression_output_format": "表达格式", "workspace_dir": "工作区目录",
                  "data_dir": "数据目录", "memory_mode": "记忆写入方式（保存后重启生效）", "embedding_provider": "语义检索模型",
                  "model_cache_dir": "语义检索模型缓存目录"}
        for key, label in labels.items():
            field: QLineEdit | QComboBox
            if key in choices:
                field = ArrowComboBox()
                from .widgets import label as enum_label
                for option in choices[key]: field.addItem(enum_label(option), option)
            else:
                field = QLineEdit()
                field.setMaxLength(4096)
            self.fields[key] = field
            if isinstance(field, QComboBox):
                field.currentTextChanged.connect(self._edited)
            else:
                field.textChanged.connect(self._edited)
            form.addRow(label, field)
        self.secret = QLineEdit()
        self.secret.setMaxLength(8192)
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret.textChanged.connect(self._edited)
        form.addRow("保存新密钥到 Windows 凭据库", self.secret)
        self.secret_action = ArrowComboBox()
        for option, title in [("keep", "保留当前凭据"), ("replace", "替换为本次输入"), ("remove", "移除当前凭据")]: self.secret_action.addItem(title, option)
        self.secret_action.currentTextChanged.connect(self._edited)
        self.secret.textChanged.connect(self._secret_edited)
        form.addRow("凭据操作（保留 / 替换 / 移除）", self.secret_action)
        self.show_secret = QCheckBox("临时查看本次输入")
        self.show_secret.toggled.connect(lambda shown: self.secret.setEchoMode(
            QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password))
        form.addRow("", self.show_secret)
        self.local_hint = QLabel("本地服务须已自行部署；仅允许 127.0.0.1 / ::1 / localhost，忽略环境代理并拒绝所有重定向。协议验证需单独点击测试连接。")
        self.local_hint.setWordWrap(True)
        form.addRow("本地连接", self.local_hint)
        self._setup_form = form
        location = self.fields["execution_location"]
        mode = self.fields["credential_mode"]
        assert isinstance(location, QComboBox) and isinstance(mode, QComboBox)
        location.currentTextChanged.connect(self._connection_fields)
        mode.currentTextChanged.connect(self._connection_fields)
        layout.addLayout(form)
        repair_hint = QLabel("修复会重建受支持的配置，其他设置会重置；原文恢复副本可能包含曾误填的秘密，请妥善保管。确认前请检查上述设置。")
        repair_hint.setWordWrap(True)
        repair_hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        layout.addWidget(repair_hint)
        self.repair_confirm = QCheckBox("我理解配置重建、其他设置重置及原文副本的秘密风险，确认修复")
        layout.addWidget(self.repair_confirm)
        self._button(layout, "重新读取配置与来源（放弃未保存编辑）", lambda: self.request("setup_status"))
        self._button(layout, "保存配置", lambda: self._save(False))
        self._button(layout, "确认修复并保存", lambda: self._save(True))
        self.test_confirm = QCheckBox("测试将发送固定公共文本，不含私人上下文；云服务可能产生少量费用")
        layout.addWidget(self.test_confirm)
        self._button(layout, "测试已保存的连接", self._test_connection)
        self._connection_fields()
        layout.addStretch()

    def _section_changed(self, index: int) -> None:
        if index != 0:
            self.secret.clear()
            self.show_secret.setChecked(False)

    def _secret_edited(self, value: str) -> None:
        if value and not self._populating:
            self.secret_action.setCurrentIndex(self.secret_action.findData("replace"))

    def _connection_fields(self) -> None:
        mode = self.fields["credential_mode"]
        location = self.fields["execution_location"]
        assert isinstance(mode, QComboBox) and isinstance(location, QComboBox)
        self._setup_form.setRowVisible(self.fields["api_key_env"], mode.currentData() == "environment")
        for widget in (self.secret, self.secret_action, self.show_secret):
            self._setup_form.setRowVisible(widget, mode.currentData() == "system_vault")
        self._setup_form.setRowVisible(self.local_hint, location.currentData() == "local")
        self._setup_form.setRowVisible(self.fields["request_use_default"], location.currentData() == "cloud")
        if mode.currentData() != "system_vault":
            self.secret.clear()
            self.show_secret.setChecked(False)

    def _test_connection(self) -> None:
        if self._dirty:
            self.status.setText("请先保存配置，再测试已保存的连接。")
            return
        if not self.test_confirm.isChecked():
            self.status.setText("请明确确认发送固定公共文本及可能的少量费用。")
            return
        self.request("connection_test", {"expected_revision": self._revision, "confirm": True})

    def _edited(self) -> None:
        if self._populating:
            return
        self._dirty = True
        self._can_start = False
        if hasattr(self, "start_button"):
            self.start_button.setEnabled(False)

    def _build_cache(self) -> None:
        layout = self._page("语义检索模型缓存")
        hint = QLabel("此模型仅用于语义检索，与聊天模型分开。缓存操作使用已保存的路径。就绪状态仅表示曾成功验证；点击离线校验才会执行本次验证，且仅用 CPU。准备模型需要显式允许下载。缺少依赖时按结果提示安装可选依赖，或换新缓存目录并重新保存配置。清理仅限 Deskpal 自有资产。")
        hint.setWordWrap(True)
        hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        layout.addWidget(hint)
        self.download_confirm = QCheckBox("允许本次联网下载语义检索模型")
        self.clear_confirm = QCheckBox("确认清理当前版本的 Deskpal 自有模型资产")
        layout.addWidget(self.download_confirm)
        layout.addWidget(self.clear_confirm)
        self._button(layout, "查看缓存状态", lambda: self.request("cache_status"))
        self._button(layout, "离线校验", lambda: self.request("cache_verify"))
        self._button(layout, "准备模型", self._prepare)
        self._button(layout, "清理自有缓存", self._clear_cache)
        layout.addStretch()

    def _path(self, layout: QVBoxLayout, label: str, mode: str) -> QLineEdit:
        row = QHBoxLayout()
        field = QLineEdit()
        field.setMaxLength(4096)
        button = QPushButton(label)
        def choose() -> None:
            if mode == "directory":
                value = QFileDialog.getExistingDirectory(self, label)
            elif mode == "save":
                value, _ = QFileDialog.getSaveFileName(self, label)
            else:
                value, _ = QFileDialog.getOpenFileName(self, label)
            if value:
                field.setText(value)
        button.clicked.connect(choose)
        row.addWidget(field)
        row.addWidget(button)
        layout.addLayout(row)
        return field

    def _build_backups(self) -> None:
        layout = self._page("备份与恢复")
        hint = QLabel("备份当前选择档案的四个数据库及受管图片；不含工作区原文件、密钥配置、模型或日志。使用备份须通过“角色档案 / 导入恢复”注册为独立新档案。本页恢复到新目录仅供离线维护，不能将该目录设为应用数据根；中断后须使用同一归档和目标继续恢复。")
        hint.setWordWrap(True)
        hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        layout.addWidget(hint)
        self.destination = self._path(layout, "选择备份输出文件", "save")
        self.sensitive_confirm = QCheckBox("我知道备份含敏感数据，会妥善保管")
        layout.addWidget(self.sensitive_confirm)
        self._button(layout, "备份当前选择档案", self._backup)
        self.archive = self._path(layout, "选择恢复归档", "open")
        self.target = QLineEdit()
        self.target.setMaxLength(4096)
        self.target.setPlaceholderText("可手输目标路径；首次恢复使用尚不存在的新目录")
        layout.addWidget(self.target)
        self._button(layout, "选择父目录并输入新目标目录名", self._choose_restore_target)
        self._button(layout, "选择已有未完成目录（继续恢复）", self._choose_resume_target)
        self.rollback_confirm = QCheckBox("我理解数据会回退，可能恢复后来已遗忘的数据")
        self.resume_confirm = QCheckBox("继续同一归档和目录的未完成恢复")
        layout.addWidget(self.rollback_confirm)
        layout.addWidget(self.resume_confirm)
        self._button(layout, "检查归档", lambda: self.request("backup_inspect", {"archive": self.archive.text()}))
        self._button(layout, "恢复到所选新目录", self._restore)
        self.use_restore_button = self._button(layout, "进入角色档案，导入原备份", self._use_restore)
        self.use_restore_button.setEnabled(False)
        layout.addStretch()

    def _choose_restore_target(self) -> None:
        parent = QFileDialog.getExistingDirectory(self, "选择新恢复目录的父目录")
        if not parent:
            return
        name, accepted = QInputDialog.getText(self, "新的恢复目录", "输入新目录名（仅单个名称，不含路径）")
        if not accepted:
            return
        if not _valid_directory_name(name):
            self.status.setText("目录名须为单个有效名称，不能包含路径、特殊字符或保留设备名。")
            return
        target = Path(parent) / name
        try:
            if target.exists() or target.is_symlink():
                self.status.setText("目标已存在；请选择新的目录名。继续未完成恢复请使用独立入口。")
                return
        except OSError:
            self.status.setText("无法检查目标路径，请选择可访问的父目录。")
            return
        self.target.setText(str(target))
        self.resume_confirm.setChecked(False)
        self.status.setText("已选择尚不存在的新目标；提交恢复后由后台创建。")

    def _choose_resume_target(self) -> None:
        target = QFileDialog.getExistingDirectory(self, "选择已有的未完成恢复目录")
        if target:
            self.target.setText(target)
            self.resume_confirm.setChecked(True)
            self.status.setText("已选择继续恢复目录；请使用原归档。后台会核查恢复标记。")

    def _save(self, repair: bool) -> None:
        if repair and not self.repair_confirm.isChecked():
            self.status.setText("请先明确确认修复损坏配置。")
            return
        values: dict[str, Any] = {}
        for key, field in self.fields.items():
            values[key] = field.currentData() if isinstance(field, QComboBox) else field.text()
        try:
            values["timeout_seconds"] = float(values["timeout_seconds"])
        except ValueError:
            self.status.setText("超时秒数须为有效数字。")
            return
        values["request_use_default"] = values["request_use_default"] == "true"
        parameters: dict[str, Any] = {"values": values, "expected_revision": self._revision}
        parameters["credential_action"] = self.secret_action.currentData()
        if repair:
            parameters["confirm"] = True
        self._can_start = False
        self.request("setup_repair" if repair else "setup_save", parameters,
                     credential_secret=self.secret.text() or None)
        self.secret.clear()
        parameters.clear()

    def _prepare(self) -> None:
        if self.download_confirm.isChecked():
            self.request("cache_prepare", {"allow_download": True})
        else:
            self.status.setText("请明确允许本次下载后再准备模型。")

    def _clear_cache(self) -> None:
        if self.clear_confirm.isChecked() and self._cache_revision:
            self.request("cache_clear", {"expected_revision": self._cache_revision, "confirm": True})
        else:
            self.status.setText("请先读取缓存状态，并明确确认清理。")

    def _backup(self) -> None:
        if self.sensitive_confirm.isChecked():
            self.request("backup_create", {"destination": self.destination.text(), "acknowledge_sensitive_data": True})
        else:
            self.status.setText("请先确认备份的敏感数据边界。")

    def _restore(self) -> None:
        if not self.rollback_confirm.isChecked():
            self.status.setText("请明确确认数据回退和遗忘数据恢复的风险。")
            return
        target = Path(self.target.text()).expanduser()
        if not self.target.text().strip() or not _valid_directory_name(target.name):
            self.status.setText("请输入有效的恢复目标路径。")
            return
        try:
            if not self.resume_confirm.isChecked() and (target.exists() or target.is_symlink()):
                self.status.setText("首次恢复目标必须尚不存在；已有未完成目录请明确选择继续恢复。")
                return
            if self.resume_confirm.isChecked() and not target.is_dir():
                self.status.setText("继续恢复需要选择已有的未完成目录和原归档。")
                return
        except OSError:
            self.status.setText("无法检查恢复目录，请检查路径和访问权限。")
            return
        self._can_start = False
        self._restore_target = ""
        self.use_restore_button.setEnabled(False)
        self.request("backup_restore", {"archive": self.archive.text(), "target_dir": self.target.text(),
            "acknowledge_data_rollback": True, "resume": self.resume_confirm.isChecked()})

    def _use_restore(self) -> None:
        # An offline recovery directory is not an application root. Only the
        # profile service may import and register a backup for normal startup.
        if self._restore_target:
            self._profiles()

    def request(self, operation: str, parameters: dict[str, Any] | None = None,
                *, credential_secret: str | None = None) -> None:
        if self._closing or self._request is not None:
            return
        try:
            if operation in {"setup_save", "setup_repair"}:
                self._request = self.bridge.send_sensitive(operation, parameters or {},
                    credential_secret=credential_secret, generation=self._generation)
            else:
                self._request = self.bridge.send(operation, parameters, generation=self._generation)
        except (RuntimeError, ValueError):
            self.status.setText("维护请求未受理；请等待当前操作完成或减少输入。")
            return
        self.tabs.setEnabled(False)
        self.start_button.setEnabled(False)
        self.browse_button.setEnabled(False)
        self.status.setText("正在执行维护操作；同步模型阶段无法强制中止。")

    @Slot(object)
    def _reply(self, envelope: Any) -> None:
        if (self._closing or not isinstance(envelope, dict)
                or envelope.get("request_id") != self._request
                or envelope.get("generation") != self._generation):
            return
        self._request = None
        self.tabs.setEnabled(True)
        if not envelope.get("ok"):
            error = envelope.get("error", {})
            steps = error.get("next_steps", [])
            self.output.setPlainText(str(error.get("message", "维护失败。")) + "\n" + "\n".join(str(x) for x in steps))
            self._receipt(_selected(error, ("code", "message")))
            self.status.setText("操作失败；按下方指引修正后重试。")
            self._starting = False
            self.start_button.setEnabled(self._can_start)
            self.browse_button.setEnabled(self._status_loaded and (not self._config_exists or self._can_start))
            return
        data = envelope.get("data", {})
        operation = envelope.get("operation", "")
        self.status.setText("维护操作完成。")
        if operation in {"setup_status", "setup_save", "setup_repair"}:
            self._status_loaded = True
            self._config_exists = bool(data.get("config_exists"))
            self._config_valid = bool(data.get("config_valid"))
            self._revision = str(data.get("revision", ""))
            values = data.get("values", {})
            self._populating = True
            for key, field in self.fields.items():
                raw_value = values.get(key, "")
                value = str(raw_value).lower() if type(raw_value) is bool else str(raw_value)
                if isinstance(field, QComboBox):
                    if key == 'memory_mode' and value in ('explicit_only', 'candidate_review'):
                        value = 'automatic'
                    field.setCurrentIndex(field.findData(value))
                else:
                    field.setText(value)
            self.secret.clear()
            self.secret_action.setCurrentIndex(self.secret_action.findData("keep"))
            self.show_secret.setChecked(False)
            self._populating = False
            self._dirty = False
            blocking = [issue for issue in data.get("issues", [])
                        if issue.get("code") != "environment_override"]
            self._can_start = bool(data.get("can_start", bool(data.get("config_exists"))
                and bool(data.get("config_valid")) and not blocking))
            self.start_button.setText("启动应用" if data.get("llm_ready") else "启动浏览（聊天待配置）")
            # Only allowlisted safe fields enter the optional exact receipt.
            sources = data.get("sources", {})
            effective = data.get("effective", {})
            receipt = _selected(data, ("revision", "config_exists", "config_valid", "can_start", "llm_ready",
                                       "credential_present", "credential_source", "credential_cleanup_pending",
                                       "configuration_reset", "local_recovery_copy"))
            receipt["sources"] = _selected(sources, self.fields)
            receipt["effective"] = _selected(effective, self.fields)
            receipt["values"] = _selected(values, self.fields)
            receipt["issues"] = [_selected(issue, ("code", "message")) for issue in data.get("issues", [])[:20]]
            self._receipt(receipt)
            current = effective if isinstance(effective, dict) and effective else values
            location = ("离线演示" if current.get("provider") == "offline" else
                        "本机模型" if current.get("execution_location") == "local" else "云模型")
            lines = [("配置可供启动" if self._can_start else "配置仍需处理，请查看提示") + " · " + location,
                     "聊天模型：" + str(current.get("model") or "尚未选择")]
            source = {"system_vault": "Windows 系统凭据库", "environment": "环境变量", "dotenv": "本地环境文件",
                      "none": "无需凭据", "missing": "尚未提供凭据"}.get(str(data.get("credential_source")), "来源待确认")
            lines.append("凭据：" + source)
            if isinstance(sources, dict) and any(value in {"environment", "dotenv"}
                                                for value in sources.values() if isinstance(value, str)):
                lines.append("部分设置由环境覆盖，下次启动采用的完整值可展开查看。")
            else:
                lines.append("下次启动采用的完整设置与来源可展开查看。")
            if data.get("credential_cleanup_pending"):
                lines.append("配置已移除凭据引用，但系统凭据库清理尚未完成。")
            lines.extend(str(issue.get("message", "配置需修正"))[:500] for issue in data.get("issues", [])[:20])
            if data.get("configuration_reset"):
                self._setup_notice = "已重建受支持的配置。请重新检查所有设置。"
            if data.get("local_recovery_copy"):
                self._setup_notice += " 原文恢复副本可能包含曾误填的秘密，请妥善保管。"
            if self._setup_notice:
                lines.append(self._setup_notice)
            self.output.setPlainText("\n".join(lines))
            if operation in {"setup_save", "setup_repair"}:
                self._setup_notice = "已保存，等待重新启动后生效；尚未测试连接。" + self._setup_notice
                self.status.setText("已保存，等待启动后生效。")
                self.request("setup_status")
                return
            if self._starting:
                self._starting = False
                if self._can_start:
                    self._shutdown("start")
                    return
        elif operation == "connection_test":
            self.status.setText("连接验证成功；已验证保存版本的 Responses 文本响应。")
            self.output.setPlainText("固定公共文本请求成功；未发送聊天、历史、人格或记忆。配置是否生效取决于应用是否已重新启动。")
            self._receipt(_selected(data, ("status", "revision", "protocol", "execution_location", "model")))
        elif operation.startswith("cache_"):
            self._cache_revision = str(data.get("revision", ""))
            keys: tuple[str, ...] = ("status", "revision", "verification", "dependency_available", "managed", "files", "bytes",
                    "model", "purpose", "install_command")
            self._receipt(_selected(data, keys))
            cache_state = {"missing": "尚未准备模型缓存", "ready": "模型缓存已准备",
                           "unmanaged": "发现未托管的模型缓存", "invalid": "模型缓存校验不通过",
                           "optional_dependency_missing": "缺少语义检索依赖"}.get(str(data.get("status")), "模型缓存状态待确认")
            evidence = {"previously_verified": "只记录曾成功验证；本次尚未验证。",
                        "verified_offline": "本次已完成离线校验与固定文本推理。",
                        "failed": "本次验证失败，请先修复缓存。",
                        "not_checked": "尚未执行本次离线校验。"}.get(str(data.get("verification")), "校验证据待确认。")
            lines = [cache_state + "。", evidence,
                     "语义检索依赖" + ("已安装。" if data.get("dependency_available") else "尚未安装；安装命令见精确回执。"),
                     "缓存" + ("由本应用托管；清理仍需明确确认。" if data.get("managed") else "未取得托管清理资格。")]
            if type(data.get("files")) is int and type(data.get("bytes")) is int:
                lines.append(f"共 {data['files']} 个文件，约 {data['bytes'] / 1048576:.1f} MiB。")
            self.output.setPlainText("\n".join(lines))
        elif operation.startswith("backup_"):
            self._receipt(_backup_receipt(data))
            lines = [{"backup_create": "备份已创建。", "backup_inspect": "归档完整性检查已完成。",
                      "backup_restore": "恢复操作已完成。"}.get(str(operation), "备份维护操作已完成。")]
            if data.get("archive_path"):
                lines.append("归档：" + compact_path(data["archive_path"]))
            if data.get("target_dir"):
                lines.append("恢复目录：" + compact_path(data["target_dir"]))
            manifest = data.get("manifest", {})
            if isinstance(manifest, dict):
                lines.append("归档格式版本：" + str(manifest.get("format_version", ""))[:80])
                entries = manifest.get("databases", manifest.get("entries", []))
                if isinstance(entries, list):
                    lines.append("数据库条目数：" + str(len(entries)))
            lines.append("展开精确回执可查看 / 复制完整位置、归档哈希与逐库校验值。哈希用于校验完整性，不证明来源可信。")
            self.output.setPlainText("\n".join(lines))
            if operation == "backup_restore" and data.get("status") in {"completed", "complete", "restored", "ready"}:
                self._restore_target = str(data.get("target_dir", ""))
                self.use_restore_button.setEnabled(bool(self._restore_target))
                self.output.appendPlainText("离线恢复已完成，该目录不能直接作为应用数据根。要在应用中使用，请进入角色档案并导入原备份，注册为新档案。恢复不代表历史遗忘已完成。")
        self.start_button.setEnabled(self._can_start)
        self.browse_button.setEnabled(self._status_loaded and (not self._config_exists or self._can_start))

    def _browse(self) -> None:
        if self._dirty:
            self.status.setText("仍有未保存更改；请保存，或重新读取配置以放弃编辑后浏览。")
            return
        if not self.bridge.busy and self._status_loaded and (not self._config_exists or self._can_start):
            self._shutdown("start")

    def _start(self) -> None:
        if self._can_start and not self.bridge.busy:
            self._starting = True
            self.request("setup_status")

    def _profiles(self) -> None:
        if self._dirty:
            self.status.setText("请先保存更改，或重新读取配置以放弃编辑后进入角色档案。")
            return
        if not self.bridge.busy and not self._closing:
            self._shutdown("profiles")

    def _shutdown(self, action: str) -> None:
        self._closing = True
        self._generation += 1
        self._action = action
        self.secret.clear()
        self.tabs.setEnabled(False)
        self.start_button.setEnabled(False)
        self.status.setText("已请求停止，等待当前阶段退出；窗口和事件循环会保留至后台释放。")
        self.bridge.shutdown()

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        if not self._closing:
            self._shutdown("quit")

    @Slot()
    def _stopped(self) -> None:
        self.hide()
        self.finished.emit(self._action)
