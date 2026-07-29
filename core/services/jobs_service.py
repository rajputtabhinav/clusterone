from __future__ import annotations

from data.repositories.jobs_repo import JobsRepo


class JobsService:
    def __init__(self, repo: JobsRepo) -> None:
        self._repo = repo

    def list_jobs(self, limit: int = 50) -> list[dict]:
        return self._repo.list_jobs(limit)

    def get_job(self, job_id: int) -> dict | None:
        return self._repo.get_job(job_id)

    def list_tasks(self, job_id: int) -> list[dict]:
        return self._repo.list_tasks_with_server(job_id)

    def latest_running_job(self) -> dict | None:
        return self._repo.latest_running_job()
