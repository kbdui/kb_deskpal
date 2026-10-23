"""Explicit, bounded management of the fixed Chinese retrieval model."""

import hashlib
import importlib
import importlib.metadata
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Callable

from deskpal.memory.cache_lock import CacheLease, CacheLeaseError
from deskpal.memory.embedding import MODEL, FastEmbedProvider

from .errors import MaintenanceError

MANIFEST = ".deskpal-model-cache.json"
MAX_FILES = 4096
MAX_BYTES = 4 * 1024**3


def _safe(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.exists() or candidate.is_symlink():
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise MaintenanceError("unsafe_path")


def _inventory(root: Path) -> dict[str, int]:
    _safe(root)
    files: dict[str, int] = {}
    pending = [root]
    entries = 0
    size = 0
    while pending:
        parent = pending.pop()
        with os.scandir(parent) as iterator:
            for item in iterator:
                entries += 1
                if entries > MAX_FILES:
                    raise MaintenanceError("cache_invalid")
                path = Path(item.path)
                _safe(path)
                # Windows DirEntry.stat() omits hard-link metadata on some
                # Python versions. Query the path so nlink remains authoritative.
                info = os.stat(path, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise MaintenanceError("unsafe_path")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1:
                        raise MaintenanceError("unsafe_path")
                    size += info.st_size
                    if size > MAX_BYTES:
                        raise MaintenanceError("cache_invalid")
                    files[path.relative_to(root).as_posix()] = info.st_size
                else:
                    raise MaintenanceError("unsafe_path")
    return files


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _directories(root: Path) -> set[str]:
    # Call only after the bounded no-link inventory has passed.
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir()}


def _owned(manifest: dict[str, Any], root: Path, files: dict[str, int], *, allow_missing: bool = False) -> None:
    assets = manifest["assets"]
    directories = manifest.get("directories")
    present = set(files) - {MANIFEST}
    actual_directories = _directories(root)
    if (not isinstance(directories, list) or any(not isinstance(name, str) for name in directories)
            or not present.issubset(assets) or not actual_directories.issubset(directories)
            or (not allow_missing and (set(assets) != present or set(directories) != actual_directories))):
        raise MaintenanceError("cache_invalid")
    for name in present:
        info = assets[name]
        if (not isinstance(info, dict) or info.get("bytes") != files[name]
                or info.get("sha256") != _hash(root / name)):
            raise MaintenanceError("cache_invalid")


class ModelCacheManager:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = Path(cache_dir).expanduser().absolute()

    @staticmethod
    def _dependency() -> bool:
        try:
            return importlib.metadata.version("fastembed") == "0.9.0"
        except Exception:
            return False

    def _manifest(self) -> tuple[dict[str, Any], str]:
        path = self.cache_dir / MANIFEST
        if path.stat().st_size > 2 * 1024**2:
            raise MaintenanceError("cache_invalid")
        content = path.read_bytes()
        value = json.loads(content)
        if (not isinstance(value, dict) or value.get("version") != 1 or value.get("model") != MODEL
                or not isinstance(value.get("assets"), dict)):
            raise MaintenanceError("cache_invalid")
        return value, hashlib.sha256(content).hexdigest()

    def status(self) -> dict[str, Any]:
        """Metadata only: ready means a prior successful validation, not a new SDK run."""
        result: dict[str, Any] = {
            "status": "missing", "revision": "missing", "managed": False,
            "files": 0, "bytes": 0, "model": MODEL, "purpose": "语义检索（非聊天模型）",
            "install_command": "python -m pip install -r requirements-embedding.txt",
            "verification": "not_checked", "dependency_available": self._dependency(),
        }
        try:
            _safe(self.cache_dir)
            if self.cache_dir.exists():
                files = _inventory(self.cache_dir)
                result.update(files=len(files), bytes=sum(files.values()), status="unmanaged")
                if not files and not _directories(self.cache_dir):
                    result["status"] = "missing"
                if MANIFEST in files:
                    manifest, revision = self._manifest()
                    result.update(revision=revision, managed=True)
                    expected = manifest["assets"]
                    if (set(expected) != set(files) - {MANIFEST}
                            or not isinstance(manifest.get("directories"), list)
                            or any(not isinstance(name, str) for name in manifest["directories"])
                            or set(manifest["directories"]) != _directories(self.cache_dir)
                            or any(not isinstance(item, dict) or item.get("bytes") != files[name]
                                   or not isinstance(item.get("sha256"), str)
                                   for name, item in expected.items())):
                        raise MaintenanceError("cache_invalid")
                    result.update(status="ready", verification="previously_verified")
            if not result["dependency_available"] and result["status"] != "missing":
                result["status"] = "optional_dependency_missing"
        except (OSError, ValueError, MaintenanceError):
            result.update(status="invalid", verification="failed")
        return result

    @staticmethod
    def _verify_at(root: Path) -> None:
        provider = FastEmbedProvider(cache_dir=root)
        try:
            # Exercise tokenizer, CPU session and output validation with fixed public text.
            provider.embed_query("语义检索模型校验")
            provider.embed_documents(("这是离线校验。",))
        finally:
            provider.close()

    def verify(self) -> dict[str, Any]:
        try:
            if not self._dependency():
                raise MaintenanceError("optional_dependency_missing")
            if not self.cache_dir.is_dir():
                raise MaintenanceError("cache_missing")
            with CacheLease(self.cache_dir, exclusive=False):
                files = _inventory(self.cache_dir)
                if MANIFEST in files:
                    manifest, _ = self._manifest()
                    _owned(manifest, self.cache_dir, files)
                self._verify_at(self.cache_dir)
            result = self.status()
            result.update(status="ready", verification="verified_offline")
            return result
        except MaintenanceError:
            raise
        except CacheLeaseError:
            raise MaintenanceError("busy") from None
        except Exception:
            raise MaintenanceError("cache_invalid") from None

    def prepare(self, *, allow_download: bool,
                cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Synchronous worker operation; cancellation waits for an active SDK call."""
        if allow_download is not True:
            raise MaintenanceError("download_not_allowed")
        stage: Path | None = None
        def check_cancelled() -> None:
            if cancelled is not None and cancelled():
                raise MaintenanceError("cancelled")
        try:
            _safe(self.cache_dir)
            with CacheLease(self.cache_dir, exclusive=True):
                check_cancelled()
                if self.cache_dir.exists() and any(self.cache_dir.iterdir()):
                    raise MaintenanceError("cache_unmanaged")
                if not self._dependency():
                    raise MaintenanceError("optional_dependency_missing")
                stage = self.cache_dir.parent / ("." + self.cache_dir.name + ".stage-" + uuid.uuid4().hex)
                stage.mkdir(mode=0o700)
                embedding = importlib.import_module("fastembed").TextEmbedding
                if MODEL not in {item["model"] for item in embedding.list_supported_models()}:
                    raise MaintenanceError("cache_invalid")
                model = embedding(model_name=MODEL, cache_dir=str(stage), threads=4,
                                  providers=["CPUExecutionProvider"], local_files_only=False)
                del model
                check_cancelled()
                self._verify_at(stage)
                assets = _inventory(stage)
                payload = {"version": 1, "model": MODEL, "directories": sorted(_directories(stage)), "assets": {
                    name: {"bytes": size, "sha256": _hash(stage / name)} for name, size in assets.items()
                }}
                (stage / MANIFEST).write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
                check_cancelled()
                if self.cache_dir.exists():
                    self.cache_dir.rmdir()  # only an empty directory can be removed
                stage.rename(self.cache_dir)
                stage = None
            return self.status()
        except MaintenanceError:
            raise
        except CacheLeaseError:
            raise MaintenanceError("busy") from None
        except Exception:
            raise MaintenanceError("cache_invalid") from None
        finally:
            if stage is not None:
                # Never recursively delete untrusted SDK-created links or unknown trees.
                try:
                    self._remove_tree(stage, _inventory(stage))
                except (OSError, MaintenanceError):
                    pass

    @staticmethod
    def _remove_tree(root: Path, assets: dict[str, int], *, remove_root: bool = True) -> None:
        for name in assets:
            if name != MANIFEST:
                (root / name).unlink()
        # Bounded inventory already verified every entry and ruled out links.
        directories = [path for path in root.rglob("*") if path.is_dir()]
        for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            path.rmdir()
        # Preserve ownership/revision while any asset or directory removal can
        # fail. A partial clear can then be retried using the same manifest.
        if MANIFEST in assets:
            (root / MANIFEST).unlink()
        if remove_root:
            root.rmdir()

    def clear(self, *, expected_revision: str, confirm: bool) -> dict[str, Any]:
        if confirm is not True:
            raise MaintenanceError("confirmation_required")
        try:
            _safe(self.cache_dir)
            with CacheLease(self.cache_dir, exclusive=True):
                files = _inventory(self.cache_dir)
                if MANIFEST not in files:
                    raise MaintenanceError("cache_unmanaged")
                manifest, revision = self._manifest()
                if revision != expected_revision:
                    raise MaintenanceError("revision_conflict")
                _owned(manifest, self.cache_dir, files, allow_missing=True)
                self._remove_tree(self.cache_dir, files, remove_root=False)
            return self.status()
        except MaintenanceError:
            raise
        except CacheLeaseError:
            raise MaintenanceError("busy") from None
        except Exception:
            raise MaintenanceError("io_error") from None
