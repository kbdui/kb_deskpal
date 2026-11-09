"""Profile-bound storage, event delivery, and declarative page endpoints."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any

from deskpal.workflow.store import WorkflowStore, canonical

from .manifest import PluginManifest, valid_id


class PluginData:
    """A single plugin's data namespace in the selected profile."""

    def __init__(self, store: WorkflowStore, manifest: PluginManifest) -> None:
        self._store = store
        self.manifest = manifest

    def state(self, key: str, default: Any = None) -> tuple[Any, int]:
        self._check_key(key)
        with self._store.read_connection() as db:
            self._store.check_privacy(db)
            row = db.execute("SELECT value,revision FROM plugin_state WHERE plugin_id=? AND key=?",
                             (self.manifest.id, key)).fetchone()
            return (json.loads(row["value"]), row["revision"]) if row else (default, 0)

    def set_state(self, key: str, value: Any, *, expected_revision: int) -> int:
        self._check_key(key)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Invalid plugin state revision")
        encoded = canonical(value)
        if len(encoded.encode("utf-8")) > 8192:
            raise ValueError("Plugin state value exceeds its byte limit")
        with self._store.transaction() as db:
            self._store.check_privacy(db)
            row = db.execute("SELECT value,revision FROM plugin_state WHERE plugin_id=? AND key=?",
                             (self.manifest.id, key)).fetchone()
            revision = row["revision"] if row else 0
            if row and row["value"] == encoded:
                return revision  # Repeating a completed state change is harmless.
            if revision != expected_revision:
                raise ValueError("Plugin state changed; read its current revision")
            if row is None:
                count = db.execute("SELECT COUNT(*) FROM plugin_state WHERE plugin_id=?",
                                   (self.manifest.id,)).fetchone()[0]
                if count >= 64:
                    raise ValueError("Plugin state key limit reached")
            db.execute("""INSERT INTO plugin_state VALUES(?,?,?,?)
                          ON CONFLICT(plugin_id,key) DO UPDATE SET value=excluded.value,revision=excluded.revision""",
                       (self.manifest.id, key, encoded, revision + 1))
            return revision + 1

    def _check_key(self, key: str) -> None:
        if not self.manifest.storage or not valid_id(key):
            raise ValueError("Plugin state key or storage capability is invalid")

    def emit(self, source: str, event_id: str, occurred_at: str, payload: Mapping[str, Any]) -> None:
        """Commit one event, reusing the caller's transaction when already open."""
        self._check_source(source)
        if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 200:
            raise ValueError("Plugin events require a stable identity")
        if not isinstance(occurred_at, str) or len(occurred_at) > 64:
            raise ValueError("Plugin events require a timestamp")
        stamp = datetime.fromisoformat(occurred_at)
        if stamp.utcoffset() is None:
            raise ValueError("Plugin event timestamp needs an offset")
        stamp_text = stamp.astimezone(timezone.utc).isoformat()
        encoded = canonical(dict(payload))
        if len(encoded.encode("utf-8")) > 8192:
            raise ValueError("Plugin event payload exceeds its byte limit")
        with self._store.transaction() as db:
            self._store.check_privacy(db)
            inserted = db.execute(
                """INSERT INTO plugin_event(plugin_id,source,event_id,occurred_at,payload) VALUES(?,?,?,?,?)
                   ON CONFLICT(plugin_id,source,event_id) DO NOTHING""",
                (self.manifest.id, source, event_id, stamp_text, encoded)).rowcount
            if inserted:
                db.execute("""DELETE FROM plugin_event WHERE sequence IN (
                              SELECT sequence FROM plugin_event WHERE plugin_id=? AND source=?
                              ORDER BY sequence DESC LIMIT -1 OFFSET 500)""", (self.manifest.id, source))

    def events(self, source: str, *, after_sequence: int | None = None,
               limit: int = 20) -> list[dict[str, Any]]:
        """Read the newest events, or subscribe by polling forward from a cursor."""
        self._check_source(source)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid plugin event page size")
        if after_sequence is not None and (type(after_sequence) is not int or after_sequence < 0
                                           or after_sequence > 2**63 - 1):
            raise ValueError("Invalid plugin event cursor")
        with self._store.read_connection() as db:
            self._store.check_privacy(db)
            if after_sequence is None:
                rows = db.execute("""SELECT * FROM plugin_event WHERE plugin_id=? AND source=?
                                     ORDER BY sequence DESC LIMIT ?""",
                                  (self.manifest.id, source, limit)).fetchall()
            else:
                if after_sequence:
                    oldest = db.execute("""SELECT MIN(sequence) FROM plugin_event
                                           WHERE plugin_id=? AND source=?""",
                                        (self.manifest.id, source)).fetchone()[0]
                    if oldest is not None and after_sequence < oldest:
                        raise ValueError("Plugin event cursor fell outside its retention window")
                rows = db.execute("""SELECT * FROM plugin_event WHERE plugin_id=? AND source=? AND sequence>?
                                     ORDER BY sequence LIMIT ?""",
                                  (self.manifest.id, source, after_sequence, limit)).fetchall()
        return [dict(sequence=row["sequence"], event_id=row["event_id"], occurred_at=row["occurred_at"],
                     payload=json.loads(row["payload"])) for row in rows]

    def _check_source(self, source: str) -> None:
        if source not in self.manifest.event_sources:
            raise ValueError("Undeclared plugin event source")


