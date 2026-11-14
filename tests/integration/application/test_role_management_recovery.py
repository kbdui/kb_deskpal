"""角色卡误改、取消回执及部分完成的回归；只使用临时档案和合成模型。"""
import pytest
from dataclasses import replace

from deskpal.application import create_application, factory
from deskpal.application.desktop import DesktopFacade
from deskpal.companion import CompanionRuntime
from deskpal.companion.errors import CompanionRevisionConflict, CompanionValidationError
from deskpal.config import load_config
from deskpal.llm.models import LLMResponse
from deskpal.planner.llm_planner import PLANNING_INSTRUCTION
from deskpal.tasks.models import TaskStatus
from deskpal.tools.manager import _execution_error
from deskpal.tools.models import PermissionLevel, ToolCall
from deskpal.workflow.models import WorkflowBlocked
from tests.support.planning_decisions import PlanningDecisionProvider


class Model:
    def __init__(self):
        self.decide = lambda: LLMResponse("已完成。")
        self.planning_inputs = []
        self.planning_tools = []

    def chat(self, messages, *, tools=()):
        system = messages[0].content
        if system.startswith("[MEMORY_FEEDBACK]"):
            return LLMResponse('{"memories":[],"reviews":[],"habits":[],"emotion":null,"summary":null}')
        if system.startswith("[EXPRESSION]"):
            return LLMResponse('{"reply_text":"已根据实际结果处理。"}')
        assert system.startswith("[PLANNING]"), system[:80]
        self.planning_inputs.append(messages)
        self.planning_tools.append({spec.name for spec in tools})
        return self.decide()

    def generate(self, prompt):
        raise AssertionError("No real model call is allowed")


@pytest.fixture
def app_model(tmp_path, monkeypatch):
    config = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
        "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "DESKPAL_DATA_DIR": str(tmp_path / "data"), "DESKPAL_PROACTIVE_ENABLED": "false"})
    model = Model()
    model.settings = config
    monkeypatch.setattr(factory, "OfflineLLMProvider", lambda: PlanningDecisionProvider(model))
    app = create_application(config)
    try:
        yield app, model
    finally:
        assert app.close(timeout=10)


def change(app, text, revision=None):
    return LLMResponse("", (ToolCall("role_update", {
        "text": text, "expected_revision": revision if revision is not None else app.interaction_snapshot()["revision"]}, "change"),))


def confirm(app, task_id, command):
    review = app.task_review(task_id)
    return DesktopFacade(app).execute("confirm", dict(task_id=task_id, command_id=command,
        plan_hash=review["plan_hash"], expected_revision=review["task_revision"],
        ordinary_tools=review["missing_permissions"]["ordinary_tools"], high_risk_tools=[]))


def test_role_change_requires_permission_and_cancel_does_not_write(app_model):
    app, model = app_model
    before = app.interaction_snapshot()["character_card"]
    model.decide = lambda: change(app, "未经确认不能保存的设定")
    task = app.submit("修改角色卡")
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    assert app.interaction_snapshot()["character_card"] == before
    assert not app.journal.attempts(task.id)
    review = app.task_review(task.id)
    assert review["missing_permissions"]["ordinary_tools"] == ["role_update"]
    spec = next(s for s in app.tools.list_tools() if s.name == "role_update")
    assert spec.permission_level == PermissionLevel.CONFIRM and spec.contract_version == "2"
    receipt = DesktopFacade(app).execute("cancel", {"task_id": task.id, "command_id": "cancel-card"})
    assert receipt["accepted"] and receipt["status"] == "Cancelled"
    assert app.interaction_snapshot()["character_card"] == before
    assert app.command_status("cancel-card")["accepted"]


def test_two_distinct_role_changes_in_one_task_use_separate_receipts(app_model):
    app, model = app_model
    def decide():
        n = len(model.planning_inputs)
        return change(app, f"角色设定{n}") if n <= 2 else LLMResponse("两次已授权修改已保存。")
    model.decide = decide
    task = app.submit("先改一次，再改一次角色卡")
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    assert confirm(app, task.id, "approve-first")["accepted"]
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    assert app.interaction_snapshot()["character_card"]["text"] == "角色设定1"
    assert len(app.journal.attempts(task.id)) == 1
    confirm(app, task.id, "approve-second")
    assert app.wait(task.id, 10).status == TaskStatus.COMPLETED
    assert app.interaction_snapshot()["character_card"]["text"] == "角色设定2"
    assert all(a.status == "Succeeded" for a in app.journal.attempts(task.id))
    assert 'completed_operations' in str(model.planning_inputs[-1])


