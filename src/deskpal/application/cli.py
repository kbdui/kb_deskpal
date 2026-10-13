"""Local backend CLI. Tool authorization is always explicit per invocation."""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from deskpal.agent.models import AgentResult
from deskpal.config import ConfigError, load_config
from deskpal.llm.errors import LLMError
from deskpal.tasks.models import TaskStatus

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
        if result.required_authorizations:
            print("Resubmit with " + " ".join(f"--authorize {name}" for name in result.required_authorizations))
    if result.status == TaskStatus.COMPLETED:
        return 0
    if result.status == TaskStatus.WAITING:
        return 2
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_streams()
    parser = argparse.ArgumentParser(description="Deskpal V0.1 local agent backend")
    parser.add_argument("--config", metavar="PATH", help="TOML configuration file")
    request_mode = parser.add_mutually_exclusive_group()
    request_mode.add_argument("--request", metavar="TEXT", help="Run one request")
    parser.add_argument("--authorize", action="append", default=[], metavar="TOOL",
                        help="Authorize a tool for the single --request; repeatable")
    parser.add_argument("--json", action="store_true", help="Print complete results as JSON")
    request_mode.add_argument("--list-tools", action="store_true", help="List registered tools and exit")
    args = parser.parse_args(argv)
    if args.authorize and args.request is None:
        parser.error("--authorize requires a single --request")
    try:
        settings = load_config(args.config)
        with create_agent(settings) as agent:
            if args.list_tools:
                specs = agent.tools.list_tools()
                if args.json:
                    print(json.dumps([asdict(spec) for spec in specs], ensure_ascii=False, allow_nan=False))
                else:
                    for spec in specs:
                        print(f"{spec.name} (permission {int(spec.permission_level)}): {spec.description}")
                return 0
            if args.request is not None:
                return _display(agent.run(args.request, authorized_tools=args.authorize), json_output=args.json)
            if settings.llm.provider == "offline":
                print("[离线演示] 固定命令：echo 文本、time、read 路径、write 路径 文本。输入 /quit 退出。", file=sys.stderr)
            else:
                print("Deskpal interactive mode. Enter /quit to exit.", file=sys.stderr)
            code = 0
            while True:
                try:
                    request = input("deskpal> " if sys.stdin.isatty() else "")
                except EOFError:
                    return code
                if request.strip() in {"/quit", "/exit"}:
                    return code
                if not request.strip():
                    continue
                code = _display(agent.run(request), json_output=args.json)
    except KeyboardInterrupt:
        print("Request interrupted.", file=sys.stderr)
        return 130
    except (ConfigError, LLMError) as exc:
        print(f"Deskpal error: {exc}", file=sys.stderr)
        return 1
    except ValueError:
        print("Deskpal error: invalid request, configuration, or result data.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Deskpal error: backend operation failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
