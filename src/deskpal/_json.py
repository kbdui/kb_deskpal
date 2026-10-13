"""Shared finite JSON snapshots at orchestration and planning boundaries."""

import math
from collections.abc import Mapping
from typing import Any


def snapshot_json(value: Any, active: set[int] | None = None) -> Any:
    """Detach JSON data from callbacks without accepting cycles or custom values."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if not isinstance(value, Mapping) and type(value) is not list:
        raise ValueError("Data must contain only finite JSON values")
    seen = set() if active is None else active
    identity = id(value)
    if identity in seen:
        raise ValueError("Data must not contain cycles")
    seen.add(identity)
    try:
        if isinstance(value, Mapping):
            if any(type(key) is not str for key in value):
                raise ValueError("Object keys must be strings")
            return {key: snapshot_json(item, seen) for key, item in value.items()}
        return [snapshot_json(item, seen) for item in value]
    finally:
        seen.remove(identity)
