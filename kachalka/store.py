"""История загрузок в SQLite: что добавлено, когда скачано, куда легло, что удалено."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS torrents (
    info_hash    TEXT PRIMARY KEY,
    name         TEXT,
    source       TEXT,
    save_path    TEXT,
    total_size   INTEGER,
    added_at     TEXT,
    completed_at TEXT,
    removed_at   TEXT,
    files_trashed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    info_hash TEXT,
    at        TEXT,
    kind      TEXT,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS events_hash ON events(info_hash);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def _exec(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._db.execute(sql, args)
            rows = cur.fetchall()
            self._db.commit()
            return rows

    def event(self, info_hash: str, kind: str, detail: str = "") -> None:
        self._exec("INSERT INTO events(info_hash, at, kind, detail) VALUES (?,?,?,?)", (info_hash, now(), kind, detail))

    def added(self, info_hash: str, name: str, source: str, save_path: str, total_size: int) -> None:
        self._exec(
            """INSERT INTO torrents(info_hash, name, source, save_path, total_size, added_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(info_hash) DO UPDATE SET removed_at = NULL, files_trashed = 0,
                   save_path = excluded.save_path""",
            (info_hash, name, source, save_path, total_size, now()),
        )
        self.event(info_hash, "added", source)

    def update_meta(self, info_hash: str, name: str, total_size: int) -> None:
        self._exec("UPDATE torrents SET name = ?, total_size = ? WHERE info_hash = ?", (name, total_size, info_hash))

    def completed(self, info_hash: str) -> bool:
        """Отмечает окончание загрузки. Возвращает True, если это первое окончание."""
        rows = self._exec("SELECT completed_at FROM torrents WHERE info_hash = ?", (info_hash,))
        if rows and rows[0]["completed_at"]:
            return False
        self._exec("UPDATE torrents SET completed_at = ? WHERE info_hash = ?", (now(), info_hash))
        self.event(info_hash, "completed")
        return True

    def removed(self, info_hash: str, trashed: bool) -> None:
        self._exec(
            "UPDATE torrents SET removed_at = ?, files_trashed = ? WHERE info_hash = ?",
            (now(), int(trashed), info_hash),
        )
        self.event(info_hash, "removed", "files to trash" if trashed else "kept files")

    def history(self, limit: int = 50) -> list[dict]:
        rows = self._exec("SELECT * FROM torrents ORDER BY added_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]
