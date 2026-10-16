import json
from datetime import datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QSystemTrayIcon, QWidget

from deskpal.gui.notifications import NotificationController
from deskpal.gui.preferences import GuiPreferences
from deskpal.gui.tray import TrayController


def temporary_settings(tmp_path, name="settings.ini"):
    settings = QSettings(str(tmp_path / name), QSettings.Format.IniFormat)
    settings.clear()
    settings.sync()
    return settings


def feed_item(kind="task", entity_id="t-1", revision=1, status="Waiting"):
    return {"kind": kind, "entity_id": entity_id, "revision": revision, "status": status}


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def complete_revalidation(controller, requested, valid_items):
    payload = requested[-1]
    controller.revalidated(valid_items, payload["request_id"])


def test_preferences_validate_and_store_only_gui_values(qtbot, tmp_path):
    settings = temporary_settings(tmp_path)
    prefs = GuiPreferences(settings)
    events = []
    prefs.changed.connect(lambda key, value: events.append((key, value)))

    assert prefs.close_to_tray is False
    assert prefs.notifications_enabled is False
    assert (prefs.quiet_start, prefs.quiet_end) == (22, 8)
    assert not prefs.set_quiet_hours(24, 8)
    assert not prefs.set_notifications_enabled("yes")
    assert prefs.set_quiet_hours(21, 7)
    assert prefs.set_notifications_enabled(True)
    assert settings.value("gui/quiet_start") == 21
    assert settings.value("gui/notifications_enabled") is True
    assert events[-1] == ("notifications_enabled", True)


