"""Bounded Qt/application boundary; the existing dispatcher alone executes Core.

Qt never owns a database connection or waits for a service call. The command,
read and control lanes are bounded and owned until shutdown has actually ended.
"""

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
from queue import Empty, Full, Queue
from threading import Event, RLock, Thread
from typing import Any, Protocol
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot

from deskpal.application.service import ApplicationService


class DesktopPort(Protocol):
    def execute(self, operation: str, parameters: Mapping[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class _Command:
    request_id: str
    operation: str
    parameters: dict[str, Any]
    generation: int
    privacy_epoch: int


READ_OPERATIONS = frozenset({"tasks", "task", "memories", "goals", "goal", "milestones",
                             "bindings", "proposals", "proposal", "settings", "notification_feed",
                             "notification_validate"})
CONTROL_OPERATIONS = frozenset({"cancel", "diagnostics"})
PRIVACY_ACTIONS = frozenset({"forget", "correct", "accept", "reject"})
MAX_PENDING = 32
MAX_PARAMETERS_BYTES = 131072
MAX_REPLY_BYTES = 4194304


def _error(exc: Exception) -> dict[str, str]:
    """Exceptions can contain user text or credentials; expose classified errors only."""
    kind = type(exc).__name__
    if kind == "QueueFull":
        return {"code": "QueueFull", "message": "任务队列已满，请稍后重试。"}
    if kind == "WorkflowBlocked":
        return {"code": "Blocked", "message": "来源、计划或清理状态已变化，请重新审核。"}
    if "Conflict" in kind or "Revision" in kind:
        return {"code": "Conflict", "message": "记录版本已变化，请重新加载。"}
    if isinstance(exc, KeyError):
        return {"code": "NotFound", "message": "记录不存在或已被清理。"}
    if isinstance(exc, RuntimeError):
        return {"code": "Busy", "message": "后端忙碌或正在关闭；操作未受理，请稍后重新审核。"}
    if isinstance(exc, (ValueError, TypeError)):
        return {"code": "Rejected", "message": "参数、版本或操作状态无效；操作未受理。"}
    return {"code": "BackendError", "message": "本地操作失败，请检查诊断并重试。"}


class ApplicationBridge(QObject):
    state_changed = Signal(str)
    reply = Signal(object)
    privacy_changed = Signal(int)
    _delivery_wakeup = Signal()

    def __init__(self, factory: Callable[[], ApplicationService], *,
                 facade_factory: Callable[[ApplicationService], DesktopPort] | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._factory = factory
        if facade_factory is None:
            from deskpal.application.desktop import DesktopFacade
            facade_factory = DesktopFacade
        self._facade_factory = facade_factory
        self._app: ApplicationService | None = None
        self._facade: DesktopPort | None = None
        self._state = "Initializing"
        self._epoch = 0
        self._lock = RLock()
        self._commands: Queue[_Command] = Queue(MAX_PENDING)
        self._reads: Queue[_Command] = Queue(MAX_PENDING)
        self._controls: Queue[_Command] = Queue(MAX_PENDING)
        self._pending: dict[str, _Command] = {}
        self._read_keys: dict[str, str] = {}
        self._closing = Event()
        self._stopped = Event()
        self._initialized = Event()
        self._inflight = 0
        self._threads: list[Thread] = []
        self._outbox: deque[tuple[dict[str, Any], int]] = deque()
        self._outbox_bytes = 0
        self._shutdown_started = False
        self._wake_posted = False
        self._delivery_timer = QTimer(self)
        self._delivery_timer.setInterval(50)
        self._delivery_timer.timeout.connect(self._drain)
        self._delivery_wakeup.connect(self._drain, Qt.ConnectionType.QueuedConnection)

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def privacy_epoch(self) -> int:
        with self._lock:
            return self._epoch

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def _set_state(self, state: str) -> None:
        with self._lock:
            if self._closing.is_set() and state in {"Ready", "CleanupBlocked", "StartupFailed"}:
                return
            if self._state == state:
                return
            self._state = state
        self.state_changed.emit(state)

    def start(self) -> None:
        """Called once from the GUI thread; factory execution remains in the background."""
        with self._lock:
            if self._threads or self._stopped.is_set():
                return
            self._threads = [Thread(target=self._initialize, name="deskpal-gui-commands", daemon=True),
                             Thread(target=self._read_loop, name="deskpal-gui-reads", daemon=True),
                             Thread(target=self._control_loop, name="deskpal-gui-control", daemon=True)]
            for thread in self._threads:
                thread.start()

    def _initialize(self) -> None:
        try:
            app = self._factory()
            # Even a late result after a user closes the startup screen is owned.
            self._app = app
            self._facade = self._facade_factory(app)
            if not self._closing.is_set():
                blocked = bool(app.diagnostics().get("cleanup_pending"))
                self._set_state("CleanupBlocked" if blocked else "Ready")
        except Exception as exc:
            self._set_state("StartupFailed" if not self._closing.is_set() else "Closing")
            self._deliver({"request_id": "startup", "operation": "startup", "generation": 0,
                             "privacy_epoch": self.privacy_epoch, "ok": False,
                             "error": {"code": type(exc).__name__,
                                       "message": "启动失败：检查配置、依赖及同数据目录的运行实例。"}})
        finally:
            self._initialized.set()
        self._loop(self._commands)

    def send(self, operation: str, parameters: dict[str, Any] | None = None, *, generation: int = 0) -> str:
        """Return a receipt ID immediately. Busy/rejected requests also get an async reply."""
        request_id = str(uuid4())
        with self._lock:
            if len(self._outbox) >= MAX_PENDING * 2:
                raise BufferError("GUI delivery capacity reached; consume replies before submitting")
        if operation == "diagnostics" and (not self._initialized.is_set() or self.state == "StartupFailed"):
            self._deliver({"request_id": request_id, "operation": operation, "generation": generation,
                "privacy_epoch": self.privacy_epoch, "ok": True,
                "data": {"lifecycle": self.state, "backend_initialized": self._initialized.is_set(),
                         "pending_commands": self.pending_count}})
            return request_id
        try:
            serialized = json.dumps(parameters or {}, ensure_ascii=False, allow_nan=False)
            if len(serialized.encode("utf-8")) > MAX_PARAMETERS_BYTES:
                raise ValueError("Command too large")
            copied: dict[str, Any] = json.loads(serialized)
        except (TypeError, ValueError, UnicodeError):
            self._reject(request_id, operation, generation, "Rejected", "请求参数无效或过大。")
            return request_id
        with self._lock:
            if ((self._closing.is_set() and operation not in CONTROL_OPERATIONS)
                    or self._state not in {"Ready", "CleanupBlocked", "Closing"}):
                self._reject(request_id, operation, generation, "Busy", "应用尚未就绪或正在关闭。")
                return request_id
            if self._state == "CleanupBlocked" and operation not in {"diagnostics", "retry_cleanup"}:
                self._reject(request_id, operation, generation, "CleanupBlocked", "记忆清理未完成，请重试清理。")
                return request_id
            if len(self._pending) >= MAX_PENDING:
                self._reject(request_id, operation, generation, "QueueFull", "界面请求队列已满。")
                return request_id
            read_key = ""
            if operation in READ_OPERATIONS:
                read_key = operation + ":" + serialized
                existing = self._read_keys.get(read_key)
                if existing is not None:
                    # A new generation is distinct: its caller must receive its own receipt.
                    previous = self._pending.get(existing)
                    if previous is not None and previous.generation == generation and previous.privacy_epoch == self._epoch:
                        return existing
            if operation == "memory" and copied.get("action") in PRIVACY_ACTIONS:
                self._epoch += 1
                self.privacy_changed.emit(self._epoch)
            command = _Command(request_id, operation, copied, generation, self._epoch)
            self._pending[request_id] = command
            if read_key:
                self._read_keys[read_key] = request_id
            lane = self._controls if operation in CONTROL_OPERATIONS else self._reads if operation in READ_OPERATIONS else self._commands
            try:
                lane.put_nowait(command)
            except Full:
                self._finish(command)
                self._reject(request_id, operation, generation, "QueueFull", "界面请求队列已满。")
        return request_id

    def _reject(self, request_id: str, operation: str, generation: int, code: str, message: str) -> None:
        value = {"request_id": request_id, "operation": operation, "generation": generation,
                 "privacy_epoch": self.privacy_epoch, "ok": False, "error": {"code": code, "message": message}}
        self._deliver(value)

    def _deliver(self, value: dict[str, Any]) -> None:
        """Workers never post an unbounded Qt signal per result/rejection."""
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        with self._lock:
            if len(self._outbox) >= MAX_PENDING * 3:
                raise BufferError("GUI delivery queue is full")
            # Reserve bounded metadata room for at most 96 small rejection receipts.
            if self._outbox_bytes + size > MAX_REPLY_BYTES * 2 - 65536:
                value.pop("data", None)
                value.update(ok=False, error={"code": "QueueFull", "message": "界面结果缓冲已满，请重新加载。"})
                size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
            self._outbox.append((value, size))
            self._outbox_bytes += size
            if not self._wake_posted:
                self._wake_posted = True
                self._delivery_wakeup.emit()

    @Slot()
    def _drain(self) -> None:
        with self._lock:
            self._wake_posted = False
        if self._stopped.is_set() and not any(thread.is_alive() for thread in self._threads):
            # End delivery and publish Closed in the owning Qt thread, after all
            # Python workers stopped touching this QObject and its signals.
            self._delivery_timer.stop()
            self._set_state("Closed")
            return
        for _ in range(8):
            with self._lock:
                if not self._outbox:
                    return
                envelope, size = self._outbox.popleft()
                self._outbox_bytes -= size
                command = self._pending.get(envelope["request_id"])
                if command is not None:
                    self._finish(command)
                valid = (envelope["privacy_epoch"] == self._epoch
                         and (not self._closing.is_set() or envelope["operation"] in CONTROL_OPERATIONS
                              or envelope["operation"] == "startup"))
            if valid:
                self.reply.emit(envelope)
        with self._lock:
            if self._outbox and not self._wake_posted:
                self._wake_posted = True
                self._delivery_wakeup.emit()

    def _finish(self, command: _Command) -> None:
        with self._lock:
            self._pending.pop(command.request_id, None)
            for key, value in tuple(self._read_keys.items()):
                if value == command.request_id:
                    del self._read_keys[key]

    def _run(self, command: _Command) -> None:
        with self._lock:
            if ((self._closing.is_set() and command.operation not in CONTROL_OPERATIONS)
                    or command.privacy_epoch != self._epoch):
                self._finish(command)
                return
            self._inflight += 1
        envelope: dict[str, Any] = {"request_id": command.request_id, "operation": command.operation,
                                   "generation": command.generation, "privacy_epoch": command.privacy_epoch}
        try:
            facade = self._facade
            if facade is None:
                raise RuntimeError("Not ready")
            data = facade.execute(command.operation, command.parameters)
            serialized = json.dumps(data, ensure_ascii=False, allow_nan=False)
            if len(serialized.encode("utf-8")) > MAX_REPLY_BYTES:
                raise ValueError("Reply exceeds the view budget")
            envelope.update(ok=True, data=json.loads(serialized))
            if command.operation == "retry_cleanup":
                assert self._app is not None
                self._set_state("CleanupBlocked" if self._app.diagnostics().get("cleanup_pending") else "Ready")
            elif command.operation == "memory" and self._app is not None:
                if self._app.diagnostics().get("cleanup_pending"):
                    self._set_state("CleanupBlocked")
        except Exception as exc:
            envelope.update(ok=False, error=_error(exc))
        finally:
            with self._lock:
                self._inflight -= 1
        with self._lock:
            if ((not self._closing.is_set() or command.operation in CONTROL_OPERATIONS)
                    and command.privacy_epoch == self._epoch):
                self._deliver(envelope)
            else:
                self._finish(command)

    def _loop(self, lane: Queue[_Command]) -> None:
        while not self._stopped.is_set():
            try:
                command = lane.get(timeout=0.1)
            except Empty:
                continue
            try:
                self._run(command)
            finally:
                lane.task_done()

    def _read_loop(self) -> None:
        self._initialized.wait()
        self._loop(self._reads)

    def shutdown(self) -> None:
        """The main window stays alive until Closed, including startup/close timeout."""
        self._closing.set()
        self._set_state("Closing")
        self.privacy_changed.emit(self.privacy_epoch)
        # Only closing needs a short poll to observe all worker threads exiting.
        # During ordinary idle, a single coalesced queued wake handles delivery.
        self._delivery_timer.start()
        if not self._threads:
            self._stopped.set()
            self._delivery_timer.stop()
            self._set_state("Closed")

    def _control_loop(self) -> None:
        while not self._stopped.is_set():
            if self._closing.is_set() and self._initialized.is_set():
                if self._app is not None and not self._shutdown_started:
                    try:
                        self._app.begin_shutdown()
                    except Exception:
                        self._stopped.wait(0.2)
                        continue
                    self._shutdown_started = True
                try:
                    control = self._controls.get_nowait()
                except Empty:
                    pass
                else:
                    try:
                        self._run(control)
                    finally:
                        self._controls.task_done()
                with self._lock:
                    inflight = self._inflight
                if inflight:
                    self._stopped.wait(0.05)
                    continue
                try:
                    done = self._app is None or self._app.close(timeout=0.2)
                except Exception:
                    done = False
                if done:
                    self._stopped.set()
                    with self._lock:
                        self._pending.clear()
                        self._read_keys.clear()
                        self._outbox.clear()
                        self._outbox_bytes = 0
                        for lane in (self._commands, self._reads, self._controls):
                            while True:
                                try:
                                    lane.get_nowait()
                                except Empty:
                                    break
                                else:
                                    lane.task_done()
                    return
                self._stopped.wait(0.2)
                continue
            try:
                command = self._controls.get(timeout=0.1)
            except Empty:
                continue
            try:
                self._run(command)
            finally:
                self._controls.task_done()
