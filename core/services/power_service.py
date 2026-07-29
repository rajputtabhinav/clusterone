"""Power-control service.

Issues Redfish power actions (on / off / force_off / restart / cycle) across
one or more servers on the async engine, reads the resulting ``PowerState``
back, persists it on the server row, and reports per-server results via
callbacks. The callbacks fire on the engine thread — the bridge controller
marshals them to the Qt GUI thread.

Credential precedence mirrors the update path: inline creds (if the UI passed
any) win, else the vault-resolved credential for the host/OEM scope.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from core.engine.async_engine import AsyncEngine
from core.models.server import Credentials, ServerInfo
from core.plugins_api.base import RedfishProbe
from core.plugins_api.registry import PluginRegistry
from core.services.activity_service import ActivityService
from core.services.credentials_service import CredentialsService
from data.repositories.servers_repo import ServersRepo

log = logging.getLogger(__name__)

# Logical action -> human label for the audit log / toast.
ACTION_LABELS = {
    "on": "Power On",
    "off": "Shut Down",
    "force_off": "Force Off",
    "restart": "Restart",
    "graceful_restart": "Graceful Restart",
    "cycle": "Power Cycle",
}
# Actions whose intent is "host ends Off" — used to bias the state read-back.
_OFF_ACTIONS = {"off", "force_off"}


class PowerService:
    def __init__(
        self,
        engine: AsyncEngine,
        plugins: PluginRegistry,
        servers_repo: ServersRepo,
        activity: ActivityService,
        credentials: Optional[CredentialsService] = None,
    ) -> None:
        self._engine = engine
        self._plugins = plugins
        self._servers = servers_repo
        self._activity = activity
        self._credentials = credentials

    def apply(
        self,
        server_ids: list[int],
        action: str,
        *,
        username: str = "",
        password: str = "",
        concurrency: int = 8,
        on_result: Callable[[int, str, bool, str, str], None] | None = None,
        on_done: Callable[[dict], None] | None = None,
    ):
        """Schedule ``action`` against every server id on the async engine.

        Returns the engine ``Future`` (or ``None`` if there's nothing to do /
        the action is unknown). ``on_result(server_id, action, ok, message,
        power_state)`` fires once per server; ``on_done(summary)`` fires once.
        """
        ids = [int(s) for s in (server_ids or [])]
        if not ids:
            return None
        if action not in ACTION_LABELS:
            log.warning("PowerService: unknown action %r", action)
            return None
        return self._engine.submit(
            self._run(ids, action, username, password,
                      max(1, min(32, int(concurrency))), on_result, on_done)
        )

    # ---- internal -----------------------------------------------------------

    async def _run(self, ids, action, username, password, concurrency,
                   on_result, on_done):
        sem = asyncio.Semaphore(concurrency)
        ok_count = 0
        fail_count = 0

        async def one(sid: int) -> None:
            nonlocal ok_count, fail_count
            try:
                async with sem:
                    ok, msg, state = await self._apply_one(sid, action, username, password)
            except Exception as exc:  # defense-in-depth: never let gather tear down
                log.exception("power %s on server %s crashed", action, sid)
                ok, msg, state = False, f"{type(exc).__name__}: {exc}", None
            if ok:
                ok_count += 1
            else:
                fail_count += 1
            if on_result:
                on_result(sid, action, ok, msg, state or "")

        await asyncio.gather(*(one(s) for s in ids), return_exceptions=True)
        if on_done:
            on_done({"action": action, "ok": ok_count,
                     "failed": fail_count, "total": len(ids)})

    async def _apply_one(self, sid, action, username, password):
        row = self._servers.get(sid)
        if not row:
            return False, "server not found", None
        if not row.get("redfish_root"):
            return False, "no Redfish endpoint (run discovery first)", None
        info = _info_from_row(row)
        oem_override = (row.get("oem_override") or "").strip() or None
        if not oem_override:
            return False, ("no OEM selected — open the server panel and "
                           "choose the vendor first"), None
        plugin = self._plugins.resolve(
            RedfishProbe(
                ip=info.ip, manufacturer=info.manufacturer,
                redfish_root=info.redfish_root,
            ),
            override_key=oem_override,
        )
        if plugin is None:
            return False, f"selected OEM '{oem_override}' has no installed plugin", None
        creds = self._resolve_creds(info, username, password)

        result = await plugin.power_action(info, action, creds)
        if not result.ok:
            self._activity.log(
                "error", "power",
                f"{ACTION_LABELS[action]} failed on {info.ip}: {result.message}",
                server_id=sid,
            )
            return False, result.message, None

        state = await self._verify_state(plugin, info, creds, action)
        if state:
            try:
                self._servers.upsert({**row, "power_state": state})
            except Exception:
                log.exception("persisting power_state for %s failed", info.ip)
        self._activity.log(
            "info", "power",
            f"{ACTION_LABELS[action]} on {info.ip} — {result.message}"
            + (f" (PowerState={state})" if state else ""),
            server_id=sid,
        )
        return True, result.message, state

    async def _verify_state(self, plugin, info, creds, action) -> str | None:
        """Best-effort short poll of PowerState so the UI reflects the change.

        Returns as soon as the expected state is observed (``On`` for ``on``,
        ``Off`` for off-type actions), otherwise the most recent reading after
        a few tries. A graceful ``off`` that the host ignores (no OS to honor
        ACPI) will legitimately keep reporting ``On`` — the operator then uses
        the dedicated Force Off action.
        """
        want = "Off" if action in _OFF_ACTIONS else ("On" if action == "on" else None)
        last = None
        for i in range(4):
            last = await plugin.get_power_state(info, creds)
            if last and (want is None or last == want):
                return last
            if i < 3:
                await asyncio.sleep(2.0)
        return last

    def _resolve_creds(self, info: ServerInfo, username: str, password: str) -> Credentials:
        if username:
            return Credentials(username=username, password=password)
        if self._credentials is not None:
            saved = self._credentials.resolve(ip=info.ip, oem=info.oem)
            if saved is not None:
                return saved
        return Credentials(username="", password="")


def _info_from_row(row: dict) -> ServerInfo:
    return ServerInfo(
        ip=row["ip"],
        hostname=row.get("hostname"),
        manufacturer=row.get("manufacturer"),
        oem=(row.get("oem_override") or "").strip() or row.get("oem"),
        model=row.get("model"),
        serial=row.get("serial"),
        bmc_version=row.get("bmc_version"),
        bios_version=row.get("bios_version"),
        redfish_root=row.get("redfish_root"),
        tls_fingerprint=row.get("tls_fpr"),
    )
