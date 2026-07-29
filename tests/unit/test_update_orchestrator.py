"""Unit tests for the firmware update orchestrator.

The async ``_run_job`` itself drives the asyncio engine and is exercised by
the integration-style tests under ``tests/integration/``. The unit tests here
target the pure / side-effect-free pieces that we can run synchronously:

* ``_compute_final_state`` — the terminal-state decision table that used to
  be a broken nested ternary always returning ``"completed"``.
* ``_verify_firmware_sha`` — the pre-flash integrity gate, including its
  seed-URI short-circuit so demo firmware never reaches the BMC.
* ``_lookup_firmware`` — prefers the indexed ``FirmwareRepo.get`` over a
  linear scan.
* ``resume_interrupted`` — sweep that flags non-terminal tasks failed on
  startup so a stale row never appears "still flashing".
* ``start_job`` input validation — empty selections / missing firmware /
  concurrency clamp.
"""
from __future__ import annotations

import hashlib

import pytest

from core.models.server import Credentials, ServerInfo
from core.services.activity_service import ActivityService
from core.services.update_orchestrator import UpdateOrchestrator
from data.repositories.activity_repo import ActivityRepo
from data.repositories.firmware_repo import FirmwareRepo
from data.repositories.jobs_repo import JobsRepo
from data.repositories.servers_repo import ServersRepo


# ---------------------------------------------------------------- factories


class _NoopEngine:
    """Stub AsyncEngine — captures submitted coroutines without running them.

    ``start_job`` schedules ``_run_job`` onto the engine; for the unit tests
    that exercise input validation we never want it to actually run.
    """
    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, coro):
        # Close the coroutine so Python doesn't warn about an unawaited coro.
        try:
            coro.close()
        except Exception:
            pass
        self.submitted.append(coro)
        return None

    def call_soon(self, fn, *args) -> None:
        fn(*args)


def _make_orch(db_conn) -> tuple[UpdateOrchestrator, JobsRepo, ServersRepo, FirmwareRepo]:
    jobs = JobsRepo(db_conn)
    servers = ServersRepo(db_conn)
    firmware = FirmwareRepo(db_conn)
    activity = ActivityService(ActivityRepo(db_conn))
    orch = UpdateOrchestrator(
        engine=_NoopEngine(),
        jobs_repo=jobs,
        servers_repo=servers,
        firmware_repo=firmware,
        plugins=None,            # not used by the paths covered here
        activity=activity,
    )
    return orch, jobs, servers, firmware


# ---------------------------------------------------------------- _compute_final_state


@pytest.mark.parametrize(
    "cancelled,succeeded,failed,expected",
    [
        # cancellation beats everything else — the job was aborted, not failed
        (True,  0, 0, "cancelled"),
        (True,  3, 1, "cancelled"),
        # every task failed
        (False, 0, 4, "failed"),
        (False, 0, 1, "failed"),
        # mixed
        (False, 3, 2, "completed_with_errors"),
        (False, 1, 1, "completed_with_errors"),
        # clean win
        (False, 5, 0, "completed"),
        (False, 0, 0, "completed"),
    ],
)
def test_compute_final_state(cancelled, succeeded, failed, expected):
    """Regression test for the broken nested ternary that always returned
    ``"completed"``. The decision table is canonical so we lock it down."""
    got = UpdateOrchestrator._compute_final_state(
        cancelled=cancelled, succeeded=succeeded, failed=failed,
    )
    assert got == expected


@pytest.mark.asyncio
async def test_run_job_finalizes_empty_task_list_without_cancel_evt_name_error(db_conn):
    """Regression: an empty task list never entered run_one(), so the old
    finalizer referenced an undefined cancel_evt local and failed the job."""
    orch, jobs, _servers, _firmware = _make_orch(db_conn)
    job_id = jobs.create_job(fw_type="BMC")
    done = []
    errors = []

    await orch._run_job(
        job_id, [], {"id": 1, "type": "BMC", "version": "1.0"},
        4, "", "", False, None, done.append, errors.append,
    )

    assert errors == []
    assert done == [{
        "job_id": job_id,
        "succeeded": 0,
        "failed": 0,
        "state": "completed",
    }]
    assert jobs.get_job(job_id)["state"] == "completed"


# ---------------------------------------------------------------- _verify_firmware_sha


def test_verify_sha_missing_sha_field():
    ok, msg = UpdateOrchestrator._verify_firmware_sha({"file_path": "/anything"})
    assert ok is False
    assert "sha256" in msg


def test_verify_sha_rejects_seed_uri():
    """Demo firmware seeded via ``data/seed.py`` has no backing blob; a real
    flash must be refused with a clear user-facing message."""
    fw = {"sha256": "deadbeef" * 8, "file_path": "seed://BMC-demo.bin"}
    ok, msg = UpdateOrchestrator._verify_firmware_sha(fw)
    assert ok is False
    assert "demo firmware" in msg.lower()


