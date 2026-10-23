"""Bounded, local maintenance commands; no production worker or model chat."""

from collections.abc import Callable, Mapping
import json
import os
from pathlib import Path
import sys
from typing import Any

from deskpal.application.instance import InstanceLock, InstanceLockError
from deskpal.config import AppConfig, ConfigError, load_config
from deskpal.credentials import CredentialStoreError
from deskpal.llm.errors import LLMError
from deskpal.llm.url_policy import validate_base_url

from .errors import MaintenanceError, message_for


OPERATIONS: dict[str, frozenset[str]] = {
    "setup_status": frozenset(),
    "setup_save": frozenset({"values", "expected_revision", "credential_secret"}),
    "setup_repair": frozenset({"values", "expected_revision", "credential_secret", "confirm"}),
    "cache_status": frozenset(),
    "cache_verify": frozenset(),
    "cache_prepare": frozenset({"allow_download"}),
    "cache_clear": frozenset({"expected_revision", "confirm"}),
    "backup_create": frozenset({"destination", "acknowledge_sensitive_data"}),
    "backup_inspect": frozenset({"archive"}),
    "backup_restore": frozenset({"archive", "target_dir", "acknowledge_data_rollback", "resume"}),
    "diagnostics": frozenset(),
}

_GUIDANCE: dict[str, tuple[str, ...]] = {
    "invalid_config": ("检查路径和模型设置；文件损坏时使用明确确认的修复入口。",),
    "config_missing": ("先保存首次配置，或选择已有配置文件。",),
    "revision_conflict": ("刷新当前内容，保留需要的编辑，再重新确认保存。",),
    "busy": ("完整退出使用该数据目录或模型缓存的应用，再重试。",),
    "credential_missing": ("设置所选环境变量，或在 Windows 向导中保存系统凭据。",),
    "credential_store_unavailable": ("检查当前 Windows 用户的系统凭据库，或改用外部环境变量。",),
    "optional_dependency_missing": ("安装可选 embedding 依赖后，再进行离线验证或明确下载。",),
    "cache_unmanaged": ("可以离线验证旧缓存；准备或清理请使用独立的托管缓存目录。",),
    "cache_invalid": ("保留现状，选择新空目录准备模型，再保存新路径。",),
    "archive_instance_busy": ("先退出同一数据目录的 GUI 和 CLI，再创建备份或恢复。",),
    "archive_source_uninitialized": ("先完成应用初始化；损坏或不完整数据不能伪装成完整备份。",),
    "archive_invalid": ("重新选择可信的完整备份；不要修改 manifest 或数据库来绕过校验。",),
    "archive_destination_exists": ("选择一个尚不存在的备份文件名。",),
    "restore_pending": ("在维护页选择原备份和恢复目录，明确继续恢复。",),
    "restore_resume_required": ("选择相同备份，并勾选继续未完成恢复。",),
    "restore_conflict": ("保留当前目录，检查用户修改；可改用另一个新目录恢复。",),
    "io_error": ("检查目录权限和磁盘空间；保留旧数据，不覆盖重试。",),
}


def default_config_path() -> Path:
    """Prefer an existing project configuration, otherwise the user directory."""
    local = Path.cwd() / "config.toml"
    if local.is_file():
        # Keep the lexical filename so SetupService can reject a linked config
        # before a save, rather than silently editing its resolved target.
        return local.absolute()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"))
        return base / "Deskpal" / "config.toml"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"))
    return base / "deskpal" / "config.toml"


def safe_error(error: BaseException) -> dict[str, Any]:
    """Only host-owned messages cross GUI/CLI boundaries, never str(error)."""
    if isinstance(error, MaintenanceError):
        code = error.code
    elif isinstance(error, InstanceLockError):
        code = "busy"
    elif isinstance(error, ConfigError):
        code = "invalid_config"
    elif isinstance(error, CredentialStoreError):
        code = "credential_store_unavailable"
    elif isinstance(error, ImportError):
        code = "optional_dependency_missing"
    elif isinstance(error, KeyboardInterrupt):
        code = "cancelled"
    elif isinstance(error, (TypeError, ValueError, KeyError)):
        code = "invalid_config"
    else:
        code = "io_error"
    return {"code": code, "message": message_for(code),
            "next_steps": list(_GUIDANCE.get(code, ("保留现状，按错误分类检查后再明确重试。",)))}


def _text(parameters: Mapping[str, Any], key: str) -> str:
    value = parameters.get(key)
    if (not isinstance(value, str) or not value.strip() or len(value) > 8192
            or any(ord(char) < 32 for char in value)):
        raise MaintenanceError("invalid_config")
    return value


def _flag(parameters: Mapping[str, Any], key: str) -> bool:
    value = parameters.get(key, False)
    if type(value) is not bool:
        raise MaintenanceError("invalid_config")
    return value


