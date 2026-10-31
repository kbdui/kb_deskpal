"""Cross-profile data and lifecycle scenarios using synthetic offline state."""

from datetime import datetime, timedelta, timezone
import json
from threading import Event

import pytest

from deskpal.application import create_application
from deskpal.application import factory as application_factory
from deskpal.application import service as application_service
from deskpal.application.instance import InstanceLockError
from deskpal.application.profiles import ProfileService
from deskpal.application import profiles as profile_module
from deskpal.application.reminders import ReminderInbox, ReminderPump
from deskpal.config import load_config
from deskpal.planner.models import Plan
from deskpal.scheduling import SchedulingService
from deskpal.tasks.models import TaskStatus
from deskpal.workflow.store import WorkflowStore


@pytest.fixture
def profile_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[llm]\nprovider="offline"\n[storage]\ndata_dir="data"\n'
        '[sandbox]\nworkspace_dir="workspace"\n[memory]\nmodel_cache_dir="data/models"\n',
        encoding="utf-8",
    )
    settings = load_config(path, environ={})
    profiles = ProfileService(path)
    default_id = profiles.execute("profile_list", {})["selected_id"]
    return path, settings, profiles, default_id


def _turn(app, marker):
    conversation = app.conversation_ensure_default()
    task = app.submit(
        marker,
        command_id="turn-" + marker,
        conversation_id=conversation["id"],
        expected_context_epoch=conversation["context_epoch"],
        use_policy="local_only",
    )
    result = app.wait(task.id, timeout=10)
    assert result.status == TaskStatus.COMPLETED
    return conversation["id"], task.id


def _write_business_data(app, marker, preset):
    memory = app.memory_command("remember", content=marker + " memory")
    app.conversation_create(marker + " conversation", command_id="conversation-" + marker)
    conversation_id, task_id = _turn(app, marker + " chat marker")
    start = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    calendar = app.scheduling.create_calendar(marker + " calendar", start, "UTC")
    state = app.companion_snapshot()
    companion = app.companion_command(
        "character_card", expected_revision=state["revision"], text=preset
    )
    return {
        "memory_id": memory.id,
        "conversation_id": conversation_id,
        "task_id": task_id,
        "calendar_id": calendar.id,
        "preset": companion["character_card"]["text"],
    }


def _assert_business_data(app, marker, expected_preset, *, present):
    memories = [item.draft.content for item in app.core.memory_runtime.repository.list_items()]
    conversations = app.conversation_list(limit=100)["items"]
    calendar = [entry.title for entry in app.scheduling.list_calendar()]
    default = app.conversation_ensure_default()
    timeline = app.conversation_timeline(default["id"])["items"]
    chat_text = "\n".join(item["body"] for item in timeline)
    groups = [
        ("memory", memories),
        ("conversation directory", [item["title"] for item in conversations]),
        ("calendar", calendar),
        ("default conversation timeline", [chat_text]),
    ]
    if present:
        for category, values in groups:
            assert any(marker in value for value in values), f"missing {category} marker {marker!r}: {values!r}"
    else:
        for category, values in groups:
            assert all(marker not in value for value in values), f"leaked {category} marker {marker!r}: {values!r}"
    assert app.companion_snapshot()["character_card"]["text"] == expected_preset


def test_profile_switch_and_factory_reopen_isolate_business_data(profile_config):
    path, settings, profiles, a_id = profile_config
    a_data_dir = settings.storage.data_dir / "profiles" / a_id
    with create_application(settings) as app_a:
        assert app_a.config.storage.data_dir == a_data_dir
        a_rows = _write_business_data(app_a, "A_PROFILE_MARKER", "warm")

    b_id = profiles.execute("profile_create", {"name": "B"})["profile_id"]
    profiles.execute("profile_select", {"profile_id": b_id})
    with create_application(settings) as app_b:
        assert app_b.config.storage.data_dir == settings.storage.data_dir / "profiles" / b_id
        _assert_business_data(app_b, "A_PROFILE_MARKER", app_b.companion_snapshot()["character_card"]["text"], present=False)
        b_rows = _write_business_data(app_b, "B_PROFILE_MARKER", "concise")

    profiles.execute("profile_select", {"profile_id": a_id})
    with create_application(settings) as reopened_a:
        assert reopened_a.config.storage.data_dir == a_data_dir
        _assert_business_data(reopened_a, "A_PROFILE_MARKER", "warm", present=True)
        _assert_business_data(reopened_a, "B_PROFILE_MARKER", "warm", present=False)
        assert reopened_a.core.memory_runtime.repository.get(a_rows["memory_id"]) is not None

    profiles.execute("profile_select", {"profile_id": b_id})
    with create_application(settings) as reopened_b:
        assert reopened_b.config.storage.data_dir == settings.storage.data_dir / "profiles" / b_id
        _assert_business_data(reopened_b, "B_PROFILE_MARKER", "concise", present=True)
        _assert_business_data(reopened_b, "A_PROFILE_MARKER", "concise", present=False)
        assert reopened_b.core.memory_runtime.repository.get(b_rows["memory_id"]) is not None


class _BlockingPlanner:
    def __init__(self):
        self.entered = Event()
        self.release = Event()

    def plan_context(self, request, *, memories=(), tools=(), references=(), llm):
        self.entered.set()
        if not self.release.wait(10):
            raise AssertionError("test did not release the offline planner")
        return Plan("A_PROFILE_BLOCKED_RESULT")

    def plan(self, request, *, memories=(), tools=()):
        return self.plan_context(request, memories=memories, tools=tools)


