"""CapabilityDiscoverer — one bounded Redfish walk per server.

These tests pin the behavior described in the CapabilityMap design spec:

* One walk produces a typed snapshot of every URI/action/feature flag the
  orchestrator and OEM plugins need — no plugin should ever hardcode a
  ``/redfish/v1/...`` path again.
* The classifier picks the ACTIVE bank (BIOS / BIOSImage1 / BMC / BMCImage1)
  and skips every recovery/backup variant (Image2, Backup, Golden, Staging,
  Recovery, History) — the BANNED-leaf heuristic already proven in
  ``test_fleet_inventory_variants.py``.
* Discovery survives missing optional surfaces (no TaskService, no
  VirtualMedia, no FirmwareInventory) by recording a warning and leaving the
  affected fields ``None`` — never by raising.
* OEM actions are discovered from ``Actions.Oem.*`` (any vendor key), so a
  new BMC family is supported by adding heuristics to the discoverer rather
  than by writing a new plugin with a new set of constants.

Stub BMCs are tiny aiohttp apps served in-process on a random port; nothing
in this module touches real hardware.
"""
from __future__ import annotations

import pytest
from aiohttp import web

from core.plugins_api.redfish_client import RedfishClient

# The discoverer module is the single owner of every literal Redfish path
# in the system. Tests fail loudly if its public surface drifts.
from core.plugins_api.capability_discoverer import (
    CapabilityDiscoverer,
    CapabilityMap,
)


# ---------------------------------------------------------------------------
# Shared aiohttp serve helper (mirrors test_firmware_update_transport.py)
# ---------------------------------------------------------------------------

async def _serve(app: web.Application) -> tuple[web.AppRunner, int]:
    """Bind the app to a random localhost port. Returns (runner, port).

    The test must ``await runner.cleanup()`` in its ``finally`` block to
    free the socket — the harness does not own a lifecycle for it.
    """
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = list(runner.addresses)[0][1]
    return runner, port


def _client_for(port: int) -> RedfishClient:
    """Build a RedfishClient pointed at the stub BMC.

    No credentials — the stubs in this file do not enforce auth (the
    discoverer's job is path/shape discovery, not session minting).
    """
    return RedfishClient("127.0.0.1", port=port, scheme="http")


# ---------------------------------------------------------------------------
# Test 1: Supermicro / Tyrone single-image AMI MegaRAC build.
# ---------------------------------------------------------------------------

