"""Firmware/ISO repos must dedupe on sha256, not raise IntegrityError.

Both tables declare ``sha256 ... UNIQUE``. The services pre-check existence,
but a TOCTOU race (two concurrent imports of the same blob) used to surface as
an uncaught ``sqlite3.IntegrityError``. ``add()`` now uses
``ON CONFLICT(sha256) DO NOTHING`` and returns the existing row id.
"""
from __future__ import annotations

from data.repositories.firmware_repo import FirmwareRepo
from data.repositories.iso_repo import IsoRepo


def _fw(sha: str) -> dict:
    return {"name": "bios.bin", "type": "BIOS", "oem": "dell", "model": None,
            "version": "1.0", "file_path": f"/tmp/{sha}", "sha256": sha,
            "size_bytes": 1, "signature": None, "uploaded_by": "t", "notes": None}


def _iso(sha: str) -> dict:
    return {"name": "rocky.iso", "os_family": "rocky", "os_version": "9.4",
            "arch": "x86_64", "file_path": f"/tmp/{sha}.iso", "sha256": sha,
            "size_bytes": 1, "source_url": None, "uploaded_by": "t", "notes": None}


def test_firmware_add_dedupes_on_sha(db_conn):
    repo = FirmwareRepo(db_conn)
    first = repo.add(_fw("abc123"))
    second = repo.add(_fw("abc123"))   # same sha — must not raise
    assert first == second
    assert repo.count() == 1


def test_iso_add_dedupes_on_sha(db_conn):
    repo = IsoRepo(db_conn)
    first = repo.add(_iso("def456"))
    second = repo.add(_iso("def456"))  # same sha — must not raise
    assert first == second
    assert repo.count() == 1


def test_distinct_sha_still_inserts(db_conn):
    repo = FirmwareRepo(db_conn)
    a = repo.add(_fw("aaa"))
    b = repo.add(_fw("bbb"))
    assert a != b
    assert repo.count() == 2
