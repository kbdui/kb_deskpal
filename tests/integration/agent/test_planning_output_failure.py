"""真实临时档案验证规划格式失败的用户提示、记录与工具不重放。"""
import json
import pytest

from deskpal.llm.models import LLMResponse
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolCall
from tests.integration.agent.test_role_context import app_model


@pytest.mark.parametrize('after_tool', [False, True])
def test_invalid_planning_keeps_precise_diagnostics_and_existing_results(app_model, after_tool):
    app, model = app_model
    calls = []
    def chat(messages, *, tools=()):
        calls.append(messages)
        if after_tool and len(calls) == 1:
            return LLMResponse(tool_calls=(ToolCall('echo', {'text': '实际工具结果'}, 'echo-once'),))
        return LLMResponse('{"action":"answer","reply":"不能用这段文字伪装成功"}')
    model.chat = chat
    conversation = app.conversation_ensure_default()
    task = app.submit('啊，我刚才很凶吗', conversation_id=conversation['id'], expected_context_epoch=conversation['context_epoch'])
    result = app.wait(task.id, 10)
    assert result.status == TaskStatus.FAILED
    assert ('模型未能正确组织后续答复' if after_tool else '模型返回的答复格式不正确') in result.response
    assert '不能用这段文字伪装成功' not in result.response
    saved = app.tasks.get(task.id)
    assert saved.result['failure_code'] == 'planning_output_invalid'
    assert saved.error == 'Planning output invalid: response/field_set'
    assert len(calls) == (3 if after_tool else 2)
    assert len(app.journal.attempts(task.id)) == (1 if after_tool else 0)
    if after_tool:
        assert result.tool_results[0].success and result.tool_results[0].output == '实际工具结果'
        assert '1 项操作成功' in result.response and '执行记录' in result.response
        assert '重新发送这条消息' not in result.response
    diagnostics = app._humanization.call_diagnostics()
    planning = [row for row in diagnostics if row['task_id'] == task.id and row['purpose'] == 'planning']
    assert len(planning) == len(calls)
    latest = sorted(planning, key=lambda row: row['slot'])[-2:]
    assert {row['metadata']['processing_code'] for row in latest} == {'fields_invalid'}
    page = app._conversation_repository.timeline(conversation['id'])
    assert any(item['kind'] == 'assistant_message' and item['body'] == result.response for item in page['items'])


def test_format_repair_returns_to_normal_expression_without_extra_learning(app_model):
    app, model = app_model
    planning, expression = [], []
    def chat(messages, *, tools=()):
        if messages[0].content.startswith('[EXPRESSION]'):
            expression.append(messages)
            return LLMResponse('没有，我听得懂你的意思。')
        planning.append(messages)
        return LLMResponse('普通正文') if len(planning) == 1 else LLMResponse(json.dumps(
            dict(action='answer', reply='自然接话', organize_memory=False)))
    model.chat = chat
    conversation = app.conversation_ensure_default()
    task = app.submit('啊，我刚才很凶吗', conversation_id=conversation['id'], expected_context_epoch=conversation['context_epoch'])
    result = app.wait(task.id, 10)
    assert result.status == TaskStatus.COMPLETED and result.response == '没有，我听得懂你的意思。'
    assert len(planning) == 2 and len(expression) == 1
    assert app.tasks.get(task.id).result['learning_receipt']['reason'] == 'model_deferred'
