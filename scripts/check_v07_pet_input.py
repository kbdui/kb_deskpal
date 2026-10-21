"""Visible, synthetic Windows input bench for a human/Computer Use operator.

Creates only its own Qt widgets and a temporary offline DesktopHost. It never
injects input, captures a desktop, reads default config, or hooks global input.
The receiver behind the real PetWindow records native clicks which reach it.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
from time import monotonic
from typing import Any
from uuid import uuid4

from check_v07_pet import fingerprints, process_resources, qt_display_snapshot

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--slow-loader-seconds", type=float, default=0)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This visible native input bench requires Windows")
    if not math.isfinite(args.seconds) or not 5 <= args.seconds <= 600:
        parser.error("--seconds must be between 5 and 600; timeout requests normal shutdown")
    if not math.isfinite(args.slow_loader_seconds) or not 0 <= args.slow_loader_seconds <= 20:
        parser.error("--slow-loader-seconds must be between 0 and 20")
    evidence_root = ROOT / "data" / "validation" / "v07"
    output = (args.output_dir or evidence_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
              + "-pet-native-input-" + uuid4().hex[:8])).resolve()
    if not output.is_relative_to(evidence_root.resolve()):
        parser.error("output-dir must be new and beneath data/validation/v07")
    output.mkdir(parents=True, exist_ok=False)
    if not args.installed:
        sys.path.insert(0, str(ROOT / "src"))
    os.environ["QT_QPA_PLATFORM"] = "windows"
    from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSettings, QTimer, Qt
    from PySide6.QtGui import QCloseEvent, QMouseEvent, QPaintEvent, QPainter
    from PySide6.QtWidgets import QApplication, QCheckBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
    import deskpal.gui.pet_controller as controller_module
    from deskpal.application.factory import create_application
    from deskpal.config import AppConfig, SandboxConfig, StorageConfig
    from deskpal.gui.host import DesktopHost
    from deskpal.gui.pet_assets import EXPRESSIONS, load_package_images
    from deskpal.gui.preferences import GuiPreferences

    app = QApplication([sys.argv[0]])
    app.setQuitOnLastWindowClosed(False)
    started = monotonic()
    log_stream = (output / "events.jsonl").open("x", encoding="utf-8")
    event_count = 0
    report: dict[str, Any] = {"status": "running", "argv": sys.argv, "executable": sys.executable,
        "pid": os.getpid(), "parent_pid": os.getppid(),
        "python": sys.version, "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Only synthetic Qt windows, offline temporary AppConfig/QSettings; no input injection/capture/hooks",
        "native_input_observed": False, "receiver_clicks": 0, "navigation_requests": 0,
        "failures": [], "samples": [], "art_preview": None, "event_limit": 20_000,
        "backend_states": [], "bench_windows": {
            name: {"close_accepted": False, "destroyed": False} for name in ("panel", "receiver")}}
    package_root = Path(controller_module.__file__).resolve().parents[1]
    report["fingerprints_start"] = fingerprints(package_root)

    def log(kind: str, **data: Any) -> None:
        nonlocal event_count
        if log_stream.closed:
            return
        if event_count >= report["event_limit"]:
            if "event_log_limit_reached" not in report["failures"]:
                report["failures"].append("event_log_limit_reached")
            return
        event_count += 1
        log_stream.write(json.dumps({"sequence": event_count, "elapsed_seconds": monotonic() - started,
                                     "kind": kind, **data}, ensure_ascii=False) + "\n")
        log_stream.flush()

    def save() -> None:
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    save()
    log("bench_started", pid=report["pid"], parent_pid=report["parent_pid"], argv=report["argv"])

    temp = tempfile.TemporaryDirectory(prefix="deskpal-v07-input-")
    temp_root = Path(temp.name)
    prefs = GuiPreferences(QSettings(str(temp_root / "preferences.ini"), QSettings.Format.IniFormat))
    config = AppConfig(storage=StorageConfig(temp_root / "data"),
                       sandbox=SandboxConfig(workspace_dir=temp_root / "workspace"))
    host = DesktopHost(lambda: create_application(config), preferences=prefs,
        notification_settings=QSettings(str(temp_root / "notifications.ini"), QSettings.Format.IniFormat),
        quit_on_closed=False)
    if args.slow_loader_seconds:
        def slow_load(generation: int, cancel: threading.Event) -> Any:
            threading.Event().wait(args.slow_loader_seconds)
            return load_package_images(generation, cancel)
        host.pet.loader._load = slow_load
    host.window.setWindowTitle("V0.7 合成主窗口（仅测试数据）")
    closing_at: float | None = None
    final_received = False
    quit_requested = False
    attached_window: Any = None
    preview_expression: str | None = None
    neutral_mask: Any = None

    def snapshot(reason: str) -> None:
        window = host.pet.window
        log("snapshot", reason=reason, display=qt_display_snapshot(app,
            panel=None if report["bench_windows"]["panel"]["destroyed"] else panel,
            receiver=None if report["bench_windows"]["receiver"]["destroyed"] else receiver,
            pet=host.pet.window, main=host.window), loader_busy=host.pet.loader.busy,
            retired_windows=len(host.pet._retired_windows), cache_bytes=host.pet.cache_bytes,
            diagnostics=host.pet.diagnostics, lifecycle=host.pet.lifecycle,
            status=host.pet.status, expression=host.pet.expression,
            mask_equals_loaded_neutral=(window.mask() == neutral_mask
                if window is not None and neutral_mask is not None else None))

    def shutdown(reason: str) -> None:
        nonlocal closing_at
        if closing_at is not None:
            return
        closing_at = monotonic()
        log("shutdown_requested", reason=reason)
        for toggle in toggles.values():
            toggle.setEnabled(False)
        for button in control_buttons:
            button.setEnabled(False)
        host.bridge.shutdown()
        snapshot("shutdown_requested")

    def finish_bench_if_released() -> None:
        nonlocal quit_requested
        if (not quit_requested and final_received and host._finalized
                and host.bridge.state == "Closed" and host.pet.settled
                and all(state["close_accepted"] and state["destroyed"]
                        for state in report["bench_windows"].values())):
            quit_requested = True
            log("bench_owners_released", windows=report["bench_windows"])
            monitor_timer.stop()
            QTimer.singleShot(0, app.quit)

    def bench_destroyed(name: str) -> None:
        report["bench_windows"][name]["destroyed"] = True
        report["bench_windows"][name]["destroyed_seconds"] = monotonic() - started
        log("bench_window_destroyed", window=name)
        # No widget/Qt calls during QObject destruction. The monitor observes
        # these plain flags and requests exit on its next event-loop turn.

    def bench_close(name: str, event: QCloseEvent) -> None:
        if final_received or host._finalized:
            event.accept()
            report["bench_windows"][name]["close_accepted"] = True
            report["bench_windows"][name]["close_accepted_seconds"] = monotonic() - started
            log("bench_close_accepted", window=name, host_finalized=host._finalized,
                final_received=final_received)
        else:
            event.ignore()
            log("bench_close_deferred", window=name)
            shutdown(name + "_close")

    class Receiver(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.setObjectName("v07_native_receiver")
            self.setWindowTitle("V0.7 原生点击接收底窗")
            self.resize(400, 400)

        def paintEvent(self, event: QPaintEvent) -> None:
            painter = QPainter(self)
            try:
                painter.fillRect(self.rect(), Qt.GlobalColor.lightGray)
                painter.setPen(Qt.GlobalColor.darkGray)
                for value in range(0, 400, 20):
                    painter.drawLine(value, 0, value, self.height())
                    painter.drawLine(0, value, self.width(), value)
                painter.setPen(Qt.GlobalColor.black)
                painter.drawText(QRect(8, 8, self.width() - 16, 40), Qt.AlignmentFlag.AlignCenter,
                                 "只点击本接收底窗与其上的桌宠；不操作其他应用")
                painter.drawText(QRect(8, self.height() - 50, self.width() - 16, 40),
                                 Qt.AlignmentFlag.AlignCenter, "底窗点击计数：" + str(report["receiver_clicks"]))
            finally:
                painter.end()

        def mousePressEvent(self, event: QMouseEvent) -> None:
            report["receiver_clicks"] += 1
            report["native_input_observed"] |= event.spontaneous()
            global_point = event.globalPosition().toPoint()
            pet_window = host.pet.window
            pet_point = pet_window.mapFromGlobal(global_point) if pet_window is not None else None
            log("receiver_mouse_press", spontaneous=event.spontaneous(), button=event.button().name,
                position=[event.position().x(), event.position().y()],
                global_position=[global_point.x(), global_point.y()],
                pet_local=[pet_point.x(), pet_point.y()] if pet_point is not None else None,
                pet_mask_contains=(pet_window.mask().contains(pet_point)
                    if pet_window is not None and pet_point is not None else None),
                pet_click_through=prefs.pet_click_through)
            snapshot("receiver_mouse_press")
            self.update()
            event.accept()

        def closeEvent(self, event: QCloseEvent) -> None:
            bench_close("receiver", event)

    class Panel(QWidget):
        def closeEvent(self, event: QCloseEvent) -> None:
            bench_close("panel", event)

    panel = Panel(None, Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
    panel.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    panel.destroyed.connect(lambda: bench_destroyed("panel"))
    panel.setObjectName("v07_native_controls")
    panel.setWindowTitle("V0.7 合成桌宠原生输入控制台")
    layout = QVBoxLayout(panel)
    instructions = QLabel("原生鼠标验证：先点桌宠透明角落，再点不透明身体；拖动不应增加导航次数。\n"
                         "开启穿透后点身体，底窗计数应增加；取消穿透可找回交互。\n"
                         "表情按钮仅预览固定PNG并暂停快照；恢复状态按钮回到真实合成后端。")
    instructions.setWordWrap(True)
    layout.addWidget(instructions)
    state_label = QLabel()
    state_label.setWordWrap(True)
    layout.addWidget(state_label)
    toggles: dict[str, QCheckBox] = {}
    for key, title in (("pet_enabled", "启用桌宠"), ("pet_visible", "显示桌宠"),
                       ("pet_on_top", "桌宠置顶"), ("pet_click_through", "桌宠全窗穿透（取消即恢复）")):
        toggle = QCheckBox(title)
        toggle.setChecked(bool(getattr(prefs, key)))
        toggles[key] = toggle
        layout.addWidget(toggle)

        def changed(value: bool, name: str = key) -> None:
            snapshot("before:" + name)
            getattr(prefs, "set_" + name)(value)
            log("control", name=name, value=value)
            attach_pet()
            apply_preview()
            QTimer.singleShot(0, lambda: snapshot("after:" + name))
        toggle.toggled.connect(changed)

    def sync_toggle(key: str, value: object) -> None:
        toggle = toggles.get(key)
        if toggle is not None:
            toggle.blockSignals(True)
            toggle.setChecked(bool(value))
            toggle.blockSignals(False)
    prefs.changed.connect(sync_toggle)
    control_buttons: list[QPushButton] = []

    def add_button(title: str, action: Any, row: Any = layout) -> None:
        button = QPushButton(title)
        button.clicked.connect(action)
        row.addWidget(button)
        control_buttons.append(button)

    receiver = Receiver()
    receiver.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    receiver.destroyed.connect(lambda: bench_destroyed("receiver"))
    screen = app.primaryScreen()
    area = screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 720)
    panel.resize(min(440, area.width()), min(650, area.height()))
    panel.move(area.topLeft() + QPoint(20, 20))
    receiver.move(area.topLeft() + QPoint(min(500, max(0, area.width() - 420)), 60))

    def position_pet() -> None:
        snapshot("before:position_pet")
        prefs.set_pet_visible(True)
        prefs.set_pet_enabled(True)
        attach_pet()
        if host.pet.window is not None:
            host.pet.window.move(receiver.mapToGlobal(QPoint(80, 55)))
            host.pet.window.fit_to_screens()
            point = host.pet.window.pos()
            pet_screen = host.pet.window.screen()
            prefs.set_pet_position((point.x(), point.y()), pet_screen.name() if pet_screen else "")
            host.pet.window.show()
        apply_preview()
        snapshot("after:position_pet")

    def apply_preview() -> None:
        if preview_expression is not None and host.pet.window is not None:
            host.pet.poll_timer.stop()
            host.pet.stale_timer.stop()
            host.pet._cancel_decorations()
            host.pet._set_expression(preview_expression)

    def art_preview(name: str) -> None:
        nonlocal preview_expression
        snapshot("before:preview:" + name)
        preview_expression = name
        report["art_preview"] = name
        apply_preview()
        log("art_preview", expression=name, available=name in host.pet._cache,
            fallback="neutral" if name not in host.pet._cache else None)
        snapshot("after:preview:" + name)

    def resume_state() -> None:
        nonlocal preview_expression
        preview_expression = None
        report["art_preview"] = None
        host.pet._visibility()
        log("resume_authoritative_state")
        snapshot("resume_authoritative_state")

    def hide_main() -> None:
        host.window.hide()
        snapshot("hide_main")

    add_button("将真实桌宠叠放到底窗（恢复位置）", position_pet)
    expression_row = QHBoxLayout()
    layout.addLayout(expression_row)
    for expression in EXPRESSIONS:
        add_button(expression, lambda checked=False, name=expression: art_preview(name), expression_row)
    add_button("恢复权威状态查询", resume_state)
    add_button("隐藏合成主窗口（保留桌宠/后端）", hide_main)
    add_button("记录屏幕、几何、DPR与焦点快照", lambda: snapshot("operator_snapshot"))
    add_button("正常退出（等待所有自有线程/窗口）", lambda: shutdown("operator_exit"))
    layout.addWidget(QLabel("日志目录：" + str(output)))

    class PetEvents(QObject):
        def eventFilter(self, watched: QObject, event: QEvent) -> bool:
            kind = event.type()
            selected = {QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseMove,
                        QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.FocusIn, QEvent.Type.FocusOut,
                        QEvent.Type.WindowActivate, QEvent.Type.WindowDeactivate, QEvent.Type.Show,
                        QEvent.Type.Hide}
            if kind in selected:
                data: dict[str, Any] = {"event_type": kind.name, "spontaneous": event.spontaneous()}
                if kind in {QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseMove}:
                    point = event.globalPosition().toPoint()  # type: ignore[attr-defined]
                    data["global_position"] = [point.x(), point.y()]
                    data["local_position"] = [event.position().x(), event.position().y()]  # type: ignore[attr-defined]
                    report["native_input_observed"] |= event.spontaneous()
                elif kind == QEvent.Type.Move:
                    data["old_position_logical"] = [event.oldPos().x(), event.oldPos().y()]  # type: ignore[attr-defined]
                    data["new_position_logical"] = [event.pos().x(), event.pos().y()]  # type: ignore[attr-defined]
                log("pet_event", **data)
                if kind != QEvent.Type.MouseMove:
                    # A queued snapshot reflects the final widget handler's
                    # geometry/navigation, while the event itself is unchanged.
                    QTimer.singleShot(0, lambda: snapshot("after_pet_event:" + kind.name))
            return False

    event_filter = PetEvents(host)

    def navigation() -> None:
        report["navigation_requests"] += 1
        log("pet_navigation_requested", count=report["navigation_requests"])
        QTimer.singleShot(0, lambda: snapshot("after_navigation"))

    def attach_pet() -> None:
        nonlocal attached_window, neutral_mask
        window = host.pet.window
        if window is None or window is attached_window:
            return
        attached_window = window
        neutral_mask = None
        window.setObjectName("v07_real_pet")
        window.installEventFilter(event_filter)
        window.destroyed.connect(lambda: log("pet_window_destroyed"))
        window.render_failed.connect(lambda code: log("render_failed", code=code))
        log("pet_window_attached")

    host.pet.navigation_requested.connect(navigation)

    def assets_ready(result: Any) -> None:
        nonlocal neutral_mask
        attach_pet()
        if (host.pet.window is not None and result.generation == host.pet._asset_generation
                and "neutral" in host.pet._cache):
            neutral_mask = host.pet.window.mask()
        log("assets_ready", generation=result.generation,
            expressions=list(result.images), diagnostics=result.diagnostics)
        apply_preview()
        snapshot("assets_ready")

    host.pet.loader.ready.connect(assets_ready)
    host.pet.loader.idle.connect(lambda: log("assets_worker_idle", busy=host.pet.loader.busy))

    def backend_state_changed(state: str) -> None:
        report["backend_states"].append({"state": state, "elapsed_seconds": monotonic() - started})
        log("backend_lifecycle", state=state)
        apply_preview()

    host.bridge.state_changed.connect(backend_state_changed)
    host.bridge.reply.connect(lambda envelope: apply_preview()
        if isinstance(envelope, dict) and envelope.get("operation") == "presentation" else None)
    app.focusChanged.connect(lambda old, new: log("focus_changed",
        old=old.objectName() or type(old).__name__ if old is not None else None,
        new=new.objectName() or type(new).__name__ if new is not None else None))
    host.start(visible=False)
    receiver.show()
    panel.show()
    position_pet()
    snapshot("initial")
    last_state: tuple[Any, ...] | None = None
    next_sample = 0.0

    def monitor() -> None:
        nonlocal final_received, last_state, next_sample
        if final_received:
            finish_bench_if_released()
            return
        if host._finalized:
            final_received = True
            log("host_finalized", settled=host.pet.settled, bridge_state=host.bridge.state)
            snapshot("finalized")
            for name, window in (("receiver", receiver), ("panel", panel)):
                owned = report["bench_windows"][name]
                if not owned["destroyed"] and not owned["close_accepted"]:
                    accepted = window.close()
                    log("bench_close_result", window=name, accepted=accepted)
                    if not accepted:
                        report["failures"].append("bench_close_rejected:" + name)
            # WA_DeleteOnClose releases these widgets through the event loop.
            # Keep ownership/monitoring until both destroyed callbacks arrive.
            finish_bench_if_released()
            return
        attach_pet()
        # This timer records a synthetic bench; it never requests pet repainting.
        state = (host.bridge.state, host.pet.lifecycle, host.pet.loader.busy,
                 len(host.pet._retired_windows), host.pet.cache_bytes, host.pet.diagnostics)
        if state != last_state:
            log("bench_state", state=state)
            snapshot("bench_state")
            last_state = state
        elapsed = monotonic() - started
        state_label.setText("底窗点击：{} | 导航：{}\n后端：{} | 桌宠：{} | loader：{} | cache：{}\n预览：{}".format(
            report["receiver_clicks"], report["navigation_requests"], host.bridge.state,
            host.pet.lifecycle, host.pet.loader.busy, host.pet.cache_bytes, preview_expression or "权威状态"))
        if elapsed >= next_sample:
            report["samples"].append({"elapsed_seconds": elapsed, **process_resources(),
                "cache_bytes": host.pet.cache_bytes, "loader_busy": host.pet.loader.busy,
                "retired_windows": len(host.pet._retired_windows)})
            next_sample += 1
            save()
        if closing_at is None and elapsed >= args.seconds:
            shutdown("ten_minute_or_requested_timeout")
        if closing_at is not None and monotonic() - closing_at > 30:
            state_label.setText("Closing：仍等待自有资源。保留所有权与事件循环，不强杀线程。")

    monitor_timer = QTimer(host)
    monitor_timer.setInterval(250)
    monitor_timer.timeout.connect(monitor)
    monitor_timer.start()
    save()
    print(str(output), flush=True)
    qt_code = app.exec()
    states = [item["state"] for item in report["backend_states"]]
    normal_closing = (closing_at is not None and final_received and quit_requested
        and "Closing" in states and "Closed" in states and host._finalized
        and host.bridge.state == "Closed" and host.pet.settled and qt_code == 0
        and all(state["close_accepted"] and state["destroyed"] for state in report["bench_windows"].values()))
    report.update(status="completed" if normal_closing else "interrupted", qt_exit_code=qt_code,
        normalClosing=normal_closing,
        elapsed_seconds=monotonic() - started, shutdown_seconds=monotonic() - closing_at if closing_at else None,
        final_resources=process_resources(), fingerprints_end=fingerprints(package_root),
        display_end=qt_display_snapshot(app,
            panel=None if report["bench_windows"]["panel"]["destroyed"] else panel,
            receiver=None if report["bench_windows"]["receiver"]["destroyed"] else receiver,
            main=host.window, pet=host.pet.window),
        closing_final_state={"bridge": host.bridge.state, "host_finalized": host._finalized,
            "pet_settled": host.pet.settled, "loader_busy": host.pet.loader.busy,
            "cache_bytes": host.pet.cache_bytes, "retired_windows": len(host.pet._retired_windows)},
        event_count=event_count, diagnostics=host.pet.diagnostics)
    if not normal_closing:
        report["failures"].append("normal_closing_incomplete")
    if report["fingerprints_start"] != report["fingerprints_end"]:
        report["failures"].append("implementation_changed_during_bench")
    report["assessment"] = "Raw native events and Qt geometry only; operator/parent reviews acceptance separately"
    log("bench_end", status=report["status"], normalClosing=normal_closing)
    report["event_count"] = event_count
    save()
    log_stream.close()
    if host._finalized and host.pet.settled:
        temp.cleanup()
    return 0 if normal_closing and not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
