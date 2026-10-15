"""V0.3 repository contracts against temporary databases and injected clocks."""

import sqlite3
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from deskpal.memory.contracts import (
    ClaimType, EmbeddingManifest, MemoryDraft, MemoryError, MemoryNotFound,
    MemorySource, MemoryStatus, RevisionConflict, UsePolicy,
)
from deskpal.memory.models import MemoryKind
from deskpal.memory.repository import SQLiteMemoryRepository


NOW = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
MANIFEST = EmbeddingManifest("test-local-model", 3, "test-manifest-v1")


class MutableClock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return MutableClock()


@pytest.fixture
def repository(tmp_path, clock):
    with SQLiteMemoryRepository(tmp_path / "memory.sqlite", clock=clock) as repo:
        yield repo


def source(text="我喜欢简短回答", **kwargs):
    return MemorySource(text, created_at=NOW, **kwargs)


def draft(text="我喜欢简短回答", **kwargs):
    return MemoryDraft(text, text, **kwargs)


def make_v1(path):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT NOT NULL, kind TEXT NOT NULL, "
                           "importance REAL NOT NULL, confidence REAL NOT NULL, created_at TEXT NOT NULL, "
                           "last_access_at TEXT, decay_rate REAL NOT NULL, task_id TEXT)")
        record = ("legacy-id", '{"request":"中文%_\\截断', "semantic", 0.8, 1.0,
                  "2026-10-14T12:00:00+08:00", "2026-10-14T05:00:00+00:00", 0.2, "old-task")
        connection.execute("INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", record)
        connection.execute("PRAGMA user_version=1")
        connection.commit()
    return record


def test_v1_backup_migration_preserves_raw_data_and_never_confirms_legacy(tmp_path, clock):
    path = tmp_path / "old.sqlite"
    original = make_v1(path)
    with SQLiteMemoryRepository(path, clock=clock) as repo:
        migrated = repo.get("legacy-id")
        assert migrated.status == MemoryStatus.LEGACY
        assert migrated.source.origin == "legacy"
        assert migrated.source.task_id == "old-task"
        assert migrated.draft.content == original[1]
        assert migrated.draft.importance == 0.8
        assert migrated.draft.extraction_confidence == 0
        assert migrated.source.created_at == datetime(2026, 10, 14, 4, tzinfo=timezone.utc)
        assert repo.list_items(status=MemoryStatus.ACCEPTED) == ()
        with pytest.raises(MemoryError):
            repo.accept(migrated.id, migrated.revision)
    backup = Path(str(path) + ".v1.bak")
    with closing(sqlite3.connect(backup)) as copy, closing(sqlite3.connect(path)) as current:
        assert copy.execute("PRAGMA user_version").fetchone()[0] == 1
        assert copy.execute("SELECT * FROM memories").fetchone() == original
        assert current.execute("PRAGMA user_version").fetchone()[0] == 2
        assert current.execute("SELECT * FROM memories").fetchone() == original
    backup_bytes = backup.read_bytes()
    with SQLiteMemoryRepository(path, clock=clock) as reopened:
        assert len(reopened.list_items()) == 1
        assert reopened.get("legacy-id") == migrated
    assert backup.read_bytes() == backup_bytes


