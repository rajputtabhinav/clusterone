"""HGX / GPU firmware updates via Redfish.

These tests pin the behavior added in the HGX wiring sprint:

* The CapabilityDiscoverer classifies every HGX baseboard / GPU
  FirmwareInventory member into a separate ``cap.hgx_member_uris``
  dictionary — host BIOS / BMC slots are untouched.
* ``GenericRedfishPlugin.update_hgx`` selects a target from that map,
  pushes the firmware bytes via DMTF multipart push with
  ``Targets=[<resolved_uri>]`` and ``ApplyTime=OnReset``, polls the
  resulting Task, and reports a clean success message that mentions the
  AC-power-cycle requirement.
* When no HGX inventory is present, ``update_hgx`` returns a clean
  ``Result(ok=False, …)`` rather than crashing the caller.
* Recovery / backup banks (``Image2``, ``Backup_``, …) are excluded from
  ``hgx_member_uris`` so the operator can never accidentally flash a
  rollback slot — same safety contract as BIOS / BMC.

The audit at ``audit_update_apis.json`` proves real BMC .193 publishes
28+ HGX members alongside host BIOS / BMC under one
``/redfish/v1/UpdateService/upload`` endpoint; the stub BMCs here mirror
that shape.
"""
from __future__ import annotations

import json

import pytest
from aiohttp import web

from core.models.firmware import Firmware
from core.models.server import Credentials, ServerInfo
from core.plugins_api.capability_discoverer import (
    CapabilityDiscoverer,
    CapabilityMap,
)
from core.plugins_api.redfish_client import RedfishClient
from plugins.generic_redfish.plugin import GenericRedfishPlugin


# ---------------------------------------------------------------------------
# Helpers — match the patterns used by test_capability_discoverer.py and
# test_firmware_update_transport.py so the stub style is consistent.
# ---------------------------------------------------------------------------


async def _serve(app: web.Application) -> tuple[web.AppRunner, int]:
    """Bind ``app`` to a random localhost port. Caller must
    ``await runner.cleanup()`` in a ``finally``."""
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = list(runner.addresses)[0][1]
    return runner, port


def _client_for(port: int) -> RedfishClient:
    """Build a RedfishClient pointed at the stub BMC on ``port``."""
    return RedfishClient("127.0.0.1", port=port, scheme="http")


def _hgx_fw(tmp_path, name: str = "HGX_FW_GPU_0.fwpkg") -> Firmware:
    """Tiny firmware blob with the requested filename. The filename is what
    ``update_hgx`` substring-matches against the inventory keys."""
    blob = tmp_path / name
    blob.write_bytes(b"\x42" * 4096)
    return Firmware(
        id=1, name=name, type="HGX", oem="nvidia",
        version="1.0", file_path=str(blob), sha256="ab" * 32, size_bytes=4096,
    )


# ---------------------------------------------------------------------------
# Stub BMC — exposes the inventory shape from the real BMC .193 audit:
# HGX + GPU + ERoT + InfoROM + HMC firmware members live alongside host
# BIOS / BMC under one /redfish/v1/UpdateService/FirmwareInventory.
# ---------------------------------------------------------------------------


