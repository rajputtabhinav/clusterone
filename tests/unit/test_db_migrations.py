"""Regression tests for the on-disk migration runner.

These guard the bug where ``Database._current_version()`` read the FIRST row of
the ``schema_version`` ledger instead of ``MAX(version)``. On a multi-migration
DB it returned 1, so every launch re-applied 0002/0003 — rebuilding the
``servers`` table from the pre-0003 schema and silently wiping the cached
``disks_json``/``disks_at`` inventory.

The rest of the suite uses a fresh ``:memory:`` DB per test (see conftest), so
it could never catch a *second-launch* regression — hence these reopen tests
against a real file path.
"""
from __future__ import annotations

from data.database import Database


def test_current_version_is_ledger_head_not_first_row(tmp_path):
    db = Database(tmp_path / "version.db")
    try:
        versions = [r["version"] for r in
                    db.connection.execute("SELECT version FROM schema_version")]
        assert versions, "schema_version should have at least one row"
        assert db._current_version() == max(versions)
    finally:
        db.close()


def test_reopen_does_not_rerun_migrations_or_wipe_disk_cache(tmp_path):
    path = tmp_path / "persist.db"
    db = Database(path)
    with db.write_lock:
        db.connection.execute(
            "INSERT INTO servers(ip, status, disks_json, disks_at) "
            "VALUES('10.0.0.1','online','[{\"name\":\"nvme0n1\"}]','2026-01-01T00:00:00')"
        )
        db.connection.commit()
    head_before = db._current_version()
    db.close()

    # Simulate a second app launch on the same DB file: migrate() must be a
    # no-op (head unchanged) and the cached disk inventory must survive.
    db2 = Database(path)
    try:
        assert db2._current_version() == head_before
        row = db2.connection.execute(
            "SELECT disks_json, disks_at FROM servers WHERE ip='10.0.0.1'"
        ).fetchone()
        assert row is not None, "server row vanished across reopen"
        assert row["disks_json"] == '[{"name":"nvme0n1"}]', "disk cache was wiped on reopen"
        assert row["disks_at"] == "2026-01-01T00:00:00"
    finally:
        db2.close()


def test_fk_indexes_present(tmp_path):
    """0004 adds indexes on the FK columns the delete/join paths hit."""
    db = Database(tmp_path / "idx.db")
    try:
        idx = {r["name"] for r in db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"idx_tasks_server", "idx_tasks_fw", "idx_prov_tasks_server"} <= idx
    finally:
        db.close()
