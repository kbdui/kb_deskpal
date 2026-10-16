"""Explicit local workflow commands, separate from model-generated requests."""

import argparse
import re
from dataclasses import asdict, is_dataclass
from typing import Any
from uuid import uuid4

from deskpal.agent.models import AgentResult
from deskpal.tasks.models import Task, TaskStatus
from deskpal.proactive.models import ProposalConflict, ProposalStatus

from .service import ApplicationService


ACTIONS = ("tasks", "task", "submit", "resume", "confirm", "cancel", "review-uncertain",
           "goals", "goal", "goal-create", "goal-update", "goal-status", "milestones",
           "milestone-create", "milestone-update", "milestone-status", "bindings", "bind",
           "binding-update", "proposals", "proposal", "accept-proposal", "ignore-proposal",
           "poll", "diagnostics", "retry-cleanup", "backup", "source-identity", "review-consumer")
EXECUTION_ACTIONS = frozenset({"submit", "resume", "confirm", "accept-proposal"})


def add_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("V0.4 persistent workflow")
    group.add_argument("--id", help="Task, Goal, Milestone, Binding or Proposal identifier")
    group.add_argument("--goal-id", help="Parent goal identifier")
    group.add_argument("--milestone-id", help="Optional milestone scope for a source binding")
    group.add_argument("--expected-revision", type=int, help="CAS revision for a workflow write")
    group.add_argument("--command-id", help="Stable idempotency key; defaults to a new UUID")
    group.add_argument("--title")
    group.add_argument("--criteria", help="Explicit success criteria")
    group.add_argument("--deadline", help="ISO-8601 timestamp with explicit UTC offset")
    group.add_argument("--clear-deadline", action="store_true")
    group.add_argument("--progress-note")
    group.add_argument("--evidence", help="Human completion or uncertain-outcome review evidence")
    group.add_argument("--state", help="Goal, milestone, binding or proposal state")
    group.add_argument("--use-policy", choices=("local_only", "cloud_allowed"))
    group.add_argument("--source", choices=("calendar", "timer", "task"))
    group.add_argument("--source-id")
    group.add_argument("--text", help="Request text for workflow submit")
    group.add_argument("--plan-hash", help="Hash of the exact persisted plan being confirmed")
    group.add_argument("--verdict", choices=("occurred", "not_occurred", "unknown"))
    group.add_argument("--limit", type=int, default=100, help="Page size, 1..100")
    group.add_argument("--offset", type=int, default=0)
    group.add_argument("--backup-path", help="New explicit workflow SQLite backup destination")
    group.add_argument("--stream", choices=("timer", "calendar"), help="Scheduling event stream")
    group.add_argument("--expected-store-id", help="Explicitly reviewed current scheduling store identity")
    group.add_argument("--expected-generation", help="Explicitly reviewed current scheduling generation")
    group.add_argument("--strategy", choices=("tail", "bounded_history"), help="Consumer review policy; default tail")
    group.add_argument("--after-id", type=int, help="Selected stream event ID or zero, for bounded history")
    group.add_argument("--max-events", type=int, help="Explicit bounded history receipt budget, 1..10000")


def validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not 1 <= args.limit <= 100 or args.offset < 0:
        parser.error("Workflow pagination requires limit 1..100 and non-negative offset")
    if args.workflow is None:
        options = (args.id, args.goal_id, args.milestone_id, args.expected_revision, args.command_id,
                   args.title, args.criteria, args.deadline, args.progress_note, args.evidence,
                   args.state, args.use_policy, args.source, args.source_id, args.text,
                   args.plan_hash, args.verdict, args.backup_path, args.clear_deadline,
                   args.stream, args.expected_store_id, args.expected_generation, args.strategy,
                   args.after_id, args.max_events)
        if any(value is not None and value is not False for value in options):
            parser.error("Workflow options require --workflow")
        return
    action = args.workflow
    id_actions = {"task", "resume", "confirm", "cancel", "review-uncertain", "goal", "goal-update",
                  "goal-status", "milestone-update", "milestone-status", "binding-update", "proposal",
                  "accept-proposal", "ignore-proposal"}
    if action in id_actions and not args.id:
        parser.error("This workflow command requires --id")
    cas_actions = {"goal-update", "goal-status", "milestone-create", "milestone-update",
                   "milestone-status", "bind", "binding-update", "accept-proposal", "ignore-proposal"}
    if action in cas_actions and (args.expected_revision is None or args.expected_revision < 1):
        parser.error("This workflow write requires a positive --expected-revision")
    if action in {"milestones", "milestone-create", "bindings", "bind"} and not args.goal_id:
        parser.error("This workflow command requires --goal-id")
    if action in {"goal-create", "milestone-create"} and (not args.title or not args.criteria):
        parser.error("Creation requires --title and --criteria")
    if action in {"goal-status", "milestone-status"} and not args.state:
        parser.error("State transition requires --state")
    if action == "bind" and (not args.source or not args.source_id):
        parser.error("Binding requires --source and --source-id")
    if action == "submit" and not args.text:
        parser.error("Submission requires --text")
    if action == "confirm" and not args.plan_hash:
        parser.error("Cross-process confirmation requires --plan-hash")
    if action == "review-uncertain" and (not args.verdict or not args.evidence):
        parser.error("Uncertain review requires --verdict and --evidence")
    if action == "backup" and not args.backup_path:
        parser.error("Backup requires --backup-path")
    if args.deadline is not None and args.clear_deadline:
        parser.error("Choose --deadline or --clear-deadline")
    consumer_options = (args.stream, args.expected_store_id, args.expected_generation,
                        args.strategy, args.after_id, args.max_events)
    if action not in {"review-consumer", "source-identity"} and any(value is not None for value in consumer_options):
        parser.error("Consumer options require --workflow review-consumer or source-identity")
    if action == "source-identity" and any(value is not None for value in consumer_options[1:]):
        parser.error("Source identity accepts only the optional --stream filter")
    if action == "review-consumer":
        if not args.stream or not (args.expected_store_id or "").strip() or not (args.expected_generation or "").strip():
            parser.error("Consumer review requires --stream, --expected-store-id and --expected-generation")
        if (args.strategy or "tail") == "bounded_history":
            if args.after_id is None or not 0 <= args.after_id <= 2**63 - 1 or args.max_events is None or not 1 <= args.max_events <= 10000:
                parser.error("Bounded history requires --after-id 0..SQLite maximum and --max-events 1..10000")
        elif args.after_id is not None or args.max_events is not None:
            parser.error("Tail review does not accept --after-id or --max-events")
    if args.plan_hash is not None and not re.fullmatch(r"[a-f0-9]{64}", args.plan_hash):
        parser.error("--plan-hash must be the exact 64-character lowercase SHA-256 hash")
    if args.command_id is not None and (not args.command_id.strip() or len(args.command_id.encode("utf-8")) > 200):
        parser.error("--command-id must be non-empty and at most 200 UTF-8 bytes")
    states = {"goals": {"Active", "Paused", "Completed", "Cancelled"},
              "goal-status": {"Active", "Paused", "Completed", "Cancelled"},
              "milestone-status": {"Pending", "Completed", "Cancelled"},
              "binding-update": {"Active", "Removed"},
              "proposals": {"Pending", "Accepted", "Ignored", "Expired", "Invalidated", "all"}}
    if args.state is not None and action in states and args.state not in states[action]:
        parser.error("--state is not valid for this workflow action")
    if action in {"goal-status", "milestone-status"} and args.state == "Completed" and not (args.evidence or "").strip():
        parser.error("Completion requires explicit user --evidence")
    if action in {"goal-update", "milestone-update"} and not any(value is not None for value in
            (args.title, args.criteria, args.deadline, args.progress_note,
             args.use_policy if action == "goal-update" else None)) and not args.clear_deadline:
        parser.error("Update requires a changed field")
    if action == "binding-update" and args.state is None and args.use_policy is None:
        parser.error("Binding update requires --state or --use-policy")
    allowed = {
        "id": id_actions, "goal_id": {"milestones", "milestone-create", "bindings", "bind"},
        "milestone_id": {"bind"}, "expected_revision": cas_actions,
        "command_id": cas_actions | {"goal-create", "submit", "review-consumer"},
        "title": {"goal-create", "goal-update", "milestone-create", "milestone-update"},
        "criteria": {"goal-create", "goal-update", "milestone-create", "milestone-update"},
        "deadline": {"goal-create", "goal-update", "milestone-create", "milestone-update"},
        "progress_note": {"goal-update", "milestone-update"},
        "evidence": {"goal-status", "milestone-status", "review-uncertain"},
        "state": set(states), "use_policy": {"goal-create", "goal-update", "bind", "binding-update", "submit"},
        "source": {"bind"}, "source_id": {"bind"}, "text": {"submit"},
        "plan_hash": {"confirm"}, "verdict": {"review-uncertain"}, "backup_path": {"backup"},
    }
    for option, actions in allowed.items():
        if getattr(args, option) is not None and action not in actions:
            parser.error(f"--{option.replace('_', '-')} does not apply to this workflow action")
    if args.clear_deadline and action not in {"goal-update", "milestone-update"}:
        parser.error("--clear-deadline applies only to goal/milestone update")


