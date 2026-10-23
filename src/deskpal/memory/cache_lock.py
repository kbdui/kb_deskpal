"""Nonblocking shared model users / exclusive maintenance ownership."""

import ctypes
from dataclasses import dataclass
import os
import stat
import sys
import threading
from pathlib import Path
from types import TracebackType
from typing import Any

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class CacheLeaseError(RuntimeError):
    """The cache is busy or its lock cannot be safely opened."""


class _Offset(ctypes.Structure):
    _fields_ = [("Offset", ctypes.c_uint32), ("OffsetHigh", ctypes.c_uint32)]


class _Position(ctypes.Union):
    _fields_ = [("offset", _Offset), ("Pointer", ctypes.c_void_p)]


class _Overlapped(ctypes.Structure):
    # ULONG_PTR and HANDLE retain their native width on both Win32 and Win64.
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("position", _Position), ("hEvent", ctypes.c_void_p)]


class _WindowsLock:
    """One synchronous native lock, retained until the last local reader exits."""

    def __init__(self, fd: int, *, exclusive: bool) -> None:
        if sys.platform != "win32":
            raise CacheLeaseError("Windows cache locking is unavailable")
        self.api: Any = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        self.api.LockFileEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                       ctypes.c_uint32, ctypes.c_uint32,
                                       ctypes.POINTER(_Overlapped)]
        self.api.LockFileEx.restype = ctypes.c_int32
        self.api.UnlockFileEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                         ctypes.c_uint32, ctypes.c_uint32,
                                         ctypes.POINTER(_Overlapped)]
        self.api.UnlockFileEx.restype = ctypes.c_int32
        self.handle = msvcrt.get_osfhandle(fd)
        self.overlapped = _Overlapped()  # offset zero, no event, all reserved fields zero
        # os.open uses synchronous handles. FAIL_IMMEDIATELY leaves no pending
        # request; omitting EXCLUSIVE requests an actual cross-process shared lock.
        flags = 0x1 | (0x2 if exclusive else 0)
        if not self.api.LockFileEx(self.handle, flags, 0, 1, 0, ctypes.byref(self.overlapped)):
            raise CacheLeaseError("Cache is busy or unavailable")

    def release(self) -> None:
        # The synchronous acquisition has completed; reuse its exact byte range.
        if not self.api.UnlockFileEx(self.handle, 0, 1, 0, ctypes.byref(self.overlapped)):
            raise CacheLeaseError("Cache lease could not be released")


@dataclass
class _LeaseState:
    fd: int
    count: int
    exclusive: bool
    windows_lock: _WindowsLock | None = None


_registry: dict[str, _LeaseState] = {}
_guard = threading.RLock()


class CacheLease:
    """A persistent sibling lock survives cache directory publication/removal.

    Shared owners in one process reuse one descriptor so releasing one reader
    cannot accidentally release the OS lock held by another reader.
    """

    def __init__(self, cache_dir: str | Path, *, exclusive: bool) -> None:
        self.cache_dir = Path(os.path.normcase(str(Path(cache_dir).expanduser().resolve())))
        self.path = self.cache_dir.parent / ("." + self.cache_dir.name + ".deskpal-cache.lock")
        self.exclusive = exclusive
        self._key = str(self.path)
        self._held = False

    def acquire(self) -> "CacheLease":
        with _guard:
            if self._held:
                raise CacheLeaseError("Cache lease already acquired")
            existing = _registry.get(self._key)
            if existing is not None:
                if existing.exclusive or self.exclusive:
                    raise CacheLeaseError("Cache is busy")
                existing.count += 1
                self._held = True
                return self
            fd: int | None = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.is_symlink() or (self.path.exists() and
                        getattr(self.path.lstat(), "st_file_attributes", 0) & 0x400):
                    raise OSError
                if self.path.exists():
                    info = self.path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise OSError
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
                windows_lock: _WindowsLock | None = None
                if sys.platform == "win32":
                    # LockFileEx supports a range beyond EOF, so readers never
                    # write an initialization byte that could race a shared lock.
                    windows_lock = _WindowsLock(fd, exclusive=self.exclusive)
                else:
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b"\0")
                    os.lseek(fd, 0, os.SEEK_SET)
                    fcntl.flock(fd, (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                _registry[self._key] = _LeaseState(fd, 1, self.exclusive, windows_lock)
                self._held = True
            except BaseException as error:
                if fd is not None:
                    try:
                        os.close(fd)
                    finally:
                        _registry.pop(self._key, None)
                        self._held = False
                if isinstance(error, (OSError, CacheLeaseError, AttributeError, ctypes.ArgumentError)):
                    raise CacheLeaseError("Cache is busy or unavailable") from None
                raise
            return self

    def release(self) -> None:
        with _guard:
            if not self._held:
                return
            state = _registry[self._key]
            if state.count > 1:
                state.count -= 1
                self._held = False
            else:
                try:
                    try:
                        if state.windows_lock is not None:
                            state.windows_lock.release()
                        elif sys.platform != "win32":
                            os.lseek(state.fd, 0, os.SEEK_SET)
                            fcntl.flock(state.fd, fcntl.LOCK_UN)
                        else:
                            raise CacheLeaseError("Windows cache locking is unavailable")
                    finally:
                        try:
                            os.close(state.fd)
                        finally:
                            del _registry[self._key]
                            self._held = False
                except (OSError, AttributeError, ctypes.ArgumentError):
                    raise CacheLeaseError("Cache lease could not be released") from None

    def __enter__(self) -> "CacheLease":
        return self.acquire()

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc_value: BaseException | None, traceback: TracebackType | None) -> None:
        self.release()
