"""Complete local profiles. Selection changes only while the app lock is held.

The original data directory remains the application ownership root. All profile
stores use UUID-only child paths. Legacy stores are discarded on first setup,
as explicitly requested by the user; they are never migrated or snapshotted.
Configuration, credentials, workspace and model cache are never copied.
"""

from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from deskpal.config import AppConfig, load_config
from deskpal.maintenance.archives import DATABASES, _regular, _safe, _sync_directory, assert_restore_ready
from deskpal.maintenance.errors import MaintenanceError
from .instance import InstanceLock

REGISTRY = "profiles.json"
INITIALIZING = ".profiles-initializing.json"
RESTORING = ".profile-restore-request.json"
_IDENTITY = re.compile(r"[0-9a-f]{32}\Z")
PROFILE_OPERATIONS = {
    "profile_list": frozenset(),
    "profile_create": frozenset({"name"}),
    "profile_rename": frozenset({"profile_id", "name"}),
    "profile_select": frozenset({"profile_id"}),
    "profile_delete": frozenset({"profile_id"}),
    "profile_backup": frozenset({"profile_id", "destination"}),
    "profile_restore": frozenset({"archive", "name"}),
}


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 400 or any(ord(c) < 32 for c in value):
        raise MaintenanceError("invalid_config")
    return value.strip()


def _path(value: Any) -> Path:
    if not isinstance(value, str) or not value.strip() or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise MaintenanceError("invalid_config")
    return _safe(Path(value))


def _pending_restore(root: Path, identity: str) -> bool:
    request = root / RESTORING
    if not request.exists():
        return False
    _regular(request)
    if request.stat().st_size > 32768:
        raise MaintenanceError("restore_conflict")
    try:
        value = json.loads(request.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value.get("id") == identity
    except (ValueError, TypeError):
        raise MaintenanceError("restore_conflict") from None


def _write(path: Path, value: dict[str, Any]) -> None:
    _safe(path)
    if path.exists():
        _regular(path)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class ProfileRegistry:
    """Mutators require ownership of root's InstanceLock from their caller."""

    def __init__(self, root: Path) -> None:
        self.root = _safe(Path(root))
        self.path = self.root / REGISTRY

    def read(self) -> dict[str, Any]:
        _regular(self.path)
        if self.path.stat().st_size > 1024 * 1024:
            raise MaintenanceError("invalid_config")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or set(data) != {"version", "selected_id", "profiles"}
                    or type(data["version"]) is not int or data["version"] != 1
                    or not isinstance(data["profiles"], list) or len(data["profiles"]) > 1000
                    or (data["selected_id"] is not None and not isinstance(data["selected_id"], str))):
                raise ValueError
            ids = set()
            for row in data["profiles"]:
                if (not isinstance(row, dict) or set(row) != {"id", "name"}
                        or not isinstance(row["id"], str) or not _IDENTITY.fullmatch(row["id"])
                        or row["id"] in ids):
                    raise ValueError
                _name(row["name"])
                ids.add(row["id"])
            if data["selected_id"] is not None and data["selected_id"] not in ids:
                raise ValueError
            return data
        except (ValueError, TypeError, KeyError):
            raise MaintenanceError("invalid_config") from None

    def initialize(self, settings: AppConfig) -> dict[str, Any]:
        # Preserve the original restore barrier before discarding any legacy
        # store. A partial restore must be explicitly resumed in maintenance.
        assert_restore_ready(self.root)
        if self.path.exists():
            return self.read()
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / INITIALIZING
        if marker.exists():
            _regular(marker)
            try:
                identity = json.loads(marker.read_text(encoding="utf-8"))["id"]
                if not isinstance(identity, str) or not _IDENTITY.fullmatch(identity):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise MaintenanceError("invalid_config") from None
        else:
            identity = uuid4().hex
            _write(marker, {"id": identity})
        # Finite old-role inventory only; shared cache/workspace and profiles/
        # are excluded. Retrying an interrupted cleanup is idempotent.
        ProfileService._delete_paths(self.root, settings, self._role_paths(self.root))
        row = {"id": identity, "name": "默认角色"}
        directory = self.directory(row)
        directory.mkdir(parents=True, exist_ok=True)
        _initialize_stores(directory, settings)
        data = {"version": 1, "selected_id": identity,
                "profiles": [row]}
        _write(self.path, data)
        marker.unlink(missing_ok=True)
        return data

    @staticmethod
    def _role_paths(directory: Path) -> list[Path]:
        paths = [directory / name for name in (*DATABASES, "assets", "execution.jsonl")]
        paths.extend(directory / (name + suffix) for name in DATABASES for suffix in ("-wal", "-shm", "-journal"))
        return paths

    def directory(self, row: Mapping[str, Any]) -> Path:
        identity = row["id"]
        if not isinstance(identity, str) or not _IDENTITY.fullmatch(identity):
            raise MaintenanceError("invalid_config")
        return _safe(self.root / "profiles" / identity)

    def selected(self, settings: AppConfig, *, initialize: bool = False) -> AppConfig:
        data = self.initialize(settings) if initialize else self.read() if self.path.exists() else None
        if not initialize and data is None and (self.root / INITIALIZING).exists():
            raise MaintenanceError("restore_pending")
        if data is None:
            return settings
        row = next((row for row in data["profiles"] if row["id"] == data["selected_id"]), None)
        if row is None:
            raise MaintenanceError("profile_selection_required")
        directory = self.directory(row)
        if _pending_restore(self.root, row["id"]):
            raise MaintenanceError("restore_pending")
        if not directory.is_dir() or any(not (directory / database).is_file() for database in DATABASES):
            raise MaintenanceError("archive_source_uninitialized")
        for database in DATABASES:
            path = directory / database
            if path.exists():
                _regular(path)
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = directory / (database + suffix)
                if sidecar.exists():
                    _regular(sidecar)
        if (directory / ".profile-deleting").exists():
            raise MaintenanceError("restore_pending")
        if (directory / ".profile-creating").exists():
            raise MaintenanceError("restore_pending")
        assert_restore_ready(directory)
        return replace(settings, storage=replace(settings.storage, data_dir=directory))


