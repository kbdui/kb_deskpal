"""Natural receipts keep structured archives and do not rewrite user bodies."""
import copy
from pathlib import Path
from typing import Any

import pytest

from deskpal.application.desktop import DesktopFacade
from deskpal.application.factory import create_application
from deskpal.config import load_config
from deskpal.planner.models import Plan
from deskpal.response.templates import legacy_chat_reply, terminal_response, tool_result_text, tool_response_fallback, tool_summary
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolCall


def fact(name: str, output: Any, success: Any = True, error: str | None = None) -> dict[str, Any]:
    return {"tool_name": name, "success": success, "output": output, "error": error}


def test_legacy_receipts_need_exact_task_and_fact_match_without_rewriting_arbitrary_text() -> None:
    facts = [fact("calendar_list", [])]
    receipt = tool_summary(facts)
    assert legacy_chat_reply(receipt, receipt, facts) == "本次查询范围内暂无日程。"
    old = receipt + "\n补充说明：目前没有查到日程。"
    assert legacy_chat_reply(old, old, facts) == "目前没有查到日程。"
    assert legacy_chat_reply(receipt, "different persisted response", facts) is None
    assert legacy_chat_reply(receipt, receipt, [fact("calendar_list", [{"title": "会议"}])]) is None
    arbitrary = '{"output":[],"tool_name":"calendar_list"}'
    assert legacy_chat_reply(arbitrary, arbitrary, facts) is None


def test_timer_and_calendar_records_are_natural_and_noop_is_not_reported_as_effect() -> None:
    output = {"id": "internal-timer-identity", "label": "休息一下", "due_at": "2026-10-27T12:34:56.123456+00:00",
              "created_at": "2026-10-27T12:33:56.123456+00:00", "status": "pending"}
    original = copy.deepcopy(output)
    rendered = tool_summary([fact("timer_create", output)])
    assert "休息一下" in rendered and "已建立" in rendered
    assert "安排" in rendered and all(value not in rendered for value in (output["id"], output["due_at"], '"status"', "pending"))
    assert output == original
    cancelled = tool_summary([fact("timer_cancel", {"id": "hidden-id", "cancelled": False})])
    assert "未取消" in cancelled and "不存在" in cancelled and "成功" not in cancelled
    deleted = tool_summary([fact("calendar_delete", {"id": "hidden-id", "deleted": False})])
    assert "未删除" in deleted and "成功" not in deleted and "hidden-id" not in deleted
    for name, key in (("calendar_create", "已建立"), ("calendar_update", "已更新")):
        summary = tool_summary([fact(name, {"id": "hidden-id", "title": "团队会议", "start_at": output["due_at"]})])
        assert "团队会议" in summary and key in summary and output["due_at"] not in summary


@pytest.mark.parametrize("name,output,expected", [
    ("write_text", {"path": "very/long/private/subfolder/report.txt", "bytes_written": 0}, "已写入"),
    ("document_copy", {"source": "private/origin.txt", "path": "private/copy.txt", "bytes_written": 2}, "复制为"),
    ("document_move", {"source": "private/origin.txt", "path": "private/moved.txt", "completed": True}, "移动为"),
    ("document_delete", {"path": "private/report.txt", "bytes_deleted": 2}, "已删除"),
    ("screenshot_capture", {"path": "private/screen.png", "bytes_written": 2, "width": 200, "height": 100}, "已保存截图"),
])
def test_known_file_facts_have_names_and_keep_complete_locations_in_archive(name: str, output: dict[str, Any], expected: str) -> None:
    before = copy.deepcopy(output); summary = tool_summary([fact(name, output)])
    assert expected in summary and output["path"].split("/")[-1] in summary
    assert output["path"] not in summary
    assert "完整位置" in summary or name == "document_delete" and "原位置" in summary
    assert output == before and "{" not in summary


def test_list_counts_are_returned_counts_with_bounded_names_and_no_raw_metadata() -> None:
    timers = [{"id": f"internal-{index}", "label": f"计时器{index}", "due_at": "2026-10-27T12:34:56Z"} for index in range(8)]
    summary = tool_summary([fact("timer_list", timers)])
    assert "本次返回 8 个计时器" in summary and "计时器0" in summary and "计时器7" not in summary
    assert "internal-" not in summary and "due_at" not in summary
    files = tool_summary([fact("document_search", {"files": [{"path": "long/private/report.txt", "bytes": 3}], "truncated": True})])
    assert "1 个文件" in files and "report.txt" in files and "未覆盖全部文件" in files
    assert "long/private" not in files
    assert "本次返回 0 项日程" in tool_summary([fact("calendar_list", [])])


