"""P1 scenario coverage. Prepared only; execution requires user authorization."""

import json
import os
from pathlib import Path

import pytest

from deskpal.application import create_application
from deskpal.application.instance import InstanceLock, InstanceLockError
from deskpal.application.profiles import ProfileRegistry, ProfileService, resolve_profile_config
from deskpal.config import load_config
from deskpal.maintenance import archives
from deskpal.maintenance.errors import MaintenanceError
from deskpal.maintenance.service import MaintenanceService


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("DESKPAL_"):
            monkeypatch.delenv(key)
    path = tmp_path / "config.toml"
    path.write_text('[llm]\nprovider="offline"\n[storage]\ndata_dir="data"\n'
                    '[sandbox]\nworkspace_dir="workspace"\n'
                    '[memory]\nmodel_cache_dir="data/models"\n', encoding="utf-8")
    return path, ProfileService(path), load_config(path, environ={})


def test_first_setup_discards_old_role_only_and_never_cleans_new_profiles(profiles):
    path, service, config = profiles
    root = config.storage.data_dir
    root.mkdir()
    for database in archives.DATABASES:
        (root / database).write_bytes(b"old private data")
        (root / (database + "-wal")).write_bytes(b"old wal")
    (root / "assets").mkdir()
    (root / "assets" / "old.png").write_bytes(b"old image")
    (root / "execution.jsonl").write_text("old event")
    config.memory.model_cache_dir.mkdir()
    cache = config.memory.model_cache_dir / "shared-model"
    cache.write_text("keep")
    config.sandbox.workspace_dir.mkdir()
    workspace = config.sandbox.workspace_dir / "external.txt"
    workspace.write_text("keep")
    original_config = path.read_bytes()
    first = service.execute("profile_list", {})
    actual = resolve_profile_config(config).storage.data_dir
    assert actual == root / "profiles" / first["selected_id"]
    assert all((actual / name).is_file() for name in archives.DATABASES)
    assert all(not (root / name).exists() for name in archives.DATABASES)
    assert not (root / "assets").exists()
    assert not (root / "execution.jsonl").exists()
    sentinel = actual / "private-receipt"
    sentinel.write_text("new data")
    assert service.execute("profile_list", {}) == first
    assert sentinel.read_text() == "new data"
    assert cache.read_text() == workspace.read_text() == "keep"
    assert path.read_bytes() == original_config


def test_profiles_use_distinct_stores_and_stable_identity(profiles):
    path, service, config = profiles
    initial = service.execute("profile_list", {})["selected_id"]
    created = service.execute("profile_create", {"name": "B"})["profile_id"]
    with create_application(config) as first:
        first_root = first.config.storage.data_dir
        (first_root / "private-marker").write_text("A")
        assert first_root.name == initial
        assert first.conversation_ensure_default()["kind"] == "default"
    service.execute("profile_rename", {"profile_id": created, "name": "B改名"})
    assert service.execute("profile_select", {"profile_id": created})["status"] == "selected"
    with create_application(config) as second:
        assert second.config.storage.data_dir.name == created
        assert not (second.config.storage.data_dir / "private-marker").exists()
        assert second.config.sandbox == config.sandbox
        assert second.config.memory == config.memory
    service.execute("profile_select", {"profile_id": initial})
    assert resolve_profile_config(config).storage.data_dir == first_root
    assert (first_root / "private-marker").read_text() == "A"
    status = MaintenanceService(path).execute("setup_status", {})
    assert Path(status["effective"]["data_dir"]) == first_root


def test_application_lock_blocks_switch_creation_and_other_backend(profiles):
    _, service, config = profiles
    service.execute("profile_list", {})
    other = service.execute("profile_create", {"name": "B"})["profile_id"]
    with create_application(config):
        with pytest.raises(InstanceLockError):
            service.execute("profile_select", {"profile_id": other})
        with pytest.raises(InstanceLockError):
            service.execute("profile_create", {"name": "C"})
        with pytest.raises(InstanceLockError):
            create_application(config)
        assert len(service.execute("profile_list", {})["profiles"]) == 2
    with InstanceLock(config.storage.data_dir):
        with pytest.raises(InstanceLockError):
            create_application(config)


