"""Provider budgets are sent over the wire, not just checked after generation."""

import io
import json

import pytest

from deskpal.llm.errors import LLMError
from deskpal.llm.providers import OpenAILLMProvider


@pytest.mark.parametrize("budget", [True, 0, -1, 1.5, "10"])
def test_invalid_generation_budget_is_rejected(budget):
    with pytest.raises(LLMError):
        OpenAILLMProvider(model="test", api_key="test-only", max_output_tokens=budget)


def test_generation_budget_and_timeout_reach_transport():
    captured = []

    def transport(request, *, timeout):
        captured.append((json.loads(request.data), timeout))
        return io.BytesIO(json.dumps({"output": [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "ok"}]}]}).encode())

    provider = OpenAILLMProvider(model="test", api_key="test-only", transport=transport,
                                 timeout_seconds=7, max_output_tokens=256)
    assert provider.generate("synthetic") == "ok"
    assert captured[0][0]["max_output_tokens"] == 256
    assert captured[0][0]["store"] is False
    assert captured[0][1] == 7
