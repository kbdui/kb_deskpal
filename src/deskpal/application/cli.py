"""Local backend CLI. Tool authorization is always explicit per invocation."""

import argparse
import json
import sys
from collections import deque
from collections.abc import Sequence
from dataclasses import asdict
from dataclasses import is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from deskpal.agent.models import AgentResult
from deskpal.config import ConfigError, load_config
from deskpal.llm.errors import LLMError
from deskpal._json import snapshot_json
from deskpal.planner.direct import DirectToolPlanner
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolCall
from deskpal.memory.contracts import MemoryStatus, UsePolicy, MemoryError

from .factory import create_application
from .service import ApplicationService
from . import workflow_cli


def _utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        configure = getattr(stream, "reconfigure", None)
        if callable(configure):
            configure(encoding="utf-8", errors="replace")


def _result_json(result: AgentResult) -> dict[str, Any]:
    data = asdict(result)
    data["status"] = result.status.value
    return data


def _display(result: AgentResult, *, json_output: bool) -> int:
    if json_output:
        print(json.dumps(_result_json(result), ensure_ascii=False, allow_nan=False))
    else:
        print(f"Task {result.task_id}: {result.status.value}")
        print(result.response)
        for tool_result in result.tool_results:
            print("Tool result: " + json.dumps(asdict(tool_result), ensure_ascii=False, allow_nan=False))
        for warning in result.warnings:
            print("Warning: " + warning, file=sys.stderr)
        if result.required_authorizations:
            print("Pending plan: " + json.dumps([asdict(call) for call in result.planned_calls],
                                               ensure_ascii=False, allow_nan=False))
            for item in result.authorization_requests:
                print(f"Permission {int(item.permission_level)}: {item.name} "
                      + json.dumps(dict(item.arguments), ensure_ascii=False, allow_nan=False))
            if result.required_high_risk_authorizations:
                print("High-risk confirmation required for: " + ", ".join(result.required_high_risk_authorizations))
            print("Interactive confirmation: /confirm TASK_ID or /confirm-high-risk TASK_ID. "
                  "After restart use --workflow task --id TASK_ID to review the persisted plan hash.")
    if result.status == TaskStatus.COMPLETED:
        return 0
    if result.status in {TaskStatus.WAITING, TaskStatus.NEEDS_REVIEW}:
        return 2
    return 1


