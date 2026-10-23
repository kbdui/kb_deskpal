"""Offline four-store snapshots and guarded, resumable restore to a new directory.

Hashes detect corruption, not authenticity. Archives contain private application
records, not workspace files/configuration/credentials or a complete machine image.
Restoring an older archive can resurrect forgotten data; it is not privacy erasure.
No store constructor, migration, model, Core or executor is used here.
"""

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import stat
import struct
import tempfile
from typing import Any, BinaryIO, IO
import zipfile
import zlib

from deskpal.application.instance import InstanceLock, InstanceLockError
from .errors import MaintenanceError


DATABASES = ("workflow.sqlite3", "memory.sqlite3", "scheduling.sqlite3", "companion.sqlite3")
MARKER = ".restore-in-progress.json"
STAGE = ".restore-stage"
MAX_DATABASE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 16384
CHUNK = 1024 * 1024
SCOPE = "four_sqlite_stores_only"
EXCLUSIONS = ["workspace_files", "configuration", ".env", "credentials", "model_cache", "logs"]
# Frozen, recognized on-disk versions. Reading these never performs a migration.
VERSIONS = {DATABASES[0]: {1, 2}, DATABASES[1]: {1, 2}, DATABASES[2]: {1}, DATABASES[3]: {1, 2}}
_TABLES = {
    DATABASES[0]: {"schema_version", "task", "submission", "immutableplan", "attempt", "audit_outbox", "deletionjob", "memory_block"},
    DATABASES[1]: {"memory_meta", "memory_sources", "memory_revisions", "memory_heads", "memory_tombstones"},
    DATABASES[2]: {"calendar", "timers", "events", "scheduling_identity"},
    DATABASES[3]: {"state", "persona_versions", "receipts", "budget"},
}


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except MaintenanceError:
        raise
    except InstanceLockError:
        raise MaintenanceError("archive_instance_busy") from None
    except FileExistsError:
        raise MaintenanceError("archive_destination_exists") from None
    except (sqlite3.Error, zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, UnicodeError, ValueError, KeyError, TypeError, EOFError, RecursionError, OverflowError):
        raise MaintenanceError("archive_invalid") from None
    except OSError:
        raise MaintenanceError("archive_io_error") from None


def _safe(path: Path) -> Path:
    """Reject symlinks/junctions in the lexical path before resolving aliases."""
    absolute = Path(os.path.abspath(path.expanduser()))
    for part in (*reversed(absolute.parents), absolute):
        if os.path.lexists(part):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise MaintenanceError("archive_path_unsafe")
    return absolute.resolve()


def _separate(first: Path, second: Path) -> None:
    if first == second or first in second.parents or second in first.parents:
        raise MaintenanceError("archive_path_unsafe")


