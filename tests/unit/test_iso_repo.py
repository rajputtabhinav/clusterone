"""ISO Library repository CRUD + SHA dedupe."""
from __future__ import annotations

from data.repositories.iso_repo import IsoRepo


def _payload(sha: str = "AA" * 32, **kw) -> dict:
    base = {
        "name": "rocky9.iso",
        "os_family": "rocky",
        "os_version": "9.4",
        "arch": "x86_64",
        "file_path": "/tmp/iso/aa.iso",
        "sha256": sha,
        "size_bytes": 1234567,
    }
    base.update(kw)
    return base


def test_iso_add_and_list(db_conn):
    repo = IsoRepo(db_conn)
    iid = repo.add(_payload())
    assert iid > 0
    rows = repo.list()
    assert len(rows) == 1
    assert rows[0]["os_family"] == "rocky"
    assert rows[0]["os_version"] == "9.4"
    assert rows[0]["arch"] == "x86_64"


def test_iso_sha256_dedupe(db_conn):
    repo = IsoRepo(db_conn)
    repo.add(_payload(sha="A" * 64))
    assert repo.exists_sha("A" * 64) is True
    assert repo.exists_sha("B" * 64) is False
    assert repo.get_by_sha("A" * 64) is not None


def test_iso_delete(db_conn):
    repo = IsoRepo(db_conn)
    iid = repo.add(_payload())
    deleted = repo.delete(iid)
    assert deleted is not None
    assert deleted["sha256"] == _payload()["sha256"]
    assert repo.count() == 0


def test_iso_count(db_conn):
    repo = IsoRepo(db_conn)
    assert repo.count() == 0
    repo.add(_payload(sha="A" * 64, name="a.iso"))
    repo.add(_payload(sha="B" * 64, name="b.iso"))
    assert repo.count() == 2


def test_iso_get_by_id(db_conn):
    repo = IsoRepo(db_conn)
    iid = repo.add(_payload(os_version="9.5"))
    row = repo.get(iid)
    assert row is not None
    assert row["os_version"] == "9.5"
    assert repo.get(9999) is None
