"""Bundled schedule page and adapter for durable reminder occurrences."""

from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

from .manifest import SCHEDULE
from .runtime import PageSpec, PageSwitch, PluginData, PluginRuntime


def register_schedule_page(runtime: PluginRuntime, reminder_detail: Callable[[str], dict[str, Any]]) -> None:
    runtime.register_page(SCHEDULE.id, PageSpec(
        switches=(PageSwitch("timer_only", "仅显示计时器到期记录"),),
        rows=partial(_recent_occurrences, reminder_detail),
    ))


def _recent_occurrences(reminder_detail: Callable[[str], dict[str, Any]],
                        data: PluginData, preferences: Mapping[str, bool]) -> list[dict[str, str]]:
    sources = [("timer_due", "计时器到期")]
    if not preferences["timer_only"]:
        sources.append(("calendar_due", "日程提醒到期"))
    entries = [
        (event, title)
        for source, title in sources
        for event in data.events(source, limit=20)
    ]
    entries.sort(key=lambda entry: entry[0]["sequence"], reverse=True)
    rows = []
    for event, kind in entries[:20]:
        # Read the current reminder label through its existing data boundary.
        # The event log does not retain private label text.
        try:
            reminder = reminder_detail(event["event_id"]).get("item")
        except KeyError:
            reminder = None
        title = str(reminder.get("label", ""))[:240] if reminder else ""
        row = {"title": title or kind, "occurred_at": event["occurred_at"]}
        if title:
            row["text"] = kind
        rows.append(row)
    return rows


def record_schedule_occurrence(data: PluginData, source: str, identity: str, occurred_at: str) -> None:
    # Only the occurrence identifier is retained in the plugin stream.
    data.emit(f"{source}_due", identity, occurred_at, {"reminder_id": identity})
