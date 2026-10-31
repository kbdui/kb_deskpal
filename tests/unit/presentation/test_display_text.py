"""Typed display summaries never mutate the original values."""
from datetime import datetime, timezone, timedelta

from deskpal.gui.display_text import compact_path, friendly_time


def test_time_has_explicit_zone_and_keeps_invalid_values() -> None:
    original = "2026-10-27T23:04:59.123456+00:00"
    zone = timezone(timedelta(hours=8))
    assert friendly_time(original, timezone=zone) == "2026年10月28日 07:04"
    assert original == "2026-10-27T23:04:59.123456+00:00"
    assert friendly_time("tomorrow after lunch", timezone=zone) == "tomorrow after lunch"
    assert friendly_time(original, timezone="invalid/not-a-zone") == original
    assert friendly_time(None) == "未设置"


def test_time_uses_reference_zone_and_accepts_naive_explicit_field() -> None:
    reference = datetime(2026, 10, 27, tzinfo=timezone(timedelta(hours=-4)))
    assert friendly_time("2026-10-27T12:45:30Z", now=reference) == "2026年10月27日 08:45"
    assert friendly_time("2026-10-27T12:45:30", now=reference) == "2026年10月27日 12:45"


def test_paths_handle_windows_unc_posix_workspace_and_outside_locations() -> None:
    assert compact_path(r"E:\work\project\docs\notes.txt", r"E:\work\project") == r"docs\notes.txt"
    assert compact_path(r"e:\WORK\Project\docs\notes.txt", r"E:\work\project") == r"docs\notes.txt"
    assert compact_path(r"E:\work\project", r"E:\work\project") == "project"
    assert compact_path(r"E:\work\project-other\notes.txt", r"E:\work\project") == r"…\project-other\notes.txt"
    assert compact_path(r"\\server\share\team\notes.txt") == r"…\team\notes.txt"
    assert compact_path("/home/person/project/docs/notes.txt", "/home/person/project") == "docs/notes.txt"
    assert compact_path("/outside/project/docs/notes.txt", "/home/person/project") == "…/docs/notes.txt"
    assert compact_path("notes.txt") == "notes.txt"

