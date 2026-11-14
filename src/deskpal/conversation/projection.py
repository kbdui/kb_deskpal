"""Task state publication into an already bound conversation timeline."""

import json
import sqlite3
from typing import Any

from deskpal.workflow.models import WorkflowBlocked
from deskpal.workflow.store import WorkflowStore, canonical


def publish_task_state(store: WorkflowStore, conn: sqlite3.Connection, task_id: str,
                       status: str, payload: Any, error: str | None) -> None:
    """Runs in the task transition transaction; never synthesizes old history."""
    link = conn.execute("SELECT * FROM turn_ref WHERE task_id=?", (task_id,)).fetchone()
    if link is None or not conn.execute("SELECT 1 FROM conversation_turn WHERE task_id=?", (task_id,)).fetchone():
        return
    try:
        store.check_privacy(conn, task_id)
    except WorkflowBlocked:
        return
    task = conn.execute("SELECT use_policy,scrubbed FROM task WHERE id=?", (task_id,)).fetchone()
    assert task is not None
    if task["scrubbed"]:
        return
    conn.execute("UPDATE conversation_item SET revision=revision+1 WHERE task_id=? AND kind='task_card' AND redacted_at IS NULL", (task_id,))
    conn.execute("UPDATE conversation SET timeline_revision=timeline_revision+1 WHERE id=?", (link["conversation_id"],))
    data = payload if isinstance(payload, dict) else {}
    text = data.get("response", data.get("reply", ""))
    if not isinstance(text, str):
        text = ""
    if status == "Cancelled":
        completed = conn.execute("SELECT 1 FROM attempt WHERE task_id=? AND status='Succeeded' LIMIT 1", (task_id,)).fetchone()
        text = ("后续步骤已取消；此前已完成的操作仍然保留，可查看执行记录。" if completed
                else "请求已取消，没有已完成的工具操作。")
    if not text:
        text = {"Waiting": "这项安排需要你确认后继续。", "NeedsReview": "这项工作需要重新审阅。",
                "Uncertain": "执行结果需要人工核实。", "Failed": "这次请求未能完成，具体原因请查看执行详情。",
                "Cancelled": "已取消这次请求。"}.get(status, "")
    if not text and "memory_id" in data and status == "Completed":
        text = "已保存这条记忆。"
    if not text:
        return
    conn.execute("UPDATE conversation_item SET body=?,revision=revision+1 WHERE task_id=? AND kind='assistant_message' AND redacted_at IS NULL", (text, task_id))
    refs: dict[tuple[str, str], dict[str, Any]] = {}
    for row in conn.execute("SELECT kind,object_id,revision FROM dependency WHERE task_id=? UNION SELECT kind,object_id,revision FROM context_dependency WHERE task_id=?", (task_id, task_id)):
        version = conn.execute("SELECT use_policy FROM dependency_version WHERE kind=? AND object_id=? AND revision=? AND active=1", (row["kind"], row["object_id"], row["revision"])).fetchone()
        # Memory policy remains resolved externally at context admission. Keep
        # a restrictive policy here unless the entire task is cloud eligible.
        policy = str(version[0]) if version is not None else task["use_policy"]
        refs[(row["kind"], row["object_id"])] = {"kind": row["kind"], "id": row["object_id"], "revision": row["revision"], "use_policy": policy}
    origin = conn.execute("SELECT kind FROM task_origin WHERE task_id=?", (task_id,)).fetchone()
    for role in (("assistant",) if origin and origin[0] == "proactive" else ("user", "assistant")):
        source_id = task_id + ":" + role
        store.set_dependency_version("dialogue_turn", source_id, link["sequence"], use_policy=task["use_policy"])
        refs[("dialogue_turn", source_id)] = {"kind": "dialogue_turn", "id": source_id, "revision": link["sequence"], "use_policy": task["use_policy"]}
    frozen = list(refs.values())
    for item in conn.execute("SELECT id FROM conversation_item WHERE task_id=? AND redacted_at IS NULL", (task_id,)).fetchall():
        for ref in frozen:
            conn.execute("INSERT OR REPLACE INTO conversation_item_source VALUES(?,?,?,?,?)", (item[0], ref["kind"], ref["id"], ref["revision"], ref["use_policy"]))
    facts = []
    # Only the persisted execution ledger attests to a tool outcome.
    for attempt in conn.execute("SELECT result FROM attempt WHERE task_id=? AND status IN ('Succeeded','Failed') AND result IS NOT NULL ORDER BY step_index", (task_id,)):
        facts.append(canonical(json.loads(attempt[0])))
    # A mutable Waiting reply is displayable, but cannot become another turn's
    # evidence while its assistant source is still being replaced by a result.
    published = int(status in {"Completed", "Failed", "Cancelled"})
    conn.execute("UPDATE conversation_turn SET refs=?,facts=?,published=? WHERE task_id=?", (canonical(frozen), canonical(facts), published, task_id))
