"""真实临时持久层：前文检索、摘要传递、来源失效及澄清续办。"""

import json

import pytest

from deskpal.application import create_application
from deskpal.config import load_config
from deskpal.conversation.models import ConversationRef
from deskpal.llm.models import LLMResponse
from deskpal.tasks.models import TaskStatus
from tests.integration.agent.test_role_context import app_model, turn


def references(messages):
    text = next(m.content for m in messages if m.content.startswith('[UNTRUSTED HISTORICAL REFERENCES]'))
    return json.loads(text.split('\n', 1)[1])


def install(model, *, understanding=None, planner=None):
    original = model.chat
    seen = dict(understanding=[], expression=[], planning=[], feedback=[])

    def chat(messages, *, tools=()):
        system = messages[0].content
        if system.startswith('[EXPRESSION]'):
            data = json.loads(messages[-1].content)
            seen['expression'].append(data)
            return LLMResponse('请告诉我缺少的那一项。' if data['response_intent'] == 'clarify' else '总算解决了。')
        if system.startswith('[MEMORY_FEEDBACK]'):
            seen['feedback'].append(json.loads(messages[-1].content))
        else:
            seen['planning'].append(messages)
            response = planner(messages) if planner else original(messages, tools=tools)
            if '[FIRST_DECISION]' in system:
                items = references(messages)
                data = dict(history=[item for item in items if item['kind'] in {'user_reference', 'assistant_reference'}],
                    conversation_summary=next((item for item in items if item['kind'] == 'conversation_summary'), None))
                seen['understanding'].append(data)
                result = understanding(data) if understanding else dict(entries=[])
                envelope = json.loads(response.content)
                envelope['context'] = result
                response = LLMResponse(json.dumps(envelope, ensure_ascii=False))
            return response
        return original(messages, tools=tools)

    model.chat = chat
    return seen


def save_summary(app, result, *, dialogue=True):
    conversation = app.conversation_ensure_default()
    content = dict(progress=[], decisions=[], open_questions=[])
    if dialogue:
        content['dialogue_context'] = ['用户之前谈到正在排查雪杉项目的崩溃，尚不知道结果。']
    app._conversation_repository.save_context_summary(conversation['id'], conversation['context_epoch'], 0,
        content, (ConversationRef('dialogue_turn', result.task_id + ':user', 1),))
    return conversation


def test_elliptical_message_gets_one_contextual_retrieval_before_second_decision(app_model):
    app, model = app_model
    memory = app.memory_command('remember', content='雪杉项目的封面是蓝色')
    turn(app, '我在排查雪杉项目的崩溃')
    events = []
    retrieve = app._interaction.retrieve

    def understanding(data):
        events.append('understand')
        assert '雪杉项目' in str(data['history'])
        return dict(entries=[], retrieval=dict(query='雪杉项目的封面是蓝色', sources=['history:0', 'request']))

    def search(query):
        events.append(('retrieve', query))
        return retrieve(query)

    seen = install(model, understanding=understanding)
    app._interaction.retrieve = search
    result = turn(app, '终于解决了')
    assert events == [('retrieve', '终于解决了'), 'understand', ('retrieve', '雪杉项目的封面是蓝色')]
    assert any(memory.id == item['memory_id'] for item in seen['expression'][0]['memory_claims'])
    items = references(seen['planning'][1])
    hint = next(item for item in items if item['kind'] == 'retrieval_context')
    assert hint['instruction'] is False and hint['externally_verified'] is False
    frozen = app._humanization.context(result.task_id, 'planning')['items']
    assert any(ref['id'].endswith(':user') for item in frozen if item['kind'] == 'retrieval_context' for ref in item['refs'])
    assert app.journal.get_plan(result.task_id).memory_refs == ((memory.id, memory.revision),)


@pytest.mark.parametrize('dialogue', [False, True])
def test_same_attributed_summary_reaches_understanding_planning_and_expression(app_model, dialogue):
    app, model = app_model
    first = turn(app, '我在排查雪杉项目的崩溃')
    save_summary(app, first, dialogue=dialogue)
    seen = install(model)
    result = turn(app, '终于解决了')
    understanding = seen['understanding'][0]['conversation_summary']
    planning = next(item for item in references(seen['planning'][0]) if item['kind'] == 'conversation_summary')
    assert understanding == planning == seen['expression'][0]['conversation_summary']
    assert understanding['instruction'] is False and understanding['externally_verified'] is False
    assert understanding['source'] == 'summary'
    frozen = app._humanization.context(result.task_id, 'planning')['items']
    assert any(ref['id'] == first.task_id + ':user' for item in frozen
        if item['kind'] == 'conversation_summary' for ref in item['refs'])


def test_summary_can_supply_search_source_without_becoming_long_term_memory(app_model):
    app, model = app_model
    first = turn(app, '我在排查雪杉项目的崩溃')
    save_summary(app, first)
    before = tuple(app.memory.list_items())
    seen = install(model, understanding=lambda data: dict(entries=[],
        retrieval=dict(query='雪杉项目崩溃', sources=['summary'])))
    result = turn(app, '后来那件事呢')
    frozen = app._humanization.context(result.task_id, 'planning')['items']
    hint = next(item for item in frozen if item['kind'] == 'retrieval_context')
    assert any(ref['id'] == first.task_id + ':user' for ref in hint['refs'])
    assert tuple(app.memory.list_items()) == before