def _memory_default(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError("Unsupported memory output")


def _show_memory(value: Any) -> None:
    print(value if isinstance(value, str) else json.dumps(
        value, default=_memory_default, ensure_ascii=False, allow_nan=False, indent=2))


def _companion_parameters(raw: str | None, action: str) -> dict[str, Any]:
    """Reject ambiguous JSON before opening local state; domain validates values."""
    if raw is not None and len(raw.encode("utf-8")) > 16384:
        raise ValueError("Companion parameters exceed 16 KiB")

    def unique_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    value = snapshot_json(json.loads("{}" if raw is None else raw, object_pairs_hook=unique_pairs))
    if not isinstance(value, dict):
        raise ValueError("Companion parameters must be an object")
    allowed = {
        "show": set(),
        "configure": {"expected_revision", "enabled", "emotions_enabled", "preset", "style"},
        "reset": {"expected_revision", "target"},
        "rollback": {"expected_revision", "persona_version"},
        "preference": {"expected_revision", "scope", "verbosity", "humor_allowed"},
        "cue": {"expected_revision", "mode", "ttl_seconds"},
    }
    if set(value) - allowed[action]:
        raise ValueError("Unsupported companion parameter")
    if action != "show":
        revision = value.get("expected_revision")
        if type(revision) is not int or not 0 <= revision < 2**63:
            raise ValueError("Expected companion revision must be a non-negative SQLite integer")
    return dict(value)


def _interaction_parameters(raw: str | None) -> dict[str, Any]:
    if raw is not None and len(raw.encode("utf-8")) > 16384:
        raise ValueError("Interaction parameters exceed budget")

    def unique_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    value = snapshot_json(json.loads(raw or "{}", object_pairs_hook=unique_pairs))
    if not isinstance(value, dict):
        raise ValueError("Expected a bounded JSON object")
    return dict(value)


def _proposal_notice(agent: ApplicationService, shown: deque[str]) -> None:
    """Display only at an input boundary, with a bounded session history."""
    for item in agent.proposal_summary():
        if item.id not in shown:
            print(f"Proposal {item.id} (revision {item.revision}, {item.kind}); "
                  "use --workflow proposals to review and accept explicitly.", file=sys.stderr)
            shown.append(item.id)


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_streams()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(value == "--maintenance" or value.startswith("--maintenance=") for value in arguments):
        from deskpal.maintenance.cli import main as maintenance_main
        return maintenance_main(arguments)
    parser = argparse.ArgumentParser(description="Deskpal persistent local agent backend",
        epilog="Local setup/cache/backup: python -m deskpal --maintenance diagnostics --help")
    parser.add_argument("--config", metavar="PATH", help="TOML configuration file")
    request_mode = parser.add_mutually_exclusive_group()
    request_mode.add_argument("--request", metavar="TEXT", help="Run one request")
    request_mode.add_argument("--tool", metavar="TOOL", help="Propose a deterministic tool call through Agent Core")
    parser.add_argument("--arguments", metavar="JSON", help="JSON object arguments for --tool")
    parser.add_argument("--authorize", action="append", default=[], metavar="TOOL",
                        help="Authorize a tool for the single --request; repeatable")
    parser.add_argument("--authorize-high-risk", action="append", default=[], metavar="TOOL",
                        help="Explicitly authorize a Level 2 tool for this single request; repeatable")
    parser.add_argument("--json", action="store_true", help="Print complete results as JSON")
    request_mode.add_argument("--list-tools", action="store_true", help="List registered tools and exit")
    request_mode.add_argument("--workflow", choices=workflow_cli.ACTIONS, help="Persistent work and explicit goal management")
    workflow_cli.add_arguments(parser)
    request_mode.add_argument("--memory", choices=("remember", "list", "profile", "accept", "reject", "correct", "forget", "export", "rebuild-index"),
                              help="Explicit local memory management; never invokes the planner")
    request_mode.add_argument("--companion", choices=("show", "configure", "reset", "rollback", "preference", "cue"),
                              help="Local persona and expression management; never invokes the planner")
    request_mode.add_argument("--conversation", choices=("interactive",),
                              help="Explicit process-local dialogue; never restores a previous CLI history")
    parser.add_argument("--conversation-cloud", action="store_true",
                        help="Allow current requests in this interactive conversation to use the cloud")
    request_mode.add_argument("--interaction", choices=("show", "cloud_permissions", "identity", "preference",
                              "confirm_candidate", "reject_candidate", "clear_cue", "clear_constraints"),
                              help="Local interaction settings/candidate management, with zero model calls")
    parser.add_argument("--interaction-parameters", metavar="JSON",
                        help="Bounded unique-key JSON, including expected_revision for interaction writes")
    request_mode.add_argument("--privacy", choices=("status", "forget", "retry"),
                              help="Local privacy blocking and Pending cleanup management, with zero model calls")
    parser.add_argument("--privacy-command-id", help="Stable command identifier for --privacy forget")
    parser.add_argument("--companion-parameters", metavar="JSON",
                        help="Unique-key JSON object for --companion, including expected_revision for writes")
    parser.add_argument("--content", help="Content for memory remember/correct")
    parser.add_argument("--memory-id", help="Memory identifier for a revision-bound operation")
    parser.add_argument("--revision", type=int, help="Expected memory revision")
    parser.add_argument("--local-only", action="store_true", help="Prevent this memory from being sent to cloud models")
    parser.add_argument("--profile-key", default="", help="Explicit supported profile field")
    parser.add_argument("--profile-value", default="", help="Explicit supported profile value")
    parser.add_argument("--scope", default="global", help="Memory scope; global is the initial retrieval scope")
    parser.add_argument("--status", choices=tuple(item.value for item in MemoryStatus), help="Memory list status filter")
    parser.add_argument("--history", action="store_true", help="Include retained revisions in memory list/export")
    parser.add_argument("--temporal", action="store_true", help="Correction is a time change, retaining the prior valid period")
    args = parser.parse_args(argv)
    workflow_cli.validate(parser, args)
    if args.conversation_cloud and args.conversation is None:
        parser.error("--conversation-cloud requires --conversation interactive")
    if args.interaction_parameters is not None and args.interaction is None:
        parser.error("--interaction-parameters requires --interaction")
    if args.privacy_command_id is not None and args.privacy != "forget":
        parser.error("--privacy-command-id requires --privacy forget")
    if args.privacy == "forget" and (not args.memory_id or args.revision is None or args.revision < 1):
        parser.error("--privacy forget requires --memory-id and a positive --revision")
    interaction_parameters: dict[str, Any] = {}
    if args.interaction is not None:
        try:
            interaction_parameters = _interaction_parameters(args.interaction_parameters)
        except (ValueError, TypeError, RecursionError):
            parser.error("--interaction-parameters must be a bounded, finite, unique-key JSON object")
        if args.interaction == "show" and interaction_parameters:
            parser.error("--interaction show accepts no parameters")
        if args.interaction != "show" and type(interaction_parameters.get("expected_revision")) is not int:
            parser.error("Interaction writes require expected_revision in --interaction-parameters")
    if (args.authorize or args.authorize_high_risk) and args.request is None and args.tool is None and args.workflow not in {"submit", "confirm"}:
        parser.error("Authorization flags require a single --request or --tool")
    if args.arguments is not None and args.tool is None:
        parser.error("--arguments requires --tool")
    if args.companion_parameters is not None and args.companion is None:
        parser.error("--companion-parameters requires --companion")
    companion_parameters: dict[str, Any] = {}
    if args.companion is not None:
        try:
            companion_parameters = _companion_parameters(args.companion_parameters, args.companion)
        except (ValueError, TypeError, RecursionError):
            parser.error("--companion-parameters must be a finite, bounded JSON object with unique keys, "
                         "supported fields and an expected_revision for writes")
    memory_options = (args.content is not None or args.memory_id is not None or args.revision is not None
                      or args.local_only or args.profile_key or args.profile_value or args.history
                      or args.status is not None or args.temporal or args.scope != "global")
    if memory_options and args.memory is None and args.privacy != "forget":
        parser.error("Memory options require --memory")
    if args.memory in {"remember", "correct"} and not args.content:
        parser.error("Memory remember/correct requires --content")
    if args.memory in {"accept", "reject", "correct", "forget"} and (
            not args.memory_id or args.revision is None or args.revision < 1):
        parser.error("This memory operation requires --memory-id and positive --revision")
    if args.local_only and args.memory != "remember":
        parser.error("--local-only is for --memory remember")
    direct_planner = None
    if args.tool is not None:
        try:
            def unique_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in items:
                    if key in result:
                        raise ValueError("Duplicate JSON key")
                    result[key] = value
                return result
            arguments = snapshot_json(json.loads(args.arguments or "{}", object_pairs_hook=unique_pairs))
            if not isinstance(arguments, dict):
                raise ValueError("Arguments must be an object")
            direct_planner = DirectToolPlanner(ToolCall(args.tool, arguments, "cli-direct"))
        except (ValueError, TypeError, RecursionError):
            parser.error("--arguments must be a finite JSON object with unique keys")
    try:
        local_management = (args.memory is not None or args.companion is not None
                            or args.interaction is not None or args.privacy is not None
                            or (args.conversation is not None and not args.conversation_cloud)
                            or (args.workflow is not None and args.workflow not in workflow_cli.EXECUTION_ACTIONS))
        config_path = args.config
        if config_path is None:
            from deskpal.maintenance.service import default_config_path
            default_path = default_config_path()
            if default_path.is_file():
                config_path = str(default_path)
        settings = load_config(config_path, provider_override="offline" if local_management else None)
        with create_application(settings, planner=direct_planner) as agent:
            if args.interaction is not None:
                if args.interaction == "show":
                    _show_memory(agent.interaction_snapshot())
                else:
                    interaction_parameters.setdefault("command_id", str(uuid4()))
                    _show_memory(agent.interaction_command(args.interaction, **interaction_parameters))
                return 0
            if args.privacy is not None:
                if args.privacy == "status":
                    _show_memory(agent.cleanup_status())
                elif args.privacy == "retry":
                    _show_memory({"complete": agent.retry_cleanup(), "cleanup": agent.cleanup_status()})
                else:
                    _show_memory(agent.request_forget(args.memory_id, args.revision,
                        args.privacy_command_id or str(uuid4())))
                return 0
            if args.companion is not None:
                value = (agent.companion_snapshot() if args.companion == "show" else
                         agent.companion_command(args.companion, **companion_parameters))
                _show_memory(value)
                return 0
            if args.workflow is not None:
                result_value = workflow_cli.execute(agent, args)
                _show_memory(result_value)
                return workflow_cli.exit_code(result_value)
            if args.memory is not None:
                parameters: dict[str, Any] = {}
                action = "list_items" if args.memory == "list" else args.memory.replace("-", "_")
                if action == "remember":
                    parameters = {"content": args.content, "use_policy": UsePolicy.LOCAL_ONLY if args.local_only else UsePolicy.CLOUD_ALLOWED,
                                  "profile_key": args.profile_key, "profile_value": args.profile_value, "scope": args.scope}
                elif action in {"accept", "reject", "correct", "forget"}:
                    parameters = {"memory_id": args.memory_id, "expected_revision": args.revision}
                    if action == "correct":
                        parameters.update(content=args.content, temporal=args.temporal)
                elif action in {"list_items", "export"}:
                    parameters = {"include_history": args.history}
                    if action == "list_items" and args.status is not None:
                        parameters["status"] = MemoryStatus(args.status)
                _show_memory(agent.memory_command(action, **parameters))
                return 0
            if args.list_tools:
                specs = agent.tools.list_tools()
                if args.json:
                    print(json.dumps([asdict(spec) for spec in specs], ensure_ascii=False, allow_nan=False))
                else:
                    for spec in specs:
                        print(f"{spec.name} (permission {int(spec.permission_level)}): {spec.description}")
                return 0
            if args.request is not None:
                return _display(agent.run(args.request, authorized_tools=args.authorize,
                                          high_risk_authorized_tools=args.authorize_high_risk), json_output=args.json)
            if args.tool is not None:
                return _display(agent.run("Explicit tool request: " + args.tool, authorized_tools=args.authorize,
                                          high_risk_authorized_tools=args.authorize_high_risk), json_output=args.json)
            if settings.llm.provider == "offline":
                print("[离线演示] 固定命令：echo 文本、time、read 路径、write 路径 文本、tool 工具名 JSON。"
                      "输入 /confirm TASK_ID 确认原计划，/confirm-high-risk TASK_ID 确认高风险计划，/quit 退出。", file=sys.stderr)
            else:
                print("Deskpal interactive mode. Enter /quit to exit.", file=sys.stderr)
            code = 0
            conversation = agent.conversation_open() if args.conversation else None
            if conversation is not None:
                print("会话仅保留本次进程的有界历史。/conversation new|show|close|single 管理上下文；"
                      "/interaction ACTION JSON 与 /privacy status|retry|forget ID REV COMMAND_ID 为本地管理。", file=sys.stderr)
            pending: dict[str, AgentResult] = {}
            shown_proposals: deque[str] = deque(maxlen=1000)
            while True:
                if settings.proactive.enabled:
                    _proposal_notice(agent, shown_proposals)
                try:
                    request = input("deskpal> " if sys.stdin.isatty() else "")
                except EOFError:
                    return code
                if request.strip() in {"/quit", "/exit"}:
                    return code
                if not request.strip():
                    continue
                if request.startswith("/conversation "):
                    action = request.partition(" ")[2].strip()
                    try:
                        if action in {"close", "single", "new"} and conversation is not None:
                            agent.conversation_close(conversation["conversation_id"], conversation["epoch"])
                            conversation = None
                        if action == "new":
                            conversation = agent.conversation_open()
                            _show_memory(conversation)
                        elif action == "show" and conversation is not None:
                            _show_memory(agent.conversation_snapshot(conversation["conversation_id"]))
                        elif action in {"close", "single"}:
                            _show_memory({"closed": True, "persistent_tasks_retained": True})
                        else:
                            raise ValueError("Invalid conversation operation")
                    except (ValueError, RuntimeError, KeyError):
                        print("Conversation operation rejected; no history restored.", file=sys.stderr)
                        code = 1
                    continue
                if request.startswith("/interaction "):
                    try:
                        parts = request.split(maxsplit=2)
                        action = parts[1]
                        parameters_i = _interaction_parameters(parts[2] if len(parts) > 2 else None)
                        if action == "show" and not parameters_i:
                            _show_memory(agent.interaction_snapshot())
                        else:
                            parameters_i.setdefault("command_id", str(uuid4()))
                            _show_memory(agent.interaction_command(action, **parameters_i))
                    except (ValueError, TypeError, RuntimeError, KeyError):
                        print("Interaction command rejected; reload its current revision.", file=sys.stderr)
                        code = 1
                    continue
                if request.startswith("/privacy "):
                    try:
                        parts = request.split()
                        if parts == ["/privacy", "status"]:
                            _show_memory(agent.cleanup_status())
                        elif parts == ["/privacy", "retry"]:
                            _show_memory({"complete": agent.retry_cleanup()})
                        elif len(parts) == 5 and parts[1] == "forget":
                            _show_memory(agent.request_forget(parts[2], int(parts[3]), parts[4]))
                            conversation = None
                        else:
                            raise ValueError("Invalid privacy command")
                    except (ValueError, TypeError, RuntimeError, KeyError):
                        print("Privacy command rejected; inspect cleanup status before retrying.", file=sys.stderr)
                        code = 1
                    continue
                if request.startswith(("/memory ", "/remember ", "/forget ", "/correct ", "/accept ", "/reject ")):
                    try:
                        parts = request.strip().split(maxsplit=3)
                        command_name = parts[0]
                        if command_name == "/remember":
                            value = agent.memory_command("remember", content=request.partition(" ")[2])
                        elif command_name == "/memory" and len(parts) == 2 and parts[1] in {"list", "profile", "export", "rebuild-index"}:
                            value = agent.memory_command("list_items" if parts[1] == "list" else parts[1].replace("-", "_"))
                        elif command_name in {"/forget", "/accept", "/reject", "/correct"} and len(parts) >= 3:
                            parameters = {"memory_id": parts[1], "expected_revision": int(parts[2])}
                            if command_name == "/correct":
                                if len(parts) != 4:
                                    raise ValueError("Correct requires content")
                                parameters["content"] = parts[3]
                            value = agent.memory_command(command_name[1:], **parameters)
                        else:
                            raise ValueError("Invalid memory command")
                        _show_memory(value)
                        code = 0
                    except (ValueError, TypeError):
                        print("Memory command rejected; check content, identifier and current revision.", file=sys.stderr)
                        code = 1
                    finally:
                        for pending_id in tuple(pending):
                            current_task = agent.tasks.get(pending_id)
                            if current_task is None or current_task.status != TaskStatus.WAITING:
                                pending.pop(pending_id, None)
                    continue
                command = request.strip().split()
                if command[0] in {"/confirm", "/confirm-high-risk", "/cancel"}:
                    if len(command) != 2 or command[1] not in pending:
                        print("Unknown pending task or invalid confirmation command.", file=sys.stderr)
                        code = 1
                        continue
                    task_id = command[1]
                    previous = pending[task_id]
                    if command[0] == "/cancel":
                        try:
                            agent.cancel_task(task_id)
                        except (ValueError, KeyError):
                            print("Task is no longer pending.", file=sys.stderr)
                            pending.pop(task_id, None)
                            code = 1
                            continue
                        pending.pop(task_id, None)
                        print("Task cancelled.", file=sys.stderr)
                        code = 0
                        continue
                    # These commands approve only the exact plan already displayed
                    # in this process; Level 2 requires the separate command.
                    high_risk = previous.required_high_risk_authorizations if command[0] == "/confirm-high-risk" else ()
                    ordinary = tuple(name for name in previous.required_authorizations
                                     if name not in previous.required_high_risk_authorizations)
                    try:
                        result = agent.confirm_task(task_id, authorized_tools=ordinary,
                                                    high_risk_authorized_tools=high_risk)
                    except (ValueError, KeyError):
                        pending.pop(task_id, None)
                        print("Plan is no longer valid; submit a new request and confirm its displayed plan.", file=sys.stderr)
                        code = 1
                        continue
                else:
                    if conversation is None:
                        result = agent.run(request)
                    else:
                        task = agent.submit(request, conversation_id=conversation["conversation_id"],
                            expected_conversation_epoch=conversation["epoch"],
                            use_policy="cloud_allowed" if args.conversation_cloud else "local_only")
                        result = agent.wait(task.id)
                if result.status == TaskStatus.WAITING:
                    pending[result.task_id] = result
                else:
                    pending.pop(result.task_id, None)
                code = _display(result, json_output=args.json)
    except KeyboardInterrupt:
        print("Request interrupted.", file=sys.stderr)
        return 130
    except (ConfigError, LLMError) as exc:
        print(f"Deskpal error: {exc}", file=sys.stderr)
        return 1
    except MemoryError as exc:
        print(f"Deskpal memory error: {exc}", file=sys.stderr)
        return 1
    except FileExistsError:
        print("Deskpal error: backup destination already exists; choose a new path.", file=sys.stderr)
        return 1
    except ValueError:
        print("Deskpal error: invalid request, configuration, or result data.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Deskpal error: backend operation failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
