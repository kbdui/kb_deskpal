"""Workflow-backed conversation catalogue and authoritative display records.

Old tasks are deliberately not imported. Display records are written only by
submission and task publication; loading a page cannot replay an execution.
"""

from collections.abc import Callable, Mapping
from hashlib import sha256
import json
import sqlite3
from typing import Any, cast
from uuid import uuid4

from deskpal.tasks.models import utc_now
from deskpal.workflow.models import WorkflowBlocked
from deskpal.workflow.store import WorkflowStore, canonical

from .models import ConversationRef, ConversationTurn
from .runtime import ConversationConflict, MAX_HISTORY_BYTES, MAX_TURNS

MAX_DIRECTORY_ENTRIES = 10000
MAX_TIMELINE_ITEMS = 100000


class ConversationRepository:
    def __init__(self, store: WorkflowStore) -> None:
        self.store = store
        self.session_id = str(uuid4())

    def save_context_summary(self, identity: str, epoch: int, expected_revision: int,
                             content: Mapping[str, Any], refs: tuple[ConversationRef, ...]) -> dict[str, Any]:
        """P4 的摘要写入口；摘要是会话进展，不是长期事实或新指令。"""
        if (set(content) != {"progress", "decisions", "open_questions"} or not refs or len(refs) > 384
                or any(not isinstance(values, list) or len(values) > 16 or
                       any(not isinstance(text, str) or not text.strip() for text in values)
                       for values in content.values())):
            raise ValueError("Invalid conversation summary")
        body = canonical(dict(content))
        if len(body.encode("utf-8")) > 4096:
            raise ValueError("Summary exceeds context budget")
        with self.store.transaction() as conn:
            self._row(conn, identity, epoch, writable=True)
            for ref in refs:
                role = ref.id.rsplit(":", 1)
                source = conn.execute("SELECT r.sequence FROM turn_ref r JOIN conversation_turn t ON t.task_id=r.task_id WHERE r.task_id=? AND r.conversation_id=? AND r.epoch=? AND t.published=1",
                                      (role[0], identity, epoch)).fetchone()
                version = conn.execute("SELECT revision,active,use_policy FROM dependency_version WHERE kind=? AND object_id=?",
                                       (ref.kind, ref.id)).fetchone()
                if (ref.kind != "dialogue_turn" or len(role) != 2 or role[1] not in ("user", "assistant")
                        or source is None or source[0] != ref.revision or version is None
                        or version[0] != ref.revision or not version[1] or version[2] != ref.use_policy):
                    raise ConversationConflict("Summary source is unavailable")
            prior = conn.execute("SELECT revision FROM conversation_context_summary WHERE conversation_id=?", (identity,)).fetchone()
            if type(expected_revision) is not int or expected_revision != (prior[0] if prior else 0):
                raise ConversationConflict("Summary revision changed")
            revision = expected_revision + 1
            policy = "local_only" if any(ref.use_policy == "local_only" for ref in refs) else "cloud_allowed"
            conn.execute("INSERT INTO conversation_context_summary VALUES(?,?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET epoch=excluded.epoch,revision=excluded.revision,content=excluded.content,refs=excluded.refs,use_policy=excluded.use_policy",
                         (identity, epoch, revision, body, canonical([ref.to_dict() for ref in refs]), policy))
            return {"revision": revision, "conversation_id": identity, "epoch": epoch}

    def context_summary(self, identity: str, epoch: int, *, allow_restricted: bool = False) -> dict[str, Any] | None:
        with self.store.read_connection() as conn:
            self.store.check_privacy(conn)
            self._row(conn, identity, epoch)
            row = conn.execute("SELECT * FROM conversation_context_summary WHERE conversation_id=? AND epoch=?", (identity, epoch)).fetchone()
            if row is None:
                return None
            for ref in json.loads(row["refs"]):
                source = conn.execute("SELECT revision,active FROM dependency_version WHERE kind=? AND object_id=?",
                                      (ref["kind"], ref["id"])).fetchone()
                if source is None or source[0] != ref["revision"] or not source[1]:
                    return {"unavailable": True, "revision": row['revision']}
            if row["use_policy"] == "local_only" and not allow_restricted:
                return {"unavailable": True, "revision": row['revision']}
            return {"kind": "conversation_summary", "content": json.loads(row["content"]),
                    "revision": row["revision"], "instruction": False,
                    "externally_verified": False, "refs": json.loads(row["refs"])}

    def invalidate_memory_summaries(self, memory_id: str) -> None:
        """原记忆更正后，相关会话摘要须重建，不能继续引用旧值。"""
        with self.store.transaction() as db:
            db.execute("DELETE FROM conversation_context_summary WHERE conversation_id IN "
                "(SELECT i.conversation_id FROM conversation_item i JOIN conversation_item_source s ON s.item_id=i.id WHERE s.source_kind='memory' AND s.source_id=?)", (memory_id,))

    def _meta(self, conn: sqlite3.Connection) -> dict[str, Any]:
        privacy = conn.execute("SELECT epoch FROM privacy_control WHERE id=1").fetchone()
        assert privacy is not None
        return {"schema_version": 1, "privacy_epoch": int(privacy[0]), "session_id": self.session_id}

    def _summary(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result.pop("next_sequence", None)
        result.pop("next_turn", None)
        return {**result, "conversation_id": row["id"], "epoch": row["context_epoch"], **self._meta(conn)}

    def _row(self, conn: sqlite3.Connection, identity: str, epoch: int | None = None,
             *, writable: bool = False) -> sqlite3.Row:
        if not isinstance(identity, str) or not identity or len(identity) > 200:
            raise ConversationConflict("Invalid conversation identity")
        # WorkflowStore sets sqlite3.Row as this connection's row_factory.
        row = cast(sqlite3.Row | None, conn.execute("SELECT * FROM conversation WHERE id=?", (identity,)).fetchone())
        if row is None or row["deleted_at"] is not None:
            raise ConversationConflict("Conversation was deleted or does not exist")
        if epoch is not None and (type(epoch) is not int or epoch < 0 or row["context_epoch"] != epoch):
            raise ConversationConflict("Conversation context expired")
        if writable and row["archived_at"] is not None:
            raise ConversationConflict("Archived conversation cannot receive messages")
        return row

    @staticmethod
    def _title(title: str) -> str:
        if not isinstance(title, str) or not title.strip() or len(title.encode("utf-8")) > 400:
            raise ValueError("Conversation title must contain 1..400 UTF-8 bytes")
        return title.strip()

    def ensure_default(self, command_id: str | None = None) -> dict[str, Any]:
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM conversation WHERE kind='default'").fetchone()
            if row is None:
                now = utc_now().isoformat()
                conn.execute("INSERT INTO conversation(id,kind,title,created_at,updated_at) VALUES(?,'default',?,?,?)",
                             (str(uuid4()), "日常聊天", now, now))
                row = conn.execute("SELECT * FROM conversation WHERE kind='default'").fetchone()
            assert row is not None
            result = self._summary(conn, row)
            if command_id is not None:
                self._receipt(conn, command_id, "ensure_default", {}, row["id"], row["id"])
            return result

    def get(self, identity: str) -> dict[str, Any]:
        with self.store.read_connection() as conn:
            return self._summary(conn, self._row(conn, identity))

    def contains(self, identity: str) -> bool:
        """Recognize durable identities, including deleted tombstones."""
        with self.store.read_connection() as conn:
            return conn.execute("SELECT 1 FROM conversation WHERE id=?", (identity,)).fetchone() is not None

    def _prior(self, conn: sqlite3.Connection, command_id: str, operation: str,
               payload: Mapping[str, Any]) -> sqlite3.Row | None:
        if not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 200:
            raise ValueError("Conversation mutation requires a bounded command_id")
        command = conn.execute("SELECT task_id FROM command WHERE id=?", (command_id,)).fetchone()
        if command is not None and (operation != "submit" or payload.get("task_id") != command["task_id"]):
            raise ValueError("Command ID already belongs to another application operation")
        row = cast(sqlite3.Row | None, conn.execute("SELECT * FROM conversation_receipt WHERE command_id=?", (command_id,)).fetchone())
        digest = sha256(canonical(dict(payload)).encode("utf-8")).hexdigest()
        if row is not None and (row["payload_hash"] != digest or row["operation"] != operation):
            raise ValueError("Conversation command_id already used with another payload")
        return row

    def _receipt(self, conn: sqlite3.Connection, command_id: str, operation: str,
                 payload: Mapping[str, Any], identity: str, result_identity: str) -> None:
        if self._prior(conn, command_id, operation, payload) is None:
            conn.execute("INSERT INTO conversation_receipt VALUES(?,?,?,?,?)",
                         (command_id, sha256(canonical(dict(payload)).encode("utf-8")).hexdigest(),
                          operation, identity, result_identity))

    def create(self, title: str, command_id: str) -> dict[str, Any]:
        title = self._title(title)
        with self.store.transaction() as conn:
            self.store.check_privacy(conn)
            prior = self._prior(conn, command_id, "create", {"title": title})
            if prior is not None:
                # Read current content, never replay deleted titles from receipts.
                return self.get(prior["conversation_id"])
            if conn.execute("SELECT COUNT(*) FROM conversation").fetchone()[0] >= MAX_DIRECTORY_ENTRIES:
                raise WorkflowBlocked("Conversation storage capacity reached")
            identity, now = str(uuid4()), utc_now().isoformat()
            conn.execute("INSERT INTO conversation(id,kind,title,created_at,updated_at) VALUES(?,'project',?,?,?)",
                         (identity, title, now, now))
            self._receipt(conn, command_id, "create", {"title": title}, identity, identity)
            return self.get(identity)

    def update(self, identity: str, expected_revision: int, command_id: str, *,
               title: str | None = None, archived: bool | None = None) -> dict[str, Any]:
        if title is not None:
            title = self._title(title)
        if archived is not None and type(archived) is not bool:
            raise ValueError("archived must be a boolean")
        payload = {"id": identity, "expected_revision": expected_revision, "title": title, "archived": archived}
        with self.store.transaction() as conn:
            self.store.check_privacy(conn)
            if self._prior(conn, command_id, "update", payload) is not None:
                return self.get(identity)
            row = self._row(conn, identity)
            self._cas(row, expected_revision)
            if row["kind"] == "default" and archived:
                raise ValueError("Default conversation cannot be archived")
            if row["kind"] == "default" and title is not None and title != "日常聊天":
                raise ValueError("Default conversation title is fixed")
            now = utc_now().isoformat()
            archive_at = row["archived_at"] if archived is None else now if archived else None
            conn.execute("UPDATE conversation SET title=?,archived_at=?,revision=revision+1,updated_at=? WHERE id=?",
                         (title if title is not None else row["title"], archive_at, now, identity))
            self._receipt(conn, command_id, "update", payload, identity, identity)
            return self.get(identity)

    @staticmethod
    def _cas(row: sqlite3.Row, revision: int) -> None:
        if type(revision) is not int or revision < 1 or row["revision"] != revision:
            raise ConversationConflict("Conversation revision changed")

    def page(self, *, cursor: str | None = None, limit: int = 50, archived: bool = False) -> dict[str, Any]:
        self._limit(limit)
        if type(archived) is not bool:
            raise ValueError("archived must be a boolean")
        default = self.ensure_default()
        with self.store.read_connection() as conn:
            anchor = ("", "") if cursor is None else self._cursor(cursor)
            rows = conn.execute("SELECT * FROM conversation WHERE deleted_at IS NULL AND (archived_at IS NOT NULL)=? AND (created_at,id)>(?,?) ORDER BY created_at,id LIMIT ?",
                                (int(archived), *anchor, limit + 1)).fetchall()
            chosen = rows[:limit]
            return {"items": [self._summary(conn, row) for row in chosen], "default_id": default["id"],
                    "has_more": len(rows) > limit,
                    "next_cursor": canonical([chosen[-1]["created_at"], chosen[-1]["id"]]) if chosen and len(rows) > limit else None,
                    **self._meta(conn)}

    @staticmethod
    def _cursor(cursor: str) -> tuple[str, str]:
        if not isinstance(cursor, str) or len(cursor) > 1024:
            raise ValueError("Invalid directory cursor")
        try:
            values = json.loads(cursor)
        except ValueError as exc:
            raise ValueError("Invalid directory cursor") from exc
        if not isinstance(values, list) or len(values) != 2 or any(not isinstance(v, str) for v in values):
            raise ValueError("Invalid directory cursor")
        return values[0], values[1]

    @staticmethod
    def _limit(limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Conversation page limit must be 1..100")

    def bind(self, task_id: str, identity: str, epoch: int, *, command_id: str, proactive: bool = False) -> dict[str, Any]:
        """Called inside the dispatcher's Task/Submission transaction."""
        with self.store.transaction() as conn:
            self.store.check_privacy(conn, task_id)
            row = self._row(conn, identity, epoch)
            prior = conn.execute("SELECT * FROM turn_ref WHERE task_id=?", (task_id,)).fetchone()
            if prior is not None:
                if prior["conversation_id"] != identity or prior["epoch"] != epoch:
                    raise ConversationConflict("Command retry cannot change conversation")
                return self.submission(task_id)
            if row["archived_at"] is not None:
                raise ConversationConflict("Archived conversation cannot receive messages")
            if conn.execute("SELECT COUNT(*) FROM conversation_item").fetchone()[0] + 3 > MAX_TIMELINE_ITEMS:
                raise WorkflowBlocked("Conversation timeline storage capacity reached")
            task = conn.execute("SELECT request,use_policy,created_at FROM task WHERE id=?", (task_id,)).fetchone()
            assert task is not None
            privacy_epoch = conn.execute("SELECT epoch FROM privacy_control WHERE id=1").fetchone()[0]
            conn.execute("INSERT INTO turn_ref VALUES(?,?,?,?,?)", (task_id, identity, epoch, row["next_turn"], privacy_epoch))
            conn.execute("INSERT INTO conversation_turn(task_id) VALUES(?)", (task_id,))
            kinds = ("task_card", "assistant_message") if proactive else ("user_message", "task_card", "assistant_message")
            for offset, kind in enumerate(kinds):
                conn.execute("INSERT INTO conversation_item(id,conversation_id,sequence,kind,task_id,body,use_policy,created_at) VALUES(?,?,?,?,?,?,?,?)",
                             (task_id + ":" + kind, identity, row["next_sequence"] + offset, kind, task_id,
                              task["request"] if kind == "user_message" else "", task["use_policy"], task["created_at"]))
            conn.execute("UPDATE conversation SET next_sequence=next_sequence+?,next_turn=next_turn+1,timeline_revision=timeline_revision+1,updated_at=? WHERE id=?", (len(kinds), utc_now().isoformat(), identity))
            self._receipt(conn, command_id, "submit", {"task_id": task_id, "id": identity, "epoch": epoch}, identity, task_id)
            for role in (("", ":assistant") if proactive else ("", ":user", ":assistant")):
                self.store.set_dependency_version("dialogue_turn", task_id + role,
                    row["next_turn"] if role else 1, use_policy=task["use_policy"])
            return self.submission(task_id)

    def submission(self, task_id: str) -> dict[str, Any]:
        with self.store.read_connection() as conn:
            row = conn.execute("SELECT conversation_id,epoch,sequence FROM turn_ref WHERE task_id=?", (task_id,)).fetchone()
            if row is None or row["conversation_id"].startswith("single:"):
                return {}
            return {"conversation_id": row["conversation_id"], "context_epoch": row["epoch"], "epoch": row["epoch"],
                    "message_id": task_id + ":user_message", "card_id": task_id + ":task_card", **self._meta(conn)}

    def timeline(self, identity: str, *, before: int | None = None, after: int | None = None,
                 limit: int = 50, known_revision: int | None = None) -> dict[str, Any]:
        self._limit(limit)
        if before is not None and after is not None:
            raise ValueError("Specify before or after, not both")
        for value in (before, after, known_revision):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Timeline cursors and revision must be non-negative integers")
        with self.store.read_connection() as conn:
            self.store.check_privacy(conn)
            conversation = self._row(conn, identity)
            reset_required = after is not None and known_revision is not None and known_revision != conversation["timeline_revision"]
            if reset_required:
                after = None
            ascending = after is not None
            relation, anchor = (">", after) if ascending else ("<", before if before is not None else conversation["next_sequence"])
            order = "ASC" if ascending else "DESC"
            rows = conn.execute(f"SELECT i.*,t.status,t.revision AS task_revision,t.cancel_requested FROM conversation_item i LEFT JOIN task t ON t.id=i.task_id WHERE i.conversation_id=? AND i.sequence{relation}? ORDER BY i.sequence {order} LIMIT ?",
                                (identity, anchor, limit + 1)).fetchall()
            chosen = rows[:limit]
            items: list[dict[str, Any]] = []
            # Keep replies under the bridge's bounded result size even for
            # pathological histories. Full long text has an explicit detail path.
            remaining = 2 * 1024 * 1024
            for row in chosen:
                item = {key: row[key] for key in ("id", "conversation_id", "sequence", "kind", "task_id", "revision", "body", "use_policy", "created_at", "redacted_at")}
                body = str(item["body"])
                encoded = body.encode("utf-8")
                budget = min(32768, remaining)
                item["body_truncated"] = len(encoded) > budget
                item["body_bytes"] = len(encoded)
                item["body"] = encoded[:budget].decode("utf-8", errors="ignore")
                remaining -= len(str(item["body"]).encode("utf-8"))
                item["task"] = {"id": row["task_id"], "status": row["status"], "revision": row["task_revision"], "cancel_requested": bool(row["cancel_requested"])} if row["task_id"] else None
                plan = conn.execute("SELECT canonical_json FROM immutableplan WHERE task_id=? AND scrubbed=0", (row["task_id"],)).fetchone()
                has_steps = plan is not None and bool(json.loads(plan[0]).get("steps"))
                item["visible"] = row["kind"] != "task_card" or row["status"] in {"Waiting", "NeedsReview", "Uncertain", "Paused"} or has_steps
                items.append(item)
            if not ascending:
                items.reverse()
            return {"conversation": self._summary(conn, conversation), "conversation_id": identity,
                    "revision": conversation["revision"], "timeline_revision": conversation["timeline_revision"], "reset_required": reset_required,
                    "context_epoch": conversation["context_epoch"], "epoch": conversation["context_epoch"],
                    "items": items, "has_more": len(rows) > limit,
                    "next_cursor": chosen[-1]["sequence"] if chosen and len(rows) > limit else None,
                    **self._meta(conn)}

    def detail(self, identity: str, item_id: str, *, offset: int = 0, limit: int = 32768) -> dict[str, Any]:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 65536:
            raise ValueError("Invalid message detail page")
        with self.store.read_connection() as conn:
            self.store.check_privacy(conn)
            self._row(conn, identity)
            row = conn.execute("SELECT body,revision,redacted_at FROM conversation_item WHERE id=? AND conversation_id=?", (item_id, identity)).fetchone()
            if row is None:
                raise KeyError(item_id)
            # Character offset avoids splitting multi-byte characters across pages.
            body = row["body"]
            return {"id": item_id, "conversation_id": identity, "body": body[offset:offset + limit],
                    "offset": offset, "has_more": len(body) > offset + limit, "revision": row["revision"],
                    "redacted_at": row["redacted_at"], **self._meta(conn)}

    def task_location(self, identity: str, task_id: str) -> dict[str, Any]:
        """Locate one intact task card without reading or scanning message bodies."""
        if not isinstance(task_id, str) or not task_id or len(task_id) > 200:
            raise ValueError("Invalid task identity")
        with self.store.read_connection() as conn:
            self.store.check_privacy(conn)
            self._row(conn, identity)
            row = conn.execute("SELECT i.sequence FROM conversation_item i JOIN task t ON t.id=i.task_id WHERE i.id=? AND i.conversation_id=? AND i.task_id=? AND i.kind='task_card' AND i.redacted_at IS NULL AND t.scrubbed=0",
                (task_id + ":task_card", identity, task_id)).fetchone()
            if row is None:
                raise KeyError(task_id)
            # timeline uses an exclusive before cursor; include the assistant
            # placeholder immediately after this card as well as its user turn.
            return {"conversation_id": identity, "task_id": task_id,
                    "before": row["sequence"] + 2, **self._meta(conn)}

    def publish(self, task_id: str, response: str, refs: tuple[ConversationRef, ...],
                facts: tuple[str, ...], policy: str) -> None:
        with self.store.transaction() as conn:
            self.store.check_dependencies(conn, task_id)
            self.store.check_context_dependencies(conn, task_id, "expression")
            conn.execute("UPDATE conversation_item SET body=?,use_policy=?,revision=revision+1 WHERE task_id=? AND kind='assistant_message' AND redacted_at IS NULL", (response, policy, task_id))
            conn.execute("UPDATE conversation_turn SET refs=?,facts=?,published=1 WHERE task_id=?",
                         (canonical([ref.to_dict() for ref in refs]), canonical(list(facts)), task_id))
            conn.execute("UPDATE conversation SET timeline_revision=timeline_revision+1 WHERE id=(SELECT conversation_id FROM turn_ref WHERE task_id=?)", (task_id,))
            for item in conn.execute("SELECT id FROM conversation_item WHERE task_id=? AND redacted_at IS NULL", (task_id,)).fetchall():
                for ref in refs:
                    conn.execute("INSERT OR REPLACE INTO conversation_item_source VALUES(?,?,?,?,?)", (item[0], ref.kind, ref.id, ref.revision, ref.use_policy))

    def load(self, identity: str, epoch: int, before_sequence: int) -> tuple[ConversationTurn, ...]:
        # Copy a bounded candidate page, then release the workflow lock before
        # visiting Memory or another independent source authority.
        with self.store.read_connection() as conn:
            self.store.check_privacy(conn)
            self._row(conn, identity, epoch)
            privacy_epoch = self._meta(conn)["privacy_epoch"]
            rows = [dict(row) for row in conn.execute("SELECT r.sequence,r.task_id,COALESCE(u.body,'') AS request,a.body AS response,a.use_policy,c.refs,c.facts,t.revision AS task_revision,COALESCE(u.revision,0) AS user_revision,a.revision AS assistant_revision FROM turn_ref r JOIN conversation_turn c ON c.task_id=r.task_id LEFT JOIN conversation_item u ON u.task_id=r.task_id AND u.kind='user_message' JOIN conversation_item a ON a.task_id=r.task_id AND a.kind='assistant_message' JOIN task t ON t.id=r.task_id WHERE r.conversation_id=? AND r.epoch=? AND r.sequence<? AND c.published=1 AND t.scrubbed=0 AND u.redacted_at IS NULL AND a.redacted_at IS NULL ORDER BY r.sequence DESC LIMIT ?", (identity, epoch, before_sequence, MAX_TURNS)).fetchall()]
        validated: list[dict[str, Any]] = []
        for row in rows:
            try:
                self.store.validate_external_context(row["task_id"], "expression")
            except WorkflowBlocked:
                continue
            validated.append(row)
        with self.store.read_connection() as conn:
            self.store.check_privacy(conn)
            self._row(conn, identity, epoch)
            if self._meta(conn)["privacy_epoch"] != privacy_epoch:
                raise WorkflowBlocked("Privacy changed while historical sources were being checked")
            result: list[ConversationTurn] = []
            used = 0
            for row in validated:
                try:
                    self.store.check_historical_context(conn, row["task_id"], "expression")
                except WorkflowBlocked:
                    continue
                current = conn.execute("SELECT t.revision AS task_revision,COALESCE(u.revision,0) AS user_revision,a.revision AS assistant_revision FROM task t LEFT JOIN conversation_item u ON u.task_id=t.id AND u.kind='user_message' JOIN conversation_item a ON a.task_id=t.id AND a.kind='assistant_message' WHERE t.id=? AND t.scrubbed=0 AND u.redacted_at IS NULL AND a.redacted_at IS NULL", (row["task_id"],)).fetchone()
                if current is None or any(current[key] != row[key] for key in ("task_revision", "user_revision", "assistant_revision")):
                    continue
                refs = tuple(ConversationRef(**ref) for ref in json.loads(row["refs"]))
                facts = tuple(str(fact) for fact in json.loads(row["facts"]))
                turn = ConversationTurn(row["sequence"], row["task_id"], row["request"], row["response"], refs, row["use_policy"], facts)
                used += turn.history_bytes + sum(len(fact.encode("utf-8")) for fact in facts)
                if used > MAX_HISTORY_BYTES:
                    break
                result.append(turn)
            return tuple(reversed(result))

    def cleanup(self, identity: str, expected_revision: int, command_id: str, *,
                delete: bool, scrub: Callable[[str], Any]) -> dict[str, Any]:
        operation = "delete" if delete else "clear"
        payload = {"id": identity, "expected_revision": expected_revision}
        with self.store.transaction() as conn:
            self.store.check_privacy(conn)
            prior = self._prior(conn, command_id, operation, payload)
            if prior is not None:
                return self.cleanup_status(prior["result_identity"])
            row = self._row(conn, identity)
            self._cas(row, expected_revision)
            if delete and row["kind"] == "default":
                raise ValueError("Default conversation cannot be deleted")
            now, job_id = utc_now().isoformat(), str(uuid4())
            # First revoke old turns in the same transaction as scrubbing. Every
            # Started/model/publication guard also checks this context epoch.
            conn.execute("UPDATE conversation SET context_epoch=context_epoch+1,revision=revision+1,timeline_revision=timeline_revision+1,title=?,deleted_at=?,updated_at=? WHERE id=?",
                         ("日常聊天" if row["kind"] == "default" else "已清空的会话", now if delete else None, now, identity))
            conn.execute("DELETE FROM conversation_context_summary WHERE conversation_id=?", (identity,))
            ids = tuple(r[0] for r in conn.execute("""WITH RECURSIVE affected(task_id) AS (
                SELECT task_id FROM turn_ref WHERE conversation_id=?
                UNION SELECT d.task_id FROM context_dependency d JOIN affected a ON d.kind='dialogue_turn' AND d.object_id IN (a.task_id,a.task_id||':user',a.task_id||':assistant')
                UNION SELECT d.task_id FROM dependency d JOIN affected a ON d.kind='dialogue_turn' AND d.object_id IN (a.task_id,a.task_id||':user',a.task_id||':assistant')
                UNION SELECT i.task_id FROM conversation_item_source s JOIN conversation_item i ON i.id=s.item_id JOIN affected a ON s.source_kind='dialogue_turn' AND s.source_id IN (a.task_id,a.task_id||':user',a.task_id||':assistant') WHERE i.task_id IS NOT NULL
                ) SELECT task_id FROM affected""", (identity,)))
            pending_tasks: list[str] = []
            for task_id in ids:
                state = conn.execute("SELECT status FROM task WHERE id=?", (task_id,)).fetchone()
                if state is not None and state[0] in {"Planning", "Running"}:
                    pending_tasks.append(task_id)
                conn.execute("UPDATE task SET cancel_requested=1 WHERE id=? AND status NOT IN ('Completed','Failed','Cancelled')", (task_id,))
                scrub(task_id)
            conn.execute("UPDATE conversation_item SET body='',redacted_at=?,revision=revision+1 WHERE conversation_id=? AND redacted_at IS NULL", (now, identity))
            conn.execute("UPDATE desktop_reminder_inbox SET label='',delivery_state='redacted' WHERE conversation_id=?", (identity,))
            conn.execute("UPDATE desktop_resource_link SET conversation_id=NULL WHERE conversation_id=?", (identity,))
            conn.execute("INSERT INTO conversation_cleanup VALUES(?,?,?,?,?,?,?)", (job_id, identity, operation, "Pending" if pending_tasks else "Completed", len(ids), now, canonical(pending_tasks)))
            self._receipt(conn, command_id, operation, payload, identity, job_id)
            return self.cleanup_status(job_id)

    def cleanup_status(self, job_id: str) -> dict[str, Any]:
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM conversation_cleanup WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            active = any(conn.execute("SELECT 1 FROM task WHERE id=? AND status IN ('Planning','Running')", (task_id,)).fetchone() is not None for task_id in json.loads(row["pending_tasks"]))
            status = "Pending" if active else "Completed"
            conn.execute("UPDATE conversation_cleanup SET status=? WHERE id=?", (status, job_id))
            conversation = conn.execute("SELECT revision,context_epoch FROM conversation WHERE id=?", (row["conversation_id"],)).fetchone()
            assert conversation is not None
            return {"accepted": True, "job_id": job_id, "status": status, "conversation_id": row["conversation_id"],
                    "revision": conversation["revision"], "context_epoch": conversation["context_epoch"], "epoch": conversation["context_epoch"],
                    "affected_task_count": row["affected_count"],
                    "cleared": ["conversation content", "dependent task projections and context copies", "linked reminder projections"],
                    "retained": ["independent long-term memory", "already admitted requests and started external effects", "historical logs, backups, exports and external copies"], **self._meta(conn)}
