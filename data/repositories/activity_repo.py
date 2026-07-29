from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ActivityRepo:
    def __init__(self, conn: sqlite3.Connection, write_lock=None) -> None:
        self.conn = conn
        self._lock = write_lock if write_lock is not None else contextlib.nullcontext()

    def log(self, *, level: str, category: str, message: str,
            actor: str | None = None, server_id: int | None = None,
            job_id: int | None = None, detail: str | None = None,
            ts: str | None = None) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO activity_log (ts, level, category, actor, server_id, job_id, message, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts or _now(), level, category, actor, server_id, job_id, message, detail),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def recent(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM activity_log ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS c FROM activity_log").fetchone()["c"])
