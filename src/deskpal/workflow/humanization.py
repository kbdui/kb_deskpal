"""Durable turn, privacy, call-budget and effect ports for the application."""

from collections.abc import Sequence
import json
from typing import Any
from uuid import uuid4

from .models import WorkflowBlocked
from .store import WorkflowStore, canonical
from .tasks import dependency_ref


_PURPOSES = frozenset({"planning", "expression", "memory_evaluation"})


class HumanizationStore:
    def __init__(self, store: WorkflowStore) -> None:
        self.store = store

    def privacy(self) -> dict[str, Any]:
        with self.store.read_connection() as conn:
            row = conn.execute("SELECT epoch,blocked FROM privacy_control WHERE id=1").fetchone()
            assert row is not None
            return {"epoch": int(row["epoch"]), "blocked": bool(row["blocked"])}

    def block_privacy(self) -> int:
        with self.store.transaction() as conn:
            conn.execute("UPDATE privacy_control SET epoch=epoch+1,blocked=1 WHERE id=1")
            return int(conn.execute("SELECT epoch FROM privacy_control WHERE id=1").fetchone()[0])

    def unblock_privacy(self) -> None:
        with self.store.transaction() as conn:
            if conn.execute("SELECT 1 FROM deletionjob WHERE status!='Completed'").fetchone():
                raise WorkflowBlocked("Privacy cleanup still has pending deletion jobs")
            conn.execute("UPDATE privacy_control SET blocked=0 WHERE id=1")

    def bind_turn(self, task_id: str, conversation_id: str, epoch: int, sequence: int) -> None:
        if not task_id or not conversation_id or len(conversation_id) > 200:
            raise ValueError("A turn requires bounded task and conversation identities")
        if type(epoch) is not int or epoch < 0 or type(sequence) is not int or sequence < 1:
            raise ValueError("Invalid conversation epoch or turn sequence")
        with self.store.transaction() as conn:
            prior = conn.execute("SELECT * FROM turn_ref WHERE task_id=?", (task_id,)).fetchone()
            if prior is not None:
                if (prior["conversation_id"], prior["epoch"], prior["sequence"]) != (conversation_id, epoch, sequence):
                    raise WorkflowBlocked("Task is already bound to a different turn")
                return
            self.store.check_privacy(conn)
            privacy_epoch = conn.execute("SELECT epoch FROM privacy_control WHERE id=1").fetchone()[0]
            conn.execute("INSERT INTO turn_ref VALUES(?,?,?,?,?)", (task_id, conversation_id, epoch, sequence, privacy_epoch))
            # The task ID is the host-generated stable source identity. Content/use
            # changes invalidate this source, rather than a presentation read.
            row = conn.execute("SELECT use_policy FROM task WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            self.store.set_dependency_version("dialogue_turn", task_id, 1, use_policy=row[0])

    def turn(self, task_id: str) -> dict[str, Any] | None:
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM turn_ref WHERE task_id=?", (task_id,)).fetchone()
            return None if row is None else dict(row)

    def reference_policy(self, kind: str, object_id: str, revision: int) -> str | None:
        """Return only a live workflow source's policy at its exact revision.

        Memory remains an independent authority and is resolved by its runtime.
        A missing, inactive or changed source conveys no usable permission.
        """
        if kind == "memory":
            return None
        with self.store.transaction() as conn:
            row = conn.execute("SELECT use_policy FROM dependency_version WHERE kind=? AND object_id=? AND revision=? AND active=1",
                               (kind, object_id, revision)).fetchone()
            if row is None or row[0] not in {"local_only", "cloud_allowed"}:
                return None
            return str(row[0])

    def task_policy(self, task_id: str) -> str:
        with self.store.transaction() as conn:
            row = conn.execute("SELECT use_policy FROM task WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            policy = str(row[0])
            if policy not in {"local_only", "cloud_allowed"}:
                raise WorkflowBlocked("Task has an unsupported use policy")
            return policy

    def tighten_policy(self, task_id: str) -> None:
        """Source restrictions may narrow a new intent, never widen it."""
        with self.store.transaction() as conn:
            row = conn.execute("SELECT use_policy FROM task WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row[0] == "local_only":
                return
            if conn.execute("SELECT 1 FROM immutableplan WHERE task_id=?", (task_id,)).fetchone():
                raise WorkflowBlocked("A persisted plan cannot change its source policy")
            conn.execute("UPDATE task SET use_policy='local_only' WHERE id=?", (task_id,))
            conn.execute("UPDATE dependency_version SET use_policy='local_only' WHERE kind='dialogue_turn' AND object_id IN (?,?,?)",
                         (task_id, task_id + ":user", task_id + ":assistant"))

    def save_context(self, task_id: str, digest: str, items: Sequence[Any],
                     refs: Sequence[tuple[str, str, int]], purpose: str) -> None:
        if purpose not in {"planning", "expression"}:
            raise ValueError("Context purpose must be planning or expression")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Context digest must be a SHA-256 hexadecimal string")
        sources = tuple(sorted(set(dependency_ref(ref) for ref in refs), key=lambda r: (r.kind, r.object_id, r.revision)))
        if len(sources) > 512 or len(items) > 32:
            raise ValueError("Context exceeds source or item limits")
        identities = {(ref.kind, ref.object_id) for ref in sources}
        if len(identities) != len(sources):
            raise ValueError("Context contains conflicting source revisions")
        # Expression history remains memory-only. Its deletion index is durable.
        body = canonical(list(items) if purpose == "planning" else [])
        if len(body.encode("utf-8")) > self.store.plan_max_bytes:
            raise ValueError("Context snapshot exceeds byte limit")
        with self.store.transaction() as conn:
            self.store.check_dependencies(conn, task_id)
            prior = conn.execute("SELECT digest,items FROM context_snapshot WHERE task_id=? AND purpose=?", (task_id, purpose)).fetchone()
            if prior is not None:
                old_refs = tuple((r[0], r[1], r[2]) for r in conn.execute("SELECT kind,object_id,revision FROM context_dependency WHERE task_id=? AND purpose=? ORDER BY kind,object_id,revision", (task_id, purpose)))
                if prior["digest"] != digest or prior["items"] != body or old_refs != tuple((r.kind, r.object_id, r.revision) for r in sources):
                    raise WorkflowBlocked("Frozen context cannot be replaced")
                return
            if purpose == "planning" and conn.execute("SELECT 1 FROM immutableplan WHERE task_id=?", (task_id,)).fetchone():
                raise WorkflowBlocked("Planning context cannot change after plan persistence")
            conn.execute("INSERT INTO context_snapshot VALUES(?,?,?,?)", (task_id, purpose, digest, body))
            for ref in sources:
                conn.execute("INSERT INTO context_dependency VALUES(?,?,?,?,?)", (task_id, purpose, ref.kind, ref.object_id, ref.revision))
                if purpose == "planning":
                    old = conn.execute("SELECT revision FROM dependency WHERE task_id=? AND kind=? AND object_id=?", (task_id, ref.kind, ref.object_id)).fetchone()
                    if old is not None and old[0] != ref.revision:
                        raise WorkflowBlocked("Task source revision cannot be replaced")
                    conn.execute("INSERT OR IGNORE INTO dependency VALUES(?,?,?,?)", (task_id, ref.kind, ref.object_id, ref.revision))
            self.store.check_context_dependencies(conn, task_id, purpose)

    def context(self, task_id: str, purpose: str = "planning") -> dict[str, Any] | None:
        with self.store.transaction() as conn:
            row = conn.execute("SELECT digest,items FROM context_snapshot WHERE task_id=? AND purpose=?", (task_id, purpose)).fetchone()
            if row is None:
                return None
            refs = tuple((r[0], r[1], r[2]) for r in conn.execute("SELECT kind,object_id,revision FROM context_dependency WHERE task_id=? AND purpose=? ORDER BY kind,object_id", (task_id, purpose)))
            return {"digest": row["digest"], "items": json.loads(row["items"]), "refs": refs, "purpose": purpose}

    def check(self, task_id: str, purpose: str = "expression") -> None:
        """No external store is called while holding the workflow write lock."""
        self.store.validate_external_context(task_id, purpose)
        with self.store.transaction() as conn:
            self.store.check_dependencies(conn, task_id)
            self.store.check_context_dependencies(conn, task_id, purpose)

    def pending_effects(self, limit: int = 100) -> tuple[dict[str, Any], ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Effect page size must be 1..100")
        with self.store.transaction() as conn:
            return tuple(dict(row) for row in conn.execute("SELECT id,task_id,status,success,failure,epoch,terminal_revision,created_at,appraisal_version FROM companion_effect_outbox ORDER BY sequence LIMIT ?", (limit,)))

    def ack_effect(self, effect_id: str) -> None:
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM companion_effect_outbox WHERE id=?", (effect_id,))

    def discard_effects(self) -> None:
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM companion_effect_outbox")

    def reserve_call(self, task_id: str, purpose: str, limit: int = 1) -> str:
        if purpose not in _PURPOSES or type(limit) is not int or not 1 <= limit <= 1:
            raise ValueError("A model purpose has one attempt per root task")
        with self.store.transaction() as conn:
            self.store.check_dependencies(conn, task_id)
            self.store.check_context_dependencies(conn, task_id, purpose)
            count = int(conn.execute("SELECT COUNT(*) FROM task_call_attempt WHERE task_id=? AND purpose=?", (task_id, purpose)).fetchone()[0])
            if count >= limit:
                raise WorkflowBlocked("Model call budget exhausted")
            call_id = str(uuid4())
            conn.execute("INSERT INTO task_call_attempt VALUES(?,?,?,?,?)", (call_id, task_id, purpose, count + 1, "reserved"))
            return call_id

    def finish_call(self, call_id: str, status: str) -> None:
        if status not in {"finished", "unknown"}:
            raise ValueError("Model outcome must be finished or unknown")
        with self.store.transaction() as conn:
            row = conn.execute("SELECT status FROM task_call_attempt WHERE id=?", (call_id,)).fetchone()
            if row is None:
                raise KeyError(call_id)
            if row[0] != "reserved":
                if row[0] != status:
                    raise WorkflowBlocked("Model outcome cannot be replaced")
                return
            conn.execute("UPDATE task_call_attempt SET status=? WHERE id=?", (status, call_id))
