"""合成模型检查真实整理出站：精简视图不改角色资料及学习来源。"""
import json

from deskpal.llm.models import LLMResponse
from deskpal.tools.models import ToolCall
from tests.integration.agent.test_feedback_learning import app_model, draft, turn
from deskpal.tasks.models import TaskStatus


def test_role_reason_is_removed_only_from_learning_not_storage_or_other_stages(app_model):
    app, model = app_model
    model.fn = lambda data: dict(habits=[dict(content='称呼我为小林', reason='用户本轮明确提出长期称呼要求',
        sources=['user'], explicit=True)])
    turn(app, '以后请称呼我为小林')
    before = app.core.companion_runtime.interaction_snapshot()
    assert before['habits'] and before['habits'][0]['reason']
    model.fn = lambda data: {}
    turn(app, '我们继续聊')
    projected = model.inputs[-1]['role']['learned_habits']
    assert projected and projected[0]['content'] == '称呼我为小林' and 'reason' not in projected[0]
    assert model.expressions[-1]['role_context']['learned_habits'][0]['reason']
    assert app.core.companion_runtime.interaction_snapshot() == before


def test_proactive_learning_uses_registered_thought_and_saves_it_without_duplicate_draft(app_model):
    app, model = app_model
    original = model.chat
    plans = []
    thought = '我想从观察植物开始，但现在还没有真正种植。'
    def chat(messages, *, tools=()):
        if messages[0].content.startswith('[PLANNING]'):
            plans.append(messages)
            if len(plans) == 1:
                return LLMResponse('', (ToolCall('reflection_record',
                    dict(topic='自己的兴趣', content=thought, reason='值得继续探索'), 'thought'),))
            return LLMResponse(json.dumps(dict(action='answer', reply='我想试着观察植物。', organize_memory=True)))
        return original(messages, tools=tools)
    model.chat = chat
    def learn(data):
        assert 'assistant' not in data['catalog'] and 'user' not in data['catalog']
        assert data['current_exchange']['assistant_source'] is None
        source = data['catalog']['reflection']
        assert source['content'] == thought and source['published'] is False
        row = draft('角色想尝试观察植物，尚未种植', thought, sources=['reflection'])
        row.update(claim_type='hypothesis', subject=source['speaker_id'])
        return dict(memories=[row])
    model.fn = learn
    task = app.submit_proactive('想想自己的兴趣', '合成机会', 'compact-thought')
    result = app.wait(task.id, 10)
    assert result.status == TaskStatus.COMPLETED
    assert app.learning.store.job('turn:' + task.id)['status'] == 'completed'
    saved, = app.memory.list_items()
    assert saved.source.origin == 'character_reflection' and saved.draft.quote == thought
    assert len(model.inputs) == 1
