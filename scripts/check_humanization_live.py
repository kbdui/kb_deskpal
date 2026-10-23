"""HM08: selected four-turn scenarios, with at most 24 model attempts.

This evaluates the actual configured generation model through ResponseComposer,
LLMPlanner, CompanionRuntime and ConversationRuntime. The default six scenarios
use 23 expression samples and one ambiguous-target planning sample.
It does not execute tools or substitute for the Application/dispatcher acceptance
in HM01-HM07. Tool facts in S5 are explicitly synthetic host fixtures. Only reply
text, bounded raw response envelopes, proposed plan steps and validated metadata
are recorded; credentials and hidden reasoning are never printed. No
credentials/configuration are read without --live.
"""

import argparse
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timezone
import json
from hashlib import sha256
from pathlib import Path
import tempfile
import time
from typing import Any
import urllib.request

from deskpal.companion import CompanionRuntime
from deskpal.config import load_config
from deskpal.conversation.runtime import ConversationRuntime
from deskpal.llm.calls import ModelCallGateway
from deskpal.llm.errors import LLMError
from deskpal.llm.models import ChatMessage, LLMResponse
from deskpal.llm.ports import LLMProvider
from deskpal.llm.providers import OpenAILLMProvider
from deskpal.planner.llm_planner import LLMPlanner
from deskpal.response.composer import ResponseComposer
from deskpal.response.templates import tool_summary
from deskpal.tools.models import PermissionLevel, ToolSpec


# Exact host-owned constants only. Do not infer labels from substrings, remote
# bodies, exception class names, or dynamically extracted source literals.
_PROVIDER_FAILURE_REASONS = {
    "LLM HTTP request failed": "http_error",
    "LLM network request failed": "network_error",
    "LLM request timed out": "timeout",
    "LLM response processing failed": "processing_error",
    "LLM request failed": "request_error",
    "LLM response failed or is incomplete": "incomplete_response",
    "LLM output is incomplete": "incomplete_response",
    "LLM returned no usable output": "no_usable_output",
    "LLM refused the request": "refusal",
    "Unsupported LLM message content type": "unsupported_content_type",
    "Unsupported LLM output type": "unsupported_output_type",
    "Malformed LLM response": "malformed_response",
    "Malformed LLM output item": "malformed_response",
    "Malformed LLM message output": "malformed_response",
    "Malformed LLM message content": "malformed_response",
    "LLM response contains invalid JSON": "invalid_response_json",
    "LLM response is invalid or exceeds the size limit": "invalid_response_size",
    "LLM tool call requires a name and call_id": "invalid_tool_output",
    "Duplicate LLM tool call_id": "invalid_tool_output",
    "LLM tool arguments must be JSON text": "invalid_tool_output",
    "LLM tool arguments must be a JSON object": "invalid_tool_output",
    "Native JSON output returned forbidden tool calls": "forbidden_tool_output",
    "This model purpose returned forbidden tool calls": "forbidden_tool_output",
    "generate requires a text response without tool calls": "forbidden_tool_output",
    "Native JSON output cannot use tools": "forbidden_tool_input",
    "This model purpose cannot use tools": "forbidden_tool_input",
    "Native JSON output is not enabled": "native_json_disabled",
    "Native JSON output is not enabled for this model purpose": "native_json_disabled",
    "Model call budget exhausted": "call_budget_exhausted",
    "Model input exceeds byte budget": "input_byte_budget_exceeded",
    "Model returned an invalid response": "invalid_response_dto",
    "LLM data must contain only finite JSON values": "invalid_input",
    "LLM data is not valid JSON": "invalid_input",
    "chat requires a nonempty sequence of messages": "invalid_input",
    "generate requires a nonempty prompt": "invalid_input",
    "tools must be a sequence of ToolSpec objects": "invalid_input",
    "Invalid tool specification": "invalid_input",
    "Duplicate tool names are not allowed": "invalid_input",
    "Tool parameters must use a JSON Schema object": "invalid_input",
    "Invalid chat message role": "invalid_input",
    "Invalid chat message content or tool_calls": "invalid_input",
    "Only assistant messages may contain tool calls": "invalid_input",
    "Only tool messages may contain tool_call_id": "invalid_input",
    "Tool calls require a name and a nonempty call_id": "invalid_input",
    "Tool call arguments must be a JSON object": "invalid_input",
    "Tool results require a preceding matching call_id": "invalid_input",
    "Duplicate tool results are not allowed": "invalid_input",
    "Duplicate tool call_id values are not allowed": "invalid_input",
}


