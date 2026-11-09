"""Profile-scoped plugin state and bounded event streams."""

import sqlite3


def migrate_schema_7(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE plugin_state(
        plugin_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision > 0), PRIMARY KEY(plugin_id,key))""")
    conn.execute("""CREATE TABLE plugin_event(
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        plugin_id TEXT NOT NULL, source TEXT NOT NULL, event_id TEXT NOT NULL,
        occurred_at TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(plugin_id,source,event_id))""")
    conn.execute("CREATE INDEX plugin_event_stream ON plugin_event(plugin_id,source,sequence)")
    conn.execute("UPDATE schema_version SET version=8")


def validate(conn: sqlite3.Connection) -> None:
    columns = {
        "plugin_state": {"plugin_id", "key", "value", "revision"},
        "plugin_event": {"sequence", "plugin_id", "source", "event_id", "occurred_at", "payload"},
    }
    for table, expected in columns.items():
        if {row[1] for row in conn.execute(f"PRAGMA table_info({table})")} != expected:
            raise ValueError("Incomplete workflow schema 8")
    if "plugin_event_stream" not in {row[1] for row in conn.execute("PRAGMA index_list(plugin_event)")}:
        raise ValueError("Missing plugin event stream index")