def test_delete_clears_whole_profile_and_preserves_other_profile_and_shared_data(profiles):
    _, service, config = profiles
    original = service.execute("profile_list", {})["selected_id"]
    second = service.execute("profile_create", {"name": "B"})["profile_id"]
    root = config.storage.data_dir
    selected = root / "profiles" / original
    (selected / "private-file").write_text("private")
    config.memory.model_cache_dir.mkdir(exist_ok=True)
    shared = config.memory.model_cache_dir / "shared"
    shared.write_text("keep")
    result = service.execute("profile_delete", {"profile_id": original})
    assert result["selected_id"] is None
    assert not (selected / "private-file").exists()
    assert not any((selected / name).exists() for name in archives.DATABASES)
    assert all((root / "profiles" / second / name).exists() for name in archives.DATABASES)
    assert shared.read_text() == "keep"
    assert (root / ".instance.lock").exists()
    with pytest.raises(MaintenanceError, match="请选择"):
        create_application(config)


def test_backup_restore_registers_new_profile_and_corrupt_store_fails_closed(profiles, tmp_path):
    path, service, config = profiles
    source = service.execute("profile_list", {})["selected_id"]
    archive = tmp_path / "profile.zip"
    service.execute("profile_backup", {"profile_id": source, "destination": str(archive)})
    restored = service.execute("profile_restore", {"archive": str(archive), "name": "恢复"})
    assert restored["profile_id"] != source
    assert restored["selected_id"] == source
    assert restored["startup_review_required"]
    service.execute("profile_select", {"profile_id": restored["profile_id"]})
    assert Path(MaintenanceService(path).execute("diagnostics", {})["data_dir"]).name == restored["profile_id"]
    damaged = config.storage.data_dir / "profiles" / restored["profile_id"] / "companion.sqlite3"
    damaged.write_bytes(b"corrupt store")
    with pytest.raises(Exception):
        create_application(config)
    # Failure releases both locks, allowing another valid selection.
    service.execute("profile_select", {"profile_id": source})
    with create_application(config) as app:
        assert app.config.storage.data_dir.name == source


def test_interrupted_restore_is_visible_and_resumes_exact_request(profiles, tmp_path, monkeypatch):
    _, service, config = profiles
    source = service.execute("profile_list", {})["selected_id"]
    archive = tmp_path / "profile.zip"
    service.execute("profile_backup", {"profile_id": source, "destination": str(archive)})
    original_restore = archives.restore_backup

    def interrupted(*args, **kwargs):
        def fault(phase):
            if phase == "published:workflow.sqlite3":
                raise RuntimeError("synthetic interruption")
        return original_restore(*args, **kwargs, fault_hook=fault)

    monkeypatch.setattr(archives, "restore_backup", interrupted)
    with pytest.raises(RuntimeError, match="synthetic"):
        service.execute("profile_restore", {"archive": str(archive), "name": "恢复"})
    listing = service.execute("profile_list", {})
    pending = next(item["id"] for item in listing["profiles"] if item["id"] != source)
    directory = config.storage.data_dir / "profiles" / pending
    assert (directory / archives.MARKER).exists()
    with pytest.raises(MaintenanceError):
        service.execute("profile_select", {"profile_id": pending})
    monkeypatch.setattr(archives, "restore_backup", original_restore)
    resumed = service.execute("profile_restore", {"archive": str(archive), "name": "恢复"})
    assert resumed["profile_id"] == pending
    assert len(resumed["profiles"]) == 2
    assert not (directory / archives.MARKER).exists()


@pytest.mark.parametrize("invalid", [[], {"version": 1, "selected_id": [], "profiles": []},
                                   {"version": 1, "selected_id": None, "profiles": [[]]}])
def test_malformed_registry_is_rejected_with_fixed_error(profiles, invalid):
    _, service, config = profiles
    service.execute("profile_list", {})
    (config.storage.data_dir / "profiles.json").write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(MaintenanceError) as failure:
        service.execute("profile_list", {})
    assert failure.value.code == "invalid_config"


def test_legacy_restore_barrier_precedes_first_cleanup(profiles):
    _, service, config = profiles
    config.storage.data_dir.mkdir()
    old = config.storage.data_dir / "workflow.sqlite3"
    old.write_bytes(b"legacy data")
    (config.storage.data_dir / archives.MARKER).write_text("{}")
    with pytest.raises(MaintenanceError) as failure:
        service.execute("profile_list", {})
    assert failure.value.code == "restore_pending"
    assert old.read_bytes() == b"legacy data"


def test_registered_profile_missing_companion_is_not_silently_recreated(profiles):
    _, service, config = profiles
    service.execute("profile_list", {})
    actual = resolve_profile_config(config).storage.data_dir
    (actual / "companion.sqlite3").unlink()
    with pytest.raises(MaintenanceError):
        create_application(config)
    assert not (actual / "companion.sqlite3").exists()