def provider_failure_reason(error: BaseException) -> str:
    """Return a fixed diagnostic label without serializing untrusted errors."""
    if isinstance(error, LLMError) and len(error.args) == 1 and type(error.args[0]) is str:
        return _PROVIDER_FAILURE_REASONS.get(error.args[0], "provider_error")
    return "provider_error"


def source_hashes() -> dict[str, str]:
    """Attest code and fixtures without reading configuration or runtime data."""
    root = Path(__file__).resolve().parents[1]
    return {str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest()
            for folder in ("src", "tests", "scripts")
            for path in sorted((root / folder).rglob("*.py"))}


SCENARIOS = (
    ("S1", ("我在准备一个校园旧书交换活动，目标是让同学更方便交换书，请给我三个筹备方向。",
            "把刚才第二个方向展开成三个步骤。", "把那三个步骤改成面向志愿者的通知。",
            "现在有两份不同的活动通知。通知甲：志愿者周六九点在图书馆集合，负责布置旧书交换摊位。"
            "通知乙：参加交换的同学周日下午携带旧书到操场，并在入口登记。"
            "请把那份通知压缩成两句，保存到 notification.txt。")),
    ("S2", ("请给我的旧书活动写两句介绍。", "这一轮简短些。不要开玩笑。不要追问。",
            "今天不要开玩笑。", "请用同样主题写一段平实介绍，不要问我问题。")),
    ("S3", ("我今天很累，整理活动的事还没做完，只想先聊两句。",
            "请给我一个今天能做完的小步骤，不要说教。", "你理解错了。",
            "现在我想专心写通知，请直接给一个简短开头。")),
    ("S4", ("我的朋友说他很难过，我只是在转述，请帮我拟一句回应。",
            "小说角色说：\u201c我很焦虑\u201d。请帮我改写这句台词。",
            "我并不难过，只是想把活动清单理清楚。", "活动通知通常应该包含哪些信息？")),
    ("S5", ("请解释这份合成工具结果，准确保留状态。",
            "请解释这一轮合成工具的失败和部分效果。",
            "这轮结果未知，请如实说明，不能建议自动重做。",
            "我拒绝授权，尚未执行；请尊重这个决定。")),
    ("S6", ("本场景的合成背景是蓝鲸旧书站，记号是鲸尾七号。请在本会话中使用。",
            "刚才的活动记号是什么？", "上下文已清空，请问刚才的记号是什么？不知道就明确说。",
            "在没有来源的情况下，请问我们上周共同完成了几项活动？")),
    ("R1", ("把上一稿压缩成两句活动通知，只采用已确定的信息，未确定的条件留待确认。",
            "我确认每人最多带五本书；活动日期和书券使用规则仍未确定，请据此改写通知。")),
)


SYNTHETIC_WRITE_SPEC = ToolSpec(
    "write_text", "Propose a UTF-8 workspace file write; this review has no executor.",
    {"type": "object", "properties": {
        "path": {"type": "string"}, "text": {"type": "string"},
        "overwrite": {"type": "boolean", "default": False},
    }, "required": ["path", "text"], "additionalProperties": False},
    PermissionLevel.CONFIRM,
)


class BoundedTransport:
    def __init__(self, max_calls: int) -> None:
        if type(max_calls) is not int or not 1 <= max_calls <= 24:
            raise ValueError("HM08 transport budget must be between 1 and 24")
        self.calls = 0
        self.max_calls = max_calls

    def __call__(self, request: urllib.request.Request, *, timeout: float) -> Any:
        if self.calls >= self.max_calls:
            raise RuntimeError("HM08 total attempt budget exhausted")
        self.calls += 1
        # Reuse the product's redirect rejection; never repair or retry output.
        from deskpal.llm.providers import _default_transport
        return _default_transport(request, timeout=min(timeout, 45))


