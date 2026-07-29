from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobsRepo:
    def __init__(self, conn: sqlite3.Connection, write_lock=None) -> None:
        self.conn = conn
        self._lock = write_lock if write_lock is not None else contextlib.nullcontext()

    # ---- jobs ----
    def create_job(self, *, fw_type: str, concurrency: int = 4,
                   apply_time: str = "OnReset", created_by: str = "user") -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO update_jobs (created_at, created_by, fw_type, concurrency, "
                "apply_time, state, total) VALUES (?, ?, ?, ?, ?, 'pending', 0)",
                (_now(), created_by, fw_type, concurrency, apply_time),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    # Allow-list of columns updatable via the dynamic-SET helpers.
    # Any column name reaching the f-string MUST come from this set;
    # the helpers assert membership so a future refactor can't sneak
    # a user-supplied string into the SQL. SQL-injection defense in depth.
    _JOB_UPDATE_COLS = frozenset({"state", "total", "succeeded", "failed"})
    _TASK_UPDATE_COLS = frozenset({"state", "progress", "message", "version_after",
                                   "redfish_task", "finished_at"})

    @staticmethod
    def _safe_set_clauses(pairs: list[tuple[str, object]],
                          allowed: frozenset[str]) -> tuple[str, list]:
        """Build a ``"col1=?, col2=?"`` fragment + matching value list,
        rejecting any column name not in ``allowed``. The fragment is
        purely literal column names (whitelisted) — no user input."""
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
            self.conn.execute(f"UPDATE update_jobs SET {set_sql} WHERE id=?", vals)
            self.conn.commit()

    def get_job(self, job_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM update_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM update_jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- tasks ----
    def create_task(self, job_id: int, server_id: int, firmware_id: int,
                    version_before: str | None = None) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO update_tasks (job_id, server_id, firmware_id, state, progress, "
                "version_before, started_at) VALUES (?, ?, ?, 'queued', 0, ?, ?)",
                (job_id, server_id, firmware_id, version_before, _now()),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def update_task(self, task_id: int, *, state: str | None = None,
                    progress: int | None = None, message: str | None = None,
                    version_after: str | None = None,
                    redfish_task: str | None = None) -> None:
        pairs: list[tuple[str, object]] = []
        if state is not None:
            pairs.append(("state", state))
            if state in ("completed", "failed"):
                pairs.append(("finished_at", _now()))
                # Coerce progress to 100 on terminal success — keeps
                # state and progress consistent for downstream readers
                # that aggregate (audit found readers could briefly see
                # state='completed' with stale progress<100).
                if state == "completed" and progress is None:
                    pairs.append(("progress", 100))
        if progress is not None:
            pairs.append(("progress", progress))
        if message is not None:
            pairs.append(("message", message))
        if version_after is not None:
            pairs.append(("version_after", version_after))
        if redfish_task is not None:
            pairs.append(("redfish_task", redfish_task))
        if not pairs:
            return
        set_sql, vals = self._safe_set_clauses(pairs, self._TASK_UPDATE_COLS)
        vals.append(task_id)
        with self._lock:
            self.conn.execute(f"UPDATE update_tasks SET {set_sql} WHERE id=?", vals)
            self.conn.commit()

    def get_task(self, task_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM update_tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, job_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM update_tasks WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def list_tasks_with_server(self, job_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT t.*, s.ip AS server_ip, s.hostname AS server_hostname, s.oem AS server_oem, "
            "f.name AS firmware_name, f.type AS firmware_type, f.version AS firmware_version "
            "FROM update_tasks t "
            "LEFT JOIN servers s ON s.id = t.server_id "
            "LEFT JOIN firmware f ON f.id = t.firmware_id "
            "WHERE t.job_id=? ORDER BY t.id",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_running_job(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM update_jobs WHERE state IN ('pending','running') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def list_non_terminal_tasks(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM update_tasks WHERE state IN "
            "('queued','precheck','uploading','flashing','rebooting','verifying') "
            "ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def fail_non_terminal_jobs(self) -> int:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE update_jobs SET state='failed' WHERE state IN ('pending','running')"
            )
            self.conn.commit()
            return cur.rowcount

    def fail_non_terminal_jobs_except(self, keep_ids: list[int]) -> int:
        """Fail every pending/running job whose id is NOT in ``keep_ids``.

        Used by the restart sweep to settle stale/orphan jobs (e.g. a job that
        crashed between create_job and create_task, leaving zero tasks) while
        leaving jobs whose tasks are actively being resumed untouched.
        """
        keep = [int(j) for j in (keep_ids or [])]
        with self._lock:
            if keep:
                placeholders = ",".join("?" for _ in keep)
                cur = self.conn.execute(
                    "UPDATE update_jobs SET state='failed' "
                    "WHERE state IN ('pending','running') "
                    f"AND id NOT IN ({placeholders})", keep,
                )
            else:
                cur = self.conn.execute(
                    "UPDATE update_jobs SET state='failed' "
                    "WHERE state IN ('pending','running')"
                )
            self.conn.commit()
            return cur.rowcount

    def finalize_job_from_tasks(self, job_id: int) -> dict | None:
        """Recompute a job's counters + terminal state from its task rows.

        Fixes the restart-resume path leaving jobs stuck 'running' with 0/0
        counters forever. If any task is still non-terminal the job stays
        'running' (counters refreshed); otherwise it settles to
        completed / completed_with_errors / failed. Returns the new summary,
        or ``None`` if the job has no tasks.
        """
        rows = self.conn.execute(
            "SELECT state FROM update_tasks WHERE job_id=?", (job_id,)
        ).fetchall()
        if not rows:
            return None
        states = [r["state"] for r in rows]
        total = len(states)
        succeeded = sum(1 for s in states if s == "completed")
        failed = sum(1 for s in states if s == "failed")
        if any(s not in ("completed", "failed") for s in states):
            self.update_job_state(job_id, "running", total=total,
                                  succeeded=succeeded, failed=failed)
            return {"state": "running", "total": total,
                    "succeeded": succeeded, "failed": failed}
        final = ("completed" if failed == 0
                 else "failed" if succeeded == 0
                 else "completed_with_errors")
        self.update_job_state(job_id, final, total=total,
                              succeeded=succeeded, failed=failed)
        return {"state": final, "total": total,
                "succeeded": succeeded, "failed": failed}
