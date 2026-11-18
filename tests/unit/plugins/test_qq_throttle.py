"""合成时钟验证群聊等待、直接呼叫开关、积压与重启；不联网、不调用模型。"""
from contextlib import closing
from hashlib import sha256
import sqlite3
from threading import RLock
from types import SimpleNamespace

import pytest

from deskpal.plugins.qq.runtime import QqRuntime, DEFAULT
from deskpal.plugins.qq.store import QqStore, SCHEMA, SOCIAL_SCHEMA
from deskpal.plugins.qq.throttle import started
from deskpal.workflow.store import WorkflowStore, canonical
from deskpal.workflow.tasks import PersistentTaskManager


@pytest.fixture
def store(tmp_path):
    value = QqStore(tmp_path / 'qq.sqlite3')
    try:
        yield value
    finally:
        value.close()


def message(store, identity, at, *, thread='group:54321', direct=False):
    store.receive('99999', thread, identity, 'qq:12345', '甲', '内容' + identity,
        '2026-11-15T00:00:00+00:00', outgoing=False, eligible=True, direct_call=direct, received_at=at)


def begin(store, work, clock):
    with store.transaction() as db:
        db.execute("UPDATE qq_work SET state='running' WHERE id=?", (work['id'],))
        started(db, work['id'], clock)


def complete(store, work):
    with store.transaction() as db:
        db.execute("UPDATE qq_work SET state='completed' WHERE id=?", (work['id'],))


def test_first_message_waits_ten_seconds_without_continuous_debounce(store):
    message(store, '1', 100)
    assert store.reserve_work('99999', clock=109) is None
    message(store, '2', 109)
    work = store.reserve_work('99999', clock=110)
    assert work is not None
    assert len(store.rows('SELECT sequence FROM qq_message WHERE work_id=?', (work['id'],))) == 2


@pytest.mark.parametrize('interval', [30, 60, 180])
def test_normal_messages_obey_configured_interval_even_after_quiet_finish(store, interval):
    config = dict(DEFAULT, group_interval_seconds=interval)
    message(store, '1', 100)
    first = store.reserve_work('99999', config=config, clock=110)
    begin(store, first, 110)
    complete(store, first)  # 没有发送也消耗思考机会。
    message(store, '2', 111)
    assert store.reserve_work('99999', config=config, clock=110 + interval - .01) is None
    assert store.reserve_work('99999', config=config, clock=110 + interval) is not None


@pytest.mark.parametrize('cooldown,due', [(True, 112), (False, 106)])
def test_direct_calls_merge_two_seconds_and_ten_second_limit_is_switchable(store, cooldown, due):
    config = dict(DEFAULT, direct_call_cooldown=cooldown)
    message(store, '1', 100, direct=True)
    assert store.reserve_work('99999', config=config, clock=101.99) is None
    first = store.reserve_work('99999', config=config, clock=102)
    begin(store, first, 102)
    complete(store, first)
    message(store, '2', 104, direct=True)
    assert store.reserve_work('99999', config=config, clock=due - .01) is None
    assert store.reserve_work('99999', config=config, clock=due) is not None


def test_direct_call_bypasses_normal_cooldown_and_waiting_group_does_not_block_private(store):
    message(store, '1', 100)
    first = store.reserve_work('99999', clock=110)
    begin(store, first, 110)
    complete(store, first)
    message(store, '2', 111)
    message(store, '3', 112, thread='private:12345')
    private = store.reserve_work('99999', clock=112)
    assert private['thread'] == 'private:12345'
    begin(store, private, 112)
    complete(store, private)
    message(store, '4', 113, direct=True)
    assert store.reserve_work('99999', clock=114.99) is None
    assert store.reserve_work('99999', clock=115)['thread'] == 'group:54321'


def test_groups_are_independent_and_direct_call_has_priority(store):
    message(store, '1', 100)
    message(store, '2', 108, thread='group:65432', direct=True)
    first = store.reserve_work('99999', clock=110)
    assert first['thread'] == 'group:65432'
    begin(store, first, 110)
    complete(store, first)
    assert store.reserve_work('99999', clock=110)['thread'] == 'group:54321'


def test_actual_event_classifies_at_and_quote_as_direct_without_calling_a_model(store):
    runtime = QqRuntime.__new__(QqRuntime)
    runtime.store, runtime.lock, runtime.stopping = store, RLock(), False
    config = dict(DEFAULT, enabled=True, account_id='99999', group_ids=['54321'], group_mode='all')
    runtime.settings = lambda: (config, 1)
    queued = []
    runtime.app = SimpleNamespace(_dispatcher=SimpleNamespace(enqueue_internal=lambda *args: queued.append(args[0])))
    store.receive('99999', 'group:54321', '55', 'qq:99999', '角色', '已发送', '2026-11-15', outgoing=True, eligible=False)
    base = dict(self_id=99999, user_id=12345, group_id=54321, post_type='message', message_type='group', time=100)
    for identity, prefix in [('1', []), ('2', [dict(type='at', data=dict(qq='99999'))]),
                              ('3', [dict(type='reply', data=dict(id='55'))])]:
        runtime.receive(dict(base, message_id=identity, message=prefix + [dict(type='text', data=dict(text='你好'))]))
    rows = store.rows("SELECT direct_call,eligible FROM qq_message WHERE direction='in' ORDER BY sequence")
    assert rows == [dict(direct_call=0, eligible=1), dict(direct_call=1, eligible=1), dict(direct_call=1, eligible=1)]
    assert queued == ['qq:inbox'] * 3


