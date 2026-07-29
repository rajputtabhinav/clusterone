"""SQLite access layer: WAL mode + a simple forward-only migration runner.

Migrations are ``NNNN_name.sql`` files in ``data/migrations``, applied in order.
A single connection is shared across the async engine thread and the Qt main
thread (``check_same_thread=False``), so every repo's write path is serialized
through ``write_lock`` to avoid concurrent-write corruption / "database is
locked" errors.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

from app import config

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class Database:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else config.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # WAL default is synchronous=FULL which is overkill for our threat
        # model (full-power loss safety not required — operator will re-run
        # the flash). NORMAL is the WAL-recommended setting and ~3-5× faster
        # under our concurrent-read/serialized-write pattern.
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # Make readers wait up to 5s for a busy writer instead of failing
        # immediately with "database is locked" — protects the activity
        # log from tearing down an in-flight job.
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.write_lock = threading.Lock()
        self.migrate()

    def _current_version(self) -> int:
        has_table = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if not has_table:
            return 0
        # schema_version is a ledger — one row per applied migration (1, 2,
        # 3, …) because each INSERT carries a distinct version (the PRIMARY
        # KEY), so INSERT OR REPLACE never collapses them. The current head is
        # therefore MAX(version), NOT the first row. A bare `SELECT version`
        # returns row 1 on a 3-migration DB, which made migrate() re-apply
        # 0002/0003 on EVERY launch — rebuilding `servers` from the pre-0003
        # schema and silently wiping the disks_json/disks_at cache each time.
        row = self._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return int(row["v"]) if row and row["v"] is not None else 0

    def migrate(self) -> None:
        """Apply forward-only migrations under the write lock.

        Each migration must complete atomically: schema changes AND the
        version bump are wrapped in a single transaction so a crash
        midway leaves the DB at the previous version (re-run is safe)
        rather than corrupting it. ``DELETE`` + ``INSERT`` is replaced
        with ``INSERT OR REPLACE`` so a torn write doesn't leave the
        ``schema_version`` table empty.
        """
        current = self._current_version()
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(sql_file.stem.split("_", 1)[0])
            if version <= current:
                continue
            log.info("Applying migration %s", sql_file.name)
            with self.write_lock:
                # executescript() auto-commits any open transaction first.
                # Wrap the script + version bump in a SINGLE explicit
                # transaction (BEGIN IMMEDIATE so we acquire the write
                # lock before any other writer sneaks in).
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._conn.executescript(sql_file.read_text(encoding="utf-8"))
                    self._conn.execute(
                        "INSERT OR REPLACE INTO schema_version(version) VALUES (?)",
                        (version,),
                    )
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    log.exception("Migration %s failed — rolled back", sql_file.name)
                    raise

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        # Force a WAL checkpoint so clusterone.db-wal doesn't grow without
        # bound across clean shutdowns. TRUNCATE returns the WAL to zero size.
        try:
            with self.write_lock:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self._conn.close()