def test_migration_failure_rolls_back_schema_and_keeps_verified_backup(tmp_path, clock):
    path = tmp_path / "bad.sqlite"
    original = make_v1(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE memory_revisions (incompatible TEXT)")
        connection.commit()
    with pytest.raises(sqlite3.DatabaseError):
        SQLiteMemoryRepository(path, clock=clock)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT * FROM memories").fetchone() == original
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='memory_sources'").fetchone() is None
    with closing(sqlite3.connect(str(path) + ".v1.bak")) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT * FROM memories").fetchone() == original


def test_migration_refuses_to_overwrite_existing_backup(tmp_path, clock):
    path = tmp_path / "v1.sqlite"
    make_v1(path)
    backup = Path(str(path) + ".v1.bak")
    backup.write_bytes(b"existing backup must survive")
    with pytest.raises(MemoryError, match="backup already exists"):
        SQLiteMemoryRepository(path, clock=clock)
    assert backup.read_bytes() == b"existing backup must survive"


@pytest.mark.parametrize("version", [-1, 3, 500])
def test_unknown_schema_is_refused_without_mutation(tmp_path, clock, version):
    path = tmp_path / "future.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"PRAGMA user_version={version}")
    with pytest.raises(MemoryError, match="schema version"):
        SQLiteMemoryRepository(path, clock=clock)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='memory_sources'").fetchone() is None


@pytest.mark.parametrize("as_path", [False, True])
def test_in_memory_database_is_isolated_and_not_a_disk_path(tmp_path, monkeypatch, clock, as_path):
    monkeypatch.chdir(tmp_path)
    path = Path(":memory:") if as_path else ":memory:"
    with SQLiteMemoryRepository(path, clock=clock) as first, SQLiteMemoryRepository(path, clock=clock) as second:
        first.ingest(source(), draft(), accepted=True)
        assert len(first.list_items()) == 1
        assert second.list_items() == ()
    assert not (tmp_path / ":memory:").exists()


def test_round_trip_immutable_snapshots_and_reopen(tmp_path, clock):
    path = tmp_path / "nested" / "repo.sqlite"
    offset = timezone(timedelta(hours=8))
    with SQLiteMemoryRepository(path, clock=clock) as repo:
        entered = source("中文%_\\ ' OR 1=1 --", id="stable-source", use_policy=UsePolicy.LOCAL_ONLY,
                         task_id="task-id")
        entered = replace(entered, created_at=NOW.astimezone(offset))
        saved = repo.ingest(entered, draft(entered.content, importance=1, extraction_confidence=0.7,
                                          pinned=True), accepted=True)
        UUID(saved.id)
        assert saved.source.created_at.tzinfo is timezone.utc
        assert saved.source.content == entered.content
        assert saved.source.use_policy == UsePolicy.LOCAL_ONLY
        assert repo.get(saved.id) == saved
        with pytest.raises(FrozenInstanceError):
            saved.draft.content = "mutated"
        with pytest.raises(FrozenInstanceError):
            saved.source.content = "mutated"
    with SQLiteMemoryRepository(path, clock=clock) as reopened:
        assert reopened.get(saved.id) == saved


def test_sources_cannot_be_rebound_to_different_content_or_owner(repository):
    original = source(id="immutable-source")
    repository.ingest(original, draft())
    before = repository.epoch
    for changed in [replace(original, content="篡改来源"), replace(original, owner_id="other")]:
        with pytest.raises(MemoryError, match="immutable"):
            repository.ingest(changed, draft(changed.content))
    assert repository.epoch == before
    assert len(repository.list_items()) == 1


def test_idempotent_ingest_and_different_command_conflict(repository, clock):
    entered = source()
    saved = repository.ingest(entered, draft(), idempotency_key="request-1")
    before = repository.epoch
    clock.now += timedelta(hours=2)
    assert repository.ingest(entered, draft(), idempotency_key="request-1") == saved
    assert repository.epoch == before
    with pytest.raises(RevisionConflict):
        repository.ingest(source("另一个内容"), draft("另一个内容"), idempotency_key="request-1")
    assert len(repository.list_items()) == 1


@pytest.mark.parametrize("changes", [
    {"content": " "}, {"quote": "不在来源中的句子"}, {"quote": ""}, {"importance": True},
    {"importance": -0.1}, {"importance": 1.1}, {"importance": float("nan")},
    {"extraction_confidence": float("inf")}, {"extraction_confidence": "0.5"},
    {"half_life_days": 0}, {"half_life_days": True}, {"half_life_days": -1},
    {"half_life_days": float("inf")}, {"pinned": 1}, {"kind": "semantic"},
    {"claim_type": "user_report"}, {"valid_from": NOW.replace(tzinfo=None)},
    {"valid_to": NOW.replace(tzinfo=None)}, {"expires_at": NOW.replace(tzinfo=None)},
    {"expires_at": NOW}, {"valid_from": NOW, "valid_to": NOW},
    {"valid_from": NOW + timedelta(days=1), "valid_to": NOW},
    {"content": "中" * 3000}, {"scope": ""}, {"subject": 1},
])
def test_invalid_drafts_do_not_write_or_advance_epoch(repository, changes):
    before = repository.epoch
    with pytest.raises(MemoryError):
        repository.ingest(source(), replace(draft(), **changes))
    assert repository.epoch == before
    assert repository.list_items() == ()


@pytest.mark.parametrize("changes", [
    {"origin": "fake-user"}, {"origin": []}, {"owner_id": ""}, {"session_id": ""},
    {"task_id": 1}, {"id": 1}, {"use_policy": "cloud_allowed"},
    {"created_at": NOW.replace(tzinfo=None)}, {"content": "中" * 22000},
])
def test_invalid_sources_do_not_write(repository, changes):
    with pytest.raises(MemoryError):
        repository.ingest(replace(source(), **changes), draft())
    assert repository.list_items() == ()


@pytest.mark.parametrize("origin,claim", [
    ("assistant_output", ClaimType.USER_REPORT), ("tool_result", ClaimType.USER_REPORT),
    ("imported_document", ClaimType.USER_REPORT), ("user_message", ClaimType.OBSERVED_EVENT),
    ("assistant_output", ClaimType.HYPOTHESIS), ("legacy", ClaimType.LEGACY),
])
def test_origins_cannot_forge_accepted_user_facts(repository, origin, claim):
    with pytest.raises(MemoryError):
        repository.ingest(source(origin=origin), draft(claim_type=claim), accepted=True)


def test_observed_and_external_claims_keep_attribution(repository):
    tool = repository.ingest(source("文件存在", origin="tool_result"),
                             draft("文件存在", claim_type=ClaimType.OBSERVED_EVENT,
                                   kind=MemoryKind.EPISODIC), accepted=True)
    external = repository.ingest(source("月亮是奶酪", origin="imported_document"),
                                 draft("月亮是奶酪", claim_type=ClaimType.EXTERNAL_CLAIM), accepted=True)
    assert tool.draft.claim_type == ClaimType.OBSERVED_EVENT
    assert external.draft.claim_type == ClaimType.EXTERNAL_CLAIM
    with pytest.raises(MemoryError):
        repository.ingest(source("文档称用户叫A", origin="imported_document"),
                          draft("文档称用户叫A", claim_type=ClaimType.EXTERNAL_CLAIM,
                                profile_key="name", value="A"), accepted=True)


def test_candidate_review_checks_revision_and_removes_only_default_ttl(repository, clock):
    candidate = repository.ingest(source(), draft())
    assert candidate.draft.expires_at == NOW + timedelta(days=7)
    clock.now += timedelta(days=1)
    accepted = repository.accept(candidate.id, 1)
    assert accepted.revision == 2 and accepted.status == MemoryStatus.ACCEPTED
    assert accepted.accepted_at == clock.now
    assert accepted.draft.expires_at is None
    assert repository.get(candidate.id, revision=1) == candidate
    with pytest.raises(RevisionConflict):
        repository.accept(candidate.id, 1)
    explicit = repository.ingest(source("临时偏好"), draft("临时偏好", expires_at=clock.now + timedelta(days=2)))
    assert repository.accept(explicit.id, 1).draft.expires_at == explicit.draft.expires_at


def test_rejection_is_revisioned_and_remains_forgettable(repository):
    candidate = repository.ingest(source(), draft())
    rejected = repository.reject(candidate.id, 1)
    assert rejected.revision == 2 and rejected.status == MemoryStatus.REJECTED
    assert len(repository.list_items(include_history=True)) == 2
    receipt = repository.forget(rejected.id, rejected.revision)
    assert receipt.revision == 3
    assert repository.get(candidate.id, revision=1).status == MemoryStatus.DELETED


def test_candidate_ttl_purges_payload_and_does_not_remove_shared_accepted_fact(repository, clock, tmp_path):
    entered = source("甲乙", id="shared")
    accepted = repository.ingest(entered, draft("甲"), accepted=True)
    candidate = repository.ingest(entered, draft("乙"))
    clock.now += timedelta(days=7)
    deleted = repository.get(candidate.id)
    assert deleted.status == MemoryStatus.DELETED
    assert deleted.draft.content == deleted.source.content == ""
    assert repository.get(accepted.id) == accepted
    with pytest.raises((MemoryError, RevisionConflict)):
        repository.accept(candidate.id, 1)
    with closing(sqlite3.connect(tmp_path / "memory.sqlite")) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_revisions WHERE id=?", (candidate.id,)).fetchone()[0] == 0


def test_replaying_an_explicitly_expired_command_returns_tombstone(repository, clock):
    entered = source()
    requested = draft(expires_at=NOW + timedelta(hours=1))
    candidate = repository.ingest(entered, requested, idempotency_key="ttl-request")
    clock.now += timedelta(hours=1)
    repeated = repository.ingest(entered, requested, idempotency_key="ttl-request")
    assert repeated.id == candidate.id and repeated.status == MemoryStatus.DELETED
    assert repeated.draft.content == ""


@pytest.mark.parametrize("expire_via", ["get", "list", "epoch", "forget"])
def test_ttl_tombstone_retains_task_receipt_after_repeats_and_reopen(tmp_path, clock, expire_via):
    path = tmp_path / "ttl-tasks.sqlite"
    task_id = str(uuid4())
    with SQLiteMemoryRepository(path, clock=clock) as repo:
        candidate = repo.ingest(source("需要遗忘的候选正文", task_id=task_id), draft("需要遗忘的候选正文"))
        clock.now += timedelta(days=8)
        if expire_via == "get":
            assert repo.get(candidate.id).status == MemoryStatus.DELETED
        elif expire_via == "list":
            assert repo.list_items(status=MemoryStatus.CANDIDATE) == ()
        elif expire_via == "epoch":
            assert repo.epoch > 1
        receipt = repo.forget(candidate.id, candidate.revision)
        assert receipt.affected_task_ids == (task_id,)
        assert repo.forget(candidate.id, candidate.revision) == receipt
        assert repo.forget(candidate.id, receipt.revision) == receipt
    with SQLiteMemoryRepository(path, clock=clock) as reopened:
        assert reopened.forget(candidate.id, candidate.revision) == receipt
        assert reopened.get(candidate.id).source.content == ""
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT memory_id,task_id FROM memory_deletion_tasks").fetchall() == [(candidate.id, task_id)]
        assert [row[1] for row in db.execute("PRAGMA table_info(memory_deletion_tasks)")] == ["memory_id", "task_id"]
        for table in ("memory_sources", "memory_revisions"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_ttl_sweep_does_not_mix_unrelated_tombstones_task_scopes(tmp_path, clock):
    path = tmp_path / "separate-tasks.sqlite"
    first_task, second_task = str(uuid4()), str(uuid4())
    with SQLiteMemoryRepository(path, clock=clock) as repo:
        first = repo.ingest(source("甲", task_id=first_task), draft("甲"))
        second = repo.ingest(source("乙", task_id=second_task), draft("乙"))
        clock.now += timedelta(days=8)
        assert repo.list_items(status=MemoryStatus.CANDIDATE) == ()
        assert repo.forget(first.id, first.revision).affected_task_ids == (first_task,)
        assert repo.forget(second.id, second.revision).affected_task_ids == (second_task,)
    with SQLiteMemoryRepository(path, clock=clock) as reopened:
        assert reopened.forget(first.id, first.revision).affected_task_ids == (first_task,)
        assert reopened.forget(second.id, second.revision).affected_task_ids == (second_task,)


def test_shared_source_delete_receipts_retain_task_scope_for_every_tombstone(tmp_path, clock):
    path = tmp_path / "shared-tasks.sqlite"
    task_id = str(uuid4())
    with SQLiteMemoryRepository(path, clock=clock) as repo:
        entered = source("共享的甲乙", id=str(uuid4()), task_id=task_id)
        first = repo.ingest(entered, draft("甲"), accepted=True)
        second = repo.ingest(entered, draft("乙"))
        receipt = repo.forget(first.id, first.revision)
        assert receipt.affected_task_ids == (task_id,)
        assert repo.forget(first.id, first.revision) == receipt
        assert repo.forget(second.id, second.revision).affected_task_ids == (task_id,)
    with SQLiteMemoryRepository(path, clock=clock) as reopened:
        assert reopened.forget(first.id, first.revision) == receipt
        assert reopened.forget(second.id, second.revision).affected_task_ids == (task_id,)


def test_profile_conflicts_require_correction_but_scopes_owners_and_values_are_distinct(repository):
    saved = repository.ingest(source("我叫甲"), draft("我叫甲", profile_key="name", value="甲"), accepted=True)
    before = repository.epoch
    with pytest.raises(RevisionConflict):
        repository.ingest(source("我叫乙"), draft("我叫乙", profile_key="name", value="乙"), accepted=True)
    assert repository.epoch == before
    candidate = repository.ingest(source("我叫乙"), draft("我叫乙", profile_key="name", value="乙"))
    with pytest.raises(RevisionConflict):
        repository.accept(candidate.id, 1)
    assert repository.get(candidate.id) == candidate
    repository.ingest(source("我叫乙", owner_id="other"), draft("我叫乙", profile_key="name", value="乙"), accepted=True)
    repository.ingest(source("工作称呼乙"), draft("工作称呼乙", profile_key="name", value="乙", scope="work"), accepted=True)
    repository.ingest(source("我叫甲"), draft("我叫甲", profile_key="name", value="甲"), accepted=True)
    # Unstructured statements and multiple general preferences are not single-value fields.
    repository.ingest(source("喜欢茶"), draft("喜欢茶", value="茶"), accepted=True)
    repository.ingest(source("喜欢咖啡"), draft("喜欢咖啡", value="咖啡"), accepted=True)
    assert repository.get(saved.id) == saved


def test_factual_correction_supersedes_old_head_and_clears_old_index(repository):
    old = repository.ingest(source("我住北京"), draft("我住北京", profile_key="city", value="北京"), accepted=True)
    assert repository.put_embedding(old.id, 1, MANIFEST, (1.0, 0.0, -1.0))
    corrected = repository.revise(old.id, 1, source("一直住杭州"),
                                  draft("一直住杭州", profile_key="city", value="杭州"))
    assert corrected.id != old.id and corrected.revision == 1
    assert corrected.supersedes == old.id
    assert corrected.change_reason == "factual_correction"
    previous = repository.get(old.id)
    assert previous.revision == 2 and previous.status == MemoryStatus.SUPERSEDED
    assert repository.get(old.id, revision=1) == old
    assert repository.embeddings(MANIFEST) == ()
    assert not repository.put_embedding(old.id, 1, MANIFEST, (1.0, 0.0, 0.0))
    assert not repository.put_embedding(old.id, 2, MANIFEST, (1.0, 0.0, 0.0))
    assert [item.id for item in repository.list_items(status=MemoryStatus.ACCEPTED)] == [corrected.id]


def test_temporal_change_has_accepted_nonoverlapping_history(repository, clock):
    old = repository.ingest(source("我住北京"), draft("我住北京", profile_key="city", value="北京"), accepted=True)
    clock.now += timedelta(days=3)
    changed = repository.revise(old.id, 1, source("我搬到杭州了"),
                                draft("我搬到杭州了", profile_key="city", value="杭州"), temporal=True)
    ended = repository.get(old.id)
    assert ended.status == changed.status == MemoryStatus.ACCEPTED
    assert ended.draft.valid_to == changed.draft.valid_from == clock.now
    assert ended.draft.valid_from is None  # Unknown past is not fabricated as an infinite interval.
    assert ended.change_reason == changed.change_reason == "temporal_change"
    assert repository.put_embedding(ended.id, ended.revision, MANIFEST, (1.0, 0.0, 0.0))
    assert repository.put_embedding(changed.id, 1, MANIFEST, (1.0, 0.0, 0.0))
    repository.forget(changed.id, 1)
    assert repository.get(old.id) == ended  # Deletion never reopens the old validity interval.
    assert repository.embeddings(MANIFEST) == ((ended.id, ended.revision, (1.0, 0.0, 0.0)),)


def test_revision_failures_are_atomic_and_do_not_cross_owner(repository):
    old = repository.ingest(source(), draft(), accepted=True)
    before = repository.epoch
    with pytest.raises(RevisionConflict):
        repository.revise(old.id, 20, source("新内容"), draft("新内容"))
    with pytest.raises(MemoryError):
        repository.revise(old.id, 1, source("新内容", owner_id="other"), draft("新内容"))
    assert repository.epoch == before and repository.get(old.id) == old
    with pytest.raises(MemoryNotFound):
        repository.reject("not-found", 1)


def test_delete_shared_source_clears_history_indexes_and_replay(repository, tmp_path):
    entered = source("甲乙", id="source-to-forget", task_id="task-to-invalidate")
    first = repository.ingest(entered, draft("甲"), accepted=True, idempotency_key="first")
    second = repository.ingest(entered, draft("乙"), idempotency_key="second")
    second = repository.accept(second.id, 1)
    assert repository.put_embedding(first.id, 1, MANIFEST, (1.0, 0.0, 0.0))
    receipt = repository.forget(first.id, 1)
    assert receipt.affected_task_ids == ("task-to-invalidate",)
    assert receipt.revision == 2
    assert repository.get(first.id).status == repository.get(second.id).status == MemoryStatus.DELETED
    assert repository.get(second.id, revision=1).draft.content == ""
    assert repository.embeddings(MANIFEST) == ()
    assert repository.ingest(entered, draft("甲"), accepted=True, idempotency_key="first").status == MemoryStatus.DELETED
    assert not repository.put_embedding(first.id, 1, MANIFEST, (1.0, 0.0, 0.0))
    with pytest.raises(MemoryError, match="forgotten source"):
        repository.ingest(entered, draft("甲"))
    epoch = repository.epoch
    repeated = repository.forget(first.id, 1)
    assert repeated == receipt and repository.epoch == epoch
    with closing(sqlite3.connect(tmp_path / "memory.sqlite")) as db:
        for table in ("memory_sources", "memory_revisions", "memory_embeddings"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert all(row[0] is None for row in db.execute("SELECT fingerprint FROM memory_ingestions"))


def test_forget_legacy_clears_retained_v1_table_and_metadata(tmp_path, clock):
    path = tmp_path / "legacy.sqlite"
    make_v1(path)
    with SQLiteMemoryRepository(path, clock=clock) as repo:
        receipt = repo.forget("legacy-id", 1)
        assert receipt.affected_task_ids == ("old-task",)
        assert repo.get("legacy-id").status == MemoryStatus.DELETED
    with closing(sqlite3.connect(path)) as db:
        for table in ("memories", "memory_legacy_metadata", "memory_sources", "memory_revisions"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_sqlite_failure_rolls_back_item_source_epoch_and_review(tmp_path, repository):
    candidate = repository.ingest(source(), draft())
    before = repository.epoch
    with closing(sqlite3.connect(tmp_path / "memory.sqlite")) as db:
        db.execute("CREATE TRIGGER reject_revision BEFORE INSERT ON memory_revisions "
                   "WHEN NEW.revision > 1 BEGIN SELECT RAISE(ABORT,'simulated storage failure'); END")
        db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        repository.accept(candidate.id, 1)
    assert repository.get(candidate.id) == candidate
    assert repository.epoch == before
    with closing(sqlite3.connect(tmp_path / "memory.sqlite")) as db:
        db.execute("DROP TRIGGER reject_revision")
        db.execute("CREATE TRIGGER reject_item BEFORE INSERT ON memory_revisions "
                   "BEGIN SELECT RAISE(ABORT,'simulated storage failure'); END")
        db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        repository.ingest(source("新的来源", id="should-rollback"), draft("新的来源"), accepted=True)
    with closing(sqlite3.connect(tmp_path / "memory.sqlite")) as db:
        assert db.execute("SELECT id FROM memory_sources WHERE id='should-rollback'").fetchone() is None
    assert repository.epoch == before


def test_correction_failure_rolls_back_new_source_old_head_and_index(tmp_path, repository):
    previous = repository.ingest(source(), draft(), accepted=True)
    repository.put_embedding(previous.id, 1, MANIFEST, (1.0, 0.0, 0.0))
    before = repository.epoch
    with closing(sqlite3.connect(tmp_path / "memory.sqlite")) as db:
        db.execute("CREATE TRIGGER fail_new_correction BEFORE INSERT ON memory_revisions "
                   "WHEN NEW.reason='factual_correction' AND NEW.revision=1 "
                   "BEGIN SELECT RAISE(ABORT,'simulated storage failure'); END")
        db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        repository.revise(previous.id, 1, source("新的来源", id="rolled-back-source"), draft("新的来源"))
    assert repository.get(previous.id) == previous
    assert repository.epoch == before
    assert repository.embeddings(MANIFEST) == ((previous.id, 1, (1.0, 0.0, 0.0)),)
    with closing(sqlite3.connect(tmp_path / "memory.sqlite")) as db:
        assert db.execute("SELECT id FROM memory_sources WHERE id='rolled-back-source'").fetchone() is None


def test_forget_failure_rolls_back_clearing_tombstones_and_epoch(tmp_path, repository):
    previous = repository.ingest(source(), draft(), accepted=True, idempotency_key="opaque-request")
    repository.put_embedding(previous.id, 1, MANIFEST, (1.0, 0.0, 0.0))
    before = repository.epoch
    with closing(sqlite3.connect(tmp_path / "memory.sqlite")) as db:
        db.execute("CREATE TRIGGER fail_source_clear BEFORE DELETE ON memory_sources "
                   "BEGIN SELECT RAISE(ROLLBACK,'simulated storage failure'); END")
        db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="simulated storage failure"):
        repository.forget(previous.id, 1)
    assert repository.get(previous.id) == previous
    assert repository.epoch == before
    assert repository.embeddings(MANIFEST) == ((previous.id, 1, (1.0, 0.0, 0.0)),)
    with closing(sqlite3.connect(tmp_path / "memory.sqlite")) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_tombstones").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_deletion_tasks").fetchone()[0] == 0
        assert db.execute("SELECT fingerprint FROM memory_ingestions").fetchone()[0] is not None


def test_learning_claim_is_persistent_and_independent_of_forgetting(tmp_path, clock):
    path = tmp_path / "claims.sqlite"
    with SQLiteMemoryRepository(path, clock=clock) as repo:
        epoch = repo.epoch
        assert repo.claim_learning("opaque-task-id")
        assert not repo.claim_learning("opaque-task-id")
        assert repo.epoch == epoch
        saved = repo.ingest(source(), draft(), accepted=True)
        repo.forget(saved.id, 1)
        assert not repo.claim_learning("opaque-task-id")
    with SQLiteMemoryRepository(path, clock=clock) as reopened:
        assert not reopened.claim_learning("opaque-task-id")
        assert reopened.claim_learning("other-task-id")
    with closing(sqlite3.connect(path)) as db:
        assert all(row[0] not in ("opaque-task-id", "other-task-id") for row in db.execute(
            "SELECT token FROM memory_learning_receipts"))


def test_embedding_namespaces_vectors_and_expiry_are_enforced(repository, clock):
    item = repository.ingest(source(), draft(expires_at=NOW + timedelta(hours=1)), accepted=True)
    other = EmbeddingManifest(MANIFEST.model, 3, "manifest-v2")
    assert repository.put_embedding(item.id, 1, MANIFEST, (1.0, -0.5, 0.25))
    assert repository.embeddings(MANIFEST) == ((item.id, 1, (1.0, -0.5, 0.25)),)
    assert repository.embeddings(other) == ()
    assert repository.put_embedding(item.id, 1, other, (-0.5, 1.0, 0.25))
    with pytest.raises(MemoryError):
        repository.embeddings(EmbeddingManifest("different-model", 3, MANIFEST.version))
    with pytest.raises(MemoryError):
        repository.put_embedding(item.id, 1, EmbeddingManifest(MANIFEST.model, 2, MANIFEST.version), (1.0, 1.0))
    clock.now += timedelta(hours=1)
    assert repository.embeddings(MANIFEST) == repository.embeddings(other) == ()
    assert not repository.put_embedding(item.id, 1, MANIFEST, (1.0, 0.0, 0.0))


@pytest.mark.parametrize("vector", [(1.0,), (0.0, 0.0, 0.0), (True, 0.0, 1.0),
                                     (float("nan"), 0.0, 1.0), (float("inf"), 0.0, 1.0),
                                     [1.0, 0.0, 1.0]])
def test_invalid_embeddings_are_rejected_without_writes(repository, vector):
    item = repository.ingest(source(), draft(), accepted=True)
    with pytest.raises(MemoryError):
        repository.put_embedding(item.id, 1, MANIFEST, vector)
    assert repository.embeddings(MANIFEST) == ()


@pytest.mark.parametrize("bad", [True, 0, -1, 1.5])
def test_invalid_revisions_are_rejected(repository, bad):
    saved = repository.ingest(source(), draft())
    for action in (lambda: repository.accept(saved.id, bad), lambda: repository.reject(saved.id, bad),
                   lambda: repository.forget(saved.id, bad), lambda: repository.get(saved.id, revision=bad)):
        with pytest.raises(MemoryError):
            action()


def test_closed_repository_refuses_all_operations_and_close_is_idempotent(repository):
    repository.close()
    repository.close()
    for action in (lambda: repository.epoch, lambda: repository.get("id"), lambda: repository.list_items(),
                   lambda: repository.ingest(source(), draft()), lambda: repository.accept("id", 1),
                   lambda: repository.reject("id", 1), lambda: repository.forget("id", 1),
                   lambda: repository.claim_learning("opaque-task-id"),
                   lambda: repository.revise("id", 1, source(), draft()),
                   lambda: repository.put_embedding("id", 1, MANIFEST, (1.0, 0.0, 0.0)),
                   lambda: repository.embeddings(MANIFEST), lambda: repository.__enter__()):
        with pytest.raises(RuntimeError, match="closed"):
            action()
