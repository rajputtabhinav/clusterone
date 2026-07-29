"""Shared pytest fixtures."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "data" / "migrations"

# CI / dev: vault refuses plaintext fallback by default. Allow it here so the
# round-trip tests run on hosts without pywin32. Scoped autouse fixture so
# the flag clears between sessions instead of leaking into any subprocess
# (e.g. the smoke_qml launcher) the test suite spawns.
_PLAINTEXT_KEY = "CLUSTERONE_ALLOW_PLAINTEXT_VAULT"
_PREV_PLAINTEXT = os.environ.get(_PLAINTEXT_KEY)
os.environ[_PLAINTEXT_KEY] = "1"


@pytest.fixture(autouse=True, scope="session")
def _restore_plaintext_vault_env():
    """Ensure the plaintext-vault opt-in we set at import time doesn't
    persist past the test session. Important when pytest runs as part of
    a larger CI pipeline that subsequently launches the real app."""
    yield
    if _PREV_PLAINTEXT is None:
        os.environ.pop(_PLAINTEXT_KEY, None)
    else:
        os.environ[_PLAINTEXT_KEY] = _PREV_PLAINTEXT


def _apply_all_migrations(conn: sqlite3.Connection) -> None:
    """Mirror ``data.database.Database.migrate`` for the in-memory test DB.

    Iterating every ``NNNN_*.sql`` file means new migrations are picked up by
    the test suite automatically — no need to keep conftest in sync with the
    runner.
    """
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        conn.executescript(sql_file.read_text(encoding="utf-8"))


@pytest.fixture
def db_conn():
    """In-memory SQLite with every migration applied (matches production)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    _apply_all_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def qcore():
    """A QCoreApplication so QObject-derived classes (models) can be exercised."""
    from PyQt6.QtCore import QCoreApplication
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication(sys.argv)
    return app
