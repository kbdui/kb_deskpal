"""Read-only trusted metadata over the live registry; never execute a tool."""

from collections.abc import Mapping
from dataclasses import asdict
from hashlib import sha256
from typing import Any

from deskpal.tools.ports import ToolManager
from deskpal.workflow.store import canonical


# Presentation only. Presence comes exclusively from list_tools(), not this map.
_DISPLAY = {
    "echo": ("回显文本", "general", "返回指定文本"),
    "time": ("当前时间", "general", "读取当前 UTC 时间"),
    "read_text": ("读取文本", "files", "读取工作区 UTF-8 文件"),
    "write_text": ("写入文本", "files", "在工作区创建或明确覆盖文本文件"),
    "document_search": ("查找文件", "files", "按文件名查找工作区文件"),
    "document_classify": ("文件分类", "files", "按扩展名查看文件类别，不移动文件"),
    "document_copy": ("复制文件", "files", "复制单个工作区文件"),
    "document_move": ("移动文件", "files", "移动单个工作区文件"),
    "document_delete": ("删除文件", "files", "删除单个工作区普通文件"),
    "screenshot_capture": ("屏幕截图", "screen", "经授权将屏幕区域保存到工作区"),
    "calendar_create": ("新建日程", "schedule", "创建本地一次性日程及可选提醒"),
    "calendar_list": ("查看日程", "schedule", "查询本地日程"),
    "calendar_update": ("修改日程", "schedule", "更新日程及提醒时间"),
    "calendar_delete": ("删除日程", "schedule", "删除日程和待到期提醒"),
    "calendar_events": ("查看日程提醒记录", "schedule", "只读查询持久到期记录"),
    "timer_create": ("新建计时器", "schedule", "创建本地倒计时"),
    "timer_list": ("查看计时器", "schedule", "查询本地计时器"),
    "timer_cancel": ("取消计时器", "schedule", "取消仍在等待的计时器"),
    "timer_events": ("查看计时器到期记录", "schedule", "只读查询持久到期记录"),
}
_LABELS = {"path": "文件路径", "source": "源文件", "destination": "目标文件", "text": "文本内容",
    "overwrite": "覆盖已有文件", "id": "对象 ID", "title": "标题", "label": "提醒名称",
    "duration_seconds": "计时秒数", "start_at": "开始时间", "end_at": "结束时间", "reminder_at": "提醒时间",
    "timezone": "时区", "query": "查找内容", "limit": "数量上限", "monitor": "显示器编号",
    "start_after": "开始时间下界", "start_before": "开始时间上界",
    "region": "截图区域", "left": "左侧偏移", "top": "顶部偏移", "width": "宽度", "height": "高度",
    "status": "状态", "after_id": "记录游标", "clear_end": "清除结束时间", "clear_reminder": "清除提醒"}


def display_name(name: str) -> str:
    return _DISPLAY.get(name, (name, "other", ""))[0]


def parameter_fields(schema: Mapping[str, Any]) -> list[dict[str, Any]]:
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, Mapping):
        return []
    return [{"name": name, "label": _LABELS.get(name, name), "required": name in required,
             "type": field.get("type", "any") if isinstance(field, Mapping) else "any",
             "schema": field} for name, field in properties.items()]


