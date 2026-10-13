"""Permission and filesystem policy for trusted, in-process tools only."""

import re
import os
import tempfile
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
        if type(spec.permission_level) not in (int, PermissionLevel) or spec.permission_level not in tuple(PermissionLevel):
            raise PermissionDenied("Tool permission level is not supported")
        if spec.permission_level != PermissionLevel.AUTOMATIC and spec.name not in context.authorized_tools:
            raise PermissionDenied(f"Tool {spec.name} requires explicit authorization")

    def resolve_path(self, path: str, *, for_write: bool = False) -> Path:
        """Accept relative portable paths only, checking actual resolved membership."""
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
        candidate = self._root.joinpath(*parts)
        try:
            resolved = candidate.resolve()
            if not resolved.is_relative_to(self._root):
                raise PathViolation("File path escapes the configured workspace")
            if resolved.exists() and not resolved.is_file():
                raise PathViolation("File target must be a regular file")
            if not for_write and not resolved.exists():
                raise FileNotFoundError("Workspace file does not exist")
            return resolved
        except (RuntimeError, ValueError):
            raise PathViolation("File path cannot be safely resolved") from None

    def read_text(self, path: str) -> str:
        target = self.resolve_path(path)
        if target.stat().st_size > self._max_file_bytes:
            raise ValueError("File exceeds the configured byte limit")
        with target.open("rb") as stream:
            data = stream.read(self._max_file_bytes + 1)
        if len(data) > self._max_file_bytes:
            raise ValueError("File exceeds the configured byte limit")
        return data.decode("utf-8")

    def write_text(self, path: str, text: str, *, overwrite: bool = False) -> dict[str, str | int]:
        data = text.encode("utf-8")
        if len(data) > self._max_file_bytes:
            raise ValueError("Text exceeds the configured byte limit")
        target = self.resolve_path(path, for_write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Recheck after parent creation; exclusive creation avoids accidental overwrite.
        target = self.resolve_path(path, for_write=True)
        if overwrite:
            # A failed write must leave an existing file intact. Replacement is
            # atomic on the same filesystem; this is not a multi-tool transaction.
            descriptor, temporary_name = tempfile.mkstemp(prefix=".deskpal-", dir=target.parent)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    written = stream.write(data)
                    if written != len(data):
                        raise OSError("Incomplete file write")
                if self.resolve_path(path, for_write=True) != target:
                    raise PathViolation("File path changed during write")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        else:
            with target.open("xb") as stream:
                written = stream.write(data)
                if written != len(data):
                    raise OSError("Incomplete file write")
        return {"path": target.relative_to(self._root).as_posix(), "bytes_written": written}
