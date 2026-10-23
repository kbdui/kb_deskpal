"""Four-store content/WAL evidence using only synthetic temporary SQLite data."""

from contextlib import ExitStack, closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import zipfile

import pytest

from deskpal.companion import CompanionRuntime
from deskpal.credentials import WindowsCredentialStore
from deskpal.maintenance.archives import DATABASES, EXCLUSIONS, create_backup, restore_backup
from deskpal.memory.contracts import MemoryDraft, MemorySource, UsePolicy
from deskpal.memory.repository import SQLiteMemoryRepository
from deskpal.planner.models import Plan
from deskpal.scheduling import SchedulingService
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import PermissionLevel, ToolCall, ToolSpec
from deskpal.workflow import ExecutionJournal, PersistentTaskManager, WorkflowStore
from deskpal.workflow.humanization import HumanizationStore


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("DESKPAL_") or name == "OPENAI_API_KEY":
            monkeypatch.delenv(name, raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("Synthetic backup must never access system credentials")

    for operation in ("read", "write", "delete"):
        monkeypatch.setattr(WindowsCredentialStore, operation, forbidden)


def open_stores(directory, owned):
    factories = (
        lambda: WorkflowStore(directory / DATABASES[0]),
        lambda: SQLiteMemoryRepository(directory / DATABASES[1]),
        lambda: SchedulingService(directory / DATABASES[2]),
        lambda: CompanionRuntime(directory / DATABASES[3], wall_clock=lambda: 1_800_000_000.0,
                                 monotonic_clock=lambda: 100.0),
    )
    stores = []
    for factory in factories:
        store = factory()
        owned.callback(store.close)
        stores.append(store)
    return stores


def test_backup_restores_four_store_business_content_committed_only_in_wal(tmp_path):
    source = tmp_path / "synthetic-data"
    source.mkdir()
    # Build known schemas first, then establish an empty checkpoint baseline.
    with ExitStack() as owned:
        open_stores(source, owned)

    with ExitStack() as keepers:
        connections = {}
        for name in DATABASES:
            connection = keepers.enter_context(closing(sqlite3.connect(source / name)))
            connections[name] = connection
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
            connection.execute("PRAGMA wal_autocheckpoint=0")
            assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0

        # Keep an idle connection per database open so closing the business
        # adapters cannot trigger SQLite's final-connection checkpoint.
        with ExitStack() as owned:
            workflow, memory, scheduling, companion = open_stores(source, owned)
            memory_source = MemorySource(
                "synthetic backup preference", id="synthetic-source-identity",
                session_id="synthetic-session", use_policy=UsePolicy.LOCAL_ONLY,
                created_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
            )
            item = memory.ingest(
                memory_source,
                MemoryDraft("synthetic backup preference", "synthetic backup preference"),
                accepted=True, idempotency_key="synthetic-ingestion-identity",
            )
            forgotten = memory.ingest(
                MemorySource("synthetic removed memory", id="synthetic-removed-source",
                             use_policy=UsePolicy.LOCAL_ONLY),
                MemoryDraft("synthetic removed memory", "synthetic removed memory"),
                accepted=True,
            )
            deletion_receipt = memory.forget(forgotten.id, forgotten.revision)
            tasks, journal = PersistentTaskManager(workflow), ExecutionJournal(workflow)
            task = tasks.submit("synthetic backup request", command_id="synthetic-command")
            tasks.update_status(task.id, TaskStatus.PLANNING)
            plan = journal.save_plan(
                task.id, Plan("synthetic reply", (ToolCall("echo", {"text": "synthetic argument"}),)),
                (ToolSpec("echo", "synthetic tool", {"type": "object"}, PermissionLevel(0)),),
                str(tmp_path / "workspace"),
            )
            tasks.update_status(task.id, TaskStatus.WAITING)
            humanization = HumanizationStore(workflow)
            call_id = humanization.reserve_call(task.id, "expression")
            humanization.finish_call(call_id, "unknown")
            deletion_id = journal.begin_deletion(item.id, item.revision)
            calendar = scheduling.create_calendar(
                "synthetic calendar", "2030-01-01T12:00:00+00:00", "UTC",
                reminder_at="2000-01-01T11:00:00+00:00",
            )
            timer = scheduling.create_timer(3600, label="synthetic timer")
            assert scheduling.poll_due() == 1
            schedule_identity = scheduling.store_identity
            configured = companion.command(
                "configure", expected_revision=companion.snapshot()["revision"],
                enabled=True, emotions_enabled=True, preset="warm",
                command_id="synthetic-persona-command",
            )
            companion.record_outcome("synthetic-outcome", "Completed", tool_success=True,
                                     use_policy="local_only")
            assert configured["persona"]["preset"] == "warm"

        witnesses = {
            DATABASES[0]: ("SELECT COUNT(*) FROM task WHERE id=?", task.id),
            DATABASES[1]: ("SELECT COUNT(*) FROM memory_sources WHERE id=?", memory_source.id),
            DATABASES[2]: ("SELECT COUNT(*) FROM calendar WHERE id=?", calendar.id),
            DATABASES[3]: ("SELECT COUNT(*) FROM receipts WHERE task_id=?", "synthetic-outcome"),
        }
        snapshots = {}
        for name, (query, identity) in witnesses.items():
            connection = connections[name]
            assert connection.execute(query, (identity,)).fetchone() == (1,)
            wal = Path(str(source / name) + "-wal")
            assert wal.stat().st_size > 32, name
            main_only = tmp_path / ("main-only-" + name)
            shutil.copyfile(source / name, main_only)
            with closing(sqlite3.connect(main_only.as_uri() + "?mode=ro&immutable=1", uri=True)) as baseline:
                # Presence of a WAL file alone is insufficient: this proves
                # the committed business record is absent from the main file.
                assert baseline.execute(query, (identity,)).fetchone() == (0,), name
            snapshots[name] = tuple(connection.iterdump())

        workflow_connection = connections[DATABASES[0]]
        assert workflow_connection.execute(
            "SELECT hash,canonical_json FROM immutableplan WHERE task_id=?", (task.id,),
        ).fetchone() == (plan.hash, plan.canonical_json)
        assert workflow_connection.execute(
            "SELECT memory_id,expected_revision,status FROM deletionjob WHERE id=?", (deletion_id,),
        ).fetchone() == (item.id, item.revision, "Pending")
        assert workflow_connection.execute("SELECT memory_id FROM memory_block").fetchall() == [(item.id,)]
        assert workflow_connection.execute(
            "SELECT purpose,slot,status FROM task_call_attempt WHERE id=?", (call_id,),
        ).fetchone() == ("expression", 1, "unknown")
        memory_connection = connections[DATABASES[1]]
        assert memory_connection.execute(
            "SELECT revision,epoch FROM memory_tombstones WHERE id=?", (forgotten.id,),
        ).fetchone() == (deletion_receipt.revision, deletion_receipt.epoch)
        assert memory_connection.execute(
            "SELECT id FROM memory_source_tombstones WHERE id='synthetic-removed-source'",
        ).fetchone() == ("synthetic-removed-source",)
        assert connections[DATABASES[2]].execute(
            "SELECT id,label,status FROM timers WHERE id=?", (timer.id,),
        ).fetchone() == (timer.id, "synthetic timer", "pending")
        assert connections[DATABASES[2]].execute(
            "SELECT source,source_id,revision FROM events",
        ).fetchall() == [("calendar", calendar.id, 1)]
        assert dict(connections[DATABASES[2]].execute("SELECT key,value FROM scheduling_identity")) == {
            "store_id": schedule_identity.store_id, "generation": schedule_identity.generation,
        }
        companion_connection = connections[DATABASES[3]]
        assert companion_connection.execute("SELECT COUNT(*) FROM persona_versions").fetchone()[0] >= 2
        assert companion_connection.execute("SELECT COUNT(*) FROM budget").fetchone() == (1,)
        assert companion_connection.execute(
            "SELECT eligibility FROM receipts WHERE task_id='synthetic-outcome'",
        ).fetchone() == ("eligible",)

        # Excluded data really exists beside the databases, but the archive
        # must still contain only its fixed manifest and four snapshots.
        for name in ("config.toml", ".env", "execution.jsonl", "credentials.txt"):
            (source / name).write_text("synthetic excluded data", encoding="utf-8")
        for name in ("workspace", "models"):
            (source / name).mkdir()
            (source / name / "private.txt").write_text("synthetic excluded file", encoding="utf-8")
        archive = tmp_path / "synthetic-backup.zip"
        receipt = create_backup(source, archive, acknowledge_sensitive_data=True)
        assert receipt["manifest"]["exclusions"] == EXCLUSIONS
        with zipfile.ZipFile(archive) as bundle:
            assert set(bundle.namelist()) == {"manifest.json", *DATABASES}
            assert json.loads(bundle.read("manifest.json"))["databases"] == receipt["manifest"]["databases"]
        target = tmp_path / "restored-data"
        result = restore_backup(archive, target, acknowledge_data_rollback=True, current_data_dir=source)
        assert result["privacy_deletion_completed"] is False
        for name in DATABASES:
            with closing(sqlite3.connect(target / name)) as restored:
                assert restored.execute("PRAGMA quick_check").fetchall() == [("ok",)]
                # Includes all tables, identities, budgets, receipts, tombstones
                # and sqlite_sequence, rather than only four file hashes.
                assert tuple(restored.iterdump()) == snapshots[name], name
            assert tuple(connections[name].iterdump()) == snapshots[name], name