@dataclass(frozen=True)
class PageSwitch:
    key: str
    label: str
    default: bool = False


@dataclass(frozen=True)
class PageSpec:
    switches: tuple[PageSwitch, ...]
    rows: Callable[[PluginData, Mapping[str, bool]], list[dict[str, str]]]


class PluginRuntime:
    def __init__(self, store: WorkflowStore, manifests: Iterable[PluginManifest]) -> None:
        self._data = {manifest.id: PluginData(store, manifest) for manifest in manifests}
        self._pages: dict[str, PageSpec] = {}

    def data(self, plugin_id: str) -> PluginData:
        if not valid_id(plugin_id) or plugin_id not in self._data:
            raise ValueError("Plugin is not registered")
        return self._data[plugin_id]

    def register_page(self, plugin_id: str, page: PageSpec) -> None:
        manifest = self.data(plugin_id).manifest
        keys = [option.key for option in page.switches]
        if (not manifest.page_title or plugin_id in self._pages or len(keys) > 8 or len(set(keys)) != len(keys)
                or any(not valid_id(key) for key in keys) or (keys and not manifest.storage)
                or any(type(option.default) is not bool or not option.label.strip() or len(option.label) > 120
                       for option in page.switches)):
            raise ValueError("Invalid plugin page registration")
        self._pages[plugin_id] = page

    def page(self, plugin_id: str) -> dict[str, Any]:
        data, spec = self._page(plugin_id)
        controls = []
        values: dict[str, bool] = {}
        for option in spec.switches:
            value, revision = data.state(option.key, option.default)
            if type(value) is not bool:
                raise ValueError("Invalid stored plugin preference")
            controls.append(dict(key=option.key, label=option.label, value=value, revision=revision))
            values[option.key] = value
        rows = spec.rows(data, values)
        if len(rows) > 50 or any(set(row) - {"title", "text", "occurred_at"}
                                 or "title" not in row
                                 or any(not isinstance(value, str) or len(value) > 500 for value in row.values())
                                 for row in rows):
            raise ValueError("Invalid plugin page rows")
        return dict(plugin_id=plugin_id, title=data.manifest.page_title, switches=controls, rows=rows)

    def set_option(self, plugin_id: str, key: str, value: bool, expected_revision: int) -> dict[str, Any]:
        data, spec = self._page(plugin_id)
        if type(value) is not bool or key not in {option.key for option in spec.switches}:
            raise ValueError("Unknown plugin preference or invalid value")
        revision = data.set_state(key, value, expected_revision=expected_revision)
        return dict(plugin_id=plugin_id, key=key, value=value, revision=revision)

    def _page(self, plugin_id: str) -> tuple[PluginData, PageSpec]:
        data = self.data(plugin_id)
        if plugin_id not in self._pages:
            raise ValueError("Plugin does not provide a page")
        return data, self._pages[plugin_id]