class _SupermicroStubBMC:
    """Tyrone-rebadged Supermicro with the AMI MegaRAC 'BIOS + recovery'
    inventory layout: live BIOS + BMC plus three recovery banks
    (Backup_BIOS, Golden_BIOS, Staging_BIOS). The discoverer must pick
    the ACTIVE bank for both bios_member_uri and bmc_member_uri.

    Also advertises MultipartHttpPushUri and SimpleUpdate so we can verify
    transport detection independent of inventory classification.
    """

    def __init__(self) -> None:
        self.app = web.Application()
        r = self.app.router
        r.add_get("/redfish/v1/", self._service_root)
        r.add_get("/redfish/v1/UpdateService", self._update_service)
        r.add_get("/redfish/v1/UpdateService/FirmwareInventory", self._fw_inv)
        # All five inventory members served by one route; the leaf decides
        # which payload to return.
        r.add_get("/redfish/v1/UpdateService/FirmwareInventory/{leaf}", self._fw_member)
        r.add_get("/redfish/v1/Systems", self._systems)
        r.add_get("/redfish/v1/Systems/1", self._system_one)
        r.add_get("/redfish/v1/Managers", self._managers)
        r.add_get("/redfish/v1/Managers/1", self._manager_one)
        r.add_get("/redfish/v1/Managers/1/VirtualMedia", self._vm_collection)
        r.add_get("/redfish/v1/Managers/1/VirtualMedia/CD", self._vm_cd)
        r.add_get("/redfish/v1/TaskService", self._task_service)
        r.add_get("/redfish/v1/TaskService/Tasks", self._task_collection)
        r.add_get("/redfish/v1/SessionService", self._session_service)

    async def _service_root(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/",
            "RedfishVersion": "1.13.0",
            "Vendor": "Supermicro",
            "Systems": {"@odata.id": "/redfish/v1/Systems"},
            "Managers": {"@odata.id": "/redfish/v1/Managers"},
            "UpdateService": {"@odata.id": "/redfish/v1/UpdateService"},
            "Tasks": {"@odata.id": "/redfish/v1/TaskService"},
            "SessionService": {"@odata.id": "/redfish/v1/SessionService"},
            "Oem": {"Supermicro": {"@odata.type": "#Supermicro.Oem"}},
        })

    async def _update_service(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/UpdateService",
            "ServiceEnabled": True,
            "MultipartHttpPushUri": "/redfish/v1/UpdateService/upload",
            "HttpPushUri": "/redfish/v1/UpdateService/push",
            "FirmwareInventory": {
                "@odata.id": "/redfish/v1/UpdateService/FirmwareInventory"},
            "Actions": {
                "#UpdateService.SimpleUpdate": {
                    "target": "/redfish/v1/UpdateService/Actions/SimpleUpdate",
                    "TransferProtocol@Redfish.AllowableValues":
                        ["HTTP", "HTTPS", "NFS"],
                },
                "Oem": {
                    "#SmcUpdateService.Install": {
                        "target": ("/redfish/v1/UpdateService/Actions/Oem/"
                                   "SmcUpdateService.Install"),
                    },
                },
            },
        })

    async def _fw_inv(self, _req):
        return web.json_response({"Members": [
            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/BIOS"},
            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/BMC"},
            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/Backup_BIOS"},
            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/Golden_BIOS"},
            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/Staging_BIOS"},
        ]})

    async def _fw_member(self, req):
        leaf = req.match_info["leaf"]
        return web.json_response({
            "Id": leaf,
            "Version": "3.1" if "BIOS" in leaf else "1.04.06",
            "Updateable": True,
        })

    async def _systems(self, _req):
        return web.json_response(
            {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]})

    async def _system_one(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/Systems/1",
            "PowerState": "On",
            "Actions": {
                "#ComputerSystem.Reset": {
                    "target": "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
                    "ResetType@Redfish.AllowableValues": [
                        "On", "ForceOff", "GracefulRestart",
                        "ForceRestart", "PowerCycle", "Nmi",
                    ],
                },
            },
        })

    async def _managers(self, _req):
        return web.json_response(
            {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]})

    async def _manager_one(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/Managers/1",
            "FirmwareVersion": "1.04.06",
            "VirtualMedia": {
                "@odata.id": "/redfish/v1/Managers/1/VirtualMedia"},
            "Actions": {
                "#Manager.Reset": {
                    "target": "/redfish/v1/Managers/1/Actions/Manager.Reset",
                },
            },
        })

    async def _vm_collection(self, _req):
        return web.json_response({"Members": [
            {"@odata.id": "/redfish/v1/Managers/1/VirtualMedia/CD"},
        ]})

    async def _vm_cd(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/Managers/1/VirtualMedia/CD",
            "Id": "CD",
            "MediaTypes": ["CD", "DVD"],
            "Inserted": False,
            "WriteProtected": True,
            "Actions": {
                "#VirtualMedia.InsertMedia": {
                    "target": ("/redfish/v1/Managers/1/VirtualMedia/CD/Actions/"
                               "VirtualMedia.InsertMedia"),
                },
                "#VirtualMedia.EjectMedia": {
                    "target": ("/redfish/v1/Managers/1/VirtualMedia/CD/Actions/"
                               "VirtualMedia.EjectMedia"),
                },
            },
        })

    async def _task_service(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/TaskService",
            "ServiceEnabled": True,
            "Tasks": {"@odata.id": "/redfish/v1/TaskService/Tasks"},
        })

    async def _task_collection(self, _req):
        return web.json_response({"Members": []})

    async def _session_service(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/SessionService",
            "Sessions": {"@odata.id": "/redfish/v1/SessionService/Sessions"},
        })


