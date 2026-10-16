"""User-assisted native tray/notification probe with temporary synthetic data."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import tempfile
from time import monotonic
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def fingerprint() -> str:
    digest = sha256()
    for path in sorted((ROOT / "src").rglob("*.py")) + [ROOT / "pyproject.toml"]:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/validation/v05/gui-manual-session.json")
    parser.add_argument("--max-seconds", type=int, default=900)
    parser.add_argument("--default-close", action="store_true", help="Only check default window close and lock release")
    args = parser.parse_args()
    if not 60 <= args.max_seconds <= 3600:
        parser.error("max-seconds must be 60..3600")
    os.environ["QT_QPA_PLATFORM"] = "windows"
    sys.path.insert(0, str(ROOT / "src"))
    from PySide6.QtCore import QSettings, QTimer, Qt
    from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout
    from deskpal.application.factory import create_application
    from deskpal.application.instance import InstanceLock
    from deskpal.config import load_config
    from deskpal.gui.host import DesktopHost
    from deskpal.gui.preferences import GuiPreferences
    from deskpal.planner.direct import DirectToolPlanner
    from deskpal.tools.models import ToolCall

    app = QApplication(["Deskpal manual validation"])
    app.setQuitOnLastWindowClosed(False)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    started = monotonic()
    report: dict[str, Any] = {"started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "user-assisted native tray/notification interaction; synthetic temp config/QSettings/data; no cloud",
        "source_fingerprint_start": fingerprint(), "qpa": app.platformName(), "events": [],
        "default_close_phase": args.default_close, "status": "running",
        "visual_notification_attestation": None, "manual_acceptance_complete": False,
        "privacy": "event names, states, IDs/counts only; no request bodies, keys, desktop capture or user settings"}

    def save() -> None:
        report["elapsed_seconds"] = monotonic() - started
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def record(event: str, **metadata: Any) -> None:
        if len(report["events"]) < 500:
            report["events"].append({"event": event, "elapsed_seconds": monotonic() - started, **metadata})
        else:
            report["event_budget_exceeded"] = True
        save()
        print(json.dumps({"event": event, **metadata}, ensure_ascii=True), flush=True)

    with tempfile.TemporaryDirectory(prefix="deskpal-manual-") as directory:
        base = Path(directory)
        config = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
            "DESKPAL_EMBEDDING_PROVIDER": "disabled", "DESKPAL_DATA_DIR": str(base / "data"),
            "DESKPAL_WORKSPACE_DIR": str(base / "workspace")})
        preferences = GuiPreferences(QSettings(str(base / "gui.ini"), QSettings.Format.IniFormat))
        preferences.set_quiet_hours(0, 0)  # This temporary app preference only; no OS setting change.
        planner = DirectToolPlanner(ToolCall("write_text", {"path": "synthetic-manual.txt",
                                                           "text": "synthetic validation", "overwrite": True}))
        host = DesktopHost(lambda: create_application(config, planner=planner), preferences=preferences,
            notification_settings=QSettings(str(base / "notifications.ini"), QSettings.Format.IniFormat),
            quit_on_closed=False)
        host.window.setWindowTitle("Deskpal V0.5 临时验收 · 仅合成数据")
        if host.tray._tray is not None:
            host.tray._tray.setToolTip("Deskpal V0.5 临时验收")
            host.tray._tray.activated.connect(lambda reason: record("tray_activated", reason=reason.name))
        if host.tray._menu is not None:
            for action in host.tray._menu.actions():
                if not action.isSeparator():
                    label = action.text()
                    action.triggered.connect(lambda checked=False, label=label: record("tray_menu", action=label))
        report.update(tray_available=host.tray.available, supports_messages=host.tray.supports_messages)
        banner = QLabel("临时验收：先在设置页勾选关窗留托盘，关闭窗口后用托盘恢复。")
        banner.setTextFormat(Qt.TextFormat.PlainText)
        banner.setWordWrap(True)
        central = host.window.centralWidget()
        assert central is not None
        layout = central.layout()
        assert isinstance(layout, QVBoxLayout)
        layout.insertWidget(0, banner)
        task_ids: list[str] = []
        task_facts: dict[str, Any] = {}
        navigation: dict[str, str] = {}
        observed_navigation: set[str] = set()
        queries: set[str] = set()
        tick = QTimer()
        tick.setInterval(1000)

        def reply(value: Any) -> None:
            if not isinstance(value, dict):
                return
            queries.discard(str(value.get("request_id", "")))
            if not value.get("ok"):
                record("classified_error", code=value.get("error", {}).get("code"))
                return
            data = value.get("data", {})
            if value.get("operation") == "submit":
                task_id = str(data["task_id"])
                if task_id not in task_ids and len(task_ids) < 20:
                    task_ids.append(task_id)
                    record("task_submitted", task_id=task_id)
            elif value.get("operation") == "task":
                task = data.get("task", {})
                task_id = str(task.get("id", ""))
                fact = {"status": task.get("status"), "attempts": len(data.get("attempts", []))}
                if task_id in task_ids and task_facts.get(task_id) != fact:
                    task_facts[task_id] = fact
                    record("task_fact", task_id=task_id, **fact)

        def state(value: str) -> None:
            record("lifecycle", state=value)
            if value == "Closed":
                tick.stop()
                app.quit()

        def navigated(kind: str, entity_id: str) -> None:
            navigation.update(kind=kind, entity_id=entity_id)
            record("notification_navigation", kind=kind, entity_id=entity_id)

        def poll() -> None:
            if host.bridge.state != "Ready":
                return
            if args.default_close:
                banner.setText("默认关闭验收：保持关窗留托盘关闭，点击窗口右上角 × 正常退出。")
            elif preferences.notifications_enabled and not host.notifications.baseline_required:
                banner.setText("通知基线已就绪。可在对话提交“托盘通知验收”，保持工具授权未选；关闭到托盘后点击系统通知。")
            elif preferences.notifications_enabled:
                banner.setText("正在建立通知基线，请稍候再提交。")
            else:
                banner.setText("临时验收：设置页勾选关窗留托盘，关闭后用托盘恢复；通知测试时再勾选系统通知。")
            target = navigation.get("entity_id", "")
            snapshot = host.window.task_snapshot
            if target and target not in observed_navigation and snapshot.get("task", {}).get("id") == target:
                observed_navigation.add(target)
                record("navigation_result", visible=host.window.isVisible(), tab=host.window.tabs.currentIndex(),
                    task_id=target, status=snapshot["task"]["status"], attempts=len(snapshot.get("attempts", [])),
                    any_grant_selected=any(box.isChecked() for box in (*host.window._ordinary.values(), *host.window._high.values())))
            if task_ids and not queries:
                task_id = task_ids[int((monotonic() - started)) % len(task_ids)]
                try:
                    queries.add(host.bridge.send("task", {"task_id": task_id}, generation=987))
                except (BufferError, RuntimeError):
                    record("probe_query_pending")

        def expire() -> None:
            record("probe_time_limit")
            host.bridge.shutdown()

        def preference_changed(key: str, value: Any) -> None:
            if key in {"close_to_tray", "notifications_enabled", "quiet_start", "quiet_end"}:
                record("preference", key=key, value=value)

        host.bridge.state_changed.connect(state)
        host.bridge.reply.connect(reply)
        host.window.close_requested.connect(lambda: record("window_close_request", visible=host.window.isVisible(),
            close_to_tray=preferences.close_to_tray, hidden_to_tray=host._hidden_to_tray))
        host.tray.restore_requested.connect(lambda: record("tray_restore", visible=host.window.isVisible()))
        host.tray.quit_requested.connect(lambda: record("tray_quit_requested"))
        host.tray.message_clicked.connect(lambda: record("os_message_clicked"))
        host.notifications.notification_requested.connect(lambda message: record("notification_attempt"))
        host.notifications.navigation_requested.connect(navigated)
        preferences.changed.connect(preference_changed)
        tick.timeout.connect(poll)
        QTimer.singleShot(args.max_seconds * 1000, expire)
        record("probe_started", tray_available=host.tray.available)
        host.start()
        tick.start()
        app.exec()
        report.update(status="completed", closed=host.bridge.state == "Closed",
            bridge_threads_stopped=all(not thread.is_alive() for thread in host.bridge._threads),
            task_facts=task_facts, synthetic_write_effect_exists=(base / "workspace/synthetic-manual.txt").exists(),
            source_fingerprint_end=fingerprint())
        report["same_source_build"] = report["source_fingerprint_start"] == report["source_fingerprint_end"]
        if report["closed"] and report["bridge_threads_stopped"]:
            with InstanceLock(config.storage.data_dir):
                report["instance_lock_reacquired"] = True
        save()
    print(json.dumps({"output": str(output), "closed": report.get("closed"), "manual_acceptance_complete": False}), flush=True)
    return 0 if report.get("closed") and report["same_source_build"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
