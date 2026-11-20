"""模型格式纠错、有限兼容、诊断脱敏和执行边界。"""
import json

import pytest

from deskpal.llm.calls import ModelCallGateway
from deskpal.llm.models import LLMResponse
from deskpal.planner.llm_planner import LLMPlanner
from deskpal.planner.output import PlanningOutputError
from deskpal.tools.models import ToolCall, ToolSpec
from deskpal.workflow.humanization import HumanizationStore
from deskpal.workflow.store import WorkflowStore
from deskpal.workflow.tasks import PersistentTaskManager


VALID = '{"action":"answer","reply":"我明白你的意思。","organize_memory":false}'


@pytest.mark.parametrize('origin', ['proactive', 'plugin', 'unspecified'])
def test_non_local_user_origin_cannot_submit_memory_management(origin):
    from deskpal.planner.output import parse
    value = json.loads(VALID)
    value['memory_requests'] = [dict(action='forget', quote='忘掉这条记忆', targets=['synthetic'])]
    with pytest.raises(PlanningOutputError) as caught:
        parse(LLMResponse(json.dumps(value)), initial=False, origin=origin, max_steps=8)
    assert caught.value.field == 'memory_requests'


class Responses:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.tools = []
        self.diagnostics = []

    def annotate(self, values):
        self.diagnostics.append(values)

    def chat(self, messages, *, tools=()):
        self.calls.append(messages)
        self.tools.append(tools)
        response = self.responses.pop(0)
        return response if isinstance(response, LLMResponse) else LLMResponse(response)


@pytest.mark.parametrize('wrapper', ['```json\n%s\n```', '\ufeff  %s  ', '```\n%s\n```'])
def test_unambiguous_json_wrapper_does_not_cost_another_model_call(wrapper):
    model = Responses(wrapper % VALID)
    plan = LLMPlanner(model).plan('啊，我刚才很凶吗')
    assert plan.reply == '我明白你的意思。' and plan.organize_memory is False
    assert len(model.calls) == 1


@pytest.mark.parametrize('bad,field,rule', [
    ('{"action":"answer","reply":"普通回复"}', 'response', 'field_set'),
    ('{"action":[],"reply":"普通回复","organize_memory":false}', 'action', 'wrong_type'),
    ('{"action":"answer|clarify|quiet","reply":"回复","organize_memory":false}', 'action', 'invalid_choice'),
    ('{"action":"answer","reply":"回复","organize_memory":"false"}', 'organize_memory', 'wrong_type'),
    ('{"action":"clarify","reply":"哪项？","organize_memory":true}', 'organize_memory', 'clarify_learning'),
    ('{"action":"answer","reply":{},"organize_memory":false}', 'reply', 'wrong_type'),
    ('{"action":"answer","reply":"","organize_memory":false}', 'reply', 'empty_text'),
    ('普通正文，不能冒充带有学习决定的规划结果', 'response', 'json_syntax'),
    ('{"action":"answer","action":"quiet","reply":"回复","organize_memory":false}', 'action', 'duplicate_field'),
])
def test_invalid_output_gets_one_targeted_correction_with_explicit_model_decisions(bad, field, rule):
    model = Responses(bad, VALID)
    plan = LLMPlanner(model).plan('请求')
    assert plan.organize_memory is False
    assert len(model.calls) == 2 and '[FORMAT CORRECTION]' in model.calls[1][0].content
    assert field in model.calls[1][0].content
    assert model.diagnostics[0]['processing_field'] == field
    assert model.diagnostics[0]['processing_rule'] == rule


def test_second_invalid_output_is_classified_and_never_falls_back_to_guessing():
    bad = '{"action":"answer","reply":"PRIVATE_TEXT","organize_memory":1}'
    model = Responses(bad, bad)
    with pytest.raises(PlanningOutputError) as caught:
        LLMPlanner(model).plan('请求')
    assert caught.value.field == 'organize_memory' and caught.value.actual == 'number'
    assert 'PRIVATE_TEXT' not in str(caught.value)
    assert len(model.calls) == 2