def _regular(path: Path) -> None:
    _safe(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise MaintenanceError("archive_path_unsafe")


def _digest(path: Path) -> str:
    result = sha256()
    with path.open("rb") as source:
        while block := source.read(CHUNK):
            result.update(block)
    return result.hexdigest()


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def _decode(raw: bytes) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise MaintenanceError("archive_invalid")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def _lock(path: Path) -> InstanceLock:
    if os.path.lexists(path / ".instance.lock"):
        _regular(path / ".instance.lock")
    return InstanceLock(path)


@contextmanager
def _reader(path: Path, *, snapshot: bool = False) -> Iterator[sqlite3.Connection]:
    _regular(path)
    # WAL sidecars are SQLite input too: never follow an attacker supplied link.
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if os.path.lexists(sidecar):
            if snapshot:
                raise MaintenanceError("archive_invalid")
            _regular(sidecar)
    connection = sqlite3.connect(path.as_uri() + ("?mode=ro&immutable=1" if snapshot else "?mode=ro"), uri=True, timeout=0)
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def _schema(connection: sqlite3.Connection, name: str) -> int:
    if connection.execute("PRAGMA quick_check(1)").fetchall() != [("ok",)]:
        raise MaintenanceError("archive_invalid")
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if name == DATABASES[0]:
        if "schema_version" not in tables:
            raise MaintenanceError("archive_schema_unsupported")
        versions = connection.execute("SELECT version FROM schema_version LIMIT 2").fetchall()
        if len(versions) != 1 or type(versions[0][0]) is not int:
            raise MaintenanceError("archive_schema_unsupported")
        version = versions[0][0]
    else:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in VERSIONS[name]:
        raise MaintenanceError("archive_schema_unsupported")
    required = _TABLES[name]
    if name == DATABASES[1] and version == 1:
        required = {"memories"}
    if not required <= tables:
        raise MaintenanceError("archive_schema_unsupported")
    if name == DATABASES[0] and version == 2 and not {"privacy_control", "context_snapshot", "task_call_attempt", "companion_effect_outbox"} <= tables:
        raise MaintenanceError("archive_schema_unsupported")
    if name == DATABASES[3] and version == 2 and not {"interaction_settings", "command_receipts"} <= tables:
        raise MaintenanceError("archive_schema_unsupported")
    return int(version)


def _entry(path: Path, name: str) -> dict[str, Any]:
    size = path.stat().st_size
    if size < 1 or size > MAX_DATABASE_BYTES:
        raise MaintenanceError("archive_limit_exceeded")
    with _reader(path, snapshot=True) as connection:
        version = _schema(connection, name)
    return {"name": name, "present": True, "bytes": size, "sha256": _digest(path), "schema_version": version}


def _manifest(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"format_version": 1, "scope": SCOPE, "exclusions": EXCLUSIONS.copy(), "databases": entries,
            "contains_sensitive_data": True, "hashes_prove_authenticity": False,
            "rollback_may_restore_forgotten_data": True}


def _validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(_manifest([])):
        raise MaintenanceError("archive_invalid")
    fixed = _manifest([])
    for key in fixed.keys() - {"databases"}:
        if type(value[key]) is not type(fixed[key]) or value[key] != fixed[key]:
            raise MaintenanceError("archive_invalid")
    entries = value["databases"]
    if not isinstance(entries, list) or len(entries) != 4:
        raise MaintenanceError("archive_invalid")
    total = 0
    for name, entry in zip(DATABASES, entries):
        if not isinstance(entry, dict) or set(entry) != {"name", "present", "bytes", "sha256", "schema_version"} or entry["name"] != name or type(entry["present"]) is not bool:
            raise MaintenanceError("archive_invalid")
        if not entry["present"]:
            if name != DATABASES[3] or entry != {"name": name, "present": False, "bytes": 0, "sha256": None, "schema_version": None}:
                raise MaintenanceError("archive_invalid")
            continue
        size, digest, version = entry["bytes"], entry["sha256"], entry["schema_version"]
        if type(size) is not int or not 0 < size <= MAX_DATABASE_BYTES:
            raise MaintenanceError("archive_limit_exceeded")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise MaintenanceError("archive_invalid")
        if type(version) is not int or version not in VERSIONS[name]:
            raise MaintenanceError("archive_schema_unsupported")
        total += size
    if total > MAX_TOTAL_BYTES:
        raise MaintenanceError("archive_limit_exceeded")
    return value


def _copy_bounded(source: IO[bytes], target: BinaryIO, size: int) -> str:
    count, digest = 0, sha256()
    while block := source.read(min(CHUNK, size - count + 1)):
        count += len(block)
        if count > size:
            raise MaintenanceError("archive_limit_exceeded")
        target.write(block)
        digest.update(block)
    if count != size:
        raise MaintenanceError("archive_invalid")
    target.flush()
    os.fsync(target.fileno())
    return digest.hexdigest()


def _inspect(archive: Path, stage: Path) -> dict[str, Any]:
    _regular(archive)
    if archive.stat().st_size > MAX_TOTAL_BYTES + 1024 * 1024:
        raise MaintenanceError("archive_limit_exceeded")
    # Bound central-directory allocation *before* ZipFile parses its entries.
    # Our bounded format never needs ZIP64 or a multi-disk archive.
    with archive.open("rb") as file:
        file.seek(max(0, archive.stat().st_size - 65557))
        tail = file.read(65557)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise MaintenanceError("archive_invalid")
    _, disk, directory_disk, disk_count, count, directory_bytes, _, comment_bytes = struct.unpack("<4s4H2LH", tail[offset:offset + 22])
    if disk or directory_disk or disk_count != count or count > 5 or directory_bytes > MAX_MANIFEST_BYTES or len(tail) - offset != 22 + comment_bytes:
        raise MaintenanceError("archive_invalid")
    digest = _digest(archive)
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        names = [entry.filename for entry in infos]
        if len(infos) > 5 or len({name.casefold() for name in names}) != len(names):
            raise MaintenanceError("archive_invalid")
        if "manifest.json" not in names or not set(names) <= {"manifest.json", *DATABASES}:
            raise MaintenanceError("archive_invalid")
        for info in infos:
            mode = (info.external_attr >> 16) & 0xFFFF
            if info.orig_filename != info.filename or info.extra or info.flag_bits & 1 or info.is_dir() or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)) or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise MaintenanceError("archive_invalid")
            limit = MAX_MANIFEST_BYTES if info.filename == "manifest.json" else MAX_DATABASE_BYTES
            if info.file_size > limit:
                raise MaintenanceError("archive_limit_exceeded")
        with bundle.open("manifest.json") as source:
            raw = source.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise MaintenanceError("archive_limit_exceeded")
        manifest = _validate_manifest(_decode(raw))
        expected = {"manifest.json"} | {entry["name"] for entry in manifest["databases"] if entry["present"]}
        if set(names) != expected:
            raise MaintenanceError("archive_invalid")
        for entry in manifest["databases"]:
            if not entry["present"]:
                continue
            name = entry["name"]
            if bundle.getinfo(name).file_size != entry["bytes"]:
                raise MaintenanceError("archive_invalid")
            path = stage / name
            with bundle.open(name) as source, path.open("xb") as target:
                actual = _copy_bounded(source, target, entry["bytes"])
            if actual != entry["sha256"] or _entry(path, name) != entry:
                raise MaintenanceError("archive_invalid")
    # Detect changes to the archive between fingerprinting and extraction.
    if _digest(archive) != digest:
        raise MaintenanceError("archive_invalid")
    return {"format_version": 1, "archive_path": str(archive), "archive_sha256": digest, "manifest": manifest}


