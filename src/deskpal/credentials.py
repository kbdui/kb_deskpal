"""Opt-in Windows Credential Manager; no plaintext fallback or eager vault access."""

import ctypes
import re
import sys
from ctypes import wintypes
from typing import Any

PREFIX = "Deskpal/LLM/"


class CredentialStoreError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("系统凭据库不可用，请使用外部环境变量配置凭据。")


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialStore:
    @staticmethod
    def available() -> bool:
        return sys.platform == "win32"

    @staticmethod
    def valid_ref(reference: str) -> bool:
        return isinstance(reference, str) and re.fullmatch(r"Deskpal/LLM/[0-9a-f]{32}", reference) is not None

    @staticmethod
    def validate_secret(secret: str) -> None:
        try:
            if (not isinstance(secret, str) or not secret.strip()
                    or any(ord(char) < 32 or ord(char) == 127 for char in secret)
                    or len(secret.encode("utf-16-le")) > 2560):
                raise CredentialStoreError()
        except Exception:
            raise CredentialStoreError() from None

    def _api(self, reference: str) -> Any:
        if not self.available() or not self.valid_ref(reference):
            raise CredentialStoreError()
        api = getattr(ctypes, "WinDLL")("advapi32", use_last_error=True)
        api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(ctypes.POINTER(_Credential))]
        api.CredReadW.restype = wintypes.BOOL
        api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
        api.CredWriteW.restype = wintypes.BOOL
        api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        api.CredDeleteW.restype = wintypes.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        api.CredFree.restype = None
        return api

    def read(self, reference: str) -> str | None:
        try:
            api = self._api(reference)
            pointer = ctypes.POINTER(_Credential)()
            if not api.CredReadW(reference, 1, 0, ctypes.byref(pointer)):
                if getattr(ctypes, "get_last_error")() == 1168:
                    return None
                raise CredentialStoreError()
            try:
                value = pointer.contents
                if value.CredentialBlobSize > 2560:
                    raise CredentialStoreError()
                secret = ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize).decode("utf-16-le")
                self.validate_secret(secret)
                return secret
            finally:
                api.CredFree(pointer)
        except Exception:
            raise CredentialStoreError() from None

    def write(self, reference: str, secret: str) -> None:
        try:
            self.validate_secret(secret)
            api = self._api(reference)
            encoded = secret.encode("utf-16-le")
            blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
            value = _Credential()
            value.Type = 1
            value.TargetName = reference
            value.CredentialBlobSize = len(encoded)
            value.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
            value.Persist = 2  # local-machine persistence, protected by the OS account
            value.UserName = "Deskpal"
            if not api.CredWriteW(ctypes.byref(value), 0):
                raise CredentialStoreError()
        except Exception:
            raise CredentialStoreError() from None

    def delete(self, reference: str) -> None:
        try:
            api = self._api(reference)
            if not api.CredDeleteW(reference, 1, 0) and getattr(ctypes, "get_last_error")() != 1168:
                raise CredentialStoreError()
        except Exception:
            raise CredentialStoreError() from None