def argument_fields(arguments: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every actual parameter survives, including unknown/nested future fields."""
    def node(name: str, value: Any) -> dict[str, Any]:
        kind = ("object" if isinstance(value, Mapping) else "array" if isinstance(value, (tuple, list))
                else "null" if value is None else "boolean" if isinstance(value, bool)
                else "number" if isinstance(value, (int, float)) else "string")
        children = ([node(key, item) for key, item in value.items()] if isinstance(value, Mapping)
                    else [node(str(index), item) for index, item in enumerate(value)] if isinstance(value, (tuple, list)) else [])
        return {"name": name, "label": _LABELS.get(name, name), "type": kind, "value": value, "children": children}
    return [node(name, value) for name, value in arguments.items()]


class ToolCatalog:
    def __init__(self, tools: ToolManager) -> None:
        self._tools = tools

    def _items(self) -> list[dict[str, Any]]:
        specs = self._tools.list_tools()
        if len(specs) > 1024:
            raise ValueError("Tool catalogue exceeds its bounded registry limit")
        items: list[dict[str, Any]] = []
        project = getattr(self._tools, "catalog_metadata", None)
        for spec in sorted(specs, key=lambda item: item.name):
            metadata = project(spec.name) if callable(project) else {"origin": "host_registered", "plugin_id": "host_registered", "availability": "check_at_execution", "reason": "执行时检查"}
            name, category, summary = _DISPLAY.get(spec.name, (spec.name, "other", spec.description))
            items.append({"id": spec.name, "name": spec.name, "display_name": name, "category": category,
                "summary": summary, "description": spec.description, "plugin_id": metadata["plugin_id"],
                "origin": metadata["origin"], "contract_version": spec.contract_version,
                "permission_level": int(spec.permission_level), "availability": metadata["availability"],
                "reason": metadata["reason"], "parameter_fields": parameter_fields(spec.parameters),
                "parameter_schema": dict(spec.parameters),
                "result_fields": [{"name": "success", "label": "是否成功", "type": "boolean"},
                    {"name": "output", "label": "执行结果", "type": "any", "description": "实际工具结果；不承诺尚未注册的结果字段"},
                    {"name": "error", "label": "失败原因", "type": ["string", "null"]}]})
        # Force an independent finite-JSON copy before returning registry values.
        import json
        return list(json.loads(canonical(items)))

    def catalog(self, *, q: str = "", category: str | None = None, origin: str | None = None,
                cursor: str | None = None, limit: int = 50, known_catalog_revision: str | None = None) -> dict[str, Any]:
        if not isinstance(q, str) or len(q) > 200 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid catalogue query or page limit")
        if any(value is not None and (not isinstance(value, str) or len(value) > 200) for value in (category, origin)):
            raise ValueError("Invalid catalogue filter")
        items = self._items()
        revision = sha256(canonical(items).encode()).hexdigest()
        filtered = [item for item in items if (not category or item["category"] == category) and (not origin or item["origin"] == origin)
                    and (not q or q.casefold() in (item["name"] + item["display_name"] + item["summary"]).casefold())]
        query = sha256(canonical([q, category, origin]).encode()).hexdigest()[:16]
        prefix = revision + ":" + query + ":"
        offset = 0
        if cursor is not None:
            if not isinstance(cursor, str) or len(cursor) > 120 or not cursor.startswith(prefix) or not cursor[len(prefix):].isdigit():
                raise ValueError("Catalogue changed or cursor does not match filters")
            offset = int(cursor[len(prefix):])
            if offset > len(filtered):
                raise ValueError("Catalogue cursor is outside this result")
        unchanged = known_catalog_revision == revision and cursor is None
        chosen = [] if unchanged else filtered[offset:offset + limit]
        more = not unchanged and offset + limit < len(filtered)
        return {"schema_version": 1, "items": chosen, "catalog_revision": revision, "unchanged": unchanged,
                "has_more": more, "next_cursor": prefix + str(offset + limit) if more else None,
                "capabilities": {"read_only": True, "installation": False, "enable_disable": False}}

    def detail(self, id: str, *, catalog_revision: str | None = None) -> dict[str, Any]:
        if not isinstance(id, str) or not id or len(id) > 200:
            raise ValueError("Invalid tool identity")
        items = self._items()
        revision = sha256(canonical(items).encode()).hexdigest()
        if catalog_revision is not None and catalog_revision != revision:
            raise ValueError("Tool catalogue changed; refresh its current contract")
        item = next((item for item in items if item["id"] == id), None)
        if item is None:
            raise KeyError(id)
        return {"schema_version": 1, "item": item, "catalog_revision": revision}

    def plugins(self) -> dict[str, Any]:
        items = self._items()
        groups: dict[str, dict[str, Any]] = {}
        for item in items:
            identity = item["plugin_id"]
            group = groups.setdefault(identity, {"id": identity, "name": "内置工具" if identity == "builtin" else "主机注册的可信工具",
                "origin": item["origin"], "tool_ids": [], "capabilities": {"read_only": True, "installation": False, "enable_disable": False}})
            group["tool_ids"].append(item["id"])
        return {"schema_version": 1, "items": list(groups.values()), "catalog_revision": sha256(canonical(items).encode()).hexdigest()}
