"""Structured personal store — preferences, settings, simple records (stdlib sqlite3).

Graphiti holds relational/temporal *facts*; this holds the flat, graph-shaped-poorly
data: user preferences, UI/app settings, small key/value records. No service to run.
Values are JSON-encoded so any JSON-serializable type round-trips. Migration path to
Postgres is documented in the roadmap (needed only for multi-device concurrent writes).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

_DB_PATH = Path.home() / ".local" / "share" / "friday" / "sutra.db"


class Store:
    """Thread-safe JSON key/value store backed by SQLite (WAL mode)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else _DB_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # check_same_thread=False + our own lock: the store is a process-wide singleton
        # touched from async handlers and the TTS/persist threads.
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS kv ("
            "  key TEXT PRIMARY KEY,"
            "  value TEXT NOT NULL,"
            "  updated_at TEXT DEFAULT (datetime('now'))"
            ")"
        )
        self._conn.commit()

    def set(self, key: str, value: Any) -> None:
        """Upsert a JSON-serializable value under key."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES(?, ?, datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value)),
            )
            self._conn.commit()

    def get(self, key: str, default: Any = None) -> Any:
        cur = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else default

    def delete(self, key: str) -> bool:
        """Delete key; returns True if it existed."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM kv WHERE key=?", (key,))
            self._conn.commit()
            return cur.rowcount > 0

    def keys(self, prefix: str = "") -> list[str]:
        cur = self._conn.execute(
            "SELECT key FROM kv WHERE key LIKE ? ORDER BY key", (f"{prefix}%",)
        )
        return [r[0] for r in cur.fetchall()]

    def all(self) -> dict[str, Any]:
        cur = self._conn.execute("SELECT key, value FROM kv")
        return {k: json.loads(v) for k, v in cur.fetchall()}

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_instance: Store | None = None


def get_store() -> Store:
    global _instance
    if _instance is None:
        _instance = Store()
    return _instance