def test_quiet_release_revalidates_and_discards_expired_revision(qtbot, tmp_path):
    settings = temporary_settings(tmp_path)
    prefs = GuiPreferences(temporary_settings(tmp_path, "prefs.ini"))
    prefs.set_notifications_enabled(True)
    prefs.set_quiet_hours(22, 8)
    clock = MutableClock(datetime(2026, 10, 15, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
    shown = []
    requested = []
    controller = NotificationController(prefs, settings, shown.append, clock)
    controller.revalidation_requested.connect(requested.append)

    # First pass establishes the baseline and never announces existing state.
    controller.ingest([feed_item()], next_sequence=100, baseline=True)
    assert shown == []
    controller.ingest([feed_item(revision=2)], next_sequence=101)
    clock.value = datetime(2026, 10, 16, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    controller.tick()
    assert len(requested) == 1
    assert shown == []

    # The item expired or changed while quiet, so the old revision is rejected.
    complete_revalidation(controller, requested, [feed_item(revision=3, status="Cancelled")])
    assert shown == []

    # A later terminal revision is independently revalidated and announced.
    controller.ingest([feed_item(revision=3, status="Cancelled")], next_sequence=102)
    controller.tick()
    complete_revalidation(controller, requested, [feed_item(revision=3, status="Cancelled")])
    assert shown == ["DeskPal 有 1 项更新，请打开应用查看。"]


def test_disabled_then_enabled_uses_fresh_baseline_and_restart_cursor(qtbot, tmp_path):
    settings = temporary_settings(tmp_path)
    prefs = GuiPreferences(temporary_settings(tmp_path, "prefs.ini"))
    shown = []
    clock = MutableClock(datetime(2026, 10, 16, 12, tzinfo=ZoneInfo("UTC")))
    controller = NotificationController(prefs, settings, shown.append, clock)
    controller.ingest([feed_item()], next_sequence=8, baseline=True)
    assert shown == []

    prefs.set_notifications_enabled(True)
    assert controller.baseline_required
    controller.ingest([feed_item(entity_id="old", revision=4)], next_sequence=300, baseline=True)
    assert controller.after_sequence == 300
    assert shown == []

    restarted = NotificationController(prefs, settings, shown.append, clock)
    assert restarted.after_sequence == 300
    assert not restarted.baseline_required
    restarted.ingest([feed_item(entity_id="new", revision=1)], next_sequence=301)
    assert restarted.after_sequence == 301
    assert shown == []  # Display requires a completed backend revalidation.


def test_cursor_advances_after_final_page_and_survives_ledger_eviction(qtbot, tmp_path):
    settings = temporary_settings(tmp_path)
    prefs = GuiPreferences(temporary_settings(tmp_path, "prefs.ini"))
    prefs.set_notifications_enabled(True)
    controller = NotificationController(prefs, settings)
    controller.ingest([], next_sequence=100, baseline=True)
    controller.ingest([], next_sequence=150, until_sequence=200, has_more=True)
    assert controller.after_sequence == 100
    bulk = [feed_item(entity_id=f"task-{number}", revision=1, status="Completed")
            for number in range(4200)]
    controller.ingest(bulk, next_sequence=200, until_sequence=200)
    assert controller.after_sequence == 200
    ledger = settings.value("gui/notification_ledger")
    assert len(json.loads(ledger)) == 4096

    restarted = NotificationController(prefs, settings)
    assert restarted.after_sequence == 200
    assert not restarted.baseline_required


def test_quiet_policy_crosses_day_and_dst_then_revalidates(qtbot, tmp_path):
    prefs = GuiPreferences(temporary_settings(tmp_path, "prefs.ini"))
    prefs.set_notifications_enabled(True)
    prefs.set_quiet_hours(22, 8)
    settings = temporary_settings(tmp_path, "notifications.ini")
    clock = MutableClock(datetime(2026, 3, 8, 1, 30, tzinfo=ZoneInfo("America/New_York")))
    requested = []
    shown = []
    controller = NotificationController(prefs, settings, shown.append, clock)
    controller.revalidation_requested.connect(requested.append)
    controller.ingest([feed_item()], next_sequence=1, baseline=True)
    controller.ingest([feed_item(revision=2)], next_sequence=2)
    assert requested == []
    clock.value = datetime(2026, 3, 8, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    controller.tick()
    assert len(requested) == 1
    complete_revalidation(controller, requested, [feed_item(revision=2)])
    assert len(shown) == 1


def test_revalidation_reply_crossing_quiet_start_is_retained_and_rechecked(qtbot, tmp_path):
    prefs = GuiPreferences(temporary_settings(tmp_path, "prefs.ini"))
    prefs.set_notifications_enabled(True)
    prefs.set_quiet_hours(22, 8)
    clock = MutableClock(datetime(2026, 10, 16, 21, 59, tzinfo=ZoneInfo("Asia/Shanghai")))
    requested = []
    shown = []
    controller = NotificationController(prefs, temporary_settings(tmp_path), shown.append, clock)
    controller.revalidation_requested.connect(requested.append)
    controller.ingest([feed_item()], next_sequence=1, baseline=True)
    controller.ingest([feed_item(revision=2)], next_sequence=2)
    assert len(requested) == 1

    clock.value = datetime(2026, 10, 16, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    complete_revalidation(controller, requested, [feed_item(revision=2)])
    assert shown == []
    assert controller.pending_candidates == (("task", "t-1"),)

    clock.value = datetime(2026, 10, 17, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    controller.tick()
    assert len(requested) == 2
    assert requested[-1]["request_id"] != requested[0]["request_id"]
    complete_revalidation(controller, requested, [feed_item(revision=2)])
    assert shown == ["DeskPal 有 1 项更新，请打开应用查看。"]


def test_late_revalidation_cannot_restore_cleared_pending(qtbot, tmp_path):
    prefs = GuiPreferences(temporary_settings(tmp_path, "prefs.ini"))
    prefs.set_notifications_enabled(True)
    clock = MutableClock(datetime(2026, 10, 16, 12, tzinfo=ZoneInfo("UTC")))
    shown = []
    requested = []
    controller = NotificationController(prefs, temporary_settings(tmp_path), shown.append, clock)
    controller.revalidation_requested.connect(requested.append)
    controller.ingest([feed_item()], next_sequence=1, baseline=True)
    controller.ingest([feed_item(revision=2)], next_sequence=2)
    controller.tick()
    stale_request_id = requested[-1]["request_id"]
    controller.clear_pending()
    controller.revalidated([feed_item(revision=2)], stale_request_id)
    assert shown == []


def test_disable_reenable_invalidates_inflight_validation_and_rebaselines(qtbot, tmp_path):
    prefs = GuiPreferences(temporary_settings(tmp_path, "prefs.ini"))
    prefs.set_notifications_enabled(True)
    clock = MutableClock(datetime(2026, 10, 16, 12, tzinfo=ZoneInfo("UTC")))
    requested = []
    shown = []
    controller = NotificationController(prefs, temporary_settings(tmp_path), shown.append, clock)
    controller.revalidation_requested.connect(requested.append)
    controller.ingest([feed_item()], next_sequence=1, baseline=True)
    controller.ingest([feed_item(revision=2)], next_sequence=2)
    stale_request_id = requested[-1]["request_id"]

    prefs.set_notifications_enabled(False)
    prefs.set_notifications_enabled(True)
    controller.revalidated([feed_item(revision=2)], stale_request_id)
    assert controller.baseline_required
    assert controller.pending_candidates == ()
    assert shown == []
    controller.ingest([feed_item(revision=2)], next_sequence=3, baseline=True)
    assert shown == []


def test_ledger_persists_before_sequence_cursor(qtbot, tmp_path):
    class RecordingSettings(QSettings):
        def __init__(self, path):
            super().__init__(str(path), QSettings.Format.IniFormat)
            self.writes = []

        def setValue(self, key, value):
            self.writes.append(key)
            super().setValue(key, value)

    settings = RecordingSettings(tmp_path / "ordered.ini")
    settings.clear()
    settings.sync()
    prefs = GuiPreferences(temporary_settings(tmp_path, "prefs.ini"))
    prefs.set_notifications_enabled(True)
    controller = NotificationController(prefs, settings)
    controller.ingest([feed_item()], next_sequence=100, baseline=True)
    assert settings.writes.index("gui/notification_ledger") < settings.writes.index("gui/notification_baseline_complete")
    assert settings.writes.index("gui/notification_baseline_complete") < settings.writes.index(
        "gui/notification_after_sequence")


def test_tray_unavailable_never_hides_the_only_window(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    prefs = GuiPreferences(temporary_settings(tmp_path))
    prefs.set_close_to_tray(True)
    window = QWidget()
    tray = TrayController(window, prefs)
    assert tray.available is False
    assert tray.should_hide_on_close() is False
    window.close()


def test_tray_availability_is_rechecked_when_window_closes(qtbot, monkeypatch, tmp_path):
    availability = [False, True, False]
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable",
                        staticmethod(lambda: availability.pop(0)))
    prefs = GuiPreferences(temporary_settings(tmp_path))
    prefs.set_close_to_tray(True)
    window = QWidget()
    tray = TrayController(window, prefs)
    # Simulate a tray that appeared after controller creation, then disappeared.
    tray._tray = object()
    assert tray.should_hide_on_close() is True
    assert tray.should_hide_on_close() is False
