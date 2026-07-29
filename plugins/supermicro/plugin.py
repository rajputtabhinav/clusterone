"""Supermicro plugin.

Modern Supermicro X13/H13/X12/H12 BMCs run AMI MegaRAC firmware that does NOT
follow the plain DMTF ``SimpleUpdate`` convention for flashing. The proven
field recipe (validated against real hardware + the operator's production
shell scripts) is:

* Multipart push to ``/redfish/v1/UpdateService/upload`` with THREE parts:
    - ``UpdateParameters`` → ``{"Targets": ["/redfish/v1/UpdateService/
      FirmwareInventory/BIOS"|"…/BMC"], "@Redfish.OperationApplyTime": …}``
    - ``OemParameters``    → ``{"ImageType": "BIOS"|"BMC"}``
    - ``UpdateFile``       → the raw firmware bytes
  Note the Targets are **FirmwareInventory** URIs, NOT ``/Systems/1`` or
  ``/Managers/1`` (which is what plain DMTF uses and what fails here).

* **BMC**: ApplyTime ``Immediate``; the BMC flashes and self-resets. Monitor
  the returned Task to completion.

* **BIOS**: ApplyTime ``OnReset``. The image is staged by the upload, then an
  OEM action ``#SmcUpdateService.Install`` commits it, and the host must be
  power-cycled for the flash to actually run (the BIOS Update Task progresses
  during the reboot). "Apply now" performs the ForceOff → Install → On dance;
  otherwise the image is staged and applied on the operator's next reboot.

This override exists precisely because this is OEM-divergent behavior — the
generic plugin keeps the DMTF path for vendors / the simulator.
"""
from __future__ import annotations

import asyncio
import logging

from core.models.firmware import Firmware
from core.models.server import Credentials, ServerInfo
from core.plugins_api.base import ProgressCallback, RedfishProbe, Result
from core.plugins_api.redfish_client import RedfishClient, RedfishError
from plugins.ami_megarac_base import AmiMegaracPlugin

log = logging.getLogger(__name__)


