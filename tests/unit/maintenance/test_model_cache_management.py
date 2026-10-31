"""Prepared model-cache tests; no actual SDK initialization or downloads."""

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from deskpal.maintenance.cache import MANIFEST, ModelCacheManager
from deskpal.maintenance.errors import MaintenanceError
from deskpal.memory.cache_lock import CacheLease, CacheLeaseError
from deskpal.memory.embedding import MODEL, EmbeddingError, FastEmbedProvider


def managed(root: Path) -> ModelCacheManager:
    root.mkdir()
    (root / "model.onnx").write_bytes(b"fake-assets-not-a-real-model")
    payload = {"version": 1, "model": MODEL, "directories": [], "assets": {
        "model.onnx": {"bytes": (root / "model.onnx").stat().st_size,
                       "sha256": hashlib.sha256((root / "model.onnx").read_bytes()).hexdigest()}}}
    (root / MANIFEST).write_text(json.dumps(payload), encoding="utf-8")
    return ModelCacheManager(root)


def test_cache_leases_shared_users_and_exclusive_maintenance(tmp_path: Path) -> None:
    root = tmp_path / "models"
    first = CacheLease(root, exclusive=False).acquire()
    second = CacheLease(root, exclusive=False).acquire()
    try:
        with pytest.raises(CacheLeaseError):
            CacheLease(root, exclusive=True).acquire()
        first.release()
        with pytest.raises(CacheLeaseError):
            CacheLease(root, exclusive=True).acquire()
    finally:
        first.release()
        second.release()
    with CacheLease(root, exclusive=True):
        with pytest.raises(CacheLeaseError):
            CacheLease(root, exclusive=False).acquire()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native cache-lock failure paths")
@pytest.mark.parametrize("phase", ["acquire", "release"])
def test_windows_native_lock_failure_closes_descriptor_and_allows_reacquire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    descriptors: list[int] = []

    class FailingNativeLock:
        def __init__(self, fd: int, *, exclusive: bool) -> None:
            descriptors.append(fd)
            if phase == "acquire":
                raise OSError("private-native-error")

        def release(self) -> None:
            raise OSError("private-native-error")

    root = tmp_path / "models"
    lease = CacheLease(root, exclusive=False)
    with monkeypatch.context() as patch:
        patch.setattr("deskpal.memory.cache_lock._WindowsLock", FailingNativeLock)
        with pytest.raises(CacheLeaseError) as failure:
            lease.acquire()
            lease.release()
        assert "private-native-error" not in str(failure.value)
        assert len(descriptors) == 1
        with pytest.raises(OSError):
            os.fstat(descriptors[0])
        lease.release()  # Failed acquisition/release must leave this idempotent.
    with CacheLease(root, exclusive=True):
        pass


def test_missing_status_is_metadata_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ModelCacheManager, "_dependency", staticmethod(lambda: True))
    manager = ModelCacheManager(tmp_path / "models")
    assert manager.status()["status"] == "missing"
    assert not manager.cache_dir.exists()


