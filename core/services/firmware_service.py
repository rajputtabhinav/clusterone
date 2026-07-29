from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from app import config
from data.repositories.firmware_repo import FirmwareRepo


class FirmwareService:
    def __init__(self, repo: FirmwareRepo) -> None:
        self._repo = repo

    def list(self) -> list[dict]:
        return self._repo.list()

    def get(self, fw_id: int) -> dict | None:
        """Single firmware row by primary key, or None. Public so the QML
        bridge doesn't have to reach into ``_repo`` directly (which would
        break the moment we refactored to a different backing store)."""
        return self._repo.get(int(fw_id))

    def count(self) -> int:
        return self._repo.count()

    def import_file(
        self,
        path: str,
        *,
        fw_type: str,
        oem: str,
        version: str,
        model: str | None = None,
        notes: str | None = None,
        uploaded_by: str = "user",
    ) -> int | None:
        """Hash, dedupe, and store a firmware image under firmware/<sha256>/."""
        src = Path(path)
        if not src.is_file():
            raise FileNotFoundError(path)
        sha = self._sha256(src)
        if self._repo.exists_sha(sha):
            return None  # already in the repository
        dest_dir = config.firmware_dir() / sha
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        shutil.copy2(src, dest)
        return self._repo.add({
            "name": src.name,
            "type": fw_type,
            "oem": oem,
            "model": model,
            "version": version,
            "file_path": str(dest),
            "sha256": sha,
            "size_bytes": src.stat().st_size,
            "uploaded_by": uploaded_by,
            "notes": notes,
        })

    def delete(self, fw_id: int) -> dict | None:
        row = self._repo.delete(fw_id)
        if row and row.get("file_path") and row["file_path"].startswith(str(config.firmware_dir())):
            try:
                p = Path(row["file_path"])
                if p.is_file():
                    p.unlink()
                if p.parent.is_dir() and not any(p.parent.iterdir()):
                    p.parent.rmdir()
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
