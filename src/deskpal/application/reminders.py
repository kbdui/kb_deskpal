"""Independent local reminder inbox; no Core, LLM, goal or GUI dependency."""

from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
import json
from threading import Event, RLock, Thread, current_thread
from typing import Any

from deskpal.scheduling.models import ScheduleEvent, SourceSnapshot
from deskpal.scheduling.ports import SchedulingManager
from deskpal.tasks.models import utc_now
from deskpal.tools.models import ToolResult
from deskpal.workflow.models import WorkflowBlocked
from deskpal.workflow.store import WorkflowStore, canonical

SOURCES = ("timer", "calendar")
NOTIFICATION_TTL_SECONDS = 120
MAX_INBOX_ROWS = 100000


def _date(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Reminder timestamps require an explicit offset")
    return result.astimezone(timezone.utc)


class ReminderInbox:
    def __init__(self, store: WorkflowStore, scheduling: SchedulingManager, *,
                 now: Callable[[], datetime] = utc_now, policy: Any = None,
                 on_occurrence: Callable[[str, str, str], None] | None = None) -> None:
        self.store, self.scheduling, self._now = store, scheduling, now
        self.policy = policy
        self._on_occurrence = on_occurrence
        self._started_at = now()
        self._lock = RLock()
        self._error: str | None = None
        # First enablement starts at a finite baseline. Persisted consumers keep
        # their position and can recover reminders due while the app was closed.
        identity = scheduling.store_identity
        for source in SOURCES:
            tail = scheduling.event_cursor_state(source)
            with store.transaction() as connection:
                connection.execute("INSERT OR IGNORE INTO desktop_reminder_cursor(source,store_id,generation,after_id,anchor) VALUES(?,?,?,?,?)",
                    (source, identity.store_id, identity.generation, tail.tail_id, tail.anchor))

    def register_result(self, task_id: str, result: ToolResult) -> None:
        """Only host-normalized successful creation results can link resources."""
        source = {"timer_create": "timer", "calendar_create": "calendar"}.get(result.tool_name)
        if source is None or not result.success or not isinstance(result.output, dict):
            return
        identity = result.output.get("id")
        if not isinstance(identity, str) or not identity or len(identity) > 100:
            return
        snapshot = self.scheduling.source_snapshot(source, identity)
        if snapshot is None:
            return
        with self._lock, self.store.transaction() as connection:
            task = connection.execute("SELECT scrubbed FROM task WHERE id=?", (task_id,)).fetchone()
            if task is None:
                return
            turn = connection.execute("SELECT r.conversation_id FROM turn_ref r JOIN conversation c ON c.id=r.conversation_id WHERE r.task_id=? AND r.epoch=c.context_epoch AND c.deleted_at IS NULL", (task_id,)).fetchone()
            conversation = None if turn is None or task["scrubbed"] else connection.execute("SELECT id FROM conversation WHERE id=? AND deleted_at IS NULL", (turn[0],)).fetchone()
            connection.execute("INSERT OR IGNORE INTO desktop_resource_link VALUES(?,?,?,?,?,?)",
                (source, identity, snapshot.store_id, snapshot.generation, task_id, None if conversation is None else conversation[0]))
            if task["scrubbed"]:
                connection.execute("UPDATE desktop_reminder_inbox SET task_id=?,conversation_id=NULL,label='',delivery_state='redacted' WHERE source=? AND source_id=? AND store_id=? AND generation=?",
                    (task_id, source, identity, snapshot.store_id, snapshot.generation))
                return
            # A timer can expire between scheduling's commit and this callback.
            # Attach only still-unassociated records; never revive redactions.
            if not task["scrubbed"] and conversation is not None:
                pending = connection.execute("SELECT * FROM desktop_reminder_inbox WHERE source=? AND source_id=? AND store_id=? AND generation=? AND task_id IS NULL AND delivery_state IN ('due','missed','quiet')",
                    (source, identity, snapshot.store_id, snapshot.generation)).fetchall()
                connection.execute("UPDATE desktop_reminder_inbox SET task_id=?,conversation_id=? WHERE source=? AND source_id=? AND store_id=? AND generation=? AND task_id IS NULL AND delivery_state NOT IN ('redacted','suppressed')",
                    (task_id, conversation[0], source, identity, snapshot.store_id, snapshot.generation))
                for row in pending:
                    self._append_timeline(connection, row["id"], conversation[0], task_id,
                        ScheduleEvent(row["event_id"], source, identity, row["label"], row["due_at"], row["recorded_at"], row["delivery_state"] == "missed"))

    def _current(self, row: Any, snapshot: SourceSnapshot | None) -> bool:
        if snapshot is None or snapshot.store_id != row["store_id"] or snapshot.generation != row["generation"] or snapshot.occurrence_revision != row["occurrence_revision"]:
            return False
        payload = json.loads(snapshot.payload_json)
        due = payload.get("due_at") if row["source"] == "timer" else payload.get("reminder_at")
        if due != row["due_at"]:
            return False
        label = payload.get("label") if row["source"] == "timer" else payload.get("title")
        if label != row["label"]:
            return False
        if row["source"] == "calendar" and _date(payload["updated_at"]) > _date(row["recorded_at"]):
            return False
        return payload.get("status") == "fired" if row["source"] == "timer" else bool(payload.get("reminder_fired"))

    def poll(self, *, limit: int = 100) -> int:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Reminder poll limit must be 1..100")
        processed = 0
        with self._lock:
            identity = self.scheduling.store_identity
            for source in SOURCES:
                with self.store.read_connection() as connection:
                    cursor = connection.execute("SELECT * FROM desktop_reminder_cursor WHERE source=?", (source,)).fetchone()
                assert cursor is not None
                if cursor["blocked"]:
                    continue
                anchor = self.scheduling.event_anchor(source, cursor["after_id"])
                if (cursor["store_id"], cursor["generation"]) != (identity.store_id, identity.generation) or anchor != cursor["anchor"]:
                    with self.store.transaction() as connection:
                        connection.execute("UPDATE desktop_reminder_cursor SET blocked=1 WHERE source=?", (source,))
                    continue
                events = self.scheduling.list_events(source, after_id=cursor["after_id"], limit=limit)
                for event in events:
                    snapshot = self.scheduling.source_snapshot(source, event.source_id)
                    event_anchor = self.scheduling.event_anchor(source, event.id)
                    if event_anchor is None or (event.source_store_id, event.source_generation) != (identity.store_id, identity.generation):
                        with self.store.transaction() as connection:
                            connection.execute("UPDATE desktop_reminder_cursor SET blocked=1 WHERE source=?", (source,))
                        break
                    reminder_enabled = self.policy is None or self.policy.allowed('reminder')
                    reminder_now = self.policy is None or self.policy.reminder_allowed_now()
                    with self.store.transaction() as connection:
                        self.store.check_privacy(connection)
                        # Inbox and stream advance commit together. Failed
                        # writes retry the same durable event without duplicates.
                        link = connection.execute("SELECT * FROM desktop_resource_link WHERE source=? AND source_id=? AND store_id=? AND generation=?",
                            (source, event.source_id, identity.store_id, identity.generation)).fetchone()
                        task_id = None if link is None else link["task_id"]
                        conversation_id = None if link is None else link["conversation_id"]
                        now = self._now()
                        missed = _date(event.due_at) < self._started_at or (now - _date(event.due_at)).total_seconds() > NOTIFICATION_TTL_SECONDS
                        if not reminder_enabled:
                            missed = True
                        quiet = not missed and not reminder_now
                        state = "missed" if missed else "quiet" if quiet else "due"
                        candidate = {"source": source, "store_id": identity.store_id, "generation": identity.generation,
                            "occurrence_revision": event.occurrence_revision, "due_at": event.due_at,
                            "label": event.label, "recorded_at": event.recorded_at}
                        if not self._current(candidate, snapshot):
                            state = "suppressed"
                        reminder_id = sha256(canonical([source, identity.store_id, identity.generation, event.id]).encode()).hexdigest()
                        if connection.execute("SELECT COUNT(*) FROM desktop_reminder_inbox").fetchone()[0] >= MAX_INBOX_ROWS and connection.execute("SELECT 1 FROM desktop_reminder_inbox WHERE id=?", (reminder_id,)).fetchone() is None:
                            raise WorkflowBlocked("Reminder inbox capacity reached; retain the source stream for review")
                        inserted = connection.execute("INSERT OR IGNORE INTO desktop_reminder_inbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (reminder_id, source, event.source_id, event.id, identity.store_id, identity.generation,
                             event.occurrence_revision, event.due_at, event.recorded_at,
                             "" if state in {"redacted", "suppressed"} else event.label, conversation_id, task_id, None, state)).rowcount
                        if inserted and state == "quiet":
                            connection.execute('INSERT OR IGNORE INTO notification_deferred VALUES(?,NULL)', (reminder_id,))
                        if inserted and state in {"due", "missed", "quiet"}:
                            self._append_timeline(connection, reminder_id, conversation_id, task_id, event)
                            if self._on_occurrence is not None:
                                self._on_occurrence(source, reminder_id, event.due_at)
                            if state == "due":
                                self.store.record_event("desktop_reminder_due", data={"id": reminder_id})
                        connection.execute("UPDATE desktop_reminder_cursor SET after_id=?,anchor=? WHERE source=?", (event.id, event_anchor, source))
                    processed += 1
            self._release_quiet()
            self._error = None
        return processed

    def _release_quiet(self) -> None:
        if self.policy is None or not self.policy.reminder_allowed_now():
            return
        with self.store.read_connection() as db:
            rows = db.execute("SELECT * FROM desktop_reminder_inbox WHERE delivery_state='quiet' AND acknowledged_at IS NULL LIMIT 100").fetchall()
        current = self._revalidate(rows)
        with self.store.transaction() as db:
            for item in current:
                changed = db.execute("UPDATE desktop_reminder_inbox SET delivery_state='due' WHERE id=? AND delivery_state='quiet' AND acknowledged_at IS NULL", (item['id'],))
                if changed.rowcount:
                    db.execute('UPDATE notification_deferred SET released_at=? WHERE reminder_id=?', (self._now().isoformat(), item['id']))
                    self.store.record_event('desktop_reminder_due', data={'id': item['id']})

    def _append_timeline(self, connection: Any, identity: str, conversation_id: str | None,
                         task_id: str | None, event: ScheduleEvent) -> None:
        if conversation_id is None:
            return
        conversation = connection.execute("SELECT next_sequence FROM conversation WHERE id=? AND deleted_at IS NULL", (conversation_id,)).fetchone()
        if conversation is None:
            return
        policy = "local_only"
        if task_id is not None:
            task = connection.execute("SELECT use_policy,scrubbed FROM task WHERE id=?", (task_id,)).fetchone()
            if task is None or task["scrubbed"]:
                return
            policy = task["use_policy"]
        inserted = connection.execute("INSERT OR IGNORE INTO conversation_item(id,conversation_id,sequence,kind,task_id,body,use_policy,created_at) VALUES(?,?,?,'reminder',?,?,?,?)",
            ("reminder:" + identity, conversation_id, conversation[0], task_id, event.label, policy, event.recorded_at)).rowcount
        if inserted:
            connection.execute("UPDATE conversation SET next_sequence=next_sequence+1,timeline_revision=timeline_revision+1,updated_at=? WHERE id=?", (event.recorded_at, conversation_id))

    def _revalidate(self, rows: list[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in rows:
            snapshot = self.scheduling.source_snapshot(row["source"], row["source_id"])
            # 校验日程自身是否已取消或变更；来源聊天的状态不参与此判断。
            with self.store.transaction() as connection:
                self.store.check_privacy(connection)
                fresh = connection.execute("SELECT * FROM desktop_reminder_inbox WHERE id=?", (row["id"],)).fetchone()
                if fresh is None:
                    continue
                # 日程自身的状态是唯一依据，聊天来源清理不撤销提醒。
                if fresh["delivery_state"] == "suppressed" or not self._current(fresh, snapshot):
                    connection.execute("UPDATE desktop_reminder_inbox SET label='',delivery_state='suppressed' WHERE id=?", (fresh["id"],))
                    item_id = "reminder:" + fresh["id"]
                    changed = connection.execute("UPDATE conversation_item SET body='',redacted_at=?,revision=revision+1 WHERE id=? AND redacted_at IS NULL", (self._now().isoformat(), item_id)).rowcount
                    if changed and fresh["conversation_id"] is not None:
                        connection.execute("UPDATE conversation SET timeline_revision=timeline_revision+1 WHERE id=?", (fresh["conversation_id"],))
                    continue
                value = dict(fresh)
                deferred = connection.execute('SELECT released_at FROM notification_deferred WHERE reminder_id=?', (value['id'],)).fetchone()
                if value["delivery_state"] == "due" and _date(value["due_at"]) < self._started_at and not (deferred and deferred[0]):
                    value["delivery_state"] = "missed"
                    connection.execute("UPDATE desktop_reminder_inbox SET delivery_state='missed' WHERE id=?", (value["id"],))
                value["status"] = value["delivery_state"]
                result.append(value)
        return result

    def feed(self, *, cursor: str | None = None, limit: int = 50, unread: bool = False) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100 or type(unread) is not bool:
            raise ValueError("Reminder feed limit must be 1..100 and unread a boolean")
        anchor = ("", "")
        if cursor is not None:
            if not isinstance(cursor, str) or len(cursor) > 512:
                raise ValueError("Invalid reminder cursor")
            try:
                parts = json.loads(cursor)
            except (ValueError, TypeError):
                raise ValueError("Invalid reminder cursor") from None
            if not isinstance(parts, list) or len(parts) != 2 or any(not isinstance(value, str) for value in parts):
                raise ValueError("Invalid reminder cursor")
            anchor = parts[0], parts[1]
        with self._lock:
            with self.store.read_connection() as connection:
                self.store.check_privacy(connection)
                view_epoch = connection.execute("SELECT epoch FROM privacy_control WHERE id=1").fetchone()[0]
                rows = connection.execute("SELECT * FROM desktop_reminder_inbox WHERE delivery_state IN ('due','missed','quiet') AND (?=0 OR acknowledged_at IS NULL) AND (recorded_at,id)>(?,?) ORDER BY recorded_at,id LIMIT ?",
                    (int(unread), *anchor, limit + 1)).fetchall()
                streams = [dict(row) for row in connection.execute("SELECT source,blocked FROM desktop_reminder_cursor ORDER BY source")]
            chosen = rows[:limit]
            values = self._revalidate(chosen)
            with self.store.read_connection() as connection:
                self.store.check_privacy(connection)
                if connection.execute("SELECT epoch FROM privacy_control WHERE id=1").fetchone()[0] != view_epoch:
                    raise WorkflowBlocked("Reminder privacy view changed; refresh the inbox")
                unread_count = connection.execute("SELECT COUNT(*) FROM desktop_reminder_inbox WHERE acknowledged_at IS NULL AND delivery_state IN ('due','missed','quiet')").fetchone()[0]
            return {"schema_version": 1, "items": values, "has_more": len(rows) > limit,
                "next_cursor": canonical([chosen[-1]["recorded_at"], chosen[-1]["id"]]) if chosen and len(rows) > limit else None,
                "unread_count": unread_count, "streams": streams}

    def detail(self, id: str) -> dict[str, Any]:
        if not isinstance(id, str) or len(id) != 64 or any(char not in "0123456789abcdef" for char in id):
            raise ValueError("Invalid reminder identity")
        with self._lock:
            with self.store.read_connection() as connection:
                self.store.check_privacy(connection)
                row = connection.execute("SELECT * FROM desktop_reminder_inbox WHERE id=?", (id,)).fetchone()
            if row is None:
                raise KeyError(id)
            values = self._revalidate([row])
            return {"schema_version": 1, "item": values[0] if values else None, "unavailable": not bool(values)}

    def notification(self, identity: str) -> dict[str, Any] | None:
        if self.policy is not None and not self.policy.allowed("reminder"):
            return None
        item = self.detail(identity)["item"]
        if item is None or item["acknowledged_at"] is not None or item["delivery_state"] != "due":
            return None
        with self.store.read_connection() as db:
            deferred = db.execute('SELECT released_at FROM notification_deferred WHERE reminder_id=?', (identity,)).fetchone()
        anchor = deferred[0] if deferred and deferred[0] else item['due_at']
        age = (self._now() - _date(anchor)).total_seconds()
        if not 0 <= age <= NOTIFICATION_TTL_SECONDS:
            return None
        return {"kind": "reminder", "entity_id": identity, "revision": item["occurrence_revision"], "status": "due"}

    def ack(self, id: str, command_id: str) -> dict[str, Any]:
        if not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 200:
            raise ValueError("Reminder acknowledgement requires a stable command_id")
        self.detail(id)
        digest = sha256(canonical({"id": id}).encode()).hexdigest()
        with self._lock, self.store.transaction() as connection:
            self.store.check_privacy(connection)
            if connection.execute("SELECT 1 FROM command WHERE id=?", (command_id,)).fetchone() is not None:
                raise ValueError("Command ID already belongs to a task operation")
            prior = connection.execute("SELECT * FROM conversation_receipt WHERE command_id=?", (command_id,)).fetchone()
            if prior is not None and (prior["operation"] != "reminder_ack" or prior["payload_hash"] != digest):
                raise ValueError("Command ID already used with another operation or payload")
            row = connection.execute("SELECT * FROM desktop_reminder_inbox WHERE id=?", (id,)).fetchone()
            assert row is not None
            if prior is None:
                connection.execute("UPDATE desktop_reminder_inbox SET acknowledged_at=COALESCE(acknowledged_at,?) WHERE id=?", (self._now().isoformat(), id))
                default = connection.execute("SELECT id FROM conversation WHERE kind='default'").fetchone()
                connection.execute("INSERT INTO conversation_receipt VALUES(?,?,?,?,?)", (command_id, digest, "reminder_ack", row["conversation_id"] or default[0], id))
            current = connection.execute("SELECT acknowledged_at FROM desktop_reminder_inbox WHERE id=?", (id,)).fetchone()
            return {"schema_version": 1, "accepted": True, "id": id, "acknowledged_at": current[0]}

    def diagnostics(self) -> dict[str, Any]:
        return {"error": self._error, "notification_ttl_seconds": NOTIFICATION_TTL_SECONDS}


class ReminderPump:
    """One light consumer, owned/joined by Application before closing stores."""
    def __init__(self, inbox: ReminderInbox, *, interval: float = 0.5) -> None:
        self.inbox = inbox
        self._interval = interval
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            thread = Thread(target=self._run, name="deskpal-reminders", daemon=True)
            thread.start()
            self._thread = thread

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self, timeout: float) -> bool:
        self.stop()
        if self._thread is None:
            return True
        if current_thread() is self._thread:
            return False
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.inbox.poll()
            except Exception as error:
                self.inbox._error = type(error).__name__
            self._stop.wait(self._interval)