def inspect_backup(archive: Path) -> dict[str, Any]:
    """Validate an untrusted archive completely without opening application stores."""
    with _errors(), tempfile.TemporaryDirectory(prefix="deskpal-inspect-") as directory:
        return _inspect(_safe(Path(archive)), Path(directory))


def assert_restore_ready(data_dir: Path) -> None:
    """Startup barrier. Any full/partial restore marker is fail-closed."""
    with _errors():
        path = _safe(Path(data_dir))
        if path.exists() and any(item.name.startswith(".restore-") for item in path.iterdir()):
            raise MaintenanceError("restore_pending")


def create_backup(data_dir: Path, destination: Path, *, acknowledge_sensitive_data: bool = False) -> dict[str, Any]:
    """Snapshot four stores while holding the same exclusive lock as CLI/GUI."""
    if acknowledge_sensitive_data is not True:
        raise MaintenanceError("archive_sensitive_ack_required")
    with _errors():
        source, target = _safe(Path(data_dir)), _safe(Path(destination))
        _separate(source, target)
        if not source.is_dir() or any(not (source / name).is_file() for name in DATABASES[:3]):
            raise MaintenanceError("archive_source_uninitialized")
        if os.path.lexists(target):
            raise MaintenanceError("archive_destination_exists")
        if not target.parent.is_dir():
            raise MaintenanceError("archive_path_unsafe")
        with _lock(source), tempfile.TemporaryDirectory(prefix=".deskpal-backup-", dir=target.parent) as directory:
            assert_restore_ready(source)
            stage = Path(directory)
            entries: list[dict[str, Any]] = []
            for name in DATABASES:
                path = source / name
                if name == DATABASES[3] and not os.path.lexists(path):
                    entries.append({"name": name, "present": False, "bytes": 0, "sha256": None, "schema_version": None})
                    continue
                with _reader(path) as connection:
                    _schema(connection, name)
                    pages = connection.execute("PRAGMA page_count").fetchone()[0]
                    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
                    if pages * page_size > MAX_DATABASE_BYTES:
                        raise MaintenanceError("archive_limit_exceeded")
                    output = sqlite3.connect(stage / name)
                    try:
                        def progress(status: int, remaining: int, total: int) -> None:
                            if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                                raise MaintenanceError("archive_instance_busy")
                            if total * page_size > MAX_DATABASE_BYTES:
                                raise MaintenanceError("archive_limit_exceeded")
                        connection.backup(output, pages=256, progress=progress, sleep=0)
                    finally:
                        output.close()
                entries.append(_entry(stage / name, name))
                if sum(entry["bytes"] for entry in entries) > MAX_TOTAL_BYTES:
                    raise MaintenanceError("archive_limit_exceeded")
            manifest = _validate_manifest(_manifest(entries))
            temporary = stage / "archive.zip"
            with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_STORED, allowZip64=False) as bundle:
                bundle.writestr("manifest.json", _json(manifest))
                for entry in entries:
                    if entry["present"]:
                        bundle.write(stage / entry["name"], entry["name"])
            with temporary.open("r+b") as file:
                os.fsync(file.fileno())
            digest = _digest(temporary)
            os.link(temporary, target)  # Atomic create-only publication; never replace.
            _sync_directory(target.parent)
        return {"format_version": 1, "archive_path": str(target), "archive_sha256": digest, "manifest": manifest}