def resolve_profile_config(settings: AppConfig, *, initialize: bool = False) -> AppConfig:
    return ProfileRegistry(settings.storage.data_dir).selected(settings, initialize=initialize)


def _initialize_stores(directory: Path, settings: AppConfig) -> None:
    """Initialize only local stores and the fixed conversation; no workers/models."""
    from deskpal.companion import CompanionRuntime
    from deskpal.conversation.repository import ConversationRepository
    from deskpal.memory.repository import SQLiteMemoryRepository
    from deskpal.scheduling import SchedulingService
    from deskpal.workflow import WorkflowStore
    with ExitStack() as owned:
        store = WorkflowStore(directory / DATABASES[0])
        owned.callback(store.close)
        memory = SQLiteMemoryRepository(directory / DATABASES[1])
        owned.callback(memory.close)
        schedule = SchedulingService(directory / DATABASES[2])
        owned.callback(schedule.close)
        companion = CompanionRuntime(directory / DATABASES[3], timezone=settings.proactive.timezone)
        owned.callback(companion.close)
        ConversationRepository(store).ensure_default()


class ProfileService:
    def __init__(self, config_path: str | Path | None = None, *,
                 cancelled: Callable[[], bool] | None = None,
                 provider_override: str | None = None) -> None:
        from deskpal.maintenance.service import default_config_path
        if provider_override not in (None, "offline"):
            raise MaintenanceError("invalid_config")
        self.config_path = Path(config_path) if config_path is not None else default_config_path()
        self.cancelled = cancelled or (lambda: False)

    def execute(self, operation: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
        if operation not in PROFILE_OPERATIONS or not isinstance(parameters, Mapping) or set(parameters) - PROFILE_OPERATIONS[operation]:
            raise MaintenanceError("invalid_config")
        if self.cancelled():
            raise MaintenanceError("cancelled")
        settings = load_config(self.config_path, provider_override="offline")
        registry = ProfileRegistry(settings.storage.data_dir)
        # Listing an existing registry is a pure atomic-file read, safe while
        # the currently selected backend owns the application root.
        if operation == "profile_list" and registry.path.exists():
            return self._list(registry.read())
        with InstanceLock(registry.root):
            data = registry.initialize(settings)
            if operation == "profile_list":
                return self._list(data)
            if operation in {"profile_create", "profile_restore"}:
                return self._create(registry, data, settings, operation, parameters)
            identity = parameters.get("profile_id")
            if not isinstance(identity, str):
                raise MaintenanceError("profile_not_found")
            row = next((row for row in data["profiles"] if row["id"] == identity), None)
            if row is None:
                raise MaintenanceError("profile_not_found")
            directory = registry.directory(row)
            with ExitStack() as owned:
                if directory != registry.root:
                    owned.enter_context(InstanceLock(directory))
                if operation == "profile_rename":
                    row["name"] = _name(parameters.get("name"))
                elif operation == "profile_select":
                    assert_restore_ready(directory)
                    if (_pending_restore(registry.root, row["id"])
                            or any((directory / marker).exists() for marker in (".profile-deleting", ".profile-creating"))
                            or any(not (directory / database).is_file() for database in DATABASES)):
                        raise MaintenanceError("restore_pending")
                    data["selected_id"] = identity
                elif operation == "profile_backup":
                    from deskpal.maintenance.archives import create_backup
                    # Archive code takes its own per-profile lock.
                    owned.close()
                    return create_backup(directory, _path(parameters.get("destination")), acknowledge_sensitive_data=True)
                elif operation == "profile_delete":
                    self._delete(registry, row, settings)
                    request = registry.root / RESTORING
                    if _pending_restore(registry.root, identity):
                        request.unlink()
                    data["profiles"].remove(row)
                    if data["selected_id"] == identity:
                        data["selected_id"] = None
                _write(registry.path, data)
            return {"status": {"profile_select": "selected", "profile_rename": "renamed", "profile_delete": "deleted"}[operation],
                    "profile_id": identity, **self._list(data)}

    @staticmethod
    def _list(data: Mapping[str, Any]) -> dict[str, Any]:
        return {"profiles": [{"id": row["id"], "name": row["name"], "selected": row["id"] == data["selected_id"]} for row in data["profiles"]], "selected_id": data["selected_id"]}

    def _create(self, registry: ProfileRegistry, data: dict[str, Any], settings: AppConfig,
                operation: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
        row = {"id": uuid4().hex, "name": _name(parameters.get("name"))}
        if operation == "profile_create":
            row = next((item for item in data["profiles"] if item["name"] == row["name"]
                        and (registry.directory(item) / ".profile-creating").exists()), row)
        directory = registry.directory(row)
        directory.parent.mkdir(parents=True, exist_ok=True)
        # Partial profiles remain visible for retry/deletion but never become
        # selectable until every required local initialization has succeeded.
        if operation == "profile_restore":
            return self._restore(registry, data, row, parameters)
        else:
            if len(data["profiles"]) >= 1000 and not any(item["id"] == row["id"] for item in data["profiles"]):
                raise MaintenanceError("invalid_config")
            directory.mkdir(exist_ok=True)
            marker = directory / ".profile-creating"
            _write(marker, {"version": 1})
            if not any(item["id"] == row["id"] for item in data["profiles"]):
                data["profiles"].append(row)
                _write(registry.path, data)
            _initialize_stores(directory, settings)
            marker.unlink()
        return {"status": "created", "profile_id": row["id"], **self._list(data)}

    def _restore(self, registry: ProfileRegistry, data: dict[str, Any], row: dict[str, Any],
                 parameters: Mapping[str, Any]) -> dict[str, Any]:
        from deskpal.maintenance.archives import (MARKER, _entries, _file_entry,
                                                 inspect_backup, restore_backup)
        archive = _path(parameters.get("archive"))
        verified = inspect_backup(archive)
        request = registry.root / RESTORING
        if request.exists():
            _regular(request)
            if request.stat().st_size > 32768:
                raise MaintenanceError("restore_conflict")
            try:
                pending = json.loads(request.read_text(encoding="utf-8"))
                if (not isinstance(pending, dict) or set(pending) != {"id", "name", "archive", "sha256"}
                        or not isinstance(pending["id"], str) or not _IDENTITY.fullmatch(pending["id"])
                        or pending["name"] != row["name"] or pending["archive"] != str(archive)
                        or pending["sha256"] != verified["archive_sha256"]):
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                raise MaintenanceError("restore_conflict") from None
            row = {"id": pending["id"], "name": pending["name"]}
        else:
            _write(request, {**row, "archive": str(archive), "sha256": verified["archive_sha256"]})
        directory = registry.directory(row)
        existing = next((item for item in data["profiles"] if item["id"] == row["id"]), None)
        if existing is None and len(data["profiles"]) >= 1000:
            request.unlink()
            raise MaintenanceError("invalid_config")
        if directory.exists() and not (directory / MARKER).exists() and not (directory / (MARKER + ".tmp")).exists():
            # A crash can occur after archive publication but before registry
            # publication. Adopt only the exact verified bytes, never run it.
            with InstanceLock(directory):
                entries = list(_entries(verified["manifest"]))
                for entry in entries:
                    if _file_entry(directory / entry["name"], entry["name"]) != entry:
                        raise MaintenanceError("restore_conflict")
            receipt = {**verified, "status": "restored", "target_dir": str(directory),
                       "startup_review_required": True}
        else:
            try:
                receipt = restore_backup(archive, directory, acknowledge_data_rollback=True,
                                         resume=directory.exists())
            except BaseException:
                # Keep a failed import visible and deletable. Repeating the
                # same import resumes; deleting this entry clears its request.
                if existing is None:
                    data["profiles"].append(row)
                    _write(registry.path, data)
                raise
        # Legacy archive formats explicitly permit no companion store. Build
        # that single empty store now, before this becomes an openable complete
        # profile; normal startup never silently recreates a missing store.
        companion_defaults = not (directory / DATABASES[3]).exists()
        if companion_defaults:
            from deskpal.companion import CompanionRuntime
            companion = CompanionRuntime(directory / DATABASES[3])
            companion.close()
        if existing is None:
            data["profiles"].append(row)
            _write(registry.path, data)
        request.unlink()
        _sync_directory(registry.root)
        return {**receipt, "status": "restored", "profile_id": row["id"],
                "companion_defaults_created": companion_defaults, **self._list(data)}

    @staticmethod
    def _delete(registry: ProfileRegistry, row: Mapping[str, Any], settings: AppConfig) -> None:
        directory = registry.directory(row)
        _write(directory / ".profile-deleting", {"version": 1})
        # The marker survives a partial deletion and blocks backend startup.
        ProfileService._delete_paths(directory, settings, [path for path in directory.iterdir()
                                     if path.name not in {".instance.lock", ".profile-deleting"}])

    @staticmethod
    def _delete_paths(directory: Path, settings: AppConfig, targets: list[Path]) -> None:
        shared = (settings.memory.model_cache_dir.resolve(), settings.sandbox.workspace_dir.resolve())
        # Preflight the entire finite owned inventory before deleting any byte.
        leaves: list[Path] = []
        directories: list[Path] = []
        def inspect(path: Path) -> None:
            checked = _safe(path)
            if not checked.is_relative_to(directory) or any(checked == root or checked in root.parents or root in checked.parents for root in shared):
                raise MaintenanceError("unsafe_path")
            if not path.exists():
                return
            if path.is_dir():
                directories.append(path)
                for child in path.iterdir():
                    inspect(child)
            else:
                _regular(path)
                leaves.append(path)
        for target in targets:
            inspect(target)
        for leaf in leaves:
            leaf.unlink()
        for target in reversed(directories):
            target.rmdir()


__all__ = ["ProfileService", "ProfileRegistry", "resolve_profile_config", "PROFILE_OPERATIONS"]