def _bounded(value: dict[str, Any]) -> dict[str, Any]:
    # A secret field accidentally returned by an adapter is an error, never a
    # partially redacted success. credential_ref is deliberately non-secret.
    def inspect(item: Any, depth: int = 0) -> None:
        if depth > 16:
            raise MaintenanceError("io_error")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or key.lower() in {"api_key", "credential_secret", "secret", "token", "authorization"}:
                    raise MaintenanceError("io_error")
                inspect(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                inspect(child, depth + 1)

    try:
        inspect(value)
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 4 * 1024 * 1024:
            raise MaintenanceError("io_error")
        copied: dict[str, Any] = json.loads(encoded)
        return copied
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise MaintenanceError("io_error") from None


class MaintenanceService:
    """One explicit operation at a time; caller owns background scheduling."""

    def __init__(self, config_path: str | Path | None = None, *,
                 cancelled: Callable[[], bool] | None = None,
                 provider_override: str | None = None) -> None:
        if provider_override not in (None, "offline"):
            raise MaintenanceError("invalid_config")
        self.config_path = Path(config_path) if config_path is not None else default_config_path()
        self._cancelled = cancelled or (lambda: False)
        self._provider_override = provider_override

    def _settings(self) -> AppConfig:
        if not self.config_path.is_file():
            raise MaintenanceError("config_missing")
        try:
            # Maintenance does not need a cloud credential and never loads a
            # chat provider. The same path/environment precedence still applies.
            return load_config(self.config_path, provider_override="offline")
        except (ConfigError, OSError, UnicodeError):
            raise MaintenanceError("invalid_config") from None

    def _setup_status(self, status: dict[str, Any]) -> dict[str, Any]:
        """File validity, effective startup readiness and warnings are distinct."""
        result = dict(status)
        result["can_start"] = False
        if result.get("config_exists") is not True or result.get("config_valid") is not True:
            return result
        try:
            settings = load_config(self.config_path, provider_override=self._provider_override)
            try:
                validate_base_url(settings.llm.base_url)
            except LLMError:
                raise MaintenanceError("invalid_config")
            from .archives import assert_restore_ready
            assert_restore_ready(settings.storage.data_dir)
            result["effective"] = {
                "provider": settings.llm.provider, "model": settings.llm.model,
                "base_url": settings.llm.base_url, "api_key_env": settings.llm.api_key_env,
                "timeout_seconds": settings.llm.timeout_seconds,
                "expression_output_format": settings.llm.expression_output_format,
                "workspace_dir": str(settings.sandbox.workspace_dir),
                "data_dir": str(settings.storage.data_dir),
                "embedding_provider": settings.memory.embedding_provider,
                "model_cache_dir": str(settings.memory.model_cache_dir),
            }
            result["can_start"] = True
        except (MaintenanceError, ConfigError, OSError, CredentialStoreError, ValueError) as error:
            issue = safe_error(error)
            result["issues"] = [*result.get("issues", []), issue]
        return result

    def execute(self, operation: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
        if operation not in OPERATIONS or not isinstance(parameters, Mapping):
            raise MaintenanceError("invalid_config")
        if set(parameters) - OPERATIONS[operation]:
            raise MaintenanceError("invalid_config")
        try:
            encoded = json.dumps(dict(parameters), ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) > 131072:
                raise MaintenanceError("invalid_config")
        except (TypeError, ValueError, RecursionError, UnicodeError):
            raise MaintenanceError("invalid_config") from None
        if self._cancelled():
            raise MaintenanceError("cancelled")
        return _bounded(self._execute(operation, parameters))

    def _execute(self, operation: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
        if operation.startswith("setup_"):
            from .setup import SetupService
            setup = SetupService(self.config_path)
            if operation == "setup_status":
                return self._setup_status(setup.inspect())
            values = parameters.get("values")
            if not isinstance(values, dict):
                raise MaintenanceError("invalid_config")
            revision = _text(parameters, "expected_revision")
            secret = parameters.get("credential_secret")
            if secret is not None and not isinstance(secret, str):
                raise MaintenanceError("invalid_config")
            if operation == "setup_repair":
                status = setup.repair(values, expected_revision=revision,
                                      confirm=_flag(parameters, "confirm"), credential_secret=secret or None)
            else:
                status = setup.save(values, expected_revision=revision, credential_secret=secret or None)
            return self._setup_status(status)
        if operation.startswith("cache_"):
            from .cache import ModelCacheManager
            settings = self._settings()
            cache = ModelCacheManager(settings.memory.model_cache_dir)
            if operation == "cache_status":
                return cache.status()
            # Every mutating/expensive action checks the configured application
            # ownership as well as the cache reader/writer lease itself.
            with InstanceLock(settings.storage.data_dir):
                if operation == "cache_verify":
                    return cache.verify()
                if operation == "cache_prepare":
                    return cache.prepare(allow_download=_flag(parameters, "allow_download"),
                                         cancelled=self._cancelled)
                return cache.clear(expected_revision=_text(parameters, "expected_revision"),
                                   confirm=_flag(parameters, "confirm"))
        if operation.startswith("backup_"):
            from .archives import create_backup, inspect_backup, restore_backup
            if operation == "backup_inspect":
                return inspect_backup(Path(_text(parameters, "archive")))
            if operation == "backup_create":
                return create_backup(self._settings().storage.data_dir, Path(_text(parameters, "destination")),
                                     acknowledge_sensitive_data=_flag(parameters, "acknowledge_sensitive_data"))
            try:
                current = self._settings().storage.data_dir
            except MaintenanceError as error:
                if error.code not in {"config_missing", "invalid_config"}:
                    raise
                current = None
            return restore_backup(Path(_text(parameters, "archive")), Path(_text(parameters, "target_dir")),
                                  acknowledge_data_rollback=_flag(parameters, "acknowledge_data_rollback"),
                                  resume=_flag(parameters, "resume"), current_data_dir=current)
        from .setup import SetupService
        status = self._setup_status(SetupService(self.config_path).inspect())
        result: dict[str, Any] = {"config_path": str(self.config_path.absolute()),
                                  "setup": status, "network_requested": False,
                                  "backend_started": False}
        try:
            settings = self._settings()
            result["data_dir"] = str(settings.storage.data_dir)
            result["restore_pending"] = (settings.storage.data_dir / ".restore-in-progress.json").exists()
            result["databases"] = {name: (settings.storage.data_dir / name).is_file() for name in (
                "workflow.sqlite3", "memory.sqlite3", "scheduling.sqlite3", "companion.sqlite3")}
        except MaintenanceError as error:
            result["configuration_issue"] = safe_error(error)
        return result
