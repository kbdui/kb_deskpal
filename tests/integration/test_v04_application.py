"""Public application facade: real local adapters, bounded worker, and ownership."""

from dataclasses import replace
from threading import Event

import pytest

from deskpal.application.instance import InstanceLock, InstanceLockError
from deskpal.config import LLMConfig, load_config
from deskpal.planner.models import Plan
from deskpal.tasks import TaskStatus
from deskpal.tools.models import ToolCall
from deskpal.workflow.models import QueueFull


class StaticPlanner:
    def __init__(self, plan):
        self.value = plan
        self.calls = []

    def plan(self, request, *, memories=(), tools=()):
        self.calls.append(request)
        return self.value


@pytest.fixture
def config(tmp_path):
    return load_config(environ={
        "DESKPAL_LLM_PROVIDER": "offline", "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "DESKPAL_DATA_DIR": str(tmp_path / "data"),
    })


def create(config, **kwargs):
    # The facade is imported here so low-level configuration tests remain usable
    # while the independently owned composition module is being developed.
    from deskpal.application.factory import create_application
    return create_application(config, **kwargs)


def test_offline_facade_creates_exactly_one_durable_task_and_step(config):
    with create(config) as app:
        result = app.handle_request("帮我执行一个任务")
        assert result.status == TaskStatus.COMPLETED
        assert result.tool_results[0].tool_name == "echo"
        assert len(app.tasks.list_tasks()) == 1
        snapshot = app.journal.get_plan(result.task_id)
        assert snapshot.task_id == result.task_id
        assert len(app.journal.attempts(result.task_id)) == 1
        assert app.journal.attempts(result.task_id)[0].status == "Succeeded"
    with create(config) as reopened:
        assert reopened.tasks.get(result.task_id).status == TaskStatus.COMPLETED
        assert reopened.journal.get_plan(result.task_id).hash == snapshot.hash
        assert len(reopened.tasks.list_tasks()) == 1


def test_busy_worker_keeps_submissions_idempotent_and_bounded(config, monkeypatch):
    config = replace(config, workflow=replace(config.workflow, queue_capacity=2))
    planner = StaticPlanner(Plan("synthetic", (ToolCall("echo", {"text": "done"}),)))
    app = create(config, planner=planner)
    entered, release = Event(), Event()
    original = app.tools.execute

    def blocked(call, context):
        if context.task_id == first_id[0]:
            entered.set()
            assert release.wait(10), "test did not release synthetic tool"
        return original(call, context)

    first_id = [None]
    # The tool blocks regardless of whether worker scheduling beats submit's return.
    def initial_blocked(call, context):
        if first_id[0] is None:
            first_id[0] = context.task_id
        return blocked(call, context)

    monkeypatch.setattr(app.tools, "execute", initial_blocked)
    try:
        first = app.submit("first", command_id="first-command")
        first_id[0] = first.id
        assert entered.wait(5)
        second = app.submit("second", command_id="second-command")
        assert second.id != first.id
        assert app.submit("second", command_id="second-command").id == second.id
        assert app.submit("first", command_id="first-command").id == first.id
        with pytest.raises(ValueError):
            app.submit("different payload", command_id="second-command")
        with pytest.raises(QueueFull):
            app.submit("third", command_id="third-command")
        assert len(app.tasks.list_tasks()) == 2
        release.set()
        assert app.wait(first.id, timeout=5).status == TaskStatus.COMPLETED
        assert app.wait(second.id, timeout=5).status == TaskStatus.COMPLETED
        assert planner.calls == ["first", "second"]
        third = app.submit("third", command_id="third-command")
        assert app.wait(third.id, timeout=5).status == TaskStatus.COMPLETED
    finally:
        release.set()
        assert app.close(timeout=5)


