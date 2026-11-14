"""拒绝授权后由原任务继续判断、学习、表达；全程使用合成档案与模型。"""
import json

import pytest

from deskpal.application import create_application
from deskpal.application.client import ClientGateway
from deskpal.application.desktop import DesktopFacade
from deskpal.llm.models import LLMResponse
from deskpal.planner.models import Plan
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolCall
from deskpal.workflow.models import WorkflowBlocked
from tests.integration.application.test_role_management_recovery import app_model, change, confirm


def reject(app, task_id, command="reject-operations", review=None):
    review = review or app.task_review(task_id)
    return ClientGateway(DesktopFacade(app)).execute("reject_tools", dict(task_id=task_id,
        command_id=command, plan_hash=review["plan_hash"], expected_revision=review["task_revision"]))


@pytest.fixture
def recorded(app_model):
    app, model = app_model
    captured = {"learning": [], "expression": []}
    original = model.chat
    def chat(messages, *, tools=()):
        if messages[0].content.startswith("[MEMORY_FEEDBACK]"):
            captured["learning"].append(messages)
        if messages[0].content.startswith("[EXPRESSION]"):
            captured["expression"].append(messages)
        return original(messages, tools=tools)
    model.chat = chat
    return app, model, captured


def test_reject_replans_then_runs_learning_and_expression(recorded):
    app, model, captured = recorded
    before = app.interaction_snapshot()["character_card"]
    model.decide = lambda: change(app, "应当拒绝的角色卡") if len(model.planning_inputs) == 1 else LLMResponse("不会修改角色卡，我会继续处理你的要求。")
    task = app.submit("调整相处习惯，不要改变角色身份")
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    review = app.task_review(task.id)
    assert "reject_tools" in review["available_actions"] and "cancel" not in review["available_actions"]
    assert not captured["learning"] and not captured["expression"]
    receipt = reject(app, task.id, review=review)
    assert receipt["accepted"]
    result = app.wait(task.id, 10)
    assert result.status == TaskStatus.COMPLETED
    assert app.tasks.get(task.id).cancel_requested is False
    assert app.interaction_snapshot()["character_card"] == before
    assert not app.journal.attempts(task.id)
    assert len(model.planning_inputs) == 2
    assert len(captured["learning"]) == 1 and len(captured["expression"]) == 1
    assert "authorization_rejections" in str(model.planning_inputs[-1])
    assert "authorization_rejections" in str(captured["learning"][0])
    assert "用户已拒绝此前列出的具体操作" in str(captured["expression"][0])
    assert "role_update" in model.planning_tools[-1]
    assert app.journal.rejected_authorizations(task.id)[0]["operations"][0]["arguments"]["text"] == "应当拒绝的角色卡"
    assert app.command_status("reject-operations")["operation"] == "reject_tools"
    assert app.task_review(task.id)["rejected_operations"][0]["display_name"] == "修改角色卡"


def test_same_tool_can_propose_a_different_operation_after_rejection(recorded):
    app, model, captured = recorded
    def decide():
        index = len(model.planning_inputs)
        if index <= 2:
            return LLMResponse("", (ToolCall("write_text", {"path": "A.txt" if index == 1 else "B.txt", "text": "新的文件内容"}, f"write-{index}"),))
        return LLMResponse("没有写入A，经过授权写入了B。")
    model.decide = decide
    task = app.submit("考虑写入文件")
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    reject(app, task.id)
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    assert "write_text" in model.planning_tools[-1]
    next_review = app.task_review(task.id)
    assert next_review["steps"][0]["arguments"]["path"] == "B.txt"
    assert next_review["missing_permissions"]["ordinary_tools"] == ["write_text"]
    assert not (app.core.workspace_dir / "A.txt").exists()
    assert not (app.core.workspace_dir / "B.txt").exists()
    confirm(app, task.id, "approve-B")
    assert app.wait(task.id, 10).status == TaskStatus.COMPLETED
    assert not (app.core.workspace_dir / "A.txt").exists()
    assert (app.core.workspace_dir / "B.txt").read_text(encoding="utf-8") == "新的文件内容"
    assert app.task_review(task.id)["rejected_operations"][0]["summary"] == "写入文本：A.txt"
    assert len(app.journal.attempts(task.id)) == 1
    assert len(model.planning_inputs) == 3
    assert len(captured["expression"]) == 1 and len(captured["learning"]) == 1
    assert "confirm" not in app.task_review(task.id)["available_actions"]


def test_previous_effects_survive_and_other_allowed_tools_can_run(recorded):
    app, model, captured = recorded
    def decide():
        index = len(model.planning_inputs)
        if index == 1:
            return LLMResponse("", (ToolCall("memory_remember", {"content": "合成偏好：喜欢茶"}, "remember"),))
        if index == 2:
            return change(app, "不应保存的角色身份")
        if index == 3:
            return LLMResponse("", (ToolCall("time", {}, "allowed-time"),))
        return LLMResponse("已保留记忆，并且没有修改角色卡。")
    model.decide = decide
    before = app.interaction_snapshot()["character_card"]
    task = app.submit("记住偏好并考虑调整角色")
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    reject(app, task.id)
    assert app.wait(task.id, 10).status == TaskStatus.COMPLETED
    attempts = app.journal.attempts(task.id)
    assert [a.result.tool_name for a in attempts] == ["memory_remember", "time"]
    assert all(a.status == "Succeeded" for a in attempts)
    assert app.core.memory_runtime.repository.get(attempts[0].result.output["id"]) is not None
    assert app.interaction_snapshot()["character_card"] == before
    assert captured["learning"] and captured["expression"]


