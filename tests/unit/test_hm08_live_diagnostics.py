"""Offline witnesses for the review script's fixed, redacted error labels."""

import importlib.util
from io import BytesIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from deskpal.llm.errors import LLMError
from deskpal.config import LLMConfig


@pytest.fixture(scope="module")
def diagnostic_module():
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_humanization_live.py"
    spec = importlib.util.spec_from_file_location("hm08_live_diagnostics_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("message,expected", [
    ("LLM HTTP request failed", "http_error"),
    ("LLM network request failed", "network_error"),
    ("LLM request timed out", "timeout"),
    ("LLM response processing failed", "processing_error"),
    ("LLM request failed", "request_error"),
    ("LLM response failed or is incomplete", "incomplete_response"),
    ("LLM output is incomplete", "incomplete_response"),
    ("LLM returned no usable output", "no_usable_output"),
    ("LLM refused the request", "refusal"),
    ("Unsupported LLM message content type", "unsupported_content_type"),
    ("Unsupported LLM output type", "unsupported_output_type"),
    ("Malformed LLM response", "malformed_response"),
    ("Malformed LLM output item", "malformed_response"),
    ("Malformed LLM message output", "malformed_response"),
    ("Malformed LLM message content", "malformed_response"),
    ("LLM response contains invalid JSON", "invalid_response_json"),
    ("LLM response is invalid or exceeds the size limit", "invalid_response_size"),
    ("LLM tool call requires a name and call_id", "invalid_tool_output"),
    ("Duplicate LLM tool call_id", "invalid_tool_output"),
    ("LLM tool arguments must be JSON text", "invalid_tool_output"),
    ("LLM tool arguments must be a JSON object", "invalid_tool_output"),
    ("Native JSON output returned forbidden tool calls", "forbidden_tool_output"),
    ("This model purpose returned forbidden tool calls", "forbidden_tool_output"),
    ("Native JSON output cannot use tools", "forbidden_tool_input"),
    ("This model purpose cannot use tools", "forbidden_tool_input"),
    ("Native JSON output is not enabled", "native_json_disabled"),
    ("Native JSON output is not enabled for this model purpose", "native_json_disabled"),
    ("Model call budget exhausted", "call_budget_exhausted"),
    ("Model input exceeds byte budget", "input_byte_budget_exceeded"),
    ("Model returned an invalid response", "invalid_response_dto"),
    ("Invalid chat message role", "invalid_input"),
    ("LLM data is not valid JSON", "invalid_input"),
    ("Tool results require a preceding matching call_id", "invalid_input"),
])
def test_fixed_provider_failure_classifications(diagnostic_module, message: str, expected: str) -> None:
    assert diagnostic_module.provider_failure_reason(LLMError(message)) == expected


@pytest.mark.parametrize("message", [
    "Authorization: Bearer synthetic-secret; body={private reply}",
    "LLM returned no usable output: synthetic-secret {private reply}",
    "synthetic-secret: LLM request timed out",
    "LLM refused the request\nprivate response body",
    "LLM request timed out ",
    "new error from an unknown remote provider",
    "",
])
def test_unknown_error_text_always_stays_redacted(diagnostic_module, message: str) -> None:
    assert diagnostic_module.provider_failure_reason(LLMError(message)) == "provider_error"
    assert diagnostic_module.provider_failure_reason(RuntimeError(message)) == "provider_error"


def test_classifier_does_not_stringify_arbitrary_exceptions(diagnostic_module) -> None:
    class UntrustedError(LLMError):
        def __str__(self) -> str:
            raise AssertionError("classification must not serialize arbitrary errors")

    assert diagnostic_module.provider_failure_reason(UntrustedError("private body")) == "provider_error"
    assert diagnostic_module.provider_failure_reason(LLMError("LLM request timed out", "secret")) == "provider_error"
    assert diagnostic_module.provider_failure_reason(LLMError({"body": "secret"})) == "provider_error"
    assert diagnostic_module.provider_failure_reason(RuntimeError("LLM request timed out")) == "provider_error"


@pytest.mark.parametrize("response_kind", ["empty_supplement", "no_usable_output", "empty_direct"])
def test_review_script_uses_real_parser_for_optional_supplements(
    diagnostic_module, monkeypatch, tmp_path, response_kind: str,
) -> None:
    """Run S5 offline through the script and real Provider, never an HTTP opener."""
    payloads = []

    class SyntheticTransport:
        def __init__(self, max_calls):
            self.calls = 0
            self.max_calls = max_calls

        def __call__(self, request, *, timeout):
            assert self.calls < self.max_calls
            self.calls += 1
            payload = json.loads(request.data)
            payloads.append(payload)
            expression = json.loads(payload["input"][-1]["content"])
            body = json.dumps({"reply_text": "" if expression["tool_results"] or response_kind == "empty_direct"
                               else "尊重拒绝，尚未执行。", "cues": [], "preference_candidates": []})
            output = [] if response_kind == "no_usable_output" else [
                {"type": "message", "content": [{"type": "output_text", "text": body}]}]
            return BytesIO(json.dumps({"output": output}).encode())

    monkeypatch.setattr(diagnostic_module, "BoundedTransport", SyntheticTransport)
    monkeypatch.setattr(diagnostic_module, "load_config", lambda _: SimpleNamespace(llm=LLMConfig(
        provider="openai", model="synthetic", api_key="synthetic-key", expression_output_format="json_object")))
    monkeypatch.setattr(diagnostic_module, "source_hashes", lambda: {})
    output_path = tmp_path / "synthetic-review.json"
    monkeypatch.setattr(sys, "argv", ["check_humanization_live.py", "--live", "--scenes", "S5",
                                     "--output", str(output_path), "--config", str(tmp_path / "unused.toml")])
    exit_code = diagnostic_module.main()
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert all(item["text"] == {"format": {"type": "json_object"}} for item in payloads)
    if response_kind == "no_usable_output":
        assert exit_code == 1 and report["status"] == "failed"
        assert report["failure_reason"] == "no_usable_output"
        assert report["calls"] == 1 and len(payloads) == 1
        assert "raw_envelope" not in report["samples"][0]
    else:
        assert len(payloads) == report["calls"] == 4
        assert all("补充说明" not in sample["reply"] and sample["reply"].startswith("工具结果")
                   for sample in report["samples"][:3])
        assert all(sample["degraded_reason"] is None for sample in report["samples"][:3])
        if response_kind == "empty_direct":
            assert exit_code == 1 and report["status"] == "failed"
            assert report["samples"][-1]["degraded_reason"] == "response_invalid_envelope"
        else:
            assert exit_code == 0 and report["status"] == "generated_pending_review"
            assert report["samples"][-1]["reply"] == "尊重拒绝，尚未执行。"