def test_verify_sha_missing_file(tmp_path):
    fw = {"sha256": "deadbeef" * 8, "file_path": str(tmp_path / "gone.bin")}
    ok, msg = UpdateOrchestrator._verify_firmware_sha(fw)
    assert ok is False
    assert "missing" in msg


def test_verify_sha_mismatch(tmp_path):
    blob = tmp_path / "fw.bin"
    blob.write_bytes(b"real contents")
    fw = {"sha256": "00" * 32, "file_path": str(blob)}
    ok, msg = UpdateOrchestrator._verify_firmware_sha(fw)
    assert ok is False
    assert "mismatch" in msg.lower()


def test_verify_sha_match(tmp_path):
    blob = tmp_path / "fw.bin"
    payload = b"x" * (1 << 14)   # 16 KiB to exercise the streaming loop
    blob.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    fw = {"sha256": digest, "file_path": str(blob)}
    ok, msg = UpdateOrchestrator._verify_firmware_sha(fw)
    assert ok is True
    assert msg == "ok"


def test_verify_sha_is_case_insensitive(tmp_path):
    """The DB may store either case; the comparison must normalise."""
    blob = tmp_path / "fw.bin"
    blob.write_bytes(b"abc")
    digest = hashlib.sha256(b"abc").hexdigest().upper()
    fw = {"sha256": digest, "file_path": str(blob)}
    ok, _ = UpdateOrchestrator._verify_firmware_sha(fw)
    assert ok is True


# ---------------------------------------------------------------- _lookup_firmware


def test_lookup_firmware_uses_indexed_get(db_conn):
    orch, _, _, firmware = _make_orch(db_conn)
    fw_id = firmware.add({
        "name": "fw.bin", "type": "BMC", "oem": "supermicro", "version": "1.0",
        "file_path": "seed://fw.bin", "sha256": "00" * 32, "size_bytes": 1,
    })
    got = orch._lookup_firmware(fw_id)
    assert got is not None
    assert got["id"] == fw_id
    assert got["version"] == "1.0"


def test_lookup_firmware_missing_id_returns_none(db_conn):
    orch, _, _, _ = _make_orch(db_conn)
    assert orch._lookup_firmware(9999) is None


def test_lookup_firmware_falls_back_to_list(db_conn):
    """If a repo implementation predates ``.get``, the orchestrator must still
    work via a linear scan over ``.list()`` — kept for plugin authors who
    subclass an older FirmwareRepo."""
    orch, _, _, firmware = _make_orch(db_conn)
    fw_id = firmware.add({
        "name": "fw.bin", "type": "BMC", "oem": "dell", "version": "2.0",
        "file_path": "seed://fw.bin", "sha256": "00" * 32, "size_bytes": 1,
    })

    class _LegacyRepo:
        def list(self):
            return firmware.list()

    orch._firmware = _LegacyRepo()
    got = orch._lookup_firmware(fw_id)
    assert got is not None
    assert got["version"] == "2.0"


# ---------------------------------------------------------------- resume_interrupted


def test_resume_interrupted_marks_non_terminal_tasks_failed(db_conn):
    """Tasks left in flashing/precheck/etc. when the app died must surface as
    failed on restart, not stay forever 'in progress'."""
    orch, jobs, servers, firmware = _make_orch(db_conn)
    servers.upsert({"ip": "10.0.0.1", "hostname": "h", "status": "online"})
    sid = servers.list()[0]["id"]
    fw_id = firmware.add({
        "name": "fw.bin", "type": "BMC", "oem": "supermicro", "version": "1.0",
        "file_path": "seed://fw.bin", "sha256": "00" * 32, "size_bytes": 1,
    })
    job_id = jobs.create_job(fw_type="BMC")
    t1 = jobs.create_task(job_id, sid, fw_id, "0.9")
    t2 = jobs.create_task(job_id, sid, fw_id, "0.9")
    jobs.update_task(t1, state="flashing", progress=42)
    jobs.update_task(t2, state="completed", progress=100)   # already terminal
    jobs.update_job_state(job_id, "running")

    swept = orch.resume_interrupted()

    assert swept == 1
    after_t1 = jobs.get_task(t1)
    after_t2 = jobs.get_task(t2)
    assert after_t1["state"] == "failed"
    assert "restart" in (after_t1["message"] or "").lower()
    # untouched
    assert after_t2["state"] == "completed"
    # Job rolled up from its ACTUAL task outcomes (1 completed, 1 failed). The
    # old code blindly set 'failed' and left counters at 0/0 (the H4 drift bug);
    # finalize_job_from_tasks now reflects reality with real counters.
    job = jobs.get_job(job_id)
    assert job["state"] == "completed_with_errors"
    assert job["succeeded"] == 1
    assert job["failed"] == 1


def test_resume_interrupted_no_op_when_clean(db_conn):
    orch, _, _, _ = _make_orch(db_conn)
    assert orch.resume_interrupted() == 0


# ---------------------------------------------------------------- start_job validation


