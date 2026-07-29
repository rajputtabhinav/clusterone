"""Per-OEM Redfish update recipe tests.

Each OEM's flash recipe is locked in against an in-process mock BMC so any
regression in target resolution, OemParameters injection, or transport
selection is caught before it reaches real hardware (where the cost of a
failed flash is much higher).

Recipes are authored from published vendor docs and reference scripts:

* **Dell iDRAC**:   ``DeviceFirmwareMultipartUploadREDFISH.py`` (dell GitHub)
                    → empty Targets, no OemParameters
* **Lenovo XCC**:   pubs.lenovo.com/xcc-restapi
                    → FirmwareInventory targets, no OemParameters
* **AMI MegaRAC**:  Supermicro / ASRock Rack / Tyrone production scripts
                    → FirmwareInventory targets + ``OemParameters{ImageType}``
"""
from __future__ import annotations

import json

import pytest
from aiohttp import web

from core.models.firmware import Firmware
from core.models.server import Credentials, ServerInfo
from plugins.asrock.plugin import AsrockPlugin
from plugins.dell.plugin import DellPlugin
from plugins.gigabyte.plugin import GigabytePlugin
from plugins.lenovo.plugin import LenovoPlugin


def _fw(tmp_path, kind):
    blob = tmp_path / f"{kind.lower()}_image.bin"
    blob.write_bytes(b"\x00" * 4096)
    return Firmware(id=1, name=blob.name, type=kind, oem="any",
                    version="9.9", file_path=str(blob), sha256="ab"*32,
                    size_bytes=4096)