@pytest.mark.asyncio
async def test_supermicro_stub_resolves_active_banks_and_transport():
    """Discoverer against a Tyrone/Supermicro single-image build.

    Must:
    * Pick ``/BIOS`` and ``/BMC`` (NOT Backup_/Golden_/Staging_).
    * Reflect the BMC-advertised ``MultipartHttpPushUri`` exactly.
    * Capture ``ResetType@Redfish.AllowableValues`` so plugins can choose
      the strongest legal reset instead of guessing.
    * Surface the Supermicro OEM install action under its discovered key.
    """
    bmc = _SupermicroStubBMC()
    runner, port = await _serve(bmc.app)
    client = _client_for(port)
    try:
        cap: CapabilityMap = await CapabilityDiscoverer(client).discover()

        assert cap.bios_member_uri is not None, "BIOS member must be resolved"
        assert cap.bios_member_uri.endswith("/BIOS"), (
            f"BIOS picked {cap.bios_member_uri!r} — expected the active bank, "
            "not a Backup_/Golden_/Staging_ recovery slot")
        assert cap.bmc_member_uri is not None, "BMC member must be resolved"
        assert cap.bmc_member_uri.endswith("/BMC"), (
            f"BMC picked {cap.bmc_member_uri!r}")

        # Transport: multipart push wins on this BMC.
        assert cap.multipart_push_uri == "/redfish/v1/UpdateService/upload"

        # Reset AllowableValues should include the operator-safe options.
        for needed in ("ForceOff", "On", "GracefulRestart"):
            assert needed in cap.system_reset_allowed_resettypes, (
                f"{needed!r} missing from "
                f"{cap.system_reset_allowed_resettypes!r}")

        # The Supermicro OEM install action must be visible by its action
        # name — plugins look it up rather than concatenating a path.
        assert "#SmcUpdateService.Install" in cap.oem_install_actions
        assert cap.oem_install_actions["#SmcUpdateService.Install"].endswith(
            "SmcUpdateService.Install")

        # Bookkeeping: vendor_hint + service_root.
        assert cap.vendor_hint == "Supermicro"
        assert cap.service_root == "/redfish/v1/"
        # The classifier identified the recovery banks as NOT active.
        assert "Backup_BIOS" in cap.firmware_inventory
        assert cap.firmware_inventory["Backup_BIOS"].is_active_bank is False
    finally:
        await client.close()
        await runner.cleanup()


# ---------------------------------------------------------------------------
# Test 2: Group-4 AMI MegaRAC dual-image build.
# ---------------------------------------------------------------------------

class _Group4AmiStubBMC:
    """Tyrone Camarero ES381AMS / Group-4 AMI layout (4 BMCs in the fleet).

    Dual BIOS + dual BMC: BIOSImage1 / BIOSImage2 / BMCImage1 / BMCImage2,
    plus a CPLD. Image1 is the ACTIVE bank everywhere we've audited; Image2
    is the recovery slot — flashing Image2 destroys the operator's rollback
    path, so the discoverer MUST pick Image1.
    """

    def __init__(self) -> None:
        self.app = web.Application()
        r = self.app.router
        r.add_get("/redfish/v1/", self._service_root)
        r.add_get("/redfish/v1/UpdateService", self._update_service)
        r.add_get("/redfish/v1/UpdateService/FirmwareInventory", self._fw_inv)
        r.add_get(
            "/redfish/v1/UpdateService/FirmwareInventory/{leaf}",
            self._fw_member,
        )
        r.add_get("/redfish/v1/Systems", self._systems)
        r.add_get("/redfish/v1/Systems/1", self._system_one)
        r.add_get("/redfish/v1/Managers", self._managers)
        r.add_get("/redfish/v1/Managers/1", self._manager_one)
        r.add_get("/redfish/v1/TaskService", self._task_service)
        r.add_get("/redfish/v1/TaskService/Tasks", self._task_collection)

    async def _service_root(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/",
            "RedfishVersion": "1.11.0",
            # AMI BMCs often don't set Vendor — they advertise via Oem.Ami.
            "Oem": {"Ami": {"@odata.type": "#Ami.Oem"}},
            "Systems": {"@odata.id": "/redfish/v1/Systems"},
            "Managers": {"@odata.id": "/redfish/v1/Managers"},
            "UpdateService": {"@odata.id": "/redfish/v1/UpdateService"},
            "Tasks": {"@odata.id": "/redfish/v1/TaskService"},
        })

    async def _update_service(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/UpdateService",
            "ServiceEnabled": True,
            "MultipartHttpPushUri": "/redfish/v1/UpdateService/upload",
            "FirmwareInventory": {
                "@odata.id": "/redfish/v1/UpdateService/FirmwareInventory"},
            "Actions": {
                "#UpdateService.SimpleUpdate": {
                    "target": "/redfish/v1/UpdateService/Actions/SimpleUpdate",
                },
            },
        })

    async def _fw_inv(self, _req):
        return web.json_response({"Members": [
            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/BIOSImage1"},
            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/BIOSImage2"},
            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/BMCImage1"},
            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/BMCImage2"},
            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/CPLD"},
        ]})

    async def _fw_member(self, req):
        leaf = req.match_info["leaf"]
        return web.json_response({
            "Id": leaf,
            "Version": "1.0",
            "Updateable": True,
        })

    async def _systems(self, _req):
        return web.json_response(
            {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]})

    async def _system_one(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/Systems/1",
            "PowerState": "On",
            "Actions": {
                "#ComputerSystem.Reset": {
                    "target": "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
                },
            },
        })

    async def _managers(self, _req):
        return web.json_response(
            {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]})

    async def _manager_one(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/Managers/1",
            "FirmwareVersion": "1.0",
        })

    async def _task_service(self, _req):
        return web.json_response({
            "Tasks": {"@odata.id": "/redfish/v1/TaskService/Tasks"},
        })

    async def _task_collection(self, _req):
        return web.json_response({"Members": []})


