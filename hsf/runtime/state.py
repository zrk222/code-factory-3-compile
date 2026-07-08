"""StateStore: dict-backed with optional sqlite journal (durable option)."""
from __future__ import annotations
import json, sqlite3
from pathlib import Path

class StateStore:
    def __init__(self, journal: Path | None = None):
        self._d: dict = {}
        self._conn = None
        if journal:
            self._conn = sqlite3.connect(journal)
            self._conn.execute("CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT)")

    def set(self, k: str, v):
        self._d[k] = v
        if self._conn:
            self._conn.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (k, json.dumps(v, default=str)))
            self._conn.commit()

    def get(self, k: str):
        if k not in self._d:
            raise KeyError(f"unresolved reference {k!r} — halt, never improvise")
        return self._d[k]