class _UploadCapture:
    """Tiny BMC that captures the three multipart parts so each per-OEM
    test can assert the EXACT shape the vendor expects."""
    def __init__(self, *, fw_inventory=("BIOS", "BMC")):
        self.fw_inventory = list(fw_inventory)
        self.upload_uri = "/redfish/v1/UpdateService/MultipartUpload"
        self.params: dict | None = None
        self.oem_params: dict | None = None
        self.upload_seen = False
        self.app = web.Application()
        r = self.app.router
        r.add_get("/redfish/v1/UpdateService", self._update_service)
        r.add_get("/redfish/v1/UpdateService/FirmwareInventory", self._inv)
        for n in fw_inventory:
            r.add_get(f"/redfish/v1/UpdateService/FirmwareInventory/{n}", self._member(n))
        r.add_post(self.upload_uri, self._upload)
        r.add_post("/redfish/v1/UpdateService/upload", self._upload)
        r.add_get("/redfish/v1/TaskService/Tasks/1", self._task)
        r.add_get("/redfish/v1/Systems", self._sys_coll)
        r.add_get("/redfish/v1/Systems/1", self._sys)
        r.add_get("/redfish/v1/Managers", self._mgr_coll)
        r.add_get("/redfish/v1/Managers/1", self._mgr)

    async def _update_service(self, req):
        return web.json_response({
            "MultipartHttpPushUri": self.upload_uri,
            "FirmwareInventory": {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory"},
            "Actions": {"#UpdateService.SimpleUpdate": {
                "target": "/redfish/v1/UpdateService/Actions/SimpleUpdate"}},
        })

    async def _inv(self, req):
        return web.json_response({"Members": [
            {"@odata.id": f"/redfish/v1/UpdateService/FirmwareInventory/{n}"}
            for n in self.fw_inventory]})

    def _member(self, name):
        async def handler(req):
            return web.json_response({"Id": name, "Version": "9.9"})
        return handler

    async def _upload(self, req):
        self.upload_seen = True
        reader = await req.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "UpdateParameters":
                self.params = json.loads((await part.read()).decode())
            elif part.name == "OemParameters":
                self.oem_params = json.loads((await part.read()).decode())
            elif part.name == "UpdateFile":
                await part.read()
        return web.Response(status=202, headers={"Location": "/redfish/v1/TaskService/Tasks/1"})

    async def _task(self, req):
        return web.json_response({
            "@odata.id": "/redfish/v1/TaskService/Tasks/1",
            "TaskState": "Completed", "PercentComplete": 100})

    async def _sys_coll(self, req):
        return web.json_response({"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]})

    async def _sys(self, req):
        return web.json_response({"BiosVersion": "9.9"})

    async def _mgr_coll(self, req):
        return web.json_response({"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]})

    async def _mgr(self, req):
        return web.json_response({"FirmwareVersion": "9.9"})


async def _serve(mock):
    runner = web.AppRunner(mock.app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    return runner, list(runner.addresses)[0][1]


# ---- Dell iDRAC -----------------------------------------------------
@pytest.mark.asyncio
async def test_dell_idrac_empty_targets_no_oem(tmp_path):
    """Dell iDRAC: Targets MUST be empty (iDRAC auto-routes via DUP header).
    Passing a FirmwareInventory URI causes RED097."""
    mock = _UploadCapture()
    runner, port = await _serve(mock)
    try:
        plugin = DellPlugin()
        plugin._FLASH_POLL_INTERVAL_S = 0.02
        server = ServerInfo(ip="127.0.0.1", redfish_root=f"http://127.0.0.1:{port}/redfish/v1/")
        result = await plugin.update_bios(server, _fw(tmp_path, "BIOS"),
                                          lambda s, p, m="": None, Credentials("root", "calvin"))
        assert result.ok, result.message
        assert mock.upload_seen
        # Critical: Targets key must be OMITTED entirely for Dell — empty
        # list ``[]`` triggers RED097 ("target mismatch") on some iDRAC9
        # builds. Dell auto-routes by the DUP/.d7/.d9 header.
        assert "Targets" not in mock.params, (
            f"Dell got Targets={mock.params.get('Targets')!r} but the key "
            "must be omitted, not sent as empty list"
        )
        # Dell doesn't want OemParameters
        assert mock.oem_params is None
    finally:
        await runner.cleanup()


# ---- Lenovo XCC -----------------------------------------------------
@pytest.mark.asyncio
async def test_lenovo_xcc_uses_firmwareinventory_no_oem(tmp_path):
    """Lenovo XCC: FirmwareInventory URI targets, no OemParameters."""
    # XCC's typical inventory IDs include UEFI and BMC-Primary; the resolver
    # should pick the right one.
    mock = _UploadCapture(fw_inventory=("UEFI", "BMC-Primary", "BMC-Backup", "LXPM"))
    runner, port = await _serve(mock)
    try:
        plugin = LenovoPlugin()
        plugin._FLASH_POLL_INTERVAL_S = 0.02
        server = ServerInfo(ip="127.0.0.1", redfish_root=f"http://127.0.0.1:{port}/redfish/v1/")
        result = await plugin.update_bios(server, _fw(tmp_path, "BIOS"),
                                          lambda s, p, m="": None, Credentials("USERID", "PASSW0RD"))
        assert result.ok, result.message
        # BIOS resolves to the UEFI inventory entry
        assert mock.params["Targets"] == ["/redfish/v1/UpdateService/FirmwareInventory/UEFI"]
        # Lenovo doesn't want OemParameters
        assert mock.oem_params is None
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_lenovo_xcc_bmc_targets_primary_not_backup(tmp_path):
    """Lenovo XCC BMC flash: must pick BMC-Primary, NOT BMC-Backup."""
    mock = _UploadCapture(fw_inventory=("BMC-Backup", "BMC-Primary", "UEFI"))
    runner, port = await _serve(mock)
    try:
        plugin = LenovoPlugin()
        plugin._FLASH_POLL_INTERVAL_S = 0.02
        server = ServerInfo(ip="127.0.0.1", redfish_root=f"http://127.0.0.1:{port}/redfish/v1/")
        result = await plugin.update_bmc(server, _fw(tmp_path, "BMC"),
                                         lambda s, p, m="": None, Credentials("USERID", "PASSW0RD"))
        assert result.ok, result.message
        assert mock.params["Targets"] == ["/redfish/v1/UpdateService/FirmwareInventory/BMC-Primary"]
    finally:
        await runner.cleanup()


# ---- ASRock Rack (AMI MegaRAC) --------------------------------------
@pytest.mark.asyncio
async def test_asrock_includes_oem_imagetype(tmp_path):
    """ASRock Rack runs AMI MegaRAC — needs OemParameters{ImageType:BIOS}."""
    mock = _UploadCapture()
    runner, port = await _serve(mock)
    try:
        plugin = AsrockPlugin()
        plugin._FLASH_POLL_INTERVAL_S = 0.02
        server = ServerInfo(ip="127.0.0.1", redfish_root=f"http://127.0.0.1:{port}/redfish/v1/")
        result = await plugin.update_bios(server, _fw(tmp_path, "BIOS"),
                                          lambda s, p, m="": None, Credentials("admin", "admin"))
        assert result.ok, result.message
        # FirmwareInventory target
        assert mock.params["Targets"] == ["/redfish/v1/UpdateService/FirmwareInventory/BIOS"]
        # AMI requires ImageType
        assert mock.oem_params == {"ImageType": "BIOS"}
    finally:
        await runner.cleanup()


# ---- Gigabyte (AMI MegaRAC) -----------------------------------------
@pytest.mark.asyncio
async def test_gigabyte_includes_oem_imagetype_bmc(tmp_path):
    """Gigabyte also runs AMI MegaRAC — same recipe as ASRock and Supermicro."""
    mock = _UploadCapture()
    runner, port = await _serve(mock)
    try:
        plugin = GigabytePlugin()
        plugin._FLASH_POLL_INTERVAL_S = 0.02
        server = ServerInfo(ip="127.0.0.1", redfish_root=f"http://127.0.0.1:{port}/redfish/v1/")
        result = await plugin.update_bmc(server, _fw(tmp_path, "BMC"),
                                         lambda s, p, m="": None, Credentials("admin", "admin"))
        assert result.ok, result.message
        assert mock.params["Targets"] == ["/redfish/v1/UpdateService/FirmwareInventory/BMC"]
        assert mock.oem_params == {"ImageType": "BMC"}
        # BMC apply time is Immediate
        assert mock.params["@Redfish.OperationApplyTime"] == "Immediate"
    finally:
        await runner.cleanup()
