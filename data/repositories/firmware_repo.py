from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FirmwareRepo:
    def __init__(self, conn: sqlite3.Connection, write_lock=None) -> None:
        self.conn = conn
        self._lock = write_lock if write_lock is not None else contextlib.nullcontext()

    def list(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM firmware ORDER BY uploaded_at DESC, id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, fw_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM firmware WHERE id=?", (fw_id,)).fetchone()
        return dict(row) if row else None

    def exists_sha(self, sha256: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM firmware WHERE sha256=?", (sha256,)
        ).fetchone() is not None

    def add(self, fw: dict) -> int:
        cols = ["name", "type", "oem", "model", "version", "file_path",
                "sha256", "size_bytes", "signature", "uploaded_at", "uploaded_by", "notes"]
        params = {c: fw.get(c) for c in cols}
        params["uploaded_at"] = fw.get("uploaded_at") or _now()
        placeholders = ", ".join(f":{c}" for c in cols)
        with self._lock:
            cur = self.conn.execute(
                f"INSERT INTO firmware ({', '.join(cols)}) VALUES ({placeholders}) "
                "ON CONFLICT(sha256) DO NOTHING", params
            )
            inserted = cur.rowcount == 1
            new_id = int(cur.lastrowid) if inserted and cur.lastrowid else 0
            if not inserted:
                # Duplicate sha256 (content-addressable) — return the existing
                # row's id instead of raising IntegrityError. Closes the TOCTOU
                # gap between the service's exists_sha() check and this INSERT.
                row = self.conn.execute(
                    "SELECT id FROM firmware WHERE sha256=?", (params["sha256"],)
                ).fetchone()
                new_id = int(row["id"]) if row else 0
            self.conn.commit()
            return new_id

    def delete(self, fw_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM firmware WHERE id=?", (fw_id,)).fetchone()
            self.conn.execute("DELETE FROM firmware WHERE id=?", (fw_id,))
            self.conn.commit()
            return dict(row) if row else None

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS c FROM firmware").fetchone()["c"])