def test_clear_requires_confirmation_revision_and_ownership(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ModelCacheManager, "_dependency", staticmethod(lambda: True))
    manager = managed(tmp_path / "models")
    state = manager.status()
    for confirm, revision in ((False, state["revision"]), (True, "stale")):
        with pytest.raises(MaintenanceError):
            manager.clear(expected_revision=revision, confirm=confirm)
        assert manager.cache_dir.exists()
    (manager.cache_dir / "user-notes.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(MaintenanceError):
        manager.clear(expected_revision=state["revision"], confirm=True)
    assert (manager.cache_dir / "user-notes.txt").read_text() == "keep"
    (manager.cache_dir / "user-notes.txt").unlink()
    assert manager.clear(expected_revision=state["revision"], confirm=True)["status"] == "missing"


def test_clear_rejects_tamper_unknown_directory_and_active_reader(tmp_path: Path) -> None:
    manager = managed(tmp_path / "models")
    revision = manager.status()["revision"]
    with CacheLease(manager.cache_dir, exclusive=False):
        with pytest.raises(MaintenanceError) as failure:
            manager.clear(expected_revision=revision, confirm=True)
        assert failure.value.code == "busy"
    (manager.cache_dir / "unknown-empty-directory").mkdir()
    with pytest.raises(MaintenanceError):
        manager.clear(expected_revision=revision, confirm=True)
    (manager.cache_dir / "unknown-empty-directory").rmdir()
    (manager.cache_dir / "model.onnx").write_bytes(b"tampered")
    with pytest.raises(MaintenanceError):
        manager.clear(expected_revision=revision, confirm=True)
    assert manager.cache_dir.exists()


@pytest.mark.parametrize("name", ["model.onnx", MANIFEST])
def test_hard_linked_cache_files_are_rejected_before_verify_or_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    monkeypatch.setattr(ModelCacheManager, "_dependency", staticmethod(lambda: True))
    manager = managed(tmp_path / "models")
    revision = manager.status()["revision"]
    source = manager.cache_dir / name
    external = tmp_path / "outside-cache.bin"
    original = source.read_bytes()
    try:
        os.link(source, external)
    except PermissionError:
        pytest.skip("This platform does not permit synthetic hard-link creation")
    assert source.stat().st_nlink == 2

    def forbidden_verify(root: Path) -> None:
        raise AssertionError("hard-linked assets must be rejected before model initialization")

    monkeypatch.setattr(ModelCacheManager, "_verify_at", staticmethod(forbidden_verify))
    assert manager.status()["status"] == "invalid"
    with pytest.raises(MaintenanceError) as verify_failure:
        manager.verify()
    assert verify_failure.value.code == "unsafe_path"
    with pytest.raises(MaintenanceError) as clear_failure:
        manager.clear(expected_revision=revision, confirm=True)
    assert clear_failure.value.code == "unsafe_path"
    assert source.read_bytes() == original
    assert external.read_bytes() == original
    assert (manager.cache_dir / MANIFEST).is_file()


def test_prepare_requires_download_authorization_and_preserves_legacy(tmp_path: Path) -> None:
    root = tmp_path / "models"
    root.mkdir()
    (root / "personal.txt").write_text("keep", encoding="utf-8")
    manager = ModelCacheManager(root)
    with pytest.raises(MaintenanceError) as failure:
        manager.prepare(allow_download=False)
    assert failure.value.code == "download_not_allowed"
    with pytest.raises(MaintenanceError) as failure:
        manager.prepare(allow_download=True)
    assert failure.value.code == "cache_unmanaged"
    assert (root / "personal.txt").read_text() == "keep"


def test_cancel_before_sdk_loading(tmp_path: Path) -> None:
    manager = ModelCacheManager(tmp_path / "models")
    with pytest.raises(MaintenanceError) as failure:
        manager.prepare(allow_download=True, cancelled=lambda: True)
    assert failure.value.code == "cancelled"
    assert not manager.cache_dir.exists()


def test_legacy_verify_closes_provider_and_never_claims_managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "legacy"
    root.mkdir()
    (root / "weights.onnx").write_bytes(b"fake")
    called: list[Path] = []
    monkeypatch.setattr(ModelCacheManager, "_dependency", staticmethod(lambda: True))
    monkeypatch.setattr(ModelCacheManager, "_verify_at", staticmethod(lambda path: called.append(path)))
    result = ModelCacheManager(root).verify()
    assert called == [root]
    assert result["verification"] == "verified_offline"
    assert result["managed"] is False


def test_prepare_stages_verifies_and_publishes_owned_assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    class FakeEmbedding:
        @staticmethod
        def list_supported_models() -> list[dict[str, str]]:
            return [{"model": MODEL}]

        def __init__(self, **kwargs: object) -> None:
            assert kwargs["model_name"] == MODEL
            assert kwargs["local_files_only"] is False
            assert kwargs["providers"] == ["CPUExecutionProvider"]
            stage = Path(str(kwargs["cache_dir"]))
            assert not (tmp_path / "models").exists()
            (stage / "weights.onnx").write_bytes(b"fake-fixed-model")
            events.append("download")
    def verify(stage: Path) -> None:
        assert (stage / "weights.onnx").is_file()
        assert not (tmp_path / "models").exists()
        events.append("verify")
    monkeypatch.setattr(ModelCacheManager, "_dependency", staticmethod(lambda: True))
    monkeypatch.setattr("deskpal.maintenance.cache.importlib.import_module",
                        lambda name: SimpleNamespace(TextEmbedding=FakeEmbedding))
    monkeypatch.setattr(ModelCacheManager, "_verify_at", staticmethod(verify))
    manager = ModelCacheManager(tmp_path / "models")
    state = manager.prepare(allow_download=True)
    assert events == ["download", "verify"]
    assert state["status"] == "ready"
    assert state["managed"] is True
    assert not list(tmp_path.glob(".models.stage-*"))
    manager.clear(expected_revision=state["revision"], confirm=True)
    assert manager.cache_dir.is_dir()
    assert not any(manager.cache_dir.iterdir())


def test_provider_verification_closes_resources_after_failed_embedding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    class FakeProvider:
        def __init__(self, **kwargs: object) -> None:
            events.append("open")

        def embed_query(self, text: str) -> None:
            raise RuntimeError("unsafe-private-error")

        def close(self) -> None:
            events.append("close")
    monkeypatch.setattr("deskpal.maintenance.cache.FastEmbedProvider", FakeProvider)
    with pytest.raises(RuntimeError):
        ModelCacheManager._verify_at(tmp_path)
    assert events == ["open", "close"]


def test_provider_initialization_failure_releases_shared_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "models"
    root.mkdir()
    monkeypatch.setattr("deskpal.memory.embedding.importlib.metadata.version", lambda name: "wrong-version")
    with pytest.raises(EmbeddingError):
        FastEmbedProvider(cache_dir=root)
    with CacheLease(root, exclusive=True):
        pass


def test_partial_clear_retains_manifest_for_safe_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ModelCacheManager, "_dependency", staticmethod(lambda: True))
    external = tmp_path / "personal-outside-cache.txt"
    external.write_bytes(b"keep-external-file")
    manager = managed(tmp_path / "models")
    second = manager.cache_dir / "tokenizer.json"
    second.write_bytes(b"fake-tokenizer")
    manifest_path = manager.cache_dir / MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["assets"][second.name] = {"bytes": second.stat().st_size, "sha256": hashlib.sha256(second.read_bytes()).hexdigest()}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    revision = manager.status()["revision"]
    original_unlink = Path.unlink
    removed: list[Path] = []
    def fail_second(path: Path, missing_ok: bool = False) -> None:
        if path.parent == manager.cache_dir and path.name != MANIFEST:
            if removed:
                raise PermissionError("simulated-private-file-error")
            removed.append(path)
        original_unlink(path, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", fail_second)
    with pytest.raises(MaintenanceError) as failure:
        manager.clear(expected_revision=revision, confirm=True)
    assert failure.value.code == "io_error"
    assert len(removed) == 1
    partial = manager.status()
    assert partial["status"] == "invalid"
    assert partial["managed"] is True
    assert partial["revision"] == revision
    assert manifest_path.is_file()
    monkeypatch.setattr(Path, "unlink", original_unlink)
    unknown = manager.cache_dir / "personal.txt"
    unknown.write_bytes(b"keep-user-file")
    with pytest.raises(MaintenanceError):
        manager.clear(expected_revision=revision, confirm=True)
    assert unknown.read_bytes() == b"keep-user-file"
    assert manifest_path.is_file()
    unknown.unlink()  # user explicitly removes the unrelated file before retry
    result = manager.clear(expected_revision=revision, confirm=True)
    assert result["status"] == "missing"
    assert manager.cache_dir.is_dir()
    assert not any(manager.cache_dir.iterdir())
    assert external.read_bytes() == b"keep-external-file"
