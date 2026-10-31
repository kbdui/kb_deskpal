"""Exercise the real composer/provider boundary without transport or storage."""
import json

import pytest

from deskpal.llm.models import ChatMessage
from deskpal.llm.providers import OfflineLLMProvider
from deskpal.response.composer import ResponseComposer
from deskpal.tools.models import ToolSpec


@pytest.mark.parametrize("structured", [True, False])
def test_offline_composer_returns_reply_without_internal_payload(structured: bool) -> None:
    draft = ResponseComposer(OfflineLLMProvider()).compose(
        request="你好", draft="内部草稿标记", memories=["私密记忆标记"],
        history=[{"role": "user", "content": "历史正文标记"}], structured=structured)
    assert draft.degraded_reason is None
    assert "你好" in draft.reply_text and "离线演示" in draft.reply_text
    for internal in ("私密记忆标记", "历史正文标记", "内部草稿标记", "tool_results", "reply_role", "memory_claims"):
        assert internal not in draft.reply_text


@pytest.mark.parametrize("structured", [True, False])
def test_offline_tool_answer_is_nonblank_and_does_not_repeat_raw_failed_facts(structured: bool) -> None:
    draft = ResponseComposer(OfflineLLMProvider()).compose(request="继续", structured=structured,
        tool_results=[{"success": False, "output": "内部工具正文", "error": "内部错误详情", "tool_name": "echo"}])
    assert draft.degraded_reason is None
    assert draft.reply_text and "离线演示" in draft.reply_text
    assert "内部工具正文" not in draft.reply_text and "内部错误详情" not in draft.reply_text
    assert "已成功" not in draft.reply_text


def test_ordinary_user_json_and_registered_commands_keep_original_behavior() -> None:
    provider = OfflineLLMProvider()
    raw = json.dumps({"request": "原始用户JSON", "tool_results": ["正文"]}, ensure_ascii=False)
    assert raw in provider.chat((ChatMessage("user", raw),)).content
    assert raw in provider.generate(raw)
    through_composer = ResponseComposer(provider).compose(request=raw)
    assert through_composer.degraded_reason is None and raw in through_composer.reply_text
    spec = ToolSpec("echo", "回显", {"type": "object", "properties": {"text": {"type": "string"}}})
    result = provider.chat((ChatMessage("user", "echo 原始参数"),), tools=(spec,))
    assert result.tool_calls[0].arguments == {"text": "原始参数"}