def test_cancel_after_partial_completion_keeps_effect_and_reports_it(app_model):
    app, model = app_model
    original = app.interaction_snapshot()["character_card"]["text"]
    model.decide = lambda: (LLMResponse("", (ToolCall("memory_remember", {"content": "合成记忆：喜欢茶"}, "remember"),))
        if len(model.planning_inputs) == 1 else change(app, "仍待授权的角色卡"))
    conversation = app.conversation_ensure_default()
    task = app.submit("先记住偏好，再申请修改角色卡", conversation_id=conversation["id"],
                      expected_context_epoch=conversation["context_epoch"])
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    before_cancel = app.task_review(task.id)
    assert [s["tool_name"] for s in before_cancel["execution_progress"]["completed"]] == ["memory_remember"]
    assert [s["tool_name"] for s in before_cancel["steps"]] == ["role_update"]
    assert "不会撤销" in before_cancel["cancellation_notice"]
    facade = DesktopFacade(app)
    params = {"task_id": task.id, "command_id": "cancel-after-memory"}
    assert facade.execute("cancel", params)["accepted"]
    version = app.tasks.get(task.id).revision
    assert facade.execute("cancel", params)["accepted"]
    assert app.tasks.get(task.id).revision == version
    assert app.command_status(params["command_id"])["operation"] == "cancel"
    assert app.interaction_snapshot()["character_card"]["text"] == original
    assert len(app.journal.attempts(task.id)) == 1
    review = app.task_review(task.id)
    assert review["status"] == "Cancelled" and "仍然保留" in review["cancellation_notice"]
    timeline = app.conversation_timeline(app.conversation_ensure_default()["id"])["items"]
    assert any("此前已完成的操作仍然保留" in item.get("body", "") for item in timeline)
    memory_id = app.journal.attempts(task.id)[0].result.output["id"]
    assert app.core.memory_runtime.repository.get(memory_id) is not None


def test_cancel_receipt_survives_reopen_and_rejects_identity_reuse(app_model):
    app, model = app_model
    model.decide = lambda: change(app, "不会写入")
    first = app.submit("第一项修改")
    assert app.wait(first.id, 10).status == TaskStatus.WAITING
    app.cancel_task(first.id, command_id="stable-cancel")
    second = app.submit("第二项修改")
    assert app.wait(second.id, 10).status == TaskStatus.WAITING
    with pytest.raises(ValueError):
        app.cancel_task(second.id, command_id="stable-cancel")
    assert app.tasks.get(second.id).status == TaskStatus.WAITING
    app.cancel_task(second.id)
    assert app.close(timeout=10)
    with create_application(model.settings) as reopened:
        receipt = reopened.command_status("stable-cancel")
        assert receipt["accepted"] and receipt["task_id"] == first.id
        assert receipt["status"] == "Cancelled"
        assert reopened.cancel_task(first.id, command_id="stable-cancel").status == TaskStatus.CANCELLED


def test_cancel_receipt_rolls_back_if_cancellation_fails(app_model, monkeypatch):
    app, model = app_model
    model.decide = lambda: change(app, "不会写入")
    task = app.submit("申请修改")
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    def fail(task_id):
        raise RuntimeError("synthetic cancellation failure")
    monkeypatch.setattr(app.task_control.tasks, "request_cancel", fail)
    with pytest.raises(RuntimeError):
        app.cancel_task(task.id, command_id="cancel-rollback")
    assert not app.command_status("cancel-rollback")["accepted"]
    assert app.tasks.get(task.id).status == TaskStatus.WAITING


def test_old_automatic_role_contract_cannot_be_confirmed(app_model):
    app, model = app_model
    model.decide = lambda: change(app, "旧计划不能执行")
    task = app.submit("修改角色卡")
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    snapshot = app.journal.get_plan(task.id)
    legacy = replace(snapshot, tool_specs=tuple(replace(spec, permission_level=PermissionLevel.AUTOMATIC,
        contract_version="1") if spec.name == "role_update" else spec for spec in snapshot.tool_specs))
    with pytest.raises(WorkflowBlocked):
        app._verify_snapshot(legacy)


