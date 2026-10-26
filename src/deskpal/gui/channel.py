"""Small page-local async receipts; mutations outlive page navigation."""

from collections.abc import Callable
from time import monotonic
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, Qt, Signal


class Channel(QObject):
    notice = Signal(str)

    def __init__(self, bridge: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.pending: dict[str, tuple[str, int, int, Callable[[dict[str, Any]], None], float]] = {}
        self.serial = 0
        bridge.reply.connect(self._reply, Qt.ConnectionType.QueuedConnection)
        if hasattr(bridge, "state_changed"):
            bridge.state_changed.connect(self._lifecycle, Qt.ConnectionType.QueuedConnection)
        if hasattr(bridge, "privacy_changed"):
            bridge.privacy_changed.connect(self.clear, Qt.ConnectionType.QueuedConnection)

    def send(self, operation: str, parameters: dict[str, Any] | None = None,
             callback: Callable[[dict[str, Any]], None] | None = None, *, mutation: bool = False) -> str | None:
        state = str(getattr(self.bridge, "state", ""))
        if (state == "Closed" or (state == "StartupFailed" and operation != "diagnostics")
                or (state == "Closing" and operation not in self._closing_controls())):
            return None
        if len(self.pending) >= 12:
            self.notice.emit("正在等待后台结果，请稍后重试。")
            return None
        params = dict(parameters or {})
        if mutation:
            params.setdefault("command_id", str(uuid4()))
        self.serial += 1
        generation = self.serial
        try:
            identity = str(self.bridge.send(operation, params, generation=generation))
        except (ValueError, RuntimeError, BufferError):
            self.notice.emit("请求尚未受理，请稍后重试。")
            return None
        self.pending[identity] = (operation, generation, int(getattr(self.bridge, "privacy_epoch", 0)),
                                  callback or (lambda value: None), monotonic())
        return identity

    def _reply(self, envelope: Any) -> None:
        if not isinstance(envelope, dict):
            return
        pending = self.pending.pop(str(envelope.get("request_id", "")), None)
        if pending is None:
            return
        operation, generation, epoch, callback, _ = pending
        state = str(getattr(self.bridge, "state", ""))
        if (state == "Closed" or (state == "StartupFailed" and operation != "diagnostics")
                or (state == "Closing" and operation not in self._closing_controls())):
            return
        if (envelope.get("generation") != generation or envelope.get("privacy_epoch") != epoch
                or epoch != int(getattr(self.bridge, "privacy_epoch", 0))):
            return
        if envelope.get("ok") is not True:
            error = envelope.get("error", {})
            self.notice.emit(str(error.get("message", "操作未完成，请重新读取当前版本。")))
            callback({"_error": error})
            return
        value = envelope.get("data", {})
        if isinstance(value, dict):
            callback(value)

    def clear(self, epoch: int = 0) -> None:
        current = int(getattr(self.bridge, "privacy_epoch", epoch))
        self.pending = {key: value for key, value in self.pending.items() if value[2] == current}

    def invalidate(self) -> None:
        """Expire same-epoch callbacks on explicit clearing or lifecycle closure."""
        self.pending.clear()

    def _lifecycle(self, state: str) -> None:
        if state in {"Closed", "StartupFailed"}: self.invalidate()
        elif state == "Closing":
            self.pending = {key: value for key, value in self.pending.items() if value[0] in self._closing_controls()}

    @staticmethod
    def _closing_controls() -> set[str]:
        return {"conversation_clear", "conversation_delete", "cancel", "diagnostics", "request_forget"}
