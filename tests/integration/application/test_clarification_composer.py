"""真实合成档案验证澄清续办、聊天正文、状态竞争和回执。"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from deskpal.llm.models import LLMResponse
from deskpal.tasks.models import TaskStatus
from deskpal.workflow.models import WorkflowBlocked
from tests.integration.agent.test_role_context import app_model


def clarify(app, model, count=1):
    decisions = iter(['clarify'] * count + ['answer'])

    def chat(messages, *, tools=()):
        if messages[0].content.startswith('[EXPRESSION]'):
            pytest.fail('澄清和普通答复不应调用最终收尾')
        action = next(decisions)
        reply = '要处理哪个对象？' if action == 'clarify' else '已经处理好了。'
        return LLMResponse(json.dumps(dict(action=action, reply=reply, organize_memory=False)))

    model.chat = chat
    conversation = app.conversation_ensure_default()
    task = app.submit('处理这件事', conversation_id=conversation['id'], expected_context_epoch=conversation['context_epoch'])
    assert app.wait(task.id, 10).status == TaskStatus.AWAITING_INPUT
    return task, conversation


def test_two_clarifications_keep_all_messages_and_retry_does_not_duplicate(app_model):
    app, model = app_model
    task, conversation = clarify(app, model, count=2)
    for index in range(2):
        page = app._conversation_repository.timeline(conversation['id'])
        waiting = page['conversation']['awaiting_input']
        assert len(waiting) == 1 and waiting[0]['task_id'] == task.id
        assert waiting[0]['question'] == '要处理哪个对象？'
        params = dict(task_id=task.id, content=f'第{index}个对象', command_id=f'answer-{index}',
            conversation_id=conversation['id'], expected_context_epoch=conversation['context_epoch'],
            expected_revision=waiting[0]['revision'])
        receipt = app.answer_task(**params)
        assert receipt['accepted']
        assert app.command_status(params['command_id'])['accepted']
        result = app.wait(task.id, 10)
        assert result.status == (TaskStatus.AWAITING_INPUT if index == 0 else TaskStatus.COMPLETED)
        assert app.answer_task(**params)['accepted']  # 已受理命令重试不受后来状态变化影响。
    page = app._conversation_repository.timeline(conversation['id'])
    bodies = [item['body'] for item in page['items'] if item['kind'] != 'task_card' and item['body']]
    assert bodies == ['处理这件事', '要处理哪个对象？', '第0个对象', '要处理哪个对象？', '第1个对象', '已经处理好了。']
    assert page['conversation']['awaiting_input'] == []
    with app.store.read_connection() as db:
        assert db.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 1
    history = app._conversation_repository.load(conversation['id'], conversation['context_epoch'], 100)
    assert '第0个对象' in history[-1].request and '第1个对象' in history[-1].request
    assert '要处理哪个对象？' not in history[-1].request
    assert app._conversation_repository.load(conversation['id'], conversation['context_epoch'], 100, search_terms=('第1个对象',))
    deferred = app.learning.materials._entry(dict(kind='dialogue_turn', id=task.id + ':user', revision=1))
    assert '第1个对象' in deferred['content'] and '要处理哪个对象？' not in deferred['content']


def test_stale_target_and_wrong_conversation_preserve_waiting_task(app_model):
    app, model = app_model
    task, conversation = clarify(app, model)
    params = dict(task_id=task.id, content='不可误发', command_id='stale-answer',
        conversation_id=conversation['id'], expected_context_epoch=conversation['context_epoch'],
        expected_revision=app.tasks.get(task.id).revision + 1)
    with pytest.raises(WorkflowBlocked):
        app.answer_task(**params)
    other = app._conversation_repository.create('另一个会话', 'other-chat')
    params.update(conversation_id=other['id'], expected_revision=app.tasks.get(task.id).revision)
    with pytest.raises(WorkflowBlocked):
        app.answer_task(**params)
    assert app.tasks.get(task.id).status == TaskStatus.AWAITING_INPUT
    assert not app.command_status('stale-answer')['known']
    with app.store.read_connection() as db:
        assert db.execute('SELECT COUNT(*) FROM task_amendment').fetchone()[0] == 0


def test_new_topic_does_not_resume_waiting_task_and_clear_erases_exchanges(app_model):
    app, model = app_model
    task, conversation = clarify(app, model)
    new = app.submit('换个话题', conversation_id=conversation['id'], expected_context_epoch=conversation['context_epoch'])
    assert app.wait(new.id, 10).status == TaskStatus.COMPLETED
    assert app.tasks.get(task.id).status == TaskStatus.AWAITING_INPUT
    latest = app._conversation_repository.get(conversation['id'])
    app._conversation_service.cleanup(conversation['id'], latest['revision'], 'clear-chat', delete=False)
    with app.store.read_connection() as db:
        assert not db.execute("SELECT 1 FROM conversation_exchange WHERE body!=''").fetchone()


def test_restart_retains_question_and_answer_resumes_original_task(app_model, tmp_path):
    from deskpal.application import create_application
    from deskpal.config import load_config
    app, model = app_model
    task, conversation = clarify(app, model)
    assert app.close(timeout=10)
    reopened = create_application(load_config(tmp_path / 'config.toml', environ={}))
    try:
        page = reopened._conversation_repository.timeline(conversation['id'])
        target = page['conversation']['awaiting_input'][0]
        assert target['task_id'] == task.id and target['question'] == '要处理哪个对象？'
        assert any(item['body'] == target['question'] for item in page['items'])
        reopened.answer_task(task.id, '处理甲', 'restart-answer', conversation_id=conversation['id'],
            expected_context_epoch=conversation['context_epoch'], expected_revision=target['revision'])
        assert reopened.wait(task.id, 10).status == TaskStatus.COMPLETED
    finally:
        assert reopened.close(timeout=10)


def test_schema_eight_upgrade_keeps_original_timeline_and_backup(tmp_path):
    import sqlite3
    from deskpal.workflow.store import WorkflowStore
    from deskpal.workflow.tasks import PersistentTaskManager
    from deskpal.conversation.repository import ConversationRepository
    path = tmp_path / 'workflow.sqlite3'
    store = WorkflowStore(path)
    repository = ConversationRepository(store)
    conversation = repository.ensure_default()
    task = PersistentTaskManager(store).submit('升级前原文', command_id='original')
    repository.bind(task.id, conversation['id'], conversation['context_epoch'], command_id='original')
    # 去掉版本9唯一新增的表和版本号，构造结构完整的版本8。
    with store.transaction() as db:
        db.execute('DROP TABLE conversation_exchange')
        db.execute('UPDATE schema_version SET version=8')
    store.close()
    reopened = WorkflowStore(path)
    try:
        assert any(item['body'] == '升级前原文' for item in ConversationRepository(reopened).timeline(conversation['id'])['items'])
        with reopened.read_connection() as db:
            assert db.execute('SELECT version FROM schema_version').fetchone()[0] == 9
    finally:
        reopened.close()
    from deskpal.maintenance.archives import _schema
    db = sqlite3.connect(path)
    try:
        assert _schema(db, 'workflow.sqlite3') == 9
    finally:
        db.close()
    backups = list(tmp_path.glob('workflow.sqlite3.schema-8-*.backup'))
    assert len(backups) == 1
    db = sqlite3.connect(backups[0])
    try:
        assert db.execute('SELECT version FROM schema_version').fetchone()[0] == 8
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_exchange'").fetchone()
    finally:
        db.close()


@pytest.mark.parametrize('surface', ['studio', 'mobile'])
def test_composer_target_selection_survives_refresh_and_never_falls_back_on_stale(surface):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js unavailable')
    root = Path(__file__).resolve().parents[3]
    path = root / ('src/deskpal/gui/assets/studio/studio.js' if surface == 'studio' else 'src/deskpal/mobile/assets/mobile.js')
    source = path.read_text(encoding='utf-8')
    start = source.index('function replyTarget(')
    end = source.index('function selectReplyTarget(', start)
    function = source[start:end]
    variable = 'state.chat' if surface == 'studio' else 'S.conversation'
    key = 'awaitingInput' if surface == 'studio' else 'awaiting_input'
    setup = "const state={chat:{id:'chat'}};const S={id:'chat',conversation:{},loaded:true};const replyTargets=new Map();"
    script = setup + function + f"""
    const assert=require('node:assert/strict');
    const data={variable};
    data.{key}=[{{task_id:'one',revision:3,question:'哪一项？'}}];
    assert.equal(replyTarget().selection.target.task_id,'one');
    replyTarget().selection.target='';
    assert.equal(replyTarget().selection.target,'');
    replyTargets.clear();
    data.{key}.push({{task_id:'two',revision:7}});
    assert.equal(replyTarget().selection.target,null);
    replyTarget().selection.target={{task_id:'one',revision:3}};
    data.{key}=[];
    assert.equal(replyTarget().selection.stale,true);
    assert.equal(replyTarget().selection.target.task_id,'one');
    """
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True, timeout=10)