def _marker(receipt: dict[str, Any], published: list[str]) -> dict[str, Any]:
    return {"format_version": 1, "archive_sha256": receipt["archive_sha256"],
            "databases": receipt["manifest"]["databases"], "phase": "publishing", "published": published}


def _write_marker(target: Path, value: dict[str, Any], *, initial: bool = False) -> None:
    temporary = target / (MARKER + ".tmp")
    # An interrupted marker update can be safely discarded only after resume has
    # validated the committed marker and the exact directory inventory.
    with temporary.open("xb") as output:
        output.write(_json(value))
        output.flush()
        os.fsync(output.fileno())
    if initial:
        os.link(temporary, target / MARKER)
        temporary.unlink()
    else:
        os.replace(temporary, target / MARKER)
    _sync_directory(target)


def _inventory(target: Path, receipt: dict[str, Any]) -> None:
    entries = receipt["manifest"]["databases"]
    present = {entry["name"]: entry for entry in entries if entry["present"]}
    allowed = {".instance.lock", MARKER, MARKER + ".tmp", STAGE, *present}
    for path in target.iterdir():
        if path.name not in allowed:
            raise MaintenanceError("restore_conflict")
        if path.name == STAGE:
            _safe(path)
            if not path.is_dir():
                raise MaintenanceError("restore_conflict")
            for item in path.iterdir():
                if item.name not in present and item.name not in {name + ".part" for name in present}:
                    raise MaintenanceError("restore_conflict")
                _regular(item)
        else:
            _regular(path)
            if path.name in present and _entry(path, path.name) != present[path.name]:
                raise MaintenanceError("restore_conflict")


def _finish_interrupted_links(target: Path, receipt: dict[str, Any]) -> None:
    """Recover a kill between create-only publication and staging unlink.

    Two hardlinks are accepted only when both are the precise owned stage and
    destination of the same entry; external hardlinks remain forbidden.
    """
    present = {entry["name"]: entry for entry in receipt["manifest"]["databases"] if entry["present"]}
    allowed = {".instance.lock", MARKER, MARKER + ".tmp", STAGE, *present}
    # Check unknown paths and published bytes before touching staging links.
    for path in target.iterdir():
        _safe(path)
        if path.name not in allowed:
            raise MaintenanceError("restore_conflict")
        if path.name == STAGE:
            if not path.is_dir():
                raise MaintenanceError("restore_conflict")
            for item in path.iterdir():
                _safe(item)
                if item.name not in present and item.name not in {name + ".part" for name in present}:
                    raise MaintenanceError("restore_conflict")
        elif path.name in present:
            entry = present[path.name]
            if not path.is_file() or path.stat().st_size != entry["bytes"] or _digest(path) != entry["sha256"]:
                raise MaintenanceError("restore_conflict")
    for entry in receipt["manifest"]["databases"]:
        if not entry["present"]:
            continue
        destination, staged = target / entry["name"], target / STAGE / entry["name"]
        if not os.path.lexists(destination) or not os.path.lexists(staged):
            continue
        _safe(destination)
        _safe(staged)
        if destination.is_file() and staged.is_file() and destination.samefile(staged) and destination.stat().st_nlink == 2:
            staged.unlink()


