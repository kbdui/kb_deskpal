"""Permission and filesystem policy for trusted, in-process tools only."""

import re
import os
import tempfile
import stat
from pathlib import Path, PureWindowsPath

from deskpal.tools.models import PermissionLevel, ToolContext, ToolSpec

from .errors import PathViolation, PermissionDenied


_DEVICE = re.compile(r"^(CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.IGNORECASE)


class WorkspaceSandbox:
    """Application policy; it does not isolate malicious plugins or OS races."""

    def __init__(self, workspace_dir: str | Path, *, max_file_bytes: int = 1_048_576) -> None:
        if type(max_file_bytes) is not int or max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be a positive integer")
        self._root = Path(workspace_dir).resolve()
        if self._root.exists() and not self._root.is_dir():
            raise ValueError("Workspace root must be a directory")
        self._max_file_bytes = max_file_bytes

    @property
    def workspace_dir(self) -> Path:
        return self._root

    @property
    def max_file_bytes(self) -> int:
        return self._max_file_bytes

    def check_permission(self, spec: ToolSpec, context: ToolContext) -> None:
        try:
            context_root = Path(context.workspace_dir).resolve()
        except (OSError, RuntimeError, ValueError):
            raise PathViolation("Invalid workspace context") from None
        if context_root != self._root:
            raise PathViolation("Tool context does not match the configured workspace")
        for names in (context.authorized_tools, context.high_risk_authorized_tools):
            if not isinstance(names, frozenset) or any(
                    not isinstance(name, str) or not name.strip() for name in names):
                raise PermissionDenied("Invalid authorization context")
        if type(spec.permission_level) not in (int, PermissionLevel) or spec.permission_level not in tuple(PermissionLevel):
            raise PermissionDenied("Tool permission level is not supported")
        if spec.permission_level == PermissionLevel.HIGH_RISK:
            if spec.name not in context.high_risk_authorized_tools:
                raise PermissionDenied(f"Tool {spec.name} requires explicit high-risk authorization")
        elif spec.permission_level == PermissionLevel.CONFIRM and spec.name not in context.authorized_tools:
            raise PermissionDenied(f"Tool {spec.name} requires explicit authorization")

    def _candidate(self, path: str) -> Path:
        """Validate portable spelling before touching the filesystem."""
        if not isinstance(path, str) or not path or "\x00" in path:
            raise PathViolation("File path must be a non-empty relative path")
        windows = PureWindowsPath(path)
        if windows.drive or windows.root or Path(path).is_absolute():
            raise PathViolation("Absolute and drive-qualified paths are forbidden")
        parts = path.replace("\\", "/").split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise PathViolation("Empty, dot and parent path components are forbidden")
        for part in parts:
            if ":" in part or any(char in '<>"|?*' or ord(char) < 32 for char in part):
                raise PathViolation("File path contains a forbidden Windows character")
            if part.endswith((".", " ")) or _DEVICE.match(part):
                raise PathViolation("Windows device names and trailing dots/spaces are forbidden")
        return self._root.joinpath(*parts)

    @staticmethod
    def _is_link(metadata: os.stat_result) -> bool:
        return stat.S_ISLNK(metadata.st_mode) or bool(
            (getattr(metadata, "st_file_attributes", 0) or 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))

    def _resolve(self, candidate: Path, *, directory: bool = False, missing: bool = False) -> Path:
        try:
            current = self._root
            for part in candidate.relative_to(self._root).parts:
                current /= part
                try:
                    metadata = current.lstat()
                except FileNotFoundError:
                    continue
                if self._is_link(metadata):
                    raise PathViolation("Links and junctions are forbidden in workspace paths")
                if current != candidate and not stat.S_ISDIR(metadata.st_mode):
                    raise PathViolation("File parent must be a directory")
            resolved = candidate.resolve()
            if not resolved.is_relative_to(self._root):
                raise PathViolation("File path escapes the configured workspace")
            try:
                metadata = resolved.lstat()
            except FileNotFoundError:
                if not missing:
                    raise FileNotFoundError("Workspace file does not exist") from None
            else:
                correct_kind = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
                if self._is_link(metadata) or not correct_kind:
                    raise PathViolation("Workspace target has an unsupported file type")
            return resolved
        except (RuntimeError, ValueError):
            raise PathViolation("File path cannot be safely resolved") from None

    def resolve_path(self, path: str, *, for_write: bool = False) -> Path:
        """Accept relative paths to regular files; reject every link component."""
        return self._resolve(self._candidate(path), missing=for_write)

    def resolve_directory(self, path: str | None = None) -> Path:
        """An omitted path selects the root; supplied paths must be relative."""
        return self._resolve(self._root if path is None else self._candidate(path), directory=True)

    def read_bytes(self, path: str) -> bytes:
        target = self.resolve_path(path)
        if target.stat().st_size > self._max_file_bytes:
            raise ValueError("File exceeds the configured byte limit")
        if self.resolve_path(path) != target:
            raise PathViolation("File path changed during read")
        with target.open("rb") as stream:
            data = stream.read(self._max_file_bytes + 1)
        if len(data) > self._max_file_bytes:
            raise ValueError("File exceeds the configured byte limit")
        return data

    def read_text(self, path: str) -> str:
        return self.read_bytes(path).decode("utf-8")

    def write_text(self, path: str, text: str, *, overwrite: bool = False) -> dict[str, str | int]:
        return self.write_bytes(path, text.encode("utf-8"), overwrite=overwrite)

    def write_bytes(self, path: str, data: bytes, *, overwrite: bool = False) -> dict[str, str | int]:
        """Stage bytes before publishing; failures preserve any prior file.

        Empty parent directories may remain if a later filesystem operation fails.
        Trusted application policy cannot prevent hostile concurrent OS changes.
        """
        if not isinstance(data, bytes) or type(overwrite) is not bool:
            raise ValueError("Bytes and a boolean overwrite flag are required")
        if len(data) > self._max_file_bytes:
            raise ValueError("File exceeds the configured byte limit")
        target = self.resolve_path(path, for_write=True)
        if target.exists() and not overwrite:
            raise FileExistsError("Workspace file already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.resolve_path(path, for_write=True) != target:
            raise PathViolation("File path changed during write")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".deskpal-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                written = stream.write(data)
                if written != len(data):
                    raise OSError("Incomplete file write")
            if self.resolve_path(path, for_write=True) != target:
                raise PathViolation("File path changed during write")
            if overwrite:
                temporary.replace(target)
            else:
                # Atomic no-clobber publication; unsupported hardlinks fail closed.
                os.link(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return {"path": target.relative_to(self._root).as_posix(), "bytes_written": written}

    def copy_file(self, source: str, destination: str, *, overwrite: bool = False) -> dict[str, str | int]:
        origin = self.resolve_path(source)
        target = self.resolve_path(destination, for_write=True)
        if origin == target or (target.exists() and origin.samefile(target)):
            raise ValueError("Source and destination must differ")
        data = self.read_bytes(source)
        if self.resolve_path(source) != origin or self.resolve_path(destination, for_write=True) != target:
            raise PathViolation("File path changed during copy")
        output = self.write_bytes(destination, data, overwrite=overwrite)
        return {"source": origin.relative_to(self._root).as_posix(), **output}

    def move_file(self, source: str, destination: str, *, overwrite: bool = False) -> dict[str, str | int | bool]:
        """Single-file move. If source removal fails, both paths remain."""
        origin = self.resolve_path(source)
        target = self.resolve_path(destination, for_write=True)
        if origin == target or (target.exists() and origin.samefile(target)) or type(overwrite) is not bool:
            raise ValueError("Distinct paths and a boolean overwrite flag are required")
        size = len(self.read_bytes(source))
        if target.exists() and not overwrite:
            raise FileExistsError("Workspace file already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.resolve_path(source) != origin or self.resolve_path(destination, for_write=True) != target:
            raise PathViolation("File path changed during move")
        output: dict[str, str | int | bool] = {
            "source": origin.relative_to(self._root).as_posix(),
            "path": target.relative_to(self._root).as_posix(), "bytes_moved": size,
        }
        if overwrite:
            origin.replace(target)
        else:
            os.link(origin, target)
            try:
                if self.resolve_path(source) != origin:
                    raise PathViolation("File path changed during move")
                origin.unlink()
            except OSError:
                return {**output, "completed": False, "destination_created": True, "source_removed": False}
        return {**output, "completed": True}

    def delete_file(self, path: str) -> dict[str, str | int]:
        target = self.resolve_path(path)
        size = len(self.read_bytes(path))
        if self.resolve_path(path) != target:
            raise PathViolation("File path changed during delete")
        target.unlink()
        return {"path": target.relative_to(self._root).as_posix(), "bytes_deleted": size}

    def search_files(self, path: str | None = None, *, query: str = "", limit: int = 100,
                     max_entries: int = 10_000, max_depth: int = 32) -> dict[str, object]:
        """Bounded deterministic discovery, without opening contents or links."""
        if not isinstance(query, str) or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid search query or result limit")
        if type(max_entries) is not int or max_entries <= 0 or type(max_depth) is not int or max_depth < 0:
            raise ValueError("Invalid search traversal limits")
        start = self.resolve_directory(path)
        pending = [(start, 0)]
        found: list[dict[str, str | int]] = []
        scanned = 0
        truncated = False
        while pending:
            directory, depth = pending.pop()
            relative = directory.relative_to(self._root).as_posix()
            if self.resolve_directory(None if relative == "." else relative) != directory:
                raise PathViolation("Search directory changed")
            entries = []
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    scanned += 1
                    if scanned > max_entries:
                        raise ValueError("Search traversal limit exceeded; narrow the directory")
                    entries.append(entry)
            for entry in sorted(entries, key=lambda item: item.name):
                try:
                    metadata = entry.stat(follow_symlinks=False)
                    if self._is_link(metadata):
                        continue
                    entry_path = Path(entry.path)
                    relative = entry_path.relative_to(self._root).as_posix()
                    if stat.S_ISDIR(metadata.st_mode):
                        self.resolve_directory(relative)
                        if depth < max_depth:
                            pending.append((entry_path, depth + 1))
                        else:
                            truncated = True
                    elif stat.S_ISREG(metadata.st_mode) and query.casefold() in entry.name.casefold():
                        self.resolve_path(relative)
                        found.append({"path": relative, "bytes": metadata.st_size})
                except (FileNotFoundError, PathViolation):
                    # Concurrently removed or unsafe entries are never followed.
                    continue
        found.sort(key=lambda item: str(item["path"]))
        return {"files": found[:limit], "truncated": truncated or len(found) > limit,
                "scanned_entries": scanned}
