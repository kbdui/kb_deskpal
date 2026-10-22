"""HM07: frozen schema 1 fixtures, real SQLite upgrades and split-store recovery.

Workflow DDL is copied from HEAD's schema 1 store, rather than creating a new
schema 2 database and changing its version. Companion DDL is the original
four-table schema retained in repository.py. All content is synthetic.
"""

from hashlib import sha256
import json
import sqlite3

import pytest

from deskpal.application import create_application
from deskpal.companion import CompanionRuntime, CompanionStorageError
from deskpal.config import AppConfig, SandboxConfig, StorageConfig
from deskpal.tasks.models import TaskStatus
from deskpal.workflow import ExecutionJournal, PersistentTaskManager, WorkflowStore


WORKFLOW_V1 = (
    "CREATE TABLE schema_version(version INTEGER NOT NULL)",
    "INSERT INTO schema_version VALUES(1)",
    "CREATE TABLE task(id TEXT PRIMARY KEY,request TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,result TEXT,error TEXT,revision INTEGER NOT NULL DEFAULT 1,cancel_requested INTEGER NOT NULL DEFAULT 0,use_policy TEXT NOT NULL DEFAULT 'cloud_allowed',scrubbed INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE submission(sequence INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL UNIQUE REFERENCES task(id),status TEXT NOT NULL DEFAULT 'Queued')",
    "CREATE TABLE command(id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,task_id TEXT NOT NULL REFERENCES task(id))",
    "CREATE TABLE immutableplan(id TEXT PRIMARY KEY,task_id TEXT NOT NULL UNIQUE REFERENCES task(id),hash TEXT NOT NULL,canonical_json TEXT NOT NULL,scrubbed INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE attempt(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES task(id),step_index INTEGER NOT NULL,status TEXT NOT NULL,result TEXT,review_verdict TEXT,review_evidence TEXT,UNIQUE(task_id,step_index))",
    "CREATE TABLE dependency(task_id TEXT NOT NULL REFERENCES task(id),kind TEXT NOT NULL,object_id TEXT NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(task_id,kind,object_id))",
    "CREATE INDEX dependency_object ON dependency(kind,object_id)",
    "CREATE TABLE dependency_version(kind TEXT NOT NULL,object_id TEXT NOT NULL,revision INTEGER NOT NULL,active INTEGER NOT NULL,use_policy TEXT NOT NULL,PRIMARY KEY(kind,object_id))",
    "CREATE TABLE audit_outbox(sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,event TEXT NOT NULL,task_id TEXT,data TEXT NOT NULL,delivered INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE deletionjob(id TEXT PRIMARY KEY,memory_id TEXT NOT NULL,expected_revision INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'Pending')",
    "CREATE TABLE memory_block(memory_id TEXT PRIMARY KEY)",
)
COMPANION_V1 = (
    "CREATE TABLE state(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)",
    "CREATE TABLE persona_versions(version INTEGER PRIMARY KEY,payload TEXT NOT NULL)",
    "CREATE TABLE receipts(sequence INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL UNIQUE,status TEXT NOT NULL,accepted_utc REAL NOT NULL,epoch INTEGER NOT NULL,eligibility TEXT NOT NULL,delta_v REAL NOT NULL,delta_a REAL NOT NULL,delta_m REAL NOT NULL)",
    "CREATE TABLE budget(sequence INTEGER PRIMARY KEY,accepted_utc REAL NOT NULL,delta_v REAL NOT NULL,delta_a REAL NOT NULL,delta_m REAL NOT NULL)",
    "PRAGMA user_version=1",
)
STYLE = {"formality": 0.5, "warmth": 0.5, "humor": 0.2, "verbosity": 0.5,
         "expressiveness": 0.4, "curiosity": 0.3}
NOW = 1_800_000_000.0
TASK_ID = "synthetic-waiting-task"


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def read_rows(path, query):
    with sqlite3.connect(path) as connection:
        return connection.execute(query).fetchall()


def companion_rows(path):
    return {table: read_rows(path, f"SELECT * FROM {table} ORDER BY 1")
            for table in ("state", "persona_versions", "receipts", "budget")}


