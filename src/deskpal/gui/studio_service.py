"""Clear Studio setup and role maintenance, backed by the existing isolated workers."""

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QFileDialog

from .maintenance import MaintenanceBridge
from .studio_surface import StudioSurface

CONNECTION_FIELDS = (
    "provider", "execution_location", "protocol", "credential_mode", "model", "base_url",
    "api_key_env", "timeout_seconds", "request_use_default", "expression_output_format",
    "workspace_dir", "data_dir", "memory_mode", "embedding_provider", "model_cache_dir",
)


class StudioServiceWindow(StudioSurface):
    finished = Signal(str)

    def __init__(self, bridge: MaintenanceBridge, mode: str) -> None:
        super().__init__()
        self.bridge = bridge
        self.mode = mode
        self.status_text = ""
        self.startup_notice = ""
        self.initial_action = ""
        self.result: dict[str, Any] = {}
        self.receipt: dict[str, Any] = {}
        self.message = ""
        self.request_id: str | None = None
        self.generation = 0
        self.closing = False
        self.close_pending = False
        self.after_close = "quit"
        self.started = False
        self.resize(990, 730)
        self.setMinimumSize(460, 390)
        self.setWindowTitle("Deskpal · " + ("模型连接与维护" if mode == "maintenance" else "角色档案"))
        self.port.attached.connect(self._attach)
        self.port.action.connect(self._action)
        bridge.reply.connect(self._reply)
        bridge.stopped.connect(self._stopped)

    def open(self) -> None:
        self.show()
        self.load_studio()

    def _attach(self) -> None:
        self.port.publish("init", {"mode": self.mode, "lifecycle": "Ready"})
        self._send("setup_status" if self.mode == "maintenance" else "profile_list")

    def status_message(self, text: str) -> None:
        self.status_text = text
        self._publish()

    def _publish(self) -> None:
        self.port.publish("service", {"snapshot": self.result, "receipt": self.receipt,
            "message": self.message or self.status_text, "busy": self.request_id is not None or self.closing,
            "initialAction": self.initial_action})

    def _send(self, operation: str, params: dict | None = None, secret: str = "") -> None:
        if self.request_id is not None or self.closing:
            return
        self.generation += 1
        try:
            if secret:
                self.request_id = self.bridge.send_sensitive(operation, params or {},
                    credential_secret=secret, generation=self.generation)
            else:
                self.request_id = self.bridge.send(operation, params or {}, generation=self.generation)
        except (ValueError, RuntimeError, BufferError):
            self.message = "后台尚未受理，请稍后重试。"
        else:
            self.message = "正在处理…"
        self._publish()

    def _reply(self, envelope: dict[str, Any]) -> None:
        if envelope.get("request_id") != self.request_id or envelope.get("generation") != self.generation:
            return
        self.request_id = None
        if self.close_pending:
            self._shutdown("quit")
            return
        if not envelope.get("ok"):
            self.started = False
            error = envelope.get("error", {})
            self.message = str(error.get("message", "操作失败。"))
            self._publish()
            return
        operation = envelope.get("operation")
        data = envelope.get("data", {})
        self.message = "操作完成。"
        if operation in {"setup_status", "setup_save", "setup_repair", "profile_list"}:
            self.result = data
            if self.started:
                self.started = False
                if data.get("can_start"):
                    self._shutdown("start")
                    return
            if operation == "profile_list" and self.initial_action == "backup":
                self.initial_action = ""
                self._profile_action("profile-backup", {"id": data.get("selected_id")})
            elif operation == "profile_list" and self.initial_action == "restore":
                self._publish()
                return
        elif operation == "profile_select":
            self._shutdown("start")
            return
        elif operation in {"profile_create", "profile_rename", "profile_delete", "profile_restore"}:
            self._send("profile_list")
            return
        else:
            self.receipt = data
            if operation == "profile_backup":
                self.message = "档案备份已保存。"
        self._publish()

    def _action(self, action: str, args: dict) -> None:
        if self.request_id is not None or self.closing:
            return
        if action == "service-reload":
            self._send("setup_status" if self.mode == "maintenance" else "profile_list")
        elif action == "service-close":
            self._shutdown("quit")
        elif self.mode == "profiles":
            self._profile_action(action, args)
        else:
            self._maintenance_action(action, args)

    def _profile_action(self, action: str, args: dict) -> None:
        identity = args.get("id")
        valid = any(profile.get("id") == identity for profile in self.result.get("profiles", []))
        name = str(args.get("name", "")).strip()
        if action == "service-maintenance":
            self._shutdown("maintenance")
        elif action == "profile-create" and name:
            self._send("profile_create", {"name": name})
        elif action == "profile-rename" and valid and name:
            self._send("profile_rename", {"profile_id": identity, "name": name})
        elif action == "profile-select" and valid:
            self._send("profile_select", {"profile_id": identity})
        elif action == "profile-delete" and valid and args.get("confirmed") == "on":
            self._send("profile_delete", {"profile_id": identity})
        elif action == "profile-backup" and valid:
            path, _ = QFileDialog.getSaveFileName(self, "导出角色档案", "Deskpal-backup.zip", "备份 (*.zip)")
            if path:
                self._send("profile_backup", {"profile_id": identity, "destination": path})
        elif action == "profile-restore" and name:
            self.initial_action = ""
            path, _ = QFileDialog.getOpenFileName(self, "选择档案备份", "", "备份 (*.zip)")
            if path:
                self._send("profile_restore", {"archive": path, "name": name})

    def _maintenance_action(self, action: str, args: dict) -> None:
        if action == "service-profiles":
            self._shutdown("profiles")
        elif action == "service-start":
            if self.result.get("can_start"):
                self.started = True
                self._send("setup_status")
            elif self.result and not self.result.get("config_exists"):
                self._shutdown("start")
        elif action == "save-connection":
            self._save_connection(args)
        elif action == "test-connection" and args.get("confirmed") == "on":
            self._send("connection_test", {"expected_revision": self.result.get("revision"), "confirm": True})
        elif action in {"cache-status", "cache-verify"}:
            self._send(action.replace("-", "_"))
        elif action == "cache-prepare" and args.get("confirmed") == "on":
            self._send("cache_prepare", {"allow_download": True})
        elif action == "cache-clear" and args.get("confirmed") == "on":
            revision = self.receipt.get("revision")
            if revision:
                self._send("cache_clear", {"expected_revision": revision, "confirm": True})
        elif action == "backup-inspect":
            path, _ = QFileDialog.getOpenFileName(self, "选择备份归档", "", "备份 (*.zip)")
            if path:
                self._send("backup_inspect", {"archive": path})

    def _save_connection(self, args: dict) -> None:
        if not self.result:
            return
        values = {key: args[key] for key in CONNECTION_FIELDS if key in args}
        try:
            values["timeout_seconds"] = float(values["timeout_seconds"])
        except (KeyError, ValueError, TypeError):
            self.status_message("请输入有效的超时秒数。")
            return
        values["request_use_default"] = values.get("request_use_default") == "true"
        repair = self.result.get("config_exists") and self.result.get("config_valid") is False
        if repair and args.get("confirm_repair") != "on":
            self.status_message("请确认重建损坏的配置。")
            return
        secret = args.pop("secret", "")
        parameters = {"values": values, "expected_revision": self.result.get("revision"),
                      "credential_action": args.get("credential_action", "keep")}
        if repair:
            parameters["confirm"] = True
        self._send("setup_repair" if repair else "setup_save", parameters, secret if isinstance(secret, str) else "")

    def _shutdown(self, action: str) -> None:
        if not self.closing and self.request_id is None:
            self.closing = True
            self.after_close = action
            self.message = "正在安全关闭后台…"
            self._publish()
            self.bridge.shutdown()

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        if self.request_id is not None:
            self.close_pending = True
            self.status_message("当前操作完成后退出。")
        else:
            self._shutdown("quit")

    def _stopped(self) -> None:
        self.hide()
        self.release_studio()
        self.finished.emit(self.after_close)


class StudioMaintenanceWindow(StudioServiceWindow):
    def __init__(self, bridge: MaintenanceBridge) -> None:
        super().__init__(bridge, "maintenance")


class StudioProfileWindow(StudioServiceWindow):
    def __init__(self, bridge: MaintenanceBridge) -> None:
        super().__init__(bridge, "profiles")