@pytest.mark.parametrize('hint', [
    dict(query='编造的背景', sources=['not-a-source']),
    dict(query='x' * 1025, sources=['request']),
    dict(query=' ', sources=['request']),
])
def test_invalid_retrieval_hint_rechecks_original_sources_before_effects(app_model, hint):
    app, model = app_model
    install(model, understanding=lambda data: dict(entries=[], retrieval=hint))
    queries = []
    retrieve = app._interaction.retrieve
    app._interaction.retrieve = lambda query: (queries.append(query), retrieve(query))[1]
    conversation = app.conversation_ensure_default()
    task = app.submit('今天随便聊聊', conversation_id=conversation['id'], expected_context_epoch=conversation['context_epoch'])
    result = app.wait(task.id, 10)
    assert queries == ['今天随便聊聊']
    assert result.status == TaskStatus.COMPLETED
    assert any('未能解析或保存' in warning for warning in result.warnings)


def test_revoked_history_during_first_decision_stops_before_contextual_retrieval(app_model):
    app, model = app_model
    first = turn(app, '我在排查雪杉项目的崩溃')
    save_summary(app, first)
    queries = []

    def revoke(data):
        with app.store.transaction() as db:
            db.execute("UPDATE dependency_version SET active=0 WHERE kind='dialogue_turn' AND object_id=?",
                (first.task_id + ':user',))
        return dict(entries=[], retrieval=dict(query='雪杉项目', sources=['summary']))

    seen = install(model, understanding=revoke)
    retrieve = app._interaction.retrieve
    app._interaction.retrieve = lambda query: (queries.append(query), retrieve(query))[1]
    conversation = app.conversation_ensure_default()
    task = app.submit('终于解决了', conversation_id=conversation['id'], expected_context_epoch=conversation['context_epoch'])
    result = app.wait(task.id, 10)
    assert result.status == TaskStatus.NEEDS_REVIEW
    assert queries == ['终于解决了'] and len(seen['planning']) == 1 and seen['expression'] == []


def test_summary_is_not_shared_with_another_conversation_and_clear_removes_it(app_model):
    app, model = app_model
    first = turn(app, '我在排查雪杉项目的崩溃')
    conversation = save_summary(app, first)
    seen = install(model)
    other = app.conversation_create('另一个话题', 'new-topic')
    turn(app, '换个话题', other)
    assert seen['expression'][-1]['conversation_summary'] is None
    current = app.conversation_ensure_default()
    app.conversation_clear(conversation['id'], current['revision'], 'clear-continuity')
    turn(app, '重新开始')
    assert seen['understanding'][-1]['conversation_summary'] is None
    assert seen['expression'][-1]['conversation_summary'] is None


def test_clarifications_and_final_answer_are_each_composed_once_without_early_learning(app_model):
    app, model = app_model
    decisions = iter(['clarify', 'clarify', 'answer'])
    seen = install(model, planner=lambda messages: LLMResponse(json.dumps(
        dict(action=next(decisions), reply='需要补充具体对象。'), ensure_ascii=False)))
    conversation = app.conversation_ensure_default()
    task = app.submit('处理那一项', conversation_id=conversation['id'], expected_context_epoch=conversation['context_epoch'])
    for index in range(2):
        result = app.wait(task.id, 10)
        assert result.status == TaskStatus.AWAITING_INPUT
        assert result.response == '请告诉我缺少的那一项。'
        assert seen['feedback'] == []
        assert seen['expression'][-1]['response_intent'] == 'clarify'
        assert seen['expression'][-1]['learning_receipt']['status'] == 'not_run'
        app.answer_task(task.id, f'第{index + 1}次补充', f'answer-{index}')
    result = app.wait(task.id, 10)
    assert result.status == TaskStatus.COMPLETED and result.response == '总算解决了。'
    assert len(seen['expression']) == 3 and len(seen['feedback']) == 1
    assert len(seen['understanding']) == 1
    with app.store.read_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM task_call_attempt WHERE task_id=? AND purpose='expression'", (task.id,)).fetchone()[0] == 3


def test_restart_restores_summary_and_does_not_rerun_understanding_for_pending_task(app_model, tmp_path):
    app, model = app_model
    first = turn(app, '我在排查雪杉项目的崩溃')
    save_summary(app, first)
    decisions = iter(['clarify', 'answer'])
    seen = install(model, planner=lambda messages: LLMResponse(json.dumps(
        dict(action=next(decisions), reply='需要补充对象。'), ensure_ascii=False)))
    conversation = app.conversation_ensure_default()
    task = app.submit('处理那一项', conversation_id=conversation['id'], expected_context_epoch=conversation['context_epoch'])
    assert app.wait(task.id, 10).status == TaskStatus.AWAITING_INPUT
    # 工厂接收应用根配置；app.config 已被解析到角色目录，不能再当成应用根。
    settings = load_config(tmp_path / 'config.toml', environ={})
    assert app.close(timeout=10)
    reopened = create_application(settings)
    try:
        assert reopened.tasks.get(task.id).status == TaskStatus.AWAITING_INPUT
        reopened._interaction.retrieve = lambda query: pytest.fail('续办不能重新检索替换原依据')
        reopened.answer_task(task.id, '处理第一项', 'after-restart')
        assert reopened.wait(task.id, 10).status == TaskStatus.COMPLETED
        assert len(seen['understanding']) == 1
        assert seen['expression'][-1]['conversation_summary'] == seen['expression'][0]['conversation_summary']
    finally:
        assert reopened.close(timeout=10)
