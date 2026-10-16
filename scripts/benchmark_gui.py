"""Opt-in GUI + real local backend measurements using only synthetic data.

Qt offscreen is explicitly a logic/resource run, not evidence of physical tray,
monitors, DPI transitions or suspend. Source fingerprints are checked at the end.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
from time import monotonic, process_time, sleep
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from benchmark_workflow import latencies, metadata, resources, save


def fingerprint() -> str:
    digest = sha256()
    for path in sorted((ROOT / "src").rglob("*.py")) + [ROOT / "pyproject.toml"]:
        digest.update(str(path.relative_to(ROOT)).replace("\\", "/").encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("idle", "soak"), default="soak")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--warmup-seconds", type=float, default=5)
    parser.add_argument("--platform", choices=("offscreen", "windows"), default="offscreen")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 86400 or not 0 <= args.warmup_seconds <= 60:
        parser.error("duration out of bounds")
    os.environ["QT_QPA_PLATFORM"] = args.platform
    from PySide6.QtCore import QObject, QSettings, QTimer, Slot, qVersion
    from PySide6.QtWidgets import QApplication
    from deskpal.application.factory import create_application
    from deskpal.config import load_config
    from deskpal.gui.host import DesktopHost
    from deskpal.gui.preferences import GuiPreferences
    from deskpal.planner.models import Plan
    from deskpal.tools.models import ToolCall

    class SyntheticPlanner:
        def plan(self, request: str, *, memories: Any = (), tools: Any = ()) -> Plan:
            if "slow" in request or "cancel" in request:
                sleep(1 if "cancel" in request else .25)
            if "confirm" in request:
                return Plan("synthetic exact confirmation", (ToolCall("write_text", {
                    "path": "synthetic.txt", "text": "synthetic", "overwrite": True}),))
            return Plan("synthetic response", (ToolCall("echo", {"text": "synthetic"}),))

    app = QApplication(["deskpal-gui-benchmark"])
    app.setQuitOnLastWindowClosed(False)
    report = metadata("gui-" + args.mode)
    report.update(benchmark="deskpal-gui-v1", qt=qVersion(), qt_platform=args.platform,
                  source_fingerprint_start=fingerprint(), requested_seconds=args.seconds,
                  scope="real DesktopHost and Qt event loop; synthetic temp databases; offline injected planner",
                  physical_windows_interaction_tested=False)
    report["packages"].update({name: importlib.metadata.version(name) for name in ("PySide6-Essentials", "shiboken6", "pytest-qt")})
    report["baseline_before_gui"] = resources()

    with tempfile.TemporaryDirectory(prefix="deskpal-gui-benchmark-") as folder:
        root = Path(folder)
        config = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline", "DESKPAL_EMBEDDING_PROVIDER": "disabled",
            "DESKPAL_DATA_DIR": str(root / "data"), "DESKPAL_WORKSPACE_DIR": str(root / "workspace")})
        prefs = GuiPreferences(QSettings(str(root / "gui.ini"), QSettings.Format.IniFormat))
        # Local generic notifications only; no Windows system settings are changed.
        prefs.set_notifications_enabled(True)
        prefs.set_quiet_hours(0, 0)
        host = DesktopHost(lambda: create_application(config, planner=SyntheticPlanner()), preferences=prefs,
            notification_settings=QSettings(str(root / "notifications.ini"), QSettings.Format.IniFormat), quit_on_closed=False)

        class Runner(QObject):
            def __init__(self) -> None:
                super().__init__()
                self.created = monotonic()
                self.started = 0.0
                self.cpu_started = 0.0
                self.last_heartbeat = 0.0
                self.heartbeats: list[float] = []
                self.samples: list[dict[str, Any]] = []
                self.latency: list[float] = []
                self.confirm_latency: list[float] = []
                self.cancel_latency: list[float] = []
                self.confirm_started = 0.0
                self.cancel_started = 0.0
                self.submitted: set[str] = set()
                self.finished: set[str] = set()
                self.attempts_checked = 0
                self.maximum_pending = 0
                self.failures: list[str] = []
                self.current_task = ""
                self.task_read = ""
                self.confirmed: set[str] = set()
                self.submit_started = 0.0
                self.iterations = 0
                self.next_cycle = 0.0
                self.next_sample = 0.0
                self.closing_started = 0.0
                self.ended = False
                self.notification_count = 0
                self.cancellations = 0
                self.expected_cancel = False
                self.timer = QTimer(self)
                self.timer.setInterval(250)
                self.timer.timeout.connect(self.tick)
                self.heartbeat = QTimer(self)
                self.heartbeat.setInterval(50)
                self.heartbeat.timeout.connect(self.beat)
                host.bridge.reply.connect(self.reply)
                host.bridge.state_changed.connect(self.state)
                host.notifications.notification_requested.connect(self.notified)

            @Slot(str)
            def notified(self, message: str) -> None:
                self.notification_count += 1

            @Slot(str)
            def state(self, state: str) -> None:
                if state == "Ready" and not self.started:
                    report["startup_seconds"] = monotonic() - self.created
                    QTimer.singleShot(int(args.warmup_seconds * 1000), self.begin)
                elif state == "StartupFailed":
                    self.failures.append("startup failed")
                    self.stop()
                elif state == "Closed":
                    report["closed"] = True
                    report["close_seconds"] = monotonic() - self.closing_started
                    app.quit()

            @Slot()
            def begin(self) -> None:
                self.started = monotonic()
                self.cpu_started = process_time()
                self.last_heartbeat = self.started
                report["steady_start"] = resources()
                report["measurement_started_at"] = datetime.now(timezone.utc).isoformat()
                self.heartbeat.start()
                self.timer.start()

            @Slot()
            def beat(self) -> None:
                now = monotonic()
                self.heartbeats.append(now - self.last_heartbeat)
                self.last_heartbeat = now

            @Slot()
            def tick(self) -> None:
                now = monotonic()
                elapsed = now - self.started
                self.maximum_pending = max(self.maximum_pending, host.bridge.pending_count)
                # Finish the last accepted task so shutdown does not conceal a
                # missing outcome or deliberately cancel an unmeasured task.
                duration_reached = elapsed >= args.seconds
                if duration_reached and (not self.current_task and not self.submit_started):
                    self.stop()
                    return
                if elapsed >= args.seconds + 10:
                    self.failures.append("final accepted task did not drain within 10 seconds")
                    self.stop()
                    return
                if now >= self.next_sample:
                    sample = resources()
                    sample.update(elapsed_seconds=elapsed, pending=host.bridge.pending_count)
                    self.samples.append(sample)
                    report.update(elapsed_seconds=elapsed, iterations=self.iterations, samples=self.samples,
                                  failures=self.failures)
                    save(report, args.output)
                    print(json.dumps({"elapsed_seconds": round(elapsed, 1), "iterations": self.iterations,
                                      "pending": host.bridge.pending_count, "failures": len(self.failures)}), flush=True)
                    self.next_sample = now + 30
                if args.mode == "idle":
                    return
                if self.current_task:
                    if not self.task_read:
                        self.task_read = host.bridge.send("task", {"task_id": self.current_task})
                    return
                if not duration_reached and now >= self.next_cycle and not self.submit_started:
                    kind = ("echo", "confirm", "slow", "cancel")[self.iterations % 4]
                    self.expected_cancel = kind == "cancel"
                    self.submit_started = now
                    host.window.chat_input.setPlainText("synthetic " + kind)
                    host.window.submit_request()
                    self.next_cycle = now + 2

            @Slot(object)
            def reply(self, envelope: Any) -> None:
                if self.ended or not isinstance(envelope, dict):
                    return
                operation = envelope.get("operation")
                data = envelope.get("data", {})
                if not envelope.get("ok"):
                    self.failures.append(str(envelope.get("error", {}).get("code", "error")))
                    return
                if operation == "submit" and self.submit_started:
                    self.latency.append(monotonic() - self.submit_started)
                    self.submit_started = 0.0
                    self.current_task = str(data["task_id"])
                    if self.current_task in self.submitted:
                        self.failures.append("duplicate submission task id")
                    self.submitted.add(self.current_task)
                    host.window.navigate("task", self.current_task)
                    if self.expected_cancel:
                        self.cancel_started = monotonic()
                        host.bridge.send("cancel", {"task_id": self.current_task})
                if operation == "confirm" and self.confirm_started:
                    self.confirm_latency.append(monotonic() - self.confirm_started)
                    self.confirm_started = 0.0
                if operation == "cancel" and self.cancel_started:
                    self.cancel_latency.append(monotonic() - self.cancel_started)
                    self.cancel_started = 0.0
                    if not data.get("cancel_requested"):
                        self.failures.append("cancel receipt lacked durable request")
                if envelope.get("request_id") != self.task_read:
                    return
                self.task_read = ""
                status = data["task"]["status"]
                if status == "Waiting" and self.current_task not in self.confirmed:
                    # Review the real DTO in the window, then choose exactly its tool grant.
                    host.window._show_task(data)
                    host.window._update_enabled()
                    if host.window.confirm_button.isEnabled():
                        for checkbox in host.window._ordinary.values():
                            checkbox.setChecked(True)
                        self.confirm_started = monotonic()
                        host.window.confirm_plan()
                        self.confirmed.add(self.current_task)
                elif status in {"Completed", "Failed", "Cancelled", "Uncertain", "NeedsReview"}:
                    attempts = data.get("attempts", [])
                    expected_attempts = 0 if self.expected_cancel else 1
                    if len(attempts) != expected_attempts:
                        self.failures.append("unexpected attempt count " + str(len(attempts)))
                    self.attempts_checked += len(attempts)
                    if self.current_task in self.finished:
                        self.failures.append("duplicate terminal task")
                    self.finished.add(self.current_task)
                    if self.current_task in self.confirmed:
                        if (root / "workspace" / "synthetic.txt").read_text(encoding="utf-8") != "synthetic":
                            self.failures.append("confirmed write content mismatch")
                    if status == "Cancelled" and self.expected_cancel:
                        self.cancellations += 1
                    elif status != "Completed" or self.expected_cancel:
                        self.failures.append("unexpected terminal " + status)
                    self.iterations += 1
                    self.current_task = ""
                    self.confirmed.clear()
                    # Exercise view replacement and hide/restore without relying on a tray.
                    host.window.hide()
                    host.window.show()

            @Slot()
            def stop(self) -> None:
                if self.ended:
                    return
                self.ended = True
                self.timer.stop()
                self.heartbeat.stop()
                elapsed = monotonic() - self.started if self.started else 0.0
                ending = resources()
                report.update(elapsed_seconds=elapsed, iterations=self.iterations, failures=self.failures,
                    heartbeat=latencies(self.heartbeats), submit_receipt=latencies(self.latency),
                    confirmation_receipt=latencies(self.confirm_latency), cancel_receipt=latencies(self.cancel_latency),
                    submitted_tasks=len(self.submitted), terminal_tasks=len(self.finished),
                    unfinished_tasks=len(self.submitted - self.finished), checked_step_attempts=self.attempts_checked,
                    maximum_pending=self.maximum_pending,
                    average_one_core_cpu_percent=100 * (process_time() - self.cpu_started) / elapsed if elapsed else None,
                    steady_end=ending, notification_count=self.notification_count,
                    expected_cancellations=self.cancellations,
                    acceptance_duration_met=elapsed >= (300 if args.mode == "idle" else 1800))
                self.closing_started = monotonic()
                host.bridge.shutdown()

        runner = Runner()
        host.start()
        app.exec()
        report.update(status="completed", source_fingerprint_end=fingerprint())
        report["same_source_build"] = report["source_fingerprint_start"] == report["source_fingerprint_end"]
        report["passed"] = bool(report.get("closed") and report["same_source_build"] and not runner.failures
                                and runner.submitted == runner.finished)
        save(report, args.output)
        return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
