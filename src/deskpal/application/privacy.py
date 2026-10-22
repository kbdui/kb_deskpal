"""Durable use blocking ordered with response publication, not network waits."""

from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from threading import RLock
from typing import Any

from deskpal.workflow.humanization import HumanizationStore
from deskpal.workflow.journal import ExecutionJournal
from deskpal.workflow.models import WorkflowBlocked
from deskpal.workflow.store import canonical


class PrivacyCoordinator:
    def __init__(self, persistence: HumanizationStore, journal: ExecutionJournal) -> None:
        self.persistence, self.journal = persistence, journal
        self._lock = RLock()

    def check(self, epoch: int) -> None:
        value = self.persistence.privacy()
        if value["blocked"] or value["epoch"] != epoch:
            raise WorkflowBlocked("Privacy context expired or cleanup is pending")

    @contextmanager
    def publication(self, epoch: int) -> Iterator[None]:
        with self._lock:
            self.check(epoch)
            yield

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Short control ordering also remains usable while cleanup is blocked."""
        with self._lock:
            yield

    def prior(self, command_id: str, memory_id: str, revision: int) -> dict[str, Any] | None:
        digest = self._digest(memory_id, revision)
        with self.persistence.store.transaction() as conn:
            row = conn.execute("SELECT * FROM privacy_request WHERE command_id=?", (command_id,)).fetchone()
            if row is None:
                return None
            if row["payload_hash"] != digest:
                raise ValueError("Privacy command ID was already used with another request")
            job = conn.execute("SELECT status FROM deletionjob WHERE id=?", (row["job_id"],)).fetchone()
            completed = job is not None and job[0] == "Completed"
            return {"accepted": True, "status": "Completed" if completed else "AcceptedPending",
                    "job_id": row["job_id"], "memory_id": memory_id,
                    "privacy_epoch": row["epoch"], "blocked": self.persistence.privacy()["blocked"]}

    @staticmethod
    def _digest(memory_id: str, revision: int) -> str:
        return sha256(canonical({"memory_id": memory_id, "revision": revision}).encode("utf-8")).hexdigest()

    def begin(self, memory_id: str, revision: int, command_id: str) -> dict[str, Any]:
        if not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 200:
            raise ValueError("Privacy operations require a stable bounded command ID")
        with self._lock, self.persistence.store.transaction() as conn:
            prior = self.prior(command_id, memory_id, revision)
            if prior is not None:
                return prior
            job_id = self.journal.begin_deletion(memory_id, revision)
            epoch = self.persistence.block_privacy()
            self.persistence.discard_effects()
            conn.execute("INSERT INTO privacy_request VALUES(?,?,?,?,?,?)",
                         (command_id, self._digest(memory_id, revision), job_id, memory_id, revision, epoch))
            return {"accepted": True, "status": "AcceptedPending", "job_id": job_id,
                    "memory_id": memory_id, "privacy_epoch": epoch, "blocked": True,
                    "retained": ["already admitted external requests and started tool effects",
                                 "historical logs, backups, exports and external copies"]}
