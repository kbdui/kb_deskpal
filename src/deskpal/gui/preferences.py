"""Small, privacy-scoped GUI preferences backed by QSettings."""

from __future__ import annotations

import math

from PySide6.QtCore import QObject, QSettings, Signal


class GuiPreferences(QObject):
    """Validated GUI-only preferences; never stores backend config or user content."""

    changed = Signal(str, object)

    _KEYS = {
        "geometry": "gui/geometry",
        "theme": "gui/theme",
        "screen_name": "gui/screen_name",
        "close_to_tray": "gui/close_to_tray",
        "notifications_enabled": "gui/notifications_enabled",
        "pet_enabled": "pet/enabled",
        "pet_visible": "pet/visible",
        "pet_on_top": "pet/on_top",
        "pet_click_through": "pet/click_through",
        "pet_x": "pet/x",
        "pet_y": "pet/y",
        "pet_screen": "pet/screen",
        "pet_scale": "pet/scale",
        "user_display_name": "gui/user_display_name",
        "character_display_name": "gui/character_display_name",
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
    def theme(self) -> str:
        value = self._value('theme', 'auto')
        return str(value) if value in ('auto', 'light', 'dark') else 'auto'

    def set_theme(self, value: str) -> bool:
        if value not in ('auto', 'light', 'dark'): return False
        self._set('theme', value)
        return True

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
    def user_display_name(self) -> str:
        value = self._value("user_display_name", "你")
        return value.strip()[:32] if isinstance(value, str) and value.strip() else "你"

    def set_user_display_name(self, value: str) -> bool:
        if not isinstance(value, str) or len(value.strip()) > 32 or "\x00" in value:
            return False
        self._set("user_display_name", value.strip() or "你")
        return True

    @property
    def character_display_name(self) -> str:
        value = self._value("character_display_name", "Deskpal")
        return value.strip()[:32] if isinstance(value, str) and value.strip() else "Deskpal"

    def set_character_display_name(self, value: str) -> bool:
        if not isinstance(value, str) or len(value.strip()) > 32 or "\x00" in value:
            return False
        self._set("character_display_name", value.strip() or "Deskpal")
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

    @property
    def pet_scale(self) -> float:
        raw = self._value("pet_scale", 1.0)
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            return 1.0
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            return 1.0
        return value if math.isfinite(value) and .6 <= value <= 1.6 else 1.0

    def set_pet_scale(self, value: float) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            normalized = float(value)
        except (ValueError, OverflowError):
            return False
        if not math.isfinite(normalized) or not .6 <= normalized <= 1.6:
            return False
        self._set("pet_scale", round(normalized, 3))
        return True

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