@pytest.fixture
def legacy_stores(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    workflow_path, companion_path = data / "workflow.sqlite3", data / "companion.sqlite3"
    plan = {"task_id": TASK_ID, "reply": "synthetic original waiting plan",
            "steps": [{"name": "file_write", "arguments": {"path": "synthetic.txt", "content": "synthetic"},
                       "call_id": "legacy-call-0"}],
            "tool_specs": [{"name": "file_write", "description": "synthetic trusted tool",
                            "parameters": {"type": "object"}, "permission_level": 1, "contract_version": "1"}],
            "workspace_dir": str(tmp_path / "workspace"), "memory_refs": [], "dependencies": [],
            "policy_version": "1", "use_policy": "cloud_allowed"}
    canonical_plan = encode(plan)
    digest = sha256(canonical_plan.encode("utf-8")).hexdigest()
    with sqlite3.connect(workflow_path) as connection:
        for statement in WORKFLOW_V1:
            connection.execute(statement)
        connection.execute("INSERT INTO task(id,request,status,created_at,updated_at,revision) VALUES(?,?,?,?,?,?)",
                           (TASK_ID, "synthetic request", "Waiting", "2026-10-22T00:00:00+00:00",
                            "2026-10-22T00:00:00+00:00", 4))
        connection.execute("INSERT INTO submission(task_id,status) VALUES(?,?)", (TASK_ID, "Claimed"))
        connection.execute("INSERT INTO immutableplan(id,task_id,hash,canonical_json) VALUES(?,?,?,?)",
                           ("legacy-plan-id", TASK_ID, digest, canonical_plan))
    persona = {"version": 2, "preset": "balanced", "style": STYLE}
    state = {"schema_version": 1, "reducer_version": 1, "revision": 7, "reset_epoch": 0,
             "enabled": True, "emotions_enabled": True, "persona": persona,
             "anchor_utc": NOW, "last_seen_utc": NOW, "values": [0.04, 0.19, 0.01],
             "preferences": {"persistent": {"verbosity": "brief", "humor_allowed": False}},
             "outcome_mode": "neutral", "outcome_mode_until": 0.0}
    with sqlite3.connect(companion_path) as connection:
        for statement in COMPANION_V1:
            connection.execute(statement)
        connection.execute("INSERT INTO state VALUES(1,?)", (encode(state),))
        for version in (1, 2):
            connection.execute("INSERT INTO persona_versions VALUES(?,?)", (version, encode({**persona, "version": version})))
        connection.execute("INSERT INTO receipts VALUES(1,?,?,?,?,?,?,?,?)",
                           ("synthetic-terminal-task", "Completed", NOW - 10, 0, "eligible", 0.02, 0.01, 0.005))
        connection.execute("INSERT INTO budget VALUES(1,?,?,?,?)", (NOW - 10, 0.02, 0.01, 0.005))
    assert read_rows(workflow_path, "SELECT name FROM pragma_table_info('task') WHERE name='privacy_epoch'") == []
    assert read_rows(companion_path, "SELECT name FROM sqlite_master WHERE name='interaction_settings'") == []
    return data, workflow_path, companion_path, canonical_plan, digest


def assert_legacy_plan(store, canonical_plan, digest):
    review = ExecutionJournal(store).review(TASK_ID)
    plan = review["plan"]
    assert plan.canonical_json == canonical_plan and plan.hash == digest
    assert plan.policy_version == "1" and plan.context_digest == "" and plan.context_refs == ()
    assert plan.plan.steps[0].arguments == {"path": "synthetic.txt", "content": "synthetic"}
    assert int(plan.tool_specs[0].permission_level) == 1
    assert review["attempts"] == ()
    return review


def test_hm07_real_schema_one_upgrade_preserves_waiting_hash_history_budget_and_unique_backups(legacy_stores):
    data, workflow_path, companion_path, canonical_plan, digest = legacy_stores
    before = companion_rows(companion_path)
    for _ in range(2):
        store = WorkflowStore(workflow_path)
        try:
            review = assert_legacy_plan(store, canonical_plan, digest)
            assert review["task"].status == TaskStatus.WAITING
            assert PersistentTaskManager(store).get(TASK_ID).request == "synthetic request"
            assert read_rows(workflow_path, "SELECT privacy_epoch FROM task") == [(0,)]
            assert read_rows(workflow_path, "SELECT version FROM schema_version") == [(2,)]
        finally:
            store.close()
        runtime = CompanionRuntime(companion_path, wall_clock=lambda: NOW, monotonic_clock=lambda: 100.0)
        try:
            snapshot = runtime.interaction_snapshot()
            assert snapshot["cloud_permissions"] == dict.fromkeys(("custom_persona", "preferences", "cue", "affect", "history"), False)
            assert snapshot["identity_use_policy"] == "local_only"
            assert runtime.snapshot()["persona"]["version"] == 2
            assert runtime.snapshot()["preferences"]["persistent"] == {"verbosity": "brief", "humor_allowed": False}
        finally:
            runtime.close()
        assert companion_rows(companion_path) == before
    workflow_backups = list(data.glob("workflow.sqlite3.schema-1-*.backup"))
    companion_backups = list(data.glob("companion.sqlite3.schema1.*.bak"))
    assert len(workflow_backups) == len(companion_backups) == 1
    assert read_rows(workflow_backups[0], "SELECT version FROM schema_version") == [(1,)]
    assert read_rows(workflow_backups[0], "SELECT hash,canonical_json FROM immutableplan") == [(digest, canonical_plan)]
    assert read_rows(companion_backups[0], "PRAGMA user_version") == [(1,)]
    assert companion_rows(companion_backups[0]) == before


def test_hm07_workflow_upgraded_companion_failure_keeps_original_and_recovers(legacy_stores, monkeypatch, tmp_path):
    from deskpal.application import factory
    from deskpal.companion import repository

    data, workflow_path, companion_path, canonical_plan, digest = legacy_stores
    before = companion_rows(companion_path)
    store = WorkflowStore(workflow_path)
    try:
        assert_legacy_plan(store, canonical_plan, digest)
    finally:
        store.close()
    config = AppConfig(storage=StorageConfig(data), sandbox=SandboxConfig(tmp_path / "workspace"))
    original = repository.migrate_interaction
    # A fixed startup clock isolates migration preservation from the separately
    # tested clock-anomaly policy, which legitimately rebases old budget dates.
    monkeypatch.setattr(factory, "CompanionRuntime", lambda *args, **kwargs: CompanionRuntime(
        *args, **kwargs, wall_clock=lambda: NOW, monotonic_clock=lambda: 100.0))

    def fail_interaction(connection, path, **parameters):
        assert read_rows(workflow_path, "SELECT version FROM schema_version") == [(2,)]
        raise sqlite3.OperationalError("synthetic companion migration failure")

    with monkeypatch.context() as injected:
        injected.setattr(repository, "migrate_interaction", fail_interaction)
        with create_application(config) as application:
            assert "companion_storage_unavailable" in application.companion_snapshot()["warnings"]
            assert_legacy_plan(application.store, canonical_plan, digest)
            assert application.tasks.get(TASK_ID).status == TaskStatus.NEEDS_REVIEW
    assert repository.migrate_interaction is original
    assert companion_rows(companion_path) == before
    assert read_rows(companion_path, "PRAGMA user_version") == [(1,)]
    assert read_rows(companion_path, "SELECT name FROM sqlite_master WHERE name='interaction_settings'") == []
    failed_backup = list(data.glob("companion.sqlite3.schema1.*.bak"))
    assert len(failed_backup) == 1 and companion_rows(failed_backup[0]) == before
    backup_digest = sha256(failed_backup[0].read_bytes()).hexdigest()
    # Complete the failed component against an unchanged clock, then exercise
    # ordinary production reopen. No version downgrade or backup replacement.
    runtime = CompanionRuntime(companion_path, wall_clock=lambda: NOW, monotonic_clock=lambda: 100.0)
    runtime.close()
    with create_application(config) as application:
        assert "companion_storage_unavailable" not in application.companion_snapshot()["warnings"]
        assert_legacy_plan(application.store, canonical_plan, digest)
        assert application.tasks.get(TASK_ID).status == TaskStatus.NEEDS_REVIEW
        assert not any(application.interaction_snapshot()["cloud_permissions"].values())
    backups = list(data.glob("companion.sqlite3.schema1.*.bak"))
    assert len(backups) == 2 and len({path.name for path in backups}) == 2
    assert sha256(failed_backup[0].read_bytes()).hexdigest() == backup_digest
    assert len(list(data.glob("workflow.sqlite3.schema-1-*.backup"))) == 1


@pytest.mark.parametrize("component", ["workflow", "companion"])
def test_hm07_unknown_schema_refused_without_rewrite_or_backup(tmp_path, component):
    path = tmp_path / (component + ".sqlite3")
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sentinel(content TEXT)")
        connection.execute("INSERT INTO sentinel VALUES('synthetic original')")
        if component == "workflow":
            connection.execute("CREATE TABLE schema_version(version INTEGER)")
            connection.execute("INSERT INTO schema_version VALUES(42)")
        else:
            connection.execute("PRAGMA user_version=42")
    if component == "workflow":
        with pytest.raises(ValueError, match="schema"):
            WorkflowStore(path)
    else:
        with pytest.raises(CompanionStorageError):
            CompanionRuntime(path, wall_clock=lambda: NOW, monotonic_clock=lambda: 100.0)
    assert read_rows(path, "SELECT content FROM sentinel") == [("synthetic original",)]
    assert read_rows(path, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name") == (
        [("schema_version",), ("sentinel",)] if component == "workflow" else [("sentinel",)])
    assert read_rows(path, "SELECT version FROM schema_version" if component == "workflow" else "PRAGMA user_version") == [(42,)]
    assert list(tmp_path.glob("*.backup")) == [] and list(tmp_path.glob("*.bak")) == []
