"""调用计量、错误类别和脱敏；使用伪造传输与合成临时数据库。"""

import io
import json
import sqlite3
import urllib.error

import pytest

from deskpal.llm.diagnostics import sanitize
from deskpal.llm.errors import LLMError, RetryableLLMError
from deskpal.llm.models import ChatMessage
from deskpal.llm.providers import OpenAILLMProvider
from deskpal.workflow import WorkflowStore
from deskpal.workflow.humanization import HumanizationStore
from deskpal.workflow.tasks import PersistentTaskManager


def test_provider_keeps_reported_usage_without_guessing_missing_values():
    body = dict(id='response_synthetic', output=[dict(type='message', role='assistant',
        content=[dict(type='output_text', text='收到')])], usage=dict(input_tokens=123,
        input_tokens_details=dict(cached_tokens=100), output_tokens=30,
        output_tokens_details=dict(reasoning_tokens=20)))
    provider = OpenAILLMProvider(model='synthetic', api_key='synthetic',
        transport=lambda *args, **kwargs: io.BytesIO(json.dumps(body).encode()))
    result = provider.chat((ChatMessage('user', '测试'),))
    assert result.usage.input_tokens == 123 and result.usage.cached_tokens == 100
    assert result.usage.output_tokens == 30 and result.usage.reasoning_tokens == 20
    assert result.request_id == 'response_synthetic'
    body.pop('usage')
    assert provider.chat((ChatMessage('user', '测试'),)).usage is None


@pytest.mark.parametrize('status,code,retry', [(401,'http_error',False),(429,'rate_limit',True),
    (408,'http_transient',True),(503,'http_transient',True)])
@pytest.mark.parametrize('raised', [False, True])
def test_http_errors_have_safe_causes_and_no_body_or_key(status,code,retry,raised):
    def transport(*args, **kwargs):
        if raised:
            raise urllib.error.HTTPError('https://synthetic.invalid',status,'secret-body',{},None)
        stream=io.BytesIO(b'secret-body')
        stream.status=status
        return stream
    provider=OpenAILLMProvider(model='synthetic',api_key='secret-key',transport=transport)
    with pytest.raises(LLMError) as caught:
        provider.generate('secret-request')
    error=caught.value
    assert error.code==code and error.http_status==status
    assert isinstance(error,RetryableLLMError)==retry
    assert 'secret' not in str(error)


def test_sanitize_drops_text_and_invalid_metrics():
    assert sanitize(dict(prompt='secret',body='secret',headers={'Authorization':'secret'},
        processing_code='time_invalid',processing_field='memories[0].review_after',
        input_tokens=-1, output_tokens=True, cached_tokens=10, request_id='bad\nsecret')) == dict(
            processing_code='time_invalid',processing_field='memories[0].review_after',cached_tokens=10)
    assert sanitize(dict(error_code=[],processing_field='用户正文'))=={}


def test_v5_upgrade_is_additive_and_reopen_preserves_diagnostics(tmp_path):
    from deskpal.workflow.store import _SCHEMA, migrate_schema_1, migrate_schema_2, migrate_schema_3, migrate_schema_4
    from deskpal.workflow.migrations import SCHEMA_VERSION
    path=tmp_path/'workflow.sqlite3'
    # 构造真实 v5，不能把含后续表的当前库只改版本号伪装成旧库。
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA foreign_keys=ON')
        for statement in _SCHEMA:
            db.execute(statement)
        for migrate in (migrate_schema_1, migrate_schema_2, migrate_schema_3, migrate_schema_4):
            migrate(db)
        assert db.execute('SELECT version FROM schema_version').fetchone()[0] == 5
    upgraded=WorkflowStore(path)
    with upgraded.read_connection() as db:
        assert db.execute('SELECT version FROM schema_version').fetchone()[0]==SCHEMA_VERSION
        assert [row[1] for row in db.execute('PRAGMA table_info(task_call_attempt)')]==['id','task_id','purpose','slot','status']
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    upgraded.close()
    assert list(tmp_path.glob('workflow.sqlite3.schema-5-*.backup'))
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM model_call_diagnostic').fetchone()[0]==0


def test_actual_attempt_diagnostics_survive_reopen_and_late_update_cannot_resurrect(tmp_path):
    path=tmp_path/'workflow.sqlite3'
    store=WorkflowStore(path)
    task=PersistentTaskManager(store).submit('合成请求',command_id='synthetic')
    records=HumanizationStore(store)
    call=records.reserve_call(task.id,'planning')
    records.diagnose_call(call,dict(error_code='timeout',elapsed_ms=40000,input_tokens=123,prompt='private'))
    records.finish_call(call,'unknown')
    store.close()
    store=WorkflowStore(path)
    records=HumanizationStore(store)
    row=records.call_diagnostics()[0]
    assert row['task_id']==task.id and row['status']=='unknown'
    assert row['metadata']==dict(error_code='timeout',elapsed_ms=40000,input_tokens=123)
    with store.transaction() as db:
        db.execute('DELETE FROM model_call_diagnostic WHERE call_id=?',(call,))
    records.diagnose_call(call,dict(error_code='ok'))
    assert records.call_diagnostics()==[]
    with store.read_connection() as db:
        assert db.execute('SELECT COUNT(*) FROM task_call_attempt').fetchone()[0]==1
    store.close()


def test_restart_marks_unfinished_call_unknown_without_retry_or_refund(tmp_path):
    path=tmp_path/'workflow.sqlite3'
    store=WorkflowStore(path)
    task=PersistentTaskManager(store).submit('合成请求',command_id='synthetic')
    records=HumanizationStore(store)
    records.reserve_call(task.id,'planning')
    store.close()
    store=WorkflowStore(path)
    records=HumanizationStore(store)
    first=records.call_diagnostics()[0]
    assert first['status']=='unknown' and first['metadata']['error_code']=='interrupted'
    assert first['finished_at']
    records.reserve_call(task.id,'planning')
    assert {row['slot'] for row in records.call_diagnostics()}=={1,2}
    store.close()