def test_proactive_work_cannot_request_role_card_changes(app_model):
    app, model = app_model
    model.decide = lambda: LLMResponse('{"action":"quiet","reply":""}')
    task = app.submit_proactive("查看是否需要问候", "合成关注事项", "proactive-no-card")
    assert app.wait(task.id, 10).status == TaskStatus.COMPLETED
    assert model.planning_tools and all("role_update" not in names for names in model.planning_tools)


def test_recovery_resume_has_receipt_without_restoring_authority(app_model):
    app, model = app_model
    model.decide = lambda: change(app, "等待明确批准的设定")
    task = app.submit("申请角色卡修改")
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    assert app.close(timeout=10)
    with create_application(model.settings) as reopened:
        assert reopened.tasks.get(task.id).status == TaskStatus.NEEDS_REVIEW
        facade = DesktopFacade(reopened)
        args = {"task_id": task.id, "command_id": "recover-unstarted"}
        assert facade.execute("resume", args)["accepted"]
        assert facade.execute("resume", args)["accepted"]
        assert reopened.command_status(args["command_id"])["operation"] == "resume_unstarted"
        assert reopened.tasks.get(task.id).status == TaskStatus.WAITING
        assert reopened.core.pending_grants(task.id) == (frozenset(), frozenset())
        assert not reopened.journal.attempts(task.id)
        reopened.cancel_task(task.id)


def test_uncertain_review_has_parameter_bound_receipt(app_model):
    app, model = app_model
    model.decide = lambda: change(app, "合成不确定操作")
    task = app.submit("模拟等待核实的操作")
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    app.tasks.update_status(task.id, TaskStatus.RUNNING)
    app.journal.start_step(task.id, 0)
    app.journal.mark_uncertain(task.id, "synthetic interruption")
    facade = DesktopFacade(app)
    args = dict(task_id=task.id, command_id="review-result", verdict="not_occurred", evidence="合成核查：操作未发生")
    assert facade.execute("review_uncertain", args)["accepted"]
    assert facade.execute("review_uncertain", args)["accepted"]
    assert app.command_status(args["command_id"])["operation"] == "review_uncertain"
    with pytest.raises(ValueError):
        facade.execute("review_uncertain", {**args, "verdict": "occurred"})
    with app.store.read_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM audit_outbox WHERE task_id=? AND event='uncertain_reviewed'", (task.id,)).fetchone()[0] == 1
    attempt = app.journal.attempts(task.id)[0]
    assert attempt.status == "Uncertain"


def test_repeat_of_committed_change_stops_without_requesting_new_approval(app_model):
    app, model = app_model
    calls = []
    def decide():
        if not calls:
            calls.append(change(app, "只写入一次"))
        return calls[0]
    model.decide = decide
    task = app.submit("修改一次角色卡")
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    confirm(app, task.id, "approve-once")
    result = app.wait(task.id, 10)
    assert result.status == TaskStatus.FAILED and "重复" in result.error
    assert len(app.journal.attempts(task.id)) == 1
    assert "confirm" not in app.task_review(task.id)["available_actions"]
    assert app.interaction_snapshot()["character_card"]["text"] == "只写入一次"


def test_identical_card_does_not_advance_revisions(tmp_path):
    runtime = CompanionRuntime(tmp_path / "role.sqlite3")
    try:
        before = runtime.interaction_snapshot()
        values = dict(text=before["character_card"]["text"], expected_revision=before["revision"], command_id="same-card")
        runtime.command("character_card", **values)
        runtime.command("character_card", **values)
        assert runtime.interaction_snapshot() == before
    finally:
        runtime.close()


@pytest.mark.parametrize("error,expected", [
    (CompanionRevisionConflict("private detail"), "版本已变化"),
    (CompanionValidationError("companion_command_id_conflict"), "操作编号"),
    (CompanionValidationError("companion_text_invalid"), "角色内容"),
    (CompanionValidationError("private detail"), "参数无效"),
])
def test_role_errors_have_specific_safe_diagnostics(error, expected):
    diagnostic = _execution_error(error)
    assert expected in diagnostic
    assert "private detail" not in diagnostic
    assert "file byte limit" not in diagnostic


def test_prompt_separates_habits_and_role_card():
    assert "相处习惯交由本轮反馈学习" in PLANNING_INSTRUCTION
    assert "不把习惯清单搬进角色卡" in PLANNING_INSTRUCTION
