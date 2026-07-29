"""Provisioning jobs + tasks repository (parallel shape to ``jobs_repo``).

A *provisioning job* is one bulk OS install across N servers; each server
becomes a *task* with a richer state machine than firmware updates
(resolving_disk → rendering_config → uploading_config → mounting_iso →
setting_boot → power_cycling → waiting_for_installer → installing →
waiting_for_boot → verifying_ssh → completed | failed).
"""
from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProvisioningRepo:
    def __init__(self, conn: sqlite3.Connection, write_lock=None) -> None:
        self.conn = conn
        self._lock = write_lock if write_lock is not None else contextlib.nullcontext()

    # ---- jobs --------------------------------------------------------------

    def create_job(self, *, iso_id: int, os_profile_id: int | None,
                   disk_profile_id: int | None, concurrency: int = 8,
                   confirm_token: str = "", created_by: str = "user") -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO provisioning_jobs "
                "(created_at, created_by, iso_id, os_profile_id, disk_profile_id, "
                "concurrency, state, total, confirm_token) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)",
                (_now(), created_by, iso_id, os_profile_id, disk_profile_id,
                 concurrency, confirm_token),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    # Allow-list of columns updatable via the dynamic-SET helpers. Same
    # hardening as jobs_repo — defense in depth against a future caller
    # accidentally letting user input reach the f-string.
    _JOB_UPDATE_COLS = frozenset({"state", "total", "succeeded", "failed"})
    _TASK_UPDATE_COLS = frozenset({
        "state", "progress", "message", "rendered_cfg_sha", "finished_at",
    })

    @staticmethod
    def _safe_set_clauses(pairs: list[tuple[str, object]],
                          allowed: frozenset[str]) -> tuple[str, list]:
        cols: list[str] = []
        vals: list = []
        for col, val in pairs:
            if col not in allowed:
                raise ValueError(f"Refusing to UPDATE unknown column {col!r}")
            cols.append(f"{col}=?")
            vals.append(val)
        return ", ".join(cols), vals

    def update_job_state(self, job_id: int, state: str, *,
                         total: int | None = None,
                         succeeded: int | None = None,
                         failed: int | None = None) -> None:
        pairs: list[tuple[str, object]] = [("state", state)]
        if total is not None:
            pairs.append(("total", total))
        if succeeded is not None:
            pairs.append(("succeeded", succeeded))
        if failed is not None:
            pairs.append(("failed", failed))
        set_sql, vals = self._safe_set_clauses(pairs, self._JOB_UPDATE_COLS)
        vals.append(job_id)
        with self._lock:
            self.conn.execute(
                f"UPDATE provisioning_jobs SET {set_sql} WHERE id=?", vals,
            )
            self.conn.commit()

    def get_job(self, job_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM provisioning_jobs WHERE id=?", (job_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM provisioning_jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- tasks -------------------------------------------------------------

    def create_task(self, job_id: int, server_id: int, *,
                    resolved_disk: str | None = None) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO provisioning_tasks "
                "(job_id, server_id, resolved_disk, state, progress, started_at) "
                "VALUES (?, ?, ?, 'queued', 0, ?)",
                (job_id, server_id, resolved_disk, _now()),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def update_task(self, task_id: int, *, state: str | None = None,
                    progress: int | None = None, message: str | None = None,
                    rendered_cfg_sha: str | None = None) -> None:
        pairs: list[tuple[str, object]] = []
        if state is not None:
            pairs.append(("state", state))
            if state in ("completed", "failed", "cancelled"):
                pairs.append(("finished_at", _now()))
                # Same consistency rule as jobs_repo — terminal success
                # implies progress=100 unless caller specified otherwise.
                if state == "completed" and progress is None:
                    pairs.append(("progress", 100))
        if progress is not None:
            pairs.append(("progress", progress))
        if message is not None:
            pairs.append(("message", message))
        if rendered_cfg_sha is not None:
            pairs.append(("rendered_cfg_sha", rendered_cfg_sha))
        if not pairs:
            return
        set_sql, vals = self._safe_set_clauses(pairs, self._TASK_UPDATE_COLS)
        vals.append(task_id)
        with self._lock:
            self.conn.execute(
                f"UPDATE provisioning_tasks SET {set_sql} WHERE id=?", vals,
            )
            self.conn.commit()

    def get_task(self, task_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM provisioning_tasks WHERE id=?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_tasks(self, job_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT t.*, s.ip AS server_ip, s.hostname AS server_hostname "
            "FROM provisioning_tasks t "
            "LEFT JOIN servers s ON s.id = t.server_id "
            "WHERE t.job_id=? ORDER BY t.id",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- restart recovery --------------------------------------------------
    # An OS install can't be resumed across an app restart (the in-memory
    # autoinstall config + signed URLs are gone; the install proceeds on the
    # host independently). The startup sweep marks stranded tasks/jobs failed
    # so a destructive wipe job doesn't sit 'running' forever in the audit DB.

    def list_non_terminal_tasks(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM provisioning_tasks "
            "WHERE state NOT IN ('completed','failed','cancelled') ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def fail_non_terminal_tasks(self, message: str) -> int:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE provisioning_tasks SET state='failed', finished_at=?, message=? "
                "WHERE state NOT IN ('completed','failed','cancelled')",
                (_now(), message),
            )
            self.conn.commit()
            return cur.rowcount

    def fail_non_terminal_jobs(self) -> int:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE provisioning_jobs SET state='failed' "
                "WHERE state IN ('pending','running')"
            )
            self.conn.commit()
            return cur.rowcount
