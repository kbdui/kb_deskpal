"""Local backend CLI. Tool authorization is always explicit per invocation."""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from dataclasses import is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from deskpal.agent.models import AgentResult
from deskpal.config import ConfigError, load_config
from deskpal.llm.errors import LLMError
from deskpal._json import snapshot_json
from deskpal.planner.direct import DirectToolPlanner
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolCall
from deskpal.memory.contracts import MemoryStatus, UsePolicy, MemoryError

from .factory import create_agent


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
                  "Single-request mode ends this process; resubmitting creates a new plan.")
    if result.status == TaskStatus.COMPLETED:
        return 0
    if result.status == TaskStatus.WAITING:
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


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_streams()
    parser = argparse.ArgumentParser(description="Deskpal V0.3 local agent backend")
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
    request_mode.add_argument("--memory", choices=("remember", "list", "profile", "accept", "reject", "correct", "forget", "export", "rebuild-index"),
                              help="Explicit local memory management; never invokes the planner")
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
    if (args.authorize or args.authorize_high_risk) and args.request is None and args.tool is None:
        parser.error("Authorization flags require a single --request or --tool")
    if args.arguments is not None and args.tool is None:
        parser.error("--arguments requires --tool")
    memory_options = (args.content is not None or args.memory_id is not None or args.revision is not None
                      or args.local_only or args.profile_key or args.profile_value or args.history
                      or args.status is not None or args.temporal or args.scope != "global")
    if memory_options and args.memory is None:
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
        settings = load_config(args.config, provider_override="offline" if args.memory is not None else None)
        with create_agent(settings, planner=direct_planner) as agent:
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
            pending: dict[str, AgentResult] = {}
            while True:
                try:
                    request = input("deskpal> " if sys.stdin.isatty() else "")
                except EOFError:
                    return code
                if request.strip() in {"/quit", "/exit"}:
                    return code
                if not request.strip():
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
                    result = agent.run(request)
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
    except ValueError:
        print("Deskpal error: invalid request, configuration, or result data.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Deskpal error: backend operation failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
