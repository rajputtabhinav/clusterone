"""Update orchestrator + per-task state machine.

Drives bulk firmware updates: builds the job + tasks in SQLite, schedules each
task on the async engine bounded by a concurrency semaphore, and walks every
task through the safe state machine (precheck → uploading → flashing → rebooting
→ verifying → completed | failed). Each transition is persisted so an app
restart can resume monitoring from the stored Redfish ``TaskMonitor`` URI.

Flashes delegate to the OEM plugin's ``update_bmc`` / ``update_bios``.

Credential resolution for flashes follows a clear precedence:

1. Username/password supplied by the UI (typed in the Updates page).
2. Saved vault credential for the host scope (``host:<ip>``).
3. Saved vault credential for the OEM scope (``oem:<key>``).
4. Saved vault credential under ``default``.
5. None — the plugin will surface a 401 with a clear error message.

The vault lookup is delegated to ``CredentialsService.resolve``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from core.engine.async_engine import AsyncEngine
from core.models.firmware import Firmware
from core.models.server import Credentials, ServerInfo
from core.plugins_api.base import RedfishProbe
from core.plugins_api.registry import PluginRegistry
from core.services.activity_service import ActivityService
from core.services.credentials_service import CredentialsService
from data.repositories.firmware_repo import FirmwareRepo
from data.repositories.jobs_repo import JobsRepo
from data.repositories.servers_repo import ServersRepo

log = logging.getLogger(__name__)


class UpdateOrchestrator:
    def __init__(
        self,
        engine: AsyncEngine,
        jobs_repo: JobsRepo,
        servers_repo: ServersRepo,
        firmware_repo: FirmwareRepo,
        plugins: PluginRegistry,
        activity: ActivityService,
        credentials: Optional[CredentialsService] = None,
    ) -> None:
        self._engine = engine
        self._jobs = jobs_repo
        self._servers = servers_repo
        self._firmware = firmware_repo
        self._plugins = plugins
        self._activity = activity
        # Optional: used to resolve saved BMC creds when the UI didn't pass
        # any. Tests construct the orchestrator without it.
        self._credentials = credentials
        self._cancel_event: asyncio.Event | None = None
        self._current_job_id: int | None = None

    # ---- public API ----
    def start_job(
        self,
        server_ids: list[int],
        firmware_id: int,
        *,
        concurrency: int = 4,
        username: str = "",
        password: str = "",
        apply_now: bool = False,
        on_task_update: Callable[[int, str, int, str], None] | None = None,
        on_job_done: Callable[[dict], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> int | None:
        """Create job + tasks and schedule them. Returns the new job id."""
        if not server_ids:
            if on_error: on_error("No servers selected")
            return None

        # Clamp concurrency: protect the local box from a 999-target spawn.
        concurrency = max(1, min(64, int(concurrency)))

        firmware = self._lookup_firmware(firmware_id)
        if firmware is None:
            if on_error: on_error("Firmware not found")
            return None

        job_id = self._jobs.create_job(
            fw_type=firmware["type"],
            concurrency=concurrency,
        )

        task_ids: list[int] = []
        for sid in server_ids:
            server = self._servers.get(sid)
            if server is None:
                continue
            ver_before = (
                server.get("bmc_version") if firmware["type"] == "BMC"
                else server.get("bios_version") if firmware["type"] == "BIOS"
                else None
            )
            tid = self._jobs.create_task(job_id, sid, firmware_id, ver_before)
            task_ids.append(tid)

        self._jobs.update_job_state(job_id, "running", total=len(task_ids))
        self._activity.log(
            "info", "update",
            f"Update job #{job_id} started — {firmware['type']} {firmware['version']} "
            f"on {len(task_ids)} server(s)",
            job_id=job_id,
        )

        self._current_job_id = job_id
        self._engine.submit(self._run_job(
            job_id, task_ids, firmware, concurrency,
            username, password, apply_now,
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
        """Pick up any tasks left non-terminal across an app restart.

        For each task with a saved ``redfish_task`` URI, queue a one-shot
        async poll of that URI against the live BMC. Three outcomes:

        * The BMC reports a terminal ``TaskState`` (Completed/Exception/…):
          we update the row to match and finalize the parent job's counters.
        * The BMC reports it's still ``Running``/``Pending``: the task is
          left in its current state; the periodic poller (started elsewhere)
          will continue tracking it.
        * The BMC is unreachable or the Task URI 404s: the row is marked
          ``failed`` with a clear "task lost; please re-run" message —
          better than silently leaving stuck-running rows on the UI.

        Tasks WITHOUT a saved Task URI (e.g. uploaded but the BMC never
        returned a Task) are marked failed unconditionally; without a URI
        there's no way to recover.

        Returns the count of tasks the resume touched (any outcome).
        """
        tasks = self._jobs.list_non_terminal_tasks()

        no_uri = [t for t in tasks if not t.get("redfish_task")]
        for t in no_uri:
            self._jobs.update_task(
                t["id"], state="failed", progress=0,
                message="App was restarted; task has no Task URI to resume — please re-run.",
            )
        if no_uri:
            self._activity.log(
                "warning", "update",
                f"{len(no_uri)} task(s) without saved Task URI marked failed on startup",
            )

        resumable = [t for t in tasks if t.get("redfish_task")]
        resumable_job_ids = sorted({int(t["job_id"]) for t in resumable})

        # Settle every stale job EXCEPT those whose tasks we're about to resume.
        # finalize_job_from_tasks recomputes counters + terminal state for jobs
        # whose tasks have all settled (fixes the old "stuck running, 0/0" drift);
        # fail_non_terminal_jobs_except then sweeps orphan jobs (crashed with zero
        # tasks) and anything still pending/running. Resumable jobs get finalized
        # by _resume_one_task once their last task settles.
        for jid in {int(t["job_id"]) for t in tasks} - set(resumable_job_ids):
            self._jobs.finalize_job_from_tasks(jid)
        self._jobs.fail_non_terminal_jobs_except(resumable_job_ids)

        if not resumable:
            return len(no_uri)

        # Schedule an async resume for each task. Don't block startup —
        # this returns immediately; results land on the engine loop.
        for t in resumable:
            self._engine.submit(self._resume_one_task(t))
        self._activity.log(
            "info", "update",
            f"Resuming {len(resumable)} task(s) from saved Task URIs on startup",
        )
        return len(no_uri) + len(resumable)

    async def _resume_one_task(self, task_row: dict) -> None:
        """Resume one task, then finalize its parent job from the (now-updated)
        task rows so a resumed job never stays 'running' with 0/0 counters."""
        try:
            await self._resume_one_task_inner(task_row)
        finally:
            try:
                self._jobs.finalize_job_from_tasks(int(task_row["job_id"]))
            except Exception:
                log.exception("finalize job after resume failed (task %s)",
                              task_row.get("id"))

    async def _resume_one_task_inner(self, task_row: dict) -> None:
        """Poll the saved Task URI ONCE and update the task row. A task left
        ``flashing`` (BMC still Running) keeps its job ``running``; the next
        app launch re-runs this resume."""
        from core.plugins_api.redfish_client import RedfishClient, RedfishError
        task_id = task_row["id"]
        task_uri = task_row.get("redfish_task") or ""
        server = self._servers.get(task_row["server_id"])
        if not server:
            self._jobs.update_task(task_id, state="failed", progress=0,
                                   message="Resume: server vanished from DB")
            return
        effective_oem = (server.get("oem_override") or "").strip() or server.get("oem")
        creds = (self._credentials.resolve(ip=server["ip"], oem=effective_oem)
                 if self._credentials else None)
        if not creds:
            self._jobs.update_task(task_id, state="failed", progress=0,
                                   message="Resume: no saved credential for this BMC")
            return
        host, port, scheme = self._parse_redfish_root(server.get("redfish_root"))
        # Guard against the RedfishClient ctor raising synchronously (e.g.
        # bad TLS fingerprint hex). Without the pre-assignment the
        # ``finally`` block NameErrors and obscures the real error.
        client = None
        try:
            client = RedfishClient(host, port=port, scheme=scheme,
                                   username=creds.username, password=creds.password,
                                   timeout=10.0,
                                   tls_fingerprint=server.get("tls_fpr"))
            task = await client.get_json(task_uri)
        except RedfishError as exc:
            self._jobs.update_task(
                task_id, state="failed", progress=task_row.get("progress") or 0,
                message=f"Resume: Task URI {task_uri} unreachable ({exc}) — please re-run.",
            )
            return
        except Exception as exc:
            self._jobs.update_task(
                task_id, state="failed", progress=task_row.get("progress") or 0,
                message=f"Resume: client init failed ({type(exc).__name__}: {exc})",
            )
            return
        finally:
            if client is not None:
                await client.close()

        state = (task.get("TaskState") or "Running").lower()
        pct = int(task.get("PercentComplete", task_row.get("progress") or 0) or 0)
        if state == "completed":
            self._jobs.update_task(task_id, state="completed", progress=100,
                                   message="Resumed: BMC reports completed.")
            self._activity.log("success", "update",
                               f"task#{task_id} resumed and finalized as completed")
        elif state in ("exception", "killed", "cancelled"):
            msgs = task.get("Messages") or []
            detail = msgs[-1].get("Message", "") if msgs else ""
            self._jobs.update_task(
                task_id, state="failed", progress=pct,
                message=f"Resumed: BMC reports {state}. {detail}".strip(),
            )
            self._activity.log("warning", "update",
                               f"task#{task_id} resumed and finalized as {state}")
        else:
            # Still running — leave in current state. Periodic monitor
            # (if running) will continue tracking.
            self._jobs.update_task(task_id, state="flashing", progress=pct,
                                   message=f"Resumed: BMC reports {state} ({pct}%).")
            self._activity.log("info", "update",
                               f"task#{task_id} resumed; BMC reports {state} ({pct}%)")

    # ---- internal ----
    def _lookup_firmware(self, firmware_id: int) -> dict | None:
        # Prefer FirmwareRepo.get(id) (indexed) over scanning the list.
        getter = getattr(self._firmware, "get", None)
        if callable(getter):
            row = getter(firmware_id)
            if row:
                return row
        for row in self._firmware.list():
            if row["id"] == firmware_id:
                return row
        return None

    @staticmethod
    def _compute_final_state(*, cancelled: bool, succeeded: int, failed: int) -> str:
        """Resolve a job's terminal state from its task outcomes.

        Pulled out of ``_run_job`` so it can be exercised directly in unit
        tests — see ``tests/unit/test_update_orchestrator.py``.
        """
        if cancelled:
            return "cancelled"
        if failed > 0 and succeeded == 0:
            return "failed"
        if failed > 0:
            return "completed_with_errors"
        return "completed"

    @staticmethod
    def _verify_firmware_sha(firmware: dict) -> tuple[bool, str]:
        """Re-hash the on-disk firmware blob and compare to the stored SHA-256.

        Called immediately before a real flash so a tampered or moved file is
        caught and the flash is refused.
        """
        import hashlib
        from pathlib import Path
        expected = (firmware.get("sha256") or "").lower()
        if not expected:
            return False, "firmware has no stored sha256"
        file_path = firmware.get("file_path") or ""
        # Demo firmware seeded via data/seed.py has file_path "seed://name" —
        # there's no backing blob to hash, so a real flash must be refused
        # with a clear message rather than the generic "file missing".
        if file_path.startswith("seed://"):
            return False, "demo firmware has no backing file — re-import the real image"
        path = Path(file_path)
        if not path.is_file():
            return False, f"firmware file missing: {path}"
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        actual = h.hexdigest().lower()
        if actual != expected:
            return False, "SHA-256 mismatch — firmware file altered since import"
        return True, "ok"

    async def _run_job(self, job_id, task_ids, firmware, concurrency,
                       username, password, apply_now, on_task_update, on_job_done, on_error):
        self._cancel_event = asyncio.Event()
        job_cancel_evt = self._cancel_event
        sem = asyncio.Semaphore(max(1, concurrency))
        creds = Credentials(username=username, password=password)
        succeeded = 0
        failed = 0

        async def run_one(task_id: int) -> None:
            nonlocal succeeded, failed
            if job_cancel_evt.is_set():
                self._mark_cancelled(task_id, on_task_update)
                failed += 1
                return
            async with sem:
                if job_cancel_evt.is_set():
                    self._mark_cancelled(task_id, on_task_update)
                    failed += 1
                    return
                # Wrap in try/except so an UNHANDLED plugin exception in
                # _run_task increments `failed` instead of escaping into
                # gather() and corrupting the job-final counters (totals
                # would show succeeded+failed < total — misleading UI).
                try:
                    ok = await self._run_task(task_id, firmware, creds, apply_now, on_task_update)
                except Exception as exc:
                    log.exception("Task %s crashed in _run_task: %s", task_id, exc)
                    self._jobs.update_task(
                        task_id, state="failed", progress=0,
                        message=f"Internal error: {type(exc).__name__}: {exc}"[:300],
                    )
                    if on_task_update:
                        on_task_update(task_id, "failed", 0, f"Internal error: {exc}"[:200])
                    ok = False
                if ok:
                    succeeded += 1
                else:
                    failed += 1

        try:
            # return_exceptions=True so gather() never propagates a stray
            # exception that escaped run_one's try/except (defense in depth).
            await asyncio.gather(*(run_one(tid) for tid in task_ids),
                                 return_exceptions=True)
            # Snapshot the local cancel-event we captured at the top of
            # _run_job — not self._cancel_event, which a concurrent
            # start_job may have already reassigned to a fresh event for
            # the next run. The local ref is stable for the lifetime of
            # this job's coroutine.
            final_state = self._compute_final_state(
                cancelled=job_cancel_evt.is_set(),
                succeeded=succeeded,
                failed=failed,
            )
            self._jobs.update_job_state(
                job_id, final_state, succeeded=succeeded, failed=failed,
            )
            level = "success" if failed == 0 else ("error" if succeeded == 0 else "warning")
            self._activity.log(
                level, "update",
                f"Update job #{job_id} finished — {succeeded} succeeded, {failed} failed",
                job_id=job_id,
            )
            if on_job_done:
                on_job_done({
                    "job_id": job_id,
                    "succeeded": succeeded,
                    "failed": failed,
                    "state": final_state,
                })
        except Exception as exc:
            log.exception("Job %s crashed", job_id)
            self._jobs.update_job_state(job_id, "failed")
            if on_error:
                on_error(str(exc))
        finally:
            if self._cancel_event is job_cancel_evt:
                self._cancel_event = None
            self._current_job_id = None

    def _mark_cancelled(self, task_id, on_task_update):
        self._jobs.update_task(task_id, state="failed", message="Cancelled")
        if on_task_update:
            on_task_update(task_id, "failed", 0, "Cancelled")

    async def _run_task(self, task_id, firmware, creds, apply_now, on_task_update):
        def progress(state: str, pct: int, message: str = "") -> None:
            self._jobs.update_task(task_id, state=state, progress=pct, message=message)
            if on_task_update:
                on_task_update(task_id, state, pct, message)

        try:
            version_after = await self._real_task(task_id, firmware, creds, apply_now, progress)
            progress("completed", 100, "")
            self._jobs.update_task(task_id, state="completed", version_after=version_after)
            # bump server's stored version + status
            task = self._jobs.get_task(task_id)
            if task:
                self._refresh_server(task["server_id"], firmware["type"], version_after)
            return True
        except asyncio.CancelledError:
            progress("failed", 0, "Cancelled")
            return False
        except Exception as exc:
            log.warning("Task %s failed: %s", task_id, exc)
            progress("failed", 0, str(exc))
            return False

    async def _real_task(self, task_id, firmware, creds, apply_now, progress) -> str | None:
        task = self._jobs.get_task(task_id)
        if not task:
            raise RuntimeError("Task vanished from DB")
        server = self._servers.get(task["server_id"])
        if not server:
            raise RuntimeError("Server not found")
        if not server.get("redfish_root"):
            raise RuntimeError("Server has no Redfish endpoint (re-run discovery)")

        # Pre-flight: re-verify firmware SHA-256 so a file altered or moved
        # since import is caught BEFORE we send it to the BMC.
        ok, msg = await asyncio.to_thread(self._verify_firmware_sha, firmware)
        if not ok:
            raise RuntimeError(f"Firmware integrity check failed: {msg}")

        probe = RedfishProbe(
            ip=server["ip"],
            manufacturer=server.get("manufacturer"),
            redfish_root=server["redfish_root"],
        )
        # MANUAL-ONLY routing: the operator must have picked the OEM in the UI.
        oem_override = (server.get("oem_override") or "").strip() or None
        if not oem_override:
            raise RuntimeError(
                "No OEM selected for this server — open the server panel and "
                "choose the vendor before flashing"
            )
        plugin = self._plugins.resolve(probe, override_key=oem_override)
        if plugin is None:
            raise RuntimeError(
                f"Selected OEM '{oem_override}' has no installed plugin — "
                "re-select the vendor in the server panel"
            )

        info = ServerInfo(
            ip=server["ip"],
            hostname=server.get("hostname"),
            manufacturer=server.get("manufacturer"),
            oem=oem_override,
            model=server.get("model"),
            serial=server.get("serial"),
            bmc_version=server.get("bmc_version"),
            bios_version=server.get("bios_version"),
            redfish_root=server.get("redfish_root"),
            tls_fingerprint=server.get("tls_fpr"),   # enforces pin in RedfishClient
        )
        fw = Firmware(
            id=firmware["id"],
            name=firmware["name"],
            type=firmware["type"],
            oem=firmware["oem"],
            version=firmware["version"],
            file_path=firmware["file_path"],
            sha256=firmware["sha256"],
            model=firmware.get("model"),
            size_bytes=firmware.get("size_bytes") or 0,
        )

        progress("precheck", 5)
        compat = await plugin.validate(info, fw)
        if not compat.compatible:
            raise RuntimeError(f"Pre-flight failed: {compat.reason}")

        flash_creds = self._resolve_creds(creds, info)

        # Automatic BMC pre-flight — read-only probe of UpdateService +
        # FirmwareInventory so we catch auth failures, dead endpoints, and
        # blocking active tasks BEFORE pushing 30+ MB of firmware bytes.
        # Returns an actionable error message instead of a generic HTTP 4xx
        # buried inside a partially-uploaded multipart request.
        ok, msg = await self._preflight_bmc(info, fw, flash_creds)
        if not ok:
            raise RuntimeError(f"BMC pre-flight failed: {msg}")
        progress("precheck", 8, msg)

        if fw.type == "BMC":
            result = await plugin.update_bmc(info, fw, progress, flash_creds, apply_now)
        elif fw.type == "BIOS":
            result = await plugin.update_bios(info, fw, progress, flash_creds, apply_now)
        else:
            result = await plugin.update_hgx(info, fw, progress, flash_creds, apply_now)

        if not result.ok:
            raise RuntimeError(result.message or "Update failed")
        return result.version_after

    async def _preflight_bmc(
        self, server: ServerInfo, firmware: Firmware, creds: Credentials,
    ) -> tuple[bool, str]:
        """Quick read-only BMC check before the actual flash.

        Confirms three things that otherwise produce confusing failures
        deep inside the multipart upload:

        1. **Auth + endpoint**: ``GET /UpdateService`` returns 200 — proves
           the credential resolves to a working session and the service is
           enabled (vs. a generic 401 or 503 mid-upload).
        2. **Target exists**: the firmware-inventory member we'd target is
           actually present and ``Updateable=True``. Catches typos in
           rebadged OEM IDs (e.g. ``UEFI`` vs ``BIOS``) early.
        3. **Not blocked**: no other firmware task is currently running on
           this BMC — pushing concurrent uploads gets you HTTP 409 or
           silent corruption on AMI MegaRAC.

        Returns ``(True, oneliner)`` on success; ``(False, reason)`` on
        failure. The oneliner is shown on the live progress row so the
        operator can confirm what the BMC reported.
        """
        from core.plugins_api.redfish_client import RedfishClient, RedfishError
        host, port, scheme = self._parse_redfish_root(server.redfish_root)
        # Pre-assign so the finally block doesn't NameError if the
        # RedfishClient ctor raises synchronously (e.g. bad TLS pin hex).
        client = None
        try:
            client = RedfishClient(
                host, port=port, scheme=scheme,
                username=creds.username, password=creds.password,
                timeout=10.0, tls_fingerprint=server.tls_fingerprint,
            )
            try:
                us = await client.get_json("/redfish/v1/UpdateService")
            except RedfishError as exc:
                # Most useful failure mode — surface the BMC's actual reply
                return False, f"{server.ip} {exc}"
            if not us.get("ServiceEnabled", True):
                return False, "UpdateService is disabled on the BMC"

            push_uri = us.get("MultipartHttpPushUri") or us.get("HttpPushUri")
            if not push_uri:
                return False, "BMC advertises no upload URI (no MultipartHttpPushUri / HttpPushUri)"

            # Active-task check — concurrent flashes commonly corrupt AMI
            # MegaRAC. Skip silently if the BMC doesn't expose TaskService.
            try:
                tasks = await client.get_json("/redfish/v1/TaskService/Tasks")
                running = [
                    m for m in (tasks.get("Members") or [])
                    if "update" in (m.get("@odata.id") or "").lower()
                ]
                if running:
                    return False, f"BMC is busy ({len(running)} active task(s)) — wait or cancel them first"
            except RedfishError:
                pass

            # Target availability — only check if the plugin uses
            # FirmwareInventory targets (Dell uses []; skip there).
            # Excludes Backup/Golden/Staging/Image2 banks: those are
            # rollback slots; flashing them destroys the recovery path.
            target_label = firmware.type
            BANNED = ("Backup", "Golden", "Staging", "Image2", "Image_2")
            try:
                fi = await client.get_json("/redfish/v1/UpdateService/FirmwareInventory")
                ids = [m["@odata.id"].rsplit("/", 1)[-1] for m in (fi.get("Members") or [])]
                if firmware.type == "BIOS":
                    found = any(("BIOS" in i or "UEFI" in i) and
                                not any(b in i for b in BANNED) for i in ids)
                elif firmware.type == "BMC":
                    found = any(any(k in i for k in ("BMC", "iDRAC", "iLO", "XCC", "Manager"))
                                and not any(b in i for b in BANNED) for i in ids)
                else:
                    found = True
                if not found:
                    return False, f"No {firmware.type} target in FirmwareInventory (saw: {ids})"
                target_label = next(
                    (i for i in ids if firmware.type in i.upper()
                     and not any(b in i for b in BANNED)),
                    firmware.type,
                )
            except RedfishError as exc:
                # CRITICAL: distinguish 404 (legitimate Dell-style empty
                # FirmwareInventory — the plugin's _update_targets returns
                # [] and the BMC routes by DUP header) from network
                # errors (TLS reset, connection refused, timeout). The
                # latter must NOT be silently swallowed — that's how a
                # transient probe failure used to let a flash proceed
                # against a BANNED bank (RED097). Reject on anything
                # that isn't a clean 404.
                err_text = str(exc)
                is_404 = "404" in err_text or "Not Found" in err_text
                if not is_404:
                    return False, (
                        f"FirmwareInventory probe failed ({exc}). Refusing "
                        "to proceed — transient probe failure could mask a "
                        "missing-target condition and corrupt a rollback bank."
                    )
                # Genuine 404 — Dell-style empty-target flow. Let the plugin handle.
                log.debug("%s FirmwareInventory 404 — assuming Dell-style empty Targets flow",
                          server.ip)

            return True, (f"BMC ready — push={push_uri} target={target_label} "
                          f"vendor={us.get('Oem', {})}").replace("  ", " ")
        finally:
            if client is not None:
                await client.close()

    @staticmethod
    def _parse_redfish_root(root: str | None) -> tuple[str, int, str]:
        """Split a ``https://host:port/redfish/v1/`` URL back into parts."""
        if not root:
            return ("", 443, "https")
        s = root.replace("/redfish/v1/", "").rstrip("/")
        scheme, _, hostport = s.partition("://")
        host, _, port_s = hostport.partition(":")
        port = int(port_s) if port_s else (443 if scheme == "https" else 80)
        return (host, port, scheme or "https")

    def _resolve_creds(self, ui_creds: Credentials, server: ServerInfo) -> Credentials:
        """Pick the BMC creds to flash with.

        Precedence: UI-supplied → vault host scope → vault OEM scope →
        vault default → empty. If nothing's stored and the UI didn't pass
        anything, we still hand the plugin an empty Credentials so the
        plugin / RedfishClient can produce a clean 401-with-context error
        message instead of crashing.
        """
        if ui_creds and ui_creds.username:
            return ui_creds
        if self._credentials is not None:
            saved = self._credentials.resolve(ip=server.ip, oem=server.oem)
            if saved is not None:
                return saved
        return Credentials(username="", password="")

    def _refresh_server(self, server_id: int, fw_type: str, version_after: str | None) -> None:
        if not version_after:
            return
        server = self._servers.get(server_id)
        if not server:
            return
        upserted = dict(server)
        if fw_type == "BMC":
            upserted["bmc_version"] = version_after
        elif fw_type == "BIOS":
            upserted["bios_version"] = version_after
        upserted["status"] = "online"
        self._servers.upsert(upserted)
