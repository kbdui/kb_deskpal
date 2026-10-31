"""Deterministic receipts and bounded tool facts from caller-owned results."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Sequence

CONFIRMATION_REQUIRED = "任务需要确认；尚未执行。请在任务审查中核对原计划后确认。"
CANCELLED = "任务已取消。已开始的操作可能仍产生效果，已发生的效果未被撤回。"
FAILED = "这次请求未能完成。"
UNCERTAIN = "执行结果未知；请先核查实际效果。系统不会自动重做。"
PREFERENCE_SAVED = "表达偏好已保存，后续回复按所选范围生效。"
PREFERENCE_REJECTED = "已忽略这条偏好建议。"
PREFERENCE_CONFIRMATION_FAILED = "偏好未保存：建议已失效、来源不可用或保存失败。请重新明确设置。"


def _bounded(value: Any, limit: int) -> str:
    try:
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError, OverflowError):
        return "[内容不可表示]"
    rendered = "".join(char if ord(char) >= 32 or char in "\n\t" else " " for char in rendered)
    encoded = rendered.encode("utf-8")
    if len(encoded) > limit:
        return encoded[:limit].decode("utf-8", errors="ignore") + "…[正文已省略]"
    return rendered


_TOOL_NAMES = {
    "time": "当前时间", "read_text": "读取文本", "write_text": "写入文本",
    "document_search": "查找文件", "document_classify": "文件分类", "document_copy": "复制文件",
    "document_move": "移动文件", "document_delete": "删除文件", "screenshot_capture": "屏幕截图",
    "timer_create": "新建计时器", "timer_list": "查看计时器", "timer_cancel": "取消计时器",
    "timer_events": "查看计时器到期记录", "calendar_create": "新建日程", "calendar_list": "查看日程",
    "calendar_update": "修改日程", "calendar_delete": "删除日程", "calendar_events": "查看日程提醒记录",
}
_RECORDED = "结构化结果已记录，可在任务详情查看。"


def _filename(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    path = PureWindowsPath(value) if "\\" in value or PureWindowsPath(value).drive else PurePosixPath(value)
    return _bounded(path.name, 160)


def _record_name(value: Any, key: str) -> str:
    return _bounded(value[key], 160).replace("\n", " ") if isinstance(value, dict) and isinstance(value.get(key), str) else ""


def _list_detail(values: list[Any], key: str, noun: str, *, paths: bool = False) -> str:
    names = [_filename(item.get(key)) if paths and isinstance(item, dict) else _record_name(item, key)
             for item in values[:5]]
    shown = "、".join("“" + name + "”" for name in names if name)
    return f"本次返回 {len(values)} {noun}。" + ("包括" + shown + "。" if shown else "") + (
        "其余内容可在任务详情查看。" if len(values) > 5 else "")


def _output_detail(name: str, output: Any, *, reliable: bool) -> str:
    """Interpret only known built-in record fields; never parse user text JSON."""
    if isinstance(output, str):
        if name == "time" and reliable:
            try:
                instant = datetime.fromisoformat(output)
                if instant.tzinfo is not None:
                    return instant.astimezone().strftime("当前时间：%Y年%m月%d日 %H:%M（本机时区）。")
            except (ValueError, OverflowError):
                pass
            return "时间结果已记录，可在任务详情查看。"
        return _bounded(output, 1024)
    if not isinstance(output, (dict, list, tuple)):
        return _bounded(output, 1024) if output is not None else "无可用正文"
    if not reliable:
        # A failed move explicitly records the effects of its partial operation.
        # Other records cannot establish that their intended operation completed.
        if (name == "document_move" and isinstance(output, dict) and output.get("completed") is False
                and output.get("destination_created") is True and output.get("source_removed") is False):
            target, source = _filename(output.get("path")), _filename(output.get("source"))
            return f"目标文件{'“' + target + '”' if target else ''}已建立，源文件{'“' + source + '”' if source else ''}未移除；请核查两个位置。"
        if isinstance(output, dict) and name in {"write_text", "document_copy", "screenshot_capture", "document_delete"}:
            key = "bytes_deleted" if name == "document_delete" else "bytes_written"
            if type(output.get(key)) is int and output[key] >= 0:
                target = _filename(output.get("path"))
                return f"输出记录显示{'删除' if name == 'document_delete' else '写入'} {output[key]} 字节{'（' + target + '）' if target else ''}；整体效果仍需核查。"
        return "输出内容已记录，可在任务详情查看；实际效果仍需核查。"
    if isinstance(output, list):
        key, noun = {"timer_list": ("label", "个计时器"), "calendar_list": ("title", "项日程"),
                     "timer_events": ("label", "条到期记录"), "calendar_events": ("label", "条提醒记录")}.get(name, ("", ""))
        return _list_detail(output, key, noun) if key else _RECORDED
    if not isinstance(output, dict):
        return _RECORDED
    if name == "timer_cancel" and type(output.get("cancelled")) is bool:
        return "计时器已取消。" if output["cancelled"] else "未取消计时器：可能已到期、不存在或已取消。"
    if name == "calendar_delete" and type(output.get("deleted")) is bool:
        return "日程已删除。" if output["deleted"] else "未删除日程：该记录可能已不存在。"
    if name == "timer_create" and isinstance(output.get("id"), str) and isinstance(output.get("due_at"), str):
        title = _record_name(output, "label")
        return f"计时器{'“' + title + '”' if title else ''}已建立；到期时间请在安排中查看。"
    if name in {"calendar_create", "calendar_update"} and isinstance(output.get("id"), str) and isinstance(output.get("start_at"), str):
        title = _record_name(output, "title")
        return f"日程{'“' + title + '”' if title else ''}已{'建立' if name == 'calendar_create' else '更新'}；具体时间请在安排中查看。"
    if name in {"document_search", "document_classify"} and isinstance(output.get("files"), list):
        detail = _list_detail(output["files"], "path", "个文件", paths=True)
        return detail + ("查询范围受限，结果未覆盖全部文件。" if output.get("truncated") is True else "")
    target = _filename(output.get("path"))
    if target:
        if name in {"write_text", "document_copy"} and type(output.get("bytes_written")) is int and output["bytes_written"] >= 0:
            if name == "document_copy":
                source = _filename(output.get("source"))
                return f"已把文件{'“' + source + '”' if source else ''}复制为“{target}”；完整位置见任务详情。"
            return f"已写入文件“{target}”；完整位置见任务详情。"
        if name == "document_move" and output.get("completed") is True:
            source = _filename(output.get("source"))
            return f"已把文件{'“' + source + '”' if source else ''}移动为“{target}”；完整位置见任务详情。"
        if name == "document_delete" and type(output.get("bytes_deleted")) is int and output["bytes_deleted"] >= 0:
            return f"已删除文件“{target}”；原位置见任务详情。"
        if name == "screenshot_capture" and type(output.get("bytes_written")) is int and output["bytes_written"] >= 0:
            return f"已保存截图“{target}”；完整位置见任务详情。"
    return _RECORDED


def tool_summary(tool_results: Sequence[dict[str, Any]]) -> str:
    """Keep success/failure/unknown even when individual bodies are omitted."""
    if not tool_results:
        return "未执行工具。"
    lines: list[str] = []
    for index, item in enumerate(tool_results[:32], 1):
        success = item.get("success")
        output = item.get("output")
        outcome = ("已返回结果" if isinstance(output, (dict, list, tuple)) else "成功") if success is True else "失败（可能已有部分效果）" if success is False else "结果未知（需核查）"
        raw_name = str(item.get("tool_name", item.get("name", "工具")))
        name = _bounded(_TOOL_NAMES.get(raw_name, raw_name), 128).replace("\n", " ")
        details: list[str] = []
        if success is not True and item.get("error") not in (None, ""):
            error = item["error"]
            details.append("原因：" + (_bounded(error, 1024) if isinstance(error, str) else "错误详情已记录，可在任务详情查看。"))
        if output not in (None, ""):
            details.append(_output_detail(raw_name, output, reliable=success is True))
        detail = " ".join(details) if details else "无可用正文"
        lines.append(f"{index}. {name}：{outcome}。{detail}")
    if len(tool_results) > 32:
        lines.append(f"另有 {len(tool_results) - 32} 条结果正文省略；完整结果请查看任务记录。")
    return "工具结果（记录事实）：\n" + "\n".join(lines)


def confirmation_failed() -> str:
    return PREFERENCE_CONFIRMATION_FAILED


def _readable_time(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is not None:
            return instant.astimezone().strftime("%Y年%m月%d日 %H:%M（本机时区）")
    except (ValueError, OverflowError):
        pass
    return _bounded(value, 160)


def _detail_record(record: dict[str, Any], *, calendar: bool) -> str:
    title = _record_name(record, "title" if calendar else "label")
    parts = [title] if title else []
    for key, label in (("start_at", "开始"), ("end_at", "结束"), ("reminder_at", "提醒")) if calendar else (("due_at", "到期"),):
        stamp = _readable_time(record.get(key))
        if stamp:
            parts.append(label + "：" + stamp)
    if not calendar:
        state = {"pending": "等待到期", "fired": "已到期", "cancelled": "已取消"}.get(str(record.get("status")))
        if state:
            parts.append("状态：" + state)
    return "；".join(parts)


def tool_result_text(result: dict[str, Any]) -> str:
    """Readable single recorded outcome for details, without numbered log headings.

    Only built-in structured fields establish effects. Text bodies stay literal;
    this helper does not rewrite or parse user-authored JSON strings.
    """
    success = result.get("success")
    name = str(result.get("tool_name", result.get("name", "")))
    output = result.get("output")
    detail = _output_detail(name, output, reliable=success is True) if output not in (None, "") else ""
    if success is True:
        if name in {"calendar_list", "timer_list", "calendar_events", "timer_events"} and isinstance(output, list):
            noun = "日程" if name == "calendar_list" else "计时器" if name == "timer_list" else "提醒记录"
            lines = [f"本次返回 {len(output)} 项{noun}。"]
            for item in output[:20]:
                if isinstance(item, dict):
                    text = _detail_record(item, calendar=name == "calendar_list")
                    if text:
                        lines.append(text)
            if len(output) > 20:
                lines.append("仅展示前 20 项；完整内容见原始记录。")
            return _bounded("\n".join(lines), 8192)
        if name in {"calendar_create", "calendar_update", "timer_create"} and isinstance(output, dict):
            record = _detail_record(output, calendar=name != "timer_create")
            if record:
                return detail.split("；", 1)[0] + "\n" + record
        if isinstance(output, dict) and name in {"write_text", "document_copy", "document_move", "document_delete", "screenshot_capture"}:
            fields = [(label + "：" + (_filename(output[key]) if key in {"source", "path"} else _bounded(output[key], 1024))) for key, label in
                      (("source", "原位置"), ("path", "位置"), ("bytes_written", "写入字节"), ("bytes_deleted", "删除字节")) if key in output]
            if fields:
                return detail + "\n" + "\n".join(fields)
        return detail or "操作已完成，未返回正文。"
    prefix = "操作未完成，可能已有部分效果。" if success is False else "操作结果未知，需要核查实际效果。"
    error = result.get("error")
    reason = "原因：" + _bounded(error, 1024) if isinstance(error, str) and error else ""
    return prefix + " ".join(part for part in (detail, reason) if part)


def tool_response_fallback(tool_results: Sequence[dict[str, Any]], *, expression_failed: bool = True) -> str:
    """Short neutral feedback; never turn expression failure into tool failure.

    The result ledger stays separate and complete. Raw exception strings and
    arbitrary failed output cannot become a chat answer through this fallback.
    """
    lines = ["回复整理暂不可用。"] if expression_failed else []
    if len(tool_results) > 1:
        completed = sum(item.get("success") is True for item in tool_results)
        lines.append(f"已完成 {completed} 项操作。其余状态和具体内容可在工具结果中查看。")
    for item in tool_results[:3]:
        name = str(item.get("tool_name", item.get("name", "")))
        output = item.get("output")
        success = item.get("success")
        if success is True:
            if name == "calendar_list" and isinstance(output, list) and not output:
                lines.append("本次查询范围内暂无日程。")
            elif name == "timer_list" and isinstance(output, list) and not output:
                lines.append("本次查询范围内暂无计时器。")
            elif len(tool_results) == 1 and not isinstance(output, str):
                lines.append(_output_detail(name, output, reliable=True))
            elif len(tool_results) == 1:
                lines.append("操作已完成，具体内容可在工具结果中查看。")
        else:
            # Known structured partial effects remain useful; arbitrary error
            # strings and failed text bodies belong in task details only.
            partial = _output_detail(name, output, reliable=False) if isinstance(output, dict) else ""
            if success is False:
                lines.append("这一步未能完成，不能据此确认查询结果或操作效果。" + partial)
            else:
                lines.append("这一步的结果未知，需要核查实际效果。" + partial)
    if len(tool_results) > 3:
        lines.append("其余结果可在任务详情查看。")
    return "\n".join(lines) if lines else "暂时无法整理回复，请查看任务状态。"


def terminal_response(status: str, tool_results: Sequence[dict[str, Any]] = ()) -> str:
    prefix = {"waiting": CONFIRMATION_REQUIRED, "cancelled": CANCELLED,
              "failed": FAILED, "uncertain": UNCERTAIN, "needsreview": UNCERTAIN}.get(status.casefold(), "任务结果以下方记录为准。")
    return prefix + ("\n" + tool_response_fallback(tool_results, expression_failed=False) if tool_results else "")


def legacy_chat_reply(body: str, stored_response: Any, tool_results: Sequence[dict[str, Any]]) -> str | None:
    """Display an exact old host receipt briefly, without rewriting history.

    Both the persisted task response and the deterministic receipt from its
    recorded outcomes must match. Never rewrite arbitrary JSON or quoted text.
    """
    if not tool_results or body != stored_response:
        return None
    receipt = tool_summary(tool_results)
    if body == receipt:
        return tool_response_fallback(tool_results, expression_failed=False)
    separator = receipt + "\n补充说明："
    if body.startswith(separator):
        return body[len(separator):].strip() or tool_response_fallback(tool_results, expression_failed=False)
    return None
