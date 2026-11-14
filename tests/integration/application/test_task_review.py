"""D2 behavior cases prepared during development; not executed in D1-D5."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from threading import Event

import pytest

from deskpal.application.desktop import DesktopFacade
from deskpal.application.factory import create_application
from deskpal.application.reminders import ReminderInbox, ReminderPump
from deskpal.application.tool_catalog import argument_fields
from deskpal.config import load_config
from deskpal.conversation.repository import ConversationRepository
from deskpal.planner.models import Plan
from deskpal.scheduling import SchedulingService
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolCall, ToolResult
from deskpal.workflow import WorkflowStore
from deskpal.workflow.models import WorkflowBlocked
from deskpal.workflow.tasks import PersistentTaskManager


@pytest.fixture
def config(tmp_path):
    return load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
        "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "DESKPAL_DATA_DIR": str(tmp_path / "data")})


class ContextPlanner:
    """当前多轮接口：执行后的观察只用于收尾，不重复发出旧的固定操作。"""
    def plan_context(self, request, *, memories=(), tools=(), references=(), llm=None):
        if any(item.get("kind") == "execution_observations" for item in references):
            return Plan("已按实际执行结果完成。")
        return self.plan(request, memories=memories, tools=tools)


class WritePlanner(ContextPlanner):
    def plan(self, request, *, memories=(), tools=()):
        return Plan("先确认两项写入。", (
            ToolCall("echo", {"text": "只读前置说明"}),
            ToolCall("write_text", {"path": "one.txt", "text": "原计划正文"}),
            ToolCall("write_text", {"path": "two.txt", "text": "第二份正文"})))


def waiting(app):
    conversation = app.conversation_ensure_default()
    task = app.submit("synthetic intent", command_id="submission",
        conversation_id=conversation["id"], expected_context_epoch=conversation["context_epoch"])
    assert app.wait(task.id, 5).status == TaskStatus.WAITING
    review = app.task_review(task.id)
    return task, review


def confirmation(task_id, review, command_id="confirmation"):
    return {"task_id": task_id, "plan_hash": review["plan_hash"],
        "expected_revision": review["task_revision"], "ordinary_tools": ["write_text"],
        "high_risk_tools": [], "command_id": command_id}


def test_full_plan_projection_detached_fields_and_complete_grants(config):
    with create_application(config, planner=WritePlanner()) as app:
        task, review = waiting(app)
        assert len(review["steps"]) == 3
        assert [step["tool_name"] for step in review["steps"]] == ["echo", "write_text", "write_text"]
        assert review["steps"][1]["fields"][0]["label"] == "文件路径"
        assert review["required_permissions"] == {"ordinary_tools": ["write_text"], "high_risk_tools": []}
        assert review["missing_permissions"] == review["required_permissions"]
        assert "confirm" in review["available_actions"]
        facade = DesktopFacade(app)
        params = confirmation(task.id, review)
        for changes in ({"ordinary_tools": []}, {"ordinary_tools": ["write_text", "time"]},
                        {"high_risk_tools": ["write_text"]}, {"expected_revision": review["task_revision"] + 1}):
            with pytest.raises((ValueError, WorkflowBlocked)):
                facade.execute("confirm", {**params, **changes})
        assert app.journal.attempts(task.id) == ()
        assert not (config.sandbox.workspace_dir / "one.txt").exists()
        review["steps"][1]["arguments"]["text"] = "client mutation"
        assert app.task_review(task.id)["steps"][1]["arguments"]["text"] == "原计划正文"


def test_standalone_task_does_not_expose_internal_turn_as_persistent_chat(config):
    with create_application(config, planner=WritePlanner()) as app:
        task = app.submit("standalone intent", command_id="standalone-submission")
        assert app.wait(task.id, 5).status == TaskStatus.WAITING
        assert app.task_review(task.id)["conversation_id"] is None
        assert app.review_task(task.id)["task"].id == task.id


def test_nonblocking_confirmation_duplicate_payload_precedes_revision_and_no_second_command(config, monkeypatch):
    app = create_application(config, planner=WritePlanner())
    release, entered = Event(), Event()
    original = app.tools.execute

    def blocked(call, context):
        entered.set()
        assert release.wait(5)
        return original(call, context)

    try:
        task, review = waiting(app)
        facade = DesktopFacade(app)
        params = confirmation(task.id, review)
        monkeypatch.setattr(app.tools, "execute", blocked)
        assert facade.execute("confirm", params)["accepted"]
        assert entered.wait(5)
        active = app.task_review(task.id)
        assert active["confirmation_state"] == "accepted"
        assert active["confirmation_revision"] > review["confirmation_revision"]
        assert "confirm" not in active["available_actions"]
        # Same original CAS revision is accepted as history even after Running.
        assert facade.execute("confirm", params)["accepted"]
        with pytest.raises(ValueError):
            facade.execute("confirm", {**params, "ordinary_tools": []})
        with pytest.raises(WorkflowBlocked):
            facade.execute("confirm", {**params, "command_id": "second-click"})
        release.set()
        assert app.wait(task.id, 5).status == TaskStatus.COMPLETED
        assert len(app.journal.attempts(task.id)) == 3
        completed_review = app.task_review(task.id)
        assert completed_review["blocked_reason"] is None
        assert completed_review["attempt_summary"]["count"] == 3
        assert facade.execute("confirm", params)["accepted"]
    finally:
        release.set()
        assert app.close(5)


def test_restart_direct_confirmation_restores_original_plan_without_planning(config):
    with create_application(config, planner=WritePlanner()) as app:
        task, review = waiting(app)
    class NeverPlan(ContextPlanner):
        def plan(self, request, *, memories=(), tools=()):
            raise AssertionError("Restoring confirmation cannot replan")
    with create_application(config, planner=NeverPlan()) as reopened:
        validation_depths = []

        def validate_outside_workflow_transaction(task_id, refs):
            depth = getattr(reopened.store._local, "depth", 0)
            validation_depths.append(depth)
            assert depth == 0, "External sources must be checked outside workflow transactions"

        reopened.journal.add_validator(validate_outside_workflow_transaction)
        current = reopened.task_review(task.id)
        assert current["status"] == "NeedsReview"
        assert current["plan_hash"] == review["plan_hash"]
        assert current["granted_in_current_process"] == {"ordinary_tools": [], "high_risk_tools": []}
        assert "confirm" in current["available_actions"]
        assert DesktopFacade(reopened).execute("confirm", confirmation(task.id, current))["accepted"]
        assert reopened.wait(task.id, 5).status == TaskStatus.COMPLETED
        assert validation_depths and all(depth == 0 for depth in validation_depths)
        assert (config.sandbox.workspace_dir / "one.txt").read_text(encoding="utf-8") == "原计划正文"


def test_historical_confirmation_receipt_never_restores_execution_authority(config):
    with create_application(config, planner=WritePlanner()) as app:
        task, review = waiting(app)
        params = confirmation(task.id, review)
        DesktopFacade(app).execute("confirm", params)
        assert app.wait(task.id, 5).status == TaskStatus.COMPLETED
    with create_application(config) as reopened:
        assert DesktopFacade(reopened).execute("confirm", params)["accepted"]
        assert reopened.core.pending_grants(task.id) == (frozenset(), frozenset())
        assert reopened.task_review(task.id)["confirmation_state"] == "none"
        assert len(reopened.journal.attempts(task.id)) == 3
        with pytest.raises(WorkflowBlocked):
            DesktopFacade(reopened).execute("confirm", {**params, "command_id": "new-command"})


def test_recovered_confirmation_can_queue_while_other_task_executes(config, monkeypatch):
    with create_application(config, planner=WritePlanner()) as app:
        recovered, _ = waiting(app)
    class EchoPlanner(ContextPlanner):
        def plan(self, request, *, memories=(), tools=()):
            return Plan("完成", (ToolCall("echo", {"text": request}),))
    app = create_application(config, planner=EchoPlanner())
    entered, release = Event(), Event()
    original = app.tools.execute

    def blocked(call, context):
        if context.task_id != recovered.id:
            entered.set()
            assert release.wait(5)
        return original(call, context)
    try:
        monkeypatch.setattr(app.tools, "execute", blocked)
        other = app.submit("other task", command_id="other")
        assert entered.wait(5)
        review = app.task_review(recovered.id)
        assert DesktopFacade(app).execute("confirm", confirmation(recovered.id, review))["accepted"]
        assert app.task_review(recovered.id)["confirmation"]["queued"]
        release.set()
        assert app.wait(other.id, 5).status == TaskStatus.COMPLETED
        assert app.wait(recovered.id, 5).status == TaskStatus.COMPLETED
    finally:
        release.set()
        assert app.close(5)


def test_high_risk_grant_is_distinct_and_incomplete_confirmation_cannot_run_ordinary_step(config):
    class MixedPlanner(ContextPlanner):
        def plan(self, request, *, memories=(), tools=()):
            return Plan("写入后删除", (ToolCall("write_text", {"path": "new.txt", "text": "ok"}),
                ToolCall("document_delete", {"path": "old.txt"})))
    with create_application(config, planner=MixedPlanner()) as app:
        task = app.submit("mixed risk", command_id="mixed")
        assert app.wait(task.id, 5).status == TaskStatus.WAITING
        review = app.task_review(task.id)
        assert review["required_permissions"] == {"ordinary_tools": ["write_text"], "high_risk_tools": ["document_delete"]}
        params = confirmation(task.id, review)
        with pytest.raises(WorkflowBlocked):
            DesktopFacade(app).execute("confirm", params)
        with pytest.raises(ValueError):
            DesktopFacade(app).execute("confirm", {**params, "ordinary_tools": ["write_text", "document_delete"]})
        assert app.journal.attempts(task.id) == ()
        assert not (config.sandbox.workspace_dir / "new.txt").exists()


def test_command_status_namespaces_and_bounded_updates(config):
    with create_application(config, planner=WritePlanner()) as app:
        task, review = waiting(app)
        created = app.conversation_create("专项", "create-chat")
        assert app.command_status("submission")["task_id"] == task.id
        assert app.command_status("create-chat")["result_identity"] == created["id"]
        assert not app.command_status("unknown")["known"]
        with pytest.raises(ValueError):
            app.submit("different operation", command_id="create-chat")
        with pytest.raises(ValueError):
            DesktopFacade(app).execute("confirm", confirmation(task.id, review, "create-chat"))
        with pytest.raises(ValueError):
            app.conversation_create("collision", "submission")
        assert app.task_updates([task.id], {task.id: review["task_revision"]})["items"] == []
        for ids in ([str(index) for index in range(101)], [task.id, task.id], [{}]):
            with pytest.raises(ValueError):
                app.task_updates(ids)


def test_unstarted_recovery_without_plan_exposes_only_valid_resume(config):
    with create_application(config) as app:
        with app._dispatcher.management():
            task = app.tasks.submit("recover interrupted planning", command_id="interrupted")
            app.tasks.block_for_review(task.id, "synthetic interruption before planning")
        review = app.task_review(task.id)
        assert review["plan_hash"] is None
        assert "resume" in review["available_actions"] and "confirm" not in review["available_actions"]
        app.tasks.scrub(task.id)
        assert "resume" not in app.task_review(task.id)["available_actions"]


def test_current_schedule_lists_future_resources_after_conversation_clear(config):
    with create_application(config) as app:
        conversation = app.conversation_ensure_default()
        task = app.submit("你好，聊一会儿。", conversation_id=conversation["id"],
                          expected_context_epoch=conversation["context_epoch"])
        assert app.wait(task.id, 5).status == TaskStatus.COMPLETED
        saved_result = app.tasks.get(task.id).result
        timer = app.scheduling.create_timer(3600, label="未来计时器")
        app._reminders.register_result(task.id, timer_result(timer))
        future = datetime.now(timezone.utc) + timedelta(days=1)
        event = app.scheduling.create_calendar("未来日程", future.isoformat(), "UTC")
        facade = DesktopFacade(app)
        page = facade.execute("schedule_items", {"limit": 1})
        assert len(page["items"]) == 1 and page["has_more"]
        rest = facade.execute("schedule_items", {"offset": 1, "limit": 1})
        assert {item["source_id"] for item in page["items"] + rest["items"]} == {timer.id, event.id}
        assert facade.execute("schedule_items", {"source": "timer", "status": "pending"})["items"][0]["title"] == "未来计时器"
        assert facade.execute("reminder_feed", {})["items"] == []
        conversation = app.conversation_ensure_default()
        app.conversation_clear(conversation["id"], conversation["revision"], "clear-schedule-source")
        remaining = facade.execute("schedule_items", {"source": "timer"})["items"][0]
        assert remaining["title"] == "未来计时器"
        assert app.tasks.get(task.id).result == saved_result
        updated = app.scheduling.update_calendar(event.id, title="修改后日程")
        assert updated.title == "修改后日程"
        assert facade.execute("schedule_items", {"source": "calendar"})["items"][0]["title"] == "修改后日程"
        for bad in ({"limit": 101}, {"offset": -1}, {"source": []}, {"status": "unknown"}):
            with pytest.raises(ValueError):
                facade.execute("schedule_items", bad)


def test_historical_review_survives_unrelated_epoch_and_scrub_never_returns_body(config):
    with create_application(config, planner=WritePlanner()) as app:
        task, _ = waiting(app)
        with app.store.transaction() as connection:
            connection.execute("UPDATE privacy_control SET epoch=epoch+1 WHERE id=1")
        assert app.task_review(task.id)["steps"][1]["arguments"]["text"] == "原计划正文"
        facade = DesktopFacade(app)
        assert "原计划正文" in json.dumps(facade.execute("task_review", {"task_id": task.id}), ensure_ascii=False)
        app.tasks.scrub(task.id)
        cleared = facade.execute("task_review", {"task_id": task.id})
        assert cleared["steps"] == [] and cleared["plan_hash"] is None
        assert "原计划正文" not in json.dumps(cleared, ensure_ascii=False)


def test_review_current_target_summary_is_trusted_and_outside_frozen_plan_hash(config):
    class CancelPlanner(ContextPlanner):
        identity = ""

        def plan(self, request, *, memories=(), tools=()):
            # 计时器取消本身为自动操作；同组写入需要许可，供本例审核冻结参数。
            return Plan("取消指定计时器并写入结果", (ToolCall("timer_cancel", {"id": self.identity}),
                ToolCall("write_text", {"path": "timer-result.txt", "text": "取消计时器"})))
    planner = CancelPlanner()
    with create_application(config, planner=planner) as app:
        timer = app.scheduling.create_timer(3600, label="阅读休息")
        planner.identity = timer.id
        task = app.submit("cancel existing timer", command_id="cancel-intent")
        assert app.wait(task.id, 5).status == TaskStatus.WAITING
        review = app.task_review(task.id)
        step = review["steps"][0]
        assert step["arguments"]["id"] == timer.id
        assert step["target_summary"]["name"] == "阅读休息"
        assert step["target_summary"]["covered_by_plan_hash"] is False
        assert step["target_summary"]["label"] == "当前对象名称"
        assert app.journal.get_plan(task.id).hash == review["plan_hash"]


def test_catalogue_is_live_read_only_and_unknown_arguments_are_lossless(config, monkeypatch):
    with create_application(config) as app:
        monkeypatch.setattr(app.tools, "execute", lambda *a: pytest.fail("Catalogue cannot execute tools"))
        facade = DesktopFacade(app)
        catalogue = facade.execute("tools_catalog", {})
        assert {item["id"] for item in catalogue["items"]} == {spec.name for spec in app.tools.list_tools()}
        assert len(catalogue["items"]) == len(app.tools.list_tools())
        screenshot = facade.execute("tool_detail", {"id": "screenshot_capture"})["item"]
        assert screenshot["availability"] in {"unavailable", "check_at_execution"}
        assert screenshot["reason"]
        assert not catalogue["capabilities"]["installation"]
        assert facade.execute("tools_catalog", {"known_catalog_revision": catalogue["catalog_revision"]})["unchanged"]
        with pytest.raises(ValueError):
            facade.execute("tool_detail", {"id": "time", "catalog_revision": "old"})
        params = {"future_argument": {"unknown_child": [False, None, {"x": "保留"}]}}
        fields = argument_fields(params)
        assert fields[0]["value"] == params["future_argument"]
        assert fields[0]["children"][0]["children"][2]["children"][0]["value"] == "保留"


class Clock:
    def __init__(self):
        self.wall = datetime(2026, 10, 25, 1, tzinfo=timezone.utc)
        self.elapsed = 0.0

    def now(self):
        return self.wall

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.elapsed += seconds


@pytest.fixture
def inbox(tmp_path):
    clock = Clock()
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    scheduling = SchedulingService(tmp_path / "scheduling.sqlite3", clock=clock)
    repository = ConversationRepository(store)
    conversation = repository.ensure_default()
    tasks = PersistentTaskManager(store)
    task = tasks.submit("synthetic reminder", command_id="intent")
    repository.bind(task.id, conversation["id"], conversation["context_epoch"], command_id="intent")
    reminders = ReminderInbox(store, scheduling, now=clock.now)
    try:
        yield reminders, scheduling, clock, store, tasks, task, repository, conversation
    finally:
        scheduling.close()
        store.close()


def timer_result(timer):
    return ToolResult(True, timer.as_dict(), tool_name="timer_create", call_id="create")


def test_reminder_short_timer_association_race_is_repaired_and_two_events_share_task(inbox):
    reminders, scheduling, clock, store, tasks, task, repository, conversation = inbox
    timers = [scheduling.create_timer(1, label=f"提醒{index}") for index in range(2)]
    clock.advance(1)
    scheduling.poll_due()
    reminders.poll()  # Expiration wins the race against ToolManager's callback.
    assert len(reminders.feed()["items"]) == 2
    for timer in timers:
        reminders.register_result(task.id, timer_result(timer))
        reminders.register_result(task.id, timer_result(timer))
    page = repository.timeline(conversation["id"])
    assert len([item for item in page["items"] if item["kind"] == "reminder"]) == 2
    assert all(item["task_id"] == task.id for item in reminders.feed()["items"])
    assert reminders.poll() == 0
    assert reminders.feed()["unread_count"] == 2
    identity = reminders.feed()["items"][0]["id"]
    assert reminders.notification(identity) == {"kind": "reminder", "entity_id": identity, "revision": 1, "status": "due"}
    assert reminders.ack(identity, "read")["accepted"]
    assert reminders.ack(identity, "read")["accepted"]
    assert reminders.notification(identity) is None
    assert reminders.feed(unread=True)["unread_count"] == 1


def test_clearing_conversation_keeps_timer_and_detaches_late_tool_callback(inbox):
    reminders, scheduling, clock, store, tasks, task, repository, conversation = inbox
    timers = [scheduling.create_timer(1, label=label) for label in ("早关联", "晚关联")]
    reminders.register_result(task.id, timer_result(timers[0]))
    repository.cleanup(conversation["id"], conversation["revision"], "clear-reminders",
                       delete=False, scrub=tasks.clear_working_context)
    reminders.register_result(task.id, timer_result(timers[1]))
    clock.advance(1)
    scheduling.poll_due()
    assert reminders.poll() == 2
    items = reminders.feed()["items"]
    assert {item["label"] for item in items} == {"早关联", "晚关联"}
    assert all(item["conversation_id"] is None for item in items)
    assert all(reminders.notification(item["id"]) for item in items)


@pytest.mark.parametrize("bypass,paused,expected", [
    (False, False, "quiet"),
    (True, False, "due"),
    (True, True, "missed"),
])
def test_backend_quiet_defers_timer_and_recovers_after_restart(inbox, bypass, paused, expected):
    from deskpal.proactive.policy import NotificationPolicy
    reminders, scheduling, clock, store, *_ = inbox
    policy = NotificationPolicy(store, clock=clock.now, zone="UTC")
    policy.set_quiet(22 * 60, 8 * 60, bypass, "quiet-settings")
    if paused:
        policy.set("reminder", True, None, "pause")
    reminders.policy = policy
    scheduling.create_timer(1, label="免打扰时的提醒")
    clock.advance(1)
    scheduling.poll_due()
    assert reminders.poll() == 1
    item = reminders.feed()["items"][0]
    assert item["status"] == expected
    assert (reminders.notification(item["id"]) is not None) == (expected == "due")
    if expected == "quiet":
        clock.advance(7 * 3600)
        restarted = ReminderInbox(store, scheduling, now=clock.now,
            policy=NotificationPolicy(store, clock=clock.now, zone="UTC"))
        restarted.poll()
        assert restarted.notification(item["id"]) is not None
        restarted.poll()
        with store.read_connection() as db:
            assert db.execute("SELECT COUNT(*) FROM audit_outbox WHERE event='desktop_reminder_due'").fetchone()[0] == 1


def test_deleted_calendar_reminder_is_not_released_at_quiet_end(inbox):
    from deskpal.proactive.policy import NotificationPolicy
    reminders, scheduling, clock, store, *_ = inbox
    reminders.policy = NotificationPolicy(store, clock=clock.now, zone="UTC")
    entry = scheduling.create_calendar("会删除的日程", (clock.wall + timedelta(days=1)).isoformat(),
                                       "UTC", reminder_at=clock.wall.isoformat())
    scheduling.poll_due()
    reminders.poll()
    item = reminders.feed()["items"][0]
    assert item["status"] == "quiet"
    assert scheduling.delete_calendar(entry.id)
    clock.advance(7 * 3600)
    reminders.poll()
    assert reminders.feed()["items"] == []
    assert reminders.notification(item["id"]) is None


def test_reminder_cursor_and_inbox_rollback_together_then_retry_same_event(inbox, monkeypatch):
    reminders, scheduling, clock, store, tasks, task, repository, conversation = inbox
    scheduling.create_timer(1, label="事务提醒")
    clock.advance(1)
    scheduling.poll_due()
    original = store.record_event

    def fail_projection(event, task_id=None, data=None):
        if event == "desktop_reminder_due":
            raise OSError("synthetic outbox write failure")
        return original(event, task_id, data)
    monkeypatch.setattr(store, "record_event", fail_projection)
    with pytest.raises(OSError):
        reminders.poll()
    with store.read_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM desktop_reminder_inbox").fetchone()[0] == 0
        assert connection.execute("SELECT after_id FROM desktop_reminder_cursor WHERE source='timer'").fetchone()[0] == 0
    monkeypatch.setattr(store, "record_event", original)
    assert reminders.poll() == 1
    assert reminders.poll() == 0
    assert len(reminders.feed()["items"]) == 1


def test_source_revision_change_suppresses_label_navigation_and_unread(inbox):
    reminders, scheduling, clock, store, tasks, task, repository, conversation = inbox
    entry = scheduling.create_calendar("旧会议", (clock.wall + timedelta(hours=1)).isoformat(), "UTC", reminder_at=clock.wall.isoformat())
    reminders.register_result(task.id, ToolResult(True, entry.as_dict(), tool_name="calendar_create"))
    scheduling.poll_due()
    reminders.poll()
    identity = reminders.feed()["items"][0]["id"]
    scheduling.update_calendar(entry.id, title="改期", reminder_at=(clock.wall + timedelta(minutes=10)).isoformat())
    page = reminders.feed()
    assert page["items"] == [] and page["unread_count"] == 0
    assert reminders.detail(identity)["unavailable"]
    assert not reminders.notification(identity)


def test_source_label_only_update_suppresses_old_event_without_new_occurrence(inbox):
    reminders, scheduling, clock, store, tasks, task, repository, conversation = inbox
    entry = scheduling.create_calendar("旧名称", (clock.wall + timedelta(hours=1)).isoformat(), "UTC", reminder_at=clock.wall.isoformat())
    scheduling.poll_due()
    reminders.poll()
    identity = reminders.feed()["items"][0]["id"]
    clock.advance(1)
    scheduling.update_calendar(entry.id, title="新名称")
    assert scheduling.poll_due() == 0
    assert reminders.feed()["items"] == []
    assert reminders.detail(identity)["unavailable"]


def test_new_consumer_baseline_restart_missed_and_stream_generation_block(inbox):
    reminders, scheduling, clock, store, tasks, task, repository, conversation = inbox
    first = scheduling.create_timer(1, label="启用前")
    clock.advance(1)
    scheduling.poll_due()
    # Simulate first ever enablement rather than a restart with saved cursor.
    with store.transaction() as connection:
        connection.execute("DELETE FROM desktop_reminder_cursor")
    fresh = ReminderInbox(store, scheduling, now=clock.now)
    assert fresh.poll() == 0 and fresh.feed()["items"] == []
    second = scheduling.create_timer(1, label="退出期间")
    clock.advance(180)
    scheduling.poll_due()
    restarted = ReminderInbox(store, scheduling, now=clock.now)
    assert restarted.poll() == 1
    item = restarted.feed()["items"][0]
    assert item["source_id"] == second.id and item["status"] == "missed"
    assert restarted.notification(item["id"]) is None
    with store.transaction() as connection:
        connection.execute("UPDATE desktop_reminder_cursor SET generation='different-store'")
    assert restarted.poll() == 0
    assert all(row["blocked"] for row in restarted.feed()["streams"])


def test_reminder_pump_lifecycle_is_owned_independently_of_proactive(config):
    config = replace(config, proactive=replace(config.proactive, enabled=False))
    app = create_application(config)
    pump = app._reminder_pump
    assert isinstance(pump, ReminderPump)
    assert pump._thread.is_alive()
    assert app.close(5)
    assert not pump._thread.is_alive()
