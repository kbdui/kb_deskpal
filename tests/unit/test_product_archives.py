"""Prepared offline archive checks; all data is synthetic and temporary."""

from contextlib import ExitStack
import os
from pathlib import Path
import sqlite3
import zipfile

import pytest

from deskpal.application.instance import InstanceLock
from deskpal.companion import CompanionRuntime
from deskpal.maintenance import archives
from deskpal.maintenance.errors import MaintenanceError
from deskpal.memory.repository import SQLiteMemoryRepository
from deskpal.scheduling import SchedulingService
from deskpal.workflow import WorkflowStore


@pytest.fixture
def source(tmp_path):
    directory = tmp_path / "synthetic-data"
    directory.mkdir()
    with ExitStack() as owned:
        for store in (WorkflowStore(directory / "workflow.sqlite3"),
                      SQLiteMemoryRepository(directory / "memory.sqlite3"),
                      SchedulingService(directory / "scheduling.sqlite3"),
                      CompanionRuntime(directory / "companion.sqlite3")):
            owned.callback(store.close)
    return directory


def backup(source, tmp_path):
    path = tmp_path / "snapshot.zip"
    archives.create_backup(source, path, acknowledge_sensitive_data=True)
    return path


def rewrite(source, destination, change):
    with zipfile.ZipFile(source) as bundle:
        entries = [(info, bundle.read(info)) for info in bundle.infolist()]
    entries = change(entries)
    with zipfile.ZipFile(destination, "w") as bundle:
        for info, data in entries:
            bundle.writestr(info, data)


def assert_code(code, action):
    with pytest.raises(MaintenanceError) as caught:
        action()
    assert caught.value.code == code


def test_roundtrip_has_four_verified_stores_and_explicit_scope(source, tmp_path):
    path = backup(source, tmp_path)
    receipt = archives.inspect_backup(path)
    assert receipt["manifest"]["scope"] == "four_sqlite_stores_only"
    assert receipt["manifest"]["exclusions"] == archives.EXCLUSIONS
    assert receipt["manifest"]["hashes_prove_authenticity"] is False
    assert [entry["name"] for entry in receipt["manifest"]["databases"]] == list(archives.DATABASES)
    original = {name: (source / name).read_bytes() for name in archives.DATABASES}
    restored = tmp_path / "new-data"
    result = archives.restore_backup(path, restored, acknowledge_data_rollback=True, current_data_dir=source)
    assert result["status"] == "restored"
    assert result["privacy_deletion_completed"] is False
    assert not (restored / archives.MARKER).exists()
    archives.assert_restore_ready(restored)
    for entry in receipt["manifest"]["databases"]:
        assert archives._digest(restored / entry["name"]) == entry["sha256"]
        assert (source / entry["name"]).read_bytes() == original[entry["name"]]
    assert archives.inspect_backup(path) == receipt


def test_sensitive_and_rollback_ack_are_mandatory(source, tmp_path):
    path = tmp_path / "backup.zip"
    assert_code("archive_sensitive_ack_required", lambda: archives.create_backup(source, path))
    assert not path.exists()
    backup(source, tmp_path)
    assert_code("archive_rollback_ack_required", lambda: archives.restore_backup(tmp_path / "snapshot.zip", tmp_path / "new"))
    assert not (tmp_path / "new").exists()


def test_required_stores_missing_does_not_initialize_any_database(tmp_path):
    directory = tmp_path / "uninitialized"
    directory.mkdir()
    assert_code("archive_source_uninitialized", lambda: archives.create_backup(directory, tmp_path / "a.zip", acknowledge_sensitive_data=True))
    assert list(directory.iterdir()) == []


def test_missing_companion_is_explicit_but_corrupt_companion_is_rejected(source, tmp_path):
    path = source / "companion.sqlite3"
    path.unlink()
    receipt = archives.create_backup(source, tmp_path / "without.zip", acknowledge_sensitive_data=True)
    assert receipt["manifest"]["databases"][-1] == {"name": path.name, "present": False, "bytes": 0, "sha256": None, "schema_version": None}
    path.write_bytes(b"synthetic-corrupt-sqlite")
    assert_code("archive_invalid", lambda: archives.create_backup(source, tmp_path / "corrupt.zip", acknowledge_sensitive_data=True))
    assert not (tmp_path / "corrupt.zip").exists()


