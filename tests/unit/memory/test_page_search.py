"""只读记忆搜索应在分页之前执行，并且不搜索已遗忘的历史正文。"""
import pytest
from deskpal.memory.contracts import MemoryDraft, MemorySource, MemoryError
from deskpal.memory.repository import SQLiteMemoryRepository


def test_search_filters_before_pagination_and_treats_wildcards_literally(tmp_path):
    with SQLiteMemoryRepository(tmp_path / 'memory.sqlite') as repo:
        for n in range(55):
            text = f'报告中的 RAG 事项 {n}'
            repo.ingest(MemorySource(text), MemoryDraft(text, text), accepted=True)
        repo.ingest(MemorySource('无关事项'), MemoryDraft('无关事项', '无关事项'), accepted=True)
        literal = repo.ingest(MemorySource('进度50%_完成'), MemoryDraft('进度50%_完成','进度50%_完成'), accepted=True)
        page = repo.page_items(group='facts', query='rag', limit=50)
        assert len(page.items) == 50 and page.has_more
        second = repo.page_items(group='facts', query='RAG', offset=50, limit=50)
        assert len(second.items) == 5 and not second.has_more
        assert not {i.id for i in page.items}.intersection(i.id for i in second.items)
        assert [i.id for i in repo.page_items(query='%_').items] == [literal.id]
        repo.forget(literal.id, literal.revision)
        assert repo.page_items(query='%_').items == ()
        with pytest.raises(MemoryError): repo.page_items(query='x' * 201)
