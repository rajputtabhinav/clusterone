"""Redfish CapabilityMap + Discoverer.

One bounded walk per BMC produces a typed snapshot every plugin reads. The
discoverer is the ONLY code in the system that hardcodes ``/redfish/v1/`` —
everything else flows from ``@odata.id`` references followed during the walk.

Design rationale
----------------
Across the six AMI MegaRAC FirmwareInventory variants we audited (plus
Supermicro, Dell, HPE, Lenovo, ASRock Rack), every OEM advertises its own
active-bank naming, OEM install actions, and Virtual Media slot names. The
old approach — every plugin re-walks ``/UpdateService`` and
``/FirmwareInventory`` on every flash and pre-flight, with hardcoded
``/Systems/1`` and ``/Managers`` paths sprinkled in half a dozen
places — produces bugs like RED097 (rollback bank flashed because the
substring matcher picked up ``BIOSImage2``) and forces a new constant for
every new OEM.

:class:`CapabilityMap` inverts that: one walk per server (≤8 GETs in the
happy path; every step has a 404 fallback) produces a typed snapshot. The
discoverer encodes the hard-won knowledge once:

* **Banned rollback banks**: ``backup``, ``golden``, ``staging``,
  ``recovery``, ``history``, ``image2``, ``image_2`` are never selected
  as the active bank.
* **OEM install actions** discovered from ``UpdateService.Actions.Oem`` —
  automatically covers Group 1 ``UploadCABundle``, Group 2
  ``AMIUpdateService`` variants, Group 6 ``BMCFwUpdate`` +
  ``UploadFirmwareImage``, Supermicro ``SmcUpdateService.Install``,
  HPE ``HpeiLOUpdateServiceExt.AddFromUri`` — without any per-plugin
  code.
* **Virtual Media slot selection** by ``MediaTypes`` rather than by
  name — kills Dell's ``_VM_SLOT = 'RemovableDisk'`` override.
* **Reset types** chosen from ``ResetType@Redfish.AllowableValues`` —
  kills Supermicro's hardcoded ``/Systems/1/Actions/...``.

Plugins receive the map on every call (attached to ``ServerInfo`` as
``server.capabilities``; the repository persists it as JSON). The only
literal path in the codebase becomes ``/redfish/v1/`` inside
:meth:`RedfishCapabilityDiscoverer._bootstrap` below.

Tolerant by design
------------------
Every step beyond the bootstrap GET handles HTTP 404 gracefully — emits a
debug log, appends a string to :attr:`CapabilityMap.discovery_warnings`,
and leaves the corresponding field at its default ``None`` / empty value.
Discovery NEVER raises on a missing optional endpoint; only the bootstrap
GET (``/redfish/v1/``) is allowed to fail hard.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from core.plugins_api.redfish_client import RedfishClient, RedfishError

log = logging.getLogger(__name__)

# The only hardcoded path in the system. Every other URI flows from
# @odata.id references read during the walk.
_SERVICE_ROOT_PATH = "/redfish/v1/"

# Rollback / recovery / staging bank suffixes that must NEVER be selected
# as the active firmware target. Flashing one of these would destroy the
# operator's rollback option — exactly the RED097 failure mode.
_BANNED_BANK_LEAVES: tuple[str, ...] = (
    "backup", "golden", "staging", "recovery", "history",
    "image2", "image_2",
)

# Canonical FirmwareInventory leaf priority. Order matters: the first
# matching leaf in this list wins, so we prefer the canonical "BIOS" over
# AMI's "BIOSImage1". Mirrors the priority list inside the existing
# GenericRedfishPlugin._resolve_firmware_target so behavior is identical.
_BIOS_CANDIDATES: tuple[str, ...] = (
    "BIOS", "Bios", "SystemBios", "SystemBIOS", "UEFI", "Uefi",
    "BIOSImage1", "BIOSImage_1",
)
_BMC_CANDIDATES: tuple[str, ...] = (
    "BMC", "BMC-Primary", "BMCActive", "BMC_Active",
    "BMCImage1", "BMCImage_1",
    "iDRAC", "iLO", "iLO5", "iLO6", "XCC", "Manager",
)

# Substring tokens used in the fallback classifier when no leaf matched a
# canonical candidate. Same tokens used by the existing plugin.
_BIOS_TOKENS: tuple[str, ...] = ("bios", "uefi")
_BMC_TOKENS: tuple[str, ...] = ("bmc", "idrac", "ilo", "xcc", "manager")
_CPLD_TOKENS: tuple[str, ...] = ("cpld",)
_FPGA_TOKENS: tuple[str, ...] = ("fpga",)
_BP_TOKENS: tuple[str, ...] = ("backplane", "bp")
_PSU_TOKENS: tuple[str, ...] = ("psu", "powersupply", "power_supply")
_RETIMER_TOKENS: tuple[str, ...] = ("retimer",)

# HGX baseboard tokens — anything carrying these in the FirmwareInventory
# leaf is part of an NVIDIA HGX accelerator baseboard. The classifier groups
# every HGX/GPU member into a separate dictionary (``cap.hgx_member_uris``)
# so the plugin can target each die / ERoT / InfoROM independently.
#
# * ``hgx`` — the AMI MegaRAC prefix on every HGX firmware ID
#   (HGX_FW_GPU_0..7, HGX_FW_ERoT_*, HGX_InfoROM_*, HGX_FW_BMC_0, …).
# * ``hmc`` — HGX Management Controller (the on-board "BMC for the HGX").
# * ``gpu`` — GPU die / fabric firmware (FW_GPU_0..7 on AMI builds that
#   don't prefix every entry with HGX_).
# * ``erot`` — Externally-Routable Open Tracer, the security IC on every
#   HGX board (one per major component).
# * ``inforom`` — NVIDIA InfoROM, the per-GPU non-volatile config blob.
_HGX_TOKENS: tuple[str, ...] = ("hgx", "hmc")
_GPU_TOKENS: tuple[str, ...] = ("gpu",)
_EROT_TOKENS: tuple[str, ...] = ("erot",)
_INFOROM_TOKENS: tuple[str, ...] = ("inforom",)


@dataclass(frozen=True)
class FirmwareMember:
    """One member of ``/UpdateService/FirmwareInventory``.

    The discoverer GETs every member once and classifies it; downstream
    plugins read ``firmware_inventory[id]`` instead of re-walking. Frozen
    because we want hash-stable copies for caching on ``ServerInfo``.
    """
    uri: str
    version: str | None = None
    updateable: bool = True
    software_id: str | None = None
    related_item_uris: tuple[str, ...] = ()
    # False when the leaf matches a BANNED rollback/recovery/staging name.
    # Plugins MUST NOT flash an inactive bank by default.
    is_active_bank: bool = True
    # Coarse classification used by extra_firmware_members and the
    # active-bank lookup. One of: bios, bmc, cpld, fpga, bp, psu, retimer,
    # other.
    component_class: str = "other"


@dataclass(frozen=True)
class VirtualMediaSlot:
    """One slot of ``/Managers/<id>/VirtualMedia/<slot>``.

    Captures the action targets and the slot's advertised ``MediaTypes`` so
    ``mount_iso`` can pick the right slot by content type ('CD' for ISO,
    'USBStick' for img) instead of by hardcoded slot name. This is what
    kills the Dell ``_VM_SLOT = 'RemovableDisk'`` override — Dell's
    RemovableDisk slot advertises ``MediaTypes = ['CD','DVD']`` so it gets
    picked the same way a generic CD slot would.
    """
    uri: str
    media_types: tuple[str, ...] = ()
    insert_action: str | None = None
    eject_action: str | None = None
    transfer_method: str = ""
    transfer_protocol_types: tuple[str, ...] = ()
    inserted: bool = False
    write_protected: bool = True


@dataclass
class CapabilityMap:
    """Typed snapshot of what a single BMC actually exposes.

    Every field defaults to ``None`` / empty so a partial walk (BMC with no
    UpdateService, BMC with no VirtualMedia, etc.) still produces a usable
    map. Plugins check ``cap.<field> is None`` before issuing the
    corresponding action and surface a clean error instead of crashing.
    """
    # -- Service root identity --------------------------------------------
    service_root: str = _SERVICE_ROOT_PATH
    redfish_version: str | None = None
    vendor_hint: str | None = None
    oem_blocks: tuple[str, ...] = ()

    # -- UpdateService surface --------------------------------------------
    update_service_uri: str | None = None
    update_service_enabled: bool = True
    multipart_push_uri: str | None = None
    http_push_uri: str | None = None
    simple_update_target: str | None = None
    simple_update_allowed_transfer_protocols: tuple[str, ...] = ()
    start_update_target: str | None = None
    oem_install_actions: dict[str, str] = field(default_factory=dict)
    oem_update_flags: dict[str, object] = field(default_factory=dict)

    # -- FirmwareInventory ------------------------------------------------
    firmware_inventory_uri: str | None = None
    firmware_inventory: dict[str, FirmwareMember] = field(default_factory=dict)
    bios_member_uri: str | None = None
    bmc_member_uri: str | None = None
    extra_firmware_members: dict[str, str] = field(default_factory=dict)
    # HGX baseboard / GPU firmware members keyed by canonical leaf name
    # (e.g. ``HGX_FW_GPU_0`` -> ``/redfish/v1/UpdateService/FirmwareInventory/HGX_FW_GPU_0``).
    # Skips banned banks (Image2/Backup/etc.) so the operator can't
    # accidentally flash a recovery slot. Multiple GPUs produce multiple
    # entries; the plugin's update_hgx() routes by substring match against
    # the firmware filename.
    hgx_member_uris: dict[str, str] = field(default_factory=dict)

    # -- Tasks ------------------------------------------------------------
    task_service_uri: str | None = None
    task_collection_uri: str | None = None

    # -- Systems / boot / power -------------------------------------------
    system_uri: str | None = None
    system_reset_target: str | None = None
    system_reset_allowed_resettypes: tuple[str, ...] = ()
    system_boot_patch_uri: str | None = None
    system_power_state: str = "Unknown"

    # -- Managers / VirtualMedia ------------------------------------------
    manager_uri: str | None = None
    manager_reset_target: str | None = None
    virtualmedia_collection_uri: str | None = None
    virtualmedia_uris: dict[str, VirtualMediaSlot] = field(default_factory=dict)

    # -- Session / auth ---------------------------------------------------
    session_service_uri: str | None = None

    # -- Diagnostics ------------------------------------------------------
    raw_action_info: dict[str, dict] = field(default_factory=dict)
    discovery_warnings: tuple[str, ...] = ()
    discovered_at: float = 0.0


def _classify_component(leaf: str) -> str:
    """Map a FirmwareInventory member leaf-name to a coarse class.

    Order matters: BIOS/UEFI is checked first because some BMCs name a
    chipset region ``SystemBios`` which would otherwise be mis-classified
    by the substring fallback. Returns ``other`` when nothing matches —
    those entries still land in :attr:`CapabilityMap.firmware_inventory`
    so the operator can see them, but they don't fill
    :attr:`bios_member_uri` or :attr:`bmc_member_uri`.
    """
    leaf_l = leaf.lower()
    if any(t in leaf_l for t in _BIOS_TOKENS):
        return "bios"
    if any(t in leaf_l for t in _BMC_TOKENS):
        return "bmc"
    if any(t in leaf_l for t in _CPLD_TOKENS):
        return "cpld"
    if any(t in leaf_l for t in _FPGA_TOKENS):
        return "fpga"
    if any(t in leaf_l for t in _BP_TOKENS):
        return "bp"
    if any(t in leaf_l for t in _PSU_TOKENS):
        return "psu"
    if any(t in leaf_l for t in _RETIMER_TOKENS):
        return "retimer"
    return "other"


def _is_active_bank(leaf: str) -> bool:
    """False when the leaf name matches a BANNED rollback/recovery suffix.

    The banned list is exactly the one the existing
    ``GenericRedfishPlugin._resolve_firmware_target`` uses, so behavior is
    preserved bit-for-bit. ``image2`` / ``image_2`` is the AMI MegaRAC
    secondary slot — flashing it destroys the operator's rollback path.
    """
    leaf_l = leaf.lower()
    return not any(b in leaf_l for b in _BANNED_BANK_LEAVES)


def _is_hgx_member(leaf: str) -> bool:
    """True when the leaf names an HGX baseboard / GPU firmware entry.

    Catches every variant we've seen in the field: ``HGX_FW_GPU_*``,
    ``HGX_FW_ERoT_*``, ``HGX_InfoROM_*``, ``HGX_FW_HMC_*``,
    ``HGX_FW_BMC_0`` (the HGX-side BMC, NOT the host BMC), plus the
    AMI variants that drop the HGX prefix: ``FW_GPU_0..7``,
    ``InfoROM_GPU_0..7``. The discoverer routes these into a separate
    dictionary so the host BIOS/BMC classification stays clean — the
    audit at ``audit_update_apis.json`` shows .193 publishes 28 such
    entries side-by-side with the host BIOS and BMC.
    """
    leaf_l = leaf.lower()
    return (
        any(t in leaf_l for t in _HGX_TOKENS)
        or any(t in leaf_l for t in _GPU_TOKENS)
        or any(t in leaf_l for t in _EROT_TOKENS)
        or any(t in leaf_l for t in _INFOROM_TOKENS)
    )


class RedfishCapabilityDiscoverer:
    """Walks the Redfish tree once and returns a typed :class:`CapabilityMap`.

    Construction takes the connected :class:`RedfishClient`; the client's
    auth + TLS configuration carries through every GET. Call
    :meth:`discover` to perform the walk.

    The 9-step walk:

    1. Bootstrap ``/redfish/v1/`` (the only hardcoded path).
    2. UpdateService → push URIs, OEM install actions, flags.
    3. FirmwareInventory → classify members, resolve active banks.
    4. ActionInfo blobs → cache AllowableValues for downstream callers.
    5. Systems[0] → reset target, allowed reset types, boot URI, power.
    6. Managers[0] → manager URI, reset target, FirmwareVersion fallback.
    7. VirtualMedia collection → slot dict keyed by ID.
    8. TaskService → task collection URI for progress polling.
    9. SessionService → token-auth fallback hint.

    Steps 2–9 each handle 404 gracefully; only step 1 is allowed to fail.
    """

    def __init__(self, client: RedfishClient) -> None:
        self._client = client
        self._cap = CapabilityMap()
        self._warnings: list[str] = []
        self._oem_block_set: set[str] = set()
        # One-shot: a second discover() on the same instance would mix
        # state from two walks (warnings accumulate, oem_blocks dedupe
        # across runs, etc.). Construct a fresh discoverer per walk.
        self._consumed: bool = False

    async def discover(self) -> CapabilityMap:
        """Execute the walk. Returns a populated :class:`CapabilityMap`.

        Raises :class:`RedfishError` only if the service root itself is
        unreachable — every other endpoint contributes its piece on
        success and degrades to ``None`` on 404 / network failure.
        Raises :class:`RuntimeError` if called more than once on the
        same instance — construct a fresh ``RedfishCapabilityDiscoverer``
        per walk.
        """
        if self._consumed:
            raise RuntimeError(
                "RedfishCapabilityDiscoverer is one-shot; construct a fresh "
                "instance per walk (state from a previous discover() call "
                "would contaminate this one)."
            )
        self._consumed = True
        root = await self._bootstrap()
        await self._walk_update_service(root)
        await self._walk_systems(root)
        await self._walk_managers(root)
        await self._walk_task_service(root)
        await self._walk_session_service(root)

        # Freeze accumulated state into the immutable fields.
        self._cap.oem_blocks = tuple(sorted(self._oem_block_set))
        self._cap.discovery_warnings = tuple(self._warnings)
        # Wrap every mutable dict field in MappingProxyType so plugins
        # can read but not mutate the snapshot — prevents one plugin
        # from poisoning another plugin's view of the BMC. Docs/tests
        # treat the map as frozen-by-convention; this enforces it.
        from types import MappingProxyType
        self._cap.oem_install_actions = MappingProxyType(dict(self._cap.oem_install_actions))
        self._cap.oem_update_flags = MappingProxyType(dict(self._cap.oem_update_flags))
        self._cap.firmware_inventory = MappingProxyType(dict(self._cap.firmware_inventory))
        self._cap.extra_firmware_members = MappingProxyType(dict(self._cap.extra_firmware_members))
        self._cap.hgx_member_uris = MappingProxyType(dict(self._cap.hgx_member_uris))
        self._cap.virtualmedia_uris = MappingProxyType(dict(self._cap.virtualmedia_uris))
        self._cap.raw_action_info = MappingProxyType(dict(self._cap.raw_action_info))
        # Wall clock (UTC epoch) — NOT time.monotonic(). The map is
        # serialized to JSON and persisted on ServerInfo; monotonic time
        # is per-process and meaningless across restarts, so freshness
        # comparisons against time.monotonic() would be wildly wrong
        # after an app restart. time.time() survives.
        self._cap.discovered_at = time.time()
        return self._cap

    # ------------------------------------------------------------------
    # Step 1 — bootstrap
    # ------------------------------------------------------------------

    async def _bootstrap(self) -> dict:
        """GET /redfish/v1/. Hard failure on 404 — everything else needs it.

        This is the ONLY method allowed to hardcode a Redfish path. Every
        other URI in the discoverer flows from an ``@odata.id`` reference
        read out of the dict returned here.
        """
        try:
            root = await self._client.get_json(_SERVICE_ROOT_PATH)
        except RedfishError as exc:
            raise RedfishError(
                f"Redfish ServiceRoot unavailable at {_SERVICE_ROOT_PATH}: {exc}"
            ) from exc

        # service_root: use the resource's own @odata.id if it advertises
        # one (spec lets BMCs publish at alternate paths); fall back to
        # the literal we used to fetch it.
        self._cap.service_root = root.get("@odata.id") or _SERVICE_ROOT_PATH
        self._cap.redfish_version = root.get("RedfishVersion")

        # vendor_hint: prefer the explicit Vendor key; otherwise the first
        # key under Oem (Tyrone-rebadged Supermicros publish "Smc" or
        # "Supermicro" here, AMI Group-6 publishes "Ami", HPE "Hpe").
        vendor = root.get("Vendor")
        if not vendor:
            oem = root.get("Oem") or {}
            keys = list(oem.keys())
            vendor = keys[0] if keys else None
        self._cap.vendor_hint = vendor

        self._absorb_oem_keys(root)
        return root

    # ------------------------------------------------------------------
    # Step 2 — UpdateService
    # ------------------------------------------------------------------

    async def _walk_update_service(self, root: dict) -> None:
        """Resolve UpdateService URI and follow the link.

        Captures push URIs, OEM install actions, and OEM update flags. On
        404 / missing link, leaves every UpdateService field at its
        default (None / empty) and appends a warning. Discovery still
        succeeds — the map is usable for power and provisioning, just not
        for flashing.
        """
        link = self._odata_id(root.get("UpdateService"))
        if not link:
            self._warn("UpdateService missing — host treated as discovery-only")
            return
        us = await self._safe_get(link, "UpdateService")
        if us is None:
            return

        self._cap.update_service_uri = self._odata_id(us) or link
        # ServiceEnabled defaults to True per spec when the key is absent
        # — only an explicit False blocks flashing.
        self._cap.update_service_enabled = bool(us.get("ServiceEnabled", True))
        self._cap.multipart_push_uri = us.get("MultipartHttpPushUri") or None
        self._cap.http_push_uri = us.get("HttpPushUri") or None
        self._cap.firmware_inventory_uri = self._odata_id(us.get("FirmwareInventory"))
        self._absorb_oem_keys(us)

        actions = us.get("Actions") or {}
        await self._walk_update_actions(actions)
        await self._walk_firmware_inventory()
        self._derive_oem_update_flags(us)

    async def _walk_update_actions(self, actions: dict) -> None:
        """Resolve SimpleUpdate, StartUpdate, and every OEM install action.

        DMTF-standard actions land in their dedicated fields; everything
        under ``Actions.Oem`` is added to :attr:`oem_install_actions` so
        plugins look up by name (e.g.
        ``cap.oem_install_actions.get('#SmcUpdateService.Install')``)
        instead of hardcoding the target path. This is what lets the
        generic flow handle Supermicro, HPE, AMI Group 1/2/6, and Dell
        with one branch per OEM action name.
        """
        simple = actions.get("#UpdateService.SimpleUpdate") or {}
        if simple.get("target"):
            self._cap.simple_update_target = simple["target"]
            allowed = simple.get("TransferProtocol@Redfish.AllowableValues")
            if allowed:
                self._cap.simple_update_allowed_transfer_protocols = tuple(allowed)
            await self._fetch_action_info(simple, "SimpleUpdate")

        start = actions.get("#UpdateService.StartUpdate") or {}
        if start.get("target"):
            self._cap.start_update_target = start["target"]

        oem_actions = actions.get("Oem") or {}
        for name, blob in oem_actions.items():
            if isinstance(blob, dict) and blob.get("target"):
                # name carries the leading hash on most BMCs
                # (e.g. '#SmcUpdateService.Install'); preserve it
                # verbatim so plugins can match on the exact action key.
                self._cap.oem_install_actions[name] = blob["target"]

    async def _walk_firmware_inventory(self) -> None:
        """Walk FirmwareInventory members, classify, resolve active banks.

        For each member: GET the member resource, classify by leaf-name
        token, mark active vs banned-bank. The result fills
        :attr:`firmware_inventory` (everything seen) and the resolved
        :attr:`bios_member_uri` / :attr:`bmc_member_uri`
        / :attr:`extra_firmware_members` (active banks only).

        Priority: canonical leaf match wins, falls back to active-bank
        token match. Mirrors the existing
        ``GenericRedfishPlugin._resolve_firmware_target`` priority bit-for-bit.
        """
        inv_uri = self._cap.firmware_inventory_uri
        if not inv_uri:
            self._warn("No FirmwareInventory — flash targets cannot be resolved")
            return
        inv = await self._safe_get(inv_uri, "FirmwareInventory")
        if inv is None:
            return

        member_refs = list(inv.get("Members") or [])
        # Follow @odata.nextLink so a large (HGX) FirmwareInventory isn't
        # truncated to page 1 — otherwise a bundle flash silently skips the
        # GPUs/ERoTs whose members live on later pages.
        _next = inv.get("Members@odata.nextLink")
        _guard = 0
        while _next and _guard < 50:
            _guard += 1
            _page = await self._safe_get(_next, "FirmwareInventory page")
            if not _page:
                break
            member_refs.extend(_page.get("Members") or [])
            _next = _page.get("Members@odata.nextLink")
        for ref in member_refs:
            uri = self._odata_id(ref)
            if not uri:
                continue
            mb = await self._safe_get(uri, f"FirmwareInventory member {uri}")
            if mb is None:
                continue
            leaf = uri.rsplit("/", 1)[-1]
            related = tuple(
                self._odata_id(r) or ""
                for r in (mb.get("RelatedItem") or [])
                if self._odata_id(r)
            )
            member = FirmwareMember(
                uri=uri,
                version=mb.get("Version"),
                updateable=bool(mb.get("Updateable", True)),
                software_id=mb.get("SoftwareId"),
                related_item_uris=related,
                is_active_bank=_is_active_bank(leaf),
                component_class=_classify_component(leaf),
            )
            self._cap.firmware_inventory[leaf] = member

        # Resolve active banks per class using the priority list.
        self._cap.bios_member_uri = self._pick_active(
            "bios", _BIOS_CANDIDATES,
        )
        self._cap.bmc_member_uri = self._pick_active(
            "bmc", _BMC_CANDIDATES,
        )
        # Everything else: keep the first active-bank member per class so
        # the operator can target CPLD / FPGA / BP / PSU / RETIMER without
        # the plugin learning each class.
        for klass in ("cpld", "fpga", "bp", "psu", "retimer"):
            uri = self._pick_active(klass, ())
            if uri:
                self._cap.extra_firmware_members[klass] = uri

        # HGX baseboard / GPU members: separate pass so the host
        # bios_member_uri / bmc_member_uri stay correctly pointing at the
        # host firmware on systems (like .193 in the audit) that publish
        # 28+ HGX firmware members alongside the host BIOS/BMC. Each
        # entry preserves the canonical leaf name so the operator can
        # target ``HGX_FW_GPU_3`` distinctly from ``HGX_FW_GPU_4``.
        for leaf, mb in self._cap.firmware_inventory.items():
            if not _is_hgx_member(leaf):
                continue
            if not mb.is_active_bank:
                # Banned bank (Image2/Backup/etc.) — never flashable as
                # the active target. Matches the BIOS/BMC rule so the
                # operator can never accidentally hit a recovery slot.
                continue
            self._cap.hgx_member_uris[leaf] = mb.uri

    def _pick_active(
        self, component_class: str, priority_leaves: tuple[str, ...],
    ) -> str | None:
        """Choose the active-bank URI for a component class.

        Priority order:

        1. Canonical leaf names (e.g. ``BIOS`` beats ``BIOSImage1``).
        2. First active-bank member with the matching component_class.

        Returns ``None`` when no active-bank candidate exists — the
        caller surfaces a clean error instead of falling back to a banned
        bank.
        """
        # 1) Canonical leaf match in priority order.
        for cand in priority_leaves:
            mb = self._cap.firmware_inventory.get(cand)
            if mb and mb.is_active_bank and mb.component_class == component_class:
                return mb.uri
        # 2) First active member of the right class. Lexical sort over
        # leaf names gives deterministic output across discovery passes
        # (matters for snapshots persisted on ServerInfo).
        for leaf in sorted(self._cap.firmware_inventory.keys()):
            mb = self._cap.firmware_inventory[leaf]
            if mb.component_class == component_class and mb.is_active_bank:
                return mb.uri
        return None

    def _derive_oem_update_flags(self, us: dict) -> None:
        """Peel vendor-specific feature flags out of ``UpdateService.Oem``.

        Captures stable signals plugins use without re-walking the tree:

        * ``hpe.state_endpoint`` — HPE iLO polls ``Oem.Hpe.State`` on
          UpdateService itself, so we record the URI here.
        * ``ami.requires_image_type`` — True when any AMI / SMC OEM key
          is present; plugins inject the ``ImageType`` OemParameter.
        * ``hpe.has_repository`` — True when the HPE repo block exists.
        """
        oem = us.get("Oem") or {}
        flags: dict[str, object] = {}
        if "Hpe" in oem:
            flags["hpe.state_endpoint"] = self._cap.update_service_uri
            if "Repository" in (oem.get("Hpe") or {}) or "RepositoryUpdates" in (oem.get("Hpe") or {}):
                flags["hpe.has_repository"] = True
        if any(k in oem for k in ("Ami", "Smc", "Supermicro")):
            flags["ami.requires_image_type"] = True
        if "Dell" in oem:
            # Dell's iDRAC auto-routes by DUP header — Targets must be empty.
            flags["dell.empty_targets"] = True
        if flags:
            self._cap.oem_update_flags.update(flags)

    async def _fetch_action_info(self, action: dict, name: str) -> None:
        """Follow ``@Redfish.ActionInfo`` if present, cache the blob.

        Some BMCs put AllowableValues inline on the action; others
        publish an ActionInfo resource. Cache whatever we find under
        :attr:`raw_action_info` keyed by action target so plugins don't
        re-GET. 404 here is benign (spec marks ActionInfo optional) —
        callers fall back to DMTF defaults.
        """
        info_link = action.get("@Redfish.ActionInfo")
        target = action.get("target") or name
        if not info_link:
            return
        info = await self._safe_get(info_link, f"ActionInfo for {name}")
        if info is None:
            self._warn(f"ActionInfo for {name} unavailable — using DMTF defaults")
            return
        self._cap.raw_action_info[target] = info
        # If the action info carries AllowableValues for TransferProtocol,
        # promote them into the dedicated tuple field for fast access.
        if name == "SimpleUpdate" and not self._cap.simple_update_allowed_transfer_protocols:
            for param in info.get("Parameters") or []:
                if param.get("Name") == "TransferProtocol":
                    vals = param.get("AllowableValues") or ()
                    if vals:
                        self._cap.simple_update_allowed_transfer_protocols = tuple(vals)
                        break

    # ------------------------------------------------------------------
    # Step 5 — Systems
    # ------------------------------------------------------------------

    async def _walk_systems(self, root: dict) -> None:
        """Resolve Systems[0], its reset action, boot URI, and power state.

        The 'one server per BMC' assumption matches every host in our
        audit, so we always pick Members[0]. Boot is usually inlined on
        the system resource; older Lenovo XCC exposes it as a child link
        — we honor that by setting :attr:`system_boot_patch_uri` to the
        explicit Boot URI when present.
        """
        link = self._odata_id(root.get("Systems"))
        if not link:
            self._warn("No Systems collection — power/boot/provisioning disabled")
            return
        coll = await self._safe_get(link, "Systems collection")
        if coll is None:
            return
        members = coll.get("Members") or []
        if not members:
            self._warn("Systems collection empty — power/boot/provisioning disabled")
            return
        # Multi-node chassis (Tyrone/Supermicro twins, Lenovo NeXtScale,
        # AMD ZT Systems) publish multiple Systems and we silently target
        # the first. Warn so the operator knows ClusterOne is treating
        # the chassis as a single host. Future: surface per-node controls.
        if len(members) > 1:
            self._warn(
                f"Systems collection has {len(members)} members — "
                f"using Members[0] ({members[0].get('@odata.id', '?')}). "
                "Multi-node chassis aren't yet individually addressable."
            )
        sys_uri = self._odata_id(members[0])
        if not sys_uri:
            return
        system = await self._safe_get(sys_uri, "ComputerSystem")
        if system is None:
            return

        self._cap.system_uri = sys_uri
        self._cap.system_power_state = system.get("PowerState") or "Unknown"
        self._absorb_oem_keys(system)

        # Reset action + allowable values.
        reset = (system.get("Actions") or {}).get("#ComputerSystem.Reset") or {}
        if reset.get("target"):
            self._cap.system_reset_target = reset["target"]
            allowed = reset.get("ResetType@Redfish.AllowableValues")
            if allowed:
                self._cap.system_reset_allowed_resettypes = tuple(allowed)
            await self._fetch_action_info(reset, "ComputerSystem.Reset")
            # Promote AllowableValues from the ActionInfo blob if the
            # inline tuple is still empty.
            if not self._cap.system_reset_allowed_resettypes:
                info = self._cap.raw_action_info.get(reset["target"]) or {}
                for param in info.get("Parameters") or []:
                    if param.get("Name") == "ResetType":
                        vals = param.get("AllowableValues") or ()
                        if vals:
                            self._cap.system_reset_allowed_resettypes = tuple(vals)
                            break

        # Boot URI — inlined on the system resource on every audited BMC,
        # but spec allows a child link. Walking the link costs us nothing
        # and earns Lenovo XCC support for free.
        boot = system.get("Boot")
        if isinstance(boot, dict) and boot.get("@odata.id"):
            self._cap.system_boot_patch_uri = boot["@odata.id"]
        else:
            self._cap.system_boot_patch_uri = sys_uri

    # ------------------------------------------------------------------
    # Step 6 — Managers
    # ------------------------------------------------------------------

    async def _walk_managers(self, root: dict) -> None:
        """Resolve Managers[0] and follow VirtualMedia + Manager.Reset.

        Manager.FirmwareVersion is used as a fallback BMC version source
        when FirmwareInventory was unavailable in step 3. The
        VirtualMedia walk runs as part of this step so a missing
        Managers collection automatically disables OS provisioning.
        """
        link = self._odata_id(root.get("Managers"))
        if not link:
            self._warn("No Managers collection")
            return
        coll = await self._safe_get(link, "Managers collection")
        if coll is None:
            return
        members = coll.get("Members") or []
        if not members:
            self._warn("Managers collection empty")
            return
        mgr_uri = self._odata_id(members[0])
        if not mgr_uri:
            return
        manager = await self._safe_get(mgr_uri, "Manager")
        if manager is None:
            return

        self._cap.manager_uri = mgr_uri
        self._absorb_oem_keys(manager)

        reset = (manager.get("Actions") or {}).get("#Manager.Reset") or {}
        if reset.get("target"):
            self._cap.manager_reset_target = reset["target"]

        vm_link = self._odata_id(manager.get("VirtualMedia"))
        if vm_link:
            self._cap.virtualmedia_collection_uri = vm_link
            await self._walk_virtualmedia(vm_link)
        else:
            self._warn("VirtualMedia not exposed — OS provisioning disabled on this BMC")

    # ------------------------------------------------------------------
    # Step 7 — VirtualMedia
    # ------------------------------------------------------------------

    async def _walk_virtualmedia(self, vm_link: str) -> None:
        """Populate :attr:`virtualmedia_uris` with every slot.

        Each slot captures its action targets, MediaTypes, transfer
        details, and current inserted state. ``mount_iso`` then picks by
        ``MediaTypes`` (e.g. 'CD' for ISO, 'USBStick' for img) instead of
        by slot name — same code path handles Dell's RemovableDisk and
        DMTF CD slots without an OEM override.
        """
        coll = await self._safe_get(vm_link, "VirtualMedia collection")
        if coll is None:
            return
        for ref in coll.get("Members") or []:
            uri = self._odata_id(ref)
            if not uri:
                continue
            slot = await self._safe_get(uri, f"VirtualMedia slot {uri}")
            if slot is None:
                continue
            actions = slot.get("Actions") or {}
            insert = (actions.get("#VirtualMedia.InsertMedia") or {}).get("target")
            eject = (actions.get("#VirtualMedia.EjectMedia") or {}).get("target")
            slot_id = slot.get("Id") or uri.rsplit("/", 1)[-1]
            self._cap.virtualmedia_uris[slot_id] = VirtualMediaSlot(
                uri=uri,
                media_types=tuple(slot.get("MediaTypes") or ()),
                insert_action=insert,
                eject_action=eject,
                transfer_method=slot.get("TransferMethod") or "",
                transfer_protocol_types=tuple(slot.get("TransferProtocolType") or ()),
                inserted=bool(slot.get("Inserted", False)),
                write_protected=bool(slot.get("WriteProtected", True)),
            )

    # ------------------------------------------------------------------
    # Step 8 — TaskService
    # ------------------------------------------------------------------

    async def _walk_task_service(self, root: dict) -> None:
        """Resolve TaskService and its Tasks collection.

        The collection URI is what the orchestrator polls when the BMC
        didn't return a Location header on the update POST. A missing
        TaskService is degraded but not fatal — flash progress just
        reports 0% then 100%.
        """
        link = self._odata_id(root.get("Tasks") or root.get("TaskService"))
        if not link:
            self._warn("No TaskService — flash progress will be reported as 0%/100% only")
            return
        ts = await self._safe_get(link, "TaskService")
        if ts is None:
            return
        self._cap.task_service_uri = self._odata_id(ts) or link
        self._cap.task_collection_uri = self._odata_id(ts.get("Tasks"))

    # ------------------------------------------------------------------
    # Step 9 — SessionService
    # ------------------------------------------------------------------

    async def _walk_session_service(self, root: dict) -> None:
        """Resolve SessionService — used by the auth layer for token fallback.

        Most calls succeed with basic auth, so this is only consulted
        when the BMC rejects basic or sets a short session timeout. 404
        downgrades to a warning and basic-auth-only operation.
        """
        link = self._odata_id(root.get("SessionService"))
        if not link:
            # Try the Links.Sessions back-reference as a fallback before
            # giving up — older BMCs publish only there.
            links = root.get("Links") or {}
            link = self._odata_id(links.get("Sessions"))
            if not link:
                self._warn("No SessionService — basic auth only")
                return
        # We don't GET it eagerly; recording the URI is enough for the
        # auth layer to find when basic auth fails.
        self._cap.session_service_uri = link

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _odata_id(value: object) -> str | None:
        """Pull ``@odata.id`` out of a dict-shaped reference.

        Redfish references are dicts of the form ``{"@odata.id": "/foo"}``.
        Handles ``None`` and non-dict values (returning ``None``) so
        callers can flow through with ``self._odata_id(d.get('Foo'))``
        even when ``Foo`` is missing.
        """
        if isinstance(value, dict):
            v = value.get("@odata.id")
            if isinstance(v, str) and v:
                return v
        return None

    async def _safe_get(self, path: str, label: str) -> dict | None:
        """GET ``path`` and return the dict, or ``None`` on failure.

        Failures are logged at debug level (the discoverer is run on
        every server during a scan; a chatty BMC must not flood the log)
        and recorded as a discovery warning. We deliberately swallow
        every :class:`RedfishError` — including 404, 5xx, and TLS — so
        the walk can continue and produce a partial map. Only the
        bootstrap GET in :meth:`_bootstrap` is allowed to raise.
        """
        try:
            return await self._client.get_json(path)
        except RedfishError as exc:
            log.debug("Discovery: %s GET %s failed: %s", label, path, exc)
            self._warn(f"{label} unavailable ({exc})")
            return None
        except (asyncio.CancelledError, RuntimeError):
            # Cancellation must propagate cleanly. RuntimeError almost
            # always means the underlying aiohttp.ClientSession was
            # closed (the client died, not the BMC) — silently turning
            # that into "endpoint not present" produces a deceptive
            # empty CapabilityMap. Let it propagate so the orchestrator
            # surfaces "client died mid-walk" instead of "BMC has no
            # UpdateService".
            raise
        except Exception as exc:  # pragma: no cover — defensive
            # Anything aiohttp throws (TimeoutError, ClientConnectorError,
            # ServerDisconnectedError, etc.) — a single rude BMC must
            # not sink the walk.
            log.debug("Discovery: %s GET %s exception: %s", label, path, exc)
            self._warn(f"{label} error ({exc})")
            return None

    def _warn(self, message: str) -> None:
        """Record a non-fatal observation.

        Warnings end up in :attr:`CapabilityMap.discovery_warnings` so
        the server-detail drawer can show the operator WHY a capability
        is greyed out (instead of just observing failure later).
        """
        self._warnings.append(message)

    def _absorb_oem_keys(self, resource: dict) -> None:
        """Collect Oem block names into :attr:`oem_blocks`.

        Every Oem key on any walked resource lands here. Used by the
        plugin registry to score matches — a Tyrone-rebadged Supermicro
        publishes "Supermicro" under ServiceRoot.Oem AND "Smc" under
        Systems[0].Oem; both are recorded so the Supermicro plugin wins
        the match regardless of which one the operator's BMC chose.
        """
        oem = resource.get("Oem")
        if isinstance(oem, dict):
            for key in oem.keys():
                if isinstance(key, str) and key:
                    self._oem_block_set.add(key)


# Public alias — shorter, matches design docs + the test module. The longer
# name remains canonical because plugin imports already use it.
CapabilityDiscoverer = RedfishCapabilityDiscoverer

__all__ = [
    "CapabilityDiscoverer",
    "CapabilityMap",
    "FirmwareMember",
    "RedfishCapabilityDiscoverer",
    "VirtualMediaSlot",
]
