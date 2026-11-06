"""Persistent conversation state shared by independent window selections."""
from dataclasses import dataclass
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4
from PySide6.QtCore import QEvent, QObject, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QHideEvent, QPainter, QPen, QPixmap, QShowEvent
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget)
from .components import (ChoiceBox as QComboBox, PushButton as QPushButton)
from .channel import Channel
from .widgets import ArrowComboBox, MessageInput, scroll, text_label
from .task_card import TaskCard
from .appearance import AppearanceController
from deskpal.response.templates import legacy_chat_reply

MAX_REVIEWS = 128
MAX_DRAFTS = 128
MAX_DRAFT_BYTES = 512 * 1024

@dataclass
class Draft:
    text: str = ""
    revision: int = 0


class ScrollToBottomButton(QPushButton):
    """Translucent circular overlay with a crisp, theme-independent arrow."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background:transparent; border:none; padding:0;")

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = self.rect().adjusted(2, 2, -2, -2)
        painter.setPen(QPen(QColor(224, 226, 230, 230), 1.5))
        painter.setBrush(QColor(255, 255, 255, 224))
        painter.drawEllipse(bounds)
        center_x = self.width() / 2
        center_y = self.height() / 2
        scale = min(self.width(), self.height())
        painter.setPen(QPen(QColor(25, 27, 30), max(1.6, scale / 24), Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.drawLine(round(center_x), round(center_y - scale * .20), round(center_x), round(center_y + scale * .17))
        painter.drawLine(round(center_x - scale * .15), round(center_y + scale * .04), round(center_x), round(center_y + scale * .19))
        painter.drawLine(round(center_x + scale * .15), round(center_y + scale * .04), round(center_x), round(center_y + scale * .19))

class ChatCoordinator(QObject):
    conversations_changed = Signal()
    timeline_changed = Signal(str)
    task_changed = Signal(str)
    reviews_cleared = Signal()
    tool_results_requested = Signal(str)
    draft_changed = Signal(str, str, int)
    notice = Signal(str)
    ready_changed = Signal()
    expand_requested = Signal(str, str)
    appearance_changed = Signal()

    def __init__(self, bridge: Any, parent: QObject | None = None, *, preferences: Any = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.preferences = preferences
        self.channel = Channel(bridge, self)
        self.channel.notice.connect(self.notice)
        self.appearance = AppearanceController(bridge, self)
        self.conversations: dict[str, dict[str, Any]] = {}
        self.timelines: dict[str, list[dict[str, Any]]] = {}
        self.cursors: dict[str, Any] = {}
        self.more: dict[str, bool] = {}
        self.accepted: dict[str, tuple[int, str]] = {}
        self.drafts: dict[str, Draft] = {}
        self.reviews: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.grants: dict[str, set[str]] = {}
        self.task_pending: set[str] = set()
        self.submissions: dict[str, dict[str, Any]] = {}
        self.task_commands: dict[str, dict[str, Any]] = {}
        self.cleanup_jobs: set[str] = set()
        self._views: dict[int, str] = {}
        self._review_views: dict[int, str] = {}
        self._reads: set[str] = set()
        self._review_queue: OrderedDict[str, None] = OrderedDict()
        self._capacity_noticed_at = -10.0
        self.default_id = ""
        self.model_ready = False
        self.available = False
        self.directory_cursor: Any = None
        self.directory_more = False
        self.archived = False
        self.timer = QTimer(self); self.timer.setInterval(1800); self.timer.timeout.connect(self.poll)
        bridge.state_changed.connect(self.set_state)
        if hasattr(bridge, "privacy_changed"):
            bridge.privacy_changed.connect(self.clear_sensitive)
        self.set_state(str(getattr(bridge, "state", "Initializing")))

    def set_state(self, state: str) -> None:
        self.available = state == "Ready"
        if self.available:
            self.timer.start(); self.refresh_directory(); self.read_settings()
        else:
            self.timer.stop()
            if state in {"Closing", "Closed", "StartupFailed"}: self.clear_sensitive()
        self.ready_changed.emit()

    def _read(self, key: str, op: str, params: dict[str, Any], callback: Any, on_error: Any = None) -> None:
        if not self.available or key in self._reads:
            return
        self._reads.add(key)
        def done(data: dict[str, Any]) -> None:
            self._reads.discard(key)
            if "_error" not in data:
                callback(data)
            elif on_error is not None:
                on_error(data)
        if self.channel.send(op, params, done) is None:
            self._reads.discard(key)
            if on_error is not None: on_error({"_error": {"message": "审核读取暂未受理，请稍后重新读取。"}})

    def read_settings(self) -> None:
        def done(data: dict[str, Any]) -> None:
            self.model_ready = bool(data.get("llm_ready", False)); self.ready_changed.emit()
        self._read("settings", "settings", {}, done)

    def refresh_directory(self, more: bool = False) -> None:
        if not self.default_id:
            self._read("default", "conversation_ensure_default", {}, self._default)
        self._read("directory", "conversation_list", {"cursor": self.directory_cursor if more else None, "limit": 100, "archived": self.archived}, lambda data: self._directory(data, more))

    def _default(self, data: dict[str, Any]) -> None:
        value = data.get("conversation", data)
        self.default_id = str(value.get("id", value.get("conversation_id", "")))
        if self.default_id:
            self.conversations[self.default_id] = value
        self.conversations_changed.emit()

    def _directory(self, data: dict[str, Any], more: bool) -> None:
        if not more:
            self.conversations = {key: value for key, value in self.conversations.items() if key == self.default_id}
        for item in data.get("items", []):
            identity = str(item.get("id", item.get("conversation_id", "")))
            if identity:
                self.conversations[identity] = item
        self.directory_cursor, self.directory_more = data.get("next_cursor"), bool(data.get("has_more"))
        self.conversations_changed.emit()

    def create(self, title: str) -> None:
        def done(data: dict[str, Any]) -> None:
            if "_error" in data:
                return
            value = data.get("conversation", data); identity = str(value.get("id", value.get("conversation_id", "")))
            if identity:
                self.conversations[identity] = value
            self.conversations_changed.emit(); self.expand_requested.emit(identity, "")
        self.channel.send("conversation_create", {"title": title}, done, mutation=True)

    def update_conversation(self, identity: str, **changes: Any) -> None:
        item = self.conversations.get(identity)
        if item:
            self.channel.send("conversation_update", {"id": identity, "expected_revision": item["revision"], **changes}, lambda data: self.refresh_directory(), mutation=True)

    def remove_conversation(self, identity: str, delete: bool = False) -> None:
        item = self.conversations.get(identity)
        if item:
            self.channel.send("conversation_delete" if delete else "conversation_clear", {"id": identity, "expected_revision": item["revision"]}, self._cleanup_receipt, mutation=True)

    def _cleanup_receipt(self, data: dict[str, Any]) -> None:
        if "_error" in data: return
        job_id = str(data.get("job_id", "")); status = data.get("status")
        if job_id:
            if status == "Pending": self.cleanup_jobs.add(job_id)
            else: self.cleanup_jobs.discard(job_id)
        self.notice.emit(("正文已遮蔽，正在等待运行中的工作结束。" if status == "Pending" else "会话清理已完成。") + f" 影响任务 {data.get('affected_task_count', 0)} 项；聊天、依赖任务正文及关联提醒已纳入清理，独立长期记忆保留。")
        self.refresh_directory()

    def watch(self, view: QWidget, identity: str, visible: bool) -> None:
        if visible and identity:
            self._views[id(view)] = identity; self.load(identity)
        else:
            self._views.pop(id(view), None)

    def draft(self, identity: str) -> Draft:
        hot = set(self._views.values()) | {item["conversation_id"] for item in self.submissions.values()} | {identity}
        for key in tuple(self.drafts):
            if not self.drafts[key].text and key not in hot: self.drafts.pop(key)
        if identity not in self.drafts and len(self.drafts) < MAX_DRAFTS: self.drafts[identity] = Draft()
        return self.drafts.get(identity, Draft())

    def edit_draft(self, identity: str, text: str) -> bool:
        if not self.available:
            self.notice.emit("后台尚未就绪，暂不能编辑聊天草稿。")
            return False
        draft = self.draft(identity)
        if len(text) > 20000 or len(text.encode("utf-8")) > 64000:
            self.notice.emit("这份草稿过长（最多 20,000 字符 / 64,000 字节）；新输入未保存，原草稿已保留。")
            self.draft_changed.emit(identity, draft.text, draft.revision); return False
        total = sum(len(item.text.encode("utf-8")) for key, item in self.drafts.items() if key != identity)
        if text and (total + len(text.encode("utf-8")) > MAX_DRAFT_BYTES or identity not in self.drafts):
            self.notice.emit("草稿总容量已满（最多 128 份 / 512 KiB）；新输入未保存，请先发送或清除已有草稿。")
            self.draft_changed.emit(identity, draft.text, draft.revision)
            return False
        if identity not in self.drafts: return True
        if draft.text != text:
            draft.text = text; draft.revision += 1
            self.draft_changed.emit(identity, text, draft.revision)
        return True

    def load(self, identity: str, older: bool = False) -> None:
        if not identity:
            return
        params: dict[str, Any] = {"id": identity, "limit": 50}
        self._read("timeline:" + identity, "conversation_timeline", params, lambda data: self._timeline(identity, data, False))

    def _timeline(self, identity: str, data: dict[str, Any], older: bool) -> None:
        conversation = data.get("conversation", {})
        if conversation:
            self.conversations[identity] = conversation
        incoming = data.get("items", [])
        merged: dict[str, dict[str, Any]] = {}
        for item in incoming:
            merged[str(item.get("id"))] = item
            if item.get("kind") == "task_card" and item.get("visible", True):
                self.review(str(item.get("task_id", "")))
        self.timelines.pop(identity, None)
        self.timelines[identity] = sorted(merged.values(), key=lambda item: item.get("sequence", 0))
        for cached in tuple(self.timelines):
            if len(self.timelines) <= 4: break
            if cached not in set(self._views.values()) and cached != identity:
                self.timelines.pop(cached, None); self.cursors.pop(cached, None); self.more.pop(cached, None)
        self.cursors[identity], self.more[identity] = data.get("next_cursor"), bool(data.get("has_more"))
        self.timeline_changed.emit(identity)

    def latest(self, identity: str) -> None:
        self.load(identity)

    def read_page(self, identity: str, before: int, callback: Any, key: str) -> None:
        self._read(key, "conversation_timeline", {"id": identity, "before": before, "limit": 50}, callback, callback)

    def locate(self, identity: str, task_id: str, callback: Any, key: str) -> None:
        self._read(key, "conversation_task_location", {"id": identity, "task_id": task_id}, callback, callback)

    def load_body(self, identity: str, item_id: str, offset: int = 0) -> None:
        def done(data: dict[str, Any]) -> None:
            value = data.get("item", data)
            body = str(value.get("body", ""))
            for item in self.timelines.get(identity, []):
                if item.get("id") == item_id:
                    item["body"] = body
                    item["body_start"] = offset
                    item["body_truncated"] = bool(data.get("has_more", value.get("has_more", False)))
                    item["body_offset"] = data.get("next_offset", offset + len(body))
            self.timeline_changed.emit(identity)
        self._read("body:" + item_id, "conversation_item", {"id": identity, "item_id": item_id, "offset": offset, "limit": 32768}, done)

    def submit(self, identity: str) -> None:
        if not self.available:
            self.notice.emit("后台尚未就绪，暂不能发送消息。")
            return
        draft = self.draft(identity)
        conversation = self.conversations.get(identity, {})
        if conversation.get("archived_at") or conversation.get("archived"):
            self.notice.emit("这段会话已归档，恢复后可以继续聊天；草稿已保留。"); return
        if len(self.submissions) >= 32:
            self.notice.emit("正在核对多条消息回执，请等当前请求处理后再发送。"); return
        if not self.model_ready or not draft.text.strip():
            self.notice.emit("请先在设置中完成模型连接，再发送消息。" if not self.model_ready else "请写下要说的话。")
            return
        if len(draft.text) > 20000 or len(draft.text.encode("utf-8")) > 64000:
            self.notice.emit("这条消息过长，请拆分后发送（最多 20,000 字符 / 64,000 字节）。"); return
        if any(item["conversation_id"] == identity and item["draft_revision"] == draft.revision for item in self.submissions.values()):
            self.notice.emit("这份草稿已提交，正在核对回执，请勿重复发送。"); return
        if "context_epoch" not in conversation and "epoch" not in conversation:
            self.load(identity); self.notice.emit("正在读取会话版本，请稍后发送。"); return
        command = str(uuid4())
        record: dict[str, Any] = {"command_id": command, "conversation_id": identity, "draft_revision": draft.revision, "request": draft.text, "at": monotonic(), "state": "正在发送"}
        self.submissions[command] = record
        record["parameters"] = {"request": draft.text, "conversation_id": identity, "expected_context_epoch": conversation.get("context_epoch", conversation.get("epoch")), "command_id": command}
        self.channel.send("submit", record["parameters"], lambda data: self._submitted(command, data))
        self.timeline_changed.emit(identity)

    def retry_submission(self, command: str) -> None:
        record = self.submissions.get(command)
        if record and record.get("retryable"):
            conversation = self.conversations.get(record["conversation_id"], {})
            if conversation.get("archived_at") or conversation.get("archived"):
                self.notice.emit("这段会话已归档，恢复后可以重试；原消息与命令已保留。"); return
            record["retryable"] = False; record["at"] = monotonic(); record["state"] = "使用原命令重试受理"
            self.channel.send("submit", record["parameters"], lambda data: self._submitted(command, data))
            self.timeline_changed.emit(record["conversation_id"])
        else:
            self.poll()

    def _submitted(self, command: str, data: dict[str, Any]) -> None:
        item = self.submissions.get(command)
        if item is None:
            return
        identity = item["conversation_id"]
        if "_error" in data:
            item["state"] = "受理结果未确认，正在核查"; item["at"] = 0
            self.timeline_changed.emit(identity); return
        if data.get("accepted") or data.get("task_id"):
            draft = self.draft(identity)
            if draft.revision == item["draft_revision"] and draft.text == item["request"]:
                self.edit_draft(identity, "")
            self.submissions.pop(command, None); self.load(identity)
            self.notice.emit("")
            if data.get("task_id"):
                self.review(str(data["task_id"]))
        self.timeline_changed.emit(identity)

    def review(self, task_id: str) -> None:
        if task_id:
            self._enqueue_review(task_id); self._drain_reviews()

    def _review_pins(self) -> set[str]:
        return (set(self._review_views.values()) | set(self.task_commands) | self.task_pending
                | {identity for identity, grants in self.grants.items() if grants}
                | {str(item["task_id"]) for identity in set(self._views.values())
                   for item in self.timelines.get(identity, []) if item.get("task_id") and item.get("visible", True)})

    def _capacity_notice(self) -> None:
        if monotonic() - self._capacity_noticed_at >= 8:
            self._capacity_noticed_at = monotonic()
            self.notice.emit("审核缓存已满（最多 128 项），请先处理已选授权或关闭其他审核内容，再重新读取；现有选择已保留。")

    def _reserve_review(self, task_id: str) -> bool:
        if task_id in self.reviews:
            self.reviews.move_to_end(task_id); return True
        pins = self._review_pins()
        while len(self.reviews) >= MAX_REVIEWS:
            candidate = next((identity for identity in self.reviews if identity not in pins), None)
            if candidate is None:
                self._capacity_notice(); return False
            self.reviews.pop(candidate); self.grants.pop(candidate, None); self.accepted.pop(candidate, None)
        return True

    def _enqueue_review(self, task_id: str) -> None:
        if task_id in self.reviews: self.reviews.move_to_end(task_id)
        if "task:" + task_id in self._reads or task_id in self._review_queue: return
        if len(self._review_queue) >= MAX_REVIEWS or (task_id not in self.reviews and len(self.reviews) >= MAX_REVIEWS and not (set(self.reviews) - self._review_pins())):
            self._capacity_notice(); return
        self._review_queue[task_id] = None

    def watch_review(self, token: int, task_id: str | None) -> None:
        if task_id: self._review_views[token] = task_id
        else: self._review_views.pop(token, None)

    def _drain_reviews(self) -> None:
        active = sum(key.startswith("task:") for key in self._reads)
        while self._review_queue and active < 4 and len(self.channel.pending) < 10 and self.available:
            task_id, _ = self._review_queue.popitem(last=False)
            if "task:" + task_id in self._reads: continue
            active += 1
            self._read("task:" + task_id, "task_review", {"task_id": task_id}, lambda value, task_id=task_id: self.set_review(task_id, value), lambda data, task_id=task_id: self._review_failed(task_id, data))

    def _review_failed(self, task_id: str, data: dict[str, Any]) -> None:
        if not self._reserve_review(task_id): return
        value = dict(self.reviews.get(task_id, {})); value["review_unavailable"] = str(data.get("_error", {}).get("message", "当前审核不可用，请重新读取。")); value["available_actions"] = []
        self.reviews[task_id] = value; self.task_changed.emit(task_id); self._drain_reviews()

    def set_review(self, task_id: str, value: dict[str, Any]) -> None:
        before = self.reviews.get(task_id, {})
        if int(value.get("task_revision", 0)) < int(before.get("task_revision", 0)):
            return
        if int(value.get("confirmation_revision", 0)) < int(before.get("confirmation_revision", 0)):
            return
        if not self._reserve_review(task_id): return
        accepted = self.accepted.get(task_id)
        if (accepted and value.get("confirmation_state") != "accepted"
                and int(value.get("confirmation_revision", 0)) > int(before.get("confirmation_revision", 0))):
            # A newer dispatcher snapshot can finish or reject an accepted
            # confirmation even without changing the plan itself.
            self.accepted.pop(task_id, None); accepted = None
        if accepted and value.get("task_revision") == accepted[0] and value.get("plan_hash") == accepted[1] and value.get("confirmation_state") != "accepted":
            value = dict(value); value["confirmation_state"] = "accepted"; value["available_actions"] = [action for action in value.get("available_actions", []) if action != "confirm"]
        if value.get("confirmation_state") == "accepted":
            self.accepted[task_id] = (int(value.get("task_revision", 0)), str(value.get("plan_hash", "")))
        if before.get("task_revision") != value.get("task_revision") or before.get("plan_hash") != value.get("plan_hash"):
            self.grants.pop(task_id, None)
        self.reviews[task_id] = value; self.task_changed.emit(task_id); self._drain_reviews()
        if before.get("confirmation_state") == "accepted" and value.get("confirmation_state") != "accepted":
            self.notice.emit(str(value.get("blocked_reason") or {
                "Completed": "操作已完成。", "Failed": "操作未完成，请查看执行记录。",
                "NeedsReview": "操作未能继续，请查看当前任务说明。",
                "Running": "正在执行已确认的操作。"}.get(str(value.get("status")), "任务状态已更新。")))

    def set_grant(self, task_id: str, tool: str, enabled: bool) -> None:
        if task_id not in self.reviews: return
        grant = self.grants.setdefault(task_id, set())
        grant.add(tool) if enabled else grant.discard(tool)
        self.task_changed.emit(task_id)

    def confirm(self, task_id: str) -> None:
        review = self.reviews.get(task_id, {})
        if (not review or task_id in self.task_pending or "confirm" not in review.get("available_actions", [])
                or review.get("confirmation_state") == "accepted"
                or not set(review.get("missing_permissions", {}).get("high_risk_tools", [])).issubset(self.grants.get(task_id, set()))):
            return
        self._task_send(task_id, "confirm", {"task_id": task_id, "plan_hash": review.get("plan_hash"), "expected_revision": review.get("task_revision"), "ordinary_tools": list(review.get("missing_permissions", {}).get("ordinary_tools", [])), "high_risk_tools": sorted(self.grants.get(task_id, set()))})

    def task_action(self, task_id: str, action: str) -> None:
        if task_id not in self.task_pending:
            self._task_send(task_id, action, {"task_id": task_id})

    def _task_send(self, task_id: str, action: str, params: dict[str, Any]) -> None:
        previous = self.task_commands.get(task_id)
        if previous is None and len(self.task_commands) >= 32:
            self.notice.emit("正在核对多项操作回执，请等待当前结果后再继续。"); return
        if previous and (previous["action"] != action or previous["parameters"] != params):
            self.notice.emit("上一项操作回执尚未核实，请先核查结果。"); return
        if previous and not previous.get("retryable"):
            self.reconcile_task(task_id); return
        record = previous or {"command_id": str(uuid4()), "action": action, "parameters": params, "at": monotonic()}
        record["retryable"] = False; record["at"] = monotonic(); self.task_commands[task_id] = record
        self.task_pending.add(task_id); self.task_changed.emit(task_id)
        if self.channel.send(action, {**params, "command_id": record["command_id"]}, lambda data: self._task_receipt(task_id, data)) is None:
            record["at"] = 0

    def _task_receipt(self, task_id: str, data: dict[str, Any]) -> None:
        record = self.task_commands.get(task_id)
        if not record: return
        if "_error" in data:
            record["error"] = data["_error"]
            record["at"] = 0; self.notice.emit("操作返回错误，正在核查是否已受理。"); return
        if data.get("accepted") or data.get("task_id") == task_id:
            self.task_pending.discard(task_id)
            if record["action"] == "confirm":
                review = self.reviews.get(task_id, {})
                self.accepted[task_id] = (int(review.get("task_revision", 0)), str(review.get("plan_hash", "")))
                review["confirmation_state"] = "accepted"
                review["available_actions"] = [item for item in review.get("available_actions", []) if item != "confirm"]
            self.task_commands.pop(task_id, None); self.review(task_id); self.task_changed.emit(task_id)
            self.notice.emit("操作已受理，正在更新状态。")
        else:
            record["at"] = 0
            self.notice.emit("尚未取得明确受理回执，正在核查操作状态。")

    def reconcile_task(self, task_id: str) -> None:
        record = self.task_commands.get(task_id)
        if not record: return
        record["at"] = monotonic()
        def done(data: dict[str, Any]) -> None:
            if self.task_commands.get(task_id) is not record:
                return
            if data.get("accepted"):
                self._task_receipt(task_id, data)
            elif data.get("known") is False:
                if record.get("error"):
                    self.task_commands.pop(task_id, None); self.task_pending.discard(task_id)
                    self.accepted.pop(task_id, None); self.review(task_id); self.task_changed.emit(task_id)
                    self.notice.emit(str(record["error"].get("message") or "操作未受理，请查看任务说明。"))
                    return
                record["retryable"] = True; self.task_pending.discard(task_id); self.task_changed.emit(task_id)
                self.notice.emit("后台尚无此命令回执，可点击原操作使用相同命令重试。")
        self._read("task-receipt:" + task_id, "command_status", {"command_id": record["command_id"]}, done)

    def poll(self) -> None:
        for job_id in tuple(self.cleanup_jobs):
            self._read("cleanup:" + job_id, "conversation_cleanup_status", {"job_id": job_id}, self._cleanup_receipt)
        for task_id, record in tuple(self.task_commands.items()):
            if monotonic() - record["at"] > 8: self.reconcile_task(task_id)
        for identity in set(self._views.values()):
            self.load(identity)
        for command, item in tuple(self.submissions.items()):
            if monotonic() - item["at"] > 8:
                item["at"] = monotonic()
                self._read("receipt:" + command, "command_status", {"command_id": command}, lambda data, command=command: self._reconciled(command, data))
        visible_tasks = set(self._review_views.values()) | {str(item.get("task_id")) for identity in set(self._views.values()) for item in self.timelines.get(identity, []) if item.get("visible") and item.get("task_id")}
        ids = sorted(visible_tasks)[:100]
        if ids:
            known = {task_id: int(self.reviews[task_id]["task_revision"]) for task_id in ids if self.reviews.get(task_id, {}).get("task_revision", 0) > 0}
            self._read("task-updates", "task_updates", {"task_ids": ids, "known_revisions": known}, self._task_updates)
        self._drain_reviews()

    def _task_updates(self, data: dict[str, Any]) -> None:
        for item in data.get("items", []):
            task_id = str(item.get("task_id", ""))
            if task_id: self._enqueue_review(task_id)
        for task_id in data.get("missing_ids", []):
            self._review_failed(str(task_id), {"_error": {"message": "任务已不存在或已清理。"}})
        self._drain_reviews()

    def _reconciled(self, command: str, data: dict[str, Any]) -> None:
        if data.get("accepted") or data.get("task_id"):
            self._submitted(command, data)
        elif data.get("known") is False or data.get("found") is False or data.get("status") in {"NotFound", "not_found", "unknown"}:
            item = self.submissions.get(command)
            if item:
                item["state"] = "后台尚未找到回执，原文与原命令已保留"
                item["retryable"] = True
                self.timeline_changed.emit(item["conversation_id"])

    def clear_sensitive(self, epoch: int = 0) -> None:
        self.channel.invalidate()
        self.timelines.clear(); self.conversations.clear(); self.cursors.clear(); self.more.clear(); self.drafts.clear()
        self.reviews.clear(); self.grants.clear(); self.accepted.clear(); self.submissions.clear(); self.task_commands.clear(); self.task_pending.clear(); self._reads.clear(); self._review_queue.clear(); self.default_id = ""
        self._review_views.clear()
        self.reviews_cleared.emit()
        self.conversations_changed.emit(); self.timeline_changed.emit(""); self.draft_changed.emit("", "", 0)
        if self.available:
            QTimer.singleShot(0, self, self.refresh_directory)

class ChatView(QWidget):
    expand_requested = Signal(str, str)
    conversation_selected = Signal(str)
    directory_requested = Signal()
    execution_requested = Signal()
    _avatar_pixmaps: dict[str, QPixmap] = {}
    def __init__(self, coordinator: ChatCoordinator, compact: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.coordinator, self.compact = coordinator, compact
        self.conversation_id = ""; self.task_id = ""; self._populating = False; self._draft_rejected = False
        self._signature: Any = None
        self._history_items: list[dict[str, Any]] | None = None; self._history_cursor: Any = None; self._history_more = False
        self._page_generation = 0; self._page_reading = False
        self._render_generation = 0
        self._follow_next_render = False
        self._tail_signature = ""
        self._tail_ids: set[str] = set()
        self._rendered_ids: set[str] = set()
        self.positions: dict[str, int] = {}; self._restore_position: int | None = None
        layout = QVBoxLayout(self); layout.setContentsMargins(20 if not compact else 10, 16, 20 if not compact else 10, 12)
        layout.setSpacing(14)
        top = QHBoxLayout(); self.avatar = text_label("◈"); self.avatar.setFixedSize(42, 42); top.addWidget(self.avatar)
        self.avatar.setObjectName('chat_avatar')
        from qfluentwidgets import TransparentPushButton, FluentIcon
        self.title: QLabel | TransparentPushButton
        session_button = TransparentPushButton('日常聊天', self)
        font = session_button.font(); font.setPointSize(17); font.setWeight(QFont.Weight.DemiBold); session_button.setFont(font)
        session_button.setIcon(FluentIcon.CHEVRON_DOWN_MED)
        session_button.setToolTip('选择或管理当前角色的会话')
        session_button.clicked.connect(self.directory_requested.emit)
        self.title = session_button
        self.title.setMinimumWidth(0); self.title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        top.addWidget(self.title); top.addStretch(1)
        if not compact:
            records = QPushButton("执行记录"); records.clicked.connect(self.execution_requested.emit); top.addWidget(records)
        if compact:
            expand = QPushButton("展开"); expand.clicked.connect(lambda: self.expand_requested.emit(self.conversation_id, self.task_id)); top.addWidget(expand)
        layout.addLayout(top)
        self.conversation_row = QWidget(); switcher = QHBoxLayout(self.conversation_row); switcher.setContentsMargins(0, 0, 0, 0)
        self.selector_label = text_label("切换会话"); self.selector_label.setWordWrap(False)
        switcher.addWidget(self.selector_label)
        self.selector = ArrowComboBox(); self.selector.setObjectName("conversation_selector")
        self.selector.setMinimumWidth(120)
        self.selector.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.selector.setPlaceholderText("正在读取会话…"); self.selector.setAccessibleName("切换会话")
        self.selector_label.setBuddy(self.selector); switcher.addWidget(self.selector, 1)
        layout.addWidget(self.conversation_row)
        self.conversation_row.setVisible(not compact)
        self.status = text_label()
        layout.addWidget(self.status)
        self.body = QWidget(); self.messages = QVBoxLayout(self.body); self.messages.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.messages.setSpacing(18)
        self.body.setMaximumWidth(780)
        self.area = scroll(self.body); self.area.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop); layout.addWidget(self.area, 1)
        self.older = QPushButton("加载更早记录"); self.older.clicked.connect(self._load_older); self.older.hide()
        self.latest_button = QPushButton("返回最新消息"); self.latest_button.hide(); self.latest_button.clicked.connect(self._latest); layout.addWidget(self.latest_button)
        self.new_message = ScrollToBottomButton(); self.new_message.setObjectName("chat_scroll_latest"); self.new_message.setAccessibleName("有新消息，回到底部")
        self.new_message.setFixedSize(36 if compact else 52, 36 if compact else 52)
        self.new_message.hide()
        self.new_message.clicked.connect(self._new_messages); self.new_message.setParent(self.area.viewport())
        self.area.viewport().installEventFilter(self)
        self.area.verticalScrollBar().valueChanged.connect(self._scroll_changed)
        self.input = MessageInput(); self.input.setObjectName("chat_input"); layout.addWidget(self.input)
        row = QHBoxLayout(); self.feedback = text_label(); row.addWidget(self.feedback, 1)
        self.send = QPushButton("发送"); self.send.setObjectName("primary"); row.addWidget(self.send); layout.addLayout(row)
        self.input.textChanged.connect(self._edited); self.input.send_requested.connect(self._send); self.send.clicked.connect(self._send)
        self.selector.currentIndexChanged.connect(self._selected)
        coordinator.conversations_changed.connect(self._directory); coordinator.timeline_changed.connect(self._timeline)
        coordinator.task_changed.connect(self._task_reply_changed)
        coordinator.draft_changed.connect(self._draft); coordinator.ready_changed.connect(self._ready)
        coordinator.notice.connect(self.feedback.setText)
        coordinator.expand_requested.connect(self._expand)
        if hasattr(coordinator.bridge, "privacy_changed"): coordinator.bridge.privacy_changed.connect(self._privacy)
        if hasattr(coordinator, "appearance"):
            coordinator.appearance.image_changed.connect(self._avatar)
            self._avatar("character_avatar", coordinator.appearance.images.get("character_avatar"))
        if coordinator.preferences is not None and hasattr(coordinator.preferences, "changed"):
            coordinator.preferences.changed.connect(self._display_names_changed)
        self._directory(); self._ready()

    def set_selector_visible(self, visible: bool) -> None:
        """The main window controls its selector row; compact chat uses the title menu."""
        self.conversation_row.setVisible(visible and not self.compact)

    def _avatar(self, slot: str, pixmap: Any) -> None:
        if slot not in {"user_avatar", "character_avatar"}: return
        if slot == "character_avatar": self._set_avatar(self.avatar, slot, pixmap, 42)
        # Only visit the active page; retired frames await deferred deletion.
        for index in range(self.messages.count()):
            layout_item = self.messages.itemAt(index)
            frame = layout_item.widget() if layout_item is not None else None
            label = frame.findChild(QLabel, "message_" + slot) if frame is not None else None
            if label is not None: self._set_avatar(label, slot, pixmap, 24)

    def _display_names_changed(self, key: str, value: Any) -> None:
        if key in {"user_display_name", "character_display_name"}:
            self._signature = None
            self._timeline(self.conversation_id)

    def _display_name(self, slot: str) -> str:
        preferences = self.coordinator.preferences
        if preferences is None: return "你" if slot == "user_avatar" else "Deskpal"
        return (preferences.user_display_name if slot == "user_avatar"
                else preferences.character_display_name)

    @staticmethod
    def _set_avatar(label: QLabel, slot: str, pixmap: Any, edge: int) -> None:
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon = ChatView._avatar_pixmaps.get(slot)
        if icon is None:
            filename = "user-dog.png" if slot == "user_avatar" else "deskpal-cat.png"
            # Generated files have transparent margins; crop once so the icon
            # remains legible at the small size used beside chat messages.
            crop = (225, 63, 946, 848) if slot == "user_avatar" else (195, 20, 1269, 972)
            source = QPixmap(str(Path(__file__).parent / "assets" / "avatars" / filename))
            icon = source.copy(*crop) if not source.isNull() else QPixmap()
            ChatView._avatar_pixmaps[slot] = icon
        if icon.isNull():
            label.clear(); label.setText("◈")
            return
        scaled = icon.scaled(edge, edge, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        if slot == "character_avatar":
            shifted = QPixmap(edge, edge); shifted.fill(Qt.GlobalColor.transparent)
            painter = QPainter(shifted)
            offset = round(edge * .08)
            painter.drawPixmap((edge - scaled.width()) // 2 + offset,
                               (edge - scaled.height()) // 2, scaled)
            painter.end(); label.setPixmap(shifted)
        else:
            label.setPixmap(scaled)

    def _expand(self, conversation_id: str, task_id: str) -> None:
        if self.isVisible() and (conversation_id == self.conversation_id or not conversation_id):
            self.expand_requested.emit(conversation_id or self.conversation_id, task_id)

    def _directory(self) -> None:
        selected = self.conversation_id
        self.selector.blockSignals(True); self.selector.clear()
        for identity, item in self.coordinator.conversations.items():
            self.selector.addItem(str(item.get("title") or "专项会话"), identity)
        index = self.selector.findData(selected or self.coordinator.default_id)
        self.selector.setCurrentIndex(index); self.selector.blockSignals(False)
        self.selector.setEnabled(self.selector.count() > 0)
        self.selector.setToolTip(self.selector.currentText() or "正在读取会话目录")
        if not selected or selected not in self.coordinator.conversations:
            if self.coordinator.default_id:
                self.set_conversation(self.coordinator.default_id)
        elif index >= 0:
            self.title.setText(str(self.coordinator.conversations[selected].get("title", "聊天")))
        self._ready()

    def _selected(self, index: int) -> None:
        identity = str(self.selector.currentData() or "")
        if identity:
            self.set_conversation(identity); self.conversation_selected.emit(identity)

    def show_default(self) -> None:
        if self.coordinator.default_id:
            self.set_conversation(self.coordinator.default_id)
        else:
            self.coordinator.refresh_directory()

    def set_conversation(self, conversation_id: str, task_id: str | None = None) -> None:
        if self.conversation_id != conversation_id:
            if self.conversation_id: self.positions[self.conversation_id] = self.area.verticalScrollBar().value()
            self._restore_position = self.positions.get(conversation_id)
            if self._restore_position is None: self._follow_next_render = True
        self.conversation_id, self.task_id = conversation_id, task_id or ""
        self._drop_history()
        self._signature = None
        self.title.setText(str(self.coordinator.conversations.get(conversation_id, {}).get("title", "聊天")))
        self.selector.blockSignals(True); self.selector.setCurrentIndex(self.selector.findData(conversation_id)); self.selector.blockSignals(False)
        self.selector.setToolTip(self.selector.currentText() or self.title.text())
        self._draft(conversation_id, self.coordinator.draft(conversation_id).text, 0)
        self.coordinator.watch(self, conversation_id, self.isVisible()); self._timeline(conversation_id)
        self._ready()
        if task_id:
            self._locate(task_id)
            self.coordinator.review(task_id)

    def _drop_history(self) -> None:
        self._page_generation += 1; self._page_reading = False; self._history_items = None
        self._history_cursor = None; self._history_more = False
        self._tail_signature = ""
        self._tail_ids.clear(); self._rendered_ids.clear(); self.new_message.hide()

    def _latest(self) -> None:
        self._drop_history(); self._signature = None; self._restore_position = None
        self.coordinator.latest(self.conversation_id); self._timeline(self.conversation_id); self._bottom()

    def _new_messages(self) -> None:
        if self._history_items is not None: self._latest()
        else: self._bottom()

    def _load_older(self) -> None:
        if self._page_reading or not self.coordinator.available: return
        before = self._history_cursor if self._history_items is not None else self.coordinator.cursors.get(self.conversation_id)
        if type(before) is int:
            self._page_generation += 1; self._read_history_page(before, self._page_generation)

    def _read_history_page(self, before: int, generation: int) -> None:
        self._page_reading = True; identity = self.conversation_id
        def done(data: dict[str, Any]) -> None:
            if generation != self._page_generation or identity != self.conversation_id or not self.coordinator.available: return
            self._page_reading = False
            if "_error" in data:
                self.feedback.setText("历史读取失败，当前页面已保留；可以重新加载。"); return
            self._history_items = list(data.get("items", []))[:50]
            self._tail_signature = repr(self.coordinator.timelines.get(identity, []))
            self._tail_ids = self._message_ids(self.coordinator.timelines.get(identity, []))
            self._history_cursor = data.get("next_cursor"); self._history_more = bool(data.get("has_more"))
            for item in self._history_items:
                if item.get("kind") == "task_card" and item.get("visible", True): self.coordinator.review(str(item.get("task_id") or ""))
            self._signature = None; self._timeline(identity)
        self.coordinator.read_page(identity, before, done, f"page:{id(self)}:{generation}")

    def _locate(self, task_id: str) -> None:
        self._page_generation += 1; generation = self._page_generation; identity = self.conversation_id
        self._page_reading = True
        def done(data: dict[str, Any]) -> None:
            if generation != self._page_generation or identity != self.conversation_id or not self.coordinator.available: return
            self._page_reading = False
            if "_error" in data or type(data.get("before")) is not int:
                self.feedback.setText("原消息无法定位，可能已清理；当前页面已保留。"); return
            self._read_history_page(data["before"], generation)
        self.coordinator.locate(identity, task_id, done, f"location:{id(self)}:{generation}")

    def _load_body(self, item_id: str, offset: int) -> None:
        if self._history_items is None:
            self.coordinator.load_body(self.conversation_id, item_id, offset); return
        generation = self._page_generation; identity = self.conversation_id
        def done(data: dict[str, Any]) -> None:
            if generation != self._page_generation or self._history_items is None: return
            for item in self._history_items:
                if item.get("id") == item_id:
                    body = str(data.get("body", "")); item.update(body=body, body_start=offset, body_truncated=bool(data.get("has_more")), body_offset=offset + len(body))
            self._timeline(identity)
        self.coordinator._read(f"history-body:{id(self)}:{item_id}", "conversation_item", {"id": identity, "item_id": item_id, "offset": offset, "limit": 32768}, done)

    def focus_input(self) -> None:
        self.input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _edited(self) -> None:
        if not self._populating and self.conversation_id:
            self._draft_rejected = not self.coordinator.edit_draft(self.conversation_id, self.input.toPlainText())
            self._ready()

    def _draft(self, identity: str, text: str, revision: int) -> None:
        if identity == self.conversation_id or not identity:
            self._draft_rejected = False
            if self.input.toPlainText() != text:
                self._populating = True; self.input.setPlainText(text); self._populating = False
            self._ready()

    def _send(self) -> None:
        if self._draft_rejected:
            self.feedback.setText("超出草稿容量的新输入未保存；当前显示原草稿。请先编辑或释放容量后再发送。"); return
        previous_submissions = len(self.coordinator.submissions)
        self._follow_next_render = True
        self.coordinator.submit(self.conversation_id)
        if len(self.coordinator.submissions) == previous_submissions:
            self._follow_next_render = False

    def _ready(self) -> None:
        if not self.coordinator.available: self._drop_history()
        conversation = self.coordinator.conversations.get(self.conversation_id, {})
        working = any(item.get("conversation_id") == self.conversation_id for item in self.coordinator.submissions.values())
        self.input.set_context_placeholder("可以继续补充要求…  Enter 发送，Shift + Enter 换行" if working
                                           else "写点什么…  Enter 发送，Shift + Enter 换行")
        archived = bool(conversation.get("archived_at") or conversation.get("archived"))
        self.input.setReadOnly(archived or not self.coordinator.available)
        ready = self.coordinator.available and self.coordinator.model_ready and not archived and not self._draft_rejected
        self.send.setEnabled(ready)
        message = ("新增输入超过草稿容量；当前显示原草稿，请先编辑或释放容量。" if self._draft_rejected
                   else "这段会话已归档，恢复后可继续聊天；草稿会保留。" if archived
                   else "" if ready else "本地记录可以浏览；模型连接尚未就绪，请从设置完成配置。")
        self.status.setText(message); self.status.setVisible(bool(message))

    @staticmethod
    def _message_ids(items: list[dict[str, Any]]) -> set[str]:
        return {str(item.get("id") or item.get("sequence") or item.get("task_id") or "")
                for item in items if not item.get("redacted_at") and item.get("visible") is not False
                and (item.get("body") or item.get("kind") == "task_card")} - {""}

    def _task_reply_changed(self, task_id: str) -> None:
        if any(item.get("task_id") == task_id and item.get("kind") == "assistant_message"
               for item in (self._history_items if self._history_items is not None else self.coordinator.timelines.get(self.conversation_id, []))):
            self._timeline(self.conversation_id)

    def _timeline(self, identity: str) -> None:
        if identity and identity != self.conversation_id:
            return
        self._ready()
        if self._history_items is not None:
            tail = repr(self.coordinator.timelines.get(self.conversation_id, []))
            tail_ids = self._message_ids(self.coordinator.timelines.get(self.conversation_id, []))
            self._tail_signature = tail; self._tail_ids = tail_ids
        items = self._history_items if self._history_items is not None else self.coordinator.timelines.get(self.conversation_id, [])
        pending = [value for value in self.coordinator.submissions.values() if value["conversation_id"] == self.conversation_id]
        replies: dict[str, str] = {}
        for item in items:
            if item.get("kind") != "assistant_message" or item.get("body_truncated") or item.get("body_start", 0):
                continue
            review = self.coordinator.reviews.get(str(item.get("task_id") or ""), {})
            result = review.get("task", {}).get("result")
            if isinstance(result, dict) and isinstance(result.get("tool_results"), list):
                facts = [fact for fact in result["tool_results"] if isinstance(fact, dict)]
                reply = legacy_chat_reply(str(item.get("body", "")), result.get("response"), facts)
                if reply is not None:
                    replies[str(item["id"])] = reply
        signature = repr((items, pending, replies))
        if self._signature == signature:
            return
        rendered_ids = self._message_ids(items)
        self._rendered_ids = rendered_ids
        self._signature = signature
        self._render_generation += 1
        bar = self.area.verticalScrollBar()
        follow = self._follow_next_render or (bar.maximum() > 0 and bar.maximum() - bar.value() < 50)
        self._follow_next_render = False
        position = bar.value()
        while self.messages.count():
            layout_item = self.messages.takeAt(0)
            retired_widget = layout_item.widget() if layout_item is not None else None
            if retired_widget is self.older:
                continue
            if retired_widget is not None:
                if isinstance(retired_widget, TaskCard): retired_widget.retire()
                retired_widget.hide(); retired_widget.deleteLater()
        self.messages.addWidget(self.older)
        redaction_shown = False
        for item in items:
            if item.get("redacted_at"):
                if not redaction_shown:
                    removed = text_label("部分聊天记录已因遗忘操作清理；这些消息和相关任务正文不再显示。")
                    removed.setObjectName("chat_redaction_notice"); self.messages.addWidget(removed)
                    redaction_shown = True
                continue
            if item.get("visible") is False:
                continue
            kind = str(item.get("kind", ""))
            if kind == "task_card":
                task_id = str(item.get("task_id", ""))
                card = TaskCard(self.coordinator, task_id, self.compact); self.messages.addWidget(card)
                if self.task_id == task_id:
                    def locate_card(card: TaskCard = card) -> None:
                        self.area.ensureWidgetVisible(card)
                    self._queue_scroll(locate_card, card)
                continue
            body = replies.get(str(item.get("id", "")), str(item.get("body", "")))
            if not body:
                continue
            frame = QFrame(); frame.setObjectName("user_message" if kind in {"user_message", "user"} else "message")
            layout = QVBoxLayout(frame); author = QHBoxLayout()
            slot = "user_avatar" if kind in {"user_message", "user"} else "character_avatar"
            avatar = text_label(); avatar.setObjectName("message_" + slot); avatar.setFixedSize(24, 24)
            avatar.setAlignment(Qt.AlignmentFlag.AlignCenter); avatar.setAccessibleName("用户头像" if slot == "user_avatar" else "角色头像")
            self._set_avatar(avatar, slot, self.coordinator.appearance.images.get(slot), 24)
            nickname = text_label(self._display_name(slot)); nickname.setObjectName("message_author")
            if slot == "user_avatar":
                author.addStretch(1); author.addWidget(nickname); author.addWidget(avatar)
            else:
                author.addWidget(avatar); author.addWidget(nickname); author.addStretch(1)
            layout.addLayout(author); layout.addWidget(text_label(body))
            if item.get("body_truncated"):
                more = QPushButton("读取消息下一段")
                offset = int(item.get("body_offset", len(body)))
                more.clicked.connect(lambda checked=False, item_id=str(item["id"]), offset=offset: self._load_body(item_id, offset))
                layout.addWidget(more)
            if item.get("body_start", 0):
                previous = QPushButton("读取消息上一段")
                previous.clicked.connect(lambda checked=False, item_id=str(item["id"]), offset=max(0, int(item.get("body_start", 0)) - 32768): self._load_body(item_id, offset))
                layout.addWidget(previous)
            self.messages.addWidget(frame)
        for item in pending:
            self.messages.addWidget(text_label(item["request"] + "\n" + item["state"]))
            check = QPushButton("使用原命令重试" if item.get("retryable") else "核查受理结果"); check.clicked.connect(lambda checked=False, command=item["command_id"]: self.coordinator.retry_submission(command)); self.messages.addWidget(check)
        if not items and not pending:
            empty = "欢迎回来。这里会保存我们的日常聊天。" if self.conversation_id == self.coordinator.default_id else "这段会话还没有消息。可以从这个主题开始聊。"
            self.messages.addWidget(text_label(empty)); self.new_message.hide()
        self.older.setVisible(self._history_more if self._history_items is not None else self.coordinator.more.get(self.conversation_id, False))
        self.latest_button.setVisible(self._history_items is not None)
        if self._restore_position is not None:
            desired = self._restore_position; self._restore_position = None
            self._queue_scroll(lambda: bar.setValue(desired))
        elif follow:
            self._queue_scroll(self._bottom)
        else:
            self._queue_scroll(lambda: bar.setValue(position))

    def _queue_scroll(self, callback: Callable[[], None], context: QObject | None = None) -> None:
        identity = self.conversation_id
        page_generation, render_generation = self._page_generation, self._render_generation
        def apply() -> None:
            if (identity == self.conversation_id and page_generation == self._page_generation
                    and render_generation == self._render_generation):
                callback()
                self._scroll_changed(self.area.verticalScrollBar().value())
        # Qt drops the queued functor when its widget is destroyed. A redraw also
        # retires the old layout's scroll intents while this view remains alive.
        def after_layout() -> None:
            QTimer.singleShot(0, context if context is not None else self, apply)
        QTimer.singleShot(0, context if context is not None else self, after_layout)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.area.viewport() and event.type() == QEvent.Type.Resize:
            self._position_new_message()
        return super().eventFilter(watched, event)

    def _position_new_message(self) -> None:
        viewport = self.area.viewport()
        self.new_message.move(max(8, (viewport.width() - self.new_message.width()) // 2),
                              max(8, viewport.height() - self.new_message.height() - 16))
        self.new_message.raise_()

    def _scroll_changed(self, value: int) -> None:
        bar = self.area.verticalScrollBar()
        self.new_message.setVisible(bar.maximum() - value >= 120)
        self._position_new_message()

    def _privacy(self, epoch: int = 0) -> None:
        self._drop_history()
        self.positions.clear(); self._restore_position = None; self.conversation_id = ""; self.task_id = ""; self.title.setText("聊天")
        self.input.clear(); self._signature = None; self.coordinator.watch(self, "", False); self._timeline("")

    def _bottom(self) -> None:
        self.area.verticalScrollBar().setValue(self.area.verticalScrollBar().maximum()); self.new_message.hide()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event); self.coordinator.watch(self, self.conversation_id, True)

    def hideEvent(self, event: QHideEvent) -> None:
        self.coordinator.watch(self, self.conversation_id, False); super().hideEvent(event)
