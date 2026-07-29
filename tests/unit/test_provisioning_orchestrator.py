"""Provisioning orchestrator — preview + start-job validation."""
from __future__ import annotations

import json

import pytest

from core.services.activity_service import ActivityService
from core.services.provisioning_orchestrator import (
    ProvisioningOrchestrator,
    _drives_from_row,
)
from data.repositories.activity_repo import ActivityRepo
from data.repositories.disk_profile_repo import DiskProfileRepo
from data.repositories.iso_repo import IsoRepo
from data.repositories.provisioning_repo import ProvisioningRepo
from data.repositories.servers_repo import ServersRepo


class _NoopEngine:
    def submit(self, coro):
        try:
            coro.close()
        except Exception:
            pass
        return None

    def call_soon(self, fn, *args):
        fn(*args)


def _make_orch(db_conn):
    activity = ActivityService(ActivityRepo(db_conn))
    iso_repo = IsoRepo(db_conn)
    servers = ServersRepo(db_conn)
    prov_repo = ProvisioningRepo(db_conn)
    disk_repo = DiskProfileRepo(db_conn)
    orch = ProvisioningOrchestrator(
        engine=_NoopEngine(),
        provisioning_repo=prov_repo,
        servers_repo=servers,
        iso_repo=iso_repo,
        disk_profile_repo=disk_repo,
        plugins=None,
        activity=activity,
        credentials=None,
        file_server=None,
    )
    return orch, iso_repo, servers, prov_repo, disk_repo


def _stock_iso(repo) -> int:
    return repo.add({
        "name": "rocky9.iso", "os_family": "rocky", "os_version": "9.4",
        "arch": "x86_64", "file_path": "/tmp/iso/x.iso",
        "sha256": "AA" * 32, "size_bytes": 1_500_000_000,
    })


def _seed_server_with_disks(servers, ip="10.0.0.5", disks_payload=None):
    sid = servers.upsert({"ip": ip, "hostname": "h-" + ip, "status": "online"})
    if disks_payload is not None:
        servers.set_disks_by_ip(ip, json.dumps(disks_payload))
    return sid


def test_drives_from_row_handles_empty():
    assert _drives_from_row({}) == []
    assert _drives_from_row({"disks_json": None}) == []
    assert _drives_from_row({"disks_json": ""}) == []


def test_drives_from_row_parses_cached_json():
    payload = [{"name": "nvme0n1", "protocol": "NVME", "media_type": "SSD",
                "capacity_bytes": 1_920_000_000_000, "model": "Samsung", "serial": "x"}]
    drives = _drives_from_row({"disks_json": json.dumps(payload)})
    assert len(drives) == 1
    assert drives[0].name == "nvme0n1"
    assert drives[0].is_nvme


def test_preview_with_strategy_marks_match_and_no_match(db_conn):
    orch, iso_repo, servers, *_ = _make_orch(db_conn)
    nvme_payload = [{
        "name": "nvme0n1", "protocol": "NVME", "media_type": "SSD",
        "capacity_bytes": 1_920_000_000_000, "model": "Samsung",
        "serial": "s1", "slot": None, "redfish_uri": "",
    }]
    sata_payload = [{
        "name": "sda", "protocol": "SATA", "media_type": "HDD",
        "capacity_bytes": 2_000_000_000_000, "model": "WDC",
        "serial": "s2", "slot": None, "redfish_uri": "",
    }]
    s1 = _seed_server_with_disks(servers, ip="10.0.0.1", disks_payload=nvme_payload)
    s2 = _seed_server_with_disks(servers, ip="10.0.0.2", disks_payload=sata_payload)

    rows = orch.preview([s1, s2], None, strategy_override="first_nvme")
    matched = [r for r in rows if r["resolution"]["status"] == "matched"]
    unmatched = [r for r in rows if r["resolution"]["status"] == "unmatched"]
    assert len(matched) == 1 and matched[0]["ip"] == "10.0.0.1"
    assert len(unmatched) == 1 and unmatched[0]["ip"] == "10.0.0.2"


def test_preview_without_strategy_or_profile_says_unmatched(db_conn):
    orch, _, servers, *_ = _make_orch(db_conn)
    s = _seed_server_with_disks(servers, ip="10.0.0.7", disks_payload=[])
    rows = orch.preview([s], None)
    assert rows[0]["resolution"]["status"] == "unmatched"


def test_start_job_validates_empty_selection(db_conn):
    orch, iso_repo, *_ = _make_orch(db_conn)
    iid = _stock_iso(iso_repo)
    errors: list[str] = []
    out = orch.start_job(
        server_ids=[], iso_id=iid, os_profile_id=None,
        disk_profile_id=None, confirm_token="dry-run",
        on_error=errors.append,
    )
    assert out is None
    assert errors and "No servers" in errors[0]


def test_start_job_validates_missing_iso(db_conn):
    orch, *_ = _make_orch(db_conn)
    errors: list[str] = []
    out = orch.start_job(
        server_ids=[1], iso_id=9999, os_profile_id=None,
        disk_profile_id=None, confirm_token="dry-run",
        on_error=errors.append,
    )
    assert out is None
    assert errors and "ISO not found" in errors[0]


