import json
import pytest

from deskpal.llm.models import LLMResponse, ChatMessage
from deskpal.llm.calls import ModelCallGateway
from deskpal.llm.errors import LLMError
from deskpal.response.composer import ResponseComposer
from deskpal.memory.feedback import INSTRUCTIONS, moment
from deskpal.workflow.models import WorkflowBlocked


class SequenceModel:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def chat(self, messages, *, tools=()):
        self.calls.append((messages, tools))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return LLMResponse(value)


def test_plain_mode_returns_polished_natural_language_once():
    model = SequenceModel('本次查询范围内没有安排。')
    client = ModelCallGateway(model).bind('task', 'expression', lambda: None)
    result = ResponseComposer(client).compose(request='查日程', structured=False,
        learning_receipt={'status': 'completed', 'saved_understandings': []})
    assert result.reply_text == '本次查询范围内没有安排。'
    assert len(model.calls) == 1 and not model.calls[0][1]
    assert 'OUTPUT PROTOCOL' not in model.calls[0][0][0].content


def test_explicit_json_mode_remains_strict_without_plain_fallback():
    model = SequenceModel('ordinary text')
    result = ResponseComposer(model).compose(request='继续', structured=True)
    assert not result.reply_text and result.diagnostics[0].code == 'json_invalid'
    assert len(model.calls) == 1


def test_plain_mode_still_propagates_privacy_failure_after_send():
    model = SequenceModel('must be discarded')
    def guard():
        if model.calls:
            raise WorkflowBlocked('privacy invalidated')
    client = ModelCallGateway(model).bind('task', 'expression', guard)
    with pytest.raises(WorkflowBlocked):
        ResponseComposer(client).compose(request='继续', structured=False)
    assert len(model.calls) == 1


def test_gateway_expression_budget_remains_one():
    model = SequenceModel('one', 'two')
    client = ModelCallGateway(model).bind('task', 'expression', lambda: None)
    client.chat((ChatMessage('user', 'test'),))
    with pytest.raises(LLMError, match='budget'):
        client.chat((ChatMessage('user', 'test'),))
    assert len(model.calls) == 1


def test_feedback_time_instruction_matches_strict_timezone_validation():
    assert '所有时间' in INSTRUCTIONS and '带时区的完整 ISO 时间' in INSTRUCTIONS
    with pytest.raises(ValueError):
        moment('2026-11-01')
    assert moment('2026-11-01T00:00:00+08:00').utcoffset().total_seconds() == 28800


def test_planning_and_learning_agree_forgetting_preserves_chat_and_personality():
    from deskpal.planner.llm_planner import PLANNING_INSTRUCTION
    from deskpal.application.management_tools import management_tools
    assert '不清除聊天、其他记忆或人格' in PLANNING_INSTRUCTION
    assert '不删除聊天或其他人格资料' in INSTRUCTIONS
    assert 'clears related chat' not in PLANNING_INSTRUCTION
    description = next(tool.spec.description for tool in management_tools(None) if tool.spec.name == 'memory_forget')
    assert '保留聊天、其他记忆、人格、执行记录和安排' in description
    assert '会清理相关聊天' not in description


def test_planner_corrects_json_call_proposal_without_executing_text_tools():
    from deskpal.planner.llm_planner import LLMPlanner
    from deskpal.tools.models import ToolCall, ToolSpec
    class Model:
        def __init__(self): self.count = 0
        def chat(self, messages, *, tools=()):
            self.count += 1
            if self.count == 1:
                return LLMResponse('{"action":"plan","calls":[{"tool":"invented"}]}')
            assert 'FORMAT CORRECTION' in messages[0].content
            return LLMResponse('', (ToolCall('echo', {'text': 'actual'}),))
    model = Model()
    plan = LLMPlanner(model).plan('执行', tools=(ToolSpec('echo', 'echo', {}),))
    assert model.count == 2 and [x.name for x in plan.steps] == ['echo']


def test_planner_invalid_text_decision_stops_after_one_correction():
    from deskpal.planner.llm_planner import LLMPlanner
    model = SequenceModel('{"action":"plan"}', '{"action":"plan"}')
    with pytest.raises(ValueError):
        LLMPlanner(model).plan('执行')
    assert len(model.calls) == 2


def test_directory_dot_means_workspace_root_but_dot_segments_still_rejected(tmp_path):
    from deskpal.sandbox.workspace import WorkspaceSandbox
    from deskpal.sandbox.errors import PathViolation
    (tmp_path / 'one.txt').write_text('synthetic', encoding='utf-8')
    sandbox = WorkspaceSandbox(tmp_path)
    assert sandbox.search_files('.', query='one')['files'][0]['path'] == 'one.txt'
    for path in ('./one.txt', '../', 'sub/./folder'):
        with pytest.raises(PathViolation):
            sandbox.resolve_directory(path)
