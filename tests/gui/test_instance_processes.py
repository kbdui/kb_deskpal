"""Real GUI/CLI process ownership with synthetic config and offscreen Qt.

This covers instance ownership only; it does not validate physical tray or DPI.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from threading import Thread
from time import monotonic

import pytest


CHILD = r'''
import json
from pathlib import Path
import sys

from deskpal.application.factory import create_application
from deskpal.config import load_config

mode, data, runtime, stop = sys.argv[1:]
root = Path(runtime)
config = load_config(environ={
    "DESKPAL_LLM_PROVIDER": "offline",
    "DESKPAL_EMBEDDING_PROVIDER": "disabled",
    "DESKPAL_DATA_DIR": data,
    "DESKPAL_WORKSPACE_DIR": str(root / "workspace"),
})

def event(name, **details):
    print("INSTANCE:" + json.dumps({"event": name, **details}), flush=True)

if mode.startswith("cli"):
    from deskpal.application import cli
    # Inject only a hermetic configuration: real CLI startup, lock and close run.
    cli.load_config = lambda *args, **kwargs: config
    def factory(*args, **kwargs):
        application = create_application(*args, **kwargs)
        event("Ready")
        return application
    cli.create_application = factory
    code = cli.main([] if mode == "cli-owner" else ["--list-tools", "--json"])
    event("CLIExit", code=code)
    raise SystemExit(code)

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication
from deskpal.gui.host import DesktopHost
from deskpal.gui.preferences import GuiPreferences

app = QApplication([])
app.setQuitOnLastWindowClosed(False)
preferences = GuiPreferences(QSettings(str(root / "prefs.ini"), QSettings.IniFormat))
notifications = QSettings(str(root / "notifications.ini"), QSettings.IniFormat)
host = DesktopHost(lambda: create_application(config), preferences=preferences,
                   notification_settings=notifications)
host.bridge.state_changed.connect(lambda state: event(state))
def reply(envelope):
    if envelope.get("operation") == "startup" and not envelope.get("ok"):
        event("StartupError", code=envelope["error"]["code"])
host.bridge.reply.connect(reply)
timer = QTimer()
timer.setInterval(20)
timer.timeout.connect(lambda: host.bridge.shutdown() if Path(stop).exists()
                      and host.bridge.state not in {"Closing", "Closed"} else None)
timer.start()
QTimer.singleShot(15000, host.bridge.shutdown)
QTimer.singleShot(18000, lambda: app.exit(71))
host.start(visible=False)
raise SystemExit(app.exec())
'''


class Child:
    def __init__(self, mode, data, runtime):
        runtime.mkdir()
        self.stop_path = runtime / "stop"
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("DESKPAL_")}
        environment.update(PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
                           QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-c", CHILD, mode, str(data), str(runtime), str(self.stop_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", env=environment, cwd=runtime,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        self.lines = []
        self.events = Queue()
        self.reader = Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.append(line.rstrip())
            if line.startswith("INSTANCE:"):
                self.events.put(json.loads(line[len("INSTANCE:"):]))

    def wait_event(self, name, timeout=12):
        deadline = monotonic() + timeout
        while True:
            try:
                value = self.events.get(timeout=max(0, deadline - monotonic()))
            except Empty:
                pytest.fail(f"Missing {name}; process={self.process.poll()}: {self.lines}")
            if value["event"] == name:
                return value
            assert value["event"] not in {"Ready", "StartupFailed"}, self.lines

    def finish(self, *, gui):
        if gui:
            self.stop_path.touch()
            self.wait_event("Closed")
        else:
            assert self.process.stdin is not None
            self.process.stdin.write("/quit\n")
            self.process.stdin.flush()
        assert self.process.wait(timeout=10) == 0, self.lines
        self.reader.join(timeout=2)

    def cleanup(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                stream.close()


@contextmanager
def running_child(mode, data, runtime):
    child = Child(mode, data, runtime)
    try:
        yield child
    finally:
        child.cleanup()


def cli_probe(data, runtime, *, expected):
    with running_child("cli-probe", data, runtime) as child:
        assert child.process.wait(timeout=12) == expected, child.lines
        child.reader.join(timeout=2)
        if expected:
            assert any("InstanceLockError" in line for line in child.lines), child.lines
        else:
            assert any('"event": "Ready"' in line for line in child.lines), child.lines


@pytest.fixture(params=["canonical", "case", "junction"])
def data_paths(request, tmp_path):
    target = tmp_path / "MiXeD-data"
    target.mkdir()
    alias = target
    if request.param != "canonical" and sys.platform != "win32":
        pytest.skip("Windows path alias semantics")
    if request.param == "case":
        alias = Path(str(target).upper())
    elif request.param == "junction":
        alias = tmp_path / "data-junction"
        if any(char in str(path) for path in (target, alias) for char in '%!&|<>\r\n'):
            pytest.skip("Temporary path is unsuitable for mklink")
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)],
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert result.returncode == 0, result.stderr
        assert alias.resolve() == target.resolve()
    try:
        yield target, alias
    finally:
        # A nonrecursive rmdir unlinks only this verified temporary junction.
        if request.param == "junction" and os.path.lexists(alias):
            assert alias.parent.resolve() == tmp_path.resolve()
            assert alias.resolve() == target.resolve()
            os.rmdir(alias)


@pytest.mark.parametrize("owner,contender", [("gui", "gui"), ("gui", "cli"), ("cli", "gui")])
def test_gui_cli_process_competition_and_graceful_release(tmp_path, data_paths, owner, contender):
    target, alias = data_paths
    owner_mode = "gui" if owner == "gui" else "cli-owner"
    with running_child(owner_mode, target, tmp_path / "owner") as first:
        first.wait_event("Ready")
        if contender == "gui":
            with running_child("gui", alias, tmp_path / "contender") as second:
                second.wait_event("StartupFailed")
                error = second.wait_event("StartupError")
                assert error["code"] == "busy"
                second.finish(gui=True)
        else:
            cli_probe(alias, tmp_path / "contender", expected=1)
        # Failed startup/close cannot release the live owner's descriptor.
        assert first.process.poll() is None
        cli_probe(alias, tmp_path / "still-owned", expected=1)
        first.finish(gui=owner == "gui")

    if contender == "gui":
        with running_child("gui", alias, tmp_path / "reopened") as reopened:
            reopened.wait_event("Ready")
            reopened.finish(gui=True)
    else:
        cli_probe(alias, tmp_path / "reopened", expected=0)
