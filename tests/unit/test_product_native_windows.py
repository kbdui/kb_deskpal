"""Bounded Windows OS probes using temporary data and no SDK or cloud calls.

The vault probe alone requires PRODUCT_NATIVE_VAULT_TESTS=1. It uses one newly
generated target, never enumerates credentials, and does not expose its values.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from threading import Thread
from uuid import uuid4

import pytest

from deskpal.credentials import WindowsCredentialStore
from deskpal.maintenance.cache import MANIFEST
from deskpal.memory.cache_lock import CacheLease
from deskpal.memory.embedding import MODEL


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native OS evidence")


CHILD = r'''
import json
import os
from pathlib import Path
import sys

from deskpal.application.instance import InstanceLockError
from deskpal.maintenance.errors import MaintenanceError
from deskpal.memory.cache_lock import CacheLease, CacheLeaseError

mode, data, cache = sys.argv[1:]
data, cache = Path(data), Path(cache)

def event(name, **values):
    print("NATIVE:" + json.dumps({"event": name, **values}), flush=True)

def wait():
    command = sys.stdin.readline().strip()
    if command == "exit":
        os._exit(0)  # Deliberately bypass Python release/finally.
    if command != "release":
        raise RuntimeError("Unexpected native-test command")

try:
    if mode in {"reader", "writer"}:
        with CacheLease(cache, exclusive=mode == "writer"):
            event("ready")
            wait()
    elif mode == "application":
        from deskpal.application.factory import create_application
        from deskpal.config import load_config
        config = load_config(environ={
            "DESKPAL_LLM_PROVIDER": "offline",
            "DESKPAL_EMBEDDING_PROVIDER": "disabled",
            "DESKPAL_DATA_DIR": str(data),
            "DESKPAL_WORKSPACE_DIR": str(Path.cwd() / "workspace"),
            "DESKPAL_MODEL_CACHE_DIR": str(cache),
        })
        application = create_application(config)
        try:
            event("ready")
            wait()
        finally:
            if not application.close(timeout=5):
                raise RuntimeError("Synthetic application did not close")
    elif mode in {"maintenance", "maintenance-probe"}:
        from deskpal.maintenance.cache import ModelCacheManager
        from deskpal.maintenance.service import MaintenanceService
        # Real configuration parsing, service ownership and cache locking run.
        config_path = Path.cwd() / "synthetic.toml"
        config_path.write_text(
            '[llm]\nprovider="offline"\n[storage]\ndata_dir='
            + json.dumps(str(data)) + '\n[sandbox]\nworkspace_dir='
            + json.dumps(str(Path.cwd() / "workspace"))
            + '\n[memory]\nembedding_provider="disabled"\nmodel_cache_dir='
            + json.dumps(str(cache)) + '\n', encoding="utf-8")
        revision = ModelCacheManager(cache).status()["revision"]
        if mode == "maintenance":
            remove_tree = ModelCacheManager._remove_tree
            def blocked_remove(root, assets, *, remove_root=True):
                # This seam runs only after both real locks were acquired.
                event("ready")
                wait()
                remove_tree(root, assets, remove_root=remove_root)
            ModelCacheManager._remove_tree = staticmethod(blocked_remove)
        result = MaintenanceService(config_path).execute(
            "cache_clear", {"expected_revision": revision, "confirm": True})
        if result["status"] != "missing":
            raise RuntimeError("Synthetic cache was not cleared")
    else:
        raise RuntimeError("Unknown native-test mode")
except (CacheLeaseError, InstanceLockError) as error:
    event("busy", kind=type(error).__name__)
except MaintenanceError as error:
    if error.code != "busy":
        raise
    event("busy", kind="MaintenanceError")
else:
    event("released")
'''


class Child:
    def __init__(self, mode, data, cache, runtime):
        runtime.mkdir()
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("DESKPAL_") and key != "OPENAI_API_KEY"}
        environment.update(PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
                           PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-c", CHILD, mode, str(data), str(cache)],
            cwd=runtime, env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.lines = []
        self.events = Queue()
        self.reader = Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.append(line.rstrip())
            if line.startswith("NATIVE:"):
                self.events.put(json.loads(line[len("NATIVE:"):]))
        self.events.put({"event": "eof"})

    def expect(self, name):
        try:
            value = self.events.get(timeout=15)
        except Empty:
            pytest.fail(f"Native child timed out awaiting {name}: {self.lines}")
        assert value["event"] == name, self.lines
        return value

    def finish(self, command="release"):
        assert self.process.stdin is not None
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()
        if command == "release":
            self.expect("released")
        self.exited()

    def exited(self):
        assert self.process.wait(timeout=15) == 0, self.lines
        self.reader.join(timeout=2)
        assert not self.reader.is_alive()

    def cleanup(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                stream.close()


@contextmanager
def child(mode, data, cache, runtime):
    running = Child(mode, data, cache, runtime)
    try:
        yield running
    finally:
        running.cleanup()


def busy_child(mode, data, cache, runtime, kind):
    with child(mode, data, cache, runtime) as contender:
        assert contender.expect("busy")["kind"] == kind
        contender.exited()


def managed_cache(root):
    root.mkdir()
    content = b"synthetic-cache-only-no-real-model"
    (root / "model.onnx").write_bytes(content)
    (root / MANIFEST).write_text(json.dumps({
        "version": 1, "model": MODEL, "directories": [],
        "assets": {"model.onnx": {"bytes": len(content),
                                  "sha256": hashlib.sha256(content).hexdigest()}},
    }), encoding="utf-8")


def test_windows_cache_readers_share_across_processes_and_last_reference_protects(tmp_path):
    cache, data = tmp_path / "cache", tmp_path / "unused-data"
    first = CacheLease(cache, exclusive=False)
    second = CacheLease(cache, exclusive=False)
    try:
        first.acquire()
        second.acquire()
        with child("reader", data, cache, tmp_path / "reader-one") as reader:
            reader.expect("ready")  # A real second process can share the lock.
            with child("reader", data, cache, tmp_path / "reader-two") as other:
                other.expect("ready")
                busy_child("writer", data, cache, tmp_path / "all-readers", "CacheLeaseError")
                first.release()
                busy_child("writer", data, cache, tmp_path / "one-parent-reference", "CacheLeaseError")
                reader.finish()
                other.finish()
            # Both child readers closed, but the parent's second reference lives.
            busy_child("writer", data, cache, tmp_path / "last-parent-reference", "CacheLeaseError")
        second.release()
        with child("writer", data, cache, tmp_path / "writer-after-release") as writer:
            writer.expect("ready")
            busy_child("reader", data, cache, tmp_path / "blocked-reader", "CacheLeaseError")
            busy_child("writer", data, cache, tmp_path / "blocked-writer", "CacheLeaseError")
            writer.finish()
        # Neither failed contenders nor normal closes poison later ownership.
        with CacheLease(cache, exclusive=True):
            pass
    finally:
        first.release()
        second.release()


@pytest.mark.parametrize("mode", ["reader", "writer"])
def test_windows_cache_process_exit_releases_os_lease(tmp_path, mode):
    cache, data = tmp_path / "cache", tmp_path / "unused-data"
    with child(mode, data, cache, tmp_path / "owner") as owner:
        owner.expect("ready")
        busy_child("writer", data, cache, tmp_path / "contender", "CacheLeaseError")
        owner.finish("exit")
    with CacheLease(cache, exclusive=True):
        pass


def test_windows_application_and_maintenance_ownership_lifetimes(tmp_path):
    data, other_data, cache = tmp_path / "data", tmp_path / "other-data", tmp_path / "cache"
    managed_cache(cache)
    original = (cache / MANIFEST).read_bytes()
    with child("application", data, cache, tmp_path / "application") as application:
        application.expect("ready")
        busy_child("maintenance-probe", data, cache, tmp_path / "blocked-maintenance", "InstanceLockError")
        assert (cache / MANIFEST).read_bytes() == original
        # A failed maintenance attempt cannot release the application's lock.
        busy_child("application", data, cache, tmp_path / "still-owned", "InstanceLockError")
        application.finish()
    with child("maintenance", data, cache, tmp_path / "maintenance") as maintenance:
        maintenance.expect("ready")
        busy_child("application", data, cache, tmp_path / "blocked-application", "InstanceLockError")
        busy_child("maintenance-probe", data, cache, tmp_path / "same-data", "InstanceLockError")
        # A different data directory passes InstanceLock, then the shared cache
        # itself rejects replacement/deletion while maintenance holds it.
        busy_child("maintenance-probe", other_data, cache, tmp_path / "shared-cache", "MaintenanceError")
        assert (cache / MANIFEST).read_bytes() == original
        maintenance.finish()
    assert cache.is_dir() and not any(cache.iterdir())
    with child("application", data, cache, tmp_path / "reopened") as reopened:
        reopened.expect("ready")
        reopened.finish()


def _native_vault_roundtrip():
    # Keep the random reference/value out of pytest's assertion introspection
    # and traceback locals even when an OS call fails.
    __tracebackhide__ = True
    store = WindowsCredentialStore()
    reference = "Deskpal/LLM/" + uuid4().hex
    value = "synthetic-only-假凭据-\U0001f680-" + uuid4().hex
    cleanup_owned = False
    phase = "fresh-target-check"
    failure_phase = None
    cleanup_failed = False
    try:
        if store.read(reference) is not None:
            return "fresh-target-collision", False
        cleanup_owned = True  # A partial write must also be cleaned up.
        phase = "write"
        store.write(reference, value)
        phase = "read"
        if store.read(reference) != value:
            failure_phase = "roundtrip-mismatch"
        else:
            phase = "delete"
            store.delete(reference)
            phase = "confirm-deleted"
            if store.read(reference) is not None:
                failure_phase = "target-still-present"
    except Exception:
        failure_phase = phase
    finally:
        if cleanup_owned:
            try:
                store.delete(reference)
                cleanup_failed = store.read(reference) is not None
            except Exception:
                cleanup_failed = True
    return failure_phase, cleanup_failed


@pytest.mark.skipif(os.environ.get("PRODUCT_NATIVE_VAULT_TESTS") != "1",
                    reason="Explicit opt-in required for one new synthetic WinCred target")
def test_windows_native_vault_random_target_roundtrip_and_cleanup(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("DESKPAL_") or name == "OPENAI_API_KEY":
            monkeypatch.delenv(name, raising=False)
    failure_phase, cleanup_failed = _native_vault_roundtrip()
    # These diagnostics reveal only fixed phase labels, never target or value.
    assert cleanup_failed is False, "Synthetic WinCred target cleanup failed"
    assert failure_phase is None, f"Synthetic WinCred operation failed: {failure_phase}"
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
