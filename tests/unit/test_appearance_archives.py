"""Prepared asset-format/archive crash checks; deliberately not run in development."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import sqlite3
import struct
import zipfile
import zlib

import pytest

from deskpal.assets import AssetService, NormalizedImage
from deskpal.companion import CompanionRuntime
from deskpal.maintenance import archives
from deskpal.maintenance.errors import MaintenanceError
from deskpal.memory.repository import SQLiteMemoryRepository
from deskpal.scheduling import SchedulingService
from deskpal.workflow import WorkflowStore


def image_bytes():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0\x11\x22\x33\xff")) + chunk(b"IEND", b"")


class Decoder:
    def normalize(self, raw):
        return NormalizedImage(image_bytes(), 1, 1, True)

    def thumbnail(self, raw, max_edge):
        return self.normalize(raw)


def stores(directory, *, legacy=False):
    directory.mkdir()
    with ExitStack() as owned:
        if legacy:
            # Frozen genuine schema-2 fixture, never a new-store constructor
            # followed by a pretend version downgrade.
            from deskpal.workflow.store import _SCHEMA
            from deskpal.workflow.migrations import migrate_schema_1
            with sqlite3.connect(directory / "workflow.sqlite3") as connection:
                for statement in _SCHEMA:
                    connection.execute(statement)
                migrate_schema_1(connection)
        else:
            workflow = WorkflowStore(directory / "workflow.sqlite3")
            owned.callback(workflow.close)
            source = directory.parent / "selection.png"
            source.write_bytes(image_bytes())
            service = AssetService(workflow, directory, decoder=Decoder())
            selected = service.prepare(source, "pet_main", generation=1)
            service.apply("pet_main", selected.token, 1, generation=1)
        for store in (SQLiteMemoryRepository(directory / "memory.sqlite3"), SchedulingService(directory / "scheduling.sqlite3"), CompanionRuntime(directory / "companion.sqlite3")):
            owned.callback(store.close)


def test_format2_roundtrip_includes_verified_managed_image_and_profile(tmp_path):
    source = tmp_path / "source"
    stores(source)
    archive = tmp_path / "snapshot.zip"
    created = archives.create_backup(source, archive, acknowledge_sensitive_data=True)
    assert created["format_version"] == 2
    assert len(created["manifest"]["assets"]) == 1
    entry = created["manifest"]["assets"][0]
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.read(entry["name"]) == image_bytes()
        assert not any("credential" in name for name in bundle.namelist())
    target = tmp_path / "restored"
    archives.restore_backup(archive, target, acknowledge_data_rollback=True)
    assert (target / entry["name"]).read_bytes() == image_bytes()
    with ExitStack() as owned:
        workflow = WorkflowStore(target / "workflow.sqlite3")
        owned.callback(workflow.close)
        service = AssetService(workflow, target, decoder=Decoder())
        assert {value.slot: value for value in service.snapshot()}["pet_main"].asset_id == Path(entry["name"]).stem


@pytest.mark.parametrize("external_link", [False, True])
def test_offline_backup_recovers_only_its_interrupted_import_pair(tmp_path, external_link):
    source = tmp_path / "source"
    stores(source)
    current = next((source / "assets").glob("*.png"))
    temporary = current.with_name(".import-" + current.stem + ".tmp")
    os.link(current, temporary)
    if external_link:
        os.link(current, tmp_path / "external.png")
        with pytest.raises(MaintenanceError) as rejected:
            archives.create_backup(source, tmp_path / "rejected.zip", acknowledge_sensitive_data=True)
        assert rejected.value.code == "archive_invalid"
        assert temporary.exists()
    else:
        result = archives.create_backup(source, tmp_path / "recovered.zip", acknowledge_sensitive_data=True)
        assert not temporary.exists()
        assert current.read_bytes() == image_bytes()
        assert len(result["manifest"]["assets"]) == 1


def test_format1_four_store_archive_restores_and_schema3_initializes_default_appearance(tmp_path):
    source = tmp_path / "legacy"
    stores(source, legacy=True)
    modern = tmp_path / "modern.zip"
    receipt = archives.create_backup(source, modern, acknowledge_sensitive_data=True)
    legacy = tmp_path / "legacy.zip"
    with zipfile.ZipFile(modern) as original, zipfile.ZipFile(legacy, "w") as output:
        for info in original.infolist():
            raw = original.read(info)
            if info.filename == "manifest.json":
                raw = json.dumps(archives._manifest(receipt["manifest"]["databases"])).encode()
            output.writestr(info, raw)
    assert archives.inspect_backup(legacy)["format_version"] == 1
    target = tmp_path / "restored"
    archives.restore_backup(legacy, target, acknowledge_data_rollback=True)
    workflow = WorkflowStore(target / "workflow.sqlite3")
    try:
        assert all(profile.builtin for profile in AssetService(workflow, target).snapshot())
    finally:
        workflow.close()


@pytest.mark.parametrize("damage", ["hash", "path", "missing"])
def test_asset_corruption_and_uncontrolled_names_fail_before_restore_target(tmp_path, damage):
    source = tmp_path / "source"
    stores(source)
    archive = tmp_path / "snapshot.zip"
    archives.create_backup(source, archive, acknowledge_sensitive_data=True)
    hostile = tmp_path / "hostile.zip"
    with zipfile.ZipFile(archive) as original, zipfile.ZipFile(hostile, "w") as output:
        for info in original.infolist():
            raw = original.read(info)
            if info.filename.startswith("assets/"):
                if damage == "missing":
                    continue
                if damage == "hash":
                    raw = raw[:-1] + bytes([raw[-1] ^ 1])
                else:
                    info.filename = "assets/../escape.png"
            output.writestr(info, raw)
    target = tmp_path / "restored"
    with pytest.raises(MaintenanceError) as rejected:
        archives.restore_backup(hostile, target, acknowledge_data_rollback=True)
    assert rejected.value.code == "archive_invalid"
    assert not target.exists()


def test_interruption_after_image_publication_stays_blocked_until_same_archive_resume(tmp_path):
    source = tmp_path / "source"
    stores(source)
    archive = tmp_path / "snapshot.zip"
    archives.create_backup(source, archive, acknowledge_sensitive_data=True)
    target = tmp_path / "restored"
    def stop(phase):
        if phase.startswith("published:assets/"):
            raise RuntimeError("synthetic interruption")
    with pytest.raises(RuntimeError):
        archives.restore_backup(archive, target, acknowledge_data_rollback=True, fault_hook=stop)
    with pytest.raises(MaintenanceError) as blocked:
        archives.assert_restore_ready(target)
    assert blocked.value.code == "restore_pending"
    assert archives.restore_backup(archive, target, acknowledge_data_rollback=True, resume=True)["status"] == "restored"
    archives.assert_restore_ready(target)