def test_start_job_clamps_concurrency_at_32(db_conn):
    """Power-cycle batch must never exceed 32 — breaker inrush safety."""
    orch, iso_repo, servers, prov_repo, _ = _make_orch(db_conn)
    iid = _stock_iso(iso_repo)
    sid = _seed_server_with_disks(servers, ip="10.0.0.20")
    job_id = orch.start_job(
        server_ids=[sid], iso_id=iid, os_profile_id=None,
        disk_profile_id=None, confirm_token="dry-run",
        concurrency=999,
    )
    assert job_id is not None
    assert prov_repo.get_job(job_id)["concurrency"] == 32


def test_start_job_records_confirm_token_for_audit(db_conn):
    orch, iso_repo, servers, prov_repo, _ = _make_orch(db_conn)
    iid = _stock_iso(iso_repo)
    sid = _seed_server_with_disks(servers, ip="10.0.0.30")
    job_id = orch.start_job(
        server_ids=[sid], iso_id=iid, os_profile_id=None,
        disk_profile_id=None, confirm_token="WIPE 1 SERVERS",
    )
    assert prov_repo.get_job(job_id)["confirm_token"] == "WIPE 1 SERVERS"


def test_preview_override_wins_over_strategy(db_conn):
    """Operator's per-host disk pick must beat the bulk strategy."""
    orch, _, servers, *_ = _make_orch(db_conn)
    payload = [
        {"name": "nvme0n1", "protocol": "NVME", "media_type": "SSD",
         "capacity_bytes": 240_000_000_000, "model": "Samsung",
         "serial": "x", "slot": None, "redfish_uri": ""},
        {"name": "sda", "protocol": "SATA", "media_type": "HDD",
         "capacity_bytes": 2_000_000_000_000, "model": "WDC",
         "serial": "y", "slot": None, "redfish_uri": ""},
    ]
    sid = _seed_server_with_disks(servers, ip="10.0.0.50", disks_payload=payload)
    rows = orch.preview([sid], None, strategy_override="first_nvme",
                        overrides={sid: "sda"})
    assert rows[0]["resolution"]["status"] == "override"
    assert rows[0]["resolution"]["name"] == "sda"
    assert "override" in rows[0]["resolution"]["reason"].lower()


def test_preview_override_for_missing_drive_marks_unmatched(db_conn):
    """A typo or removed drive in the override should fail loud, not silent."""
    orch, _, servers, *_ = _make_orch(db_conn)
    payload = [{"name": "nvme0n1", "protocol": "NVME", "media_type": "SSD",
                "capacity_bytes": 240_000_000_000, "model": "x",
                "serial": "x", "slot": None, "redfish_uri": ""}]
    sid = _seed_server_with_disks(servers, ip="10.0.0.51", disks_payload=payload)
    rows = orch.preview([sid], None, overrides={sid: "nvme9n9"})
    assert rows[0]["resolution"]["status"] == "unmatched"
    assert "not in inventory" in rows[0]["resolution"]["reason"]


def test_preview_includes_power_state(db_conn):
    """UI shows a power-state badge so operators don't surprise-wipe live hosts."""
    orch, _, servers, *_ = _make_orch(db_conn)
    sid = servers.upsert({"ip": "10.0.0.60", "status": "online", "power_state": "On"})
    rows = orch.preview([sid], None, strategy_override="first_nvme")
    assert rows[0]["power_state"] == "On"


def test_preview_accepts_string_keys_in_overrides(db_conn):
    """QML JSON.stringify produces string keys; the orchestrator must
    accept them so the controller doesn't need to deep-rekey first."""
    orch, _, servers, *_ = _make_orch(db_conn)
    payload = [{"name": "sda", "protocol": "SATA", "media_type": "HDD",
                "capacity_bytes": 1_000_000_000, "model": "x",
                "serial": "x", "slot": None, "redfish_uri": ""}]
    sid = _seed_server_with_disks(servers, ip="10.0.0.61", disks_payload=payload)
    rows = orch.preview([sid], None, overrides={str(sid): "sda"})
    assert rows[0]["resolution"]["status"] == "override"


def test_start_job_creates_tasks_with_resolved_disk_recorded(db_conn):
    """Per-task disk resolution must be captured on the task row so the
    audit trail freezes what disk we intended to wipe at job time."""
    orch, iso_repo, servers, prov_repo, _ = _make_orch(db_conn)
    iid = _stock_iso(iso_repo)
    nvme = [{
        "name": "nvme0n1", "protocol": "NVME", "media_type": "SSD",
        "capacity_bytes": 1_920_000_000_000, "model": "Samsung",
        "serial": "s1", "slot": None, "redfish_uri": "",
    }]
    sid = _seed_server_with_disks(servers, ip="10.0.0.40", disks_payload=nvme)

    # Inject a strategy directly via the orchestrator's preview override path
    # — start_job uses the preview helper, but doesn't expose strategy_override
    # so we patch the disk-profile lookup with a stub row.
    from data.repositories.disk_profile_repo import DiskProfileRepo
    repo = DiskProfileRepo(db_conn)
    pid = repo.add(name="lab-nvme", strategy="first_nvme")
    job_id = orch.start_job(
        server_ids=[sid], iso_id=iid, os_profile_id=None,
        disk_profile_id=pid, confirm_token="dry-run",
    )
    tasks = prov_repo.list_tasks(job_id)
    assert len(tasks) == 1
    payload = json.loads(tasks[0]["resolved_disk"])
    assert payload["status"] == "matched"
    assert payload["name"] == "nvme0n1"