class _HgxStubBMC:
    """An AMI MegaRAC BMC that publishes a realistic HGX inventory.

    Mirrors the shape captured in ``audit_update_apis.json`` for
    172.16.11.193: a single MultipartHttpPushUri at
    ``/redfish/v1/UpdateService/upload`` handles every component — BIOS,
    BMC, AND every HGX / GPU / ERoT / InfoROM firmware member.

    ``inventory_ids`` is configurable so each test exercises a specific
    slice (canonical 4 HGX + BIOS/BMC, Image1/Image2 pair, BIOS/BMC only).
    """

    def __init__(self, inventory_ids: list[str]):
        self.inventory_ids = list(inventory_ids)
        self.upload_seen = False
        self.upload_params: dict | None = None
        self.upload_oem_seen = False  # set True if the test BMC sees OemParameters
        self.upload_filename: str | None = None
        self.app = web.Application()
        r = self.app.router
        r.add_get("/redfish/v1/", self._service_root)
        r.add_get("/redfish/v1/UpdateService", self._update_service)
        r.add_get(
            "/redfish/v1/UpdateService/FirmwareInventory", self._fw_inv)
        r.add_get(
            "/redfish/v1/UpdateService/FirmwareInventory/{leaf}", self._fw_member)
        r.add_post(
            "/redfish/v1/UpdateService/upload", self._multipart_push)
        r.add_get("/redfish/v1/Systems", self._systems)
        r.add_get("/redfish/v1/Systems/1", self._system_one)
        r.add_get("/redfish/v1/Managers", self._managers)
        r.add_get("/redfish/v1/Managers/1", self._manager_one)
        r.add_get("/redfish/v1/TaskService", self._task_service)
        r.add_get("/redfish/v1/TaskService/Tasks", self._task_collection)
        r.add_get("/redfish/v1/TaskService/Tasks/1", self._task_one)

    async def _service_root(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/",
            "RedfishVersion": "1.15.1",
            "Vendor": "AMI",
            "Systems": {"@odata.id": "/redfish/v1/Systems"},
            "Managers": {"@odata.id": "/redfish/v1/Managers"},
            "UpdateService": {"@odata.id": "/redfish/v1/UpdateService"},
            "Tasks": {"@odata.id": "/redfish/v1/TaskService"},
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
                    "target":
                        "/redfish/v1/UpdateService/Actions/SimpleUpdate"},
            },
        })

    async def _fw_inv(self, _req):
        return web.json_response({"Members": [
            {"@odata.id":
             f"/redfish/v1/UpdateService/FirmwareInventory/{leaf}"}
            for leaf in self.inventory_ids
        ]})

    async def _fw_member(self, req):
        leaf = req.match_info["leaf"]
        return web.json_response({
            "Id": leaf, "Version": "1.0", "Updateable": True,
        })

    async def _multipart_push(self, req):
        """Capture the multipart parts so the test can assert what the
        plugin actually sent. Returns 202 + Location so the poll loop
        proceeds to GET /TaskService/Tasks/1."""
        self.upload_seen = True
        reader = await req.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "UpdateParameters":
                self.upload_params = json.loads((await part.read()).decode())
            elif part.name == "OemParameters":
                self.upload_oem_seen = True
                await part.read()
            elif part.name == "UpdateFile":
                # Capture the filename announced by the form-data part so a
                # future test can assert it round-trips correctly.
                self.upload_filename = part.filename
                await part.read()
        return web.Response(
            status=202,
            headers={"Location": "/redfish/v1/TaskService/Tasks/1"},
        )

    async def _systems(self, _req):
        return web.json_response(
            {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]})

    async def _system_one(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/Systems/1",
            "PowerState": "On",
            "Actions": {
                "#ComputerSystem.Reset": {
                    "target":
                        "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
                    "ResetType@Redfish.AllowableValues":
                        ["ForceRestart", "GracefulRestart", "PowerCycle"],
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

    async def _task_service(self, _req):
        return web.json_response({
            "@odata.id": "/redfish/v1/TaskService",
            "Tasks": {"@odata.id": "/redfish/v1/TaskService/Tasks"},
        })

    async def _task_collection(self, _req):
        return web.json_response({"Members": [
            {"@odata.id": "/redfish/v1/TaskService/Tasks/1"},
        ]})

    async def _task_one(self, _req):
        # Always Completed so the poll loop exits quickly.
        return web.json_response({
            "@odata.id": "/redfish/v1/TaskService/Tasks/1",
            "TaskState": "Completed",
            "PercentComplete": 100,
        })


# ---------------------------------------------------------------------------
# Test 1 — Discoverer classifies HGX/GPU inventory members.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discoverer_classifies_hgx_gpu_inventory():
    """Given a BMC that publishes a mix of HGX and host firmware members,
    the discoverer must:

    * Place every HGX_* / GPU / ERoT / InfoROM / HMC entry into
      ``cap.hgx_member_uris`` (keyed by the canonical leaf name).
    * Leave the host BIOS / BMC entries OUT of that dictionary — they
      still resolve into ``cap.bios_member_uri`` and ``cap.bmc_member_uri``
      as before so the existing flow continues to work.

    The audit shows real BMC .193 publishes 28+ HGX members; this test
    uses a representative 5-member slice (GPU, ERoT, InfoROM, HMC, HGX BMC)
    that exercises every token branch of ``_is_hgx_member``.
    """
    inventory_ids = [
        "HGX_FW_GPU_0",       # 'hgx', 'gpu' tokens
        "HGX_FW_GPU_1",
        "HGX_FW_ERoT_GPU_0",  # 'erot' + 'hgx' tokens
        "HGX_FW_BMC_0",       # 'hgx' wins over 'bmc' for HGX classification
        "HGX_InfoROM_GPU_0",  # 'inforom' + 'hgx' tokens
        "BIOS",               # host firmware — must NOT appear in hgx_member_uris
        "BMC",                # host firmware — must NOT appear in hgx_member_uris
    ]
    bmc = _HgxStubBMC(inventory_ids)
    runner, port = await _serve(bmc.app)
    client = _client_for(port)
    try:
        cap: CapabilityMap = await CapabilityDiscoverer(client).discover()

        # All 5 HGX_* entries land in the new dictionary…
        assert set(cap.hgx_member_uris.keys()) == {
            "HGX_FW_GPU_0",
            "HGX_FW_GPU_1",
            "HGX_FW_ERoT_GPU_0",
            "HGX_FW_BMC_0",
            "HGX_InfoROM_GPU_0",
        }, (
            "cap.hgx_member_uris should contain exactly the HGX_* entries; "
            f"got {sorted(cap.hgx_member_uris.keys())}"
        )
        # …and each URI is the full /redfish/v1/UpdateService/FirmwareInventory/<id>.
        for key, uri in cap.hgx_member_uris.items():
            assert uri.endswith(f"/FirmwareInventory/{key}"), (
                f"hgx_member_uris[{key!r}] = {uri!r} is not a FirmwareInventory URI")

        # Host BIOS / BMC still go to their normal fields — HGX
        # classification must NOT poach the host slots.
        assert cap.bios_member_uri is not None
        assert cap.bios_member_uri.endswith("/BIOS"), (
            f"BIOS picked {cap.bios_member_uri!r} — host BIOS member must "
            "still resolve normally")
        assert cap.bmc_member_uri is not None
        assert cap.bmc_member_uri.endswith("/BMC"), (
            f"BMC picked {cap.bmc_member_uri!r} — host BMC member must NOT "
            "be replaced by HGX_FW_BMC_0 which is the HGX-side BMC")

        # The host BIOS / BMC keys are NOT in hgx_member_uris (negative check).
        assert "BIOS" not in cap.hgx_member_uris
        assert "BMC" not in cap.hgx_member_uris
    finally:
        await client.close()
        await runner.cleanup()


# ---------------------------------------------------------------------------
# Test 2 — update_hgx() pushes multipart with the correct target.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_hgx_uses_multipart_push_with_correct_target(tmp_path):
    """``GenericRedfishPlugin.update_hgx`` must send a DMTF multipart push
    whose ``UpdateParameters`` part carries:

    * ``Targets`` — a list containing exactly one HGX FirmwareInventory URI
      (chosen from ``cap.hgx_member_uris``). The substring matcher should
      pick ``HGX_FW_GPU_0`` for a firmware named ``HGX_FW_GPU_0.fwpkg``.
    * ``@Redfish.OperationApplyTime`` — ``"OnReset"`` (HGX firmware applies
      on a host AC cycle, not on BMC reset; ``Immediate`` is wrong).
    * NO ``OemParameters`` part — HGX/GPU firmware on AMI BMCs does NOT
      use the ``ImageType`` field that BIOS/BMC updates require, and
      sending one would have the BMC reject the multipart body with 400.

    The plugin must also poll the returned Task to completion and report
    a success message that mentions the AC-cycle requirement so operators
    know the firmware is staged but not yet live.
    """
    inventory_ids = [
        "HGX_FW_GPU_0", "HGX_FW_GPU_1", "HGX_FW_GPU_2",
        "HGX_FW_ERoT_GPU_0", "HGX_InfoROM_GPU_0",
        "BIOS", "BMC",
    ]
    bmc = _HgxStubBMC(inventory_ids)
    runner, port = await _serve(bmc.app)
    try:
        plugin = GenericRedfishPlugin()
        plugin._FLASH_POLL_INTERVAL_S = 0.02
        server = ServerInfo(
            ip="127.0.0.1",
            redfish_root=f"http://127.0.0.1:{port}/redfish/v1/",
        )
        result = await plugin.update_hgx(
            server, _hgx_fw(tmp_path, "HGX_FW_GPU_0.fwpkg"),
            lambda s, p, m="": None,
            Credentials("ADMIN", "ADMIN"),
        )
        assert result.ok, result.message
        assert bmc.upload_seen, "multipart push was never received"

        # Targets must end with an HGX_* inventory URI — proves the plugin
        # consulted cap.hgx_member_uris and built the body correctly.
        assert isinstance(bmc.upload_params, dict)
        targets = bmc.upload_params.get("Targets")
        assert isinstance(targets, list) and len(targets) == 1, (
            f"Targets should be a single-element list, got {targets!r}")
        assert targets[0].startswith(
            "/redfish/v1/UpdateService/FirmwareInventory/HGX_"), (
            f"Targets[0] = {targets[0]!r} — expected an HGX_* member URI")
        # Substring router picked GPU_0 (the filename match), NOT another
        # GPU index — confirms the matcher works on the longest substring.
        assert targets[0].endswith("/HGX_FW_GPU_0"), (
            f"router picked {targets[0]!r} for HGX_FW_GPU_0.fwpkg; expected "
            "the matching HGX_FW_GPU_0 inventory entry")

        # ApplyTime must be OnReset — HGX needs an AC cycle to activate.
        assert (bmc.upload_params.get("@Redfish.OperationApplyTime")
                == "OnReset"), (
            f"ApplyTime should be OnReset, got "
            f"{bmc.upload_params.get('@Redfish.OperationApplyTime')!r}")

        # CRITICAL: no OemParameters part for HGX firmware. The AMI
        # ImageType field is for BIOS/BMC only.
        assert bmc.upload_oem_seen is False, (
            "OemParameters MUST NOT be sent for HGX firmware — AMI ImageType "
            "is BIOS/BMC-only and HGX BMCs reject the push when it's present")

        # Operator-facing message must surface the AC-cycle requirement so
        # they know to schedule a power cycle.
        assert "AC" in result.message and "cycle" in result.message.lower(), (
            f"result.message should mention an AC power cycle; got "
            f"{result.message!r}")
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# Test 3 — update_hgx() fails cleanly on a BMC with no HGX inventory.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_hgx_fails_cleanly_when_no_hgx_in_inventory(tmp_path):
    """A BMC with only BIOS / BMC in its FirmwareInventory (the common
    non-HGX case — every host in the operator's fleet except the GPU
    nodes) must NOT crash when ``update_hgx`` is called. It must return
    ``Result(ok=False, ...)`` with an actionable message explaining why.

    This is the orchestrator's protection against
    ``NotImplementedError`` — the prior contract crashed on the very
    first GPU-less host the operator tried to update.
    """
    inventory_ids = ["BIOS", "BMC"]  # no HGX, no GPU, no ERoT
    bmc = _HgxStubBMC(inventory_ids)
    runner, port = await _serve(bmc.app)
    try:
        plugin = GenericRedfishPlugin()
        plugin._FLASH_POLL_INTERVAL_S = 0.02
        server = ServerInfo(
            ip="127.0.0.1",
            redfish_root=f"http://127.0.0.1:{port}/redfish/v1/",
        )
        result = await plugin.update_hgx(
            server, _hgx_fw(tmp_path, "HGX_FW_GPU_0.fwpkg"),
            lambda s, p, m="": None,
            Credentials("ADMIN", "ADMIN"),
        )

        # The call returned cleanly (no NotImplementedError, no crash).
        assert result.ok is False, (
            f"expected ok=False, got result={result!r}")
        # The message points at the missing HGX inventory so the operator
        # understands why this host can't be flashed.
        msg = (result.message or "").lower()
        assert "hgx" in msg or "gpu" in msg, (
            f"result.message should explain the missing HGX inventory; "
            f"got {result.message!r}")
        # And the multipart push was never attempted — saves the operator
        # 30+ MB of useless upload time.
        assert bmc.upload_seen is False, (
            "plugin must NOT push firmware when no HGX target exists")
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# Test 4 — Discoverer skips HGX Image2 (recovery) banks.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discoverer_skips_hgx_image2_banks():
    """Dual-image HGX builds publish ``HGX_FW_GPU_0_Image1`` (active) and
    ``HGX_FW_GPU_0_Image2`` (recovery). The discoverer MUST select only
    Image1 in ``cap.hgx_member_uris`` — flashing the recovery slot
    destroys the rollback path. Same safety contract that protects
    BIOSImage1 / BMCImage1 on dual-image AMI builds.
    """
    inventory_ids = [
        "HGX_FW_GPU_0_Image1",   # active — should be selected
        "HGX_FW_GPU_0_Image2",   # recovery — must be excluded
        "BIOS",
        "BMC",
    ]
    bmc = _HgxStubBMC(inventory_ids)
    runner, port = await _serve(bmc.app)
    client = _client_for(port)
    try:
        cap = await CapabilityDiscoverer(client).discover()

        # The active bank is present.
        assert "HGX_FW_GPU_0_Image1" in cap.hgx_member_uris, (
            f"active HGX bank Image1 missing from {sorted(cap.hgx_member_uris)}")
        # The recovery bank is explicitly excluded — flashing it would
        # corrupt the operator's rollback option.
        assert "HGX_FW_GPU_0_Image2" not in cap.hgx_member_uris, (
            "HGX_FW_GPU_0_Image2 is a recovery bank and must never appear "
            "in hgx_member_uris — flashing it destroys rollback")
        # The Image2 entry still appears in the full firmware_inventory so
        # the operator can see what's there; it's just tagged inactive.
        assert "HGX_FW_GPU_0_Image2" in cap.firmware_inventory
        assert (cap.firmware_inventory["HGX_FW_GPU_0_Image2"].is_active_bank
                is False)
        assert (cap.firmware_inventory["HGX_FW_GPU_0_Image1"].is_active_bank
                is True)
    finally:
        await client.close()
        await runner.cleanup()
