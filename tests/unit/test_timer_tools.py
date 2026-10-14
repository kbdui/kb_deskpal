"""Timer registration, argument validation and non-blocking tool results."""

import time

import pytest

from deskpal.sandbox.errors import PermissionDenied
from deskpal.sandbox.workspace import WorkspaceSandbox
from deskpal.scheduling import SchedulingService
from deskpal.tools.manager import BuiltinToolManager
from deskpal.tools.models import PermissionLevel, ToolCall, ToolContext
from deskpal.tools.timer import timer_tools


@pytest.fixture
def timers(tmp_path):
    service = SchedulingService(tmp_path / "timer.db")
    workspace = tmp_path / "workspace"
    manager = BuiltinToolManager(WorkspaceSandbox(workspace))
    for tool in timer_tools(service):
        manager.register(tool)
    yield service, manager, ToolContext("task", workspace, frozenset({"timer_create", "timer_cancel"}))
    service.close()


def test_timer_permissions_and_create_returns_immediately(timers):
    service, manager, context = timers
    levels = {spec.name: spec.permission_level for spec in manager.list_tools()}
    assert levels == {"timer_create": PermissionLevel.CONFIRM, "timer_cancel": PermissionLevel.CONFIRM,
                      "timer_list": PermissionLevel.AUTOMATIC, "timer_events": PermissionLevel.AUTOMATIC}
    with pytest.raises(PermissionDenied):
        manager.execute(ToolCall("timer_create", {"duration_seconds": 1000}), ToolContext("task", context.workspace_dir))
    assert service.list_timers() == ()
    started = time.monotonic()
    created = manager.execute(ToolCall("timer_create", {"duration_seconds": 1000, "label": "Tea"}), context)
    assert time.monotonic() - started < 1
    assert created.success and created.output["status"] == "pending"
    timer_id = created.output["id"]
    read_context = ToolContext("task", context.workspace_dir)
    assert manager.execute(ToolCall("timer_list", {}), read_context).output[0]["id"] == timer_id
    assert manager.execute(ToolCall("timer_events", {}), read_context).output == []
    cancelled = manager.execute(ToolCall("timer_cancel", {"id": timer_id}), context)
    assert cancelled.output == {"id": timer_id, "cancelled": True}
    assert manager.execute(ToolCall("timer_list", {"status": "cancelled"}), context).output[0]["id"] == timer_id


@pytest.mark.parametrize("name,args", [
    ("timer_create", {"duration_seconds": True}), ("timer_create", {"duration_seconds": 0}),
    ("timer_create", {"duration_seconds": float("inf")}), ("timer_create", {"duration_seconds": 2678401}),
    ("timer_create", {"duration_seconds": 2, "label": " "}), ("timer_create", {"duration_seconds": 2, "callback": "x"}),
    ("timer_list", {"status": "unknown"}), ("timer_list", {"limit": 1001}),
    ("timer_cancel", {"id": ""}), ("timer_events", {"after_id": -1}), ("timer_events", {"consume": True}),
])
def test_timer_invalid_arguments(timers, name, args):
    service, manager, context = timers
    assert not manager.execute(ToolCall(name, args), context).success
    assert service.list_timers() == ()