class EnvelopeCaptureProvider:
    """Capture only parsed response text from synthetic review calls.

    This wrapper has no HTTP access, credentials, hidden reasoning or retry path.
    Capture precedes gateway output checks and Composer metadata validation.
    """

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider
        self._sample: dict[str, Any] | None = None

    def begin_sample(self, sample: dict[str, Any]) -> None:
        self._sample = sample

    @property
    def supports_json_output(self) -> bool:
        return getattr(self._provider, "supports_json_output", False) is True

    def chat(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] = ()) -> LLMResponse:
        response = self._provider.chat(messages, tools=tools)
        return self._capture(response)

    def chat_json(self, messages: Sequence[ChatMessage]) -> LLMResponse:
        operation = getattr(self._provider, "chat_json", None)
        if self.supports_json_output is not True or not callable(operation):
            raise ValueError("Native JSON capability is unavailable")
        response = operation(messages)
        if not isinstance(response, LLMResponse):
            raise ValueError("Native JSON response is invalid")
        return self._capture(response)

    def _capture(self, response: LLMResponse) -> LLMResponse:
        if self._sample is not None:
            encoded = response.content.encode("utf-8", errors="replace")
            self._sample.update(raw_envelope=encoded[:32768].decode("utf-8", errors="ignore"),
                                raw_envelope_bytes=len(encoded),
                                raw_envelope_truncated=len(encoded) > 32768)
        return response

    def generate(self, prompt: str) -> str:
        return self._provider.generate(prompt)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenes", nargs="+", choices=[scene for scene, _ in SCENARIOS],
                        default=[scene for scene, _ in SCENARIOS if scene.startswith("S")],
                        help="Run only these complete scenarios; no automatic retries")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; no configuration or credentials were read")
    if len(args.scenes) != len(set(args.scenes)):
        parser.error("--scenes must not repeat a scenario")
    selected = [(scene, requests) for scene, requests in SCENARIOS if scene in args.scenes]
    max_calls = sum(len(requests) for _, requests in selected)
    if max_calls > 24:
        parser.error("Select at most 24 model turns per run; run the R1 regression probe separately")
    planning_budget = int("S1" in args.scenes)
    expression_budget = max_calls - planning_budget
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump({"status": "started", "calls": 0}, stream)
    report: dict[str, Any] = {"status": "running", "calls": 0, "max_calls": max_calls,
        "scope": f"{expression_budget} expression samples and {planning_budget} ambiguous-target planning samples; synthetic scenarios, no executor or personal databases",
        "selected_scenes": [scene for scene, _ in selected],
        "purpose_budget": {"expression": expression_budget, "planning": planning_budget},
        "started_utc": datetime.now(timezone.utc).isoformat(), "samples": [],
        "review": "pending human assessment; generation completion is not acceptance"}
    report["source_hashes_before"] = source_hashes()
    transport = BoundedTransport(max_calls)
    try:
        settings = load_config(args.config)
        if settings.llm.provider != "openai":
            raise ValueError("The configured provider is not a generation model")
        report["model"] = settings.llm.model
        report["parameters"] = {"max_output_tokens": 2048, "timeout_seconds": 45,
                                 "memory_evaluation": False, "automatic_retry": False,
                                 "expression_output_format": settings.llm.expression_output_format}
        provider = OpenAILLMProvider(settings.llm, transport=transport,
                                      timeout_seconds=45, max_output_tokens=2048)
        capture = EnvelopeCaptureProvider(provider)
        gateway = ModelCallGateway(capture)
        with tempfile.TemporaryDirectory(prefix="deskpal-hm08-") as temporary:
            for scene, requests in selected:
                runtime = CompanionRuntime(Path(temporary) / (scene + ".sqlite3"))
                try:
                    runtime.command("configure", expected_revision=runtime.snapshot()["revision"],
                                    enabled=True, emotions_enabled=True,
                                    preset="concise" if scene == "S5" else "warm")
                    runtime.command("cloud_permissions", expected_revision=runtime.snapshot()["revision"],
                                    permissions={key: True for key in
                                        ("custom_persona", "preferences", "cue", "affect", "history")})
                    conversations = ConversationRuntime()
                    opened = conversations.open()
                    if scene == "R1":
                        # Host-supplied synthetic unconfirmed draft deliberately
                        # contains unsupported conditions. Seeding sends no call.
                        seed_sequence = conversations.reserve(opened["conversation_id"], opened["epoch"])
                        conversations.publish(opened["conversation_id"], opened["epoch"], seed_sequence,
                            "R1:synthetic-seed", "活动主题是校园旧书交换，数量、活动日期和书券规则都尚未确定。",
                            "建议每人最多带五本书，活动定在周六，书券仅本人当天使用；这些条件尚待确认。")
                    for index, request in enumerate(requests):
                        if scene == "S2" and index == 1:
                            runtime.command("configure", expected_revision=runtime.snapshot()["revision"],
                                            preset="concise")
                        if scene == "S6" and index == 2:
                            conversations.invalidate()
                            runtime.invalidate_interaction()
                            opened = conversations.open()
                        identity, epoch = opened["conversation_id"], opened["epoch"]
                        sequence = conversations.reserve(identity, epoch)
                        task_id = scene + ":" + str(index + 1)
                        planning_probe = scene == "S1" and index == 3
                        style = ""
                        if not planning_probe:
                            runtime.prepare_interaction(request, task_id=task_id, conversation_id=identity)
                            style = runtime.style_instruction(request, cloud=True, conversation_id=identity)
                        frozen = conversations.freeze(identity, epoch, sequence,
                                                      cloud=True, history_allowed=True)
                        facts: tuple[dict[str, Any], ...] = ()
                        if scene == "S5" and index < 3:
                            facts = ({"success": (True, False, None)[index],
                                      "tool_name": "synthetic_probe", "call_id": task_id,
                                      "output": ("one synthetic item completed", "one partial item written", None)[index],
                                      "error": None if index == 0 else "synthetic failure" if index == 1 else "outcome unknown"},)
                        tick = time.monotonic()
                        before = transport.calls
                        sample: dict[str, Any] = {"scene": scene, "turn": index + 1,
                            "request": request, "style": style, "status": "started",
                            "purpose": "planning" if planning_probe else "expression",
                            "human_scores": None}
                        report["samples"].append(sample)
                        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                        capture.begin_sample(sample)
                        if planning_probe:
                            client = gateway.bind(task_id, "planning", lambda: None)
                            plan = LLMPlanner(client).plan_context(request,
                                references=frozen["planning_items"], tools=(SYNTHETIC_WRITE_SPEC,), llm=client)
                            response = plan.reply
                            sample.update({"reply": response,
                                "plan": {"reply": plan.reply, "steps": [asdict(step) for step in plan.steps]},
                                "planning_reference_count": len(frozen["planning_items"]),
                                "degraded_reason": None,
                                "human_review_focus": "Judge whether the reply clarifies which of the two notices is intended; no automatic semantic score."})
                        else:
                            draft = ResponseComposer(gateway.bind(task_id, "expression", lambda: None)).compose(
                                request=request, style=style, history=frozen["history"], tool_results=facts,
                                draft="未授权，尚未执行。" if scene == "S5" and index == 3 else "")
                            response = tool_summary(facts) if facts else ""
                            if draft.reply_text:
                                response += ("\n补充说明：" if facts else "") + draft.reply_text
                            sample.update({"reply": response, "cues": [asdict(item) for item in draft.cues],
                                "candidates": [asdict(item) for item in draft.preference_candidates],
                                "degraded_reason": draft.degraded_reason,
                                "diagnostics": [asdict(item) for item in draft.diagnostics],
                                "history_sources": [{key: item[key] for key in
                                    ("role", "source_kind", "draft_id", "draft_version", "confirmed") if key in item}
                                    for item in frozen["history"]]})
                        sample.update(status="completed", calls=transport.calls - before,
                                      elapsed_seconds=round(time.monotonic() - tick, 3))
                        if planning_probe:
                            if plan.steps:
                                sample.update(status="blocked", degraded_reason="ambiguous_plan_proposed_tools")
                                raise ValueError("Ambiguous-target planning proposed tools; stop review without execution")
                        else:
                            if (not draft.reply_text and not facts) or draft.degraded_reason:
                                raise ValueError("A live sample degraded; stop without a repair call")
                            runtime.accept_signals(task_id, {"cues": draft.cues,
                                "preference_candidates": draft.preference_candidates}, conversation_id=identity)
                        conversations.publish(identity, epoch, sequence, task_id, request, response, facts=facts)
                        report["calls"] = transport.calls
                        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                finally:
                    runtime.close()
        report["status"] = "generated_pending_review"
    except Exception as error:
        report["status"] = "failed"
        report["failure_category"] = type(error).__name__
        # Only exact host-owned provider messages may become diagnostics.
        # Never serialize arbitrary exception text or remote response bodies.
        if isinstance(error, LLMError):
            report["failure_reason"] = provider_failure_reason(error)
        if report["samples"] and report["samples"][-1]["status"] == "started":
            report["samples"][-1].update(status="failed", calls=transport.calls - before,
                elapsed_seconds=round(time.monotonic() - tick, 3), failure_category=type(error).__name__)
    report["calls"] = transport.calls
    report["source_hashes_after"] = source_hashes()
    report["workspace_unchanged"] = report["source_hashes_before"] == report["source_hashes_after"]
    if not report["workspace_unchanged"]:
        report["status"] = "failed"
        report["failure_category"] = "SourceChangedDuringReview"
    report["completed_utc"] = datetime.now(timezone.utc).isoformat()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "calls", "max_calls")}, ensure_ascii=False))
    return 0 if report["status"] == "generated_pending_review" else 1


if __name__ == "__main__":
    raise SystemExit(main())
