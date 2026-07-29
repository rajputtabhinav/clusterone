"""Disk Selection Profile repository.

A profile encodes *intent* — "first NVMe", "smallest SSD", "by model glob"
— so an operator-authored rule can be applied uniformly across a fleet
without typing per-host device paths. See ``core.services.disk_resolver``
for the strategy grammar.
"""
from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DiskProfileRepo:
    def __init__(self, conn: sqlite3.Connection, write_lock=None) -> None:
        self.conn = conn
        self._lock = write_lock if write_lock is not None else contextlib.nullcontext()

    def list(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM disk_selection_profiles ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, profile_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM disk_selection_profiles WHERE id=?", (profile_id,)
        ).fetchone()
        return dict(row) if row else None

    def add(self, *, name: str, strategy: str, allow_nonempty: bool = False,
            on_no_match: str = "fail", notes: str | None = None) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO disk_selection_profiles "
                "(name, strategy, allow_nonempty, on_no_match, created_at, notes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, strategy, 1 if allow_nonempty else 0, on_no_match, _now(), notes),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def delete(self, profile_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM disk_selection_profiles WHERE id=?", (profile_id,)
            ).fetchone()
            self.conn.execute(
                "DELETE FROM disk_selection_profiles WHERE id=?", (profile_id,)
            )
            self.conn.commit()
            return dict(row) if row else None

    def count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) AS c FROM disk_selection_profiles"
        ).fetchone()["c"])
