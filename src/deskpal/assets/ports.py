"""Ports for the existing workflow connection and an optional image codec."""

from contextlib import AbstractContextManager
import sqlite3
from typing import Protocol

from .models import NormalizedImage


class AssetRepository(Protocol):
    def transaction(self) -> AbstractContextManager[sqlite3.Connection]: ...
    def read_connection(self) -> AbstractContextManager[sqlite3.Connection]: ...


class AssetDecoder(Protocol):
    """Codec checks actual PNG/JPEG content, frame count and predecode size.

    normalize returns fresh RGBA PNG pixels without EXIF/text/ICC metadata.
    thumbnail likewise returns independently owned, metadata-free pixels.
    Implementations must not initialize a GUI/application or contact a server.
    """

    def normalize(self, raw: bytes) -> NormalizedImage: ...
    def thumbnail(self, png: bytes, max_edge: int) -> NormalizedImage: ...