def test_switch_waits_for_blocking_offline_planner_and_keeps_task_in_source_profile(profile_config):
    _, settings, profiles, a_id = profile_config
    planner = _BlockingPlanner()
    b_id = profiles.execute("profile_create", {"name": "B"})["profile_id"]
    app_a = create_application(settings, planner=planner)
    conversation = app_a.conversation_open()
    task = app_a.submit(
        "A_PROFILE_BLOCKED_REQUEST",
        command_id="blocked-A-request",
        conversation_id=conversation["id"],
        expected_context_epoch=conversation["context_epoch"],
        use_policy="local_only",
    )
    try:
        assert planner.entered.wait(5)
        assert app_a.close(timeout=0.05) is False
        with pytest.raises(InstanceLockError):
            profiles.execute("profile_select", {"profile_id": b_id})
    finally:
        planner.release.set()
        if not app_a.closed:
            app_a.close(timeout=10)
    assert app_a.closed

    profiles.execute("profile_select", {"profile_id": b_id})
    with create_application(settings) as app_b:
        assert app_b.tasks.get(task.id) is None
        assert not any("A_PROFILE_BLOCKED" in item.draft.content
                       for item in app_b.core.memory_runtime.repository.list_items())

    profiles.execute("profile_select", {"profile_id": a_id})
    with create_application(settings) as reopened_a:
        saved = reopened_a.tasks.get(task.id)
        assert saved is not None and saved.request == "A_PROFILE_BLOCKED_REQUEST"
        assert reopened_a.conversation_timeline(conversation["id"])["conversation_id"] == conversation["id"]


class _FakeClock:
    def __init__(self):
        self.wall = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
        self.elapsed = 100.0

    def now(self):
        return self.wall

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.elapsed += seconds


def test_timer_due_while_profile_inactive_is_missed_without_backfill(profile_config, monkeypatch):
    _, settings, profiles, a_id = profile_config
    clock = _FakeClock()
    real_scheduling = SchedulingService

    class ClockedScheduling(real_scheduling):
        def __init__(self, path, *, execution_log=None, poll_interval=1.0, **kwargs):
            super().__init__(path, execution_log=execution_log, clock=clock,
                             poll_interval=poll_interval)

    monkeypatch.setattr(application_factory, "SchedulingService", ClockedScheduling)
    real_inbox = ReminderInbox
    monkeypatch.setattr(application_service, "ReminderInbox",
                        lambda store, scheduling, **kwargs: real_inbox(store, scheduling, now=clock.now, **kwargs))
    monkeypatch.setattr(ReminderPump, "start", lambda self: None)

    with create_application(settings) as app_a:
        timer = app_a.scheduling.create_timer(1, label="A_INACTIVE_REMINDER")
        a_database = app_a.config.storage.data_dir

    b_id = profiles.execute("profile_create", {"name": "B"})["profile_id"]
    clock.advance(2)
    profiles.execute("profile_select", {"profile_id": b_id})
    with create_application(settings) as app_b:
        assert app_b.scheduling.list_timers(status="all") == ()
        assert app_b._reminders.feed()["items"] == []

    profiles.execute("profile_select", {"profile_id": a_id})
    with create_application(settings) as reopened_a:
        assert reopened_a.config.storage.data_dir == a_database
        reopened_a.scheduling.poll_due()
        assert reopened_a._reminders.poll() == 1
        items = reopened_a._reminders.feed()["items"]
        item = next(value for value in items if value["source_id"] == timer.id)
        assert item["delivery_state"] == "missed"
        assert reopened_a._reminders.notification(item["id"]) is None
        with reopened_a.store.read_connection() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM audit_outbox WHERE event='desktop_reminder_due'"
            ).fetchone()[0] == 0


def test_interrupted_first_initialization_reuses_uuid_and_completes_once(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        '[llm]\nprovider="offline"\n[storage]\ndata_dir="data"\n'
        '[sandbox]\nworkspace_dir="workspace"\n[memory]\nmodel_cache_dir="data/models"\n',
        encoding="utf-8",
    )
    settings = load_config(path, environ={})
    profiles = ProfileService(path)
    root = settings.storage.data_dir
    root.mkdir(parents=True, exist_ok=True)
    (root / "workflow.sqlite3").write_bytes(b"synthetic old data")
    (root / "assets").mkdir()
    (root / "assets" / "old.png").write_bytes(b"synthetic old asset")

    real_initialize_stores = profile_module._initialize_stores
    attempted_directories = []

    def interrupted(directory, config):
        attempted_directories.append(directory)
        # Leave one actual valid application store, then simulate interruption.
        store = WorkflowStore(directory / "workflow.sqlite3")
        store.close()
        raise RuntimeError("synthetic first-init interruption")

    monkeypatch.setattr(profile_module, "_initialize_stores", interrupted)
    with pytest.raises(RuntimeError, match="synthetic first-init interruption"):
        profiles.execute("profile_list", {})
    marker = root / profile_module.INITIALIZING
    assert marker.is_file()
    assert not (root / "profiles.json").exists()
    first_id = json.loads(marker.read_text(encoding="utf-8"))["id"]
    partial = root / "profiles" / first_id
    assert (partial / "workflow.sqlite3").is_file()
    assert not (root / "workflow.sqlite3").exists()
    assert not (root / "assets").exists()

    monkeypatch.setattr(profile_module, "_initialize_stores", real_initialize_stores)
    completed = profiles.execute("profile_list", {})
    assert completed["selected_id"] == first_id
    assert len(completed["profiles"]) == 1
    assert not marker.exists()
    assert all((partial / database).is_file() for database in (
        "workflow.sqlite3", "memory.sqlite3", "scheduling.sqlite3", "companion.sqlite3"
    ))
    sentinel = partial / "new-profile-data"
    sentinel.write_text("keep", encoding="utf-8")
    assert profiles.execute("profile_list", {}) == completed
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert attempted_directories == [partial]
