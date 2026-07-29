"""Run the actual flash from Python — bypassing all QML/UI.

This calls UpdateOrchestrator.start_job with the same args the QML Flash
button would pass. If this works, the bug is in the UI. If it fails, the
real BMC error surfaces here in the terminal.

USE WITH CAUTION: this triggers a REAL flash on the BMC if everything
passes pre-flight. Pass --dry to STOP after pre-flight (no upload).

Usage:
    python tools/flash_now.py BIOS --dry      # safe — only pre-flight
    python tools/flash_now.py BIOS            # commits — real flash
    python tools/flash_now.py BMC --dry
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time

os.environ["CLUSTERONE_ALLOW_PLAINTEXT_VAULT"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path                                     # noqa: E402

from app import config                                       # noqa: E402
from core.engine.async_engine import AsyncEngine             # noqa: E402
from core.plugins_api.registry import PluginRegistry         # noqa: E402
from core.security.credential_vault import CredentialVault   # noqa: E402
from core.services.activity_service import ActivityService   # noqa: E402
from core.services.credentials_service import CredentialsService  # noqa: E402
from core.services.update_orchestrator import UpdateOrchestrator  # noqa: E402
from data.repositories.activity_repo import ActivityRepo      # noqa: E402
from data.repositories.credentials_repo import CredentialsRepo  # noqa: E402
from data.repositories.firmware_repo import FirmwareRepo      # noqa: E402
from data.repositories.jobs_repo import JobsRepo              # noqa: E402
from data.repositories.servers_repo import ServersRepo        # noqa: E402


def main():
    if len(sys.argv) < 2 or sys.argv[1].upper() not in ("BIOS", "BMC"):
        print("Usage: python tools/flash_now.py <BIOS|BMC> [--dry | --commit FLASH]")
        print()
        print("  --dry      Stop after pre-flight; no firmware bytes uploaded (safe).")
        print("  --commit FLASH   Required to actually push firmware to the BMC.")
        print("                   The literal token 'FLASH' must be typed — a typo")
        print("                   or missing arg aborts. Replaces the old 5-second")
        print("                   sleep, which any operator could blow through.")
        sys.exit(2)
    fw_type = sys.argv[1].upper()
    rest = sys.argv[2:]
    dry = "--dry" in rest
    commit_ok = False
    if "--commit" in rest:
        idx = rest.index("--commit")
        if idx + 1 < len(rest) and rest[idx + 1] == "FLASH":
            commit_ok = True
        else:
            print("ERROR: --commit requires the literal token FLASH (caps).")
            print("       Example: python tools/flash_now.py BIOS --commit FLASH")
            sys.exit(2)
    if not dry and not commit_ok:
        print("Refusing to push firmware without an explicit --commit FLASH token.")
        print("Run with --dry to do a safe pre-flight check first, then re-run with")
        print("    python tools/flash_now.py %s --commit FLASH" % fw_type)
        print("once you're confident.")
        sys.exit(2)

    print(f"\n=== flash_now: target={fw_type}  dry={dry}  commit={commit_ok} ===\n")

    db = sqlite3.connect(str(config.db_path()), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")

    servers = ServersRepo(db)
    firmware = FirmwareRepo(db)
    jobs = JobsRepo(db)
    activity = ActivityService(ActivityRepo(db))
    creds = CredentialsService(CredentialsRepo(db), CredentialVault())

    plugins = PluginRegistry()
    plugins.load_bundled(Path(__file__).resolve().parent.parent / "plugins")
    print(f"Plugins loaded: {[p.key for p in plugins.all()]}")

    srv = servers.list()[0]
    fw = next((r for r in firmware.list() if r["type"] == fw_type), None)
    if not fw:
        print(f"No {fw_type} firmware in DB"); return 1
    print(f"Server: id={srv['id']} ip={srv['ip']} oem={srv['oem']}")
    print(f"Firmware: id={fw['id']} {fw['name'][:60]}")

    engine = AsyncEngine()
    engine.start()

    orch = UpdateOrchestrator(
        engine=engine, plugins=plugins,
        jobs_repo=jobs, servers_repo=servers, firmware_repo=firmware,
        activity=activity, credentials=creds,
    )

    done = []
    errors = []
    def on_task(tid, state, pct, msg):
        line = f"  task#{tid:>3}  {state:10}  {pct:>3}%  {msg[:100]}"
        print(line)
    def on_job_done(stats):
        print(f"\n[JOB DONE] {stats}")
        done.append(stats)
    def on_error(msg):
        print(f"\n[ERROR] {msg}")
        errors.append(msg)

    # In --dry mode, monkey-patch the plugin to abort right after pre-flight
    if dry:
        from core.plugins_api import base as plugin_base
        for p in plugins.all():
            if hasattr(p, "update_bios"):
                async def stop(*a, **k):
                    return plugin_base.Result(ok=False, message="DRY: stopped after pre-flight (no upload)")
                p.update_bios = stop
                p.update_bmc = stop
    else:
        print("\n!!! THIS IS A REAL FLASH — firmware bytes WILL be pushed to the BMC !!!")
        print(f"!!! Server : {srv['ip']}")
        print(f"!!! Target : {fw_type}")
        print(f"!!! File   : {fw['name']}")
        print("!!! --commit FLASH token verified, proceeding now.")

    print(f"\nStarting job: server_ids=[{srv['id']}] firmware_id={fw['id']}")
    job_id = orch.start_job(
        server_ids=[srv["id"]],
        firmware_id=fw["id"],
        concurrency=1,
        username="", password="",
        apply_now=False,
        on_task_update=on_task,
        on_job_done=on_job_done,
        on_error=on_error,
    )
    print(f"job_id = {job_id}")
    if job_id is None:
        print("FAIL: orchestrator returned None — couldn't create job")
        engine.stop()
        return 1

    # Wait for completion. Real BMC flash takes a few minutes; BIOS more.
    # 30 min ceiling matches the orchestrator's own polling timeout.
    wait_s = 30 if dry else 30 * 60
    print(f"Waiting up to {wait_s}s for completion...")
    deadline = time.time() + wait_s
    while not done and not errors and time.time() < deadline:
        time.sleep(2)
    if not done and not errors:
        print(f"\nTimeout — flash didn't finish in {wait_s}s")

    # Print final DB state
    print(f"\n=== Final DB state ===")
    for r in jobs.list_jobs(limit=2):
        print(f"  job#{r['id']} fw_type={r['fw_type']} state={r['state']} "
              f"total={r['total']} succ={r['succeeded']} fail={r['failed']}")
    for t in jobs.list_tasks(job_id):
        print(f"  task#{t['id']} state={t['state']} pct={t['progress']} "
              f"msg={(t.get('message') or '')[:120]}")
    engine.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
