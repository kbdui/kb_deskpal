"""Desktop boundary tests use only synthetic temporary stores and local tools."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from threading import Event

import pytest

from deskpal.application.desktop import DesktopFacade
from deskpal.application.factory import create_application
from deskpal.config import load_config
from deskpal.memory.contracts import MemoryDraft, MemoryItem, MemorySource, MemoryStatus
from deskpal.memory.repository import SQLiteMemoryRepository, _draft_data, _source_data
from deskpal.planner.models import Plan
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolCall
from deskpal.workflow.models import WorkflowBlocked


@pytest.fixture
def config(tmp_path):
    return load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
        "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "DESKPAL_DATA_DIR": str(tmp_path / "data")})


class Planner:
    def plan(self, request, *, memories=(), tools=()):
        return Plan("synthetic", (ToolCall("write_text", {"path": "demo.txt", "text": "synthetic secret"}),))


class EchoPlanner:
    def plan(self, request, *, memories=(), tools=()):
        return Plan("synthetic", (ToolCall("echo", {"text": "synthetic"}),))


def test_confirmation_receipt_returns_while_tool_blocks_and_duplicate_never_reexecutes(config, monkeypatch):
    app = create_application(config, planner=Planner())
    entered, release = Event(), Event()
    original = app.tools.execute
    calls = []

    def blocked(call, context):
        calls.append(call.name)
        entered.set()
        assert release.wait(5)
        return original(call, context)

    try:
        facade = DesktopFacade(app)
        task = facade.execute("submit", {"request": "synthetic", "command_id": "intent"})
        assert app.wait(task["task_id"], 5).status == TaskStatus.WAITING
        review = facade.execute("task", {"task_id": task["task_id"]})
        assert review["plan"]["plan"]["steps"][0]["arguments"]["text"] == "synthetic secret"
        assert review["plan"]["tool_specs"]
        monkeypatch.setattr(app.tools, "execute", blocked)
        parameters = {"task_id": task["task_id"], "plan_hash": review["plan"]["hash"],
            "ordinary_tools": ["write_text"], "high_risk_tools": [], "command_id": "confirm"}
        receipt = facade.execute("confirm", parameters)
        assert receipt["accepted"] and entered.wait(5)
        assert facade.execute("confirm", parameters)["task_id"] == task["task_id"]
        with pytest.raises(ValueError):
            facade.execute("confirm", {**parameters, "ordinary_tools": []})
        release.set()
        assert app.wait(task["task_id"], 5).status == TaskStatus.COMPLETED
        assert calls == ["write_text"]
        assert facade.execute("confirm", parameters)["accepted"]
    finally:
        release.set()
        assert app.close(5)
    with create_application(config) as reopened:
        assert DesktopFacade(reopened).execute("confirm", parameters)["accepted"]
        assert len(reopened.journal.attempts(task["task_id"])) == 1


def test_no_implicit_grants_and_bad_plan_never_queues(config):
    with create_application(config, planner=Planner()) as app:
        task = app.submit("synthetic")
        assert app.wait(task.id, 5).status == TaskStatus.WAITING
        facade = DesktopFacade(app)
        snapshot = app.journal.get_plan(task.id)
        with pytest.raises(WorkflowBlocked):
            facade.execute("confirm", {"task_id": task.id, "plan_hash": "stale", "command_id": "wrong"})
        facade.execute("confirm", {"task_id": task.id, "plan_hash": snapshot.hash, "command_id": "empty"})
        assert app.wait(task.id, 5).status == TaskStatus.WAITING
        assert not (config.sandbox.workspace_dir / "demo.txt").exists()
        assert not app.journal.attempts(task.id)


def test_json_detached_reviews_and_summary_lists_exclude_private_bodies(config):
    with create_application(config, planner=Planner()) as app:
        task = app.submit("synthetic private request")
        app.wait(task.id, 5)
        facade = DesktopFacade(app)
        baseline = facade.execute("notification_feed", {})
        assert baseline["baseline"] and baseline["items"] == []
        app.store.record_event("synthetic_state_refresh", task.id)
        page = facade.execute("tasks", {"limit": 1})
        assert "request" not in page["items"][0] and "result" not in page["items"][0]
        review = facade.execute("task", {"task_id": task.id})
        json.dumps(review, allow_nan=False)
        review["plan"]["plan"]["steps"][0]["arguments"]["text"] = "tampered"
        assert facade.execute("task", {"task_id": task.id})["plan"]["plan"]["steps"][0]["arguments"]["text"] == "synthetic secret"
        feed = facade.execute("notification_feed", {"after_sequence": baseline["next_sequence"]})
        assert feed["items"][0]["entity_id"] == task.id
        assert "synthetic" not in json.dumps(feed)


def test_notification_ranges_are_finite_and_unrelated_events_advance_cursor(config):
    with create_application(config) as app:
        facade = DesktopFacade(app)
        baseline = facade.execute("notification_feed", {})
        for index in range(5):
            app.store.record_event("synthetic-unrelated", data={"private": f"secret-{index}"})
        first = facade.execute("notification_feed", {"after_sequence": baseline["next_sequence"], "limit": 2})
        assert first["items"] == [] and first["has_more"]
        upper = first["until_sequence"]
        app.store.record_event("later-unrelated")
        second = facade.execute("notification_feed", {"after_sequence": first["next_sequence"],
            "until_sequence": upper, "limit": 100})
        assert second["next_sequence"] == upper and not second["has_more"]
        assert facade.execute("notification_validate", {"items": []})["items"] == []


def test_memory_sql_pages_epoch_and_replays_never_restore_forgotten_body(config, monkeypatch):
    with create_application(config) as app:
        facade = DesktopFacade(app)
        created = [facade.execute("memory", {"action": "remember", "content": f"private-{index}",
            "command_id": f"memory-{index}"}) for index in range(7)]
        runtime = app.core.memory_runtime
        monkeypatch.setattr(runtime, "list_items", lambda **_: pytest.fail("must use SQL paging"))
        pages = [facade.execute("memories", {"offset": offset, "limit": 3}) for offset in (0, 3, 6)]
        assert [len(page["items"]) for page in pages] == [3, 3, 1]
        assert [page["has_more"] for page in pages] == [True, True, False]
        assert len({item["id"] for page in pages for item in page["items"]}) == 7
        assert all(page["memory_epoch"] == runtime.epoch for page in pages)
        monkeypatch.undo()
        target = created[0]
        receipt = facade.execute("memory", {"action": "forget", "memory_id": target["id"],
            "expected_revision": target["revision"], "command_id": "forget"})
        assert receipt["memory_epoch"] > target["memory_epoch"]
        replay = facade.execute("memory", {"action": "remember", "content": "private-0", "command_id": "memory-0"})
        assert replay["replayed"] and "private-0" not in json.dumps(replay)
        erased = facade.execute("memories", {"status": "deleted"})
        assert erased["items"][0]["source"]["content"] == ""
        assert erased["items"][0]["id"] == target["id"]


@pytest.mark.parametrize("operation", ["tasks", "memories", "goals", "proposals", "notification_feed"])
@pytest.mark.parametrize("parameters", [{"limit": 101}, {"limit": True}, {"offset": -1}])
def test_every_list_rejects_unbounded_or_invalid_page(config, operation, parameters):
    with create_application(config) as app:
        with pytest.raises(ValueError):
            DesktopFacade(app).execute(operation, parameters)


def test_goal_commands_keep_cas_idempotency_and_explicit_completion(config):
    with create_application(config) as app:
        facade = DesktopFacade(app)
        payload = {"title": "synthetic", "success_criteria": "evidence", "command_id": "goal"}
        goal = facade.execute("create_goal", payload)
        assert facade.execute("create_goal", payload)["id"] == goal["id"]
        changed = facade.execute("update_goal", {"goal_id": goal["id"], "expected_revision": goal["revision"],
            "title": "changed", "command_id": "change"})
        with pytest.raises(ValueError):
            facade.execute("goal_status", {"goal_id": goal["id"], "expected_revision": goal["revision"],
                "status": "Completed", "evidence": "test", "command_id": "stale"})
        assert changed["status"] == "Active"


def test_settings_never_return_credential_or_endpoint(config):
    config = replace(config, llm=replace(config.llm, api_key="synthetic-credential",
        base_url="https://synthetic-credential.invalid"))
    with create_application(config) as app:
        settings = DesktopFacade(app).execute("settings", {})
        assert "synthetic-credential" not in json.dumps(settings)
        assert "api_key_env" not in settings and settings["restart_required"]


def test_submitted_forget_failure_is_pending_and_blocks_reads_until_retry(config, monkeypatch):
    with create_application(config) as app:
        facade = DesktopFacade(app)
        item = facade.execute("memory", {"action": "remember", "content": "synthetic private memory",
            "command_id": "remember"})
        original = app.core.memory_command

        def fail_forget(action, **parameters):
            if action == "forget":
                raise OSError("synthetic cleanup failure")
            return original(action, **parameters)

        monkeypatch.setattr(app.core, "memory_command", fail_forget)
        pending = facade.execute("memory", {"action": "forget", "memory_id": item["id"],
            "expected_revision": item["revision"], "command_id": "erase"})
        assert pending["status"] == "Pending" and pending["blocked"]
        assert app.journal.pending_deletions()
        with pytest.raises(WorkflowBlocked):
            facade.execute("memories", {})
        assert not facade.execute("retry_cleanup", {})["complete"]
        monkeypatch.setattr(app.core, "memory_command", original)
        assert facade.execute("retry_cleanup", {})["complete"]
        assert facade.execute("memories", {})["items"][0]["draft"]["content"] == ""


def test_begin_shutdown_stops_claims_keeps_resources_and_cancels_only_once(config, monkeypatch):
    app = create_application(config, planner=EchoPlanner())
    entered, release = Event(), Event()
    original = app.tools.execute

    def blocked(call, context):
        entered.set()
        assert release.wait(5)
        return original(call, context)

    monkeypatch.setattr(app.tools, "execute", blocked)
    try:
        active = app.submit("synthetic first")
        assert entered.wait(5)
        queued = app.submit("synthetic queued")
        app.begin_shutdown()
        revision = app.tasks.get(active.id).revision
        app.begin_shutdown()
        assert app.tasks.get(active.id).revision == revision
        assert not app.close(0)
        assert not app.closed and app.tasks.get(active.id).revision == revision
        assert app.tasks.get(queued.id).status == TaskStatus.CREATED
        assert not app.journal.attempts(queued.id)
        with pytest.raises(RuntimeError):
            app.submit("must not accept")
    finally:
        release.set()
        assert app.close(5)
    app.begin_shutdown()


def test_memory_response_cannot_gain_epoch_from_concurrent_forget(config, monkeypatch):
    import deskpal.application.desktop as desktop_module

    with create_application(config) as app:
        facade = DesktopFacade(app)
        serializing, release, forgetting = Event(), Event(), Event()
        original = desktop_module.json_value
        item_id = []

        def serialize(value):
            if isinstance(value, MemoryItem) and value.draft.content == "synthetic epoch body":
                item_id.append((value.id, value.revision))
                serializing.set()
                assert release.wait(5)
            return original(value)

        monkeypatch.setattr(desktop_module, "json_value", serialize)

        def forget():
            forgetting.set()
            return facade.execute("memory", {"action": "forget", "memory_id": item_id[0][0],
                "expected_revision": item_id[0][1], "command_id": "erase"})

        with ThreadPoolExecutor(max_workers=2) as pool:
            remember = pool.submit(facade.execute, "memory", {"action": "remember",
                "content": "synthetic epoch body", "command_id": "remember"})
            try:
                assert serializing.wait(5)
                erase = pool.submit(forget)
                assert forgetting.wait(5)
                assert not erase.done()
            finally:
                release.set()
            remembered, erased = remember.result(5), erase.result(5)
        assert remembered["memory_epoch"] < erased["memory_epoch"]


def test_candidate_expiry_filters_before_decode_at_ten_thousand_heads(tmp_path, monkeypatch):
    """SQL-only synthetic capacity seed; ingestion validation is tested separately."""
    now = datetime(2026, 10, 16, 8, tzinfo=timezone.utc)
    with SQLiteMemoryRepository(tmp_path / "memory.sqlite3", clock=lambda: now) as repo:
        source_rows, revision_rows, head_rows = [], [], []
        for index in range(10003):
            memory_id, source_id = f"memory-{index:05d}", f"source-{index:05d}"
            content = f"synthetic candidate {index}"
            source = MemorySource(content, id=source_id, created_at=now - timedelta(days=2))
            draft = MemoryDraft(content, content,
                expires_at=now + timedelta(days=1) if index < 10000 else now - timedelta(seconds=1))
            source_rows.append((source_id, json.dumps(_source_data(source))))
            revision_rows.append((memory_id, 1, "candidate", source_id, json.dumps(_draft_data(draft)),
                now.isoformat(), None, None, "synthetic capacity fixture", 0))
            head_rows.append((memory_id, 1))
        with repo._transaction():
            repo._db.executemany("INSERT INTO memory_sources VALUES(?,?)", source_rows)
            repo._db.executemany("INSERT INTO memory_revisions VALUES(?,?,?,?,?,?,?,?,?,?)", revision_rows)
            repo._db.executemany("INSERT INTO memory_heads VALUES(?,?)", head_rows)
            repo._db.execute("UPDATE memory_meta SET value=10003 WHERE name='epoch'")
        decoded = []
        original = repo._decode

        def decode(row):
            decoded.append(row["id"])
            return original(row)

        monkeypatch.setattr(repo, "_decode", decode)
        assert repo.epoch == 10004
        assert len(decoded) == 3
        assert all(int(memory_id.split("-")[1]) >= 10000 for memory_id in decoded)
        decoded.clear()
        first = repo.page_items(status=MemoryStatus.CANDIDATE, limit=100)
        again = repo.page_items(status=MemoryStatus.CANDIDATE, limit=100)
        assert first == again and first.epoch == 10004 and first.has_more
        assert len(first.items) == 100 and len(decoded) == 200
        assert len(repo.page_items(status=MemoryStatus.DELETED, limit=100).items) == 3
