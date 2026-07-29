from __future__ import annotations

from data.repositories.settings_repo import SettingsRepo


class SettingsService:
    def __init__(self, repo: SettingsRepo) -> None:
        self._repo = repo

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._repo.get(key, default)

    def set(self, key: str, value) -> None:
        self._repo.set(key, "" if value is None else str(value))

    def get_int(self, key: str, default: int = 0) -> int:
        v = self._repo.get(key)
        try:
            return int(v) if v is not None and v != "" else default
        except ValueError:
            return default
