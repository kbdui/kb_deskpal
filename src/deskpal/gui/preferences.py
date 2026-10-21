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
        "pet_enabled": "pet/enabled",
        "pet_visible": "pet/visible",
        "pet_on_top": "pet/on_top",
        "pet_click_through": "pet/click_through",
        "pet_x": "pet/x",
        "pet_y": "pet/y",
        "pet_screen": "pet/screen",
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
    def pet_enabled(self) -> bool:
        return self._bool("pet_enabled", False)

    @property
    def pet_visible(self) -> bool:
        return self._bool("pet_visible", True)

    @property
    def pet_on_top(self) -> bool:
        return self._bool("pet_on_top", True)

    @property
    def pet_click_through(self) -> bool:
        return self._bool("pet_click_through", False)

    def _set_pet_bool(self, key: str, value: bool) -> bool:
        if not isinstance(value, bool):
            return False
        self._set(key, value)
        return True

    def set_pet_enabled(self, value: bool) -> bool:
        return self._set_pet_bool("pet_enabled", value)

    def set_pet_visible(self, value: bool) -> bool:
        return self._set_pet_bool("pet_visible", value)

    def set_pet_on_top(self, value: bool) -> bool:
        return self._set_pet_bool("pet_on_top", value)

    def set_pet_click_through(self, value: bool) -> bool:
        return self._set_pet_bool("pet_click_through", value)

    @property
    def pet_position(self) -> tuple[int, int]:
        values: list[int] = []
        for key in ("pet_x", "pet_y"):
            raw = self._value(key, 80)
            try:
                value = int(raw) if isinstance(raw, (str, int)) and not isinstance(raw, bool) else 80
            except ValueError:
                value = 80
            values.append(value if abs(value) <= 1_000_000 else 80)
        return values[0], values[1]

    @property
    def pet_screen(self) -> str:
        raw = self._value("pet_screen", "")
        return raw[:256] if isinstance(raw, str) else ""

    def set_pet_position(self, position: tuple[int, int], screen: str) -> bool:
        if (len(position) != 2 or any(type(v) is not int or abs(v) > 1_000_000 for v in position)
                or not isinstance(screen, str) or len(screen) > 256 or "\x00" in screen):
            return False
        self._settings.setValue(self._KEYS["pet_x"], position[0])
        self._settings.setValue(self._KEYS["pet_y"], position[1])
        self._settings.setValue(self._KEYS["pet_screen"], screen)
        self._settings.sync()
        self.changed.emit("pet_position", position)
        return True

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
