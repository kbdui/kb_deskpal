"""Bounded, local-only consumption of reliable terminal tool outcomes."""

from collections.abc import Callable
from datetime import datetime, timezone

from deskpal.agent.ports import CompanionPort
from deskpal.workflow.humanization import HumanizationStore

from .privacy import PrivacyCoordinator


class CompanionEffects:
    def __init__(self, persistence: HumanizationStore, privacy: PrivacyCoordinator,
                 companion: CompanionPort | None, *, should_stop: Callable[[], bool] | None = None) -> None:
        self.persistence, self.privacy, self.companion = persistence, privacy, companion
        self.should_stop = should_stop if should_stop is not None else lambda: False

    def drain(self) -> None:
        if self.should_stop():
            return
        if self.companion is None:
            self.persistence.discard_effects()
            return
        # The durable queue itself is capped at 4096. Consume bounded pages so
        # startup recovery does not need a new user task to reach later rows.
        # Failures leave the unacknowledged page for the next safe delivery.
        remaining = 4096
        while remaining:
            events = self.persistence.pending_effects(min(100, remaining))
            if not events:
                return
            for event in events:
                if self.should_stop():
                    return
                with self.privacy.publication(int(self.persistence.privacy()["epoch"])):
                    now = datetime.now(timezone.utc)
                    recorded = datetime.fromisoformat(event["created_at"])
                    age = (now - recorded).total_seconds()
                    if event["epoch"] != self.persistence.privacy()["epoch"] or not 0 <= age <= 60:
                        self.persistence.ack_effect(event["id"])
                        remaining -= 1
                        continue
                    # The companion transaction owns root dedup, state and budgets.
                    # An acknowledgement failure repeats delivery, never a tool.
                    self.companion.record_outcome(event["task_id"], event["status"],
                        tool_success=bool(event["success"]) and not event["failure"],
                        use_policy=self.persistence.task_policy(event["task_id"]),
                        occurred_at=recorded.timestamp())
                    self.persistence.ack_effect(event["id"])
                    remaining -= 1