def test_rejection_is_idempotent_and_old_confirmation_cannot_execute(recorded):
    app, model, _ = recorded
    model.decide = lambda: change(app, "不应保存") if len(model.planning_inputs) == 1 else LLMResponse("已经遵守拒绝。")
    task = app.submit("修改角色卡")
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    reviewed = app.task_review(task.id)
    reject(app, task.id, review=reviewed)
    assert app.wait(task.id, 10).status == TaskStatus.COMPLETED
    rounds = app.journal.round_count(task.id)
    assert reject(app, task.id, review=reviewed)["accepted"]
    assert app.journal.round_count(task.id) == rounds
    assert len(app.journal.rejected_authorizations(task.id)) == 1
    with pytest.raises(WorkflowBlocked):
        DesktopFacade(app).execute("confirm", dict(task_id=task.id, command_id="stale-approve",
            plan_hash=reviewed["plan_hash"], expected_revision=reviewed["task_revision"],
            ordinary_tools=["role_update"], high_risk_tools=[]))


@pytest.mark.parametrize("field,value", [("plan_hash", "stale"), ("task_revision", 999)])
def test_stale_rejection_does_not_change_the_pending_plan(recorded, field, value):
    app, model, _ = recorded
    model.decide = lambda: change(app, "尚未决定的修改")
    task = app.submit("修改角色卡")
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    review = app.task_review(task.id)
    stale = {**review, field: value}
    with pytest.raises(WorkflowBlocked):
        reject(app, task.id, review=stale)
    assert app.journal.get_plan(task.id).hash == review["plan_hash"]
    assert not app.journal.rejected_authorizations(task.id)
    assert not app.command_status("reject-operations")["accepted"]


def test_queue_failure_rolls_back_rejection_and_receipt(recorded, monkeypatch):
    app, model, _ = recorded
    model.decide = lambda: change(app, "等待决定的修改")
    task = app.submit("修改角色卡")
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    review = app.task_review(task.id)
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic queue failure")
    monkeypatch.setattr(app._dispatcher, "confirm", fail)
    with pytest.raises(RuntimeError):
        reject(app, task.id, review=review)
    assert app.journal.get_plan(task.id).hash == review["plan_hash"]
    assert not app.journal.rejected_authorizations(task.id)
    assert not app.command_status("reject-operations")["accepted"]
    assert app.tasks.get(task.id).status == TaskStatus.WAITING


def test_rejection_survives_restart_before_worker_runs(recorded, monkeypatch):
    app, model, _ = recorded
    before = app.interaction_snapshot()["character_card"]
    model.decide = lambda: change(app, "不应执行的旧修改")
    task = app.submit("申请修改角色卡")
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    with monkeypatch.context() as patch:
        patch.setattr(app._dispatcher, "confirm", lambda *args, **kwargs: None)
        reject(app, task.id)
    assert app.close(timeout=10)
    model.decide = lambda: LLMResponse("已记住本轮拒绝，不修改角色卡。")
    with create_application(model.settings) as reopened:
        assert reopened.command_status("reject-operations")["accepted"]
        assert reopened.journal.rejected_authorizations(task.id)[0]["tools"] == ["role_update"]
        review = reopened.task_review(task.id)
        assert not review["missing_permissions"]["ordinary_tools"]
        confirm(reopened, task.id, "resume-thinking")
        assert reopened.wait(task.id, 10).status == TaskStatus.COMPLETED
        assert reopened.interaction_snapshot()["character_card"] == before
        assert not reopened.journal.attempts(task.id)


def test_rejection_scope_does_not_leak_into_a_new_request(recorded):
    app, model, _ = recorded
    model.decide = lambda: change(app, "旧请求拒绝的内容") if len(model.planning_inputs) == 1 else LLMResponse("遵守拒绝。")
    task = app.submit("第一项请求")
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    reject(app, task.id)
    assert app.wait(task.id, 10).status == TaskStatus.COMPLETED
    model.decide = lambda: change(app, "用户在新请求中明确提出的新角色卡")
    following = app.submit("新请求：允许重新申请修改角色卡")
    assert app.wait(following.id, 10).status == TaskStatus.WAITING
    assert "role_update" in model.planning_tools[-1]
    assert not app.journal.rejected_authorizations(following.id)


def test_rejection_keeps_the_last_opportunity_to_rethink(recorded):
    app, model, captured = recorded
    model.decide = lambda: change(app, "待决定的角色卡") if len(model.planning_inputs) == 1 else LLMResponse("不会执行被拒的提议，继续正常回复。")
    task = app.submit("申请修改角色卡")
    assert app.wait(task.id, 10).status == TaskStatus.WAITING
    old = app.journal.get_plan(task.id)
    # 初始计划在 immutableplan 中，不计入 decision_round 的修订数量。
    for _ in range(7):
        app.journal.save_round(task.id, Plan("仍待用户决定", (), "continue"), old.tool_specs,
            old.workspace_dir, old.memory_refs)
    assert app.journal.round_count(task.id) == 7
    reject(app, task.id)
    assert app.wait(task.id, 10).status == TaskStatus.COMPLETED
    assert captured["learning"] and captured["expression"]
