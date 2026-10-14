"""Non-blocking local Timer tools; no tool starts or owns threads."""

from typing import Any, Mapping

from deskpal.scheduling.ports import SchedulingManager
from deskpal.scheduling.service import MAX_DURATION_SECONDS

from .calendar import _SchedulingTool, _event_schema, _schema, _string
from .models import PermissionLevel, ToolContext, ToolSpec


class TimerCreateTool(_SchedulingTool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("timer_create", "Create a local countdown without waiting for it. Duration is 1 to 2678400 seconds. Due events are recorded by the running scheduler, and overdue timers are recovered on restart; this is not a notification service.",
                        _schema({"duration_seconds": {"type": "number", "minimum": 1, "maximum": MAX_DURATION_SECONDS},
                                 "label": _string()}, ("duration_seconds",)), PermissionLevel.CONFIRM)

    def _execute(self, args: Mapping[str, Any], context: ToolContext) -> Any:
        return self._service.create_timer(args["duration_seconds"], label=args.get("label", "Timer"),
                                          task_id=context.task_id).as_dict()


class TimerListTool(_SchedulingTool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("timer_list", "List local timers ordered by persisted UTC due time. Status is pending, fired, or cancelled. Listing does not generate events.",
                        _schema({"status": {"type": "string", "enum": ["all", "pending", "fired", "cancelled"], "default": "all"},
                                 "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100}}))

    def _execute(self, args: Mapping[str, Any], context: ToolContext) -> Any:
        return [entry.as_dict() for entry in self._service.list_timers(status=args.get("status", "all"),
                                                                      limit=args.get("limit", 100))]


class TimerCancelTool(_SchedulingTool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("timer_cancel", "Cancel a pending local timer by id. Returns cancelled=false for a missing, fired or already cancelled timer; recorded events remain queryable.",
                        _schema({"id": _string(100)}, ("id",)), PermissionLevel.CONFIRM)

    def _execute(self, args: Mapping[str, Any], context: ToolContext) -> Any:
        return {"id": args["id"], "cancelled": self._service.cancel_timer(args["id"], task_id=context.task_id)}


class TimerEventsTool(_SchedulingTool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("timer_events", "Peek durable local timer due records after the global event id cursor. Repeated reads return the same records. Consumers own their cursor; no consumption, acknowledgement or system notification occurs.", _event_schema())

    def _execute(self, args: Mapping[str, Any], context: ToolContext) -> Any:
        return [event.as_dict() for event in self._service.list_events("timer", after_id=args.get("after_id", 0),
                                                                     limit=args.get("limit", 100))]


def timer_tools(service: SchedulingManager) -> tuple[_SchedulingTool, ...]:
    return (TimerCreateTool(service), TimerListTool(service), TimerCancelTool(service), TimerEventsTool(service))
