"""Small, Qt-free summaries for explicitly typed display fields.

Callers retain the original value in their detail/copy surface. These helpers
must not be applied to arbitrary user messages or model responses.
"""

from datetime import datetime, tzinfo
from pathlib import PurePosixPath, PureWindowsPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def friendly_time(value: object, now: datetime | None = None,
                  timezone: str | tzinfo | None = None) -> str:
    """Show a timestamp to the minute in the specified or local timezone.

    ``now`` provides a deterministic timezone reference for callers/tests. No
    timestamp is rewritten when parsing or timezone selection fails.
    """
    raw = str(value) if value is not None else "未设置"
    try:
        instant = value if isinstance(value, datetime) else datetime.fromisoformat(raw.strip())
        reference = now if now is not None else datetime.now().astimezone()
        target = ZoneInfo(timezone) if isinstance(timezone, str) else timezone or reference.tzinfo
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=target)
        local = instant.astimezone(target)
        return local.strftime("%Y年%m月%d日 %H:%M")
    except (TypeError, ValueError, OverflowError, ZoneInfoNotFoundError):
        return raw


def compact_path(value: object, workspace: str = "") -> str:
    """Summarize an explicit path lexically, without reading the filesystem."""
    raw = str(value) if value is not None else "未设置"
    if not raw:
        return raw
    windows = bool(PureWindowsPath(raw).drive) or "\\" in raw
    path = PureWindowsPath(raw) if windows else PurePosixPath(raw)
    separator = "\\" if windows else "/"
    if workspace:
        base = PureWindowsPath(workspace) if windows else PurePosixPath(workspace)
        try:
            relative = path.relative_to(base)
            if not relative.parts:
                return base.name or str(base)
            raw = str(relative)
            path = relative
        except ValueError:
            pass
    if not path.is_absolute() and len(raw) <= 56:
        return raw
    parts = [part for part in path.parts if part != path.anchor]
    if not parts:
        return str(path)
    tail = separator.join(parts[-2:])
    return ("…" + separator if path.is_absolute() or len(parts) > 2 else "") + tail
