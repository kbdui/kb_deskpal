"""Non-blocking local Timer tools; no tool starts or owns threads."""

from typing import Any, Mapping

from deskpal.scheduling.ports import SchedulingManager
from deskpal.scheduling.service import MAX_DURATION_SECONDS

from .calendar import _SchedulingTool, _event_schema, _schema, _string
from .models import PermissionLevel, ToolContext, ToolSpec


class TimerCreateTool(_SchedulingTool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("timer_create", "创建本地倒计时，立即返回；duration 为1至2678400秒。运行中的调度器记录到期事件，重启恢复逾期计时；本工具不负责系统通知。",
                        _schema({"duration_seconds": {"type": "number", "minimum": 1, "maximum": MAX_DURATION_SECONDS},
                                 "label": _string()}, ("duration_seconds",)), PermissionLevel.AUTOMATIC, contract_version="2")

    def _execute(self, args: Mapping[str, Any], context: ToolContext) -> Any:
        return self._service.create_timer(args["duration_seconds"], label=args.get("label", "Timer"),
                                          task_id=context.task_id).as_dict()


class TimerListTool(_SchedulingTool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("timer_list", "按 UTC 到期时间列出计时器；状态为 pending/fired/cancelled。查询不生成事件。",
                        _schema({"status": {"type": "string", "enum": ["all", "pending", "fired", "cancelled"], "default": "all"},
                                 "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100}}))

    def _execute(self, args: Mapping[str, Any], context: ToolContext) -> Any:
        return [entry.as_dict() for entry in self._service.list_timers(status=args.get("status", "all"),
                                                                      limit=args.get("limit", 100))]


class TimerCancelTool(_SchedulingTool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("timer_cancel", "按 id 取消待到期计时器；不存在、已触发或已取消时返回 cancelled=false，保留历史事件。",
                        _schema({"id": _string(100)}, ("id",)), PermissionLevel.AUTOMATIC, contract_version="2")

    def _execute(self, args: Mapping[str, Any], context: ToolContext) -> Any:
        return {"id": args["id"], "cancelled": self._service.cancel_timer(args["id"], task_id=context.task_id)}


class TimerEventsTool(_SchedulingTool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("timer_events", "按全局游标查询已持久保存的计时到期事件，可重复读取；消费者维护游标，不消费、不确认，也不发送系统通知。", _event_schema())

    def _execute(self, args: Mapping[str, Any], context: ToolContext) -> Any:
        return [event.as_dict() for event in self._service.list_events("timer", after_id=args.get("after_id", 0),
                                                                     limit=args.get("limit", 100))]


def timer_tools(service: SchedulingManager) -> tuple[_SchedulingTool, ...]:
    return (TimerCreateTool(service), TimerListTool(service), TimerCancelTool(service), TimerEventsTool(service))
