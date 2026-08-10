"""Message log — one table, both directions.

Outbound rows record what was sent and how. Inbound rows are what the modem
received. `GET /messages?since=<id>` is a cursor over this table, which is what
lets a poller (or the MCP server) catch up without holding a connection open.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    direction  TEXT NOT NULL,              -- 'in' | 'out'
    peer       TEXT NOT NULL,              -- the other party's number
    text       TEXT NOT NULL,
    image_path TEXT,
    status     TEXT NOT NULL DEFAULT 'ok', -- ok | failed
    via        TEXT,                       -- which sink sent it
    error      TEXT,
    meta       TEXT                        -- free-form JSON from the caller
);
CREATE INDEX IF NOT EXISTS messages_ts ON messages (ts);
CREATE INDEX IF NOT EXISTS messages_dir ON messages (direction, id);

-- One row per outbound message actually handed to a sink. Drives the rate ceiling.
CREATE TABLE IF NOT EXISTS sent_log (ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS sent_log_ts ON sent_log (ts);
"""


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def record(self, direction, peer, text, image_path=None, status="ok",
               via=None, error=None, meta=None) -> int:
        cur = self.db.execute(
            "INSERT INTO messages (ts, direction, peer, text, image_path, status, "
            "via, error, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), direction, peer, text, image_path, status, via, error,
             json.dumps(meta) if meta else None),
        )
        self.db.commit()
        if direction == "out" and status == "ok":
            self._record_sent()
        return cur.lastrowid

    def get(self, msg_id: int):
        return self.db.execute(
            "SELECT * FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()

    def since(self, since_id: int = 0, limit: int = 50, direction: str | None = None):
        sql = "SELECT * FROM messages WHERE id > ?"
        args: list = [since_id]
        if direction:
            sql += " AND direction = ?"
            args.append(direction)
        sql += " ORDER BY id LIMIT ?"
        args.append(min(limit, 500))
        return self.db.execute(sql, args).fetchall()

    def seen_inbound(self, peer: str, text: str, within: float = 300) -> bool:
        """The modem can re-report a message before deletion lands; dedupe on content."""
        row = self.db.execute(
            "SELECT 1 FROM messages WHERE direction='in' AND peer=? AND text=? "
            "AND ts > ? LIMIT 1",
            (peer, text, time.time() - within),
        ).fetchone()
        return row is not None

    # --- rate ceiling ---------------------------------------------------------

    def _record_sent(self) -> None:
        now = time.time()
        self.db.execute("INSERT INTO sent_log (ts) VALUES (?)", (now,))
        self.db.execute("DELETE FROM sent_log WHERE ts < ?", (now - 86400 * 2,))
        self.db.commit()

    def rate_exceeded(self, max_per_hour: int, max_per_day: int) -> str | None:
        now = time.time()
        if max_per_hour:
            n = self.db.execute(
                "SELECT COUNT(*) c FROM sent_log WHERE ts > ?", (now - 3600,)
            ).fetchone()["c"]
            if n >= max_per_hour:
                return f"hourly ceiling reached ({n}/{max_per_hour})"
        if max_per_day:
            n = self.db.execute(
                "SELECT COUNT(*) c FROM sent_log WHERE ts > ?", (now - 86400,)
            ).fetchone()["c"]
            if n >= max_per_day:
                return f"daily ceiling reached ({n}/{max_per_day})"
        return None

    def counts(self) -> dict:
        now = time.time()
        q = lambda sql, a: self.db.execute(sql, a).fetchone()["c"]  # noqa: E731
        return {
            "total": q("SELECT COUNT(*) c FROM messages", ()),
            "inbound": q("SELECT COUNT(*) c FROM messages WHERE direction='in'", ()),
            "outbound": q("SELECT COUNT(*) c FROM messages WHERE direction='out'", ()),
            "failed": q("SELECT COUNT(*) c FROM messages WHERE status='failed'", ()),
            "sent_last_hour": q("SELECT COUNT(*) c FROM sent_log WHERE ts > ?",
                                (now - 3600,)),
            "sent_last_day": q("SELECT COUNT(*) c FROM sent_log WHERE ts > ?",
                               (now - 86400,)),
        }


def row_to_dict(row) -> dict:
    d = dict(row)
    if d.get("meta"):
        try:
            d["meta"] = json.loads(d["meta"])
        except ValueError:
            pass
    return d
