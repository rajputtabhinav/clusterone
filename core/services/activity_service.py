from __future__ import annotations

import logging

from data.repositories.activity_repo import ActivityRepo

log = logging.getLogger(__name__)


class ActivityService:
    """Best-effort audit log. The audit trail must NEVER tear down
    operational work — a transient ``database is locked`` from a slow
    write contention used to crash an in-flight flash job. We catch
    every exception, surface it to the Python logger, and return -1
    so callers can detect (but don't have to handle) the failure."""

    def __init__(self, repo: ActivityRepo) -> None:
        self._repo = repo

    def log(self, level: str, category: str, message: str, **kwargs) -> int:
        try:
            return self._repo.log(level=level, category=category,
                                  message=message, **kwargs)
        except Exception as exc:
            # File logger is durable even if SQLite is locked / migrated /
            # disk-full. Operator still sees the line in clusterone.log.
            log.warning(
                "activity_log write failed (DB problem) — message lost from "
                "in-DB audit trail: [%s] [%s] %s  err=%s",
                level, category, message, exc,
            )
            return -1

    def recent(self, limit: int = 100) -> list[dict]:
        try:
            return self._repo.recent(limit)
        except Exception as exc:
            log.warning("activity_log read failed: %s", exc)
            return []
