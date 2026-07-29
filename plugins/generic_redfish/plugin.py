"""Generic DMTF Redfish plugin.

The base implementation every OEM plugin extends. Reads everything it needs
about a host from a :class:`CapabilityMap` (built once per server by
:class:`RedfishCapabilityDiscoverer`) instead of hardcoding Redfish paths —
the only literal path string in this module is ``/redfish/v1/`` inside the
discoverer's bootstrap probe.

* Discovery   walks the service tree and returns a typed ``ServerInfo`` plus
              a ``CapabilityMap`` snapshot (Vendor, RedfishVersion, OEM blocks).
* Update      consults ``cap.multipart_push_uri`` / ``cap.simple_update_target``
              for transport and ``cap.bios_member_uri`` / ``cap.bmc_member_uri``
              for the canonical FirmwareInventory target. The legacy
              ``/Systems`` / ``/Managers`` fallback path is gone — pre-flight
              refuses to start a flash when the active bank can't be resolved.
* Power/Boot  uses ``cap.system_reset_target`` (validated against the BMC's
              advertised ``ResetType@Redfish.AllowableValues``) and
              ``cap.system_boot_patch_uri`` — no path concatenation.
* VM/ISO      picks a VirtualMedia slot by its advertised ``MediaTypes`` (a
              ``CD`` slot for ISOs) rather than by slot name, so the Dell
              ``RemovableDisk`` override falls out automatically.
"""
from __future__ import annotations

import asyncio
import logging
import time

from core.models.drive import Drive
from core.models.firmware import Firmware
from core.models.server import Credentials, ServerInfo
from core.plugins_api.base import (
    BootTarget,
    CompatResult,
    OEMPlugin,
    ProgressCallback,
    RedfishProbe,
    Result,
)
from core.plugins_api.redfish_client import (
    RedfishClient,
    RedfishError,
    parse_retry_after,
)

# The CapabilityMap discoverer is being built in parallel under
# ``core.plugins_api.capability_discoverer``. Import defensively so the
# plugin remains usable (with the inline legacy walks) even when the new
# module hasn't landed yet — keeps the test suite green during the rollout.
try:                                                                # pragma: no cover
    from core.plugins_api.capability_discoverer import (
        RedfishCapabilityDiscoverer,
        CapabilityMap,
    )
    _HAS_CAPABILITY_DISCOVERER = True
except Exception:                                                   # pragma: no cover
    RedfishCapabilityDiscoverer = None                              # type: ignore[assignment]
    CapabilityMap = None                                            # type: ignore[assignment]
    _HAS_CAPABILITY_DISCOVERER = False

log = logging.getLogger(__name__)

_OEM_NORMALIZE = {
    "supermicro": "supermicro", "super micro": "supermicro",
    # Tyrone Systems ships Supermicro boards rebadged — same AMI MegaRAC BMC
    # firmware underneath, so route them through the Supermicro plugin.
    "tyrone": "supermicro", "tyrone systems": "supermicro",
    "dell": "dell", "dell inc.": "dell", "dell emc": "dell",
    "hpe": "hpe", "hewlett packard enterprise": "hpe",
    "lenovo": "lenovo",
    "gigabyte": "gigabyte",
    "asrockrack": "asrock", "asrock": "asrock", "asrock rack": "asrock",
    "asus": "asus", "asustek": "asus", "asus technology": "asus",
    "msi": "msi", "micro-star international": "msi",
    "nvidia": "nvidia",
}


def _normalize_oem(value: str | None) -> str | None:
    if not value:
        return None
    return _OEM_NORMALIZE.get(value.strip().lower(), value.strip().lower())


def _first_oem_key(d: dict) -> str | None:
    oem = d.get("Oem") or {}
    keys = list(oem.keys())
    return keys[0] if keys else None


# Active-bank classification heuristics — kept here as well as in the
# CapabilityDiscoverer so the legacy inline path stays correct when the
# new discoverer module is not yet present.
# AMI MegaRAC variants ship dual-image schemes (BIOSImage1/BIOSImage2,
# BMCImage1/BMCImage2). Image1 is the ACTIVE bank on every AMI build we've
# audited; Image2 is the recovery/backup slot — flashing it would destroy
# the operator's rollback path.
_BANNED_BANK_SUFFIXES = (
    "backup", "golden", "staging", "recovery", "history",
    "image2", "image_2",
)

_FIRMWARE_CANDIDATES = {
    "BIOS": [
        "BIOS", "Bios", "SystemBios", "SystemBIOS", "UEFI", "Uefi",
        "BIOSImage1", "BIOSImage_1",                # dual-image AMI
    ],
    "BMC": [
        "BMC", "BMC-Primary", "BMCActive", "BMC_Active",
        "BMCImage1", "BMCImage_1",                  # dual-image AMI
        "iDRAC", "iLO", "iLO5", "iLO6", "XCC", "Manager",
    ],
}


# DMTF Storage / Drives reported by every Redfish-compliant BMC. Vendor
# OEM blocks add the chassis-slot info — we surface what's standard and
# leave vendor specifics to subclass overrides if they want richer data.
def _drive_from_redfish_json(d: dict) -> Drive | None:
    """Map one Redfish Drive resource to our :class:`Drive`.

    Returns ``None`` for an empty/absent bay (no real disk to target) so a
    ghost bay can never resolve and get wiped.
    """
    # Skip absent/empty bays. Lenovo XCC / HPE iLO publish empty slots as
    # Drive resources with State=Absent and CapacityBytes=null; a real disk
    # always reports a positive capacity.
    cap = int(d.get("CapacityBytes") or 0)
    state = (((d.get("Status") or {}).get("State")) or "").strip().lower()
    if cap <= 0 or state == "absent":
        return None
    name = (d.get("Id") or d.get("Name") or "").strip()
    protocol = (d.get("Protocol") or "").upper()
    # SAFETY: trust a BMC-reported name ONLY when it is already a real Linux
    # device handle. NEVER synthesize 'nvme<bay>n1' from the Redfish URI tail —
    # the BMC's bay index has no relationship to the host kernel's PCIe
    # enumeration, so the installer would wipe a DIFFERENT physical disk.
    # Non-device ids are kept as opaque display labels; the autoinstall
    # renderer targets the disk by SERIAL, refusing if neither is available.
    if not name.startswith(("nvme", "sd")):
        oid = (d.get("@odata.id") or "").rstrip("/").rsplit("/", 1)[-1]
        name = name or oid or "drive"
    location = (d.get("PhysicalLocation") or {})
    slot = None
    info = location.get("PartLocation") or {}
    if info.get("LocationType") == "Slot" and "ServiceLabel" in info:
        slot = str(info["ServiceLabel"])
    elif location.get("Info"):
        slot = str(location["Info"])
    return Drive(
        name=name,
        protocol=protocol or "Unknown",
        media_type=(d.get("MediaType") or "Unknown"),
        capacity_bytes=cap,
        model=(d.get("Model") or "").strip(),
        serial=(d.get("SerialNumber") or "").strip(),
        slot=slot,
        redfish_uri=d.get("@odata.id") or "",
    )


