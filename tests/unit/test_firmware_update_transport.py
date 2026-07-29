"""Firmware update transport selection + multipart push.

Verifies the fix for "can't update BIOS/BMC on real hardware":

* When the BMC advertises ``MultipartHttpPushUri``, the plugin pushes the
  firmware bytes via multipart/form-data (the real-hardware path) — NOT a
  ``file:///`` SimpleUpdate.
* When it does not, the plugin falls back to ``SimpleUpdate`` (simulator).
* The poll loop drives a Task to completion and verifies.

A tiny in-process aiohttp app stands in for the BMC so we never touch real
hardware (a real flash would brick the box).
"""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from core.models.firmware import Firmware
from core.models.server import Credentials, ServerInfo
from plugins.generic_redfish.plugin import GenericRedfishPlugin


def _fw(tmp_path):
    blob = tmp_path / "BIOS_3.1.bin"
    blob.write_bytes(b"\xDE\xAD\xBE\xEF" * 1024)   # 4 KiB dummy image
    return Firmware(
        id=1, name="BIOS_3.1.bin", type="BIOS", oem="supermicro",
        version="3.1", file_path=str(blob), sha256="ab"*32, size_bytes=4096,
    )


class _MockBMC:
    """Minimal Redfish BMC. ``multipart`` flag toggles the advertised
    transport so we can drive both code paths. ``firmware_inventory`` flag
    toggles whether the BMC exposes the DMTF-canonical FirmwareInventory
    collection (modern BMCs do; old/simulator may not)."""

    def __init__(self, multipart: bool, firmware_inventory: bool = True):
        self.multipart = multipart
        self.firmware_inventory = firmware_inventory
        self.received_bytes = b""
        self.received_params = ""
        self.simple_update_body = None
        self.app = web.Application()
        r = self.app.router
        r.add_get("/redfish/v1/Managers", self._managers)
        r.add_get("/redfish/v1/Systems", self._systems)
        r.add_get("/redfish/v1/Systems/1", self._system_one)
        r.add_get("/redfish/v1/UpdateService", self._update_service)
        r.add_get("/redfish/v1/UpdateService/FirmwareInventory", self._fw_inventory)
        r.add_get("/redfish/v1/UpdateService/FirmwareInventory/BIOS", self._fw_bios)
        r.add_get("/redfish/v1/UpdateService/FirmwareInventory/BMC", self._fw_bmc)
        r.add_post("/redfish/v1/UpdateService/upload", self._multipart_push)
        r.add_post("/redfish/v1/UpdateService/Actions/SimpleUpdate", self._simple_update)
        r.add_get("/redfish/v1/TaskService/Tasks/1", self._task)

    async def _managers(self, req):
        return web.json_response({"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]})

    async def _systems(self, req):
        return web.json_response({"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]})

    async def _system_one(self, req):
        return web.json_response({"BiosVersion": "3.1"})

    async def _update_service(self, req):
        doc = {"ServiceEnabled": True, "Actions": {
            "#UpdateService.SimpleUpdate": {
                "target": "/redfish/v1/UpdateService/Actions/SimpleUpdate"}}}
        if self.multipart:
            doc["MultipartHttpPushUri"] = "/redfish/v1/UpdateService/upload"
        if self.firmware_inventory:
            doc["FirmwareInventory"] = {
                "@odata.id": "/redfish/v1/UpdateService/FirmwareInventory"}
        return web.json_response(doc)

    async def _fw_inventory(self, req):
        if not self.firmware_inventory:
            return web.Response(status=404)
        return web.json_response({"Members": [
            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/BIOS"},
            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/BMC"},
        ]})

    async def _fw_bios(self, req):
        return web.json_response({"Id": "BIOS", "Version": "3.1"})

    async def _fw_bmc(self, req):
        return web.json_response({"Id": "BMC", "Version": "1.10"})

    async def _multipart_push(self, req):
        reader = await req.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "UpdateParameters":
                self.received_params = (await part.read()).decode()
            elif part.name == "UpdateFile":
                self.received_bytes = await part.read(decode=False)
        return web.Response(status=202, headers={"Location": "/redfish/v1/TaskService/Tasks/1"})

    async def _simple_update(self, req):
        self.simple_update_body = await req.json()
        return web.Response(status=202, headers={"Location": "/redfish/v1/TaskService/Tasks/1"})

    async def _task(self, req):
        # Always report Completed immediately for the test.
        return web.json_response({
            "@odata.id": "/redfish/v1/TaskService/Tasks/1",
            "TaskState": "Completed", "PercentComplete": 100,
        })


async def _serve(mock: _MockBMC):
    runner = web.AppRunner(mock.app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = list(runner.addresses)[0][1]
    return runner, port


@pytest.mark.asyncio
async def test_multipart_push_used_when_advertised(tmp_path):
    mock = _MockBMC(multipart=True)
    runner, port = await _serve(mock)
    try:
        plugin = GenericRedfishPlugin()
        # Speed up the poll for the test
        plugin._FLASH_POLL_INTERVAL_S = 0.05
        server = ServerInfo(ip="127.0.0.1",
                            redfish_root=f"http://127.0.0.1:{port}/redfish/v1/")
        # _client_for parses host/port/scheme from redfish_root
        result = await plugin.update_bios(
            server, _fw(tmp_path), lambda s, p, m="": None,
            Credentials("ADMIN", "ADMIN"),
        )
        assert result.ok, result.message
        # The firmware bytes actually arrived at the BMC
        assert len(mock.received_bytes) == 4096
        # And the SimpleUpdate file:/// path was NOT used
        assert mock.simple_update_body is None
        # DMTF-canonical: targets are FirmwareInventory URIs, NOT /Systems/1
        # (this is the fix — the old code used /Systems/1 which real BMCs reject)
        assert "/redfish/v1/UpdateService/FirmwareInventory/BIOS" in mock.received_params
        assert "OnReset" in mock.received_params
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_simple_update_fallback_when_no_multipart(tmp_path):
    mock = _MockBMC(multipart=False)
    runner, port = await _serve(mock)
    try:
        plugin = GenericRedfishPlugin()
        plugin._FLASH_POLL_INTERVAL_S = 0.05
        server = ServerInfo(ip="127.0.0.1",
                            redfish_root=f"http://127.0.0.1:{port}/redfish/v1/")
        result = await plugin.update_bios(
            server, _fw(tmp_path), lambda s, p, m="": None,
            Credentials("ADMIN", "ADMIN"),
        )
        assert result.ok, result.message
        # Fell back to SimpleUpdate, nothing pushed via multipart
        assert mock.received_bytes == b""
        assert mock.simple_update_body is not None
        assert mock.simple_update_body["ImageURI"].startswith("file:///")
    finally:
        await runner.cleanup()