def test_restart_discards_both_grant_classes_and_confirms_exact_saved_plan(config):
    plan = Plan("exact approved plan", (
        ToolCall("write_text", {"path": "new.txt", "text": "precise content"}, "write-1"),
        ToolCall("document_copy", {"source": "new.txt", "destination": "copy.txt"}, "copy-1"),
        ToolCall("document_delete", {"path": "victim.txt"}, "delete-1"),
    ))
    planner = StaticPlanner(plan)
    with create(config, planner=planner) as first:
        victim = config.sandbox.workspace_dir / "victim.txt"
        victim.write_text("synthetic disposable file", encoding="utf-8")
        waiting = first.run("synthetic explicit plan", authorized_tools=("write_text",),
                            high_risk_authorized_tools=("document_delete",))
        assert waiting.status == TaskStatus.WAITING
        assert waiting.required_authorizations == ("document_copy",)
        saved = first.journal.get_plan(waiting.task_id)
        assert not (config.sandbox.workspace_dir / "new.txt").exists()
    never = StaticPlanner(Plan("must not replace historical plan"))
    with create(config, planner=never) as reopened:
        assert reopened.tasks.get(waiting.task_id).status == TaskStatus.NEEDS_REVIEW
        with pytest.raises(ValueError):
            reopened.confirm_task(waiting.task_id, authorized_tools=("write_text", "document_copy"),
                                  high_risk_authorized_tools=("document_delete",))
        restored = reopened.resume(waiting.task_id)
        assert restored.hash == saved.hash
        assert restored.canonical_json == saved.canonical_json
        with pytest.raises(ValueError):
            reopened.confirm_task(waiting.task_id, plan_hash="wrong-plan-hash")
        still_waiting = reopened.confirm_task(waiting.task_id, plan_hash=saved.hash)
        assert still_waiting.status == TaskStatus.WAITING
        assert set(still_waiting.required_authorizations) == {"write_text", "document_copy", "document_delete"}
        assert still_waiting.required_high_risk_authorizations == ("document_delete",)
        assert victim.exists()
        assert not (config.sandbox.workspace_dir / "new.txt").exists()
        done = reopened.confirm_task(waiting.task_id, plan_hash=saved.hash,
            authorized_tools=("write_text", "document_copy"), high_risk_authorized_tools=("document_delete",))
        assert done.status == TaskStatus.COMPLETED
        assert len(reopened.tasks.list_tasks()) == 1
        assert never.calls == []
        assert not victim.exists()
        assert (config.sandbox.workspace_dir / "copy.txt").read_text(encoding="utf-8") == "precise content"


def test_local_only_submission_is_blocked_before_cloud_planning(config, monkeypatch):
    settings = replace(config, llm=LLMConfig(provider="openai", model="synthetic-never-called",
                                           api_key="synthetic-test-key"))
    planner = StaticPlanner(Plan("cloud-free check"))
    with create(settings, planner=planner) as app:
        cloud_calls = []

        def forbidden_chat(*args, **kwargs):
            cloud_calls.append(args)
            raise AssertionError("No local source may reach the cloud provider")

        monkeypatch.setattr(app.core.llm, "chat", forbidden_chat)
        task = app.submit("synthetic local source body", command_id="local-only", use_policy="local_only")
        assert app.wait(task.id, timeout=5).status == TaskStatus.NEEDS_REVIEW
        assert planner.calls == []
        assert cloud_calls == []
        assert app.journal.get_plan(task.id) is None


def test_close_timeout_keeps_active_resources_and_lock_until_worker_stops(config, monkeypatch):
    planner = StaticPlanner(Plan("two steps", (
        ToolCall("echo", {"text": "first"}, "first"), ToolCall("echo", {"text": "second"}, "second"))))
    app = create(config, planner=planner)
    entered, release = Event(), Event()
    calls = []
    original = app.tools.execute

    def blocked(call, context):
        calls.append(call.call_id)
        entered.set()
        assert release.wait(10)
        # This lookup proves storage is still owned/open after close returned False.
        assert app.tasks.get(context.task_id) is not None
        return original(call, context)

    monkeypatch.setattr(app.tools, "execute", blocked)
    try:
        task = app.submit("synthetic blocked tool")
        assert entered.wait(5)
        assert app.close(timeout=0.01) is False
        assert app.closed is False
        assert app.tasks.get(task.id).cancel_requested
        with pytest.raises(InstanceLockError):
            InstanceLock(config.storage.data_dir).acquire()
        with pytest.raises(RuntimeError):
            app.submit("closing application")
        release.set()
        assert app.close(timeout=5) is True
        assert app.closed is True
        assert calls == ["first"]
        assert app.close(timeout=0) is True
        with InstanceLock(config.storage.data_dir):
            pass
    finally:
        release.set()
        app.close(timeout=5)
    with create(config) as reopened:
        assert reopened.tasks.get(task.id).status == TaskStatus.CANCELLED
        assert len(reopened.journal.attempts(task.id)) == 1


def test_same_data_directory_rejects_second_application_and_startup_failure_releases_lock(config, monkeypatch):
    from deskpal.application import factory

    with create(config):
        with pytest.raises(InstanceLockError):
            create(replace(config, storage=replace(config.storage,
                data_dir=config.storage.data_dir / ".." / "data")))
    original = factory.SQLiteMemoryRepository

    def failing_repository(*args, **kwargs):
        raise RuntimeError("synthetic startup failure")

    monkeypatch.setattr(factory, "SQLiteMemoryRepository", failing_repository)
    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        create(config)
    monkeypatch.setattr(factory, "SQLiteMemoryRepository", original)
    with create(config) as reopened:
        assert reopened.run("hello").status == TaskStatus.COMPLETED


def test_cancel_waiting_task_updates_wait_receipt_and_rejects_late_confirmation(config):
    planner = StaticPlanner(Plan("pending write", (ToolCall("write_text", {"path": "cancelled.txt", "text": "no effect"}),)))
    with create(config, planner=planner) as app:
        waiting = app.run("synthetic cancel waiting")
        assert waiting.status == TaskStatus.WAITING
        cancelled = app.cancel_task(waiting.task_id)
        assert cancelled.status == TaskStatus.CANCELLED
        assert app.wait(waiting.task_id, timeout=1).status == TaskStatus.CANCELLED
        with pytest.raises(ValueError):
            app.confirm_task(waiting.task_id, plan_hash=app.journal.get_plan(waiting.task_id).hash,
                             authorized_tools=("write_text",))
        assert not (config.sandbox.workspace_dir / "cancelled.txt").exists()


