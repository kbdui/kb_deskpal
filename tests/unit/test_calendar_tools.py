"""Calendar tools exercise production schema and permission routing."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from deskpal.sandbox.errors import PermissionDenied
from deskpal.sandbox.workspace import WorkspaceSandbox
from deskpal.scheduling import SchedulingService
from deskpal.tools.calendar import calendar_tools
from deskpal.tools.manager import BuiltinToolManager
from deskpal.tools.models import PermissionLevel, ToolCall, ToolContext


class FixedClock:
    def now(self):
        return datetime(2026, 10, 13, 9, tzinfo=timezone.utc)

    def monotonic(self):
        return 100.0


@pytest.fixture
def calendar(tmp_path):
    service = SchedulingService(tmp_path / "calendar.db", clock=FixedClock())
    manager = BuiltinToolManager(WorkspaceSandbox(tmp_path / "workspace"))
    for tool in calendar_tools(service):
        manager.register(tool)
    context = ToolContext("task", Path(tmp_path / "workspace"),
                          frozenset({"calendar_create", "calendar_update", "calendar_delete"}))
    yield service, manager, context
    service.close()


def test_calendar_registration_and_authorization(calendar):
    service, manager, context = calendar
    levels = {spec.name: spec.permission_level for spec in manager.list_tools()}
    assert levels == {"calendar_create": PermissionLevel.CONFIRM, "calendar_update": PermissionLevel.CONFIRM,
                      "calendar_delete": PermissionLevel.CONFIRM, "calendar_list": PermissionLevel.AUTOMATIC,
                      "calendar_events": PermissionLevel.AUTOMATIC}
    args = {"title": "Meet", "start_at": "2026-10-13T09:00:00Z", "timezone": "UTC"}
    with pytest.raises(PermissionDenied):
        manager.execute(ToolCall("calendar_create", args), ToolContext("task", context.workspace_dir))
    assert service.list_calendar() == ()


def test_calendar_create_list_update_delete(calendar):
    service, manager, context = calendar
    result = manager.execute(ToolCall("calendar_create", {
        "title": "Meet", "start_at": "2026-10-13T09:00:00Z", "timezone": "UTC",
        "end_at": "2026-10-13T10:00:00Z", "reminder_at": "2026-10-13T08:00:00Z"}), context)
    assert result.success
    entry_id = result.output["id"]
    result = manager.execute(ToolCall("calendar_update", {"id": entry_id, "title": "New", "clear_end": True}), context)
    assert result.success and result.output["end_at"] is None
    listing = manager.execute(ToolCall("calendar_list", {}), ToolContext("task", context.workspace_dir))
    assert listing.success and listing.output[0]["title"] == "New"
    assert service.poll_due() == 1
    events = manager.execute(ToolCall("calendar_events", {}), ToolContext("task", context.workspace_dir))
    assert events.success and events.output[0]["source"] == "calendar"
    cursor = events.output[-1]["id"]
    assert manager.execute(ToolCall("calendar_events", {"after_id": cursor}), context).output == []
    assert manager.execute(ToolCall("calendar_delete", {"id": entry_id}), context).output == {"id": entry_id, "deleted": True}


@pytest.mark.parametrize("args", [
    {"title": "x", "start_at": "2026-10-13T08:00:00", "timezone": "UTC"},
    {"title": "x", "start_at": "2026-10-13T08:00:00Z", "timezone": "Invalid/Name"},
    {"title": "x", "start_at": "2026-10-13T08:00:00Z", "timezone": "UTC", "recurrence": "daily"},
    {"title": "x", "start_at": "2026-10-13T08:00:00Z", "timezone": "UTC", "reminder_at": "2026-10-13T09:00:00Z"},
    {"title": " "},
])
def test_calendar_create_invalid_arguments_no_side_effect(calendar, args):
    service, manager, context = calendar
    assert not manager.execute(ToolCall("calendar_create", args), context).success
    assert service.list_calendar() == ()


def test_update_conflicting_clear_and_empty_change(calendar):
    _, manager, context = calendar
    created = manager.execute(ToolCall("calendar_create", {
        "title": "Meet", "start_at": "2026-10-13T09:00:00Z", "timezone": "UTC"}), context)
    entry_id = created.output["id"]
    for args in ({"id": entry_id}, {"id": entry_id, "clear_reminder": True, "reminder_at": "2026-10-13T08:00:00Z"}):
        assert not manager.execute(ToolCall("calendar_update", args), context).success
