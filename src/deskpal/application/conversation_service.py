"""Short conversation control operations ordered with late publications."""

from collections.abc import Callable
from threading import RLock
from typing import Any

from deskpal.conversation.repository import ConversationRepository
from deskpal.conversation.runtime import ConversationRuntime
from deskpal.workflow.tasks import PersistentTaskManager

from .dispatcher import SubmissionDispatcher
from .privacy import PrivacyCoordinator


class ConversationService:
    def __init__(self, repository: ConversationRepository, runtime: ConversationRuntime,
                 tasks: PersistentTaskManager, privacy: PrivacyCoordinator,
                 dispatcher: SubmissionDispatcher,
                 revoke: Callable[[tuple[str, ...]], None],
                 discard: Callable[[tuple[str, ...]], None]) -> None:
        self.repository, self.runtime, self.tasks = repository, runtime, tasks
        self.privacy, self.dispatcher, self.revoke = privacy, dispatcher, revoke
        self.discard = discard
        self._lock = RLock()
        self._pending_revoke: set[str] = set()

    def cleanup(self, identity: str, expected_revision: int, command_id: str, *, delete: bool) -> dict[str, Any]:
        affected: list[str] = []

        def scrub(task_id: str) -> None:
            self.tasks.request_cancel(task_id)
            self.tasks.scrub(task_id)
            affected.append(task_id)

        with self.privacy.exclusive():
            result = self.repository.cleanup(identity, expected_revision, command_id, delete=delete, scrub=scrub)
        # No SQLite lock while acquiring the dispatcher's condition; submit
        # owns that condition before entering its atomic registration transaction.
        self.runtime.invalidate()
        self.discard(tuple(affected))
        self.dispatcher.clear_cached(affected)
        with self._lock:
            self._pending_revoke.update(affected)
        try:
            with self.dispatcher.management():
                self.drain()
        except RuntimeError:
            pass  # The active worker drains at its next safe idle boundary.
        return result

    def drain(self) -> None:
        """Called only by the sole worker at its safe idle boundary."""
        with self._lock:
            pending = tuple(self._pending_revoke)
            self.revoke(pending)
            self._pending_revoke.difference_update(pending)
