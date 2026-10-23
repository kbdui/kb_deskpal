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
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QPlainTextEdit, QPushButton, QScrollArea,
    QTabWidget, QVBoxLayout, QWidget,
)


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
        if self._closing or self.busy:
            raise RuntimeError("maintenance unavailable")
        # A JSON copy bounds input and prevents later edits to the command.
        encoded = json.dumps(parameters or {}, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 65536:
            raise ValueError("maintenance command too large")
        request_id = str(uuid4())
        self._active = request_id
        self._commands.put_nowait((request_id, operation, json.loads(encoded), generation))
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


class MaintenanceWindow(QMainWindow):
    """Only safe, selected DTO fields are displayed; credential input is write-only."""

    finished = Signal(str)

    def __init__(self, bridge: MaintenanceBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
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
        self.setWindowTitle("Deskpal · 首次配置与数据维护")
        self.resize(820, 720)
        root = QWidget()
        layout = QVBoxLayout(root)
        self.status = QLabel("首次配置、语义检索模型缓存及四库备份恢复。维护期间应用未启动。")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.config_label = QLabel("配置文件：" + str(getattr(bridge, "config_path", "")))
        self.config_label.setWordWrap(True)
        self.config_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.config_label)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.fields: dict[str, QLineEdit | QComboBox] = {}
        self._build_setup()
        self._build_cache()
        self._build_backups()
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.document().setMaximumBlockCount(200)
        layout.addWidget(self.output)
        controls = QHBoxLayout()
        self.start_button = QPushButton("启动应用")
        self.start_button.setEnabled(False)
        self.stop_button = QPushButton("退出 / 请求停止")
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        layout.addLayout(controls)
        self.setCentralWidget(root)
        self.start_button.clicked.connect(self._start)
        self.stop_button.clicked.connect(self.close)
        bridge.reply.connect(self._reply)
        bridge.stopped.connect(self._stopped)

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

    def _button(self, layout: QVBoxLayout, text: str, callback: Callable[[], Any]) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _build_setup(self) -> None:
        layout = self._page("配置")
        hint = QLabel("离线演示是确定性示例。云模式兼容 OpenAI API；保存不调用云连接测试，也不自动安装依赖。新密钥留空沿用环境变量或 Windows 凭据库；环境变量优先。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        choices = {"provider": ["offline", "openai"],
                   "embedding_provider": ["disabled", "fastembed"],
                   "expression_output_format": ["prompt", "json_object"]}
        labels = {"provider": "聊天模型类型", "model": "聊天模型名", "base_url": "API 地址",
                  "api_key_env": "密钥环境变量名", "timeout_seconds": "超时秒数",
                  "expression_output_format": "表达格式", "workspace_dir": "工作区目录",
                  "data_dir": "数据目录", "embedding_provider": "语义检索模型",
                  "model_cache_dir": "语义检索模型缓存目录"}
        for key, label in labels.items():
            field: QLineEdit | QComboBox
            if key in choices:
                field = QComboBox()
                field.addItems(choices[key])
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
        layout.addLayout(form)
        repair_hint = QLabel("修复会重建受支持的配置，其他设置会重置；原文恢复副本可能包含曾误填的秘密，请妥善保管。确认前请检查上述设置。")
        repair_hint.setWordWrap(True)
        layout.addWidget(repair_hint)
        self.repair_confirm = QCheckBox("我理解配置重建、其他设置重置及原文副本的秘密风险，确认修复")
        layout.addWidget(self.repair_confirm)
        self._button(layout, "重新读取配置与来源（放弃未保存编辑）", lambda: self.request("setup_status"))
        self._button(layout, "保存配置", lambda: self._save(False))
        self._button(layout, "确认修复并保存", lambda: self._save(True))

    def _edited(self) -> None:
        if self._populating:
            return
        self._dirty = True
        self._can_start = False
        if hasattr(self, "start_button"):
            self.start_button.setEnabled(False)

    def _build_cache(self) -> None:
        layout = self._page("语义检索模型缓存")
        hint = QLabel("此模型仅用于语义检索，与聊天模型分开。缓存操作使用已保存的路径。ready 仅表示曾成功验证；点击离线校验才会执行本次验证，且仅用 CPU。准备模型需要显式允许下载。缺少依赖时按结果提示安装可选依赖，或换新缓存目录并重新保存配置。清理仅限 Deskpal 自有资产。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.download_confirm = QCheckBox("允许本次联网下载语义检索模型")
        self.clear_confirm = QCheckBox("确认清理当前版本的 Deskpal 自有模型资产")
        layout.addWidget(self.download_confirm)
        layout.addWidget(self.clear_confirm)
        self._button(layout, "查看缓存状态", lambda: self.request("cache_status"))
        self._button(layout, "离线校验", lambda: self.request("cache_verify"))
        self._button(layout, "准备模型", self._prepare)
        self._button(layout, "清理自有缓存", self._clear_cache)

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
        hint = QLabel("备份包含工作、记忆、调度及人格四个数据库；不含工作区原文件、密钥配置、模型或日志。恢复到新的数据目录，保留旧数据；恢复不会自动修改配置或启动应用。中断后使用同一归档和目标继续恢复，完成前不能启动该目录。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.destination = self._path(layout, "选择备份输出文件", "save")
        self.sensitive_confirm = QCheckBox("我知道备份含敏感数据，会妥善保管")
        layout.addWidget(self.sensitive_confirm)
        self._button(layout, "创建四库备份", self._backup)
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
        self.use_restore_button = self._button(layout, "使用恢复目录（填入配置后需明确保存）", self._use_restore)
        self.use_restore_button.setEnabled(False)

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
            values[key] = field.currentText() if isinstance(field, QComboBox) else field.text()
        try:
            values["timeout_seconds"] = float(values["timeout_seconds"])
        except ValueError:
            self.status.setText("超时秒数须为有效数字。")
            return
        parameters: dict[str, Any] = {"values": values, "expected_revision": self._revision}
        if self.secret.text():
            parameters["credential_secret"] = self.secret.text()
        if repair:
            parameters["confirm"] = True
        self._can_start = False
        self.request("setup_repair" if repair else "setup_save", parameters)
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
        field = self.fields["data_dir"]
        if isinstance(field, QLineEdit) and self._restore_target:
            field.setText(self._restore_target)
            self.tabs.setCurrentIndex(0)
            self._can_start = False
            self.start_button.setEnabled(False)
            self.status.setText("恢复目录已填入。请检查并明确保存配置后启动。")

    def request(self, operation: str, parameters: dict[str, Any] | None = None) -> None:
        if self._closing or self._request is not None:
            return
        try:
            self._request = self.bridge.send(operation, parameters, generation=self._generation)
        except (RuntimeError, ValueError):
            self.status.setText("维护请求未受理；请等待当前操作完成或减少输入。")
            return
        self.tabs.setEnabled(False)
        self.start_button.setEnabled(False)
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
            self.status.setText("操作失败；按下方指引修正后重试。")
            self._starting = False
            return
        data = envelope.get("data", {})
        operation = envelope.get("operation", "")
        self.status.setText("维护操作完成。")
        if operation in {"setup_status", "setup_save", "setup_repair"}:
            self._revision = str(data.get("revision", ""))
            values = data.get("values", {})
            self._populating = True
            for key, field in self.fields.items():
                value = str(values.get(key, ""))
                if isinstance(field, QComboBox):
                    field.setCurrentText(value)
                else:
                    field.setText(value)
            self.secret.clear()
            self._populating = False
            self._dirty = False
            blocking = [issue for issue in data.get("issues", [])
                        if issue.get("code") != "environment_override"]
            self._can_start = bool(data.get("can_start", bool(data.get("config_exists"))
                and bool(data.get("config_valid")) and not blocking))
            # Safe source labels/issue messages only, never arbitrary provider DTO JSON.
            sources = data.get("sources", {})
            lines = [f"{key}: {str(sources.get(key, ''))[:160]}" for key in self.fields]
            effective = data.get("effective", {})
            if isinstance(effective, dict):
                lines.extend(f"当前生效 {key}: {str(effective[key])[:500]}"
                             for key in self.fields if key in effective)
            lines.append("已有凭据：" + ("是" if data.get("credential_present") else "否 / 离线无需"))
            lines.extend(str(issue.get("message", "配置需修正"))[:500] for issue in data.get("issues", [])[:20])
            if data.get("configuration_reset"):
                self._setup_notice = "已重建受支持的配置。请重新检查所有设置。"
            if data.get("local_recovery_copy"):
                self._setup_notice += " 原文恢复副本可能包含曾误填的秘密，请妥善保管。"
            if self._setup_notice:
                lines.append(self._setup_notice)
            self.output.setPlainText("\n".join(lines))
            if operation in {"setup_save", "setup_repair"}:
                self.request("setup_status")
                return
            if self._starting:
                self._starting = False
                if self._can_start:
                    self._shutdown("start")
                    return
        elif operation.startswith("cache_"):
            self._cache_revision = str(data.get("revision", ""))
            keys: tuple[str, ...] = ("status", "verification", "dependency_available", "managed", "files", "bytes",
                    "model", "purpose", "install_command")
            self.output.setPlainText("\n".join(f"{key}: {str(data.get(key, ''))[:1000]}" for key in keys))
            self.output.appendPlainText("ready 仅表示曾成功验证；离线校验才会执行本次验证。")
        elif operation.startswith("backup_"):
            keys = ("archive_path", "archive_sha256", "target_dir", "status")
            lines = [f"{key}: {str(data.get(key, ''))[:1000]}" for key in keys]
            manifest = data.get("manifest", {})
            if isinstance(manifest, dict):
                lines.append("归档格式版本：" + str(manifest.get("format_version", ""))[:80])
                entries = manifest.get("databases", manifest.get("entries", []))
                if isinstance(entries, list):
                    lines.append("数据库条目数：" + str(len(entries)))
            self.output.setPlainText("\n".join(lines))
            if operation == "backup_restore" and data.get("status") in {"completed", "complete", "restored", "ready"}:
                self._restore_target = str(data.get("target_dir", ""))
                self.use_restore_button.setEnabled(bool(self._restore_target))
                self.output.appendPlainText("恢复后仍需启动审核原有任务；恢复不代表历史遗忘已完成。请先明确保存恢复目录配置。")
        self.start_button.setEnabled(self._can_start)

    def _start(self) -> None:
        if self._can_start and not self.bridge.busy:
            self._starting = True
            self.request("setup_status")

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
