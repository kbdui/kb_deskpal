"""Deterministic receipts and bounded tool facts from caller-owned results."""

from __future__ import annotations

import json
from typing import Any, Sequence

CONFIRMATION_REQUIRED = "任务需要确认；尚未执行。请在任务审查中核对原计划后确认。"
CANCELLED = "任务已取消。已开始的操作可能仍产生效果，已发生的效果未被撤回。"
FAILED = "任务未能可靠完成。以下已记录的工具结果为准。"
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


def tool_summary(tool_results: Sequence[dict[str, Any]]) -> str:
    """Keep success/failure/unknown even when individual bodies are omitted."""
    if not tool_results:
        return "未执行工具。"
    lines: list[str] = []
    for index, item in enumerate(tool_results[:32], 1):
        success = item.get("success")
        outcome = "成功" if success is True else "失败（可能已有部分效果）" if success is False else "结果未知（需核查）"
        name = _bounded(item.get("tool_name", item.get("name", "工具")), 128).replace("\n", " ")
        body = item.get("output") if success is True else item.get("error")
        if success is False and item.get("error") not in (None, "") and item.get("output") not in (None, ""):
            body = {"error": item["error"], "partial_output": item["output"]}
        if body in (None, ""):
            body = item.get("output")
        detail = _bounded(body, 1024) if body not in (None, "") else "无可用正文"
        lines.append(f"{index}. {name}：{outcome}。{detail}")
    if len(tool_results) > 32:
        lines.append(f"另有 {len(tool_results) - 32} 条结果正文省略；完整结果请查看任务记录。")
    return "工具结果（记录事实）：\n" + "\n".join(lines)


def confirmation_failed() -> str:
    return PREFERENCE_CONFIRMATION_FAILED


def terminal_response(status: str, tool_results: Sequence[dict[str, Any]] = ()) -> str:
    prefix = {"waiting": CONFIRMATION_REQUIRED, "cancelled": CANCELLED,
              "failed": FAILED, "uncertain": UNCERTAIN, "needsreview": UNCERTAIN}.get(status.casefold(), "任务结果以下方记录为准。")
    return prefix + ("\n" + tool_summary(tool_results) if tool_results else "")
