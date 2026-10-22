"""Pure, clock-injected affect math; version-1 weights and budgets."""

from __future__ import annotations

import math
from typing import Any, Sequence

from .policy import baseline
from .constants import HALF_LIVES, WINDOW_CAPS


def values_at(state: dict[str, Any], now: float) -> list[float]:
    dt = max(0.0, now - state["anchor_utc"])
    base = baseline(state["persona"]["style"])
    return [b + (x - b) * 2 ** (-dt / half) for x, b, half in zip(state["values"], base, HALF_LIVES)]


def outcome_delta(status: str, rows: Sequence[tuple[Any, ...]], now: float) -> list[float]:
    minute = [row for row in rows if row[0] > now - 60]
    raw = (0.08, 0.04, 0.005) if status == "completed" else (-0.08, 0.04, -0.005)
    delta = [0.0, 0.0, 0.0]
    for index in range(3):
        window = rows if index == 2 else minute
        used = sum(abs(row[index + 1]) for row in window)
        delta[index] = math.copysign(min(abs(raw[index]), max(0.0, WINDOW_CAPS[index] - used), 0.15), raw[index])
    return delta