def test_ordinary_chat_quiet_is_corrected_before_core_failure():
    model = Responses('{"action":"quiet","reply":"","organize_memory":false}', VALID)
    plan = LLMPlanner(model).plan_context('你好', references=({'kind': 'task_origin', 'data': {'kind': 'user'}},), llm=model)
    assert plan.action == 'answer' and len(model.calls) == 2


def test_late_context_is_included_in_the_same_single_correction_budget():
    model = Responses('{"action":"answer","reply":"回复","organize_memory":false,"context":{"entries":[]}}', VALID)
    assert LLMPlanner(model).plan('请求').reply == '我明白你的意思。'
    assert len(model.calls) == 2 and 'context' in model.calls[1][0].content


def test_post_tool_format_retry_only_repairs_the_ending():
    model = Responses('格式不合约定的正文', VALID)
    references = (
        {'kind': 'task_origin', 'data': {'kind': 'user'}},
        {'kind': 'role_context', 'data': {'character_card': {'text': '关心自然接话'}}},
        {'kind': 'context_clock', 'now': '2026-11-20T03:30:00Z'},
        {'kind': 'execution_observations', 'additional_requirements': ['只讨论，不执行'],
         'authorization_rejections': [{'operations': [{'tool_name': 'example'}]}]},
    )
    tool = ToolSpec('example', '示例工具', {'type': 'object'})
    plan = LLMPlanner(model).plan_context('你怎么看？', references=references, tools=(tool,), llm=model)
    assert plan.reply == '我明白你的意思。'
    assert model.tools == [(tool,), ()]
    for messages in model.calls:
        instruction = messages[0].content
        assert '[AFTER_TOOLS]' in instruction and 'authorization_rejections' in instruction
        assert '[FIRST_DECISION]' not in instruction and '[PROACTIVE]' not in instruction
        assert '只讨论，不执行' in messages[-2].content

    model = Responses('格式不合约定的正文', LLMResponse(tool_calls=(ToolCall('example', {}, 'call'),)))
    with pytest.raises(PlanningOutputError) as caught:
        LLMPlanner(model).plan_context('你怎么看？', references=references, tools=(tool,), llm=model)
    assert caught.value.field == 'tool_calls'
    assert model.tools == [(tool,), ()]


@pytest.mark.parametrize('bad', ['前后说明：' + VALID, VALID + '\n' + VALID])
def test_ambiguous_prose_or_multiple_objects_is_not_silently_extracted(bad):
    model = Responses(bad, VALID)
    LLMPlanner(model).plan('请求')
    assert len(model.calls) == 2
    assert model.diagnostics[0]['processing_code'] == 'json_invalid'
    assert model.diagnostics[0]['processing_json_line'] > 0


def test_persistent_diagnostics_distinguish_successful_http_and_failed_protocol(tmp_path):
    store = WorkflowStore(tmp_path / 'workflow.sqlite3')
    try:
        records = HumanizationStore(store)
        task = PersistentTaskManager(store).submit('请求', command_id='request')
        model = Responses('{"action":"answer","reply":"PRIVATE_REPLY","PRIVATE_FIELD":"secret"}', VALID)
        gateway = ModelCallGateway(model, reserve=records.reserve_call, finish=records.finish_call, diagnose=records.diagnose_call)
        provider = gateway.bind(task.id, 'planning', lambda: None)
        LLMPlanner(provider).plan('请求')
        rows = sorted(records.call_diagnostics(), key=lambda row: row['slot'])
        first, second = [row['metadata'] for row in rows]
        assert first['error_code'] == 'ok' and first['processing_code'] == 'fields_invalid'
        assert first['processing_missing_fields'] == ['organize_memory']
        assert first['processing_extra_fields'] == ['unknown']
        assert first['planning_attempt'] == 1 and first['planning_outcome'] == 'invalid'
        assert second['planning_attempt'] == 2 and second['planning_outcome'] == 'corrected'
        assert 'PRIVATE' not in json.dumps(rows) and 'secret' not in json.dumps(rows)
    finally:
        store.close()