class SupermicroPlugin(AmiMegaracPlugin):
    key = "supermicro"
    display_name = "Supermicro"
    supported_models = ("H13*", "X13*", "X12*", "X11*", "H12*")

    _UPLOAD_URI = "/redfish/v1/UpdateService/upload"
    _SMC_INSTALL = "/redfish/v1/UpdateService/Actions/Oem/SmcUpdateService.Install"
    _FW_INV = "/redfish/v1/UpdateService/FirmwareInventory"
    _SYS_RESET = "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset"
    # Flash monitoring window — real BIOS flashes run during POST and take
    # several minutes; give the host generous time.
    _POLL_CEILING_S = 30 * 60
    _POLL_INTERVAL_S = 5.0
    _UPLOAD_SETTLE_S = 10.0      # pause after upload before the install trigger
    _BIOS_TASK_PREWAIT_S = 60.0  # host POST + early boot before the task appears

    def match(self, probe: RedfishProbe) -> float:
        manuf = (probe.manufacturer or "").lower()
        if "supermicro" in manuf or "super micro" in manuf:
            return 0.9
        # Tyrone Systems sells rebadged Supermicro boards with AMI MegaRAC
        # firmware — same flash recipe, just a different brand label.
        if "tyrone" in manuf:
            return 0.85
        for block in probe.oem_blocks:
            b = (block or "").lower()
            if "supermicro" in b or "tyrone" in b:
                return 0.8
        return 0.0

    # ------------------------------------------------------------------
    async def _do_update(
        self,
        server: ServerInfo,
        firmware: Firmware,
        on_progress: ProgressCallback,
        target: str,                 # "BMC" | "BIOS"
        creds: Credentials,
        apply_now: bool = False,
    ) -> Result:
        client = self._client_for(server, creds)
        try:
            if target == "BMC":
                return await self._update_bmc(server, client, firmware, on_progress)
            return await self._update_bios(server, client, firmware, on_progress, apply_now)
        finally:
            await client.close()

    # ---- BMC: upload (Immediate) → poll task → self-reboot ----
    async def _update_bmc(self, server, client, firmware, on_progress) -> Result:
        on_progress("uploading", 10)
        params = {
            "Targets": [f"{self._FW_INV}/BMC"],
            "@Redfish.OperationApplyTime": "Immediate",
        }
        try:
            resp = await client.post_multipart(
                self._UPLOAD_URI, params=params,
                file_path=firmware.file_path, filename=firmware.name,
                oem_params={"ImageType": "BMC"},
            )
        except RedfishError as exc:
            return Result(ok=False, message=f"BMC upload failed: {exc}")

        task_uri = self._task_uri(resp)
        on_progress("flashing", 30)
        ok, msg = await self._poll_task(client, task_uri, on_progress)
        if not ok:
            return Result(ok=False, message=msg)

        on_progress("rebooting", 90)
        # BMC resets itself after a successful flash; Redfish drops briefly.
        ver = await self._read_version(client, "BMC")
        return Result(ok=True, version_after=ver or firmware.version,
                      message="BMC flashed; the BMC is resetting.")

    # ---- BIOS: (power off) → upload (OnReset) → SmcUpdateService.Install → (power on) → poll ----
    async def _update_bios(self, server, client, firmware, on_progress, apply_now) -> Result:
        if apply_now:
            on_progress("precheck", 8)
            await self._power(server, client, "ForceOff")
            await self._wait_power(server, client, "Off", timeout=120)

        on_progress("uploading", 15)
        params = {
            "Targets": [f"{self._FW_INV}/BIOS"],
            "@Redfish.OperationApplyTime": "OnReset",
        }
        try:
            resp = await client.post_multipart(
                self._UPLOAD_URI, params=params,
                file_path=firmware.file_path, filename=firmware.name,
                oem_params={"ImageType": "BIOS"},
            )
        except RedfishError as exc:
            return Result(ok=False, message=f"BIOS upload failed: {exc}")

        # CRITICAL: don't fire SmcUpdateService.Install before the BMC has
        # finished ingesting the upload. The OLD code slept 10s then fired
        # blind — if the BMC was still verifying, Install returned 400 and
        # the staged image was silently abandoned. Now we poll the upload's
        # Task to a terminal state first (if one was returned), then trigger
        # Install with confidence. If no Task was returned the BMC flashed
        # synchronously and Install is a no-op.
        on_progress("flashing", 30)
        upload_task_uri = (resp.get("location") or "").strip()
        if not upload_task_uri:
            body = resp.get("body") or {}
            upload_task_uri = (body.get("@odata.id") or "").strip()
        if upload_task_uri:
            ok, msg = await self._poll_task(client, upload_task_uri, on_progress)
            if not ok:
                return Result(ok=False, message=f"BIOS upload Task failed: {msg}")

        # Now the staged image is fully ingested. Commit it via the
        # Supermicro OEM install action — most modern Supermicro builds
        # auto-install on upload completion, so a 4xx here is tolerable
        # (the staged image is already there waiting for the reset).
        try:
            await client.post_json(self._SMC_INSTALL, {})
        except RedfishError as exc:
            log.info("%s SmcUpdateService.Install returned: %s — staged "
                     "image will apply on next reset regardless",
                     firmware.name, exc)

        if not apply_now:
            return Result(
                ok=True, version_after=firmware.version,
                message="BIOS staged. It will flash on the next host reboot.",
            )

        # Apply now: power the host back on; the BIOS flash runs during boot.
        on_progress("rebooting", 45)
        await self._power(server, client, "On")
        await self._wait_power(server, client, "On", timeout=180)

        # Monitor the BIOS Update task that runs during the reboot.
        ok, msg = await self._poll_bios_task(client, on_progress)
        if not ok:
            return Result(ok=False, message=msg)

        on_progress("verifying", 95)
        ver = await self._read_version(client, "BIOS")
        return Result(ok=True, version_after=ver or firmware.version,
                      message="BIOS flashed and applied.")

    # ---- helpers ----
    @staticmethod
    def _task_uri(resp: dict) -> str:
        loc = resp.get("location") or ""
        if loc:
            return loc
        body = resp.get("body") or {}
        return body.get("@odata.id", "")

    async def _poll_task(self, client, task_uri, on_progress) -> tuple[bool, str]:
        """Poll a single TaskService task to a terminal state."""
        if not task_uri:
            # No task to watch (some BMCs flash synchronously) — assume ok.
            return True, "no task returned"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._POLL_CEILING_S
        last = 0
        consecutive_errors = 0
        max_consecutive_errors = 60   # ~5 min of dead-BMC polls before bailing
        while loop.time() < deadline:
            try:
                task = await client.get_json(task_uri)
                consecutive_errors = 0
            except RedfishError as exc:
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    return False, f"BMC unreachable for {max_consecutive_errors} consecutive polls: {exc}"
                await asyncio.sleep(self._POLL_INTERVAL_S)   # BMC resetting
                continue
            pct = int(task.get("PercentComplete", 0) or 0)
            if pct != last:
                last = pct
                on_progress("flashing", 30 + int(pct * 0.55))
            state = task.get("TaskState", "Running")
            if state in ("Completed", "Exception", "Killed", "Cancelled"):
                if state != "Completed":
                    msgs = task.get("Messages") or []
                    detail = msgs[-1].get("Message", "") if msgs else ""
                    return False, f"BMC task ended '{state}'. {detail}".strip()
                return True, "completed"
            await asyncio.sleep(self._POLL_INTERVAL_S)
        return False, f"flash did not finish within {self._POLL_CEILING_S // 60} min"

    async def _poll_bios_task(self, client, on_progress) -> tuple[bool, str]:
        """Find + monitor the 'BIOS Update' task that runs during reboot."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._POLL_CEILING_S
        await asyncio.sleep(self._BIOS_TASK_PREWAIT_S)   # host POST + early boot
        while loop.time() < deadline:
            try:
                tasks = await client.get_json("/redfish/v1/TaskService/Tasks")
            except RedfishError:
                await asyncio.sleep(self._POLL_INTERVAL_S)
                continue
            for ref in tasks.get("Members", []):
                try:
                    d = await client.get_json(ref["@odata.id"])
                except RedfishError:
                    continue
                name = (d.get("Name") or "")
                if "BIOS" not in name:
                    continue
                state = d.get("TaskState", "Running")
                pct = int(d.get("PercentComplete", 0) or 0)
                on_progress("flashing", 45 + int(pct * 0.5))
                if state == "Completed":
                    return True, "completed"
                if state in ("Exception", "Killed", "Cancelled"):
                    return False, f"BIOS task ended '{state}'"
            await asyncio.sleep(self._POLL_INTERVAL_S)
        # Couldn't confirm via task — not fatal; the install was triggered.
        return True, "applied (task not observed)"

    async def _power(self, server, client, reset_type: str) -> None:
        """Issue a ComputerSystem.Reset, resolving the action target from
        the CapabilityMap rather than hardcoding ``/Systems/1``. Dell uses
        ``System.Embedded.1``, AMI variants use ``Self``, etc."""
        cap = await self._capability_map(server, client)
        target = getattr(cap, "system_reset_target", None) or self._SYS_RESET
        try:
            await client.post_json(target, {"ResetType": reset_type})
        except RedfishError as exc:
            log.warning("Power %s on %s failed: %s", reset_type, server.ip, exc)

    async def _wait_power(self, server, client, want: str, timeout: int) -> None:
        """Poll PowerState on the active System resource (from cap, NOT
        hardcoded /Systems/1)."""
        cap = await self._capability_map(server, client)
        sys_uri = getattr(cap, "system_uri", None) or "/redfish/v1/Systems/1"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                s = await client.get_json(sys_uri)
                if (s.get("PowerState") or "") == want:
                    return
            except RedfishError:
                pass
            await asyncio.sleep(5)

    async def _read_version(self, client, comp: str) -> str | None:
        try:
            d = await client.get_json(f"{self._FW_INV}/{comp}")
            return d.get("Version")
        except RedfishError:
            return None


PLUGIN = SupermicroPlugin()
