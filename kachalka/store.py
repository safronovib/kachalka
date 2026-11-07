"""История загрузок в SQLite: что добавлено, когда скачано, куда легло, что удалено."""

from __future__ import annotations

import json
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
CREATE TABLE IF NOT EXISTS moves (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    info_hash  TEXT,
    name       TEXT,
    category   TEXT,
    title      TEXT,
    from_path  TEXT,
    to_path    TEXT,
    renames    TEXT,            -- JSON [[индекс, было, стало], …]
    status     TEXT,            -- pending | done | failed | undoing | undone
    at         TEXT,
    done_at    TEXT,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS moves_hash ON moves(info_hash);
CREATE TABLE IF NOT EXISTS watch (
    key             TEXT PRIMARY KEY,   -- info_hash:индекс файла
    info_hash       TEXT,
    idx             INTEGER,
    path            TEXT,
    duration        REAL,
    vlc_pos         REAL,
    watched_seconds REAL DEFAULT 0,
    last_seen       TEXT,              -- когда последний раз видели открытым в плеере
    watched_at      TEXT,              -- когда впервые стал «посмотрено»
    manual          INTEGER DEFAULT 0, -- 1 = отмечено посмотренным, -1 = не смотрел
    trashed_at      TEXT
);
CREATE INDEX IF NOT EXISTS watch_hash ON watch(info_hash);
CREATE TABLE IF NOT EXISTS follows (
    info_hash  TEXT PRIMARY KEY,
    ahead      INTEGER,            -- сколько серий держать наготове после текущей
    keep_hours REAL,               -- через сколько часов после просмотра убирать серию
    cleanup    INTEGER,            -- 1 = убирать просмотренные в Корзину
    from_ep    TEXT,               -- с какой серии начинать, если ещё ничего не смотрел
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS pending_select (
    info_hash TEXT PRIMARY KEY,
    spec      TEXT              -- JSON {"episodes": "4,5"} или {"files": [1,2]}
);
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

    # --- журнал перемещений (этап 2) ---

    def move_start(self, info_hash: str, name: str, category: str, title: str,
                   from_path: str, to_path: str, renames: list, status: str = "pending") -> int:
        with self._lock:
            cur = self._db.execute(
                """INSERT INTO moves(info_hash, name, category, title, from_path, to_path, renames, status, at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (info_hash, name, category, title, from_path, to_path, json.dumps(renames, ensure_ascii=False), status, now()),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def move_set(self, move_id: int, status: str, error: str = "") -> None:
        self._exec("UPDATE moves SET status = ?, done_at = ?, error = ? WHERE id = ?", (status, now(), error, move_id))

    def move_get(self, move_id: int) -> dict | None:
        rows = self._exec("SELECT * FROM moves WHERE id = ?", (move_id,))
        return self._move_row(rows[0]) if rows else None

    def last_move(self, info_hash: str) -> dict | None:
        rows = self._exec("SELECT * FROM moves WHERE info_hash = ? AND status = 'done' ORDER BY id DESC LIMIT 1", (info_hash,))
        return self._move_row(rows[0]) if rows else None

    def moves(self, limit: int = 30) -> list[dict]:
        return [self._move_row(r) for r in self._exec("SELECT * FROM moves ORDER BY id DESC LIMIT ?", (limit,))]

    @staticmethod
    def _move_row(r) -> dict:
        d = dict(r)
        d["renames"] = json.loads(d["renames"] or "[]")
        return d

    def set_save_path(self, info_hash: str, path: str) -> None:
        self._exec("UPDATE torrents SET save_path = ? WHERE info_hash = ?", (path, info_hash))

    # --- просмотр (этап 3) ---

    def watch_get(self, info_hash: str, idx: int) -> dict:
        rows = self._exec("SELECT * FROM watch WHERE key = ?", (f"{info_hash}:{idx}",))
        return dict(rows[0]) if rows else {"key": f"{info_hash}:{idx}", "info_hash": info_hash, "idx": idx}

    def watch_for(self, info_hash: str) -> dict[int, dict]:
        return {r["idx"]: dict(r) for r in self._exec("SELECT * FROM watch WHERE info_hash = ?", (info_hash,))}

    def watch_put(self, rec: dict) -> None:
        cols = ["key", "info_hash", "idx", "path", "duration", "vlc_pos", "watched_seconds",
                "last_seen", "watched_at", "manual", "trashed_at"]
        vals = [rec.get(c) for c in cols]
        vals[cols.index("watched_seconds")] = rec.get("watched_seconds") or 0
        vals[cols.index("manual")] = rec.get("manual") or 0
        self._exec(f"INSERT OR REPLACE INTO watch({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", tuple(vals))

    def follow_set(self, info_hash: str, ahead: int, keep_hours: float, cleanup: bool, from_ep: str | None) -> None:
        self._exec("INSERT OR REPLACE INTO follows VALUES (?,?,?,?,?,?)",
                   (info_hash, ahead, keep_hours, int(cleanup), from_ep, now()))

    def follow_del(self, info_hash: str) -> None:
        self._exec("DELETE FROM follows WHERE info_hash = ?", (info_hash,))

    def follows(self) -> dict[str, dict]:
        return {r["info_hash"]: dict(r) for r in self._exec("SELECT * FROM follows")}

    def torrent(self, info_hash: str) -> dict | None:
        rows = self._exec("SELECT * FROM torrents WHERE info_hash = ?", (info_hash,))
        return dict(rows[0]) if rows else None

    # --- выбор файлов до прихода метаданных ---

    def pending_select_set(self, info_hash: str, spec: dict) -> None:
        self._exec("INSERT OR REPLACE INTO pending_select(info_hash, spec) VALUES (?,?)",
                   (info_hash, json.dumps(spec, ensure_ascii=False)))

    def pending_select_pop(self, info_hash: str) -> dict | None:
        rows = self._exec("SELECT spec FROM pending_select WHERE info_hash = ?", (info_hash,))
        if not rows:
            return None
        self._exec("DELETE FROM pending_select WHERE info_hash = ?", (info_hash,))
        return json.loads(rows[0]["spec"])

    def history(self, limit: int = 50) -> list[dict]:
        rows = self._exec("SELECT * FROM torrents ORDER BY added_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]
