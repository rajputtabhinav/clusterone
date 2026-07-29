"""Bulk OS install orchestrator (v1.1).

Drives the full per-task state machine:

    queued
      → resolving_disk          (disk resolver applied to host's disks_json)
      → rendering_config        (OS profile + per-host context → autoinstall body)
      → publishing              (registered with FileServer; signed URL minted)
      → mounting_iso            (plugin.mount_iso via Virtual Media)
      → setting_boot            (plugin.set_boot_once = Cd, one-time)
      → power_cycling           (plugin.power_cycle = ForceRestart)
      → installing              (host pulls ISO, runs autoinstall, reboots)
      → verifying               (TCP ping on port 22 / 5985 — host reachable?)
      → completed | failed

Each transition persists state via ProvisioningRepo so an app restart
can resume monitoring (post-power-cycle, our job is purely observation
until the host comes back up; restarting ClusterOne mid-install doesn't
abort the install).

Multi-stage safety:
* Caller must compute a per-host preview + WIPE confirmation BEFORE calling
  ``start_job`` — the orchestrator trusts that the preview already happened.
* The ``confirm_token`` is recorded on the job row for audit.
* Resolved disk + drive metadata is captured on each task at kickoff so
  later inventory changes can't retroactively confuse the audit trail.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Callable

from core.engine.async_engine import AsyncEngine
from core.models.drive import Drive
from core.models.server import Credentials, ServerInfo
from core.plugins_api.base import BootTarget, RedfishProbe
from core.plugins_api.registry import PluginRegistry
from core.services.activity_service import ActivityService
from core.services.autoinstall_renderer import render as render_autoinstall
from core.services.credentials_service import CredentialsService
from core.services.disk_resolver import resolve as resolve_disk
from core.services.file_server import FileServer
from data.repositories.disk_profile_repo import DiskProfileRepo
from data.repositories.iso_repo import IsoRepo
from data.repositories.provisioning_repo import ProvisioningRepo
from data.repositories.servers_repo import ServersRepo

log = logging.getLogger(__name__)


class ProvisioningOrchestrator:
    def __init__(
        self,
        engine: AsyncEngine,
        provisioning_repo: ProvisioningRepo,
        servers_repo: ServersRepo,
        iso_repo: IsoRepo,
        disk_profile_repo: DiskProfileRepo,
        plugins: PluginRegistry,
        activity: ActivityService,
        credentials: CredentialsService | None = None,
        file_server: FileServer | None = None,
    ) -> None:
        self._engine = engine
        self._jobs = provisioning_repo
        self._servers = servers_repo
        self._isos = iso_repo
        self._disk_profiles = disk_profile_repo
        self._plugins = plugins
        self._activity = activity
        self._credentials = credentials
        self._file_server = file_server
        self._cancel_event: asyncio.Event | None = None
        self._current_job_id: int | None = None
        # Map autoinstall token → (rendered_config, expires_at_epoch).
        # The TTL on entries means a token that wasn't cleaned up
        # (engine cancelled, app crashed mid-install, etc.) doesn't
        # leak a root password forever — entries auto-expire and are
        # purged on the next provider call. 30 min is enough for any
        # legitimate installer to fetch its config.
        self._configs: dict[str, tuple[str, float]] = {}
        self._configs_ttl_s: float = 30 * 60

    # ---- file server integration --------------------------------------

    def autoinstall_provider(self, token: str) -> bytes | None:
        # Sweep expired entries every call — cheap, and prevents a
        # crashed/cancelled job from leaving root passwords resident
        # in memory longer than necessary.
        self._purge_expired_configs()
        entry = self._configs.get(token)
        if entry is None:
            return None
        body, _expires = entry
        return body.encode("utf-8")

    def _purge_expired_configs(self) -> None:
        import time as _time
        now = _time.time()
        expired = [k for k, (_, exp) in self._configs.items() if exp <= now]
        for k in expired:
            self._configs.pop(k, None)

    # ---- public API ---------------------------------------------------

    def preview(
        self,
        server_ids: list[int],
        disk_profile_id: int | None,
        strategy_override: str | None = None,
        overrides: dict[int, str] | None = None,
    ) -> list[dict]:
        """Produce the per-host preview rows BEFORE the operator confirms.

        ``overrides`` (``{server_id: drive_name}``) wins over both the
        strategy and any saved disk profile — that's how the per-host
        picker in the UI signals "this server, this exact drive".

        Each returned row:
        ``{server_id, hostname, ip, oem, power_state, disk_count, resolution}``
        where ``resolution`` is ``{name, model, capacity_gb, reason, status}``.
        Status values: ``matched`` | ``unmatched`` | ``ambiguous`` |
        ``requires_override`` | ``override`` (operator-picked).
        """
        strategy = strategy_override or ""
        if not strategy and disk_profile_id is not None:
            prof = self._disk_profiles.get(disk_profile_id)
            if prof:
                strategy = prof.get("strategy") or ""
        overrides = overrides or {}
        out = []
        for sid in server_ids:
            row = self._servers.get(sid)
            if row is None:
                continue
            drives = _drives_from_row(row)
            res = self._resolve_for(sid, drives, strategy, overrides)
            out.append({
                "server_id": sid,
                "hostname": row.get("hostname") or row.get("ip"),
                "ip": row.get("ip"),
                "oem": row.get("oem") or "",
                "power_state": row.get("power_state") or "Unknown",
                "disk_count": len(drives),
                "resolution": res,
            })
        return out

    @staticmethod
    def _resolve_for(server_id: int, drives, strategy: str,
                     overrides: dict[int, str]) -> dict:
        """Pick the drive for one server, honoring an explicit override first."""
        override_name = overrides.get(server_id) or overrides.get(str(server_id))
        if override_name:
            for d in drives:
                if d.name == override_name:
                    return {
                        "name": d.name, "model": d.model,
                        "capacity_gb": d.capacity_gb,
                        "serial": d.serial,   # stable disk-targeting key
                        "reason": "operator override",
                        "status": "override",
                    }
            return {
                "name": override_name, "model": None, "capacity_gb": None,
                "serial": None,
                "reason": f"override '{override_name}' not in inventory",
                "status": "unmatched",
            }
        if not strategy:
            return {"name": None, "model": None, "capacity_gb": None,
                    "serial": None,
                    "reason": "no strategy or override selected",
                    "status": "unmatched"}
        res = resolve_disk(strategy, drives)
        return {
            "name": res.drive.name if res.drive else None,
            "model": res.drive.model if res.drive else None,
            "capacity_gb": res.drive.capacity_gb if res.drive else None,
            "serial": res.drive.serial if res.drive else None,
            "reason": res.reason,
            "status": res.status,
        }

    def start_job(
        self,
        *,
        server_ids: list[int],
        iso_id: int,
        os_profile_id: int | None,
        disk_profile_id: int | None,
        confirm_token: str,
        concurrency: int = 8,
        strategy_override: str | None = None,
        overrides: dict[int, str] | None = None,
        on_task_update: Callable[[int, str, int, str], None] | None = None,
        on_job_done: Callable[[dict], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> int | None:
        """Create the job + per-server tasks and dispatch to the async engine.

        Caller MUST have already shown a preview screen and collected the
        operator's typed confirmation token. The orchestrator records that
        token for the audit log and otherwise trusts the caller.
        """
        if not server_ids:
            if on_error: on_error("No servers selected")
            return None
        iso = self._isos.get(iso_id)
        if iso is None:
            if on_error: on_error("ISO not found")
            return None
        # Stagger power-cycle to avoid breaker inrush; cap at 32 even if UI asks more.
        concurrency = max(1, min(32, int(concurrency)))

        job_id = self._jobs.create_job(
            iso_id=iso_id, os_profile_id=os_profile_id,
            disk_profile_id=disk_profile_id,
            concurrency=concurrency,
            confirm_token=confirm_token or "",
        )

        # Build per-host tasks, capturing the resolver's pick (with any
        # operator overrides applied) on each task row so the audit trail
        # freezes the disk we *intended* to wipe at job time.
        previews = self.preview(
            server_ids, disk_profile_id,
            strategy_override=strategy_override,
            overrides=overrides,
        )
        task_ids: list[int] = []
        for p in previews:
            resolved_blob = json.dumps(p["resolution"])
            tid = self._jobs.create_task(
                job_id, p["server_id"], resolved_disk=resolved_blob,
            )
            task_ids.append(tid)

        self._jobs.update_job_state(job_id, "running", total=len(task_ids))
        self._activity.log(
            "warning", "provisioning",
            f"Provisioning job #{job_id} started — {iso['name']} "
            f"on {len(task_ids)} server(s); confirm='{confirm_token[:40]}'",
            job_id=job_id,
        )
        self._current_job_id = job_id
        self._engine.submit(self._run_job(
            job_id, task_ids, iso, os_profile_id, disk_profile_id,
            concurrency,
            on_task_update, on_job_done, on_error,
        ))
        return job_id

    def cancel(self) -> None:
        if self._cancel_event is not None:
            self._engine.call_soon(self._cancel_event.set)

    @property
    def current_job_id(self) -> int | None:
        return self._current_job_id

    def resume_interrupted(self) -> int:
        """Settle any provisioning task/job left mid-flight by a prior run.

        Provisioning can't truly resume across an app restart — the in-memory
        autoinstall config + signed URLs are gone and the install proceeds on
        the host independently of us. Mark stranded tasks failed (the operator
        verifies the host manually) and settle the jobs so a destructive wipe
        never sits 'running' forever in the audit trail. Returns the count of
        tasks touched.
        """
        stranded = self._jobs.list_non_terminal_tasks()
        n = 0
        if stranded:
            n = self._jobs.fail_non_terminal_tasks(
                "App was restarted mid-install — ClusterOne cannot resume an OS "
                "install. Verify the host and re-run if needed."
            )
            self._activity.log(
                "warning", "provisioning",
                f"{n} provisioning task(s) marked failed on startup "
                "(an OS install can't be resumed across a restart)",
            )
        # Settle jobs too (covers orphan jobs that crashed with zero tasks).
        self._jobs.fail_non_terminal_jobs()
        return n

    # ---- internal -----------------------------------------------------

    async def _run_job(self, job_id, task_ids, iso, os_profile_id,
                       disk_profile_id, concurrency,
                       on_task_update, on_job_done, on_error):
        self._cancel_event = asyncio.Event()
        sem = asyncio.Semaphore(max(1, concurrency))
        succeeded = 0
        failed = 0

        # Snapshot the event at job start. If a concurrent start_job
        # later reassigns self._cancel_event, this local stays bound to
        # the original (matches the update_orchestrator fix).
        job_cancel_evt = self._cancel_event

        async def run_one(task_id: int) -> None:
            nonlocal succeeded, failed
            cancel_evt = job_cancel_evt
            if cancel_evt is None or cancel_evt.is_set():
                self._mark_cancelled(task_id, on_task_update)
                failed += 1
                return
            async with sem:
                if cancel_evt.is_set():
                    self._mark_cancelled(task_id, on_task_update)
                    failed += 1
                    return
                # Catch unhandled exceptions so the counter stays accurate
                # (same defense-in-depth as update_orchestrator).
                try:
                    ok = await self._run_task(task_id, iso, on_task_update)
                except Exception as exc:
                    log.exception("Provisioning task %s crashed: %s", task_id, exc)
                    self._jobs.update_task(
                        task_id, state="failed", progress=0,
                        message=f"Internal error: {type(exc).__name__}: {exc}"[:300],
                    )
                    ok = False
                if ok:
                    succeeded += 1
                else:
                    failed += 1

        try:
            await asyncio.gather(*(run_one(tid) for tid in task_ids),
                                 return_exceptions=True)
            # Use the LOCAL job_cancel_evt — not self._cancel_event,
            # which a concurrent start_job may have reassigned.
            final_state = (
                "cancelled" if (job_cancel_evt and job_cancel_evt.is_set())
                else "failed" if succeeded == 0 and failed > 0
                else "completed_with_errors" if failed > 0
                else "completed"
            )
            self._jobs.update_job_state(
                job_id, final_state, succeeded=succeeded, failed=failed,
            )
            level = "success" if failed == 0 else ("error" if succeeded == 0 else "warning")
            self._activity.log(
                level, "provisioning",
                f"Provisioning job #{job_id} finished — {succeeded} ok, {failed} failed",
                job_id=job_id,
            )
            if on_job_done:
                on_job_done({"job_id": job_id, "succeeded": succeeded,
                             "failed": failed, "state": final_state})
        except Exception as exc:
            log.exception("Provisioning job %s crashed", job_id)
            self._jobs.update_job_state(job_id, "failed")
            if on_error:
                on_error(str(exc))
        finally:
            self._cancel_event = None
            self._current_job_id = None

    def _mark_cancelled(self, task_id, on_task_update):
        self._jobs.update_task(task_id, state="cancelled", message="Cancelled")
        if on_task_update:
            on_task_update(task_id, "cancelled", 0, "Cancelled")

    async def _run_task(self, task_id, iso, on_task_update) -> bool:
        def progress(state: str, pct: int, message: str = "") -> None:
            self._jobs.update_task(task_id, state=state, progress=pct, message=message)
            if on_task_update:
                on_task_update(task_id, state, pct, message)

        try:
            await self._real_task(task_id, iso, progress)
            progress("completed", 100, "")
            return True
        except asyncio.CancelledError:
            progress("cancelled", 0, "Cancelled")
            return False
        except Exception as exc:
            log.warning("Provisioning task %s failed: %s", task_id, exc)
            progress("failed", 0, str(exc))
            return False

    async def _real_task(self, task_id, iso, progress) -> None:
        """The genuine flow — Virtual Media mount, boot override, power cycle,
        wait for installer, verify reachable."""
        task = self._jobs.get_task(task_id)
        if task is None:
            raise RuntimeError("Task vanished from DB")
        server = self._servers.get(task["server_id"])
        if server is None:
            raise RuntimeError("Server not found")
        info = _server_info_from_row(server)
        if not info.redfish_root:
            raise RuntimeError("Server has no Redfish endpoint (re-run discovery)")

        oem_override = (server.get("oem_override") or "").strip() or None
        if not oem_override:
            raise RuntimeError(
                "No OEM selected for this server — open the server panel and "
                "choose the vendor before provisioning"
            )
        plugin = self._plugins.resolve(
            RedfishProbe(
                ip=info.ip, manufacturer=info.manufacturer,
                redfish_root=info.redfish_root,
            ),
            override_key=oem_override,
        )
        if plugin is None:
            raise RuntimeError(
                f"Selected OEM '{oem_override}' has no installed plugin — "
                "re-select the vendor in the server panel"
            )
        creds = self._resolve_creds(info)

        # Confirm a disk was resolved at job kickoff (preview).
        progress("resolving_disk", 5)
        resolved = json.loads(task.get("resolved_disk") or "{}")
        # "matched", "override", and "ambiguous" all have a concrete drive
        # name and are safe to proceed with.  "requires_override" and
        # "unmatched" have no drive and must be rejected before any wipe.
        runnable_statuses = {"matched", "override", "ambiguous"}
        if resolved.get("status") not in runnable_statuses:
            raise RuntimeError(
                f"Disk not resolved at preview "
                f"(status={resolved.get('status')!r}): "
                f"{resolved.get('reason', 'unknown')}"
            )

        # Render autoinstall config (Sprint 3 wiring; OS profile fetch deferred
        # to v1.1.1 — for now we use the bundled template by os_family).
        progress("rendering_config", 10)
        # Target by serial (stable); render_autoinstall REFUSES (raises, caught
        # below as a clean task failure) rather than wipe a guessed disk if it
        # can't identify the target safely.
        cfg_body = render_autoinstall(
            template_body=None,
            os_family=iso["os_family"],
            hostname=info.hostname or info.ip,
            ip=info.ip,
            disk=resolved.get("name") or "",
            serial=resolved.get("serial") or "",
            root_password="",   # OS profile would carry the vault-encrypted pw
        )
        token = secrets.token_urlsafe(24)
        import time as _time
        # Store with TTL so a crashed / cancelled job doesn't leak root
        # passwords forever. The autoinstall_provider sweeps expired
        # entries on every call.
        self._configs[token] = (cfg_body, _time.time() + self._configs_ttl_s)

        try:
            # Publish: mint signed URLs for ISO + autoinstall config.
            progress("publishing", 15)
            if self._file_server is None:
                raise RuntimeError("File server not started — cannot publish ISO")
            iso_url = self._file_server.signed_iso_url(iso["sha256"])
            # autoinstall_url is consumed by the installer (kickstart's `inst.ks=…`
            # boot-line, or cloud-init's seed URL). For v1.1 the URL is recorded
            # for audit; advanced flows use it via the OS profile's boot-line.
            _ai_url = self._file_server.signed_autoinstall_url(token)

            progress("mounting_iso", 25)
            r = await plugin.mount_iso(info, iso_url, creds)
            if not r.ok:
                raise RuntimeError(f"Mount failed: {r.message}")

            progress("setting_boot", 30)
            r = await plugin.set_boot_once(info, BootTarget.CD, creds)
            if not r.ok:
                raise RuntimeError(f"Set-boot failed: {r.message}")

            progress("power_cycling", 40)
            r = await plugin.power_cycle(info, creds)
            if not r.ok:
                raise RuntimeError(f"Power cycle failed: {r.message}")

            # Wait for the installer to do its thing. Heuristic: poll until SSH
            # is reachable. Real OS install on NVMe Linux is 4–10 min; we cap
            # at 30 min and surface a failure if the host doesn't come back.
            progress("installing", 50)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 30 * 60   # 30-min ceiling
            while loop.time() < deadline:
                if self._cancel_event and self._cancel_event.is_set():
                    raise asyncio.CancelledError()
                if await _tcp_ready(info.ip, 22, timeout=2.0):
                    break
                # Bump progress so the bar moves while we wait. Map the
                # remaining-time fraction onto [50, 85] so the bar is at
                # 50% when we start the wait, 85% when the 30-min deadline
                # is hit. (Old code did `min(85, 50 + int((30 - secs/60)))`
                # which is mostly negative → int → 0, bar stuck at 50.)
                remaining_s = max(0.0, deadline - loop.time())
                elapsed_frac = 1.0 - (remaining_s / (30 * 60))
                progress("installing", 50 + int(35 * elapsed_frac))
                await asyncio.sleep(15)
            else:
                raise RuntimeError("Installer never came back (30-min ceiling exceeded)")

            progress("verifying", 95)
            if not await _tcp_ready(info.ip, 22, timeout=5.0):
                raise RuntimeError("Host not SSH-reachable after install")

            # Eject the virtual media so the next reboot boots from disk cleanly.
            await plugin.eject_iso(info, creds)
        finally:
            # Always remove the per-host autoinstall config from memory —
            # regardless of success, failure, or cancellation. Without this
            # `finally`, a failed install leaks the rendered config body and
            # the accumulation of many failed jobs grows `_configs` without
            # bound.
            self._configs.pop(token, None)

    def _resolve_creds(self, info: ServerInfo) -> Credentials:
        if self._credentials is not None:
            saved = self._credentials.resolve(ip=info.ip, oem=info.oem)
            if saved is not None:
                return saved
        return Credentials(username="", password="")


# ---- helpers --------------------------------------------------------------


def _drives_from_row(server_row: dict) -> list[Drive]:
    raw = (server_row or {}).get("disks_json") or ""
    if not raw:
        return []
    try:
        return [Drive.from_dict(d) for d in json.loads(raw)]
    except Exception:
        return []


def _server_info_from_row(row: dict) -> ServerInfo:
    return ServerInfo(
        ip=row["ip"],
        hostname=row.get("hostname"),
        manufacturer=row.get("manufacturer"),
        # Manual override wins — keeps credential scoping + validate() aligned
        # with the plugin the registry will actually route to.
        oem=(row.get("oem_override") or "").strip() or row.get("oem"),
        model=row.get("model"),
        serial=row.get("serial"),
        bmc_version=row.get("bmc_version"),
        bios_version=row.get("bios_version"),
        redfish_root=row.get("redfish_root"),
        tls_fingerprint=row.get("tls_fpr"),
        drives=_drives_from_row(row),
    )


async def _tcp_ready(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False
