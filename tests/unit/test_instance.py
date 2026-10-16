"""Real descriptor ownership, including process death without Python cleanup."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from deskpal.application.instance import InstanceLock, InstanceLockError


CHILD = """
import os
import sys
from deskpal.application.instance import InstanceLock
lock = InstanceLock(sys.argv[1])
lock.acquire()
print('acquired', flush=True)
command = sys.stdin.readline().strip()
if command == 'crash':
    os._exit(23)
if command == 'release':
    lock.release()
# 'exit' deliberately relies on OS descriptor cleanup at normal process exit.
"""


def test_construction_is_lazy_and_missing_parents_are_canonical(tmp_path):
    lock = InstanceLock(tmp_path / "missing" / ".." / "new" / "nested")
    assert lock.data_dir == Path(os.path.normcase(str(tmp_path / "new" / "nested")))
    assert lock.path == lock.data_dir / ".instance.lock"
    assert not lock.data_dir.exists()
    assert not lock.acquired
    with lock as acquired:
        assert acquired is lock
        assert lock.acquired
        assert lock.path.is_file()
    assert not lock.acquired
    # The stable lock inode remains available for all subsequent owners.
    assert lock.path.read_bytes() == b"\0"


def test_same_object_and_same_process_competitors_are_rejected(tmp_path):
    first = InstanceLock(tmp_path)
    second = InstanceLock(tmp_path)
    assert first.acquire() is first
    try:
        with pytest.raises(InstanceLockError):
            first.acquire()
        with pytest.raises(InstanceLockError):
            second.acquire()
        second.release()  # A failed contender never releases the owner's lock.
        with pytest.raises(InstanceLockError):
            InstanceLock(tmp_path).acquire()
    finally:
        first.release()
    first.release()
    with second:
        assert second.acquired
    with first:
        assert first.acquired


def test_relative_and_parent_path_aliases_are_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with InstanceLock("data"):
        for alias in (Path(".") / "data", tmp_path / "data" / ".." / "data"):
            with pytest.raises(InstanceLockError):
                InstanceLock(alias).acquire()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows case-insensitive path semantics")
def test_windows_case_alias_is_rejected(tmp_path):
    with InstanceLock(tmp_path / "MiXeD"):
        with pytest.raises(InstanceLockError):
            InstanceLock(str(tmp_path / "MiXeD").upper()).acquire()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory junction semantics")
def test_windows_junction_alias_is_canonical_and_rejected(tmp_path):
    target, alias = tmp_path / "actual", tmp_path / "junction"
    target.mkdir()
    result = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(alias), str(target)],
        capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stderr
    with InstanceLock(target) as owner:
        contender = InstanceLock(alias)
        assert contender.data_dir == owner.data_dir
        with pytest.raises(InstanceLockError):
            contender.acquire()


def test_context_exception_releases_ownership(tmp_path):
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with InstanceLock(tmp_path):
            raise RuntimeError("synthetic failure")
    with InstanceLock(tmp_path):
        pass


def test_open_failure_does_not_poison_registry(tmp_path):
    target = tmp_path / "data"
    target.write_text("not a directory", encoding="utf-8")
    lock = InstanceLock(target)
    with pytest.raises(InstanceLockError):
        lock.acquire()
    assert not lock.acquired
    target.unlink()
    with lock:
        assert lock.acquired


@pytest.mark.parametrize("command,returncode", [("release", 0), ("exit", 0), ("crash", 23)])
def test_real_process_competition_and_exit_release(tmp_path, command, returncode):
    env = dict(os.environ)
    source = Path(__file__).resolve().parents[2] / "src"
    env["PYTHONPATH"] = str(source) + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", CHILD, str(tmp_path / "data")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=env,
    )
    try:
        assert process.stdout is not None
        assert process.stdin is not None
        assert process.stdout.readline().strip() == "acquired"
        with pytest.raises(InstanceLockError):
            InstanceLock(tmp_path / "data" / ".." / "data").acquire()
        _, error = process.communicate(command + "\n", timeout=15)
        assert process.returncode == returncode, error
        with InstanceLock(tmp_path / "data"):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=15)