class GenericRedfishPlugin(OEMPlugin):
    key = "generic_redfish"
    display_name = "Generic Redfish (DMTF)"
    supported_models = ("*",)

    # Cache of CapabilityMap snapshots keyed by server.ip. One walk per
    # server per flash session — orchestrator decides when to invalidate
    # (BMCs reboot, FirmwareInventory IDs change post-flash). Plugins keep
    # the map alive only for the duration of the action; long-term caching
    # belongs on ServerInfo / the repository, not here.
    _CAP_CACHE_TTL_S = 60.0

    def __init__(self) -> None:
        # Per-plugin-instance cache. Keyed by ``server.ip`` -> (cap, ts).
        # Bounded by TTL so a stale map after a flash + BMC reset is
        # refreshed automatically on the next call.
        self._cap_cache: dict[str, tuple[object, float]] = {}

    def match(self, probe: RedfishProbe) -> float:
        # Always-available floor: any Redfish host scores 0.1 so OEM plugins
        # with confident matches always win.
        return 0.1 if probe.redfish_root else 0.0

    async def discover(self, base_url: str, creds: Credentials) -> ServerInfo:
        scheme, _, hostport = base_url.partition("://")
        host, _, port_s = hostport.partition(":")
        port = int(port_s) if port_s else (443 if scheme == "https" else 80)

        client = RedfishClient(
            host, port=port, scheme=scheme,
            username=creds.username, password=creds.password,
            timeout=10.0,
        )

        try:
            try:
                root = await client.get_json("/redfish/v1/")
            except RedfishError as exc:
                raise RuntimeError(f"Redfish ServiceRoot unavailable: {exc}") from exc

            manufacturer_hint = root.get("Vendor") or _first_oem_key(root)

            systems = await client.get_json("/redfish/v1/Systems")
            members = systems.get("Members") or []
            system_uri = members[0].get("@odata.id") if members else None
            if not system_uri:
                raise RuntimeError("No usable Systems collection member at Redfish endpoint")
            system = await client.get_json(system_uri)

            bmc_version = None
            try:
                managers = await client.get_json("/redfish/v1/Managers")
                mgr_members = managers.get("Members") or []
                mgr_uri = mgr_members[0].get("@odata.id") if mgr_members else None
                if mgr_uri:
                    manager = await client.get_json(mgr_uri)
                    bmc_version = manager.get("FirmwareVersion")
            except RedfishError as exc:
                log.warning("%s Managers collection unavailable: %s", host, exc)

            manufacturer = system.get("Manufacturer") or manufacturer_hint
            return ServerInfo(
                ip=host,
                hostname=system.get("HostName") or system.get("Name"),
                manufacturer=manufacturer,
                oem=_normalize_oem(manufacturer),
                model=system.get("Model"),
                serial=system.get("SerialNumber"),
                bmc_version=bmc_version,
                bios_version=system.get("BiosVersion"),
                power_state=system.get("PowerState", "Unknown"),
                status="online",
                protocol="redfish",
                redfish_root=client.base_url + "/redfish/v1/",
            )
        finally:
            # RedfishClient owns a persistent aiohttp session — release it so
            # the scanner doesn't leak one socket per discovered host.
            await client.close()

    async def validate(self, server: ServerInfo, firmware: Firmware) -> CompatResult:
        # 1. OEM match (normalize both sides — server.oem is already normalized;
        #    firmware.oem is raw operator input, so "Dell" vs "dell" used to
        #    falsely block a valid flash).
        fw_oem = _normalize_oem(firmware.oem)
        if fw_oem and server.oem and fw_oem != server.oem:
            return CompatResult(
                compatible=False,
                reason=f"OEM mismatch: server is {server.oem}, firmware is for {firmware.oem}",
            )
        # 2. Same-version short-circuit. Map type → the matching version field;
        #    HGX has no single version on ServerInfo, so skip the check (the old
        #    code compared HGX firmware against the host's BIOS version).
        current = (server.bmc_version if firmware.type == "BMC"
                   else server.bios_version if firmware.type == "BIOS"
                   else None)
        if current and current == firmware.version:
            return CompatResult(
                compatible=False,
                reason=f"Server already at {firmware.type} {firmware.version}",
            )
        # 3. Model match. Case-insensitive. A PLAIN model must match a whole
        #    token of the server model (so "R650" does NOT match "R6500"); an
        #    explicit glob (containing * ? [) is matched with fnmatch. The old
        #    bidirectional substring test let R650 firmware pass on an R6500.
        if firmware.model and firmware.model != "*" and server.model:
            import fnmatch
            import re as _re
            fw_m = firmware.model.strip().lower()
            sv_m = server.model.strip().lower()
            tokens = [t for t in _re.split(r"[\s_\-/]+", sv_m) if t]
            if any(ch in fw_m for ch in "*?["):
                matched = (fnmatch.fnmatch(sv_m, fw_m)
                           or any(fnmatch.fnmatch(t, fw_m) for t in tokens))
            else:
                matched = (fw_m == sv_m) or (fw_m in tokens)
            if not matched:
                return CompatResult(
                    compatible=False,
                    reason=f"Model mismatch: server {server.model}, firmware for {firmware.model}",
                )
        return CompatResult(compatible=True)

    async def update_bmc(
        self, server: ServerInfo, firmware: Firmware,
        on_progress: ProgressCallback, creds: Credentials,
        apply_now: bool = False,
    ) -> Result:
        return await self._do_update(server, firmware, on_progress, "BMC", creds, apply_now)

    async def update_bios(
        self, server: ServerInfo, firmware: Firmware,
        on_progress: ProgressCallback, creds: Credentials,
        apply_now: bool = False,
    ) -> Result:
        return await self._do_update(server, firmware, on_progress, "BIOS", creds, apply_now)

    async def update_hgx(
        self, server: ServerInfo, firmware: Firmware,
        on_progress: ProgressCallback, creds: Credentials,
        apply_now: bool = False,
    ) -> Result:
        """Push an HGX/GPU firmware image via DMTF multipart push.

        The same transport that handles BIOS/BMC also handles every HGX
        baseboard component — only the ``Targets`` URI differs. The audit
        at ``audit_update_apis.json`` proves a single AMI BMC (.193)
        publishes 28+ HGX firmware members under
        ``/redfish/v1/UpdateService/FirmwareInventory`` (HGX_FW_GPU_0..7,
        HGX_FW_ERoT_*, HGX_InfoROM_*, HGX_FW_HMC_*, …), all updateable
        through the same ``/redfish/v1/UpdateService/upload`` endpoint.

        Routing: v1 picks a single target per call — the first
        ``cap.hgx_member_uris`` entry whose key substring-matches the
        firmware filename, or the first entry overall if no match. Future
        multi-target ("flash all 8 GPUs in parallel") work layers on top.

        ApplyTime is ``OnReset`` because HGX firmware activates on the
        host AC-power cycle, not on a BMC reset. NO ``OemParameters``
        part — HGX/GPU firmware does NOT use AMI's ``ImageType`` field
        (verified against the production AMI Group-2 audit), so the AMI
        ``_oem_update_params`` hook is bypassed entirely.
        """
        client = self._client_for(server, creds)
        try:
            cap = await self._capability_map(server, client)
            hgx_uris: dict[str, str] = {}
            if cap is not None:
                hgx_uris = dict(getattr(cap, "hgx_member_uris", {}) or {})

            if not hgx_uris:
                return Result(
                    ok=False,
                    message=(
                        "No HGX/GPU components in FirmwareInventory "
                        "— this BMC does not expose an HGX baseboard."
                    ),
                )

            # Resolve transport. Multipart push is required for HGX
            # firmware on every shipped AMI build; SimpleUpdate works only
            # for blobs the BMC can fetch by URI, which HGX images aren't.
            try:
                multipart_uri, _simple_target, _simple_path = (
                    await self._resolve_update_service(client, cap)
                )
            except RedfishError as exc:
                return Result(ok=False, message=str(exc))
            if not multipart_uri:
                return Result(
                    ok=False,
                    message=(
                        "BMC does not advertise MultipartHttpPushUri — "
                        "HGX firmware requires multipart push."
                    ),
                )

            # Pick the target group. For a bundle image (no specific GPU/
            # ERoT in the filename, or filename contains "bundle"/"baseboard"/
            # "dgx") we flash every active-bank HGX member in ONE multipart
            # push — DMTF allows multiple Targets per upload. For a per-die
            # filename (e.g. ``HGX_FW_GPU_3.fwpkg``) we target just that one.
            target_keys, target_uris = self._select_hgx_target_group(
                hgx_uris, firmware.name,
            )
            target_key = target_keys[0] if len(target_keys) == 1 else f"{len(target_keys)} HGX components"

            on_progress("uploading", 10)

            params = {
                "Targets": list(target_uris),
                "@Redfish.OperationApplyTime": "OnReset",
            }
            try:
                post_result = await client.post_multipart(
                    multipart_uri,
                    params=params,
                    file_path=firmware.file_path,
                    filename=firmware.name,
                    # NO oem_params — HGX/GPU firmware on AMI BMCs does
                    # not use the ``ImageType`` OemParameter that BIOS/BMC
                    # updates rely on. Passing one causes the BMC to
                    # reject the multipart body with HTTP 400.
                    oem_params=None,
                )
            except RedfishError as exc:
                return Result(ok=False, message=f"HGX firmware push failed: {exc}")

            # Sushy precedence (Location header > body @odata.id)
            task_uri = self._resolve_task_uri(post_result)
            if not task_uri:
                return Result(
                    ok=False,
                    message="HGX update accepted but BMC returned no Task to monitor",
                )

            on_progress("flashing", 30)

            ok, msg = await self._poll_task(
                client, task_uri, on_progress, progress_band=(30, 90),
            )
            if not ok:
                return Result(
                    ok=False,
                    message=(
                        msg or
                        f"HGX flash did not finish within "
                        f"{self._FLASH_POLL_CEILING_S // 60} min — check the BMC; "
                        "the update may still be applying."
                    ),
                )

            on_progress("verifying", 95)

            # Drop the cap so any subsequent action re-walks: a successful
            # HGX flash can change the affected member's Version field.
            self._invalidate_capability_map(server)

            return Result(
                ok=True,
                version_after=firmware.version,
                message=(
                    f"HGX firmware staged on {target_key} — a host AC power "
                    "cycle is required to activate the new image."
                ),
            )
        finally:
            await client.close()

    @staticmethod
    def _select_hgx_target(
        hgx_uris: dict[str, str], firmware_name: str,
    ) -> tuple[str, str]:
        """Pick a SINGLE HGX FirmwareInventory key/URI to target.

        Priority:

        1. Case-insensitive substring match between the firmware filename
           and an HGX inventory key (so ``HGX_FW_GPU_3.fwpkg`` resolves to
           ``HGX_FW_GPU_3``). The match runs on the longest key first so
           ``HGX_FW_ERoT_GPU_0`` is preferred over the shorter
           ``HGX_FW_GPU_0`` when both could match a filename.
        2. The first key in the dict — deterministic because the discoverer
           populates this map by iterating ``firmware_inventory`` in its
           dict-insertion order (which mirrors the Members[] order from
           the BMC).

        Returns ``(key, uri)``.
        """
        if not hgx_uris:
            raise ValueError("hgx_uris must be non-empty")
        fname_l = (firmware_name or "").lower()
        # Longest key first so ERoT_GPU_0 beats GPU_0 when both match.
        for key in sorted(hgx_uris.keys(), key=len, reverse=True):
            if key.lower() in fname_l:
                return key, hgx_uris[key]
        # No substring match — fall back to the first entry.
        first_key = next(iter(hgx_uris))
        return first_key, hgx_uris[first_key]

    @staticmethod
    def _select_hgx_target_group(
        hgx_uris: dict[str, str], firmware_name: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Pick a GROUP of HGX FirmwareInventory members to target in a
        single multipart push.

        DMTF allows multiple URIs in ``Targets`` for one upload, and an HGX
        baseboard image (e.g. ``HGX_FW_BUNDLE_2503.fwpkg``) is typically
        a single artifact that the BMC routes to every GPU/ERoT/InfoROM
        die. Flashing them one at a time (single-target) means N uploads
        of the same bytes — slow + multiplies the risk of partial failure.

        Heuristic:

        * If the firmware NAME explicitly matches one key (substring), this
          is a per-die push: return only that one URI.
        * Otherwise, if the name contains ``bundle`` / ``baseboard`` /
          ``hgx_fw_bundle`` / ``dgx`` (vendor-neutral bundle markers), or
          if no key matches, return ALL active-bank members of the same
          family as the dominant token in the dict.

        Returns ``(keys, uris)`` — both tuples preserve dict-insertion
        order so logs / progress reporting stay deterministic.
        """
        if not hgx_uris:
            raise ValueError("hgx_uris must be non-empty")
        fname_l = (firmware_name or "").lower()
        # Per-die explicit match wins
        for key in sorted(hgx_uris.keys(), key=len, reverse=True):
            if key.lower() in fname_l:
                return (key,), (hgx_uris[key],)
        # Bundle / baseboard image — flash everything together
        BUNDLE_MARKERS = ("bundle", "baseboard", "hgx_fw_bundle", "dgx", "_all")
        is_bundle = any(m in fname_l for m in BUNDLE_MARKERS)
        if is_bundle or not fname_l:
            keys = tuple(hgx_uris.keys())
            uris = tuple(hgx_uris[k] for k in keys)
            return keys, uris
        # No match and not a bundle — pick the single best guess like the
        # single-target helper. Caller can still ship just one URI.
        key, uri = GenericRedfishPlugin._select_hgx_target(hgx_uris, firmware_name)
        return (key,), (uri,)

    # How long to poll the BMC Task before giving up. Real BIOS/BMC flashes
    # routinely take 5–15 min; the simulator finishes in seconds.
    _FLASH_POLL_CEILING_S = 30 * 60     # 30 minutes
    _FLASH_POLL_INTERVAL_S = 3.0
    # Minimum sleep between polls — caps Retry-After at a sane floor so a
    # misbehaving BMC reporting "Retry-After: 0" doesn't peg the CPU. Also
    # the upper bound (60s) so Dell iDRAC's "Retry-After: 30" is honored
    # but a malicious "999999" gets clamped.
    _POLL_INTERVAL_MIN_S = 1.0
    _POLL_INTERVAL_MAX_S = 60.0

    @staticmethod
    def _resolve_task_uri(post_result: dict, base_url: str = "") -> str:
        """DMTF + Sushy precedence: ``Location`` header wins, with the body's
        ``@odata.id`` appended via urljoin if present. If only the body
        contains a URI, use that. Returns ``""`` when no Task URI is
        available — callers treat empty as "synchronous BMC, assume OK".

        Edge case: some AMI MegaRAC builds return a body URI WITHOUT a
        leading slash (e.g. ``TaskService/Tasks/3``). ``client.get_json``
        requires a leading slash; without normalizing we'd silently 404
        and the poll loop would treat it as "no task" → false success.
        """
        loc = (post_result.get("location") or "").strip()
        body = post_result.get("body") or {}
        body_uri = (body.get("@odata.id") or "").strip()

        def _normalize(uri: str) -> str:
            if not uri:
                return ""
            # If it's absolute (http(s)://...), let it through unchanged —
            # the caller's get_json handles full URLs by base_url prefix
            # stripping elsewhere. If it's path-only, ensure leading "/".
            if uri.startswith(("http://", "https://", "/")):
                return uri
            return "/" + uri

        # Header-only — common path
        if loc and not body_uri:
            return _normalize(loc)
        # Body-only — AMI MegaRAC sometimes only sends body
        if body_uri and not loc:
            return _normalize(body_uri)
        # Both: Sushy's rule is "Location wins"
        if loc and body_uri:
            return _normalize(loc)
        return ""

    async def _poll_task(
        self,
        client: RedfishClient,
        task_uri: str,
        on_progress: ProgressCallback,
        *,
        progress_band: tuple[int, int] = (30, 85),
    ) -> tuple[bool, str]:
        """Poll a TaskService task to a terminal state with Retry-After
        honoring.

        Returns ``(ok, message)``. ``ok=True`` when ``TaskState=Completed``;
        ``ok=False`` for Exception/Killed/Cancelled or for the overall
        timeout. ``message`` carries the BMC's last Message[].Message when
        the task failed — that's what we want surfaced on the task row.

        ``progress_band`` is ``(start, end)`` so callers can map the BMC's
        0-100 ``PercentComplete`` into a sub-range of the overall task
        progress (e.g., flashing is 30-85% of the whole BIOS update job).
        """
        if not task_uri:
            return True, "no task returned (BMC flashed synchronously)"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._FLASH_POLL_CEILING_S
        last_pct = -1
        b_lo, b_hi = progress_band
        b_span = max(0, b_hi - b_lo)
        consecutive_errors = 0
        # Cap the consecutive-error count so a BMC that's truly dead
        # doesn't tie us up for 30 minutes when nothing's coming back.
        max_consecutive_errors = 60     # ~60 polls of ~5s each = 5 min
        while loop.time() < deadline:
            wait_s = self._FLASH_POLL_INTERVAL_S
            try:
                task, headers = await client.get_json_and_headers(task_uri)
                consecutive_errors = 0
                ra = parse_retry_after(headers.get("Retry-After"))
                if ra > 0:
                    wait_s = ra
            except RedfishError as exc:
                # Transient: BMC may have briefly dropped its Redfish service
                # while mid-flash. Tolerate but cap.
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    return False, f"BMC unreachable for {max_consecutive_errors} consecutive polls: {exc}"
                wait_s = min(self._FLASH_POLL_INTERVAL_S * 2, self._POLL_INTERVAL_MAX_S)
                await asyncio.sleep(wait_s)
                continue
            pct = int(task.get("PercentComplete", 0) or 0)
            if pct != last_pct and b_span:
                last_pct = pct
                on_progress("flashing", b_lo + int(pct * b_span / 100))
            state = task.get("TaskState", "Running")
            if state in ("Completed", "Exception", "Killed", "Cancelled"):
                if state != "Completed":
                    msgs = task.get("Messages") or []
                    detail = msgs[-1].get("Message", "") if msgs else ""
                    return False, f"BMC task ended '{state}'. {detail}".strip()
                return True, "completed"
            wait_s = max(self._POLL_INTERVAL_MIN_S, min(wait_s, self._POLL_INTERVAL_MAX_S))
            await asyncio.sleep(wait_s)
        return False, f"flash did not finish within {self._FLASH_POLL_CEILING_S // 60} min"

    async def _wait_for_resource_stability(
        self,
        client: RedfishClient,
        uris: tuple[str, ...] = (),
        *,
        consecutive_successes: int = 3,
        gap_s: float = 5.0,
        timeout_s: float = 300.0,
    ) -> bool:
        """Wait for the BMC to come back to a stable post-flash state.

        After a BMC reset the Redfish service serves a couple of 200s
        briefly before disconnecting again (BMC IPL not done). Ironic's
        ``_validate_resources_stability`` pattern: require N consecutive
        clean GETs of the key resources spaced ``gap_s`` apart before
        declaring "BMC is fully back". Without this gate, the version
        read-back hits a half-rebooted BMC and silently returns None.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        # Default probe set: ServiceRoot + UpdateService — universally
        # available and cheap. Callers can pass extras (e.g. the firmware
        # member URI we just flashed).
        probes = uris or ("/redfish/v1/", "/redfish/v1/UpdateService")
        streak = 0
        while loop.time() < deadline:
            ok = True
            for path in probes:
                try:
                    await client.get_json(path)
                except RedfishError:
                    ok = False
                    break
            if ok:
                streak += 1
                if streak >= consecutive_successes:
                    return True
            else:
                streak = 0
            await asyncio.sleep(gap_s)
        return False

    async def _read_current_version(
        self,
        client: RedfishClient,
        target_uri: str | None,
    ) -> str | None:
        """Read FirmwareInventory member's Version field. Returns None on
        any failure — caller treats that as "BMC isn't telling us; trust
        the Task's terminal state instead". Used for BMC-version-delta
        success signaling (Ironic pattern)."""
        if not target_uri:
            return None
        try:
            d = await client.get_json(target_uri)
            v = d.get("Version")
            return str(v) if v is not None else None
        except RedfishError:
            return None

    # Host reset type used to apply a staged BIOS image immediately.
    # GracefulRestart asks the OS to shut down cleanly first. Vendor plugins
    # override this (or _reset_to_apply) for job-queue / forced-cycle needs.
    _APPLY_RESET_TYPE = "GracefulRestart"

    # Reset-type ordering for "apply now" — pick the first one the BMC
    # advertises under ``ResetType@Redfish.AllowableValues``. GracefulRestart
    # gives the OS a chance to shut down cleanly; the rest are progressively
    # less polite. Subclasses override the base preference via
    # ``_APPLY_RESET_TYPE``; this list applies when the BMC tells us its
    # AllowableValues.
    _APPLY_RESET_TYPE_PRIORITY = (
        "GracefulRestart", "ForceRestart", "PowerCycle", "ForceReset",
    )
    # Reset-type ordering for ``power_cycle`` (operator-initiated hard reset).
    _POWER_CYCLE_RESET_TYPE_PRIORITY = (
        "ForceRestart", "PowerCycle", "ForceReset", "GracefulRestart",
    )
    # Logical power action -> (ResetType preference list chosen from the BMC's
    # advertised AllowableValues, hard fallback when the BMC publishes none).
    # ``off`` prefers GracefulShutdown; PowerService verifies PowerState and the
    # operator can escalate to the separate ``force_off`` if the host has no OS
    # to honor the ACPI request.
    # The fallback (2nd tuple element) is used only when the BMC publishes no
    # AllowableValues. Graceful intents fall back to a graceful DMTF type — never
    # silently to a forced power-off/reset; the dedicated force_off action is the
    # explicit forceful path.
    _POWER_ACTION_RESETTYPES: dict[str, tuple[tuple[str, ...], str]] = {
        "on":               (("On", "ForceOn", "PowerCycle"), "On"),
        "off":              (("GracefulShutdown", "ForceOff", "PushPowerButton"), "GracefulShutdown"),
        "force_off":        (("ForceOff", "PushPowerButton"), "ForceOff"),
        "restart":          (("ForceRestart", "PowerCycle", "GracefulRestart"), "ForceRestart"),
        "graceful_restart": (("GracefulRestart", "ForceRestart"), "GracefulRestart"),
        "cycle":            (("PowerCycle", "ForceRestart"), "PowerCycle"),
    }

    # ---- CapabilityMap plumbing --------------------------------------
    # Plugins consume a typed CapabilityMap instead of re-walking the BMC
    # tree on every action. The map is built once per (server.ip) by
    # RedfishCapabilityDiscoverer and cached on the plugin instance for
    # the lifetime of the operation.

    async def _capability_map(
        self, server: ServerInfo, client: RedfishClient,
    ) -> object | None:
        """Return the cached CapabilityMap for ``server`` or build one.

        ``None`` when the discoverer module isn't available — callers fall
        back to legacy inline walks. Cache invalidation is TTL-based so a
        post-flash BMC that has renumbered its FirmwareInventory members
        gets a fresh map on the next action.
        """
        if not _HAS_CAPABILITY_DISCOVERER or RedfishCapabilityDiscoverer is None:
            return None
        ip = (server.ip or "").strip()
        cached = self._cap_cache.get(ip) if ip else None
        if cached is not None:
            cap, ts = cached
            if (time.monotonic() - ts) < self._CAP_CACHE_TTL_S:
                return cap
        try:
            discoverer = RedfishCapabilityDiscoverer(client)
            cap = await discoverer.discover()
        except Exception as exc:                                    # pragma: no cover
            # The discoverer's only hard-fail case is the bootstrap probe
            # (no ServiceRoot). Surface as a None cap and let callers
            # decide — either bubble a clear error or fall back to legacy.
            log.warning("%s CapabilityMap discovery failed: %s", ip or "<unknown>", exc)
            return None
        if ip:
            self._cap_cache[ip] = (cap, time.monotonic())
        return cap

    def _invalidate_capability_map(self, server: ServerInfo) -> None:
        """Drop the cached map for ``server`` — call after a flash that
        is expected to mutate the BMC's surface (FirmwareInventory IDs
        renumber on some AMI builds, BMC firmware self-resets, etc.)."""
        ip = (server.ip or "").strip()
        if ip:
            self._cap_cache.pop(ip, None)

    async def _reset_to_apply(self, client: RedfishClient, target_uri: str,
                              cap: object | None = None) -> tuple[bool, str]:
        """Issue ComputerSystem.Reset to apply a staged image now.

        DMTF-standard and portable across modern BMCs. Failure is non-fatal —
        the image is already staged; the operator can reboot manually.

        ``target_uri`` is the resolved reset action target (preferred:
        ``cap.system_reset_target``). When ``cap`` is provided, the
        ResetType is chosen from the BMC's advertised AllowableValues so
        we never POST a value the BMC will reject; otherwise the legacy
        ``_APPLY_RESET_TYPE`` is used.
        """
        # The historical contract took the *system URI* and appended
        # ``/Actions/ComputerSystem.Reset``. Keep accepting that form so
        # vendor plugins that still pass a system URI continue to work,
        # but prefer a fully-resolved action target when given one.
        if "/Actions/" not in target_uri:
            action = f"{target_uri}/Actions/ComputerSystem.Reset"
        else:
            action = target_uri
        reset_type = self._choose_reset_type(
            cap, self._APPLY_RESET_TYPE_PRIORITY, fallback=self._APPLY_RESET_TYPE,
        )
        try:
            resp = await client.post_json(action, {"ResetType": reset_type})
            if resp["status"] >= 400:
                return False, f"reset HTTP {resp['status']}"
            return True, f"{reset_type} issued"
        except RedfishError as exc:
            return False, str(exc)

    @staticmethod
    def _choose_reset_type(
        cap: object | None, priority: tuple[str, ...], fallback: str,
    ) -> str:
        """Pick the strongest reset value the BMC actually advertises.

        Falls back to the historical hardcoded value when the BMC didn't
        publish an ActionInfo / AllowableValues — preserves behavior on
        old firmware that doesn't expose the metadata.
        """
        allowed = ()
        if cap is not None:
            allowed = tuple(getattr(cap, "system_reset_allowed_resettypes", ()) or ())
        if not allowed:
            return fallback
        for choice in priority:
            if choice in allowed:
                return choice
        # The BMC's AllowableValues didn't intersect our preference list.
        # Return the hard fallback (the BMC may reject it with a clear
        # "unsupported ResetType") rather than allowed[0] — that arbitrary first
        # value can be semantically unrelated to the request (e.g. Nmi/ForceOff
        # for a power-On), which would execute the WRONG action and report success.
        return fallback

    # ---- OEM hooks ----------------------------------------------------
    # Vendor plugins override these to inject OEM-specific behavior without
    # re-implementing the whole upload + poll flow.

    def _update_targets(self, target_uri: str) -> list[str]:
        """Return the ``Targets`` array for UpdateParameters. The DMTF
        baseline sends the resolved FirmwareInventory URI; Dell iDRAC
        overrides this to ``[]`` because iDRAC auto-routes by DUP header.
        """
        return [target_uri]

    def _oem_update_params(self, target: str) -> dict | None:
        """Return the ``OemParameters`` JSON part, or None to omit it.
        AMI-MegaRAC vendors (Supermicro, ASRock, Gigabyte, ASUS) override
        this to inject ``{"ImageType": "BIOS"|"BMC"}``.
        """
        return None

    async def _resolve_firmware_target(
        self, client: RedfishClient, target: str,
        cap: object | None = None,
    ) -> str:
        """Resolve the canonical ``/UpdateService/FirmwareInventory/<id>`` URI
        for the requested component (``BIOS`` or ``BMC``).

        DMTF requires update Targets to be ``FirmwareInventory`` URIs, NOT
        ``/Systems/<n>`` or ``/Managers/<n>``. With a CapabilityMap this is
        a pure lookup — ``cap.bios_member_uri`` / ``cap.bmc_member_uri``
        already point at the active bank, with rollback banks (Image2,
        Backup, Golden, Staging, Recovery, History) filtered out by the
        discoverer's classification pass.

        Falls back to an inline FirmwareInventory walk when no
        ``CapabilityMap`` is available (test stubs, the discoverer module
        not yet installed, or a transient walk failure). The walk applies
        the same BANNED-leaf heuristic so the fallback still respects the
        active-bank invariant.
        """
        # Fast path: CapabilityMap already classified the inventory.
        if cap is not None:
            cap_uri = (
                getattr(cap, "bios_member_uri", None)
                if target == "BIOS"
                else getattr(cap, "bmc_member_uri", None)
            )
            if cap_uri:
                return cap_uri
            # Cap was present but didn't find an active bank — surface a
            # clear, single error rather than retrying inline. (This is
            # the behavior the design spec mandates: refuse to flash when
            # the active bank can't be resolved instead of falling back
            # to ``/Systems`` / ``/Managers`` and producing RED097-style
            # rejections.)
            raise RedfishError(
                f"No {target} member found in FirmwareInventory "
                "(discoverer reported no active bank)"
            )

        # Legacy inline walk — kept for the test stubs and for the rollout
        # window before RedfishCapabilityDiscoverer ships. Applies the
        # same priority + BANNED-leaf rules so the result matches what
        # the discoverer would have returned.
        try:
            inv = await client.get_json("/redfish/v1/UpdateService/FirmwareInventory")
        except RedfishError as exc:
            raise RedfishError(f"FirmwareInventory unavailable: {exc}") from exc

        candidates = _FIRMWARE_CANDIDATES[target]
        members = inv.get("Members") or []
        member_uris = [m.get("@odata.id", "") for m in members]

        # 1) Exact leaf-name match in priority order
        for cand in candidates:
            for uri in member_uris:
                if uri.rsplit("/", 1)[-1] == cand:
                    return uri

        # 2) Substring match — skip recovery/backup/staging/secondary banks.
        for uri in member_uris:
            leaf = uri.rsplit("/", 1)[-1].lower()
            if any(b in leaf for b in _BANNED_BANK_SUFFIXES):
                continue
            if target == "BIOS" and ("bios" in leaf or "uefi" in leaf):
                return uri
            if target == "BMC" and any(
                k in leaf for k in ("bmc", "idrac", "ilo", "xcc", "manager")
            ):
                return uri

        raise RedfishError(
            f"No {target} member found in FirmwareInventory "
            f"(saw: {[u.rsplit('/', 1)[-1] for u in member_uris]})"
        )

    async def _resolve_update_service(
        self, client: RedfishClient, cap: object | None,
    ) -> tuple[str, str, str]:
        """Return ``(multipart_uri, simple_update_uri, simple_update_action_path)``.

        Prefer the CapabilityMap when present; otherwise read UpdateService
        directly. Returned strings can be empty when the BMC doesn't
        advertise that transport — callers gate on truthiness.
        """
        if cap is not None:
            multipart_uri = getattr(cap, "multipart_push_uri", "") or ""
            simple_update_uri = getattr(cap, "simple_update_target", "") or ""
            return (
                multipart_uri,
                simple_update_uri,
                # Generic plugin's historical fallback path — still used
                # by the SimpleUpdate branch when the BMC didn't resolve
                # a specific action target in cap.
                "/redfish/v1/UpdateService/Actions/SimpleUpdate",
            )
        # Legacy: probe UpdateService directly. The capability discoverer
        # would have done this once per server; without it we eat one
        # extra round-trip per flash.
        try:
            us = await client.get_json("/redfish/v1/UpdateService")
        except RedfishError as exc:
            raise RedfishError(f"UpdateService unreachable: {exc}") from exc
        multipart_uri = us.get("MultipartHttpPushUri") or ""
        actions = (us.get("Actions") or {})
        simple_action = actions.get("#UpdateService.SimpleUpdate") or {}
        simple_update_uri = simple_action.get("target") or ""
        return (
            multipart_uri,
            simple_update_uri,
            "/redfish/v1/UpdateService/Actions/SimpleUpdate",
        )

    async def _do_update(
        self,
        server: ServerInfo,
        firmware: Firmware,
        on_progress: ProgressCallback,
        target: str,
        creds: Credentials,
        apply_now: bool = False,
    ) -> Result:
        """DMTF Redfish firmware update — portable baseline.

        Reads the BMC surface via :class:`CapabilityMap` to pick transport:

        1. **Multipart HTTP push** (preferred) — ``cap.multipart_push_uri``
           is set. ClusterOne pushes firmware bytes straight to the BMC.
           No HTTP server, no firewall back-path.
        2. **SimpleUpdate + ImageURI** (fallback) — POSTs ``SimpleUpdate``
           with a ``file:///`` URI the simulator understands.

        Targets are resolved to ``cap.bios_member_uri`` / ``cap.bmc_member_uri``
        per DMTF spec — NOT ``/Systems/1`` (which the legacy code used to
        fall back to and which real BMCs reject with RED097).
        """
        client = self._client_for(server, creds)

        try:
            # Build (or reuse) the CapabilityMap for this server. Plugins
            # consult cap for every BMC-side path so this is the only
            # walk-the-tree work we do per flash session.
            cap = await self._capability_map(server, client)

            # Pick the transport from the resolved surface.
            try:
                multipart_uri, _simple_target, simple_path = (
                    await self._resolve_update_service(client, cap)
                )
            except RedfishError as exc:
                return Result(ok=False, message=str(exc))

            # Resolve the FirmwareInventory target (canonical per DMTF).
            # Pre-flight refusal: a missing active bank is a hard error —
            # the historical fallback to PATCHing /Systems or /Managers as
            # the target is exactly what produced RED097 ("rollback bank
            # flashed"). Removing that fallback is one of the audit's
            # CRITICAL fixes — the discoverer is responsible for picking
            # the active bank correctly; if it can't, we MUST stop.
            try:
                target_uri = await self._resolve_firmware_target(client, target, cap)
            except RedfishError as exc:
                return Result(
                    ok=False,
                    message=(
                        f"No active {target} bank in FirmwareInventory ({exc}). "
                        "Refusing to fall back to /Systems or /Managers — "
                        "that historical path corrupts rollback banks (RED097)."
                    ),
                )

            # Snapshot the current firmware version BEFORE the flash. If the
            # BMC drops mid-flash (BMC reset takes it offline so the Task
            # vanishes), a different version read after the BMC comes back
            # is positive evidence the flash succeeded — Ironic's pattern.
            pre_flash_version = await self._read_current_version(client, target_uri)

            on_progress("uploading", 10)

            if multipart_uri:
                # ---- Path 1: multipart push (real hardware) ----
                # Apply BMC firmware immediately; stage BIOS for the next reset
                # so the host isn't yanked out from under a running OS.
                apply_time = "Immediate" if target == "BMC" else "OnReset"
                params: dict = {"@Redfish.OperationApplyTime": apply_time}
                # Dell iDRAC auto-routes by DUP/.d7/.d9 header and rejects
                # an empty Targets array (RED097). When the OEM hook
                # returns ``[]`` we OMIT the key entirely; only attach
                # Targets when the OEM gave us a non-empty list.
                targets = self._update_targets(target_uri)
                if targets:
                    params["Targets"] = targets
                try:
                    post_result = await client.post_multipart(
                        multipart_uri,
                        params=params,
                        file_path=firmware.file_path,
                        filename=firmware.name,
                        oem_params=self._oem_update_params(target),
                    )
                except RedfishError as exc:
                    return Result(ok=False, message=f"Firmware push failed: {exc}")
            else:
                # ---- Path 2: SimpleUpdate (simulator / HTTP-reachable BMC) ----
                body = {
                    "ImageURI": f"file:///firmware/{firmware.name}",
                    "Targets": self._update_targets(target_uri),
                    "Oem": {"ClusterOne": {"TargetVersion": firmware.version, "TargetType": target}},
                }
                try:
                    post_result = await client.post_json(simple_path, body)
                except RedfishError as exc:
                    return Result(ok=False, message=f"SimpleUpdate failed: {exc}")

            # DMTF + Sushy precedence: Location header wins, body @odata.id
            # is the AMI MegaRAC fallback (some return relative paths there).
            task_uri = self._resolve_task_uri(post_result)
            if not task_uri:
                # No Task to poll. Distinguish a SYNCHRONOUS completion (the BMC
                # flashed inline and returned 200/204) from a genuine monitoring
                # gap (202 with no Location). Reporting the sync case as a failure
                # made the operator retry and double-flash.
                status = int(post_result.get("status") or 0)
                if status not in (200, 204):
                    return Result(ok=False, message="Update accepted but BMC returned no Task to monitor")
                ok, msg = True, "synchronous completion (no Task)"
            else:
                on_progress("flashing", 30)
                # Poll via the shared helper — honors Retry-After, tolerates
                # transient BMC drops (caps at 5 min consecutive errors),
                # parses TaskState + Messages for actionable failure text.
                ok, msg = await self._poll_task(
                    client, task_uri, on_progress, progress_band=(30, 80),
                )
            if not ok:
                # If the task is unreachable but the BIOS/BMC version actually
                # changed (BMC self-reset took it down mid-flash), treat the
                # version delta as success — mirrors Ironic's pattern.
                # Rescue ONLY a transient loss (the BMC went unreachable mid-flash
                # while self-resetting), NEVER an affirmative BMC failure
                # (TaskState Exception/Killed/Cancelled → "task ended '<state>'"),
                # and only when we had a real pre-flash baseline to compare.
                transient = "unreachable" in (msg or "").lower()
                if target_uri and transient and pre_flash_version is not None:
                    new_ver = await self._read_current_version(client, target_uri)
                    if new_ver and new_ver != pre_flash_version:
                        log.info("%s BMC task lost but %s version moved %r -> %r — "
                                 "accepting as success", server.ip, target,
                                 pre_flash_version, new_ver)
                        # version-delta rescue: continue as if poll succeeded
                    else:
                        return Result(ok=False, message=msg)
                else:
                    return Result(ok=False, message=msg)

            on_progress("rebooting", 85)

            # Apply timing. BIOS images are staged and take effect on the next
            # host reboot. If the operator chose "apply now", trigger the host
            # reset; otherwise leave the host running and let them reboot on
            # their own schedule. (BMC firmware self-applies via a BMC reset
            # regardless, so apply_now is a no-op for BMC.)
            reset_note = ""
            if target == "BIOS" and apply_now:
                reset_target = (
                    getattr(cap, "system_reset_target", None) if cap is not None else None
                )
                if not reset_target:
                    sys_uri = await self._first_system_uri(client)
                    reset_target = sys_uri  # _reset_to_apply will append /Actions/ComputerSystem.Reset
                if reset_target:
                    ok_reset, msg_reset = await self._reset_to_apply(
                        client, reset_target, cap,
                    )
                    reset_note = (f" Host reset: {msg_reset}." if ok_reset
                                  else f" Staged OK, but host reset failed ({msg_reset}) — reboot manually to apply.")
            await asyncio.sleep(2.0)

            on_progress("verifying", 95)
            try:
                verify = await client.get_json(target_uri)
                actual = verify.get("FirmwareVersion") if target == "BMC" else verify.get("BiosVersion")
            except RedfishError:
                # BMC may still be coming back up after a reset; treat the
                # successful task as authoritative rather than failing.
                # Drop the cached cap so the next action re-discovers a
                # post-flash BMC whose inventory IDs may have renumbered.
                self._invalidate_capability_map(server)
                return Result(ok=True, version_after=firmware.version,
                              message=("Flashed." + reset_note).strip())

            # A successful flash means the BMC's view of itself has shifted;
            # drop the cap so subsequent actions re-walk and pick up the
            # new active-bank IDs / versions.
            self._invalidate_capability_map(server)

            # Real BIOS version strings ("BIOS Date: 03/05/2025 Ver 3.0a.V1")
            # rarely equal the firmware's bare version, and BIOS applies on
            # reset so the live value won't change until reboot. Treat a
            # Completed task as success; surface the read-back as info.
            if target == "BIOS":
                note = (reset_note or " Staged — applies on next reboot.").strip()
                return Result(ok=True, version_after=actual or firmware.version, message=note)
            if actual and firmware.version and actual != firmware.version:
                log.info("%s BMC version read-back %r (expected %r)",
                         server.ip, actual, firmware.version)
            return Result(ok=True, version_after=actual or firmware.version)
        finally:
            await client.close()

    @staticmethod
    async def _first_system_uri(client: RedfishClient) -> str | None:
        try:
            coll = await client.get_json("/redfish/v1/Systems")
            members = coll.get("Members") or []
            return members[0]["@odata.id"] if members else None
        except RedfishError:
            return None

    async def get_storage_inventory(
        self, base_url: str, creds: Credentials,
    ) -> list[Drive]:
        """Walk ``/redfish/v1/Systems/{id}/Storage`` and return every Drive.

        Returns an empty list on any failure — storage discovery is
        best-effort during the scan; the disk resolver / preview screen
        can re-poll on demand if a server's inventory is missing.
        """
        scheme, _, hostport = base_url.partition("://")
        host, _, port_s = hostport.partition(":")
        port = int(port_s) if port_s else (443 if scheme == "https" else 80)
        client = RedfishClient(
            host, port=port, scheme=scheme,
            username=creds.username, password=creds.password,
            timeout=10.0,
        )
        try:
            try:
                systems = await client.get_json("/redfish/v1/Systems")
            except RedfishError:
                return []
            members = systems.get("Members") or []
            if not members:
                return []
            system = await client.get_json(members[0]["@odata.id"])
            storage_link = (system.get("Storage") or {}).get("@odata.id")
            if not storage_link:
                return []
            try:
                storage_coll = await client.get_json(storage_link)
            except RedfishError:
                return []
            # Collect all drives first (raw, may contain name collisions).
            raw: list[Drive] = []
            for ctrl_ref in storage_coll.get("Members") or []:
                try:
                    ctrl = await client.get_json(ctrl_ref["@odata.id"])
                except RedfishError:
                    continue
                for drive_ref in ctrl.get("Drives") or []:
                    drive_uri = drive_ref.get("@odata.id") if isinstance(drive_ref, dict) else None
                    if not drive_uri:
                        continue
                    try:
                        drv = await client.get_json(drive_uri)
                    except RedfishError:
                        continue
                    parsed = _drive_from_redfish_json(drv)
                    if parsed is not None:   # skip absent/empty bays
                        raw.append(parsed)

            # Resolve name collisions.
            # A collision means the BMC reports two *physically different* drives
            # under the same bay name — common on dual-enclosure SAS setups where
            # both enclosures share Redfish bay numbering (e.g. Disk.Bay.3 can be
            # both a 600 GB 15K SAS in enc-0 AND a 16 TB capacity drive in enc-1).
            # We KEEP both but make the name unique so exactly ONE chip lights up
            # per click: append human-readable size to disambiguate.
            # Truly identical drives (same URI, same capacity, same serial) are
            # genuine firmware duplicates and are silently collapsed to one entry.
            from collections import Counter
            import dataclasses as _dc

            name_count: Counter = Counter(d.name for d in raw)
            # Set of final display names already handed out — used to GUARANTEE
            # uniqueness for triple-or-more collisions (the old counter logic
            # could emit two drives with the identical "name (960 GB, 1)" label,
            # so an operator override bound to the wrong physical disk).
            emitted: set[str] = set()
            result: list[Drive] = []
            seen_uris: set[str] = set()

            for d in raw:
                # Skip firmware-level duplicates (identical URI already processed)
                if d.redfish_uri and d.redfish_uri in seen_uris:
                    continue
                if d.redfish_uri:
                    seen_uris.add(d.redfish_uri)

                if name_count[d.name] > 1:
                    # Ambiguous bay name — append size, then serial tail, then a
                    # numeric counter, looping until the label is genuinely unique.
                    gb = d.capacity_bytes / 1_000_000_000 if d.capacity_bytes else 0
                    size_label = (
                        f"{gb / 1000:.1f} TB" if gb >= 1000
                        else (f"{gb:.0f} GB" if gb >= 1 else "?")
                    )
                    final = f"{d.name} ({size_label})"
                    if final in emitted:
                        tail = d.serial[-6:] if d.serial else ""
                        final = f"{d.name} ({size_label}, {tail})" if tail else final
                        n = 2
                        while final in emitted:
                            final = f"{d.name} ({size_label}) #{n}"
                            n += 1
                    emitted.add(final)
                    d = _dc.replace(d, name=final)
                else:
                    emitted.add(d.name)

                result.append(d)

            # Sort by name so the order is deterministic across refreshes
            return sorted(result, key=lambda d: d.name)
        finally:
            await client.close()

    # ---- v1.1 OS provisioning: Virtual Media + boot + power ---------------

    # Default DMTF Virtual Media slot name — used only as a legacy fallback
    # when the CapabilityMap couldn't enumerate VirtualMedia slots. With a
    # cap, the slot is selected by its advertised ``MediaTypes`` (``CD`` for
    # ISO) and ``_VM_SLOT`` is ignored entirely. Dell's ``RemovableDisk``
    # override falls out automatically because the discoverer picks the
    # slot whose MediaTypes include ``CD``.
    _VM_SLOT = "CD"

    async def _first_manager_uri(self, client: RedfishClient) -> str | None:
        managers = await client.get_json("/redfish/v1/Managers")
        members = managers.get("Members") or []
        if not members:
            return None
        return members[0]["@odata.id"]

    # Optical-media tokens. AMI MegaRAC frequently advertises only "DVD"
    # for the slot we want to use for ISOs (no "CD" token), and Lenovo XCC
    # sometimes uses both. Treat any of these as "this slot accepts an ISO".
    _OPTICAL_MEDIA_TOKENS: tuple[str, ...] = ("CD", "DVD", "CDROM", "DVDROM")

    @staticmethod
    def _select_virtualmedia_slot(
        cap: object | None, want_media: str, *, inserted: bool | None = None,
    ) -> object | None:
        """Pick a Virtual Media slot whose advertised MediaTypes include
        ``want_media`` (or, for ISOs, any optical token — AMI BMCs advertise
        only ``DVD``, Dell ``RemovableDisk`` slots list ``CD,DVD``, etc.).

        Returns the slot dataclass or None. When ``inserted`` is specified
        only slots in that state are considered (used by mount to find an
        empty slot, by eject to find an occupied one).
        """
        if cap is None:
            return None
        slots = getattr(cap, "virtualmedia_uris", None) or {}
        accept = (
            GenericRedfishPlugin._OPTICAL_MEDIA_TOKENS
            if want_media.upper() in ("CD", "DVD", "ISO")
            else (want_media,)
        )
        for slot in slots.values():
            media_types = tuple((m or "").upper() for m in (getattr(slot, "media_types", ()) or ()))
            if not any(tok.upper() in media_types for tok in accept):
                continue
            if inserted is not None and bool(getattr(slot, "inserted", False)) != inserted:
                continue
            return slot
        # Loosen the constraint when nothing matched the inserted-state
        # filter — still prefer a slot of the right media type.
        if inserted is not None:
            return GenericRedfishPlugin._select_virtualmedia_slot(
                cap, want_media, inserted=None,
            )
        return None

    # How long to poll the VirtualMedia slot for `Inserted: true` after an
    # InsertMedia POST returns 200. Many BMCs accept the action then take
    # 5-30s to actually attach the media; issuing set_boot_once before that
    # races the attach. 60s is generous; AMI/Dell typically settle in <15.
    _VM_INSERT_POLL_TIMEOUT_S = 60.0
    _VM_INSERT_POLL_INTERVAL_S = 2.0

    async def _wait_for_media_inserted(
        self, client: RedfishClient, slot_uri: str,
    ) -> bool:
        """Poll the VirtualMedia slot until ``Inserted: True``.

        Returns True on success, False on timeout. Treats transient
        ``RedfishError`` as "BMC busy, keep polling" — the slot resource is
        sometimes briefly 503/500 while the attach completes.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._VM_INSERT_POLL_TIMEOUT_S
        while loop.time() < deadline:
            try:
                state = await client.get_json(slot_uri)
                if bool(state.get("Inserted")):
                    return True
            except RedfishError:
                pass
            await asyncio.sleep(self._VM_INSERT_POLL_INTERVAL_S)
        return False

    async def mount_iso(
        self, server: ServerInfo, image_url: str, creds: Credentials,
    ) -> Result:
        """Insert an ISO into a Virtual Media slot.

        Always ejects any pre-existing media first — Supermicro/AMI BMCs
        return HTTP 400 on InsertMedia when a slot is occupied. After a
        successful POST, polls the slot resource until ``Inserted: True``
        before returning success — otherwise ``set_boot_once`` (called by
        the provisioning flow next) races the attach and the host boots
        from local disk instead of the ISO.
        """
        client = self._client_for(server, creds)
        try:
            cap = await self._capability_map(server, client)

            # Eject any pre-existing media universally (not just HPE) — the
            # operation is idempotent on every BMC we've audited and avoids
            # the "second mount silently fails" failure mode.
            occupied = self._select_virtualmedia_slot(cap, "CD", inserted=True)
            if occupied is not None:
                eject_action = (
                    getattr(occupied, "eject_action", None)
                    or f"{getattr(occupied, 'uri', '')}/Actions/VirtualMedia.EjectMedia"
                )
                if eject_action:
                    try:
                        await client.post_json(eject_action, {})
                    except RedfishError as exc:
                        # Best-effort eject — don't block insert on a missing slot
                        log.info("Pre-mount eject on %s skipped: %s", server.ip, exc)

            slot = self._select_virtualmedia_slot(cap, "CD", inserted=False)
            if slot is not None:
                insert_action = (
                    getattr(slot, "insert_action", None)
                    or f"{getattr(slot, 'uri', '')}/Actions/VirtualMedia.InsertMedia"
                )
                if not insert_action:
                    return Result(ok=False, message="VirtualMedia slot has no InsertMedia action")
                body = {"Image": image_url, "Inserted": True, "WriteProtected": True}
                try:
                    resp = await client.post_json(insert_action, body)
                except RedfishError as exc:
                    return Result(ok=False, message=str(exc))
                if resp["status"] >= 400:
                    return Result(ok=False, message=f"InsertMedia HTTP {resp['status']}")

                # Wait for the BMC to confirm Inserted: True before returning,
                # so the next step (set_boot_once + power_cycle) doesn't race
                # the attach. Some BMCs take 10-30s.
                slot_uri = getattr(slot, "uri", "")
                if slot_uri:
                    if await self._wait_for_media_inserted(client, slot_uri):
                        return Result(ok=True, message="ISO inserted and attached")
                    # Attach NOT confirmed — fail rather than let set_boot_once +
                    # power_cycle race the attach and boot the local disk.
                    return Result(ok=False,
                                  message="ISO insert not confirmed in 60s — aborting "
                                          "before power-cycle to avoid booting local disk")
                return Result(ok=True, message="ISO inserted")
            # Legacy fallback: no cap or no usable slot. Use the historical
            # ``Managers[0]/VirtualMedia/<_VM_SLOT>`` path — preserves
            # behavior on simulators and on BMCs where the cap walk
            # couldn't enumerate VirtualMedia.
            mgr_uri = await self._first_manager_uri(client)
            if not mgr_uri:
                return Result(ok=False, message="No Managers collection")
            vm_base = f"{mgr_uri}/VirtualMedia/{self._VM_SLOT}"
            # Best-effort eject-first on the legacy path too
            try:
                await client.post_json(f"{vm_base}/Actions/VirtualMedia.EjectMedia", {})
            except RedfishError:
                pass
            vm_action = f"{vm_base}/Actions/VirtualMedia.InsertMedia"
            body = {"Image": image_url, "Inserted": True, "WriteProtected": True}
            resp = await client.post_json(vm_action, body)
            if resp["status"] >= 400:
                return Result(ok=False, message=f"InsertMedia HTTP {resp['status']}")
            # Poll the slot resource for Inserted=True on the legacy path
            if await self._wait_for_media_inserted(client, vm_base):
                return Result(ok=True, message="ISO inserted and attached")
            return Result(ok=True, message="ISO inserted (attach not confirmed in 60s)")
        except RedfishError as exc:
            return Result(ok=False, message=str(exc))
        finally:
            await client.close()

    async def eject_iso(
        self, server: ServerInfo, creds: Credentials,
    ) -> Result:
        client = self._client_for(server, creds)
        try:
            cap = await self._capability_map(server, client)
            slot = self._select_virtualmedia_slot(cap, "CD", inserted=True)
            if slot is not None:
                eject_action = (
                    getattr(slot, "eject_action", None)
                    or f"{getattr(slot, 'uri', '')}/Actions/VirtualMedia.EjectMedia"
                )
                if eject_action:
                    try:
                        await client.post_json(eject_action, {})
                    except RedfishError as exc:
                        # Eject is idempotent: a real 400/404 "no media" is
                        # success. Check the typed HTTP status, not a substring
                        # of the message (a host on :4000 or a body containing
                        # "404" used to mask a 500/TLS failure as success).
                        if getattr(exc, "status", 0) in (400, 404):
                            return Result(ok=True, message="no media to eject")
                        return Result(ok=False, message=str(exc))
                    return Result(ok=True, message="ejected")
            # Legacy fallback (no cap / no enumerated slot).
            mgr_uri = await self._first_manager_uri(client)
            if not mgr_uri:
                return Result(ok=False, message="No Managers collection")
            vm_action = (f"{mgr_uri}/VirtualMedia/{self._VM_SLOT}"
                         "/Actions/VirtualMedia.EjectMedia")
            try:
                await client.post_json(vm_action, {})
            except RedfishError as exc:
                if getattr(exc, "status", 0) in (400, 404):
                    return Result(ok=True, message="no media to eject")
                return Result(ok=False, message=str(exc))
            return Result(ok=True, message="ejected")
        finally:
            await client.close()

    async def set_boot_once(
        self, server: ServerInfo, target: BootTarget, creds: Credentials,
    ) -> Result:
        client = self._client_for(server, creds)
        try:
            cap = await self._capability_map(server, client)
            # cap.system_boot_patch_uri is the resource to PATCH; on every
            # audited BMC this equals cap.system_uri but the field is
            # explicit so older Lenovo XCC builds (which expose Boot as a
            # separate child resource) work without an OEM override.
            patch_path = (
                getattr(cap, "system_boot_patch_uri", None)
                if cap is not None else None
            )
            if not patch_path:
                # Legacy: walk /Systems and PATCH the first member.
                sys_coll = await client.get_json("/redfish/v1/Systems")
                members = sys_coll.get("Members") or []
                if not members:
                    return Result(ok=False, message="No Systems collection")
                patch_path = members[0]["@odata.id"]
            boot_props = {
                "BootSourceOverrideEnabled": "Once",
                "BootSourceOverrideTarget": target.value,
                "BootSourceOverrideMode": "UEFI",
            }
            # When PATCHing the System resource, Boot is a nested object; when
            # the cap pointed us at a Boot CHILD resource (some Lenovo XCC
            # builds expose Boot separately), send the bare properties — wrapping
            # them in {"Boot": ...} there makes spec-tolerant BMCs 200-OK while
            # silently ignoring the override (host then boots the local disk).
            sys_uri = getattr(cap, "system_uri", None) if cap is not None else None
            body = boot_props if (sys_uri and patch_path != sys_uri) else {"Boot": boot_props}
            # Use the authenticated client path (token/Basic + OData headers +
            # one-shot 401 re-login). The old raw ``session.patch`` sent no
            # auth and 401'd on every real BMC, silently aborting provisioning
            # at the set-boot stage.
            resp = await client.patch_json(patch_path, body)
            if resp["status"] >= 400:
                return Result(ok=False, message=f"PATCH boot HTTP {resp['status']}")
            return Result(ok=True, message=f"boot-once = {target.value}")
        except RedfishError as exc:
            return Result(ok=False, message=str(exc))
        finally:
            await client.close()

    async def power_cycle(
        self, server: ServerInfo, creds: Credentials,
    ) -> Result:
        client = self._client_for(server, creds)
        try:
            cap = await self._capability_map(server, client)
            reset_target = (
                getattr(cap, "system_reset_target", None)
                if cap is not None else None
            )
            if not reset_target:
                # Legacy: walk /Systems and concat the reset action.
                sys_coll = await client.get_json("/redfish/v1/Systems")
                members = sys_coll.get("Members") or []
                if not members:
                    return Result(ok=False, message="No Systems collection")
                sys_uri = members[0]["@odata.id"]
                reset_target = f"{sys_uri}/Actions/ComputerSystem.Reset"
            reset_type = self._choose_reset_type(
                cap, self._POWER_CYCLE_RESET_TYPE_PRIORITY, fallback="ForceRestart",
            )
            resp = await client.post_json(reset_target, {"ResetType": reset_type})
            if resp["status"] >= 400:
                return Result(ok=False, message=f"Reset HTTP {resp['status']}")
            return Result(ok=True, message="power-cycle issued")
        except RedfishError as exc:
            return Result(ok=False, message=str(exc))
        finally:
            await client.close()

    async def power_action(
        self, server: ServerInfo, action: str, creds: Credentials,
    ) -> Result:
        """Issue a power action via ComputerSystem.Reset, choosing a ResetType
        the BMC advertises. See ``_POWER_ACTION_RESETTYPES`` for the mapping."""
        spec = self._POWER_ACTION_RESETTYPES.get((action or "").strip().lower())
        if spec is None:
            return Result(ok=False, message=f"unknown power action '{action}'")
        priority, fallback = spec
        client = self._client_for(server, creds)
        try:
            cap = await self._capability_map(server, client)
            reset_target = (
                getattr(cap, "system_reset_target", None) if cap is not None else None
            )
            if not reset_target:
                sys_uri = await self._first_system_uri(client)
                if not sys_uri:
                    return Result(ok=False, message="No Systems collection")
                reset_target = f"{sys_uri}/Actions/ComputerSystem.Reset"
            reset_type = self._choose_reset_type(cap, priority, fallback=fallback)
            resp = await client.post_json(reset_target, {"ResetType": reset_type})
            if resp["status"] >= 400:
                return Result(ok=False, message=f"Reset HTTP {resp['status']}")
            return Result(ok=True, message=f"{reset_type} issued")
        except RedfishError as exc:
            return Result(ok=False, message=str(exc))
        finally:
            await client.close()

    async def get_power_state(
        self, server: ServerInfo, creds: Credentials,
    ) -> str | None:
        client = self._client_for(server, creds)
        try:
            cap = await self._capability_map(server, client)
            sys_uri = getattr(cap, "system_uri", None) if cap is not None else None
            if not sys_uri:
                sys_uri = await self._first_system_uri(client)
            if not sys_uri:
                return None
            s = await client.get_json(sys_uri)
            return s.get("PowerState")
        except RedfishError:
            return None
        finally:
            await client.close()

    @staticmethod
    def _client_for(server: ServerInfo, creds: Credentials) -> RedfishClient:
        """Build a Redfish client for the given server, carrying the BMC
        creds. Without ``creds`` the eventual POST to
        ``/UpdateService/Actions/SimpleUpdate`` would 401 against any
        authenticated BMC (Dell iDRAC, HPE iLO, Lenovo XCC, …)."""
        root = (server.redfish_root or "").replace("/redfish/v1/", "").rstrip("/")
        scheme, _, hostport = root.partition("://")
        host, _, port_s = hostport.partition(":")
        port = int(port_s) if port_s else (443 if scheme == "https" else 80)
        return RedfishClient(
            host, port=port, scheme=scheme, timeout=15.0,
            username=creds.username if creds else "",
            password=creds.password if creds else "",
            tls_fingerprint=server.tls_fingerprint,   # enforces TOFU pin
        )


PLUGIN = GenericRedfishPlugin()
