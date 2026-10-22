"""Version-one affect constants, unchanged by interaction support."""

from __future__ import annotations

import math
from typing import Any

SCHEMA_VERSION = 1
REDUCER_VERSION = 1
RECEIPT_LIMIT = 100_000
PERSONA_HISTORY_LIMIT = 32
MINUTE_COUNT_LIMIT = 32
HOUR_COUNT_LIMIT = 256
MAX_OFFLINE_SECONDS = 30 * 24 * 3600
WINDOW_CAPS = (0.25, 0.25, 0.05)
HALF_LIVES = (300.0, 120.0, 7200.0)


def finite(value: Any, low: float, high: float) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and low <= value <= high
    except OverflowError:
        return False