@pytest.mark.asyncio
async def test_dual_image_ami_bmc_picks_image1_not_image2():
    """Critical safety check: on dual-image AMI builds the active bank is
    Image1; Image2 is the recovery slot. The discoverer MUST resolve
    Image1 — flashing Image2 corrupts rollback.

    The classifier must also tag Image2 entries as is_active_bank=False so
    the per-class lookup (cap.bios_member_uri / cap.bmc_member_uri) never
    points at recovery — even if the iteration order changes.
    """
    bmc = _Group4AmiStubBMC()
    runner, port = await _serve(bmc.app)
    client = _client_for(port)
    try:
        cap = await CapabilityDiscoverer(client).discover()

        # Picked Image1 — NOT Image2 — for both classes.
        assert cap.bios_member_uri is not None
        assert cap.bios_member_uri.endswith("/BIOSImage1"), (
            f"BIOS picked {cap.bios_member_uri!r} — Image2 is the recovery "
            "bank and must never be selected as the active target")
        assert cap.bmc_member_uri is not None
        assert cap.bmc_member_uri.endswith("/BMCImage1"), (
            f"BMC picked {cap.bmc_member_uri!r}")

        # Image2 entries appear in the inventory but are tagged inactive.
        assert "BIOSImage2" in cap.firmware_inventory
        assert cap.firmware_inventory["BIOSImage2"].is_active_bank is False
        assert "BMCImage2" in cap.firmware_inventory
        assert cap.firmware_inventory["BMCImage2"].is_active_bank is False

        # Image1 entries are tagged active.
        assert cap.firmware_inventory["BIOSImage1"].is_active_bank is True
        assert cap.firmware_inventory["BMCImage1"].is_active_bank is True

        # CPLD entry classified separately, available via extra_firmware_members.
        assert "cpld" in cap.extra_firmware_members
        assert cap.extra_firmware_members["cpld"].endswith("/CPLD")

        # Vendor hint falls back to the first Oem key when Vendor is absent.
        assert cap.vendor_hint == "Ami"
        assert "Ami" in cap.oem_blocks
    finally:
        await client.close()
        await runner.cleanup()


# ---------------------------------------------------------------------------
# Test 3: BMC with no TaskService (graceful 404 fallback).
# ---------------------------------------------------------------------------

