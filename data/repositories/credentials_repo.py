from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CredentialsRepo:
    def __init__(self, conn: sqlite3.Connection, write_lock=None) -> None:
        self.conn = conn
        self._lock = write_lock if write_lock is not None else contextlib.nullcontext()

    def list(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, label, username, scope, created_at FROM credentials ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def add(self, *, label: str, username: str, secret_enc: bytes,
            scope: str = "default") -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO credentials (label, username, secret_enc, scope, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (label, username, secret_enc, scope, _now()),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def get(self, cred_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM credentials WHERE id=?", (cred_id,)
        ).fetchone()
        return dict(row) if row else None

    def find_for_scope(self, scope: str) -> dict | None:
        """Most-specific match for a scope. Caller resolves precedence."""
        row = self.conn.execute(
            "SELECT * FROM credentials WHERE scope=? ORDER BY id DESC LIMIT 1",
            (scope,),
        ).fetchone()
        return dict(row) if row else None

    def delete(self, cred_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM credentials WHERE id=?", (cred_id,))
            self.conn.commit()

    def count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) AS c FROM credentials"
        ).fetchone()["c"])