def test_start_job_empty_servers_returns_none_and_errors(db_conn):
    orch, _, _, _ = _make_orch(db_conn)
    errors: list[str] = []
    job_id = orch.start_job(
        server_ids=[],
        firmware_id=1,
        on_error=errors.append,
    )
    assert job_id is None
    assert errors and "No servers" in errors[0]


def test_start_job_missing_firmware_returns_none_and_errors(db_conn):
    orch, _, servers, _ = _make_orch(db_conn)
    servers.upsert({"ip": "10.0.0.2", "status": "online"})
    sid = servers.list()[0]["id"]
    errors: list[str] = []
    job_id = orch.start_job(
        server_ids=[sid],
        firmware_id=9999,
        on_error=errors.append,
    )
    assert job_id is None
    assert errors and "Firmware not found" in errors[0]


def _server(ip="10.0.0.1", oem="supermicro") -> ServerInfo:
    return ServerInfo(ip=ip, oem=oem, redfish_root=f"https://{ip}/redfish/v1/")


class _StubCredsService:
    """Minimal stand-in for ``CredentialsService.resolve`` precedence tests."""
    def __init__(self, host_match=None, oem_match=None, default_match=None):
        self._host = host_match
        self._oem = oem_match
        self._default = default_match
        self.calls: list[tuple[str | None, str | None]] = []

    def resolve(self, *, ip=None, oem=None) -> Credentials | None:
        self.calls.append((ip, oem))
        # Mirror real service precedence: host > oem > default.
        if ip and self._host is not None:
            return self._host
        if oem and self._oem is not None:
            return self._oem
        return self._default


def _orch_with_creds(db_conn, creds_service) -> UpdateOrchestrator:
    orch, _, _, _ = _make_orch(db_conn)
    orch._credentials = creds_service
    return orch


def test_resolve_creds_prefers_ui_input(db_conn):
    """UI-typed credentials always win, even when a vault entry exists."""
    svc = _StubCredsService(host_match=Credentials("vault-user", "vault-pass"))
    orch = _orch_with_creds(db_conn, svc)
    ui = Credentials("typed-user", "typed-pass")
    got = orch._resolve_creds(ui, _server())
    assert got.username == "typed-user"
    assert got.password == "typed-pass"
    assert svc.calls == []   # vault never consulted


def test_resolve_creds_falls_back_to_vault_host_scope(db_conn):
    svc = _StubCredsService(host_match=Credentials("host-user", "host-pass"))
    orch = _orch_with_creds(db_conn, svc)
    got = orch._resolve_creds(Credentials("", ""), _server(ip="10.0.0.5"))
    assert got.username == "host-user"
    assert svc.calls == [("10.0.0.5", "supermicro")]


def test_resolve_creds_falls_back_to_oem_then_default(db_conn):
    svc = _StubCredsService(oem_match=Credentials("oem-user", "oem-pass"))
    orch = _orch_with_creds(db_conn, svc)
    got = orch._resolve_creds(Credentials("", ""), _server(oem="dell"))
    assert got.username == "oem-user"


def test_resolve_creds_returns_empty_when_nothing_stored(db_conn):
    """Plugin still receives a Credentials object (with empty fields) so
    RedfishClient can surface a clean 401 rather than crashing on None."""
    svc = _StubCredsService()
    orch = _orch_with_creds(db_conn, svc)
    got = orch._resolve_creds(Credentials("", ""), _server())
    assert got.username == ""
    assert got.password == ""


def test_resolve_creds_handles_missing_service(db_conn):
    """Orchestrators built without a credentials service (e.g. in tests)
    must not crash — they return an empty Credentials object."""
    orch, _, _, _ = _make_orch(db_conn)   # no creds service
    got = orch._resolve_creds(Credentials("", ""), _server())
    assert got.username == ""


def test_start_job_clamps_concurrency(db_conn):
    """A 999-server fleet must not spawn 999 concurrent flashes against the
    local box. The clamp caps at 64 and floors at 1."""
    orch, jobs, servers, firmware = _make_orch(db_conn)
    servers.upsert({"ip": "10.0.0.3", "status": "online"})
    sid = servers.list()[0]["id"]
    fw_id = firmware.add({
        "name": "fw.bin", "type": "BMC", "oem": "supermicro", "version": "1.0",
        "file_path": "seed://fw.bin", "sha256": "00" * 32, "size_bytes": 1,
    })

    # over the cap → 64
    job_id = orch.start_job(server_ids=[sid], firmware_id=fw_id, concurrency=999)
    assert jobs.get_job(job_id)["concurrency"] == 64

    # under the floor → 1
    job_id = orch.start_job(server_ids=[sid], firmware_id=fw_id, concurrency=0)
    assert jobs.get_job(job_id)["concurrency"] == 1

    # sane value passes through
    job_id = orch.start_job(server_ids=[sid], firmware_id=fw_id, concurrency=8)
    assert jobs.get_job(job_id)["concurrency"] == 8
