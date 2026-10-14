"""Trusted local Calendar tools; lifecycle belongs to Application."""

from typing import Any, Mapping

from deskpal.scheduling.ports import SchedulingManager

from ._schema import snapshot_json, validate_arguments
from .models import PermissionLevel, ToolContext, ToolResult, ToolSpec


def _string(maximum: int = 200) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def _event_schema() -> dict[str, Any]:
    return _schema({"after_id": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1, "default": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100}})


class _SchedulingTool:
    def __init__(self, service: SchedulingManager) -> None:
        self._service = service

    @property
    def spec(self) -> ToolSpec:
        raise NotImplementedError

    def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        try:
            args = snapshot_json(arguments)
            validate_arguments(args, self.spec.parameters)
            return ToolResult(True, output=self._execute(args, context))
        except ValueError as exc:
            # Validation errors are deliberate business text. Storage/audit and
            # worker failures propagate; the manager owns failure auditing.
            return ToolResult(False, error=str(exc))

    def _execute(self, args: Mapping[str, Any], context: ToolContext) -> Any:
        raise NotImplementedError


class CalendarCreateTool(_SchedulingTool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("calendar_create", "Create a one-off local calendar entry. Times need ISO seconds and an explicit offset/Z; timezone is IANA display metadata. Optional reminder_at must be no later than start_at.",
                        _schema({"title": _string(), "start_at": _string(40), "timezone": _string(100),
                                 "end_at": _string(40), "reminder_at": _string(40)},
                                ("title", "start_at", "timezone")), PermissionLevel.CONFIRM)

    def _execute(self, args: Mapping[str, Any], context: ToolContext) -> Any:
        return self._service.create_calendar(args["title"], args["start_at"], args["timezone"],
                                             end_at=args.get("end_at"), reminder_at=args.get("reminder_at"),
                                             task_id=context.task_id).as_dict()


class CalendarListTool(_SchedulingTool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("calendar_list", "List local entries ordered by UTC start. Optional interval is start_after inclusive to start_before exclusive. Does not trigger or consume reminders.",
                        _schema({"start_after": _string(40), "start_before": _string(40),
                                 "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100}}))

    def _execute(self, args: Mapping[str, Any], context: ToolContext) -> Any:
        return [entry.as_dict() for entry in self._service.list_calendar(
            start_after=args.get("start_after"), start_before=args.get("start_before"), limit=args.get("limit", 100))]


class CalendarUpdateTool(_SchedulingTool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("calendar_update", "Update an existing entry by id. Supply at least one changed field. clear_end/clear_reminder remove optional times. Only a changed reminder instant can create a new reminder event.",
                        _schema({"id": _string(100), "title": _string(), "start_at": _string(40),
                                 "timezone": _string(100), "end_at": _string(40), "reminder_at": _string(40),
                                 "clear_end": {"type": "boolean", "default": False},
                                 "clear_reminder": {"type": "boolean", "default": False}}, ("id",)),
                        PermissionLevel.CONFIRM)

    def _execute(self, args: Mapping[str, Any], context: ToolContext) -> Any:
        changes = {key: args[key] for key in ("title", "start_at", "timezone", "end_at", "reminder_at") if key in args}
        for flag, field in (("clear_end", "end_at"), ("clear_reminder", "reminder_at")):
            if args.get(flag, False):
                if field in changes:
                    raise ValueError(f"{flag} and {field} cannot both be supplied")
                changes[field] = None
        return self._service.update_calendar(args["id"], task_id=context.task_id, **changes).as_dict()


class CalendarDeleteTool(_SchedulingTool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("calendar_delete", "Delete a local calendar entry and its pending reminder by id; historical due events remain queryable.",
                        _schema({"id": _string(100)}, ("id",)), PermissionLevel.CONFIRM)

    def _execute(self, args: Mapping[str, Any], context: ToolContext) -> Any:
        return {"id": args["id"], "deleted": self._service.delete_calendar(args["id"], task_id=context.task_id)}


class CalendarEventsTool(_SchedulingTool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("calendar_events", "Peek durable local calendar reminder records after the global event id cursor. Repeated reads return the same records. These records are not system notifications; no consumption or acknowledgement occurs.", _event_schema())

    def _execute(self, args: Mapping[str, Any], context: ToolContext) -> Any:
        return [event.as_dict() for event in self._service.list_events(
            "calendar", after_id=args.get("after_id", 0), limit=args.get("limit", 100))]


def calendar_tools(service: SchedulingManager) -> tuple[_SchedulingTool, ...]:
    return (CalendarCreateTool(service), CalendarListTool(service), CalendarUpdateTool(service),
            CalendarDeleteTool(service), CalendarEventsTool(service))
