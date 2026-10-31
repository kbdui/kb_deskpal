"""显示组在 SQL 分页前过滤，只读取当前修订；不修改持久记忆类型。"""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from deskpal.memory.contracts import MemoryDraft, MemorySource, MemoryError
from deskpal.memory.models import MemoryKind
from deskpal.memory.repository import SQLiteMemoryRepository


def test_groups_page_over_entire_database_and_not_only_latest_fifty(tmp_path):
    with SQLiteMemoryRepository(tmp_path / 'memory.sqlite') as repo:
        facts = []
        for index in range(63):
            text = f'事实 {index}'
            facts.append(repo.ingest(MemorySource(text), MemoryDraft(text, text), accepted=True).id)
        episode = repo.ingest(MemorySource('共同完成阅读'), MemoryDraft('共同完成阅读', '共同完成阅读',
            kind=MemoryKind.EPISODIC, destination='episode'), accepted=True)
        buffer = repo.ingest(MemorySource('暂时不确定'), MemoryDraft('暂时不确定', '暂时不确定',
            destination='habit', uncertainty='有待更多交流'))
        first = repo.page_items(group='facts', limit=50)
        second = repo.page_items(group='facts', limit=50, offset=50)
        assert first.has_more and not second.has_more
        assert {row.id for row in (*first.items, *second.items)} == set(facts)
        assert [row.id for row in repo.page_items(group='episode').items] == [episode.id]
        assert [row.id for row in repo.page_items(group='buffer').items] == [buffer.id]
        repo.forget(episode.id, episode.revision)
        assert not repo.page_items(group='episode').items
        assert any(row.id == episode.id for row in repo.page_items(group='all').items)


def test_group_rejects_unknown_value_instead_of_returning_misleading_empty_page(tmp_path):
    with SQLiteMemoryRepository(tmp_path / 'memory.sqlite') as repo:
        with pytest.raises(MemoryError): repo.page_items(group='imaginary')