class _NoTaskServiceStubBMC:
    """A minimal BMC that doesn't expose /TaskService at all.

    The spec calls out this case explicitly: when TaskService is missing
    the discoverer must NOT crash; it should leave task_service_uri and
    task_collection_uri as None, surface a warning, and produce a usable
    CapabilityMap for everything else. The polling code already tolerates
    a synchronous flash response.
    """

    def __init__(self) -> None:
        self.app = web.Application()
        r = self.app.router
        r.add_get("/redfish/v1/", self._service_root)
        r.add_get("/redfish/v1/UpdateService", self._update_service)
        r.add_get("/redfish/v1/UpdateService/FirmwareInventory", self._fw_inv)
        r.add_get(
            "/redfish/v1/UpdateService/FirmwareInventory/{leaf}",
            self._fw_member,
        )
        r.add_get("/redfish/v1/Systems", self._systems)
        r.add_get("/redfish/v1/Systems/1", self._system_one)
        r.add_get("/redfish/v1/Managers", self._managers)
        r.add_get("/redfish/v1/Managers/1", self._manager_one)
        # No /redfish/v1/TaskService route — aiohttp returns 404.

    async def _service_root(self, _req):
        # ServiceRoot still advertises Tasks; the discoverer must tolerate
        # the link existing but the resource itself 404ing (rare in the wild
        # but observed on stripped-down ARM BMCs).
        return web.json_response({
            "@odata.id": "/redfish/v1/",
            "RedfishVersion": "1.6.0",
            "Vendor": "Generic",
            "Systems": {"@odata.id": "/redfish/v1/Systems"},
            "Managers": {"@odata.id": "/redfish/v1/Managers"},
            "UpdateService": {"@odata.id": "/redfish/v1/UpdateService"},
            "Tasks": {"@odata.id": "/redfish/v1/TaskService"},
        })

    async def _update_service(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/UpdateService",
            "ServiceEnabled": True,
            "FirmwareInventory": {
                "@odata.id": "/redfish/v1/UpdateService/FirmwareInventory"},
            "Actions": {
                "#UpdateService.SimpleUpdate": {
                    "target": "/redfish/v1/UpdateService/Actions/SimpleUpdate",
                },
            },
        })

    async def _fw_inv(self, _req):
        return web.json_response({"Members": [
            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/BIOS"},
            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/BMC"},
        ]})

    async def _fw_member(self, req):
        leaf = req.match_info["leaf"]
        return web.json_response({"Id": leaf, "Version": "0.1", "Updateable": True})

    async def _systems(self, _req):
        return web.json_response(
            {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]})

    async def _system_one(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/Systems/1",
            "PowerState": "On",
            "Actions": {
                "#ComputerSystem.Reset": {
                    "target": "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
                },
            },
        })

    async def _managers(self, _req):
        return web.json_response(
            {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]})

    async def _manager_one(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/Managers/1",
            "FirmwareVersion": "0.1",
        })


@pytest.mark.asyncio
async def test_missing_task_service_does_not_crash_discovery(caplog):
    """Step 8 of the walk (TaskService) is optional. A 404 must:

    * leave task_service_uri and task_collection_uri as None,
    * NOT raise — the rest of the CapabilityMap must still populate,
    * append a non-fatal note to discovery_warnings,
    * emit at most a debug-level log (no error/warning spam in CI).
    """
    import logging

    bmc = _NoTaskServiceStubBMC()
    runner, port = await _serve(bmc.app)
    client = _client_for(port)
    try:
        with caplog.at_level(logging.DEBUG, logger="core.plugins_api"):
            cap = await CapabilityDiscoverer(client).discover()

        # The two task-related fields are gracefully None.
        assert cap.task_service_uri is None, (
            f"task_service_uri should be None when /TaskService 404s, "
            f"got {cap.task_service_uri!r}")
        assert cap.task_collection_uri is None, (
            f"task_collection_uri should be None when /TaskService 404s, "
            f"got {cap.task_collection_uri!r}")

        # The walk still resolved everything else — discovery isn't gated
        # on TaskService.
        assert cap.bios_member_uri is not None
        assert cap.bmc_member_uri is not None
        assert cap.system_uri is not None
        assert cap.manager_uri is not None
        assert cap.update_service_uri == "/redfish/v1/UpdateService"

        # The operator-facing warning is recorded so the UI can explain
        # the greyed-out task-progress chip.
        assert any("TaskService" in w for w in cap.discovery_warnings), (
            f"expected a TaskService warning, got "
            f"{list(cap.discovery_warnings)!r}")

        # Nothing logged at ERROR level — the 404 is a known fallback, not a bug.
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not errors, (
            f"unexpected error-level logs from discovery: "
            f"{[r.getMessage() for r in errors]}")
    finally:
        await client.close()
        await runner.cleanup()


# ---------------------------------------------------------------------------
# Test 4: OEM action discovery — #SmcUpdateService.Install.
# ---------------------------------------------------------------------------

