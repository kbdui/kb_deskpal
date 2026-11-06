"""真实存储的合成旧库升级；不访问用户档案、模型或配置。"""
from copy import deepcopy
import sqlite3

import pytest

from deskpal.companion import CompanionRuntime, CompanionStorageError
from deskpal.companion.repository import encode, initial
from deskpal.maintenance import archives
from deskpal.memory.repository import SQLiteMemoryRepository
from deskpal.scheduling import SchedulingService
from deskpal.workflow import WorkflowStore
from deskpal.workflow.migrations import migrate_schema_1, migrate_schema_2
from deskpal.workflow.schema_v4 import FIELDS
from deskpal.workflow.store import _SCHEMA


# 冻结旧版无约束扩展，不从新版建表 SQL 反推旧版 fixture。
OLD_EXTENSIONS = (
    "CREATE TABLE decision_round(task_id TEXT,round INTEGER,hash TEXT,body TEXT,PRIMARY KEY(task_id,round))",
    "CREATE TABLE task_amendment(task_id TEXT,sequence INTEGER,command_id TEXT UNIQUE,content TEXT,consumed_round INTEGER,PRIMARY KEY(task_id,sequence))",
    "CREATE TABLE task_origin(task_id TEXT PRIMARY KEY,kind TEXT,basis TEXT)",
    "CREATE TABLE context_history(task_id TEXT,purpose TEXT,generation INTEGER,digest TEXT,items TEXT,refs TEXT,PRIMARY KEY(task_id,purpose,generation))",
    "CREATE TABLE context_archive_source(task_id TEXT,kind TEXT,object_id TEXT,revision INTEGER,PRIMARY KEY(task_id,kind,object_id,revision))",
    "CREATE INDEX context_archive_source_object ON context_archive_source(kind,object_id)",
    "CREATE TABLE conversation_context_summary(conversation_id TEXT PRIMARY KEY,epoch INTEGER,revision INTEGER,content TEXT,refs TEXT,use_policy TEXT)",
)


def old_workflow(path, *, populated=True):
    with sqlite3.connect(path) as db:
        for statement in _SCHEMA:
            db.execute(statement)
        migrate_schema_1(db)
        migrate_schema_2(db)
        if not populated:
            return {}
        for statement in OLD_EXTENSIONS:
            db.execute(statement)
        db.execute("INSERT INTO task(id,request,status,created_at,updated_at) VALUES('task','旧请求','Waiting','created','updated')")
        db.execute("INSERT INTO immutableplan VALUES('plan','task',?,?,0)", ('a' * 64, '{ "steps" : [] }'))
        db.execute("INSERT INTO decision_round VALUES('task',1,?,?)", ('b' * 64, '{ "reply" : "原字节" }'))
        db.execute("INSERT INTO task_amendment VALUES('task',1,'cmd1','已消费',1)")
        db.execute("INSERT INTO task_amendment VALUES('task',2,'cmd2','待消费',NULL)")
        db.execute("INSERT INTO task_origin VALUES('task','proactive','真实事件依据')")
        db.execute("INSERT INTO context_history VALUES('task','planning',1,?,?,'[]')", ('c' * 64, '[ {"old":true} ]'))
        db.execute("INSERT INTO context_archive_source VALUES('task','memory','memory-id',3)")
        conversation = db.execute("SELECT id FROM conversation").fetchone()[0]
        db.execute("INSERT INTO conversation_context_summary VALUES(?,2,3,?,'[]','local_only')", (conversation, '{ "progress":[] }'))
        return {name: db.execute(f"SELECT * FROM {name}").fetchall() for name in FIELDS}


def role_state():
    state = initial()
    state.update(revision=7, reset_epoch=2)
    state['card'].update(text='来自海边的角色', revision=4, origin='user', use_policy='local_only')
    for index, layer in enumerate(('habits', 'user_understanding', 'interaction_approach')):
        state[layer] = [dict(id=f'record-{index}', content=f'实际内容{index}', reason='模型记录的理由',
            nature='inferred', scope='conversation', created_at=123., expires_at=567.,
            use_policy='local_only', source={'task_id': 'source-task', 'conversation_id': 'source-conversation', 'epoch': 2},
            model_note={'retained': True})]
    state['emotion'] = dict(description_intensity='欣慰，轻微', reason_sources='共同完成工作',
        time_scope='当前交流', uncertainty='仍需确认', source={'task_id': 'source-task'},
        expires_at=789., use_policy='local_only')
    return state


