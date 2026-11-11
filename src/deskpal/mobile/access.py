"""应用级设备授权。只保存凭据摘要，配对码仅在内存中短暂存在。"""

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import secrets
import sqlite3
from threading import RLock
from time import time
from typing import Any, Iterator


class AccessStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS device (
                    id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL);
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            db = sqlite3.connect(self.path, timeout=5)
            try:
                db.row_factory = sqlite3.Row
                with db:
                    yield db
            finally:
                db.close()

    def configuration(self) -> dict[str, Any]:
        with self.connection() as db:
            values = dict(db.execute("SELECT key,value FROM setting").fetchall())
        return {"enabled": values.get("enabled") == "true", "bind": values.get("bind", "127.0.0.1"),
                "port": int(values.get("port", "8765")), "public_origin": values.get("public_origin", "")}

    def configure(self, *, enabled: bool, bind: str, port: int, public_origin: str) -> None:
        with self.connection() as db:
            for key, value in {"enabled": "true" if enabled else "false", "bind": bind,
                               "port": str(port), "public_origin": public_origin}.items():
                db.execute("INSERT OR REPLACE INTO setting VALUES(?,?)", (key, value))

    def issue(self, name: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute("DELETE FROM device WHERE expires<=?", (time(),))
            if db.execute("SELECT COUNT(*) FROM device").fetchone()[0] >= 16:
                raise ValueError("Too many devices")
            db.execute("INSERT INTO device VALUES(?,?,?,?,?)", (secrets.token_hex(16),
                sha256(token.encode()).hexdigest(), name[:60], time(), time() + 30 * 86400))
        return token

    def valid(self, token: str) -> bool:
        if not isinstance(token, str) or not 20 <= len(token) <= 100:
            return False
        with self.connection() as db:
            return db.execute("SELECT 1 FROM device WHERE digest=? AND expires>?",
                              (sha256(token.encode()).hexdigest(), time())).fetchone() is not None

    def devices(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT id,name,created,expires FROM device WHERE expires>? ORDER BY created", (time(),))]

    def revoke(self, identity: str | None = None) -> None:
        with self.connection() as db:
            if identity is None:
                db.execute("DELETE FROM device")
            else:
                db.execute("DELETE FROM device WHERE id=?", (identity,))


class Pairing:
    def __init__(self, store: AccessStore) -> None:
        self.store = store
        self.lock = RLock()
        self.code = ""
        self.expires = 0.0
        self.attempts = 0

    def generate(self) -> dict[str, Any]:
        with self.lock:
            self.code = f"{secrets.randbelow(100_000_000):08d}"
            self.expires = time() + 300
            self.attempts = 0
            return {"code": self.code, "expires": self.expires}

    def clear(self) -> None:
        with self.lock:
            self.code, self.expires = "", 0

    def exchange(self, code: str, name: str) -> str:
        with self.lock:
            if not self.code or self.expires <= time() or self.attempts >= 5:
                raise ValueError("Pairing expired")
            self.attempts += 1
            if not secrets.compare_digest(code, self.code):
                raise ValueError("Invalid pairing code")
            token = self.store.issue(name)
            self.code, self.expires = "", 0
            return token
