"""Qt-free, JSON-only desktop application boundary.

Calls are synchronous local operations and belong on the desktop bridge workers.
No operation waits for a task or carries an executable authorization object.
"""

from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from deskpal.memory.contracts import MemoryStatus, UsePolicy
from deskpal.proactive.models import ProposalStatus
from deskpal.workflow.models import WorkflowBlocked
from deskpal.workflow.store import canonical

from .service import ApplicationService


def json_value(value: Any) -> Any:
    """Produce detached primitives, rejecting unsupported or non-finite values."""
    if isinstance(value, Enum):
        return json_value(value.value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (datetime, Path)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return json_value(asdict(value))
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("DTO keys must be strings")
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    raise TypeError("Unsupported desktop DTO value")


class DesktopFacade:
    """Narrow command/query surface; each response includes session and memory epoch."""

    def __init__(self, app: ApplicationService) -> None:
        self.app = app
        self.session_id = str(uuid4())
        self._memory_lock = RLock()
        # Fingerprints and erased metadata only: no independent body cache.
        self._memory_commands: dict[str, tuple[str, int]] = {}

    @staticmethod
    def _page(parameters: dict[str, Any]) -> tuple[int, int]:
        offset, limit = parameters.pop("offset", 0), parameters.pop("limit", 50)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100 and offset non-negative")
        return offset, limit

    @staticmethod
    def _command(parameters: Mapping[str, Any]) -> str:
        command_id = parameters.get("command_id")
        if not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 200:
            raise ValueError("A stable command_id is required")
        return command_id

    @staticmethod
    def _receipt(task: Any) -> dict[str, Any]:
        return {"accepted": True, "task_id": task.id, "status": task.status.value, "revision": task.revision}

    @staticmethod
    def _listed(fetch: Callable[..., Any], parameters: dict[str, Any]) -> dict[str, Any]:
        offset, limit = DesktopFacade._page(parameters)
        items = fetch(offset=offset, limit=limit, **parameters)
        # Probe one key/page through the public bounded query, never materialize all.
        more = bool(fetch(offset=offset + limit, limit=1, **parameters))
        return {"items": items, "offset": offset, "has_more": more}

    def execute(self, operation: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(operation, str) or not isinstance(parameters, Mapping):
            raise ValueError("Invalid desktop request")
        # Round trip also rejects NaN and copies every incoming mutable container.
        args: dict[str, Any] = json.loads(json.dumps(json_value(parameters), allow_nan=False))
        with self._memory_lock if operation in {"memory", "memories"} else nullcontext():
            return self._respond(operation, args)

    def _respond(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        if operation not in {"diagnostics", "settings", "retry_cleanup", "request_forget", "cleanup_status"}:
            self.app._ready()
        before = self.app.core.memory_runtime
        assert before is not None
        epoch = before.epoch
        value = self._execute(operation, args)
        result = json_value(value)
        if not isinstance(result, dict):
            result = {"value": result}
        result["session_id"] = self.session_id
        # Read DTOs retain their read epoch; a later mutation cannot label old
        # content with the newer epoch. Memory pages provide their atomic epoch.
        mutating = operation in {"memory", "retry_cleanup", "request_forget"}
        result["memory_epoch"] = (result.pop("epoch", before.epoch if mutating else epoch)
                                  if operation in {"memory", "memories"}
                                  else before.epoch if mutating else epoch)
        return dict(json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False)))

    def _execute(self, operation: str, args: dict[str, Any]) -> Any:
        app = self.app
        if operation == "conversation_open":
            args.pop("command_id", None)
            if args:
                raise ValueError("Conversation open takes no parameters")
            return app.conversation_open()
        if operation == "conversation_close":
            args.pop("command_id", None)
            return app.conversation_close(**args)
        if operation == "conversation_snapshot":
            return app.conversation_snapshot(**args)
        if operation == "interaction_snapshot":
            if args:
                raise ValueError("Interaction snapshot takes no parameters")
            return app.interaction_snapshot()
        if operation == "interaction_command":
            self._command(args)
            action = args.pop("action", None)
            if not isinstance(action, str):
                raise ValueError("Interaction command requires an action")
            return app.interaction_command(action, **args)
        if operation == "request_forget":
            self._command(args)
            return app.request_forget(**args)
        if operation == "cleanup_status":
            if args:
                raise ValueError("Cleanup status takes no parameters")
            return app.cleanup_status()
        if operation == "companion":
            if args:
                raise ValueError("Companion query does not accept parameters")
            return app.companion_snapshot()
        if operation == "companion_command":
            action = args.pop("action", None)
            if not isinstance(action, str):
                raise ValueError("Companion command requires an action")
            return app.companion_command(action, **args)
        if operation == "presentation":
            if args:
                raise ValueError("Presentation query does not accept parameters")
            return app.presentation_snapshot()
        if operation == "tasks":
            offset, limit = self._page(args)
            return app.tasks.page_summaries(offset=offset, limit=limit, **args)
        if operation == "task":
            return app.review_task(**args)
        if operation == "submit":
            self._command(args)
            if set(args) - {"request", "command_id", "use_policy", "conversation_id", "expected_conversation_epoch"}:
                raise ValueError("Unsupported submission parameter")
            return self._receipt(app.submit(**args))
        if operation == "confirm":
            self._command(args)
            if not isinstance(args.get("plan_hash"), str) or not args["plan_hash"]:
                raise ValueError("Explicit plan hash is required")
            ordinary, high = args.pop("ordinary_tools", []), args.pop("high_risk_tools", [])
            if not isinstance(ordinary, list) or not isinstance(high, list):
                raise ValueError("Tool grants must be explicit lists")
            return self._receipt(app.enqueue_confirmation(**args, authorized_tools=ordinary,
                high_risk_authorized_tools=high))
        if operation == "resume":
            args.pop("command_id", None)
            return app.resume(**args)
        if operation == "cancel":
            args.pop("command_id", None)
            return app.cancel_task(**args)
        if operation == "review_uncertain":
            args.pop("command_id", None)
            with app._dispatcher.management():
                return app.review_uncertain(**args)
        if operation == "memories":
            offset, limit = self._page(args)
            if "status" in args and args["status"] is not None:
                args["status"] = MemoryStatus(args["status"])
            runtime = app.core.memory_runtime
            assert runtime is not None
            return runtime.page_items(offset=offset, limit=limit, **args)
        if operation == "memory":
            return self._memory(args)
        if operation == "diagnostics":
            return app.diagnostics()
        if operation == "settings":
            config = app.config
            return {"provider": config.llm.provider, "model": config.llm.model,
                "memory_mode": config.memory.mode, "embedding_provider": config.memory.embedding_provider,
                "workspace_dir": str(config.sandbox.workspace_dir), "data_dir": str(config.storage.data_dir),
                "queue_capacity": config.workflow.queue_capacity, "timezone": config.proactive.timezone,
                "quiet_start": config.proactive.quiet_start, "quiet_end": config.proactive.quiet_end,
                "proactive_enabled": config.proactive.enabled, "restart_required": True,
                "configuration_source": "effective startup configuration; environment > .env > TOML > defaults"}
        if operation == "retry_cleanup":
            return {"complete": app.retry_cleanup()}
        if operation == "goals":
            return self._listed(app.goals.list, args)
        if operation == "goal":
            return app.goals.get(**args)
        if operation == "milestones":
            return self._listed(app.goals.list_milestones, args)
        if operation == "bindings":
            return self._listed(app.goals.list_bindings, args)
        if operation == "proposals":
            if args.get("status") == "all":
                args["status"] = None
            return self._listed(app.proposals.list, args)
        if operation == "proposal":
            return app.proposals.preflight(**args)
        if operation == "accept_proposal":
            self._command(args)
            return self._receipt(app.accept_proposal(**args))
        if operation == "notification_feed":
            return self._notification_feed(args)
        if operation == "notification_validate":
            selected = args.pop("items")
            if args or not isinstance(selected, list) or len(selected) > 20:
                raise ValueError("Notification validation is bounded to 20 identifiers")
            items = []
            for item in selected:
                if not isinstance(item, dict) or set(item) != {"kind", "entity_id"}:
                    raise ValueError("Invalid notification identifier")
                current = self._notification_item(item["kind"], item["entity_id"])
                if current is not None:
                    items.append(current)
            return {"items": items}
        writes: dict[str, Callable[..., Any]] = {
            "create_goal": app.goals.create, "update_goal": app.goals.revise,
            "goal_status": app.goals.set_status, "create_milestone": app.goals.add_milestone,
            "update_milestone": app.goals.revise_milestone, "milestone_status": app.goals.set_milestone_status,
            "bind": app.goals.bind_source, "update_binding": app.goals.revise_binding,
            "ignore_proposal": app.proposals.ignore,
        }
        if operation in writes:
            self._command(args)
            with app._dispatcher.management():
                return writes[operation](**args)
        raise ValueError("Unknown desktop operation")

    def _memory(self, args: dict[str, Any]) -> Any:
        command_id = self._command(args)
        args.pop("command_id")
        action = args.pop("action")
        if action not in {"remember", "accept", "reject", "correct", "forget"}:
            raise ValueError("Unsupported desktop memory action")
        fingerprint = sha256(canonical({"action": action, **args}).encode()).hexdigest()
        runtime = self.app.core.memory_runtime
        assert runtime is not None
        with self._memory_lock:
            previous = self._memory_commands.get(command_id)
            if previous is not None:
                if previous[0] != fingerprint:
                    raise ValueError("Command ID already used with a different payload")
                # Replays never re-emit forgotten or superseded bodies. The
                # original completion metadata remains safe after any epoch.
                return {"replayed": True, "accepted": True, "epoch": runtime.epoch}
            if len(self._memory_commands) >= 4096:
                raise WorkflowBlocked("Desktop memory command capacity reached; restart the session")
            if "use_policy" in args:
                args["use_policy"] = UsePolicy(args["use_policy"])
            value = self.app.memory_command(action, **args)
            self._memory_commands[command_id] = (fingerprint, runtime.epoch)
            return value

    def _notification_feed(self, args: dict[str, Any]) -> dict[str, Any]:
        after = args.pop("after_sequence", None)
        until = args.pop("until_sequence", None)
        limit = args.pop("limit", 100)
        if args:
            raise ValueError("Unsupported notification feed parameter")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Notification limit must be 1..100")
        if any(value is not None and (type(value) is not int or value < 0) for value in (after, until)):
            raise ValueError("Notification sequences must be non-negative integers")
        # Project a bounded audit range onto *current* public state. Audit
        # payloads and private task bodies never cross this boundary.
        with self.app.store.transaction() as db:
            maximum = int(db.execute("SELECT COALESCE(MAX(sequence),0) FROM audit_outbox").fetchone()[0])
            if after is None:
                return {"items": [], "next_sequence": maximum, "until_sequence": maximum,
                    "has_more": False, "baseline": True}
            upper = maximum if until is None else min(until, maximum)
            if after > upper:
                raise ValueError("Notification cursor exceeds its upper bound")
            rows = db.execute("SELECT sequence,event,task_id,json_extract(data,'$.id') AS proposal_id "
                "FROM audit_outbox WHERE sequence>? AND sequence<=? ORDER BY sequence LIMIT ?",
                (after, upper, limit)).fetchall()
            next_sequence = int(rows[-1]["sequence"]) if rows else upper
            more = db.execute("SELECT 1 FROM audit_outbox WHERE sequence>? AND sequence<=? LIMIT 1",
                (next_sequence, upper)).fetchone() is not None
        items = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            candidates = []
            if row["task_id"]:
                candidates.append(("task", str(row["task_id"])))
            if row["event"] in {"proposal_created", "proposal_changed"} and row["proposal_id"]:
                candidates.append(("proposal", str(row["proposal_id"])))
            for kind, entity_id in candidates:
                if (kind, entity_id) in seen:
                    continue
                seen.add((kind, entity_id))
                current = self._notification_item(kind, entity_id)
                if current is not None:
                    items.append(current)
        return {"items": items, "next_sequence": next_sequence, "until_sequence": upper,
            "has_more": more, "baseline": False}

    def _notification_item(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        if not isinstance(entity_id, str) or not entity_id:
            raise ValueError("Notification entity_id must be non-empty")
        if kind == "task":
            with self.app.store.transaction() as db:
                row = db.execute("SELECT id,revision,status FROM task WHERE id=? AND scrubbed=0 "
                    "AND status IN ('Waiting','Completed','Failed','Cancelled')", (entity_id,)).fetchone()
                if row is None:
                    return None
                return {"kind": kind, "entity_id": entity_id, "revision": row["revision"], "status": row["status"]}
        if kind == "proposal":
            try:
                proposal = self.app.proposals.preflight(entity_id)
            except (ValueError, WorkflowBlocked):
                return None
            if proposal.status == ProposalStatus.PENDING:
                return {"kind": kind, "entity_id": entity_id, "revision": proposal.revision,
                    "status": proposal.status.value}
            return None
        raise ValueError("Unknown notification kind")
