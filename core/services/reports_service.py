from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from app import config
from core.reports.report_generator import (
    render_inventory_snapshot,
    render_update_job_report,
)
from core.services.inventory_service import InventoryService
from core.services.jobs_service import JobsService


class ReportsService:
    def __init__(self, inventory: InventoryService, jobs: JobsService) -> None:
        self._inventory = inventory
        self._jobs = jobs

    def default_output_dir(self) -> Path:
        d = Path.home() / "Documents" / "ClusterOne"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_inventory_snapshot(self, output_path: Path | None = None) -> Path:
        if output_path is None:
            output_path = self.default_output_dir() / (
                f"ClusterOne_Inventory_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
            )
        return render_inventory_snapshot(self._inventory.list(), output_path)

    def write_update_job_report(self, job_id: int, output_path: Path | None = None) -> Path:
        job = self._jobs.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        tasks = self._jobs.list_tasks(job_id)
        if output_path is None:
            output_path = self.default_output_dir() / (
                f"ClusterOne_Job{job_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
            )
        return render_update_job_report(job, tasks, output_path)

    def export_audit_log(self, output_path: Path | None = None) -> Path | None:
        """Copy the rolling audit log to the user's Documents directory."""
        audit = config.logs_dir() / "audit.log"
        if not audit.exists():
            return None
        if output_path is None:
            output_path = self.default_output_dir() / (
                f"ClusterOne_Audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            )
        shutil.copy2(audit, output_path)
        return output_path