def test_cancel_during_inflight_planning_blocks_returned_effects(config, monkeypatch):
    entered, release = Event(), Event()

    class BlockedPlanner:
        def plan(self, request, *, memories=(), tools=()):
            entered.set()
            assert release.wait(10)
            return Plan("late response", (ToolCall("write_text", {"path": "late.txt", "text": "must not execute"}),))

    app = create(config, planner=BlockedPlanner())
    try:
        task = app.submit("synthetic planning cancellation", authorized_tools=("write_text",))
        assert entered.wait(5)
        cancel = app.cancel_task(task.id)
        assert cancel.cancel_requested
        assert cancel.status == TaskStatus.PLANNING
        release.set()
        result = app.wait(task.id, timeout=5)
        assert result.status == TaskStatus.CANCELLED
        assert app.journal.attempts(task.id) == ()
        assert not (config.sandbox.workspace_dir / "late.txt").exists()
    finally:
        release.set()
        assert app.close(timeout=5)


def test_forget_scrubs_cached_result_and_pending_audit_before_retry_delivery(config, monkeypatch):
    marker = "SYNTHETIC_WORKFLOW_FORGET_MARKER"
    planner = StaticPlanner(Plan(marker))
    with create(config, planner=planner) as app:
        memory = app.memory_command("remember", content=marker)
        original_record = app._audit_sink.record

        def unavailable_audit(*args, **kwargs):
            raise OSError("synthetic unavailable audit sink")

        monkeypatch.setattr(app._audit_sink, "record", unavailable_audit)
        result = app.run(marker)
        assert marker in result.response
        assert app.journal.get_dependencies(result.task_id)
        app.store.record_event("synthetic_pending_copy", result.task_id, {"body": marker})
        assert any(marker in repr(event.data) for event in app.store.pending_audit())
        receipt = app.memory_command("forget", memory_id=memory.id, expected_revision=memory.revision)
        assert result.task_id in receipt.affected_task_ids
        assert app.tasks.get(result.task_id).request == "[forgotten]"
        assert app.tasks.get(result.task_id).result is None
        assert marker not in repr(app.wait(result.task_id, timeout=1))
        assert all(marker not in repr(event.data) for event in app.store.pending_audit())
        log_path = config.storage.data_dir / "execution.jsonl"
        previous = log_path.read_bytes() if log_path.exists() else b""
        monkeypatch.setattr(app._audit_sink, "record", original_record)
        app.flush_audit()
        current = log_path.read_bytes() if log_path.exists() else b""
        assert marker.encode("utf-8") not in current[len(previous):]
    with create(config) as reopened:
        assert marker not in repr(reopened.review_task(result.task_id))


def test_pending_forget_blocks_display_submission_and_audit_until_cleanup_succeeds(config, monkeypatch):
    from deskpal.workflow.models import WorkflowBlocked

    marker = "SYNTHETIC_PENDING_DELETE_BODY"
    app = create(config, planner=StaticPlanner(Plan(marker)))
    runtime = app.core.memory_runtime
    original_forget, original_audit = runtime.forget, app._audit_sink.record

    def failing_forget(*args, **kwargs):
        raise OSError("synthetic unavailable memory database")

    def failing_audit(*args, **kwargs):
        raise OSError("synthetic unavailable audit sink")

    try:
        memory = app.memory_command("remember", content=marker)
        monkeypatch.setattr(app._audit_sink, "record", failing_audit)
        result = app.run(marker)
        app.store.record_event("synthetic_private_pending", result.task_id, {"body": marker})
        monkeypatch.setattr(runtime, "forget", failing_forget)
        pending = app.memory_command("forget", memory_id=memory.id, expected_revision=memory.revision)
        assert pending["status"] == "Pending"
        assert app.journal.pending_deletions()
        with pytest.raises(WorkflowBlocked):
            app.review_task(result.task_id)
        with pytest.raises(WorkflowBlocked):
            app.submit("must remain blocked")
        delivered = []
        monkeypatch.setattr(app._audit_sink, "record", lambda *args, **kwargs: delivered.append((args, kwargs)))
        app.flush_audit()
        assert marker not in repr(delivered)
        monkeypatch.setattr(runtime, "forget", original_forget)
        assert app.retry_cleanup()
        assert app.tasks.get(result.task_id).request == "[forgotten]"
        assert app.journal.pending_deletions() == ()
        assert marker not in repr(app.review_task(result.task_id))
    finally:
        monkeypatch.setattr(runtime, "forget", original_forget)
        monkeypatch.setattr(app._audit_sink, "record", original_audit)
        app.retry_cleanup()
        assert app.close(timeout=5)
