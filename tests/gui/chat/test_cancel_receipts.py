"""取消回执和旧后台兼容；不启动真实后台或 WebEngine。"""
from tests.gui.chat.test_chat_refactor import coordinator, review


def test_cancel_receipt_stops_reconciliation(qtbot):
    bridge, state = coordinator()
    notices = []
    state.notice.connect(notices.append)
    state.task_action("t", "cancel")
    call = next(call for call in reversed(bridge.calls) if call[0] == "cancel")
    bridge.respond(call, {"accepted": True, "task_id": "t", "status": "Cancelled"})
    qtbot.waitUntil(lambda: "t" not in state.task_commands)
    assert "t" not in state.task_pending
    assert notices[-1] == "取消请求已受理；已完成的操作不会自动撤销。"
    state.poll()
    assert not [call for call in bridge.calls if call[0] == "command_status"]


def test_unknown_receipt_warns_once_and_waits_for_manual_retry(qtbot):
    bridge, state = coordinator()
    notices = []
    state.notice.connect(notices.append)
    state.task_commands["t"] = {"command_id": "unknown", "action": "confirm", "parameters": {}, "at": 0}
    state.task_pending.add("t")
    state.reconcile_task("t")
    bridge.respond(bridge.calls[-1], {"known": False, "accepted": False})
    qtbot.waitUntil(lambda: state.task_commands["t"].get("retryable", False))
    reads = len([c for c in bridge.calls if c[0] == "command_status"])
    state.task_commands["t"]["at"] = 0
    state.poll()
    assert len([c for c in bridge.calls if c[0] == "command_status"]) == reads
    assert len(notices) == 1


def test_legacy_cancel_is_reconciled_from_actual_task_status(qtbot):
    bridge, state = coordinator()
    state.task_commands["t"] = {"command_id": "legacy", "action": "cancel", "parameters": {}, "at": 0}
    state.task_pending.add("t")
    state.reconcile_task("t")
    bridge.respond(bridge.calls[-1], {"known": False, "accepted": False})
    qtbot.waitUntil(lambda: any(call[0] == "task_review" for call in bridge.calls))
    call = next(call for call in reversed(bridge.calls) if call[0] == "task_review")
    value = review()
    value.update(status="Cancelled", available_actions=[], task={"cancel_requested": True})
    bridge.respond(call, value)
    qtbot.waitUntil(lambda: "t" not in state.task_commands)
    assert "t" not in state.task_pending


def test_cancel_pending_state_does_not_close_other_command(qtbot):
    _, state = coordinator()
    record = {"command_id": "another", "action": "confirm", "parameters": {}, "at": 0}
    state.task_commands["t"] = record
    value = review()
    value.update(status="Cancelled", available_actions=[])
    state.set_review("t", value)
    assert state.task_commands["t"] is record


def test_late_mutation_receipt_cannot_clear_a_new_command(qtbot):
    bridge, state = coordinator()
    state.task_action("t", "cancel")
    call = next(call for call in reversed(bridge.calls) if call[0] == "cancel")
    replacement = {"command_id": "new-operation", "action": "confirm", "parameters": {}, "at": 0}
    state.task_commands["t"] = replacement
    bridge.respond(call, {"accepted": True, "task_id": "t", "status": "Cancelled"})
    qtbot.waitUntil(lambda: call[3] not in state.channel.pending)
    assert state.task_commands["t"] is replacement


def test_reject_uses_exact_review_and_is_not_a_cancel(qtbot):
    from deskpal.gui.bridge import CONTROL_OPERATIONS
    bridge, state = coordinator()
    value = review()
    value["available_actions"] = ["confirm", "reject_tools"]
    state.set_review("t", value)
    notices = []
    state.notice.connect(notices.append)
    state.task_action("t", "reject_tools")
    call = next(call for call in reversed(bridge.calls) if call[0] == "reject_tools")
    assert call[1]["plan_hash"] == value["plan_hash"]
    assert call[1]["expected_revision"] == value["task_revision"]
    assert call[1]["command_id"]
    assert not [call for call in bridge.calls if call[0] == "cancel"]
    assert "reject_tools" in CONTROL_OPERATIONS
    bridge.respond(call, {"accepted": True, "task_id": "t", "status": "Waiting"})
    qtbot.waitUntil(lambda: "t" not in state.task_commands)
    assert notices[-1] == "已拒绝本轮操作，正在继续处理你的请求。"
