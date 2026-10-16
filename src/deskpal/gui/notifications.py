"""Privacy-preserving, bounded desktop notification adapter."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Callable, Protocol, TypeAlias

from PySide6.QtCore import QObject, QSettings, Signal

from .preferences import GuiPreferences

FeedItem: TypeAlias = Mapping[str, object]
Identity: TypeAlias = tuple[str, str, int]
NavigationTarget: TypeAlias = tuple[str, str]


class NotificationPort(Protocol):
    def show_message(self, message: str) -> None: ...


class NotificationController(QObject):
    """Turns an incremental notification feed into generic reminders."""

    notification_requested = Signal(str)
    navigation_requested = Signal(str, str)
    revalidation_requested = Signal(object)

    _LEDGER_KEY = "gui/notification_ledger"
    _CURSOR_KEY = "gui/notification_after_sequence"
    _BASELINE_KEY = "gui/notification_baseline_complete"
    _LIMIT = 4096
    _PENDING_LIMIT = 20
    _CHANNEL = "desktop"
    _TASK_STATUSES = frozenset({"Waiting", "Completed", "Failed", "Cancelled"})

    def __init__(
        self,
        preferences: GuiPreferences,
        settings: QSettings | None = None,
        show_message: Callable[[str], None] | NotificationPort | None = None,
        clock: Callable[[], datetime] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._preferences = preferences
        self._settings = settings if settings is not None else QSettings()
        if show_message is None:
            self._show_message = None
        elif callable(show_message):
            self._show_message = show_message
        else:
            self._show_message = show_message.show_message
        self._clock = clock if clock is not None else lambda: datetime.now().astimezone()
        self._pending: deque[Identity] = deque(maxlen=self._PENDING_LIMIT)
        self._enabled = preferences.notifications_enabled
        self._baseline_in_progress = not self._read_bool(self._BASELINE_KEY, False)
        self._validation_batch: tuple[int, tuple[Identity, ...]] | None = None
        self._validation_serial = 0
        self._last_navigation: NavigationTarget | None = None
        self._ledger = self._load_ledger()
        self._after_sequence = self._load_cursor()
        preferences.changed.connect(self._preference_changed)

    def _read_bool(self, key: str, default: bool) -> bool:
        raw = self._settings.value(key, default)
        if isinstance(raw, bool):
            return raw
        return str(raw).casefold() in {"1", "true", "yes"}

    def _load_ledger(self) -> list[str]:
        raw = self._settings.value(self._LEDGER_KEY, "[]")
        try:
            if isinstance(raw, str):
                encoded = raw
            elif isinstance(raw, (bytes, bytearray)):
                encoded = bytes(raw).decode("utf-8")
            else:
                return []
            values = json.loads(encoded)
        except (ValueError, TypeError, UnicodeDecodeError):
            return []
        if not isinstance(values, list):
            return []
        return [item for item in values if isinstance(item, str)][-self._LIMIT:]

    def _load_cursor(self) -> int | None:
        raw = self._settings.value(self._CURSOR_KEY, None)
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            value = raw
        elif isinstance(raw, str):
            try:
                value = int(raw)
            except ValueError:
                return None
        else:
            return None
        return value if value >= 0 else None

    @property
    def after_sequence(self) -> int | None:
        """Cursor to pass to the next feed request; None asks the backend to baseline."""
        return None if self._baseline_in_progress else self._after_sequence

    @property
    def baseline_required(self) -> bool:
        return self._baseline_in_progress

    @property
    def pending_candidates(self) -> tuple[NavigationTarget, ...]:
        """IDs only, for the backend's current-status/TTL revalidation query."""
        return tuple((kind, entity_id) for kind, entity_id, _revision in self._pending)

    def _preference_changed(self, key: str, value: object) -> None:
        if key == "notifications_enabled":
            self.set_enabled(value is True)

    def set_enabled(self, enabled: bool) -> None:
        if enabled == self._enabled:
            return
        self._enabled = enabled
        self.clear_pending()
        self._baseline_in_progress = enabled
        if enabled:
            self._settings.setValue(self._BASELINE_KEY, False)
            self._settings.sync()

    def _identity(self, item: FeedItem) -> Identity | None:
        kind = item.get("kind")
        entity_id = item.get("entity_id", item.get("id"))
        revision = item.get("revision")
        status = item.get("status")
        if kind not in {"task", "proposal"} or not isinstance(entity_id, str) or not entity_id:
            return None
        if len(entity_id) > 512 or isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            return None
        if kind == "task" and status not in self._TASK_STATUSES:
            return None
        if kind == "proposal" and status != "Pending":
            return None
        return kind, entity_id, revision

    def _ledger_entry(self, identity: Identity) -> str:
        kind, entity_id, revision = identity
        return json.dumps(
            [entity_id, revision, f"{self._CHANNEL}:{kind}"],
            ensure_ascii=True,
            separators=(",", ":"),
        )

    def _remember(self, identity: Identity) -> bool:
        entry = self._ledger_entry(identity)
        if entry in self._ledger:
            return False
        self._ledger.append(entry)
        self._ledger = self._ledger[-self._LIMIT:]
        return True

    def _save_ledger(self) -> None:
        self._settings.setValue(self._LEDGER_KEY, json.dumps(self._ledger, separators=(",", ":")))
        self._settings.sync()

    def ingest(
        self,
        items: Sequence[FeedItem],
        *,
        next_sequence: int | None = None,
        until_sequence: int | None = None,
        has_more: bool = False,
        baseline: bool = False,
        version: int | None = None,
        offset: int | None = None,
    ) -> None:
        """Consume one fixed-high-watermark feed page and persist only full cursors."""
        del until_sequence  # The bridge holds this high watermark fixed during pagination.
        del offset  # Accepted for callers still using the former feed signature.
        if next_sequence is None:
            next_sequence = version
        is_baseline = baseline or self._baseline_in_progress or not self._enabled
        ledger_changed = False
        for item in items:
            identity = self._identity(item)
            if identity is not None:
                fresh = self._remember(identity)
                ledger_changed = ledger_changed or fresh
                if fresh and self._enabled and not is_baseline:
                    self._pending.append(identity)

        if ledger_changed:
            self._save_ledger()
        if has_more:
            return
        if is_baseline:
            self._baseline_in_progress = False
            self._settings.setValue(self._BASELINE_KEY, True)
            self._settings.sync()
        # Commit the sequence only after its IDs and baseline state are durable.
        if next_sequence is not None and not isinstance(next_sequence, bool) and next_sequence >= 0:
            self._settings.setValue(self._CURSOR_KEY, next_sequence)
            self._settings.sync()
            self._after_sequence = next_sequence
        self.tick()

    def _is_quiet(self, now: datetime) -> bool:
        # The default clock is system-local aware time; an injected aware clock
        # retains its timezone so DST boundaries remain testable.
        local = now if now.tzinfo is not None else now.astimezone()
        hour = local.hour
        start = self._preferences.quiet_start
        end = self._preferences.quiet_end
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def tick(self) -> None:
        """Request a current-state check; never display stale queued state directly."""
        if not self._enabled or not self._pending or self._is_quiet(self._clock()):
            return
        if self._validation_batch is not None:
            return
        self._validation_serial += 1
        request_id = self._validation_serial
        batch = tuple(self._pending)
        self._validation_batch = (request_id, batch)
        candidates = tuple((kind, entity_id) for kind, entity_id, _revision in batch)
        self.revalidation_requested.emit({"request_id": request_id, "candidates": candidates})

    def revalidated(self, valid_items: Sequence[FeedItem], request_id: int | None = None) -> None:
        """Complete the current recheck, displaying only still-valid revisions."""
        pending_batch = self._validation_batch
        if pending_batch is None or (request_id is not None and request_id != pending_batch[0]):
            return
        if self._is_quiet(self._clock()):
            # The response crossed into quiet time. Retain all candidates and
            # require a fresh backend check when quiet hours end.
            self._validation_batch = None
            return
        self._validation_batch = None
        batch = pending_batch[1]
        batch_set = set(batch)
        valid_set = {identity for item in valid_items if (identity := self._identity(item)) in batch_set}
        self._pending = deque((identity for identity in self._pending if identity not in batch_set),
                              maxlen=self._PENDING_LIMIT)
        if valid_set and self._enabled:
            latest = next(identity for identity in reversed(batch) if identity in valid_set)
            self._last_navigation = (latest[0], latest[1])
            message = f"DeskPal 有 {len(valid_set)} 项更新，请打开应用查看。"
            self.notification_requested.emit(message)
            if self._show_message is not None:
                try:
                    self._show_message(message)
                except Exception:
                    # OS suppression/failure does not affect feed state.
                    pass
        self.tick()

    def clear_pending(self) -> None:
        self._pending.clear()
        self._last_navigation = None
        self._validation_serial += 1
        self._validation_batch = None

    def activate_notification(self) -> None:
        """Request navigation only; the receiver must reload and validate the item."""
        if self._last_navigation is not None:
            self.navigation_requested.emit(*self._last_navigation)