class _SmcInstallActionStubBMC:
    """Bare-bones BMC that advertises a Supermicro OEM install action.

    The point of this stub is to isolate the OEM action discovery contract:
    given any Actions.Oem.* key under UpdateService, the discoverer must
    capture its target verbatim and expose it under oem_install_actions
    keyed by the action name. Plugins then look up by name without ever
    concatenating a path.

    This is the mechanism that automatically covers ALL the vendors listed
    in the spec (#SmcUpdateService.Install, #UpdateService.BMCFwUpdate,
    #UpdateService.UploadFirmwareImage, #HpeiLOUpdateServiceExt.AddFromUri,
    etc.) — there is no per-vendor code in the discoverer.
    """

    INSTALL_TARGET = ("/redfish/v1/UpdateService/Actions/Oem/"
                      "SmcUpdateService.Install")

    def __init__(self) -> None:
        self.app = web.Application()
        r = self.app.router
        r.add_get("/redfish/v1/", self._service_root)
        r.add_get("/redfish/v1/UpdateService", self._update_service)

    async def _service_root(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/",
            "RedfishVersion": "1.13.0",
            "Vendor": "Supermicro",
            "UpdateService": {"@odata.id": "/redfish/v1/UpdateService"},
            # Note: Systems/Managers omitted on purpose — this test is
            # entirely about OEM action capture, not full topology. The
            # discoverer must still produce a usable map.
            "Oem": {"Supermicro": {"@odata.type": "#Supermicro.Oem"}},
        })

    async def _update_service(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/UpdateService",
            "ServiceEnabled": True,
            "MultipartHttpPushUri": "/redfish/v1/UpdateService/upload",
            "Actions": {
                "#UpdateService.SimpleUpdate": {
                    "target": "/redfish/v1/UpdateService/Actions/SimpleUpdate",
                },
                "Oem": {
                    "#SmcUpdateService.Install": {
                        "target": self.INSTALL_TARGET,
                        "@Redfish.OperationApplyTimeSupport": {
                            "SupportedValues": ["OnReset", "Immediate"],
                        },
                    },
                },
            },
        })


@pytest.mark.asyncio
async def test_smc_install_oem_action_captured_with_verbatim_target():
    """Any ``Actions.Oem.<#Vendor.Action>`` key under UpdateService must
    end up in ``cap.oem_install_actions`` keyed by the full action name,
    with its ``target`` URI captured exactly as advertised — no
    normalization, no path concatenation, no per-vendor casing.

    This is what lets the Supermicro plugin shrink: it no longer carries
    a ``_SMC_INSTALL`` constant. It reads
    ``cap.oem_install_actions["#SmcUpdateService.Install"]`` and POSTs.
    """
    bmc = _SmcInstallActionStubBMC()
    runner, port = await _serve(bmc.app)
    client = _client_for(port)
    try:
        cap = await CapabilityDiscoverer(client).discover()

        # The OEM action was discovered by name…
        assert "#SmcUpdateService.Install" in cap.oem_install_actions, (
            f"expected #SmcUpdateService.Install in oem_install_actions, "
            f"got keys: {sorted(cap.oem_install_actions)}")

        # …and the target URI is captured verbatim.
        assert (cap.oem_install_actions["#SmcUpdateService.Install"]
                == _SmcInstallActionStubBMC.INSTALL_TARGET), (
            "oem_install_actions['#SmcUpdateService.Install'] should be the "
            "advertised target URI exactly, not a reconstructed path")

        # The transport URIs around it still populate normally — the OEM
        # action capture doesn't interfere with multipart push discovery.
        assert cap.multipart_push_uri == "/redfish/v1/UpdateService/upload"
        assert cap.simple_update_target == (
            "/redfish/v1/UpdateService/Actions/SimpleUpdate")
    finally:
        await client.close()
        await runner.cleanup()


# ---------------------------------------------------------------------------
# Test 5: HGX baseboard inventory classification — real BMC .193 shape.
# ---------------------------------------------------------------------------

