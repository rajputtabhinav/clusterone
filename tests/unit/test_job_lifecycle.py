"""Restart recovery: jobs/tasks must never stay stranded non-terminal, and
counters must reflect actual task outcomes (H4/H5)."""
from __future__ import annotations

from data.repositories.firmware_repo import FirmwareRepo
from data.repositories.jobs_repo import JobsRepo
from data.repositories.provisioning_repo import ProvisioningRepo
from data.repositories.servers_repo import ServersRepo


def _srv(db_conn, ip="10.0.0.1"):
    return ServersRepo(db_conn).upsert({"ip": ip, "status": "online"})


def _fw(db_conn):
    return FirmwareRepo(db_conn).add(
        {"name": "f.bin", "type": "BMC", "oem": "x", "version": "1",
         "file_path": "p", "sha256": "a" * 64, "size_bytes": 1})


def test_finalize_job_from_tasks_settles_with_real_counters(db_conn):
    jobs = JobsRepo(db_conn)
    sid, fw = _srv(db_conn), _fw(db_conn)
    job = jobs.create_job(fw_type="BMC")
    t1, t2 = jobs.create_task(job, sid, fw), jobs.create_task(job, sid, fw)
    jobs.update_task(t1, state="completed")
    jobs.update_task(t2, state="failed")
    jobs.update_job_state(job, "running")            # stuck 'running', 0/0
    summary = jobs.finalize_job_from_tasks(job)
    assert summary["state"] == "completed_with_errors"
    row = jobs.get_job(job)
    assert row["state"] == "completed_with_errors"
    assert row["succeeded"] == 1 and row["failed"] == 1


def test_finalize_keeps_running_while_a_task_is_in_flight(db_conn):
    jobs = JobsRepo(db_conn)
    sid, fw = _srv(db_conn), _fw(db_conn)
    job = jobs.create_job(fw_type="BMC")
    t1, t2 = jobs.create_task(job, sid, fw), jobs.create_task(job, sid, fw)
    jobs.update_task(t1, state="completed")
    jobs.update_task(t2, state="flashing")           # still running
    jobs.finalize_job_from_tasks(job)
    assert jobs.get_job(job)["state"] == "running"


def test_fail_non_terminal_jobs_except_keeps_resuming_job(db_conn):
    jobs = JobsRepo(db_conn)
    a = jobs.create_job(fw_type="BMC"); jobs.update_job_state(a, "running")
    b = jobs.create_job(fw_type="BMC"); jobs.update_job_state(b, "running")
    jobs.fail_non_terminal_jobs_except([b])          # b is being resumed
    assert jobs.get_job(a)["state"] == "failed"
    assert jobs.get_job(b)["state"] == "running"


def test_provisioning_restart_sweep_fails_stranded(db_conn):
    prov = ProvisioningRepo(db_conn)
    sid = _srv(db_conn, ip="10.0.0.2")
    job = prov.create_job(iso_id=None, os_profile_id=None, disk_profile_id=None)
    t = prov.create_task(job, sid)
    prov.update_task(t, state="installing")          # mid-install
    prov.update_job_state(job, "running")
    assert len(prov.list_non_terminal_tasks()) == 1
    prov.fail_non_terminal_tasks("restarted mid-install")
    prov.fail_non_terminal_jobs()
    assert prov.get_task(t)["state"] == "failed"
    assert prov.get_job(job)["state"] == "failed"
    assert prov.list_non_terminal_tasks() == []
