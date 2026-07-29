"""Discovery orchestrator.

Runs a scan on the async engine, routes matched hosts to the right OEM plugin,
persists results via :class:`InventoryService`, and writes audit events. The
caller's callbacks fire on the engine thread — wire them through a bridge
controller that emits Qt signals to cross to the GUI thread.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

from urllib.parse import urlparse

from core.engine.async_engine import AsyncEngine
from core.engine.scanner import parse_range, scan
from core.models.server import Credentials, ServerInfo
from core.plugins_api.base import RedfishProbe
from core.plugins_api.registry import PluginRegistry
from core.security.tls import fetch_fingerprint
from core.services.activity_service import ActivityService
from core.services.credentials_service import CredentialsService
from core.services.inventory_service import InventoryService

log = logging.getLogger(__name__)


class DiscoveryService:
    def __init__(
        self,
        engine: AsyncEngine,
        registry: PluginRegistry,
        inventory: InventoryService,
        activity: ActivityService,
        credentials: CredentialsService | None = None,
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._inventory = inventory
        self._activity = activity
        self._credentials = credentials
        self._cancel_event: asyncio.Event | None = None
        # Strong references to in-flight fire-and-forget tasks (TLS capture,
        # disk refresh callbacks).  Without this, Python's GC can collect the
        # task before it completes and emit "Task was destroyed but it is
        # pending!" on Python 3.12+.  Tasks remove themselves when done.
        self._inflight_tasks: set[asyncio.Task] = set()

    def start(
        self,
        range_spec: str,
        creds: Credentials,
        on_found: Callable[[ServerInfo], None],
        on_progress: Callable[[int, int, int], None],
        on_done: Callable[[dict], None],
        on_error: Callable[[str], None] | None = None,
    ):
        try:
            targets = parse_range(range_spec)
        except Exception as exc:
            msg = f"Invalid range: {exc}"
            if on_error: on_error(msg)
            return None
        if not targets:
            msg = "Empty range"
            if on_error: on_error(msg)
            return None
        self._activity.log(
            "info", "discovery",
            f"Discovery started ({range_spec}) — {len(targets)} targets",
        )
        return self._engine.submit(
            self._run(targets, creds, on_found, on_progress, on_done, on_error)
        )

    def cancel(self) -> None:
        if self._cancel_event is None:
            return
        self._engine.call_soon(self._cancel_event.set)

    def refresh_disks_for(
        self,
        server_id: int,
        on_done: Callable[[bool, str], None] | None = None,
        username: str = "",
        password: str = "",
    ):
        """Re-poll one server's Redfish Storage subsystem on demand.

        ``username`` / ``password`` are inline creds supplied directly by the
        Provision page.  When provided they override the vault lookup so the
        operator can fetch disk info even without a saved credential entry.
        """
        row = self._inventory_get(server_id)
        if row is None:
            if on_done: on_done(False, "Server not found")
            return None
        if not (row.get("redfish_root") or ""):
            if on_done: on_done(False, "Server has no Redfish endpoint — run discovery first")
            return None
        return self._engine.submit(
            self._refresh_disks(row, on_done,
                                inline_username=username,
                                inline_password=password)
        )

    def _inventory_get(self, server_id: int) -> dict | None:
        for r in self._inventory.list():
            if int(r.get("id") or 0) == int(server_id):
                return r
        return None

    async def _refresh_disks(self, row: dict,
                             on_done: Callable[[bool, str], None] | None,
                             inline_username: str = "",
                             inline_password: str = "") -> None:
        try:
            probe = RedfishProbe(
                ip=row["ip"],
                manufacturer=row.get("manufacturer"),
                redfish_root=row.get("redfish_root"),
            )
            oem_override = (row.get("oem_override") or "").strip() or None
            if not oem_override:
                if on_done:
                    on_done(False, "No OEM selected — pick the vendor in the server panel")
                return
            plugin = self._registry.resolve(probe, override_key=oem_override)
            if plugin is None or not hasattr(plugin, "get_storage_inventory"):
                if on_done:
                    on_done(False, f"Selected OEM '{oem_override}' has no installed plugin")
                return
            # Inline creds (from the UI prompt) take priority over vault.
            if inline_username:
                creds = Credentials(username=inline_username, password=inline_password)
            else:
                creds = Credentials(username="", password="")
                if self._credentials is not None:
                    eff_oem = (row.get("oem_override") or "").strip() or row.get("oem")
                    saved = self._credentials.resolve(ip=row["ip"], oem=eff_oem)
                    if saved is not None:
                        creds = saved
            base_url = (row.get("redfish_root") or "").replace("/redfish/v1/", "").rstrip("/")
            drives = await plugin.get_storage_inventory(base_url, creds)
            payload = json.dumps([d.to_dict() for d in drives])
            await asyncio.to_thread(self._inventory.set_disks, row["ip"], payload)
            self._activity.log(
                "info", "discovery",
                f"{row['ip']} disk inventory refreshed — {len(drives)} drives",
                server_id=row["id"],
            )
            if on_done: on_done(True, f"{len(drives)} drives")
        except Exception as exc:
            log.exception("Disk refresh failed for %s", row.get("ip"))
            if on_done: on_done(False, str(exc))

    async def _capture_tls(self, info: ServerInfo) -> None:
        """Probe + persist the BMC's TLS fingerprint (TOFU). Off-loaded from
        the event loop so the blocking socket + sqlite calls don't stall
        concurrent probes."""
        try:
            u = urlparse(info.redfish_root)
            fpr = await asyncio.to_thread(fetch_fingerprint, u.hostname, u.port or 443)
            if not fpr:
                return
            change = await asyncio.to_thread(
                self._inventory.set_tls_fingerprint, info.ip, fpr
            )
            if change and change.get("changed"):
                self._activity.log(
                    "warning", "auth",
                    f"{info.ip} TLS fingerprint CHANGED — investigate",
                    server_id=change["id"],
                )
        except Exception:
            log.exception("TLS fingerprint capture failed for %s", info.ip)

    async def _run(self, targets, creds, on_found, on_progress, on_done, on_error):
        self._cancel_event = asyncio.Event()
        try:
            def _persist_and_notify(info: ServerInfo) -> None:
                try:
                    # Detect an IP that now hosts DIFFERENT hardware (serial
                    # changed). The repo clears the stale OEM selection + disk
                    # cache atomically inside upsert; we surface it in the
                    # activity feed so the operator knows to re-select.
                    prev = self._inventory.get_by_ip(info.ip)
                    prev_serial = ((prev or {}).get("serial") or "").strip()
                    new_serial = (info.serial or "").strip()
                    hw_changed = bool(prev_serial and new_serial
                                      and prev_serial != new_serial)
                    self._inventory.upsert({
                        "ip": info.ip,
                        "hostname": info.hostname,
                        "manufacturer": info.manufacturer,
                        "oem": info.oem,
                        "model": info.model,
                        "serial": info.serial,
                        "bmc_version": info.bmc_version,
                        "bios_version": info.bios_version,
                        "power_state": info.power_state,
                        "status": info.status,
                        "protocol": info.protocol,
                        "redfish_root": info.redfish_root,
                    })
                    if hw_changed:
                        self._activity.log(
                            "warning", "discovery",
                            f"{info.ip}: different hardware detected (serial "
                            f"{prev_serial} → {new_serial}) — OEM selection and "
                            "cached disks cleared; re-select the OEM in the "
                            "server panel",
                            server_id=(prev or {}).get("id"),
                        )
                except Exception:
                    log.exception("Persisting discovered host %s failed", info.ip)
                # Cache the disk inventory (best-effort, populated by the
                # scanner via plugin.get_storage_inventory). The provisioning
                # wizard reads this without re-polling Redfish.
                if info.drives:
                    try:
                        payload = json.dumps([d.to_dict() for d in info.drives])
                        self._inventory.set_disks(info.ip, payload)
                    except Exception:
                        log.exception("Caching disk inventory for %s failed", info.ip)
                # TOFU TLS pin capture — fire-and-forget so the blocking socket
                # call doesn't stall the engine event loop.  We hold a strong
                # reference so the GC can't collect the task mid-flight.
                if info.redfish_root and info.redfish_root.startswith("https://"):
                    t = asyncio.create_task(self._capture_tls(info))
                    self._inflight_tasks.add(t)
                    t.add_done_callback(self._inflight_tasks.discard)
                try:
                    on_found(info)
                except Exception:
                    log.exception("on_found callback failed for %s", info.ip)

            stats = await scan(
                targets, creds, self._registry,
                on_found=_persist_and_notify,
                on_progress=on_progress,
                cancel_event=self._cancel_event,
            )
            self._activity.log(
                "success", "discovery",
                f"Discovery finished — {stats['found']}/{stats['scanned']} found",
            )
            on_done(stats)
        except Exception as exc:
            log.exception("Discovery run failed")
            self._activity.log("error", "discovery", f"Discovery failed: {exc}")
            if on_error:
                on_error(str(exc))
            else:
                on_done({"scanned": 0, "found": 0, "total": 0, "error": str(exc)})
        finally:
            self._cancel_event = None
