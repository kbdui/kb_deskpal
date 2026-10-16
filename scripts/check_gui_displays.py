"""Native Windows mixed-DPI probe. Moves and captures only its own synthetic windows."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
from time import monotonic
from typing import Any

SCRIPT_ENTERED = monotonic()

from PySide6.QtCore import QEventLoop, QPoint, QRect, QSettings, QTimer, qVersion
from PySide6.QtGui import QScreen
from PySide6.QtWidgets import QApplication, QPushButton, QScrollArea

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deskpal.application.factory import create_application
from deskpal.config import load_config
from deskpal.gui.host import DesktopHost
from deskpal.gui.preferences import GuiPreferences
from deskpal.gui.window import MainWindow


def fingerprint() -> str:
    digest = sha256()
    for path in sorted((ROOT / "src").rglob("*.py")) + [ROOT / "pyproject.toml"]:
        digest.update(str(path.relative_to(ROOT)).replace("\\", "/").encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def rectangle(rect: QRect) -> list[int]:
    return [rect.x(), rect.y(), rect.width(), rect.height()]


def wait(milliseconds: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def until(predicate: Callable[[], bool], seconds: float = 5) -> bool:
    deadline = monotonic() + seconds
    while not predicate() and monotonic() < deadline:
        wait(50)
    return predicate()


def inventory(screens: list[QScreen]) -> list[dict[str, Any]]:
    return [{"screen_index": index, "geometry": rectangle(screen.geometry()),
             "available_geometry": rectangle(screen.availableGeometry()),
             "device_pixel_ratio": screen.devicePixelRatio(),
             "logical_dpi": [screen.logicalDotsPerInchX(), screen.logicalDotsPerInchY()],
             "qt_scale_percent": round(screen.devicePixelRatio() * 100, 2)}
            for index, screen in enumerate(screens)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "data/validation/v05/gui-displays-final.json")
    args = parser.parse_args()
    os.environ["QT_QPA_PLATFORM"] = "windows"
    app = QApplication(["deskpal-synthetic-display-probe"])
    app.setQuitOnLastWindowClosed(False)
    screens = app.screens()
    started = monotonic()
    report: dict[str, Any] = {"probe": "native-windows-displays-v05", "python": sys.version,
        "executable": sys.executable, "qt": qVersion(), "platform": platform.platform(),
        "qpa": app.platformName(), "screens": inventory(screens),
        "source_fingerprint_start": fingerprint(), "probe_script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "privacy": "synthetic temporary config/databases/QSettings; own QWidget.grab only; no desktop capture or display serials",
        "scope": "automatic native window movement and layout; not manual tray/DPI matrix, disconnect, lock or suspend",
        "checks": [], "moves": [], "restores": [], "screen_changed_events": []}
    print(json.dumps({"qpa": report["qpa"], "screens": report["screens"]}), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        report["elapsed_seconds"] = monotonic() - started
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def check(name: str, passed: bool) -> None:
        report["checks"].append({"name": name, "passed": passed})
        if not passed:
            print(json.dumps({"failed_check": name}), flush=True)
        save()

    check("native_windows_qpa", app.platformName() == "windows")
    if args.inventory_only:
        report["status"] = "inventory_only"
        save()
        return 0
    pair = next(((a, b) for a in range(len(screens)) for b in range(a + 1, len(screens))
                 if abs(screens[a].devicePixelRatio() - screens[b].devicePixelRatio()) > .01), None)
    if app.platformName() != "windows" or pair is None:
        report["status"] = "blocked_no_native_mixed_dpi_pair"
        save()
        return 2
    report["tested_screen_indexes"] = list(pair)
    captures = args.output.parent / (args.output.stem + "-captures")
    captures.mkdir(exist_ok=True)

    def center(window: MainWindow, target: QScreen) -> None:
        frame = window.frameGeometry()
        window.move(target.availableGeometry().center() - QPoint(frame.width() // 2, frame.height() // 2))

    def inspect(window: MainWindow, target_index: int, label: str, capture: bool) -> dict[str, Any]:
        target = screens[target_index]
        available = target.availableGeometry()
        page = window._active_page()
        buttons: list[QPushButton] = [window.diagnostics_button, window.retry_button, window.exit_button]
        buttons += {0: [window.submit_button],
                    1: [window.confirm_button, window.resume_button, window.cancel_button],
                    2: [window.memory_accept, window.memory_reject, window.memory_forget],
                    4: [window.proposal_accept, window.proposal_ignore]}.get(window.tabs.currentIndex(), [])
        if page:
            buttons += [window.pages[page].refresh, window.pages[page].previous, window.pages[page].next]
        buttons += [form.submit for form in (window.uncertain_form, window.memory_edit,
                    window.memory_remember, window.goal_form, window.milestone_form, window.binding_form)
                    if form.isVisibleTo(window)]
        direct_rects = {button.text(): rectangle(QRect(button.mapToGlobal(QPoint(0, 0)), button.size()))
                        for button in buttons if button.isVisibleTo(window)}
        control_rects: dict[str, list[int]] = {}
        viewport_accessible = True
        for button in buttons:
            if not button.isVisibleTo(window):
                continue
            ancestor = button.parentWidget()
            while ancestor is not None:
                if isinstance(ancestor, QScrollArea):
                    ancestor.ensureWidgetVisible(button, 8, 8)
                ancestor = ancestor.parentWidget()
            wait(15)
            button_rect = QRect(button.mapToGlobal(QPoint(0, 0)), button.size())
            control_rects[button.text()] = rectangle(button_rect)
            ancestor = button.parentWidget()
            while ancestor is not None:
                if isinstance(ancestor, QScrollArea):
                    viewport = ancestor.viewport()
                    viewport_accessible = viewport_accessible and QRect(
                        viewport.mapToGlobal(QPoint(0, 0)), viewport.size()).contains(button_rect)
                ancestor = ancestor.parentWidget()
        frame_fits = available.contains(window.frameGeometry())
        frame_with_shadow_margin = available.adjusted(-8, -8, 8, 8).contains(window.frameGeometry())
        client = QRect(window.mapToGlobal(QPoint(0, 0)), window.size())
        client_fits = available.contains(client)
        controls_fit = all(available.contains(QRect(*rect)) for rect in control_rects.values())
        actual = next((index for index, screen in enumerate(screens) if window.screen() == screen), -1)
        metrics: dict[str, Any] = {"label": label, "target_screen": target_index,
            "actual_screen": actual, "frame": rectangle(window.frameGeometry()),
            "window_dpr": window.devicePixelRatioF(), "screen_dpr": target.devicePixelRatio(),
            "frame_in_available_geometry": frame_fits, "key_controls_in_available_geometry": controls_fit,
            "client_geometry": rectangle(client), "client_in_available_geometry": client_fits,
            "frame_with_8_logical_pixel_shadow_tolerance": frame_with_shadow_margin,
            "key_controls_before_scroll": direct_rects, "key_controls_after_ensure_visible": control_rects,
            "font_point_size": window.font().pointSizeF(), "font_pixel_size": window.font().pixelSize(),
            "key_controls_inside_all_scroll_viewports": viewport_accessible,
            "window_minimum_size_hint": [window.minimumSizeHint().width(), window.minimumSizeHint().height()]}
        if capture:
            path = captures / (label + ".png")
            metrics["own_window_capture"] = str(path.resolve())
            check(label + ":own_window_capture", window.grab().save(str(path), "PNG"))
        check(label + ":screen_and_dpr", actual == target_index and
              abs(window.devicePixelRatioF() - target.devicePixelRatio()) < .01)
        check(label + ":frame_and_controls", frame_with_shadow_margin and client_fits and controls_fit and viewport_accessible)
        return metrics

    with tempfile.TemporaryDirectory(prefix="deskpal-display-probe-") as temporary:
        root = Path(temporary)
        config = load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
            "DESKPAL_EMBEDDING_PROVIDER": "disabled", "DESKPAL_DATA_DIR": str(root / "data"),
            "DESKPAL_WORKSPACE_DIR": str(root / "workspace")})
        prefs = GuiPreferences(QSettings(str(root / "gui.ini"), QSettings.Format.IniFormat))
        host = DesktopHost(lambda: create_application(config), preferences=prefs,
            notification_settings=QSettings(str(root / "notifications.ini"), QSettings.Format.IniFormat),
            quit_on_closed=False)
        restored: MainWindow | None = None
        try:
            host.window.setWindowTitle("Deskpal · Synthetic display probe")
            host.start()
            if not until(lambda: host.window.lifecycle == "Ready", 10):
                raise RuntimeError("synthetic host did not become Ready")
            report["startup_to_window_ready_seconds"] = monotonic() - SCRIPT_ENTERED
            report["startup_scope"] = "in-process clock before PySide6 imports; includes Qt/host/window creation; excludes interpreter launch; warm caches, embedding disabled"
            print(json.dumps({"event": "window_ready"}), flush=True)
            handle = host.window.windowHandle()
            assert handle is not None

            def changed(screen: QScreen) -> None:
                index = next((i for i, value in enumerate(screens) if value == screen), -1)
                report["screen_changed_events"].append({"screen_index": index,
                    "elapsed_seconds": monotonic() - started})

            handle.screenChanged.connect(changed)
            visits = [pair[0], *[screen for _ in range(3) for screen in (pair[1], pair[0])]]
            for visit, target_index in enumerate(visits):
                center(host.window, screens[target_index])
                check(f"move-{visit}:screen_settled", until(lambda: host.window.screen() == screens[target_index]))
                wait(600)
                samples: list[dict[str, Any]] = []
                for tab in range(host.window.tabs.count()):
                    host.window.tabs.setCurrentIndex(tab)
                    nested = host.window.management if tab == 3 else host.window.memory_forms if tab == 2 else None
                    for inner in range(nested.count() if nested is not None else 1):
                        if nested is not None:
                            nested.setCurrentIndex(inner)
                        wait(450)
                        label = f"visit-{visit}-screen-{target_index}-tab-{tab}-{inner}"
                        samples.append(inspect(host.window, target_index, label, visit < 2))
                report["moves"].append({"visit": visit, "target_screen": target_index, "tabs": samples})
                save()
            check("three_native_round_trips_screen_changed", len(report["screen_changed_events"]) >= 6)
            for screen_index in pair:
                sizes = [move["tabs"][0]["client_geometry"][2:] for move in report["moves"]
                         if move["target_screen"] == screen_index]
                check(f"screen-{screen_index}:no_round_trip_size_growth", all(
                    max(size[axis] for size in sizes) - min(size[axis] for size in sizes) <= 2 for axis in (0, 1)))
            host.window.resize(920, 620)
            host.window.hide()
            host.window.show()
            wait(200)
            check("explicit_resize_survives_show_and_fit", host.window.width() == 920 and host.window.height() == 620)
            for target_index in pair:
                host.window.tabs.setCurrentIndex(0)
                center(host.window, screens[target_index])
                wait(600)
                host.window._save_geometry()
                saved = host.window.frameGeometry()
                host.window.hide()
                restored = MainWindow(host.bridge, prefs)
                restored.setWindowTitle("Deskpal · Synthetic geometry restore")
                restored.on_state_changed("Ready")
                restored.show()
                wait(700)
                sample = inspect(restored, target_index, f"restore-screen-{target_index}", True)
                sample["saved_frame"] = rectangle(saved)
                check(f"restore-screen-{target_index}:position", abs(restored.frameGeometry().x() - saved.x()) <= 2
                      and abs(restored.frameGeometry().y() - saved.y()) <= 2)
                restored.showMinimized()
                wait(500)
                check(f"restore-screen-{target_index}:minimized", restored.isMinimized())
                restored.showNormal()
                wait(600)
                check(f"restore-screen-{target_index}:normal", restored.isVisible() and not restored.isMinimized())
                sample["after_minimize_restore"] = inspect(restored, target_index,
                                                          f"normal-screen-{target_index}", False)
                normal_size = restored.size()
                restored.showMaximized()
                wait(300)
                check(f"restore-screen-{target_index}:maximized", restored.isMaximized())
                restored.showNormal()
                wait(300)
                check(f"restore-screen-{target_index}:unmaximized_size", restored.size() == normal_size)
                report["restores"].append(sample)
                restored._allow_close = True
                restored.close()
                restored = None
                host.window.show()
        except Exception as error:
            report["error_type"] = type(error).__name__
            report["status"] = "failed"
        finally:
            if restored is not None:
                restored._allow_close = True
                restored.close()
            host.bridge.shutdown()
            check("host_closed_and_threads_stopped", until(lambda: host.bridge.state == "Closed", 10))
            report["source_fingerprint_end"] = fingerprint()
            check("same_source_build", report["source_fingerprint_start"] == report["source_fingerprint_end"])
            report["automatic_checks_passed"] = "error_type" not in report and all(
                item["passed"] for item in report["checks"])
            report.setdefault("status", "completed")
            report["manual_acceptance_complete"] = False
            save()
    print(json.dumps({"output": str(args.output.resolve()), "status": report["status"],
                      "automatic_checks_passed": report["automatic_checks_passed"],
                      "elapsed_seconds": report["elapsed_seconds"]}), flush=True)
    return 0 if report["automatic_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