def test_burst_uses_latest_batch_without_falsely_marking_omitted_messages_seen_or_organized(store):
    for index in range(20):
        message(store, str(index), 100)
    work = store.reserve_work('99999', clock=110)
    owned = store.rows('SELECT sequence FROM qq_message WHERE work_id=? ORDER BY sequence', (work['id'],))
    assert [row['sequence'] for row in owned] == list(range(13, 21))
    omitted = store.rows('SELECT work_id,organized,eligible FROM qq_message WHERE sequence<=12')
    assert len(omitted) == 12 and all(row == dict(work_id=None, organized=0, eligible=0) for row in omitted)
    begin(store, work, 110)
    complete(store, work)
    assert store.reserve_work('99999', clock=1000) is None
    assert len(store.history('99999', 'group:54321')) == 20


def test_group_cooldown_survives_reopening_and_is_only_recorded_once(tmp_path):
    path = tmp_path / 'qq.sqlite3'
    first = QqStore(path)
    message(first, '1', 100, direct=True)
    work = first.reserve_work('99999', clock=102)
    begin(first, work, 102)
    begin(first, work, 108)  # 同任务下一轮规划不重置首次启动时间。
    complete(first, work)
    first.close()
    reopened = QqStore(path)
    try:
        message(reopened, '2', 104, direct=True)
        assert reopened.reserve_work('99999', clock=111.99) is None
        assert reopened.reserve_work('99999', clock=112)
    finally:
        reopened.close()


def test_schema_two_upgrade_preserves_unread_body_without_replaying_old_group_backlog(tmp_path):
    path = tmp_path / 'qq.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.executescript(SCHEMA + SOCIAL_SCHEMA)
        db.execute("INSERT INTO qq_message(account,thread,platform_id,speaker_id,speaker_name,direction,content,created_at,eligible) VALUES('99999','group:54321','1','qq:12345','甲','in','升级前原文','2026-11-15',1)")
        db.commit()
    store = QqStore(path)
    try:
        assert store.rows('SELECT content,organized,work_id,eligible FROM qq_message') == [dict(content='升级前原文', organized=0, work_id=None, eligible=0)]
        assert store.db.execute('PRAGMA user_version').fetchone()[0] == 3
        assert store.reserve_work('99999', clock=1000) is None
        from deskpal.maintenance.archives import _schema
        with closing(sqlite3.connect(path)) as db:
            assert _schema(db, 'plugin_qq.sqlite3') == 3
    finally:
        store.close()


@pytest.mark.parametrize('changes', [dict(group_interval_seconds=True), dict(group_interval_seconds=7), dict(direct_call_cooldown='false')])
def test_invalid_throttle_configuration_is_rejected_before_touching_credentials(changes):
    runtime = QqRuntime.__new__(QqRuntime)
    runtime.lock = RLock()
    runtime.settings = lambda: (dict(DEFAULT), 1)
    with pytest.raises(ValueError, match='普通群聊思考间隔'):
        runtime.command('configure', dict(expected_revision=1, **changes))


def test_local_requests_claim_before_queued_plugin_task_and_do_not_preempt_active_work(tmp_path):
    store = WorkflowStore(tmp_path / 'workflow.sqlite3')
    tasks = PersistentTaskManager(store)
    try:
        plugin = tasks.submit('群聊', command_id='qq')
        with store.transaction() as db:
            body = canonical(dict(plugin_id='qq'))
            db.execute('INSERT INTO context_snapshot VALUES(?,?,?,?)', (plugin.id, 'plugin_execution', sha256(body.encode()).hexdigest(), body))
        local = tasks.submit('桌面请求', command_id='local')
        assert tasks.claim_next().id == local.id
        assert tasks.claim_next() is None
        tasks.finish_submission(local.id)
        assert tasks.claim_next().id == plugin.id
        another = tasks.submit('稍后到达', command_id='late')
        assert tasks.claim_next() is None
        tasks.finish_submission(plugin.id)
        assert tasks.claim_next().id == another.id
    finally:
        store.close()


def test_local_arrival_during_external_validation_wins_before_plugin_claim(tmp_path):
    store = WorkflowStore(tmp_path / 'workflow.sqlite3')
    tasks = PersistentTaskManager(store)
    try:
        plugin = tasks.submit('群聊', command_id='qq')
        with store.transaction() as db:
            body = canonical(dict(plugin_id='qq'))
            db.execute('INSERT INTO context_snapshot VALUES(?,?,?,?)', (plugin.id, 'plugin_execution', sha256(body.encode()).hexdigest(), body))
        submitted = []
        def during_validation(task_id, refs):
            if task_id == plugin.id and not submitted:
                submitted.append(tasks.submit('验证期间到达的桌面请求', command_id='late'))
        store.add_validator(during_validation)
        assert tasks.claim_next().id == submitted[0].id
    finally:
        store.close()