def restore_backup(archive: Path, target_dir: Path, *, acknowledge_data_rollback: bool = False,
                   resume: bool = False, current_data_dir: Path | None = None,
                   fault_hook: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Restore only to a new directory, or explicitly resume our matching marker.

    ``fault_hook`` is a test seam; phases contain fixed names and no record data.
    Interruption after the first marker leaves startup blocked until resumed.
    Existing application recovery owns plan review, privacy cleanup and grants.
    """
    if acknowledge_data_rollback is not True:
        raise MaintenanceError("archive_rollback_ack_required")
    hook = fault_hook or (lambda phase: None)
    with _errors(), ExitStack() as owned:
        source, target = _safe(Path(archive)), _safe(Path(target_dir))
        _separate(source, target)
        if not target.parent.is_dir():
            raise MaintenanceError("archive_path_unsafe")
        if current_data_dir is not None:
            current = _safe(Path(current_data_dir))
            if current == target:
                if not resume:
                    raise MaintenanceError("archive_path_unsafe")
                # Configuration may already point at this interrupted restore.
                # The target lock below supplies ownership exactly once.
            else:
                _separate(current, target)
                if current.exists():
                    if not current.is_dir():
                        raise MaintenanceError("archive_path_unsafe")
                    # The current directory is an ownership gate, not an input
                    # to restoration: missing/broken/Pending stores are allowed.
                    owned.enter_context(_lock(current))
        # Validate everything before claiming the new destination.
        temporary = owned.enter_context(tempfile.TemporaryDirectory(prefix="deskpal-restore-"))
        verified = Path(temporary)
        receipt = _inspect(source, verified)
        if os.path.lexists(target):
            if not resume:
                raise MaintenanceError("restore_resume_required")
            if not target.is_dir() or not any((target / name).is_file() for name in (MARKER, MARKER + ".tmp")):
                raise MaintenanceError("restore_conflict")
        else:
            if resume:
                raise MaintenanceError("restore_conflict")
            target.mkdir(mode=0o700, exist_ok=False)
        owned.enter_context(_lock(target))
        if resume:
            committed, pending = target / MARKER, target / (MARKER + ".tmp")
            marker_path = committed if committed.exists() else pending
            _safe(marker_path)
            linked_marker = committed.exists() and pending.exists() and committed.samefile(pending) and committed.stat().st_nlink == 2
            if not linked_marker:
                _regular(marker_path)
            with marker_path.open("rb") as marker_file:
                raw = marker_file.read(MAX_MANIFEST_BYTES + 1)
            if len(raw) > MAX_MANIFEST_BYTES:
                raise MaintenanceError("restore_conflict")
            marker = _decode(raw)
            baseline = _marker(receipt, [])
            if not isinstance(marker, dict) or set(marker) != set(baseline) or any(_json(marker[key]) != _json(baseline[key]) for key in baseline.keys() - {"published"}):
                raise MaintenanceError("restore_conflict")
            published = marker["published"]
            expected_names = {entry["name"] for entry in receipt["manifest"]["databases"] if entry["present"]}
            if not isinstance(published, list) or any(not isinstance(name, str) or name not in expected_names for name in published) or len(published) != len(set(published)):
                raise MaintenanceError("restore_conflict")
            if any(not (target / name).is_file() for name in published):
                raise MaintenanceError("restore_conflict")
            if marker_path == pending:
                # A complete first marker may have been flushed but not yet
                # published. Nothing else may have reached this new directory.
                if published or {item.name for item in target.iterdir()} != {".instance.lock", MARKER + ".tmp"}:
                    raise MaintenanceError("restore_conflict")
                os.link(pending, committed)
                pending.unlink()
                _sync_directory(target)
            elif linked_marker:
                _safe(pending)
                pending.unlink()
            _finish_interrupted_links(target, receipt)
            _inventory(target, receipt)
            (target / (MARKER + ".tmp")).unlink(missing_ok=True)
        else:
            if {item.name for item in target.iterdir()} != {".instance.lock"}:
                raise MaintenanceError("restore_conflict")
            _write_marker(target, _marker(receipt, []), initial=True)
        hook("marker_written")
        _inventory(target, receipt)
        stage = target / STAGE
        stage.mkdir(mode=0o700, exist_ok=True)
        for entry in receipt["manifest"]["databases"]:
            if not entry["present"] or (target / entry["name"]).exists():
                continue
            name = entry["name"]
            staged, part = stage / name, stage / (name + ".part")
            # Only fixed, marker-owned private staging files can be rebuilt.
            for path in (staged, part):
                if os.path.lexists(path):
                    _regular(path)
                    path.unlink()
            with (verified / name).open("rb") as input_file, part.open("xb") as output:
                if _copy_bounded(input_file, output, entry["bytes"]) != entry["sha256"]:
                    raise MaintenanceError("archive_invalid")
            os.rename(part, staged)
        _sync_directory(stage)
        hook("staged")
        published = []
        for entry in receipt["manifest"]["databases"]:
            if not entry["present"]:
                continue
            name = entry["name"]
            destination = target / name
            if not destination.exists():
                os.link(stage / name, destination)
                (stage / name).unlink()
                _sync_directory(target)
            if _entry(destination, name) != entry:
                raise MaintenanceError("restore_conflict")
            published.append(name)
            hook("published:" + name)
            _write_marker(target, _marker(receipt, published))
        _inventory(target, receipt)
        # Remove only known staging files. No recursive removal of a target.
        for item in stage.iterdir():
            _regular(item)
            item.unlink()
        stage.rmdir()
        hook("before_marker_clear")
        _inventory(target, receipt)
        try:
            (target / MARKER).unlink()
            _sync_directory(target)
        except OSError:
            # If unlink succeeded but its durability barrier failed, reinstate
            # the fail-closed marker where possible and never report success.
            if not os.path.lexists(target / MARKER):
                try:
                    _write_marker(target, _marker(receipt, published), initial=True)
                except (OSError, MaintenanceError):
                    pass
            raise MaintenanceError("restore_pending") from None
        return {**receipt, "status": "restored", "target_dir": str(target),
                "startup_review_required": True, "privacy_deletion_completed": False}