class _HgxBaseboardStubBMC:
    """AMI MegaRAC + HGX baseboard layout (matches the .193 audit shape).

    Publishes host BIOS/BMC alongside a representative slice of the 28+
    HGX firmware members real .193 exposes. The discoverer MUST route
    every HGX_* / GPU / ERoT / InfoROM / HMC entry into
    ``cap.hgx_member_uris`` without disturbing the host BIOS/BMC
    resolution.
    """

    def __init__(self) -> None:
        self.app = web.Application()
        r = self.app.router
        r.add_get("/redfish/v1/", self._service_root)
        r.add_get("/redfish/v1/UpdateService", self._update_service)
        r.add_get(
            "/redfish/v1/UpdateService/FirmwareInventory", self._fw_inv)
        r.add_get(
            "/redfish/v1/UpdateService/FirmwareInventory/{leaf}",
            self._fw_member,
        )
        r.add_get("/redfish/v1/Systems", self._systems)
        r.add_get("/redfish/v1/Systems/1", self._system_one)
        r.add_get("/redfish/v1/Managers", self._managers)
        r.add_get("/redfish/v1/Managers/1", self._manager_one)

    async def _service_root(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/",
            "RedfishVersion": "1.15.1",
            "Vendor": "AMI",
            "Systems": {"@odata.id": "/redfish/v1/Systems"},
            "Managers": {"@odata.id": "/redfish/v1/Managers"},
            "UpdateService": {"@odata.id": "/redfish/v1/UpdateService"},
            "Oem": {"Ami": {"@odata.type": "#Ami.Oem"}},
        })

    async def _update_service(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/UpdateService",
            "ServiceEnabled": True,
            "MultipartHttpPushUri": "/redfish/v1/UpdateService/upload",
            "FirmwareInventory": {
                "@odata.id": "/redfish/v1/UpdateService/FirmwareInventory"},
            "Actions": {
                "#UpdateService.SimpleUpdate": {
                    "target": "/redfish/v1/UpdateService/Actions/SimpleUpdate",
                },
            },
        })

    async def _fw_inv(self, _req):
        return web.json_response({"Members": [
            {"@odata.id": f"/redfish/v1/UpdateService/FirmwareInventory/{leaf}"}
            for leaf in (
                "BIOS", "BMC",
                "HGX_FW_GPU_0", "HGX_FW_GPU_1",
                "HGX_FW_ERoT_GPU_0",
                "HGX_FW_BMC_0",  # HGX-side BMC, NOT host BMC
                "HGX_InfoROM_GPU_0",
                "FW_GPU_2",      # AMI variant without HGX_ prefix
                "InfoROM_GPU_3", # AMI variant without HGX_ prefix
            )
        ]})

    async def _fw_member(self, req):
        leaf = req.match_info["leaf"]
        return web.json_response({
            "Id": leaf, "Version": "1.0", "Updateable": True,
        })

    async def _systems(self, _req):
        return web.json_response(
            {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]})

    async def _system_one(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/Systems/1",
            "PowerState": "On",
            "Actions": {
                "#ComputerSystem.Reset": {
                    "target": "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
                },
            },
        })

    async def _managers(self, _req):
        return web.json_response(
            {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]})

    async def _manager_one(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/Managers/1",
            "FirmwareVersion": "1.04.06",
        })


@pytest.mark.asyncio
async def test_hgx_baseboard_inventory_classification():
    """Real BMC .193 publishes 28+ HGX members alongside host BIOS/BMC.

    The discoverer must:
    * Route every HGX_* / GPU / ERoT / InfoROM entry into
      ``cap.hgx_member_uris`` (keyed by canonical leaf name).
    * Keep the host BIOS / BMC entries pointing at the host firmware
      slots — HGX_FW_BMC_0 must NOT replace the host BMC member.
    * Accept the AMI variants that drop the HGX_ prefix (``FW_GPU_*``,
      ``InfoROM_GPU_*``) — they're still GPU firmware and belong in
      ``hgx_member_uris``.
    """
    bmc = _HgxBaseboardStubBMC()
    runner, port = await _serve(bmc.app)
    client = _client_for(port)
    try:
        cap = await CapabilityDiscoverer(client).discover()

        expected = {
            "HGX_FW_GPU_0", "HGX_FW_GPU_1",
            "HGX_FW_ERoT_GPU_0", "HGX_FW_BMC_0",
            "HGX_InfoROM_GPU_0",
            "FW_GPU_2", "InfoROM_GPU_3",
        }
        assert set(cap.hgx_member_uris.keys()) == expected, (
            f"hgx_member_uris keys = {sorted(cap.hgx_member_uris.keys())}, "
            f"expected {sorted(expected)}")

        # Host BIOS/BMC still resolved correctly — HGX detection MUST NOT
        # poach the host firmware slots.
        assert cap.bios_member_uri is not None
        assert cap.bios_member_uri.endswith("/BIOS")
        assert cap.bmc_member_uri is not None
        assert cap.bmc_member_uri.endswith("/BMC"), (
            f"host BMC resolved to {cap.bmc_member_uri!r} — must point at "
            "the host BMC member, not HGX_FW_BMC_0 (which is the HGX-side BMC)")
    finally:
        await client.close()
        await runner.cleanup()
