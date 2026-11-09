"""Bundled plugin contracts across the real tool registry and selected profile."""

from datetime import datetime, timedelta, timezone
import sqlite3
from time import monotonic, sleep

import pytest

from deskpal.application.desktop import DesktopFacade
from deskpal.application.factory import create_application
from deskpal.config import load_config
from deskpal.plugins.manifest import PluginManifest
from deskpal.plugins.runtime import PluginData
from deskpal.workflow.store import WorkflowStore


def test_schedule_plugin_tool_event_page_and_restart(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[llm]\nprovider="offline"\n[storage]\ndata_dir="data"\n'
        '[sandbox]\nworkspace_dir="workspace"\n[proactive]\nenabled=false\n',
        encoding="utf-8",
    )
    config = load_config(config_path, environ={})
    page_args = {"plugin_id": "schedule"}
    with create_application(config) as app:
        facade = DesktopFacade(app)
        plugins = {entry["id"]: entry for entry in facade.execute("plugins_catalog", {})["items"]}
        assert set(plugins) == {"general", "workspace", "screen", "schedule", "agent"}
        grouped = [tool for group in plugins.values() for tool in group["tool_ids"]]
        assert len(grouped) == len(set(grouped))
        assert set(grouped) == {tool.name for tool in app.tools.list_tools()}
        assert plugins["schedule"]["event_sources"] == ["calendar_due", "timer_due"]
        assert facade.execute("tool_detail", {"id": "calendar_delete"})["item"]["plugin_id"] == "schedule"
        schedule_tools = facade.execute("tools_catalog", page_args)["items"]
        assert {tool["id"] for tool in schedule_tools} == set(plugins["schedule"]["tool_ids"])

        initial = facade.execute("plugin_page", page_args)
        assert initial["rows"] == []
        assert initial["switches"][0]["value"] is False
        option = {**page_args, "key": "timer_only", "value": True, "expected_revision": 0}
        assert facade.execute("plugin_set_option", option)["revision"] == 1
        assert facade.execute("plugin_set_option", option)["revision"] == 1
        with pytest.raises(ValueError, match="revision"):
            facade.execute("plugin_set_option", {**option, "value": False})
        with pytest.raises(ValueError):
            facade.execute("plugin_set_option", {**option, "key": "unlisted"})

        now = datetime.now(timezone.utc)
        app.scheduling.create_calendar("日程样例", (now + timedelta(hours=1)).isoformat(), "UTC",
                                       reminder_at=now.isoformat())
        app.scheduling.create_timer(1, label="计时样例")
        scope = app.plugins.data("schedule")
        deadline = monotonic() + 8
        while not (scope.events("calendar_due") and scope.events("timer_due")):
            assert monotonic() < deadline
            sleep(0.05)
        events = scope.events("timer_due", after_sequence=0)
        assert len(events) == 1
        assert events[0]["payload"] == {"reminder_id": events[0]["event_id"]}
        assert scope.events("timer_due", after_sequence=events[0]["sequence"]) == []
        assert [row["title"] for row in facade.execute("plugin_page", page_args)["rows"]] == ["计时样例"]

    with create_application(config) as app:
        facade = DesktopFacade(app)
        page = facade.execute("plugin_page", page_args)
        assert page["switches"][0]["value"] is True
        assert len(page["rows"]) == 1
        facade.execute("plugin_set_option", {**option, "value": False, "expected_revision": 1})
        assert {row["title"] for row in facade.execute("plugin_page", page_args)["rows"]} == {
            "计时样例", "日程样例",
        }


def test_plugin_state_is_scoped_and_events_roll_back_with_host_transaction(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    try:
        alpha = PluginData(store, PluginManifest("alpha", "Alpha", "Alpha provider", ("updated",), True))
        beta = PluginData(store, PluginManifest("beta", "Beta", "Beta provider", ("updated",), True))
        alpha.set_state("cursor", {"offset": 4}, expected_revision=0)
        assert beta.state("cursor") == (None, 0)
        stamp = "2026-11-06T12:00:00+08:00"
        with pytest.raises(RuntimeError), store.transaction():
            alpha.emit("updated", "one", stamp, {"offset": 4})
            raise RuntimeError("abort transaction")
        assert alpha.events("updated") == []
        alpha.emit("updated", "one", stamp, {"offset": 4})
        alpha.emit("updated", "one", stamp, {"offset": 4})
        assert len(alpha.events("updated")) == 1
        assert beta.events("updated") == []
        with pytest.raises(ValueError):
            alpha.emit("unlisted", "one", stamp, {})
    finally:
        store.close()


def test_workflow_seven_upgrades_to_plugin_storage_without_losing_data(tmp_path):
    path = tmp_path / "workflow.sqlite3"
    WorkflowStore(path).close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE plugin_state")
        db.execute("DROP TABLE plugin_event")
        db.execute("UPDATE schema_version SET version=7")
        db.execute("UPDATE privacy_control SET epoch=7 WHERE id=1")
    store = WorkflowStore(path)
    try:
        with store.read_connection() as db:
            assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 8
            assert db.execute("SELECT epoch FROM privacy_control").fetchone()[0] == 7
            assert db.execute("SELECT COUNT(*) FROM plugin_state").fetchone()[0] == 0
    finally:
        store.close()
    assert len(list(tmp_path.glob("workflow.sqlite3.schema-7-*.backup"))) == 1
