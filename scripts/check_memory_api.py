"""Explicitly gated V0.3 API acceptance using synthetic data and temporary SQLite.

No credentials are read until --live is supplied. Never prints credentials,
provider response bodies, or user memories. At most three requests, each capped
at 1,024 output tokens; no file/screenshot or external-action tools are used.
"""

import argparse
import json
import tempfile
import urllib.request
import urllib.error
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from deskpal.agent.core import AgentCore
from deskpal.config import load_config
from deskpal.llm.providers import OpenAILLMProvider
from deskpal.llm.models import ChatMessage, LLMResponse
from deskpal.memory.contracts import MemoryDraft, MemorySource, MemoryStatus
from deskpal.memory.coordinator import MemoryCoordinator
from deskpal.memory.evaluation import LLMMemoryEvaluator
from deskpal.memory.repository import SQLiteMemoryRepository
from deskpal.planner.direct import DirectToolPlanner
from deskpal.sandbox.workspace import WorkspaceSandbox
from deskpal.tasks.manager import InMemoryTaskManager
from deskpal.tasks.models import TaskStatus
from deskpal.tools.manager import BuiltinToolManager
from deskpal.tools.models import ToolCall, ToolSpec


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


class BoundedTransport:
    def __init__(self, max_calls: int = 3) -> None:
        self.calls = 0
        self.max_calls = max_calls
        self.failure_category = ""

    def __call__(self, request: urllib.request.Request, *, timeout: float) -> Any:
        if self.calls >= self.max_calls:
            raise RuntimeError("Live request budget exhausted")
        self.calls += 1
        assert isinstance(request.data, bytes)
        payload = json.loads(request.data)
        payload["max_output_tokens"] = 1024
        payload["reasoning"] = {"effort": "none"}
        payload["stream"] = False
        limited = urllib.request.Request(request.full_url, data=json.dumps(payload).encode("utf-8"),
                                         headers=dict(request.header_items()), method="POST")
        try:
            return urllib.request.build_opener(RejectRedirects()).open(limited, timeout=min(timeout, 30))
        except urllib.error.HTTPError as error:
            self.failure_category = "HTTP " + str(error.code)
            raise
        except urllib.error.URLError:
            self.failure_category = "network"
            raise
        except TimeoutError:
            self.failure_category = "timeout"
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly enable paid API calls")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-calls", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--candidate-only", action="store_true", help="One-call extraction/review check without tool feedback")
    args = parser.parse_args()
    if not args.live:
        parser.error("Real API check is disabled; --live is required")
    transport = BoundedTransport(args.max_calls)
    report: dict[str, Any] = {"passed": False, "calls": 0, "max_calls": args.max_calls, "max_output_tokens_per_call": 1024}
    report["stage"] = "configuration"
    try:
        settings = load_config(args.config)
        if settings.llm.provider != "openai":
            raise ValueError("API configuration required")
        class DiagnosticProvider(OpenAILLMProvider):
            def chat(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] = ()) -> LLMResponse:
                try:
                    return super().chat(messages, tools=tools)
                except Exception as error:
                    from deskpal.llm.errors import LLMError
                    if isinstance(error, LLMError):
                        report["provider_error"] = str(error)
                    raise

            def _parse_response(self, raw: Any) -> LLMResponse:
                if isinstance(raw, dict):
                    status = raw.get("status")
                    report["response_status"] = status if status in {"completed", "incomplete", "failed"} else "other"
                    details = raw.get("incomplete_details")
                    if isinstance(details, dict) and details.get("reason") == "max_output_tokens":
                        report["incomplete_reason"] = "max_output_tokens"
                return super()._parse_response(raw)

        llm = DiagnosticProvider(settings.llm, transport=transport, timeout_seconds=30,
                                 max_output_tokens=1024)
        with tempfile.TemporaryDirectory(prefix="deskpal-v03-live-") as temporary:
            root = Path(temporary)
            with SQLiteMemoryRepository(root / "memory.sqlite3") as repository:
                class DiagnosticEvaluator(LLMMemoryEvaluator):
                    def evaluate(self, source: MemorySource) -> tuple[MemoryDraft, ...]:
                        try:
                            return super().evaluate(source)
                        except Exception as error:
                            # These validator messages contain fixed field names,
                            # never provider response bodies or the credential.
                            from deskpal.memory.contracts import MemoryError
                            if isinstance(error, MemoryError):
                                report["evaluation_error"] = str(error)
                            raise

                memory = MemoryCoordinator(repository, evaluator=DiagnosticEvaluator(llm, max_candidates=1),
                                           mode="candidate_review", cloud=True)
                report["stage"] = "candidate_extraction"
                assert memory.learn("我喜欢简短回答", "synthetic-preference") == ()
                candidates = memory.list_items(status=MemoryStatus.CANDIDATE)
                assert len(candidates) == 1 and not memory.profile()
                report["candidate_global_scope"] = candidates[0].draft.scope == "global"
                assert candidates[0].draft.scope == "global", "Unqualified preference requires global scope"
                report["stage"] = "review_correct_forget"
                accepted = memory.accept(candidates[0].id, candidates[0].revision)
                assert accepted.status == MemoryStatus.ACCEPTED
                corrected = memory.correct(accepted.id, accepted.revision, "我喜欢详细回答")
                assert any(entry.key == "verbosity" and entry.value == "detailed"
                           for entry in memory.profile()), "Corrected preference must enter the profile"
                memory.forget(corrected.id, corrected.revision)
                previous = repository.get(accepted.id)
                assert previous is not None
                memory.forget(accepted.id, previous.revision)
                assert not memory.profile()
                # The real candidate path was checked above. Tool feedback needs
                # one further call; automatic-learning failure/retry is offline-tested.
                if not args.candidate_only:
                    memory.mode = "explicit_only"
                    report["stage"] = "tool_feedback"
                    tools = BuiltinToolManager(WorkspaceSandbox(root))
                    tools.register_builtin()
                    core = AgentCore(llm=llm, planner=DirectToolPlanner(ToolCall("echo", {"text": "synthetic-result"}, "live-echo")),
                                     tasks=InMemoryTaskManager(), memory=memory, memory_runtime=memory,
                                     tools=tools, workspace_dir=root)
                    with core:
                        result = core.run("请回显：我喜欢简短回答")
                        assert result.status == TaskStatus.COMPLETED
                        assert not any("incomplete" in warning or "Response generation failed" in warning
                                       for warning in result.warnings)
                        assert result.tool_results[0].output == "synthetic-result"
                assert transport.calls == (1 if args.candidate_only else 2)
        report["passed"] = True
        report["stage"] = "completed"
    except Exception as exc:
        report["failure_category"] = type(exc).__name__
    report["calls"] = transport.calls
    if transport.failure_category:
        report["transport_failure"] = transport.failure_category
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
