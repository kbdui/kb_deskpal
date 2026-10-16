"""Small, privacy-scoped GUI preferences backed by QSettings."""

from __future__ import annotations

from PySide6.QtCore import QObject, QSettings, Signal


class GuiPreferences(QObject):
    """Validated GUI-only preferences; never stores backend config or user content."""

    changed = Signal(str, object)

    _KEYS = {
        "geometry": "gui/geometry",
        "screen_name": "gui/screen_name",
        "close_to_tray": "gui/close_to_tray",
        "notifications_enabled": "gui/notifications_enabled",
        "quiet_start": "gui/quiet_start",
        "quiet_end": "gui/quiet_end",
    }

    def __init__(self, settings: QSettings | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self._settings = settings if settings is not None else QSettings()

    def _value(self, name: str, default: object) -> object:
        return self._settings.value(self._KEYS[name], default)

    def _set(self, name: str, value: object) -> None:
        self._settings.setValue(self._KEYS[name], value)
        self._settings.sync()
        self.changed.emit(name, value)

    @property
    def geometry(self) -> bytes:
        raw = self._value("geometry", b"")
        if isinstance(raw, str):
            raw = raw.encode("latin1", errors="ignore")
        return bytes(raw)[:65536] if isinstance(raw, (bytes, bytearray)) else b""

    def set_geometry(self, value: bytes | bytearray) -> bool:
        if not isinstance(value, (bytes, bytearray)) or len(value) > 65536:
            return False
        self._set("geometry", bytes(value))
        return True

    @property
    def screen_name(self) -> str:
        value = self._value("screen_name", "")
        return value[:256] if isinstance(value, str) else ""

    def set_screen_name(self, value: str) -> bool:
        if not isinstance(value, str) or len(value) > 256 or "\x00" in value:
            return False
        self._set("screen_name", value)
        return True

    @property
    def close_to_tray(self) -> bool:
        return self._bool("close_to_tray", False)

    def set_close_to_tray(self, value: bool) -> bool:
        if not isinstance(value, bool):
            return False
        self._set("close_to_tray", value)
        return True

    @property
    def notifications_enabled(self) -> bool:
        return self._bool("notifications_enabled", False)

    def set_notifications_enabled(self, value: bool) -> bool:
        if not isinstance(value, bool):
            return False
        self._set("notifications_enabled", value)
        return True

    def _bool(self, name: str, default: bool) -> bool:
        value = self._value(name, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.casefold()
            if normalized in {"true", "1", "yes"}:
                return True
            if normalized in {"false", "0", "no"}:
                return False
        return default

    @property
    def quiet_start(self) -> int:
        return self._hour("quiet_start", 22)

    @property
    def quiet_end(self) -> int:
        return self._hour("quiet_end", 8)

    def _hour(self, name: str, default: int) -> int:
        raw = self._value(name, default)
        if isinstance(raw, bool):
            return default
        if isinstance(raw, int):
            value = raw
        elif isinstance(raw, str):
            try:
                value = int(raw)
            except ValueError:
                return default
        else:
            return default
        return value if 0 <= value <= 23 else default

    def set_quiet_hours(self, start: int, end: int) -> bool:
        if (isinstance(start, bool) or isinstance(end, bool) or
                not isinstance(start, int) or not isinstance(end, int) or
                not 0 <= start <= 23 or not 0 <= end <= 23):
            return False
        self._settings.setValue(self._KEYS["quiet_start"], start)
        self._settings.setValue(self._KEYS["quiet_end"], end)
        self._settings.sync()
        self.changed.emit("quiet_hours", (start, end))
        return True
