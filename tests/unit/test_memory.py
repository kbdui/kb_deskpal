"""SQLite memory contracts without user data, network or vector retrieval."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from deskpal.memory import MemoryKind, MemoryRecord, SQLiteMemoryManager


NOW = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def manager(tmp_path):
    store = SQLiteMemoryManager(tmp_path / "memories.sqlite")
    yield store
    store.close()


@pytest.mark.parametrize("kind", tuple(MemoryKind))
def test_all_memory_fields_and_kinds_round_trip_with_generated_uuid(manager, kind):
    record = MemoryRecord(content="中文记忆", kind=kind, importance=0.7, confidence=0.8,
                          created_at=NOW, decay_rate=0.2, task_id="task-1")
    saved = manager.save(record)
    assert str(UUID(saved.id)) == saved.id
    assert record.id == ""
    assert saved == replace(record, id=saved.id)
    found = manager.search("中文", kind=kind)
    assert len(found) == 1
    assert replace(found[0], last_access_at=None) == saved
    assert found[0].last_access_at is not None
    assert found[0].last_access_at.utcoffset() == timedelta(0)
    with pytest.raises(FrozenInstanceError):
        found[0].content = "cannot change frozen record"


def test_existing_id_replaces_record_instead_of_creating_duplicate(manager):
    previous = manager.save(MemoryRecord("旧内容", id="stable-id", created_at=NOW))
    replacement = MemoryRecord("新内容", id=previous.id, kind=MemoryKind.SEMANTIC,
                               importance=1.0, confidence=0.6, created_at=NOW + timedelta(hours=1),
                               last_access_at=NOW, decay_rate=0.3, task_id="new-task")
    assert manager.save(replacement) == replacement
    assert not manager.search("旧内容")
    results = manager.search("", limit=10)
    assert len(results) == 1
    assert replace(results[0], last_access_at=NOW) == replacement


def test_close_reopen_preserves_records_and_delete(tmp_path):
    path = tmp_path / "persistent.sqlite"
    with SQLiteMemoryManager(path) as first:
        original = first.save(MemoryRecord("持久化内容", kind=MemoryKind.GOAL, importance=0.9,
                                           confidence=0.8, created_at=NOW, decay_rate=0.1, task_id="task"))
    with SQLiteMemoryManager(path) as reopened:
        records = reopened.search("持久化")
        assert len(records) == 1
        assert replace(records[0], last_access_at=None) == original
        assert reopened.delete(original.id) is True
    with SQLiteMemoryManager(path) as final:
        assert not final.search("")
        assert final.delete(original.id) is False


@pytest.mark.parametrize("query,content", [
    ("%", "百分比100%"), ("_", "under_score"), ("\\", "folder\\note"),
    ("中文", "包含中文文本"), ("' OR 1=1 --", "literal ' OR 1=1 -- here"),
    ("%_\\", "combined %_\\ literal"),
])
def test_search_treats_sql_like_and_injection_text_as_literal(manager, query, content):
    selected = manager.save(MemoryRecord(content, created_at=NOW))
    manager.save(MemoryRecord("unrelated content", created_at=NOW))
    results = manager.search(query, limit=10)
    assert [record.id for record in results] == [selected.id]
    assert len(manager.search("", limit=10)) == 2


def test_recent_first_sort_uses_id_ascending_for_equal_timestamps(manager):
    for identifier, created_at in (("z", NOW), ("b", NOW + timedelta(hours=1)),
                                    ("a", NOW + timedelta(hours=1)), ("older", NOW - timedelta(hours=1))):
        manager.save(MemoryRecord("shared content", id=identifier, created_at=created_at))
    assert [record.id for record in manager.search("shared", limit=10)] == ["a", "b", "z", "older"]
    assert [record.id for record in manager.search("", limit=2)] == ["a", "b"]


def test_kind_and_limit_filters_are_combined(manager):
    manager.save(MemoryRecord("shared", id="semantic-old", kind=MemoryKind.SEMANTIC, created_at=NOW))
    manager.save(MemoryRecord("shared", id="semantic-new", kind=MemoryKind.SEMANTIC,
                              created_at=NOW + timedelta(hours=1)))
    manager.save(MemoryRecord("shared", id="goal", kind=MemoryKind.GOAL,
                              created_at=NOW + timedelta(hours=2)))
    results = manager.search("shared", limit=1, kind=MemoryKind.SEMANTIC)
    assert [record.id for record in results] == ["semantic-new"]
    assert not manager.search("no match", kind=MemoryKind.SEMANTIC)


def test_search_updates_last_access_time_for_returned_records(manager):
    old = NOW - timedelta(days=100)
    saved = manager.save(MemoryRecord("accessed", created_at=NOW, last_access_at=old))
    before = datetime.now(timezone.utc)
    result = manager.search("accessed")[0]
    after = datetime.now(timezone.utc)
    assert result.id == saved.id
    assert before <= result.last_access_at <= after
    assert result.last_access_at != old
    assert result.created_at == NOW


@pytest.mark.parametrize("limit", [0, -1, True, False, 1.5, "1", None])
def test_limit_requires_positive_integer(manager, limit):
    with pytest.raises(ValueError):
        manager.search("", limit=limit)


def test_delete_reports_existence(manager):
    saved = manager.save(MemoryRecord("to delete"))
    assert manager.delete("missing") is False
    assert manager.delete(saved.id) is True
    assert manager.delete(saved.id) is False
    assert not manager.search("")


@pytest.mark.parametrize("content", ["", "   ", "\n\t", None, True])
def test_invalid_content_is_rejected_without_inserting(manager, content):
    with pytest.raises(ValueError):
        manager.save(MemoryRecord(content))
    assert not manager.search("")


@pytest.mark.parametrize("field", ["importance", "confidence", "decay_rate"])
@pytest.mark.parametrize("value", [True, False, "0.5", float("nan"), float("inf"), float("-inf")])
def test_memory_numbers_reject_bool_wrong_types_and_non_finite(manager, field, value):
    with pytest.raises(ValueError):
        manager.save(MemoryRecord("invalid numeric", **{field: value}))
    assert not manager.search("")


@pytest.mark.parametrize("field,value", [("importance", -0.1), ("importance", 1.1),
                                         ("confidence", -0.1), ("confidence", 1.1), ("decay_rate", -0.1)])
def test_number_ranges_are_enforced(manager, field, value):
    with pytest.raises(ValueError):
        manager.save(MemoryRecord("invalid range", **{field: value}))
    assert not manager.search("")


@pytest.mark.parametrize("importance,confidence,decay_rate", [(0, 0, 0), (1, 1, 1), (0.5, 0.5, 2)])
def test_numeric_boundaries_are_allowed(manager, importance, confidence, decay_rate):
    record = MemoryRecord("valid numbers", importance=importance, confidence=confidence, decay_rate=decay_rate)
    saved = manager.save(record)
    assert saved.importance == importance
    assert saved.confidence == confidence
    assert saved.decay_rate == decay_rate


@pytest.mark.parametrize("field,value", [("created_at", datetime(2026, 10, 12)),
                                         ("created_at", "2026-10-12"), ("created_at", None),
                                         ("last_access_at", datetime(2026, 10, 12)),
                                         ("last_access_at", "2026-10-12")])
def test_invalid_or_naive_datetimes_are_rejected(manager, field, value):
    with pytest.raises(ValueError):
        manager.save(MemoryRecord("invalid time", **{field: value}))
    assert not manager.search("")


@pytest.mark.parametrize("kind", [True, "not-a-kind", None])
def test_invalid_kind_is_rejected(manager, kind):
    with pytest.raises(ValueError):
        manager.save(MemoryRecord("invalid kind", kind=kind))


@pytest.mark.parametrize("kind", [True, "not-a-kind"])
def test_search_rejects_invalid_kind(manager, kind):
    with pytest.raises(ValueError):
        manager.search("", kind=kind)


def test_aware_non_utc_dates_are_normalized_and_sorted_by_instant(manager):
    offset = timezone(timedelta(hours=8))
    equivalent = NOW.astimezone(offset)
    saved = manager.save(MemoryRecord("offset record", id="b", created_at=equivalent,
                                      last_access_at=equivalent))
    assert saved.created_at == NOW
    assert saved.created_at.utcoffset() == timedelta(0)
    assert saved.last_access_at == NOW
    assert saved.last_access_at.utcoffset() == timedelta(0)
    manager.save(MemoryRecord("UTC record", id="a", created_at=NOW))
    assert [record.id for record in manager.search("", limit=10)] == ["a", "b"]


def test_invalid_replacement_preserves_existing_record(manager):
    original = manager.save(MemoryRecord("existing content", id="same-id", created_at=NOW))
    with pytest.raises(ValueError):
        manager.save(MemoryRecord("", id=original.id))
    remaining = manager.search("existing content")
    assert len(remaining) == 1
    assert replace(remaining[0], last_access_at=None) == original


def test_close_is_idempotent_and_closed_operations_fail(tmp_path):
    store = SQLiteMemoryManager(tmp_path / "closed.sqlite")
    store.close()
    store.close()
    for operation in (lambda: store.save(MemoryRecord("closed")), lambda: store.search(""),
                      lambda: store.delete("missing")):
        with pytest.raises(RuntimeError):
            operation()
    with pytest.raises(RuntimeError):
        with store:
            pytest.fail("A closed store must not reopen implicitly")


def test_context_exit_closes_store_on_exception(tmp_path):
    store = SQLiteMemoryManager(tmp_path / "context.sqlite")
    with pytest.raises(RuntimeError, match="caller failure"):
        with store as entered:
            assert entered is store
            store.save(MemoryRecord("before exception"))
            raise RuntimeError("caller failure")
    with pytest.raises(RuntimeError):
        store.search("")


@pytest.mark.parametrize("as_path", [False, True])
def test_in_memory_database_does_not_create_literal_filename(tmp_path, monkeypatch, as_path):
    monkeypatch.chdir(tmp_path)
    database = Path(":memory:") if as_path else ":memory:"
    with SQLiteMemoryManager(database) as first:
        first.save(MemoryRecord("only in memory"))
        assert len(first.search("")) == 1
    with SQLiteMemoryManager(database) as second:
        assert not second.search("")
    assert not list(tmp_path.iterdir())