def test_failure_partial_effect_and_unknown_are_never_turned_into_success() -> None:
    partial = {"source": "private/original.txt", "path": "private/copied.txt", "completed": False,
               "destination_created": True, "source_removed": False, "bytes_moved": 3}
    rendered = tool_summary([fact("document_move", partial, False, "Destination created, but source removal failed; both files remain")])
    assert "失败（可能已有部分效果）" in rendered and "copied.txt”已建立" in rendered and "original.txt”未移除" in rendered
    assert "source removal failed" in rendered and "成功" not in rendered and "completed" not in rendered
    unknown = terminal_response("uncertain", [fact("timer_create", {"id": "private-id", "label": "休息", "due_at": "precision-time"}, None)])
    assert "未知" in unknown and "需核查" in unknown and "已建立" not in unknown and "成功" not in unknown
    failed = terminal_response("failed", [fact("timer_create", {"label": "休息"}, False, "storage unavailable")])
    assert "storage unavailable" not in failed and "实际效果仍需核查" in failed and "已建立" not in failed
    written = tool_summary([fact("write_text", {"path": "private/partial.txt", "bytes_written": 3}, False, "publication incomplete")])
    assert "写入 3 字节" in written and "publication incomplete" in written and "整体效果仍需核查" in written
    assert "成功" not in written and "private/" not in written


def test_unknown_structures_are_recorded_but_user_json_strings_stay_literal() -> None:
    unknown = {"id": "private-internal", "due_at": "precision-time", "result": [1, 2]}
    assert "结构化结果已记录" in tool_summary([fact("future_tool", unknown)])
    assert "private-internal" not in tool_summary([fact("future_tool", unknown)])
    raw = '{"status":"Completed","path":"private/path","created_at":"2026-10-27T12:34:56Z"}'
    assert tool_summary([fact("echo", raw)]) == "工具结果（记录事实）：\n1. echo：成功。" + raw
    assert raw in tool_summary([fact("read_text", raw)])


class TimerPlanner:
    def plan(self, request: str, *, memories: Any = (), tools: Any = ()) -> Plan:
        return Plan("建立一个计时器。", (ToolCall("timer_create", {"duration_seconds": 60, "label": "休息一下"}, "timer-call"),))


def test_real_core_composer_and_facade_publish_natural_timer_reply_but_keep_exact_archive(tmp_path: Path) -> None:
    config = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline", "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
                                  "DESKPAL_DATA_DIR": str(tmp_path / "data")})
    with create_application(config, planner=TimerPlanner()) as app:
        facade = DesktopFacade(app)
        conversation = app.conversation_ensure_default()
        task = app.submit("一分钟后提醒休息", command_id="natural-timer-submission", conversation_id=conversation["id"],
                          expected_context_epoch=conversation["context_epoch"])
        assert app.wait(task.id, 5).status == TaskStatus.WAITING
        review = app.task_review(task.id)
        assert facade.execute("confirm", {"task_id": task.id, "plan_hash": review["plan_hash"], "expected_revision": review["task_revision"],
            "ordinary_tools": ["timer_create"], "high_risk_tools": [], "command_id": "natural-timer-confirmation"})["accepted"]
        result = app.wait(task.id, 5)
        assert result.status == TaskStatus.COMPLETED and result.tool_results[0].success is True
        output = result.tool_results[0].output
        assert "已建立" in result.response and "休息一下" in result.response
        for internal in (output["id"], output["due_at"], output["created_at"], "tool_results", "memory_claims", "unverified_assistant_draft"):
            assert internal not in result.response
        persisted = facade.execute("task_review", {"task_id": task.id})
        assert persisted["task"]["result"]["tool_results"][0]["output"] == output
        assert persisted["task"]["result"]["response"] == result.response
        timeline = facade.execute("conversation_timeline", {"id": conversation["id"], "limit": 50})
        replies = [item["body"] for item in timeline["items"] if item["kind"] == "assistant_message"]
        assert replies == [result.response]

