"""Structured local audit logs with recursive credential-key redaction."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Mapping, Protocol

_SECRET_MARKERS = ("apikey", "authorization", "password", "secret", "accesstoken", "refreshtoken", "credential")


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            result[str(key)] = "[REDACTED]" if normalized == "token" or any(marker in normalized for marker in _SECRET_MARKERS) else redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


class ExecutionLog(Protocol):
    def record(self, event: str, task_id: str | None = None, data: Mapping[str, Any] | None = None) -> None: ...


class NullExecutionLog:
    def record(self, event: str, task_id: str | None = None, data: Mapping[str, Any] | None = None) -> None:
        pass


class JsonlExecutionLog:
    """Each instance serializes append operations; no multiprocess locking."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = Lock()

    def record(self, event: str, task_id: str | None = None, data: Mapping[str, Any] | None = None) -> None:
        if not isinstance(event, str) or not event.strip():
            raise ValueError("Log event must be a non-empty string")
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "task_id": task_id,
            "data": redact({} if data is None else data),
        }
        line = json.dumps(entry, ensure_ascii=False, allow_nan=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")


__all__ = ["ExecutionLog", "NullExecutionLog", "JsonlExecutionLog", "redact"]
