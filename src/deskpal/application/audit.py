"""Durable metadata-only audit; delivery errors never replay execution."""

from collections.abc import Mapping
from typing import Any

from deskpal.execution_log import ExecutionLog
from deskpal.workflow.store import WorkflowStore


class WorkflowExecutionLog:
    _fields = frozenset({"previous_status", "status", "stage", "tool_name", "call_id", "success",
                         "memory_id", "kind", "action", "permission_level", "allowed"})

    def __init__(self, store: WorkflowStore) -> None:
        self.store = store

    def record(self, event: str, task_id: str | None = None,
               data: Mapping[str, Any] | None = None) -> None:
        # Requests, arguments, output, and model text belong to the dependency-
        # indexed workflow records. Audit is deliberately only structured metadata.
        metadata = {key: value for key, value in (data or {}).items()
                    if key in self._fields and type(value) in (str, bool, int, type(None))}
        self.store.record_event(event, task_id, metadata)


def deliver_audit(store: WorkflowStore, sink: ExecutionLog, *, limit: int = 100) -> int:
    delivered = 0
    for item in store.pending_audit(limit):
        try:
            sink.record(item.event, item.task_id, {"event_id": item.id, **item.data})
            store.mark_audit_delivered(item.id)
        except Exception:
            # The same event_id may appear twice if the append succeeded but its
            # acknowledgement did not. The outbox never contains a tool command.
            break
        delivered += 1
    return delivered