def execute(app: ApplicationService, args: argparse.Namespace) -> Any:
    action = args.workflow
    if action == "diagnostics":
        return {**app.diagnostics(), "consumers": app.rules.consumer_status(), "inbox": app.rules.inbox_status()}
    if action == "retry-cleanup":
        return {"cleanup_complete": app.retry_cleanup()}
    if app.diagnostics()["cleanup_pending"]:
        raise ValueError("Memory cleanup is pending; use --workflow retry-cleanup")
    cid = args.command_id or str(uuid4())
    if action == "source-identity":
        streams = (args.stream,) if args.stream is not None else ("timer", "calendar")
        return {"identity": app.scheduling.store_identity,
                "streams": tuple(app.scheduling.event_cursor_state(stream) for stream in streams),
                "consumers": tuple(item for item in app.rules.consumer_status() if item["stream"] in streams)}
    if action == "review-consumer":
        return app.rules.review_consumer(args.stream, expected_store_id=args.expected_store_id,
            expected_generation=args.expected_generation, strategy=args.strategy or "tail",
            after_id=args.after_id, max_events=args.max_events, command_id=cid)
    if action == "tasks":
        return app.tasks.list_tasks(limit=args.limit, offset=args.offset)
    if action == "task":
        return app.review_task(args.id)
    if action == "submit":
        task = app.submit(args.text, command_id=cid, authorized_tools=args.authorize,
                          high_risk_authorized_tools=args.authorize_high_risk,
                          use_policy=args.use_policy or "cloud_allowed")
        return app.wait(task.id)
    if action == "resume":
        result = app.resume(args.id)
        return app.wait(result.id) if isinstance(result, Task) else result
    if action == "confirm":
        return app.confirm_task(args.id, authorized_tools=args.authorize,
            high_risk_authorized_tools=args.authorize_high_risk, plan_hash=args.plan_hash)
    if action == "cancel":
        return app.cancel_task(args.id)
    if action == "review-uncertain":
        return app.review_uncertain(args.id, args.verdict, args.evidence)
    if action == "goals":
        return app.goals.list(status=args.state, limit=args.limit, offset=args.offset)
    if action == "goal":
        return app.goals.get(args.id)
    if action == "goal-create":
        return app.goals.create(args.title, args.criteria, deadline=args.deadline,
                                use_policy=args.use_policy or "local_only", command_id=cid)
    changes = {key: value for key, value in {"title": args.title, "success_criteria": args.criteria,
               "deadline": args.deadline, "progress_note": args.progress_note}.items() if value is not None}
    if args.clear_deadline:
        changes["deadline"] = None
    if action == "goal-update":
        if args.use_policy is not None:
            changes["use_policy"] = args.use_policy
        return app.goals.revise(args.id, args.expected_revision, command_id=cid, **changes)
    if action == "goal-status":
        return app.goals.set_status(args.id, args.expected_revision, args.state,
                                    evidence=args.evidence or "", command_id=cid)
    if action == "milestones":
        return app.goals.list_milestones(args.goal_id, limit=args.limit, offset=args.offset)
    if action == "milestone-create":
        return app.goals.add_milestone(args.goal_id, args.expected_revision, args.title, args.criteria,
                                       deadline=args.deadline, command_id=cid)
    if action == "milestone-update":
        return app.goals.revise_milestone(args.id, args.expected_revision, command_id=cid, **changes)
    if action == "milestone-status":
        return app.goals.set_milestone_status(args.id, args.expected_revision, args.state,
                                              evidence=args.evidence or "", command_id=cid)
    if action == "bindings":
        return app.goals.list_bindings(args.goal_id, limit=args.limit, offset=args.offset)
    if action == "bind":
        return app.goals.bind_source(args.goal_id, args.expected_revision, args.source, args.source_id,
            milestone_id=args.milestone_id, use_policy=args.use_policy or "local_only", command_id=cid)
    if action == "binding-update":
        return app.goals.revise_binding(args.id, args.expected_revision, use_policy=args.use_policy,
                                         status=args.state, command_id=cid)
    if action == "proposals":
        return app.proposals.list(status=None if args.state == "all" else args.state or "Pending",
                                  limit=args.limit, offset=args.offset)
    if action == "proposal":
        # Historical Ignored/Expired/Invalidated suggestions remain inspectable.
        item = app.proposals.get(args.id)
        if item.status == ProposalStatus.PENDING:
            try:
                return app.proposals.preflight(args.id)
            except ProposalConflict:
                return app.proposals.get(args.id)
        return item
    if action == "accept-proposal":
        task = app.accept_proposal(args.id, args.expected_revision, command_id=cid)
        return app.wait(task.id)
    if action == "ignore-proposal":
        return app.proposals.ignore(args.id, args.expected_revision, command_id=cid)
    if action == "poll":
        return {"created": app.rules.poll(), "proposals": app.proposal_summary()}
    if action == "backup":
        from pathlib import Path
        app.store.backup(Path(args.backup_path))
        return {"workflow_backup": str(Path(args.backup_path).resolve()),
                "scope": "workflow only; independent memory/scheduling restore needs separate coordination"}
    raise ValueError("Unknown workflow command")


def exit_code(value: Any) -> int:
    if isinstance(value, AgentResult):
        if value.status == TaskStatus.COMPLETED:
            return 0
        if value.status in {TaskStatus.WAITING, TaskStatus.NEEDS_REVIEW}:
            return 2
        return 1
    if is_dataclass(value) and not isinstance(value, type) and "status" in asdict(value):
        return 0
    return 0
