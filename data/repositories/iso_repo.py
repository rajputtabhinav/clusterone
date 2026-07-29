"""ISO Library repository — content-addressable install media catalog."""
from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class IsoRepo:
    def __init__(self, conn: sqlite3.Connection, write_lock=None) -> None:
        self.conn = conn
        self._lock = write_lock if write_lock is not None else contextlib.nullcontext()

    def list(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM iso_library ORDER BY uploaded_at DESC, id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, iso_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM iso_library WHERE id=?", (iso_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_by_sha(self, sha256: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM iso_library WHERE sha256=?", (sha256,)
        ).fetchone()
        return dict(row) if row else None

    def exists_sha(self, sha256: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM iso_library WHERE sha256=?", (sha256,)
        ).fetchone() is not None

    def add(self, iso: dict) -> int:
        cols = ["name", "os_family", "os_version", "arch", "file_path",
                "sha256", "size_bytes", "source_url", "uploaded_at",
                "uploaded_by", "notes"]
        params = {c: iso.get(c) for c in cols}
        params["uploaded_at"] = iso.get("uploaded_at") or _now()
        params["arch"] = iso.get("arch") or "x86_64"
        placeholders = ", ".join(f":{c}" for c in cols)
        with self._lock:
            cur = self.conn.execute(
                f"INSERT INTO iso_library ({', '.join(cols)}) VALUES ({placeholders}) "
                "ON CONFLICT(sha256) DO NOTHING",
                params,
            )
            inserted = cur.rowcount == 1
            new_id = int(cur.lastrowid) if inserted and cur.lastrowid else 0
            if not inserted:
                # Duplicate sha256 — return the existing id (dedupe) instead of
                # raising IntegrityError on the TOCTOU race with get_by_sha().
                row = self.conn.execute(
                    "SELECT id FROM iso_library WHERE sha256=?", (params["sha256"],)
                ).fetchone()
                new_id = int(row["id"]) if row else 0
            self.conn.commit()
            return new_id

    def delete(self, iso_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM iso_library WHERE id=?", (iso_id,)
            ).fetchone()
            self.conn.execute("DELETE FROM iso_library WHERE id=?", (iso_id,))
            self.conn.commit()
            return dict(row) if row else None

    def count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) AS c FROM iso_library"
        ).fetchone()["c"])
