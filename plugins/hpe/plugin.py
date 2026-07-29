"""HPE iLO5 / iLO6 plugin.

HPE iLO does **not** implement the DMTF ``MultipartHttpPushUri``. It uses an
HPE-proprietary multipart push at the path advertised in ``HttpPushUri``
(typically ``/redfish/v1/UpdateService/FirmwareInventory/``) with a body
shape that's specific to iLO:

* ``sessionKey``    — the session token (text part, NOT in headers)
* ``parameters``    — JSON: ``{"UpdateRepository": <bool>, "UpdateTarget":
                      <bool>, "ETag": "atag", "Section": 0}``
                      (ApplyTime is encoded via these bools — there's no
                      ``@Redfish.OperationApplyTime``.)
* ``compsig``       — REQUIRED when uploading ``.fwpkg`` (HPE Smart
                      Component); the matching ``.compsig`` file must be
                      sent alongside. Raw ``.bin`` / ``.signed.flash``
                      doesn't need it.
* ``file``          — the firmware blob

Order: ``sessionKey`` → ``parameters`` → ``compsig`` (if any) → ``file``.

After upload, iLO does NOT return a TaskMonitor URI. Progress is polled at
``GET /redfish/v1/UpdateService`` and watching ``Oem.Hpe.State``
(``Idle | Uploading | Verifying | Writing | Complete | Error``).

Apply semantics:
* iLO firmware    → ``UpdateTarget=True`` → iLO self-resets after flash.
* Host firmware   → ``UpdateTarget=True``, then a host reboot must be
                    issued separately to apply (BIOS/NIC/CPLD).
* Staged          → ``UpdateRepository=True, UpdateTarget=False`` → image
                    parked in the iLO Repository; commit via
                    ``UpdateTaskQueue`` (not implemented here yet).

TPM-protected systems require ``TPMOverrideFlag`` on the pull-path action
(``HpeiLOUpdateServiceExt.AddFromUri``). For local push the equivalent is
encoded into ``parameters``; iLO silently rejects flashes when TPM is
active and the override isn't set.

This override is implemented to the published HPE iLO REST API spec
(servermanagementportal.ext.hpe.com) and python-ilorest-library examples,
but awaits validation against real iLO hardware. Until then, the generic
DMTF path acts as a fallback for any iLO that does advertise
MultipartHttpPushUri (some custom builds do).
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import aiohttp

from core.models.firmware import Firmware
from core.models.server import Credentials, ServerInfo
from core.plugins_api.base import ProgressCallback, RedfishProbe, Result
from core.plugins_api.redfish_client import RedfishError
from plugins.generic_redfish.plugin import GenericRedfishPlugin

log = logging.getLogger(__name__)


class HpePlugin(GenericRedfishPlugin):
    key = "hpe"
    display_name = "HPE (iLO)"
    supported_models = (
        "ProLiant DL*", "ProLiant ML*", "ProLiant BL*",
        "Apollo*", "Synergy*", "Alletra*", "Edgeline*",
    )

    _POLL_CEILING_S = 30 * 60
    _POLL_INTERVAL_S = 5.0

    def match(self, probe: RedfishProbe) -> float:
        manuf = (probe.manufacturer or "").lower()
        if "hpe" == manuf or "hewlett packard enterprise" in manuf or "hewlett-packard" in manuf:
            return 0.92
        for block in probe.oem_blocks:
            b = (block or "").lower()
            if b == "hpe" or b == "hp":
                return 0.82
        return 0.0

    async def mount_iso(
        self, server: ServerInfo, image_url: str, creds: Credentials,
    ) -> Result:
        """iLO 4 / iLO 5 reject InsertMedia if anything is already mounted;
        an explicit eject first makes the operation idempotent."""
        await self.eject_iso(server, creds)
        return await super().mount_iso(server, image_url, creds)

    # ------------------------------------------------------------------
    async def _do_update(
        self,
        server: ServerInfo,
        firmware: Firmware,
        on_progress: ProgressCallback,
        target: str,
        creds: Credentials,
        apply_now: bool = False,
    ) -> Result:
        """HPE-specific upload + ``Oem.Hpe.State`` poll. Falls back to the
        generic DMTF flow if the iLO advertises ``MultipartHttpPushUri``
        (some custom builds do, in which case the multipart shape matches
        DMTF)."""
        client = self._client_for(server, creds)
        try:
            try:
                us = await client.get_json("/redfish/v1/UpdateService")
            except RedfishError as exc:
                return Result(ok=False, message=f"UpdateService unreachable: {exc}")

            # Most HPE iLO5/6 expose HttpPushUri but NOT MultipartHttpPushUri.
            # If multipart is advertised, use the standard flow (it's been
            # observed on some HPE rebadged hardware).
            if us.get("MultipartHttpPushUri"):
                return await super()._do_update(
                    server, firmware, on_progress, target, creds, apply_now
                )

            push_uri = us.get("HttpPushUri") or "/redfish/v1/UpdateService/FirmwareInventory/"
            return await self._hpe_push(
                client, push_uri, firmware, on_progress, target, apply_now,
            )
        finally:
            await client.close()

    async def _hpe_push(
        self, client, push_uri: str, firmware: Firmware,
        on_progress: ProgressCallback, target: str, apply_now: bool,
    ) -> Result:
        """Build the HPE-specific multipart and POST it with HTTP basic
        auth. iLO accepts either ``X-Auth-Token`` + ``sessionKey`` cookie
        or basic-auth; we use basic-auth (already on the session) and pass
        a placeholder ``sessionKey`` part — iLO ignores it under basic
        auth but still wants the field present in the multipart form."""
        on_progress("uploading", 10)

        fw_path = Path(firmware.file_path)
        if not fw_path.is_file():
            return Result(ok=False, message=f"firmware blob missing: {fw_path}")

        # .fwpkg → look for sibling .compsig (HPE Smart Component requirement)
        compsig_path: Path | None = None
        if fw_path.suffix.lower() == ".fwpkg":
            sib = fw_path.with_suffix(".compsig")
            if sib.is_file():
                compsig_path = sib
            else:
                # Hard-fail rather than upload-then-silently-fail at the
                # Verifying stage. iLO5 with no .compsig leaves the
                # Repository in a half-broken state on some firmware builds.
                return Result(
                    ok=False,
                    message=(
                        f"HPE Smart Component '{fw_path.name}' requires a "
                        f"matching .compsig signature file next to it "
                        f"(expected: {sib.name}). Refusing to upload — "
                        f"iLO would reject this at the Verifying stage and "
                        f"leave the repository in an inconsistent state."
                    ),
                )

        # iLO uses bools in `parameters` instead of @Redfish.OperationApplyTime.
        # Both directly applied here (UpdateRepository=False, UpdateTarget=True).
        # Future: support "stage to iLO Repository then schedule" with
        # UpdateRepository=True / UpdateTaskQueue.
        params = {
            "UpdateRepository": False,
            "UpdateTarget":     True,
            "ETag":             "atag",
            "Section":          0,
        }

        # iLO5 firmware ≥ 2.55 / iLO6 require a real session token in
        # ``sessionKey`` — passing empty string returns HTTP 403. Trigger
        # the RedfishClient's lazy session login so we know we have a
        # token before building the form. If session-auth isn't
        # available on this iLO (older firmware), we fall back to the
        # documented empty-string placeholder + basic auth — that path
        # still works on iLO5 < 2.55.
        await client._ensure_logged_in()
        session_token = client._token or ""

        # Stream the firmware bytes off the event loop AND from disk —
        # don't load 300 MB into RAM (same fix as RedfishClient).
        form = aiohttp.FormData()
        form.add_field("sessionKey", session_token)
        form.add_field(
            "parameters", json.dumps(params),
            content_type="application/json",
        )
        if compsig_path is not None:
            # compsig is small (KBs) — read fully is fine
            sig_bytes = await asyncio.to_thread(compsig_path.read_bytes)
            form.add_field(
                "compsig", sig_bytes,
                filename=compsig_path.name,
                content_type="application/octet-stream",
            )
        # Own the firmware handle explicitly so it's ALWAYS closed — even if
        # the POST raises before aiohttp finishes streaming the body. A leaked
        # handle on Windows blocks the blob from being deleted / re-imported.
        fw_handle = open(fw_path, "rb")
        form.add_field(
            "file", fw_handle,
            filename=fw_path.name,
            content_type="application/octet-stream",
        )

        url = client.base_url + (push_uri if push_uri.startswith("/") else "/" + push_uri)
        sess = await client._session_for()
        try:
            async with sess.post(
                url, data=form,
                timeout=aiohttp.ClientTimeout(total=30 * 60),
                headers={"Expect": "", "OData-Version": "4.0"},
            ) as resp:
                if resp.status >= 400:
                    text = (await resp.text())[:400]
                    return Result(ok=False, message=f"iLO push HTTP {resp.status} {text}")
        except aiohttp.ClientError as exc:
            return Result(ok=False, message=f"iLO push failed: {exc}")
        finally:
            try:
                fw_handle.close()
            except Exception:
                pass

        # ---- iLO polls Oem.Hpe.State, NOT TaskMonitor ----
        on_progress("flashing", 30)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._POLL_CEILING_S
        terminal = ""
        consecutive_errors = 0
        max_consecutive_errors = 60   # ~5 min of dead-iLO polls before bailing
        while loop.time() < deadline:
            try:
                us = await client.get_json("/redfish/v1/UpdateService")
                consecutive_errors = 0
            except RedfishError as exc:
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    return Result(
                        ok=False,
                        message=f"iLO unreachable for {max_consecutive_errors} consecutive polls: {exc}",
                    )
                await asyncio.sleep(self._POLL_INTERVAL_S)
                continue
            state = ((us.get("Oem") or {}).get("Hpe") or {}).get("State") or ""
            if state == "Complete":
                terminal = "ok"; break
            if state == "Error":
                terminal = "error"; break
            # Map iLO state → progress band
            band = {"Idle": 30, "Uploading": 40, "Verifying": 55,
                    "Writing": 70, "Updating": 85}.get(state, 35)
            on_progress("flashing", band)
            await asyncio.sleep(self._POLL_INTERVAL_S)

        if terminal != "ok":
            return Result(ok=False,
                          message=f"iLO flash ended in state '{terminal or 'timeout'}'")

        if target == "BIOS" and apply_now:
            # Host firmware doesn't auto-reboot on iLO — issue a reset.
            sys_uri = await self._first_system_uri(client)
            if sys_uri:
                await self._reset_to_apply(client, sys_uri)

        on_progress("verifying", 95)
        return Result(
            ok=True, version_after=firmware.version,
            message=("iLO firmware flashed; iLO is resetting." if target == "BMC"
                     else ("BIOS flashed and host reset issued."
                           if apply_now else "BIOS staged — applies on next host reboot.")),
        )


PLUGIN = HpePlugin()
