"""Repository CRUD + invariants."""
from __future__ import annotations

from data.repositories.activity_repo import ActivityRepo
from data.repositories.credentials_repo import CredentialsRepo
from data.repositories.firmware_repo import FirmwareRepo
from data.repositories.jobs_repo import JobsRepo
from data.repositories.servers_repo import ServersRepo
from data.repositories.settings_repo import SettingsRepo


def test_servers_upsert_is_idempotent_on_ip(db_conn):
    repo = ServersRepo(db_conn)
    sid = repo.upsert({"ip": "10.0.0.5", "hostname": "h1", "status": "online"})
    repo.upsert({"ip": "10.0.0.5", "hostname": "h1-renamed", "status": "needs_update"})
    rows = repo.list()
    assert len(rows) == 1
    assert rows[0]["id"] == sid
    assert rows[0]["hostname"] == "h1-renamed"
    assert rows[0]["status"] == "needs_update"


def test_servers_status_counts(db_conn):
    repo = ServersRepo(db_conn)
    for ip, status in [("1.1.1.1", "online"), ("1.1.1.2", "online"),
                       ("1.1.1.3", "needs_update"), ("1.1.1.4", "failed")]:
        repo.upsert({"ip": ip, "status": status})
    counts = repo.status_counts()
    assert counts == {"total": 4, "online": 2, "needs_update": 1, "failed": 1, "offline": 0}


def test_firmware_sha256_dedupe(db_conn):
    repo = FirmwareRepo(db_conn)
    payload = {
        "name": "f.bin", "type": "BIOS", "oem": "supermicro",
        "version": "1.0", "file_path": "/tmp/f", "sha256": "ABC",
    }
    repo.add(payload)
    assert repo.exists_sha("ABC") is True
    assert repo.exists_sha("XYZ") is False


def test_activity_log_recent_descending(db_conn):
    repo = ActivityRepo(db_conn)
    repo.log(level="info", category="discovery", message="first", ts="2026-01-01T00:00:00+00:00")
    repo.log(level="info", category="discovery", message="second", ts="2026-01-02T00:00:00+00:00")
    rows = repo.recent(10)
    assert [r["message"] for r in rows] == ["second", "first"]


def test_settings_upsert(db_conn):
    repo = SettingsRepo(db_conn)
    repo.set("theme_mode", "dark")
    assert repo.get("theme_mode") == "dark"
    repo.set("theme_mode", "light")
    assert repo.get("theme_mode") == "light"
    assert repo.get("missing", "default") == "default"


# ---- FK cascade behavior (migration 0002) ----
#
# Regression coverage for the silent ✕-button bug: before 0002, deleting
# any server/firmware/credential that had EVER been referenced by an
# update task raised `IntegrityError`, the QML slot crashed, and the row
# stayed. After 0002, deletes succeed and the dependent FK becomes NULL.


def _seed_task_referencing(db_conn) -> tuple[int, int, int]:
    """Insert a task that pins one server + one firmware. Returns
    (server_id, firmware_id, task_id) for the assertions to check."""
    servers = ServersRepo(db_conn)
    firmware = FirmwareRepo(db_conn)
    jobs = JobsRepo(db_conn)

    sid = servers.upsert({"ip": "10.0.0.42", "hostname": "fk-test", "status": "online"})
    fw_id = firmware.add({
        "name": "fk.bin", "type": "BMC", "oem": "supermicro",
        "version": "1.0", "file_path": "/tmp/fk", "sha256": "FK" * 32,
    })
    job_id = jobs.create_job(fw_type="BMC")
    task_id = jobs.create_task(job_id, sid, fw_id, "0.9")
    return sid, fw_id, task_id


def test_delete_server_sets_task_server_id_null(db_conn):
    sid, _, task_id = _seed_task_referencing(db_conn)
    ServersRepo(db_conn).delete(sid)
    task = JobsRepo(db_conn).get_task(task_id)
    assert task is not None         # task preserved (audit history)
    assert task["server_id"] is None  # FK nulled


def test_delete_firmware_sets_task_firmware_id_null(db_conn):
    _, fw_id, task_id = _seed_task_referencing(db_conn)
    FirmwareRepo(db_conn).delete(fw_id)
    task = JobsRepo(db_conn).get_task(task_id)
    assert task is not None
    assert task["firmware_id"] is None


def test_delete_credential_sets_server_cred_id_null(db_conn):
    """servers.cred_id loses its reference when the credential row is
    removed, but the server stays in inventory."""
    creds = CredentialsRepo(db_conn)
    servers = ServersRepo(db_conn)
    cred_id = creds.add(
        label="lab-bmc", username="admin", secret_enc=b"PLAIN:dGVzdA==", scope="default",
    )
    sid = servers.upsert({"ip": "10.0.0.99", "status": "online"})
    db_conn.execute("UPDATE servers SET cred_id=? WHERE id=?", (cred_id, sid))
    db_conn.commit()

    creds.delete(cred_id)

    row = servers.get(sid)
    assert row is not None
    assert row["cred_id"] is None


def test_jobs_cascade_to_tasks_on_job_delete(db_conn):
    """update_jobs → update_tasks is CASCADE (intentional, since a task
    without a job is meaningless). Verify 0002 didn't accidentally
    downgrade that."""
    _, _, task_id = _seed_task_referencing(db_conn)
    jobs = JobsRepo(db_conn)
    task = jobs.get_task(task_id)
    assert task is not None
    db_conn.execute("DELETE FROM update_jobs WHERE id=?", (task["job_id"],))
    db_conn.commit()
    assert jobs.get_task(task_id) is None
