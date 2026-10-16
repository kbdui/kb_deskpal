"""Lifetime ownership of a canonical data directory using an OS-held lock.

The persistent lock file is deliberately never removed: replacing its inode while
another process holds it would create two independent owners.
"""

import os
import sys
import threading
from pathlib import Path
from types import TracebackType

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class InstanceLockError(RuntimeError):
    """The data directory cannot be exclusively owned by this instance."""


_held_paths: set[str] = set()
_registry_lock = threading.Lock()


class InstanceLock:
    """Nonblocking exclusive ownership; acquire before initializing any database.

    Keep this object alive and release it only after all users of storage stop.
    An OS process exit (including ``os._exit``) also releases the descriptor lock.
    Construction resolves aliases without requiring the directory to exist.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(os.path.normcase(str(Path(data_dir).expanduser().resolve())))
        self.path = self.data_dir / ".instance.lock"
        self._key = str(self.data_dir)
        self._fd: int | None = None

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    def acquire(self) -> "InstanceLock":
        with _registry_lock:
            if self._fd is not None or self._key in _held_paths:
                raise InstanceLockError("Data directory is already owned by a running instance")
            fd: int | None = None
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
                # Windows byte-range locking needs a stable byte at offset zero.
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                if sys.platform == "win32":
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException as error:
                if fd is not None:
                    os.close(fd)
                if isinstance(error, OSError):
                    raise InstanceLockError("Cannot acquire exclusive data directory ownership") from None
                raise
            self._fd = fd
            _held_paths.add(self._key)
        return self

    def release(self) -> None:
        with _registry_lock:
            fd = self._fd
            if fd is None:
                return
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if sys.platform == "win32":
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
                self._fd = None
                _held_paths.discard(self._key)

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc_value: BaseException | None, traceback: TracebackType | None) -> None:
        self.release()


__all__ = ["InstanceLock", "InstanceLockError"]
