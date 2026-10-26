"""Bounded local import, immutable publication and workflow profile CAS.

The application owns the data-directory instance lock throughout this service's
lifetime. Each service serializes its imports and collection. SQLite remains the
authority for profile revisions; it is never replaced by GUI preferences.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import math
import os
from pathlib import Path
import sqlite3
import stat
from threading import RLock
from time import monotonic
from uuid import uuid4

from .models import AppearanceProfile, AssetError, AssetMetadata, AssetPreview, CollectionResult, NormalizedImage, PreparedImport
from .ports import AssetDecoder, AssetRepository
from .schema import SLOTS

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_EDGE = 2048
MAX_PIXELS = 4_194_304
MAX_GROUP_PIXELS = 8_000_000
MAX_GROUP_BYTES = 20 * 1024 * 1024
MAX_PENDING = 7
MAX_ASSETS = 128
MAX_DIRECTORY_ENTRIES = 256
IMPORT_TTL_SECONDS = 900


def valid_asset_id(value: object) -> bool:
    return isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def safe_path(path: Path, *, regular: bool = False) -> Path:
    absolute = Path(os.path.abspath(path.expanduser()))
    for item in (*reversed(absolute.parents), absolute):
        if os.path.lexists(item):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise AssetError("asset_path_unsafe")
    if regular:
        info = absolute.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise AssetError("asset_path_unsafe")
    return absolute


def recover_imports(data_dir: Path) -> None:
    """Recover private import remnants while the caller owns the instance lock.

    This small file-only operation is shared by production startup and offline
    backup. It never opens a database, starts Qt, or accepts arbitrary hardlinks.
    """
    root = safe_path(Path(data_dir)) / "assets"
    safe_path(root)
    if not root.exists():
        return
    inventory: list[Path] = []
    for path in root.iterdir():
        if len(inventory) >= MAX_DIRECTORY_ENTRIES:
            raise AssetError("asset_storage_capacity")
        inventory.append(path)
    recover: list[Path] = []
    for path in inventory:
        if path.name.startswith(".import-") and path.name.endswith(".tmp") and valid_asset_id(path.name[8:-4]):
            safe_path(path)
            destination = root / (path.name[8:-4] + ".png")
            if os.path.lexists(destination):
                safe_path(destination)
                if not path.is_file() or not destination.is_file() or not path.samefile(destination) or path.stat().st_nlink != 2:
                    raise AssetError("asset_path_unsafe")
            else:
                safe_path(path, regular=True)
            recover.append(path)
        elif path.suffix != ".png" or not valid_asset_id(path.stem):
            raise AssetError("asset_path_unsafe")
    # Validate the complete inventory before removing any interrupted name.
    for path in inventory:
        if path not in recover and root / (".import-" + path.stem + ".tmp") not in recover:
            safe_path(path, regular=True)
    for path in recover:
        path.unlink()


@dataclass(frozen=True)
class _Pending:
    receipt: PreparedImport
    image: NormalizedImage
    expires_at: float


class AssetService:
    def __init__(self, store: AssetRepository, data_dir: Path, *, decoder: AssetDecoder | None = None) -> None:
        self._store = store
        self._root = safe_path(Path(data_dir)) / "assets"
        self._decoder = decoder
        self._lock = RLock()
        self._pending: dict[str, _Pending] = {}
        self._generations: dict[str, int] = {}
        self._recover_imports()

    def set_decoder(self, decoder: AssetDecoder) -> None:
        """One-time GUI composition; replacing a live codec is not a setting."""
        with self._lock:
            if self._decoder is decoder:
                return
            if self._decoder is not None:
                raise AssetError("asset_decoder_already_configured")
            self._decoder = decoder

    def _recover_imports(self) -> None:
        recover_imports(self._root.parent)

    def _codec(self) -> AssetDecoder:
        if self._decoder is None:
            raise AssetError("asset_decoder_unavailable")
        return self._decoder

    def _slot(self, slot: str) -> None:
        if slot not in SLOTS:
            raise AssetError("asset_slot_invalid")

    def _expire(self) -> int:
        expired = [token for token, pending in self._pending.items() if pending.expires_at <= monotonic()]
        for token in expired:
            del self._pending[token]
        return len(expired)

    def _read(self, path: Path) -> bytes:
        try:
            safe_path(path, regular=True)
            if not 0 < path.stat().st_size <= MAX_FILE_BYTES:
                raise AssetError("asset_byte_limit")
            with path.open("rb") as source:
                raw = source.read(MAX_FILE_BYTES + 1)
            if not 0 < len(raw) <= MAX_FILE_BYTES:
                raise AssetError("asset_byte_limit")
            return raw
        except OSError:
            raise AssetError("asset_read_failed") from None

    def _image(self, image: NormalizedImage) -> None:
        if not 0 < len(image.png) <= MAX_FILE_BYTES or not image.png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise AssetError("asset_encoding_invalid")
        if type(image.width) is not int or type(image.height) is not int or not (1 <= image.width <= MAX_EDGE and 1 <= image.height <= MAX_EDGE) or image.width * image.height > MAX_PIXELS:
            raise AssetError("asset_dimension_limit")

    def prepare(self, path: Path, slot: str, *, generation: int) -> PreparedImport:
        """Local file selection only. The source path never leaves this call."""
        self._slot(slot)
        if type(generation) is not int or generation < 0:
            raise AssetError("asset_generation_invalid")
        with self._lock:
            self._expire()
            if generation <= self._generations.get(slot, -1):
                raise AssetError("asset_generation_stale")
            self._generations[slot] = generation
            # A newer selection invalidates earlier previews even if it fails.
            for token in [token for token, item in self._pending.items() if item.receipt.slot == slot]:
                del self._pending[token]
            if len(self._pending) >= MAX_PENDING:
                raise AssetError("asset_import_capacity")
            raw = self._read(Path(path))
            suffix = Path(path).suffix.lower()
            if not ((suffix == ".png" and raw.startswith(b"\x89PNG\r\n\x1a\n")) or (suffix in {".jpg", ".jpeg"} and raw.startswith(b"\xff\xd8"))):
                raise AssetError("asset_format_unsupported")
            image = self._codec().normalize(raw)
            self._image(image)
            if slot.startswith("pet_") and not image.has_visible_pixels:
                raise AssetError("asset_pet_transparent")
            receipt = PreparedImport(uuid4().hex, slot, generation, image.width, image.height, len(image.png), sha256(image.png).hexdigest())
            self._pending[receipt.token] = _Pending(receipt, image, monotonic() + IMPORT_TTL_SECONDS)
            return receipt

    def _metadata(self, row: sqlite3.Row) -> AssetMetadata:
        return AssetMetadata(row["id"], row["media_type"], row["bytes"], row["width"], row["height"], row["sha256"], row["revision"], row["provenance"])

    def snapshot(self) -> tuple[AppearanceProfile, ...]:
        with self._lock, self._store.read_connection() as connection:
            rows = {row["slot"]: row for row in connection.execute("SELECT * FROM appearance_profile")}
            if set(rows) != set(SLOTS):
                raise AssetError("asset_profile_invalid")
            result: list[AppearanceProfile] = []
            for slot in SLOTS:
                row = rows[slot]
                effective = row["asset_id"]
                if effective is None and slot.startswith("pet_") and slot != "pet_main":
                    effective = rows["pet_main"]["asset_id"]
                metadata = None
                if effective is not None:
                    asset = connection.execute("SELECT * FROM asset WHERE id=?", (effective,)).fetchone()
                    if asset is None or not valid_asset_id(effective):
                        raise AssetError("asset_reference_invalid")
                    metadata = self._metadata(asset)
                result.append(AppearanceProfile(slot, row["revision"], row["asset_id"], effective, effective is None, row["display_mode"], (row["crop_x"], row["crop_y"], row["crop_width"], row["crop_height"]), metadata))
            return tuple(result)

    def preview(self, *, token: str | None = None, asset_id: str | None = None, max_edge: int = 480) -> AssetPreview:
        if (token is None) == (asset_id is None) or type(max_edge) is not int or not 1 <= max_edge <= MAX_EDGE:
            raise AssetError("asset_preview_invalid")
        with self._lock:
            self._expire()
            if token is not None:
                pending = self._pending.get(token)
                if pending is None:
                    raise AssetError("asset_import_expired")
                raw = pending.image.png
            else:
                if not valid_asset_id(asset_id):
                    raise AssetError("asset_id_invalid")
                with self._store.read_connection() as connection:
                    row = connection.execute("SELECT * FROM asset WHERE id=?", (asset_id,)).fetchone()
                if row is None:
                    raise AssetError("asset_not_found")
                raw = self._read(self._root / (str(asset_id) + ".png"))
                if len(raw) != row["bytes"] or sha256(raw).hexdigest() != row["sha256"]:
                    raise AssetError("asset_integrity_failed")
            image = self._codec().thumbnail(raw, max_edge)
            self._image(image)
            if max(image.width, image.height) > max_edge:
                raise AssetError("asset_preview_invalid")
            return AssetPreview(image.png, image.width, image.height, asset_id, token)

    def _display(self, slot: str, mode: str, crop: tuple[float, float, float, float] | None) -> tuple[float, float, float, float]:
        allowed = {"contain"} if slot.startswith("pet_") else {"contain", "square", "circle"}
        if mode not in allowed:
            raise AssetError("asset_display_invalid")
        value = crop or (0.0, 0.0, 1.0, 1.0)
        if len(value) != 4 or any(type(number) not in (int, float) or not math.isfinite(number) for number in value):
            raise AssetError("asset_crop_invalid")
        x, y, width, height = value
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
            raise AssetError("asset_crop_invalid")
        return float(x), float(y), float(width), float(height)

    def _publish(self, image: NormalizedImage, asset_id: str) -> None:
        safe_path(self._root)
        self._root.mkdir(mode=0o700, exist_ok=True)
        temporary = self._root / (".import-" + asset_id + ".tmp")
        try:
            with temporary.open("xb") as output:
                output.write(image.png)
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, self._root / (asset_id + ".png"))
        except OSError:
            raise AssetError("asset_publish_failed") from None
        finally:
            temporary.unlink(missing_ok=True)
        if os.name != "nt":
            descriptor = os.open(self._root, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def apply(self, slot: str, token: str, expected_revision: int, *, generation: int,
              display_mode: str = "contain", crop: tuple[float, float, float, float] | None = None) -> AppearanceProfile:
        self._slot(slot)
        region = self._display(slot, display_mode, crop)
        if type(expected_revision) is not int or expected_revision < 1:
            raise AssetError("asset_revision_invalid")
        with self._lock:
            self._expire()
            pending = self._pending.get(token)
            if pending is None:
                raise AssetError("asset_import_expired")
            if pending.receipt.slot != slot or pending.receipt.generation != generation or self._generations.get(slot) != generation:
                raise AssetError("asset_generation_stale")
            asset_id = uuid4().hex
            image = pending.image
            # Normal replacement remains usable indefinitely without requiring
            # the user to discover an orphan-cleanup action at the quota limit.
            self.collect(limit=32)
            with self._store.transaction() as connection:
                row = connection.execute("SELECT revision FROM appearance_profile WHERE slot=?", (slot,)).fetchone()
                if row is None or row[0] != expected_revision:
                    raise AssetError("asset_revision_conflict")
                if connection.execute("SELECT COUNT(*) FROM asset").fetchone()[0] >= MAX_ASSETS:
                    raise AssetError("asset_storage_capacity")
                if slot.startswith("pet_"):
                    group = connection.execute("SELECT SUM(a.width*a.height),SUM(a.bytes) FROM appearance_profile p JOIN asset a ON p.asset_id=a.id WHERE p.slot LIKE 'pet_%' AND p.slot<>?", (slot,)).fetchone()
                    if (group[0] or 0) + image.width * image.height > MAX_GROUP_PIXELS or (group[1] or 0) + len(image.png) > MAX_GROUP_BYTES:
                        raise AssetError("asset_group_limit")
                # Publication precedes the reference. A failed commit leaves an
                # immutable orphan, never a partially active profile.
                try:
                    self._publish(image, asset_id)
                except OSError:
                    raise AssetError("asset_publish_failed") from None
                connection.execute("INSERT INTO asset(id,media_type,bytes,width,height,sha256,revision,provenance,created_at) VALUES(?,'image/png',?,?,?,?,1,'user_import',?)", (asset_id, len(image.png), image.width, image.height, pending.receipt.sha256, datetime.now(timezone.utc).isoformat()))
                connection.execute("UPDATE appearance_profile SET asset_id=?,revision=revision+1,display_mode=?,crop_x=?,crop_y=?,crop_width=?,crop_height=? WHERE slot=? AND revision=?", (asset_id, display_mode, *region, slot, expected_revision))
            del self._pending[token]
            return next(profile for profile in self.snapshot() if profile.slot == slot)

    def reset(self, slot: str, expected_revision: int) -> AppearanceProfile:
        self._slot(slot)
        if type(expected_revision) is not int or expected_revision < 1:
            raise AssetError("asset_revision_invalid")
        with self._lock, self._store.transaction() as connection:
            updated = connection.execute("UPDATE appearance_profile SET asset_id=NULL,revision=revision+1,display_mode='contain',crop_x=0,crop_y=0,crop_width=1,crop_height=1 WHERE slot=? AND revision=?", (slot, expected_revision))
            if updated.rowcount != 1:
                raise AssetError("asset_revision_conflict")
            for token in [token for token, item in self._pending.items() if item.receipt.slot == slot]:
                del self._pending[token]
        return next(profile for profile in self.snapshot() if profile.slot == slot)

    def discard(self, token: str) -> bool:
        with self._lock:
            return self._pending.pop(token, None) is not None

    def collect(self, *, limit: int = 32) -> CollectionResult:
        if type(limit) is not int or not 1 <= limit <= 128:
            raise AssetError("asset_collection_limit")
        with self._lock, self._store.transaction() as connection:
            discarded = self._expire()
            self._recover_imports()
            rows = connection.execute("SELECT id FROM asset WHERE id NOT IN (SELECT asset_id FROM appearance_profile WHERE asset_id IS NOT NULL) ORDER BY id LIMIT ?", (limit + 1,)).fetchall()
            candidates = [row[0] for row in rows[:limit]]
            files = removed = 0
            for asset_id in candidates:
                if not valid_asset_id(asset_id):
                    raise AssetError("asset_id_invalid")
                path = self._root / (asset_id + ".png")
                if os.path.lexists(path):
                    safe_path(path, regular=True)
                    path.unlink()
                    files += 1
                connection.execute("DELETE FROM asset WHERE id=?", (asset_id,))
                removed += 1
            # A crashed publication can have no row. Only fixed generated names
            # are collectible; unknown files, links and directories are refused.
            known = {row[0] for row in connection.execute("SELECT id FROM asset")}
            more = len(rows) > limit
            if self._root.exists():
                safe_path(self._root)
                inventory: list[Path] = []
                for item in self._root.iterdir():
                    if len(inventory) >= MAX_DIRECTORY_ENTRIES:
                        raise AssetError("asset_storage_capacity")
                    inventory.append(item)
                for path in inventory:
                    if path.suffix != ".png" or not valid_asset_id(path.stem):
                        raise AssetError("asset_path_unsafe")
                    safe_path(path, regular=True)
                    if path.stem not in known:
                        if max(removed, files) >= limit:
                            more = True
                            continue
                        path.unlink()
                        files += 1
            return CollectionResult(removed, files, discarded, more)
