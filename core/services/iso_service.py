"""ISO Library service.

Content-addressable: ISOs are stored under ``iso_dir() / <sha256>.iso``.
Re-uploading the same image returns the existing id rather than duplicating.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from app import config
from data.repositories.iso_repo import IsoRepo


class IsoService:
    def __init__(self, repo: IsoRepo) -> None:
        self._repo = repo

    def list(self) -> list[dict]:
        return self._repo.list()

    def get(self, iso_id: int) -> dict | None:
        return self._repo.get(iso_id)

    def get_by_sha(self, sha256: str) -> dict | None:
        return self._repo.get_by_sha(sha256)

    def count(self) -> int:
        return self._repo.count()

    def import_file(
        self,
        path: str,
        *,
        os_family: str,
        os_version: str,
        arch: str = "x86_64",
        source_url: str | None = None,
        notes: str | None = None,
        uploaded_by: str = "user",
    ) -> int | None:
        """Hash, dedupe, copy. Returns the new id, or ``None`` if SHA already exists."""
        src = Path(path)
        if not src.is_file():
            raise FileNotFoundError(path)
        sha = self._sha256(src)
        existing = self._repo.get_by_sha(sha)
        if existing:
            return None
        config.iso_dir().mkdir(parents=True, exist_ok=True)
        dest = config.iso_dir() / f"{sha}.iso"
        # Don't re-copy if the bytes are already there (e.g., re-import after
        # the DB row was deleted but the file remained).
        if not dest.is_file():
            shutil.copy2(src, dest)
        return self._repo.add({
            "name": src.name,
            "os_family": os_family.lower(),
            "os_version": os_version,
            "arch": arch,
            "file_path": str(dest),
            "sha256": sha,
            "size_bytes": src.stat().st_size,
            "source_url": source_url,
            "uploaded_by": uploaded_by,
            "notes": notes,
        })

    def delete(self, iso_id: int) -> dict | None:
        """Delete the DB row + on-disk blob. Safe if either is missing."""
        row = self._repo.delete(iso_id)
        if row and row.get("file_path") and row["file_path"].startswith(str(config.iso_dir())):
            try:
                p = Path(row["file_path"])
                if p.is_file():
                    p.unlink()
            except OSError:
                pass
        return row

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
