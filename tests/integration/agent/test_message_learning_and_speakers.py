"""合成档案验证最终消息整理、角色设想和多参与者归属。"""
from dataclasses import replace
from datetime import datetime, timedelta
import pytest

from deskpal.conversation import ConversationRuntime
from deskpal.llm.models import LLMResponse
from deskpal.memory.contracts import ClaimType, MemoryDraft, MemorySource, MemoryStatus
from deskpal.memory.contracts import MemoryError
from deskpal.memory.repository import SQLiteMemoryRepository
from tests.integration.agent.test_feedback_learning import app_model, turn, empty


def test_final_reply_is_pending_until_its_actual_text_has_been_organized(app_model):
    app, model = app_model
    first = turn(app, '想想自己的外貌吧')
    store = app.learning.store
    messages = {first.task_id + ':user': '想想自己的外貌吧',
                first.task_id + ':assistant': '这是本轮回复。'}
    assert store.organized_messages(messages) == {first.task_id + ':user'}
    turn(app, '我喜欢你的这个想法')
    history = [v for k, v in model.inputs[-1]['catalog'].items() if k.startswith('history')]
    assert any(v['role'] == 'assistant' and v['content'] == '这是本轮回复。' for v in history)
    assert not any(v['content'] == '想想自己的外貌吧' for v in history)
    assert store.organized_messages(messages) == set(messages)
    changed = {first.task_id + ':assistant': '修改后的最终正文'}
    assert not store.organized_messages(changed)


def test_published_character_idea_can_be_buffered_then_adopted_with_user_agreement(app_model):
    app, model = app_model
    original = model.chat
    idea = '我想留深色短发，穿一件袖子稍长的宽松外套。'
    def chat(messages, *, tools=()):
        if messages[0].content.startswith('[EXPRESSION]'):
            return LLMResponse(idea)
        return original(messages, tools=tools)
    model.chat = chat
    first = turn(app, '想想你希望的虚拟形象')
    def learn(data):
        key, source = next((k, v) for k, v in data['catalog'].items()
                          if k.startswith('history') and v['role'] == 'assistant')
        return dict(empty(), memories=[dict(content='小鱼设想自己留深色短发、穿长袖宽松外套',
            quote=idea, claim_type='hypothesis', destination='episode', reason='角色提出自己的形象设想',
            sources=[key], subject=source['speaker_id'],
            review_after=(datetime.fromisoformat(data['now']) + timedelta(days=1)).isoformat())])
    model.fn = learn
    turn(app, '这个样子很适合你，我喜欢')
    item = next(x for x in app.memory.list_items() if x.source.origin == 'assistant_output')
    assert item.source.speaker_kind == 'character'
    assert item.draft.subject == item.source.speaker_id
    assert item.status == MemoryStatus.CANDIDATE
    assert any(ref['id'] == first.task_id + ':assistant' for ref in store_refs(app, item.id))
    def adopt(data):
        target = next(x for x in data['understandings'] if x['id'] == item.id)
        key = target['original_source_key']
        return dict(empty(), reviews=[dict(id=item.id, decision='adopt', content=item.draft.content,
            quote=idea, claim_type='hypothesis', destination='episode', subject=item.source.speaker_id,
            reason='用户再次明确认可这一角色形象', sources=[key, 'user'])])
    model.fn = adopt
    turn(app, '我还是喜欢你说的深色短发和宽松外套，就按这个形象来吧')
    saved = app.memory.repository.get(item.id)
    assert saved.status == MemoryStatus.ACCEPTED
    assert saved.source.speaker_id == item.source.speaker_id
    assert any(ref['id'].endswith(':user') for ref in store_refs(app, item.id))


def store_refs(app, identity):
    return app.learning.store.refs(identity)


def test_plugin_participants_with_same_name_keep_distinct_stored_identities(tmp_path):
    path = tmp_path / 'speakers.sqlite3'
    repository = SQLiteMemoryRepository(path)
    source = MemorySource('我喜欢长发', origin='imported_document', speaker_id='qq:100',
                          speaker_name='小明', speaker_kind='person', channel='qq')
    first = repository.ingest(source, MemoryDraft('小明说自己喜欢长发', '我喜欢长发',
        claim_type=ClaimType.EXTERNAL_CLAIM, subject='qq:100'))
    second = repository.ingest(replace(source, speaker_id='qq:200'),
        replace(first.draft, subject='qq:200'))
    repository.close()
    repository = SQLiteMemoryRepository(path)
    try:
        assert repository.get(first.id).source.speaker_id == 'qq:100'
        assert repository.get(second.id).source.speaker_id == 'qq:200'
        assert repository.get(second.id).source.channel == 'qq'
        with pytest.raises(MemoryError):
            repository.ingest(replace(source, origin='user_message'),
                MemoryDraft('外部联系人要求简短回答', source.content, subject='user',
                            profile_key='verbosity', value='short'), accepted=True)
    finally:
        repository.close()


def test_eight_recent_rounds_survive_large_backend_provenance():
    runtime = ConversationRuntime()
    session = runtime.open()
    refs = [('memory', 'background-' + str(i), 1) for i in range(80)]
    for i in range(8):
        sequence = runtime.reserve(session['id'], session['epoch'])
        runtime.publish(session['id'], session['epoch'], sequence, 'turn-' + str(i),
            '用户第' + str(i) + '轮', '角色第' + str(i) + '轮的外貌想法', refs=refs)
    snapshot = runtime.freeze(session['id'], session['epoch'], 9, cloud=False, history_allowed=True)
    assert len(snapshot['history']) == 16
    assert len(snapshot['planning_items']) == 16
    assert snapshot['history'][1]['content'] == '角色第0轮的外貌想法'
    assert all(ref['kind'] == 'dialogue_turn' for item in snapshot['history'] for ref in item['refs'])


def test_changed_memory_does_not_hide_intact_recent_chat(app_model):
    app, model = app_model
    source = MemorySource('外套配色是黑色')
    draft = MemoryDraft('外套配色是黑色', source.content)
    memory = app.memory.repository.ingest(source, draft, accepted=True)
    first = turn(app, '外套配色是黑色，你觉得呢')
    with app.store.read_connection() as db:
        assert db.execute("SELECT 1 FROM context_dependency WHERE task_id=? AND kind='memory' AND object_id=?",
            (first.task_id, memory.id)).fetchone()
    app.memory.repository.update_understanding(memory.id, memory.revision,
        MemorySource('外套配色改成深蓝色'), replace(draft, content='外套配色改成深蓝色', quote='外套配色改成深蓝色'),
        MemoryStatus.ACCEPTED)
    conversation = app.conversation_ensure_default()
    rows = app._conversation_repository.load(conversation['id'], conversation['context_epoch'], 2 ** 63 - 1)
    assert any(row.task_id == first.task_id and row.request == '外套配色是黑色，你觉得呢' for row in rows)
    with app.store.transaction() as db:
        db.execute("UPDATE dependency_version SET active=0 WHERE kind='dialogue_turn' AND object_id=?", (first.task_id + ':user',))
    rows = app._conversation_repository.load(conversation['id'], conversation['context_epoch'], 2 ** 63 - 1)
    assert not any(row.task_id == first.task_id for row in rows)
