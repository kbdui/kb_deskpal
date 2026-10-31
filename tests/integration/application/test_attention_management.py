"""P6 用户结束未执行关注，使用真实 Facade/数据库，禁止伪装取消运行任务。"""
from datetime import datetime, timedelta, timezone
import pytest

from deskpal.application import create_application
from deskpal.application.desktop import DesktopFacade
from deskpal.config import load_config
from deskpal.workflow.models import WorkflowBlocked


def test_end_attention_is_idempotent_conflict_checked_and_preserves_other_data(tmp_path):
    config = load_config(environ={'DESKPAL_LLM_PROVIDER': 'offline',
        'DESKPAL_WORKSPACE_DIR': str(tmp_path / 'workspace'), 'DESKPAL_DATA_DIR': str(tmp_path / 'data'),
        'DESKPAL_MEMORY_EMBEDDING_PROVIDER': 'disabled'})
    app = create_application(config)
    try:
        due = datetime.now(timezone.utc) + timedelta(hours=1)
        def offer(key):
            return app.proactive.offer(key=key, method='review', goal='后续看看阅读计划', basis='用户刚提起',
                refs=[dict(kind='memory', id='synthetic-source', revision=1)], due_at=due.isoformat())
        first, other = offer('first'), offer('other')
        facade = DesktopFacade(app)
        params = dict(id=first, expected_status='pending', command_id='end-once')
        assert facade.execute('attention_end', params)['status'] == 'ended'
        assert facade.execute('attention_end', params)['status'] == 'ended'
        with pytest.raises(WorkflowBlocked): facade.execute('attention_end', {**params, 'id': other})
        with app.store.transaction() as db:
            db.execute("UPDATE attention SET status='running' WHERE id=?", (other,))
        with pytest.raises(WorkflowBlocked):
            facade.execute('attention_end', dict(id=other, expected_status='pending', command_id='stale'))
        items = {row['id']: row for row in facade.execute('attentions', {})['items']}
        assert items[first]['status'] == 'ended' and items[other]['status'] == 'running'
    finally:
        app.close()


def test_upcoming_schedules_filter_before_paging_and_keep_history_available(tmp_path):
    config = load_config(environ={'DESKPAL_LLM_PROVIDER': 'offline',
        'DESKPAL_WORKSPACE_DIR': str(tmp_path / 'workspace'), 'DESKPAL_DATA_DIR': str(tmp_path / 'data'),
        'DESKPAL_MEMORY_EMBEDDING_PROVIDER': 'disabled'})
    app = create_application(config)
    try:
        now = datetime.now(timezone.utc)
        for index in range(55):
            app.scheduling.create_calendar(f'旧日程{index}', (now - timedelta(days=2, minutes=index)).isoformat(), 'UTC')
        upcoming = app.scheduling.create_calendar('接下来的阅读', (now + timedelta(days=1)).isoformat(), 'UTC')
        facade = DesktopFacade(app)
        page = facade.execute('schedule_items', {'status': 'upcoming', 'limit': 50})
        assert [item['title'] for item in page['items']] == ['接下来的阅读']
        assert not page['has_more']
        history = facade.execute('schedule_items', {'status': 'all', 'limit': 50})
        assert history['has_more'] and len(history['items']) == 50
    finally:
        app.close()


def test_ending_attention_during_lookup_does_not_reactivate_internal_review(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    config = load_config(environ={'DESKPAL_LLM_PROVIDER': 'offline',
        'DESKPAL_WORKSPACE_DIR': str(tmp_path / 'workspace'), 'DESKPAL_DATA_DIR': str(tmp_path / 'data'),
        'DESKPAL_MEMORY_EMBEDDING_PROVIDER': 'disabled'})
    config = replace(config, proactive=replace(config.proactive, enabled=False))
    app = create_application(config)
    try:
        now = datetime.now(timezone.utc)
        identity = app.proactive.offer(key='race', method='maintenance', goal='复核一个未决理解', basis='合成依据',
            refs=[dict(kind='memory', id='synthetic-source', revision=1)], due_at=(now + timedelta(seconds=10)).isoformat())
        app.proactive.clock = lambda: now + timedelta(seconds=20)
        enqueued = []
        monkeypatch.setattr(app.proactive, 'valid', lambda item: True)
        monkeypatch.setattr(app.learning, 'review_candidates', lambda now: [])
        def lookup(request):
            app.proactive.end_attention(identity, 'pending', 'end-during-lookup')
            return SimpleNamespace(revisions=())
        monkeypatch.setattr(app._interaction, 'retrieve', lookup)
        monkeypatch.setattr(app.proactive, '_enqueue_review', enqueued.append)
        assert app.proactive._maintenance()
        assert not enqueued
        assert app.proactive.page()['items'][0]['status'] == 'ended'
    finally:
        app.close()