def test_running_instance_blocks_backup_and_current_restore(source, tmp_path):
    path = backup(source, tmp_path)
    with InstanceLock(source):
        assert_code("archive_instance_busy", lambda: archives.create_backup(source, tmp_path / "busy.zip", acknowledge_sensitive_data=True))
        assert_code("archive_instance_busy", lambda: archives.restore_backup(path, tmp_path / "new", acknowledge_data_rollback=True, current_data_dir=source))
    assert not (tmp_path / "new").exists()


def test_committed_wal_is_included(source, tmp_path):
    connection = sqlite3.connect(source / "memory.sqlite3")
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("INSERT INTO memory_meta VALUES ('synthetic_wal_record', 47)")
        connection.commit()
        assert Path(str(source / "memory.sqlite3") + "-wal").exists()
        path = backup(source, tmp_path)
        target = tmp_path / "restored"
        archives.restore_backup(path, target, acknowledge_data_rollback=True)
        with sqlite3.connect(target / "memory.sqlite3") as restored:
            assert restored.execute("SELECT value FROM memory_meta WHERE name='synthetic_wal_record'").fetchone() == (47,)
    finally:
        connection.close()


def test_unknown_schema_is_not_migrated(source, tmp_path):
    with sqlite3.connect(source / "memory.sqlite3") as connection:
        connection.execute("PRAGMA user_version=9001")
    assert_code("archive_schema_unsupported", lambda: archives.create_backup(source, tmp_path / "a.zip", acknowledge_sensitive_data=True))
    with sqlite3.connect(source / "memory.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9001


@pytest.mark.parametrize("name", ["../outside", "/absolute", "folder/file", "WORKFLOW.SQLITE3", "unknown.txt"])
def test_zip_names_are_whitelisted(source, tmp_path, name):
    path = backup(source, tmp_path)
    hostile = tmp_path / "hostile.zip"
    def change(entries):
        return entries[:-1] + [(zipfile.ZipInfo(name), b"hostile")]
    rewrite(path, hostile, change)
    assert_code("archive_invalid", lambda: archives.inspect_backup(hostile))
    target = tmp_path / "new"
    assert_code("archive_invalid", lambda: archives.restore_backup(hostile, target, acknowledge_data_rollback=True))
    assert not target.exists()


@pytest.mark.parametrize("kind", ["duplicate", "case_duplicate", "symlink", "extra", "hash", "json_duplicate"])
def test_hostile_zip_structure_and_hashes(source, tmp_path, kind):
    path = backup(source, tmp_path)
    hostile = tmp_path / "hostile.zip"
    def change(entries):
        if kind in ("duplicate", "case_duplicate"):
            info = zipfile.ZipInfo("manifest.json" if kind == "duplicate" else "MANIFEST.JSON")
            return entries[:-1] + [(info, entries[0][1])]
        if kind == "symlink":
            entries[-1][0].create_system = 3
            entries[-1][0].external_attr = 0o120777 << 16
        elif kind == "extra":
            entries[-1][0].extra = b"\x01\x00\x00\x00"
        elif kind == "hash":
            info, raw = entries[-1]
            entries[-1] = (info, raw[:-1] + bytes([raw[-1] ^ 1]))
        else:
            info, raw = entries[0]
            entries[0] = (info, b'{"format_version":1,' + raw[1:])
        return entries
    rewrite(path, hostile, change)
    assert_code("archive_invalid", lambda: archives.inspect_backup(hostile))


def test_limits_are_checked_before_restoring(source, tmp_path, monkeypatch):
    path = backup(source, tmp_path)
    monkeypatch.setattr(archives, "MAX_DATABASE_BYTES", 1024)
    assert_code("archive_limit_exceeded", lambda: archives.inspect_backup(path))
    assert not (tmp_path / "new").exists()


def test_existing_destination_and_existing_empty_target_are_preserved(source, tmp_path):
    path = backup(source, tmp_path)
    original = path.read_bytes()
    assert_code("archive_destination_exists", lambda: archives.create_backup(source, path, acknowledge_sensitive_data=True))
    assert path.read_bytes() == original
    target = tmp_path / "existing"
    target.mkdir()
    assert_code("restore_resume_required", lambda: archives.restore_backup(path, target, acknowledge_data_rollback=True))
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("phase", ["marker_written", "staged", "published:workflow.sqlite3", "published:companion.sqlite3", "before_marker_clear"])
def test_interruptions_block_startup_and_resume_without_changing_source(source, tmp_path, phase):
    path = backup(source, tmp_path)
    original = path.read_bytes()
    target = tmp_path / "new"
    def fail(current):
        if current == phase:
            raise RuntimeError("synthetic interruption")
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        archives.restore_backup(path, target, acknowledge_data_rollback=True, fault_hook=fail)
    assert_code("restore_pending", lambda: archives.assert_restore_ready(target))
    result = archives.restore_backup(path, target, acknowledge_data_rollback=True, resume=True)
    assert result["status"] == "restored"
    assert path.read_bytes() == original
    archives.assert_restore_ready(target)


def test_resume_recovers_create_only_link_before_stage_unlink(source, tmp_path):
    path = backup(source, tmp_path)
    target = tmp_path / "new"
    def stop(phase):
        if phase == "staged":
            raise RuntimeError("interrupt")
    with pytest.raises(RuntimeError):
        archives.restore_backup(path, target, acknowledge_data_rollback=True, fault_hook=stop)
    os.link(target / archives.STAGE / "workflow.sqlite3", target / "workflow.sqlite3")
    assert archives.restore_backup(path, target, acknowledge_data_rollback=True, resume=True)["status"] == "restored"


def test_resume_when_configuration_already_points_to_pending_target(source, tmp_path):
    path = backup(source, tmp_path)
    target = tmp_path / "configured-pending"
    def stop(phase):
        if phase == "published:workflow.sqlite3":
            raise RuntimeError("interrupt")
    with pytest.raises(RuntimeError):
        archives.restore_backup(path, target, acknowledge_data_rollback=True, fault_hook=stop)
    result = archives.restore_backup(path, target, acknowledge_data_rollback=True,
                                    resume=True, current_data_dir=target)
    assert result["status"] == "restored"
    archives.assert_restore_ready(target)


@pytest.mark.parametrize("state", ["missing", "uninitialized", "corrupt", "pending"])
def test_unusable_current_directory_does_not_block_restore(source, tmp_path, state):
    path = backup(source, tmp_path)
    current = tmp_path / "unusable-current"
    if state != "missing":
        current.mkdir()
    if state == "corrupt":
        (current / "workflow.sqlite3").write_bytes(b"synthetic corrupt database")
    elif state == "pending":
        (current / archives.MARKER).write_text("unrelated malformed marker", encoding="ascii")
    target = tmp_path / "restored"
    result = archives.restore_backup(path, target, acknowledge_data_rollback=True,
                                    current_data_dir=current)
    assert result["status"] == "restored"
    if state == "missing":
        assert not current.exists()
    elif state == "corrupt":
        assert (current / "workflow.sqlite3").read_bytes() == b"synthetic corrupt database"
    elif state == "pending":
        assert (current / archives.MARKER).read_text(encoding="ascii") == "unrelated malformed marker"


def test_pending_target_owned_by_running_instance_still_blocks_resume(source, tmp_path):
    path = backup(source, tmp_path)
    target = tmp_path / "configured-pending"
    def stop(phase):
        if phase == "marker_written":
            raise RuntimeError("interrupt")
    with pytest.raises(RuntimeError):
        archives.restore_backup(path, target, acknowledge_data_rollback=True, fault_hook=stop)
    with InstanceLock(target):
        assert_code("archive_instance_busy", lambda: archives.restore_backup(
            path, target, acknowledge_data_rollback=True, resume=True, current_data_dir=target))
    assert (target / archives.MARKER).exists()


@pytest.mark.parametrize("boundary", ["before_link", "before_unlink"])
def test_resume_recovers_first_marker_publication_interruption(source, tmp_path, boundary):
    path = backup(source, tmp_path)
    target = tmp_path / "new"
    target.mkdir()
    receipt = archives.inspect_backup(path)
    pending = target / (archives.MARKER + ".tmp")
    pending.write_bytes(archives._json(archives._marker(receipt, [])))
    if boundary == "before_unlink":
        os.link(pending, target / archives.MARKER)
    assert_code("restore_pending", lambda: archives.assert_restore_ready(target))
    assert archives.restore_backup(path, target, acknowledge_data_rollback=True, resume=True)["status"] == "restored"


@pytest.mark.parametrize("change", ["published", "extra", "marker", "archive"])
def test_resume_refuses_changed_state(source, tmp_path, change):
    path = backup(source, tmp_path)
    target = tmp_path / "new"
    def stop(phase):
        if phase == "published:workflow.sqlite3":
            raise RuntimeError("interrupt")
    with pytest.raises(RuntimeError):
        archives.restore_backup(path, target, acknowledge_data_rollback=True, fault_hook=stop)
    if change == "published":
        (target / "workflow.sqlite3").write_bytes(b"user change")
    elif change == "extra":
        (target / "user-notes.txt").write_text("preserve me", encoding="utf-8")
    elif change == "marker":
        (target / archives.MARKER).write_text("{}", encoding="ascii")
    else:
        with zipfile.ZipFile(path, "a") as bundle:
            bundle.comment = b"different archive fingerprint"
    with pytest.raises(MaintenanceError):
        archives.restore_backup(path, target, acknowledge_data_rollback=True, resume=True)
    assert (target / archives.MARKER).exists()
    if change == "published":
        assert (target / "workflow.sqlite3").read_bytes() == b"user change"
    if change == "extra":
        assert (target / "user-notes.txt").read_text(encoding="utf-8") == "preserve me"


def test_marker_removal_failure_is_pending(source, tmp_path, monkeypatch):
    path = backup(source, tmp_path)
    target = tmp_path / "new"
    unlink = Path.unlink
    def fail_marker(self, *args, **kwargs):
        if self == target / archives.MARKER:
            raise PermissionError("synthetic private text must not escape")
        return unlink(self, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_marker)
    assert_code("restore_pending", lambda: archives.restore_backup(path, target, acknowledge_data_rollback=True))
    assert (target / archives.MARKER).exists()


def test_resume_refuses_deletion_of_previously_committed_database(source, tmp_path):
    path = backup(source, tmp_path)
    target = tmp_path / "new"
    def stop(phase):
        # The workflow marker update has completed before memory publication.
        if phase == "published:memory.sqlite3":
            raise RuntimeError("interrupt")
    with pytest.raises(RuntimeError):
        archives.restore_backup(path, target, acknowledge_data_rollback=True, fault_hook=stop)
    (target / "workflow.sqlite3").unlink()
    assert_code("restore_conflict", lambda: archives.restore_backup(path, target, acknowledge_data_rollback=True, resume=True))
    assert not (target / "workflow.sqlite3").exists()


def test_corrupt_sqlite_with_updated_hash_still_fails_quick_check(source, tmp_path):
    path = backup(source, tmp_path)
    hostile = tmp_path / "hostile.zip"
    def change(entries):
        import hashlib
        import json
        info, raw = entries[-1]
        corrupt = b"not sqlite" + raw[10:]
        entries[-1] = (info, corrupt)
        manifest_info, manifest_raw = entries[0]
        manifest = json.loads(manifest_raw)
        manifest["databases"][-1]["sha256"] = hashlib.sha256(corrupt).hexdigest()
        entries[0] = (manifest_info, json.dumps(manifest).encode("utf-8"))
        return entries
    rewrite(path, hostile, change)
    assert_code("archive_invalid", lambda: archives.inspect_backup(hostile))


def test_symlink_parent_and_source_target_overlap_are_rejected(source, tmp_path):
    assert_code("archive_path_unsafe", lambda: archives.create_backup(source, source / "backup.zip", acknowledge_sensitive_data=True))
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("This platform does not permit synthetic symlink creation")
    assert_code("archive_path_unsafe", lambda: archives.create_backup(alias, tmp_path / "a.zip", acknowledge_sensitive_data=True))


@pytest.mark.parametrize("name", [archives.MARKER, archives.MARKER + ".tmp", archives.STAGE])
def test_any_partial_restore_artifact_blocks_startup(tmp_path, name):
    directory = tmp_path / "data"
    directory.mkdir()
    (directory / name).write_text("malformed marker", encoding="ascii")
    assert_code("restore_pending", lambda: archives.assert_restore_ready(directory))
