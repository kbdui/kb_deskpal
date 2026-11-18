"""一次性补期限脚本在临时档案验证备份、版本、范围与幂等。"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from scripts.set_pending_archive_deadline import apply_deadline
from deskpal.memory.contracts import MemoryDraft, MemorySource, MemoryStatus
from deskpal.memory.repository import SQLiteMemoryRepository


def test_backfill_creates_restoreable_snapshot_and_only_revises_pending_heads(tmp_path):
    identity = 'a' * 32
    profile = tmp_path / 'profiles' / identity
    profile.mkdir(parents=True)
    (tmp_path / 'profiles.json').write_text(json.dumps(dict(selected_id=identity)), encoding='utf-8')
    path = profile / 'memory.sqlite3'
    deadline = datetime.now(timezone.utc) + timedelta(days=3)
    with SQLiteMemoryRepository(path) as repo:
        pending = repo.ingest(MemorySource('临时理解'), MemoryDraft('临时理解', '临时理解'))
        accepted = repo.ingest(MemorySource('确定记录'), MemoryDraft('确定记录', '确定记录'), accepted=True)
    # 非记忆库文件不进入脚本的读写范围。
    other = profile / 'companion.sqlite3'
    other.write_bytes(b'synthetic-untouched-role')
    receipt = apply_deadline(tmp_path, deadline)
    assert receipt['changed_count'] == 1 and receipt['ids'] == [pending.id]
    with closing(sqlite3.connect(receipt['backup'])) as backup:
        assert backup.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert backup.execute('SELECT revision FROM memory_heads WHERE id=?', (pending.id,)).fetchone()[0] == 1
    with SQLiteMemoryRepository(path) as repo:
        updated = repo.get(pending.id)
        assert updated.draft.archive_after == deadline and updated.revision == 2
        assert updated.draft.content == pending.draft.content and updated.source == pending.source
        assert updated.draft.review_after == pending.draft.review_after
        assert repo.get(accepted.id) == accepted and updated.status == MemoryStatus.CANDIDATE
        assert repo.get(pending.id, revision=1) == pending
    assert other.read_bytes() == b'synthetic-untouched-role'
    assert apply_deadline(tmp_path, deadline)['changed_count'] == 0