def old_role(path, state):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE role_state(id INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
        db.execute('CREATE TABLE role_receipt(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL)')
        db.execute('INSERT INTO role_state VALUES(1,?)', (encode(state),))
        db.execute("INSERT INTO role_receipt VALUES('original-command','original-fingerprint')")
        db.execute('PRAGMA user_version=3')


def test_workflow_upgrade_preserves_bytes_links_and_pending_work(tmp_path):
    path = tmp_path / 'workflow.sqlite3'
    before = old_workflow(path)
    store = WorkflowStore(path)
    try:
        with store.read_connection() as db:
            assert db.execute('SELECT version FROM schema_version').fetchone()[0] == 7
            for name, rows in before.items():
                assert [tuple(row) for row in db.execute(f'SELECT * FROM {name}')] == rows
            assert tuple(db.execute('SELECT hash,canonical_json FROM immutableplan').fetchone()) == ('a' * 64, '{ "steps" : [] }')
            assert db.execute('SELECT status FROM task').fetchone()[0] == 'Waiting'
            assert db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 0
            assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        store.close()
    backups = list(tmp_path.glob('*.backup'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute('SELECT version FROM schema_version').fetchone()[0] == 3
    WorkflowStore(path).close()
    assert list(tmp_path.glob('*.backup')) == backups


def test_workflow_without_old_optional_extensions_is_upgraded(tmp_path):
    path = tmp_path / 'workflow.sqlite3'
    old_workflow(path, populated=False)
    store = WorkflowStore(path)
    try:
        with store.read_connection() as db:
            for name in FIELDS:
                assert db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.parametrize('fault', ['orphan', 'columns', 'json'])
def test_workflow_failed_upgrade_rolls_back_every_table(tmp_path, fault):
    path = tmp_path / 'workflow.sqlite3'
    old_workflow(path)
    with sqlite3.connect(path) as db:
        if fault == 'orphan':
            db.execute("UPDATE task_origin SET task_id='missing'")
        elif fault == 'columns':
            db.execute('ALTER TABLE context_history ADD COLUMN unrecognized TEXT')
        else:
            db.execute("UPDATE decision_round SET body='invalid-json'")
        before = {name: db.execute(f'SELECT * FROM {name}').fetchall() for name in FIELDS}
    with pytest.raises((ValueError, sqlite3.Error)):
        WorkflowStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT version FROM schema_version').fetchone()[0] == 3
        assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'upgrading_v4_%'").fetchall()
        for name, rows in before.items():
            assert db.execute(f'SELECT * FROM {name}').fetchall() == rows


@pytest.mark.parametrize('missing', ['context_history', 'task_amendment_pending'])
def test_workflow_declared_current_schema_is_not_silently_repaired(tmp_path, missing):
    path = tmp_path / 'workflow.sqlite3'
    WorkflowStore(path).close()
    kind = 'INDEX' if missing.endswith('pending') else 'TABLE'
    with sqlite3.connect(path) as db:
        db.execute(f'DROP {kind} {missing}')
    with pytest.raises(ValueError):
        WorkflowStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT name FROM sqlite_master WHERE name=?', (missing,)).fetchone() is None


def test_workflow_enforces_task_parent_and_valid_origin(tmp_path):
    path = tmp_path / 'workflow.sqlite3'
    old_workflow(path)
    store = WorkflowStore(path)
    try:
        with pytest.raises(sqlite3.IntegrityError), store.transaction() as db:
            db.execute("INSERT INTO task_origin VALUES('missing','user','reason')")
        with pytest.raises(sqlite3.IntegrityError), store.transaction() as db:
            db.execute("UPDATE task_origin SET kind='model-granted-permission'")
        with store.read_connection() as db:
            assert db.execute('SELECT kind FROM task_origin').fetchone()[0] == 'proactive'
    finally:
        store.close()


def test_role_upgrade_preserves_all_layers_sources_and_receipts(tmp_path):
    path = tmp_path / 'companion.sqlite3'
    state = role_state()
    old_role(path, state)
    for _ in range(2):
        runtime = CompanionRuntime(path)
        try:
            assert runtime._repository.load() == state
            db = runtime._repository.connection
            assert db.execute('PRAGMA user_version').fetchone()[0] == 4
            assert db.execute("SELECT name FROM sqlite_master WHERE name='role_state'").fetchone() is None
            assert db.execute('SELECT * FROM role_receipt').fetchall() == [('original-command', 'original-fingerprint')]
            assert db.execute('SELECT source_task,source_conversation FROM role_record').fetchall() == [('source-task', 'source-conversation')] * 3
        finally:
            runtime.close()


@pytest.mark.parametrize('fault', ['duplicate-id', 'invalid-content', 'invalid-source'])
def test_role_invalid_legacy_data_rolls_back_without_default_replacement(tmp_path, fault):
    path = tmp_path / 'companion.sqlite3'
    state = role_state()
    if fault == 'duplicate-id':
        state['habits'][0]['id'] = state['user_understanding'][0]['id']
    elif fault == 'invalid-content':
        state['habits'][0]['content'] = ' '
    else:
        state['habits'][0]['source'] = []
    old_role(path, state)
    with pytest.raises(CompanionStorageError):
        CompanionRuntime(path)
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3
        assert db.execute('SELECT payload FROM role_state').fetchone()[0] == encode(state)
        assert db.execute("SELECT name FROM sqlite_master WHERE name='role_meta'").fetchone() is None
        assert db.execute('SELECT * FROM role_receipt').fetchall() == [('original-command', 'original-fingerprint')]


def test_role_partial_update_and_failed_save_are_atomic(tmp_path):
    path = tmp_path / 'companion.sqlite3'
    state = role_state()
    old_role(path, state)
    runtime = CompanionRuntime(path)
    try:
        repo = runtime._repository
        edited = deepcopy(state)
        edited['user_understanding'] = []
        edited['habits'][0]['reason'] = '修订后的理由'
        edited['revision'] += 1
        with repo.transaction():
            repo.save(edited)
        assert repo.load() == edited
        with pytest.raises(RuntimeError), repo.transaction():
            repo.save(state)
            raise RuntimeError('interruption after all writes')
        assert repo.load() == edited
        with pytest.raises(sqlite3.IntegrityError), repo.transaction():
            repo.connection.execute("UPDATE role_record SET scope='all-characters'")
        assert repo.load() == edited
    finally:
        runtime.close()


@pytest.mark.parametrize('missing', ['current_emotion', 'role_record_task'])
def test_role_current_schema_corruption_is_rejected(tmp_path, missing):
    path = tmp_path / 'companion.sqlite3'
    CompanionRuntime(path).close()
    with sqlite3.connect(path) as db:
        db.execute(f"DROP {'INDEX' if missing.endswith('task') else 'TABLE'} {missing}")
    with pytest.raises(CompanionStorageError):
        CompanionRuntime(path)


def test_role_command_replay_after_normalized_restart(tmp_path):
    path = tmp_path / 'companion.sqlite3'
    runtime = CompanionRuntime(path)
    runtime.command('character_card', expected_revision=1, text='稳定的角色卡', command_id='same-command')
    expected = runtime._repository.load()
    runtime.close()
    runtime = CompanionRuntime(path)
    try:
        runtime.command('character_card', expected_revision=1, text='稳定的角色卡', command_id='same-command')
        assert runtime._repository.load() == expected
    finally:
        runtime.close()


def test_old_archive_inspection_is_readonly_and_restored_stores_upgrade(tmp_path):
    source = tmp_path / 'old-data'
    source.mkdir()
    expected_workflow = old_workflow(source / 'workflow.sqlite3')
    expected_role = role_state()
    old_role(source / 'companion.sqlite3', expected_role)
    SQLiteMemoryRepository(source / 'memory.sqlite3').close()
    with sqlite3.connect(source / 'memory.sqlite3') as db:
        for table in ('learning_summary_outbox', 'learning_role_outbox', 'learning_record', 'learning_link', 'learning_job'):
            db.execute(f'DROP TABLE {table}')
        db.execute('PRAGMA user_version=2')
    SchedulingService(source / 'scheduling.sqlite3').close()
    archive = tmp_path / 'old.zip'
    archives.create_backup(source, archive, acknowledge_sensitive_data=True)
    original = archive.read_bytes()
    inspected = archives.inspect_backup(archive)
    versions = {entry['name']: entry['schema_version'] for entry in inspected['manifest']['databases']}
    assert versions == {'workflow.sqlite3': 3, 'companion.sqlite3': 3, 'memory.sqlite3': 2, 'scheduling.sqlite3': 1}
    restored = tmp_path / 'restored'
    archives.restore_backup(archive, restored, acknowledge_data_rollback=True)
    assert archive.read_bytes() == original
    with sqlite3.connect(restored / 'workflow.sqlite3') as db:
        assert db.execute('SELECT version FROM schema_version').fetchone()[0] == 3
    with sqlite3.connect(restored / 'companion.sqlite3') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3
    # 只有正式打开恢复后的存储才升级，检查和恢复不会偷偷启动后端。
    workflow = WorkflowStore(restored / 'workflow.sqlite3')
    role = CompanionRuntime(restored / 'companion.sqlite3')
    try:
        assert role._repository.load() == expected_role
        with workflow.read_connection() as db:
            for name, rows in expected_workflow.items():
                assert [tuple(row) for row in db.execute(f'SELECT * FROM {name}')] == rows
    finally:
        role.close()
        workflow.close()
    # 原源目录仍然是旧版，未被升级；已发布归档也保持原字节。
    with sqlite3.connect(source / 'companion.sqlite3') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3
    assert archive.read_bytes() == original


def test_schema_six_migrates_existing_summary_sources_and_keeps_summary(tmp_path):
    import json
    from deskpal.workflow.schema_v4 import migrate_schema_3
    from deskpal.workflow.schema_v5 import migrate_schema_4
    from deskpal.workflow.schema_v6 import migrate_schema_5

    path = tmp_path / 'workflow.sqlite3'
    old_workflow(path, populated=False)
    summary = json.dumps(dict(progress=['existing summary'], decisions=[], open_questions=[]))
    refs = json.dumps([dict(kind='dialogue_turn', id='older:user', revision=1, use_policy='local_only')])
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        migrate_schema_3(db)
        migrate_schema_4(db)
        migrate_schema_5(db)
        conversation = db.execute('SELECT id FROM conversation').fetchone()[0]
        db.execute('INSERT INTO conversation_context_summary VALUES(?,0,4,?,?,?)',
                   (conversation, summary, refs, 'local_only'))

    store = WorkflowStore(path)
    try:
        with store.read_connection() as db:
            assert db.execute('SELECT version FROM schema_version').fetchone()[0] == 7
            saved = db.execute('SELECT revision,content,refs,use_policy FROM conversation_context_summary').fetchone()
            assert tuple(saved) == (4, summary, '[]', 'local_only')
            source = db.execute('SELECT * FROM conversation_summary_source').fetchone()
            assert tuple(source) == (conversation, 0, 'dialogue_turn', 'older:user', 1, 'local_only')
    finally:
        store.close()


def test_schema_three_memory_upgrade_preserves_existing_items(tmp_path):
    from deskpal.memory.contracts import MemoryDraft, MemorySource
    path = tmp_path / 'memory.sqlite3'
    with SQLiteMemoryRepository(path) as repo:
        entry = repo.ingest(MemorySource('原有记忆'), MemoryDraft('原有记忆', '原有记忆'), accepted=True)
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE learning_summary_outbox')
        db.execute('PRAGMA user_version=3')
    with SQLiteMemoryRepository(path) as repo:
        assert repo.get(entry.id) == entry
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 4
        assert db.execute('SELECT COUNT(*) FROM learning_summary_outbox').fetchone()[0] == 0
