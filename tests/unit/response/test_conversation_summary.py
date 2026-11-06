"""表达、反馈和持久层共享摘要格式；不调用网络。"""

import json

import pytest

from deskpal.conversation.summary import validate_summary
from deskpal.llm.models import LLMResponse
from deskpal.llm.context import summary_view
from deskpal.memory.feedback import evaluate
from deskpal.response.composer import ResponseComposer


class Model:
    def __init__(self, text='收到。'):
        self.text, self.calls = text, []

    def chat(self, messages, *, tools=()):
        self.calls.append((messages, tools))
        return LLMResponse(self.text)


def summary(**changes):
    return dict(progress=[], decisions=[], open_questions=[], **changes)


@pytest.mark.parametrize('content', [summary(), summary(dialogue_context=['用户分享了搬家的感受。'])])
def test_feedback_accepts_old_and_conversational_summary_without_memory_actions(content):
    model = Model(json.dumps(dict(memories=[], reviews=[], habits=[], emotion=None, summary=content), ensure_ascii=False))
    result = evaluate(model, {'catalog': {}})
    assert result['summary'] == content and result['memories'] == []


def test_feedback_normalizes_single_summary_string_in_one_call():
    content = summary(dialogue_context='用户分享了搬家的感受。')
    model = Model(json.dumps(dict(memories=[], reviews=[], habits=[], emotion=None, summary=content)))
    result = evaluate(model, {'catalog': {}})
    assert result['summary']['dialogue_context'] == [content['dialogue_context']]
    assert len(model.calls) == 1


@pytest.mark.parametrize('content', [
    summary(dialogue_context='不是列表'), summary(dialogue_context=[' ']),
    summary(dialogue_context=['x' * 2001]), summary(dialogue_context=['x'] * 17),
    summary(unrecognized=[]),
])
def test_summary_validation_is_bounded_and_has_no_arbitrary_fields(content):
    with pytest.raises(ValueError):
        validate_summary(content)


def test_expression_rejects_summary_without_attribution_before_calling_provider():
    model = Model()
    result = ResponseComposer(model).compose(request='后来呢', structured=False,
        conversation_summary=dict(kind='conversation_summary', content=summary()))
    assert not result.reply_text and model.calls == []


def test_expression_keeps_summary_attribution_and_clarification_intent_in_plain_mode():
    model = Model('你指的是哪一件事？')
    context = dict(kind='conversation_summary', content=summary(dialogue_context=['用户谈到两件尚未确定的事。']),
        instruction=False, externally_verified=False, refs=[dict(kind='dialogue_turn', id='old:user', revision=1)])
    reply = ResponseComposer(model).compose(request='安排一下', draft='需要确定对象', structured=False,
        conversation_summary=context, response_intent='clarify')
    assert reply.reply_text == '你指的是哪一件事？'
    messages, tools = model.calls[0]
    data = json.loads(messages[-1].content)
    assert data['conversation_summary'] == summary_view(context) and data['response_intent'] == 'clarify'
    assert context['refs'][0]['id'] == 'old:user'
    assert tools == () and len(model.calls) == 1
