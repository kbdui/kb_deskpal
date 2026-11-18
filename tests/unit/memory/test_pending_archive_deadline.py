"""待确认归档期限：真实临时SQLite、有界时间、原始依据保留。"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from deskpal.llm.models import LLMResponse
from deskpal.memory.contracts import MemoryDraft, MemorySource, MemoryStatus, MemoryError
from deskpal.memory.feedback import evaluate, FeedbackValidationError
from deskpal.memory.repository import SQLiteMemoryRepository
from deskpal.memory.retrieval import MemoryRetriever


NOW = datetime(2031, 6, 9, 12, tzinfo=timezone.utc)
DEADLINE = NOW + timedelta(days=2)


@pytest.fixture
def storage(tmp_path):
    clock = [NOW]
    with SQLiteMemoryRepository(tmp_path / 'memory.sqlite3', clock=lambda: clock[0]) as repo:
        yield repo, clock


def seed(repo, *, accepted=False, archive_after=DEADLINE):
    return repo.ingest(MemorySource('我正在学习画画', created_at=NOW),
        MemoryDraft('我正在学习画画', '我正在学习画画', archive_after=archive_after,
                    review_after=NOW + timedelta(days=1)), accepted=accepted)


def test_deadline_archives_once_without_deleting_body_source_or_history(storage):
    repo, clock = storage
    item = seed(repo)
    epoch = repo.epoch
    assert repo.archive_due_candidates() == 0
    clock[0] = DEADLINE
    assert repo.archive_due_candidates() == 1
    archived = repo.get(item.id)
    assert archived.status == MemoryStatus.ARCHIVED and archived.revision == item.revision + 1
    assert archived.draft.content == item.draft.content and archived.source == item.source
    assert archived.draft.archive_after == DEADLINE and archived.draft.review_after is None
    assert archived.change_reason == 'pending_archive_deadline'
    assert repo.get(item.id, revision=1) == item and repo.epoch == epoch + 1
    assert repo.archive_due_candidates() == 0 and repo.epoch == epoch + 1
    assert repo._db.execute('SELECT COUNT(*) FROM memory_tombstones').fetchone()[0] == 0


def test_acceptance_clears_deadline_and_prevents_future_archiving(storage):
    repo, clock = storage
    item = seed(repo)
    adopted = repo.accept(item.id, item.revision)
    assert adopted.draft.archive_after is None
    clock[0] = DEADLINE + timedelta(days=3)
    assert repo.get(item.id).status == MemoryStatus.ACCEPTED
    assert repo.archive_due_candidates() == 0


def test_read_after_reopen_enforces_deadline_without_model_or_background_worker(storage, tmp_path):
    repo, clock = storage
    item = seed(repo)
    clock[0] = DEADLINE
    with SQLiteMemoryRepository(tmp_path / 'memory.sqlite3', clock=lambda: clock[0]) as other:
        assert other.get(item.id).status == MemoryStatus.ARCHIVED
        assert not other.page_items(group='buffer').items
        retriever = MemoryRetriever(other, clock=lambda: clock[0])
        assert not retriever.retrieve('学习画画').records


def test_pending_update_cannot_extend_or_remove_deadline(storage):
    repo, _ = storage
    item = seed(repo)
    with pytest.raises(MemoryError, match='cannot be extended'):
        repo.update_understanding(item.id, item.revision, item.source,
            replace(item.draft, archive_after=DEADLINE + timedelta(days=1)), MemoryStatus.CANDIDATE)
    updated = repo.update_understanding(item.id, item.revision, item.source,
        replace(item.draft, archive_after=None), MemoryStatus.CANDIDATE)
    assert updated.draft.archive_after == DEADLINE


def test_expired_snapshot_cannot_be_used_to_adopt_an_archived_candidate(storage):
    repo, clock = storage
    item = seed(repo)
    clock[0] = DEADLINE
    with pytest.raises(MemoryError):
        repo.update_understanding(item.id, item.revision, item.source, item.draft, MemoryStatus.ACCEPTED)
    assert repo.get(item.id).status == MemoryStatus.ARCHIVED


def input_data():
    return dict(now=NOW.isoformat(), catalog={'user': dict(content='我正在学习画画', origin='user_message')},
        understandings=[dict(id='old', revision=1, status='candidate', content='我正在学习画画',
            destination='facts', claim_type='user_report', uncertainty='', original_source_key='user',
            archive_after=DEADLINE.isoformat())])


def run_feedback(result, data=None):
    return evaluate(SimpleNamespace(chat=lambda *a, **k: LLMResponse(json.dumps(result))), data or input_data())


@pytest.mark.parametrize('limit', [None, NOW.isoformat(), (NOW-timedelta(seconds=1)).isoformat(),
                                  '2031-06-10T12:00:00', (NOW+timedelta(hours=1)).isoformat()])
def test_model_must_set_future_archive_time_not_before_review(limit):
    row = dict(content='我正在学习画画', quote='我正在学习画画', reason='本人明确提到',
        destination='facts', claim_type='user_report', sources=['user'],
        review_after=(NOW+timedelta(days=1)).isoformat(), archive_after=limit)
    with pytest.raises(FeedbackValidationError) as error:
        run_feedback(dict(memories=[row]))
    assert error.value.field == 'memories[0].archive_after'


def test_retention_inherits_archive_time_and_rejects_extension():
    row = dict(id='old', decision='retain', reason='继续观察', review_after=(NOW+timedelta(days=1)).isoformat())
    assert run_feedback(dict(reviews=[row]))['reviews'][0]['archive_after'] == DEADLINE.isoformat()
    with pytest.raises(FeedbackValidationError) as error:
        run_feedback(dict(reviews=[row | dict(archive_after=(DEADLINE+timedelta(days=1)).isoformat())]))
    assert error.value.diagnostics['processing_rule'] == 'archive_schedule'


@pytest.mark.parametrize('decision', ['adopt', 'archive'])
def test_terminal_decisions_clear_archive_time(decision):
    row = run_feedback(dict(reviews=[dict(id='old', decision=decision, reason='复查完成')]))['reviews'][0]
    assert row['archive_after'] is None


def test_merge_keeps_earliest_deadline():
    data = input_data()
    data['understandings'].append(dict(data['understandings'][0], id='target',
        archive_after=(DEADLINE+timedelta(days=1)).isoformat()))
    row = dict(id='old', decision='merge', target='target', content='我正在学习画画',
        quote='我正在学习画画', reason='同一理解', destination='facts', claim_type='user_report',
        sources=['user'], review_after=(NOW+timedelta(days=1)).isoformat())
    assert run_feedback(dict(reviews=[row]), data)['reviews'][0]['archive_after'] == DEADLINE.isoformat()
