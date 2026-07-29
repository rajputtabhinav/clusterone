"""Supermicro flash recipe — verifies the exact field structure the AMI BMC
needs (proven against real hardware + the operator's production scripts):

* Targets are FirmwareInventory URIs (NOT /Systems/1 or /Managers/1)
* OemParameters {"ImageType": ...} part is present
* BMC uses ApplyTime Immediate; BIOS uses OnReset + SmcUpdateService.Install
"""
from __future__ import annotations

import json

import pytest
from aiohttp import web

from core.models.firmware import Firmware
from core.models.server import Credentials, ServerInfo
from plugins.supermicro.plugin import SupermicroPlugin


def _fw(tmp_path, fw_type):
    blob = tmp_path / ("img_" + fw_type)
    blob.write_bytes(b"\x00" * 2048)
    return Firmware(id=1, name=blob.name, type=fw_type, oem="supermicro",
                    version="9.9", file_path=str(blob), sha256="ab"*32, size_bytes=2048)


class _SmcMock:
    def __init__(self):
        self.upload_params = None
        self.upload_oem = None
        self.upload_seen = False
        self.install_called = False
        self.resets = []
        self.app = web.Application()
        r = self.app.router
        r.add_post("/redfish/v1/UpdateService/upload", self._upload)
        r.add_post("/redfish/v1/UpdateService/Actions/Oem/SmcUpdateService.Install", self._install)
        r.add_post("/redfish/v1/Systems/1/Actions/ComputerSystem.Reset", self._reset)
        r.add_get("/redfish/v1/Systems/1", self._system)
        r.add_get("/redfish/v1/TaskService/Tasks", self._tasks)
        r.add_get("/redfish/v1/TaskService/Tasks/1", self._task1)
        r.add_get("/redfish/v1/UpdateService/FirmwareInventory/BMC", self._ver)
        r.add_get("/redfish/v1/UpdateService/FirmwareInventory/BIOS", self._ver)

    async def _upload(self, req):
        self.upload_seen = True
        reader = await req.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "UpdateParameters":
                self.upload_params = json.loads((await part.read()).decode())
            elif part.name == "OemParameters":
                self.upload_oem = json.loads((await part.read()).decode())
            elif part.name == "UpdateFile":
                await part.read()
        return web.Response(status=202, headers={"Location": "/redfish/v1/TaskService/Tasks/1"})

    async def _install(self, req):
        self.install_called = True
        return web.json_response({"ok": True})

    async def _reset(self, req):
        body = await req.json()
        self.resets.append(body.get("ResetType"))
        return web.Response(status=204)

    async def _system(self, req):
        # Report the power state that matches the last reset so _wait_power returns fast
        last = self.resets[-1] if self.resets else "On"
        ps = "Off" if last == "ForceOff" else "On"
        return web.json_response({"PowerState": ps})

    async def _tasks(self, req):
        return web.json_response({"Members": [{"@odata.id": "/redfish/v1/TaskService/Tasks/1"}]})

    async def _task1(self, req):
        return web.json_response({"Name": "BIOS Update", "TaskState": "Completed", "PercentComplete": 100})

    async def _ver(self, req):
        return web.json_response({"Version": "9.9"})


async def _serve(mock):
    runner = web.AppRunner(mock.app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    return runner, list(runner.addresses)[0][1]


@pytest.mark.asyncio
async def test_supermicro_bmc_uses_firmwareinventory_target_and_oem(tmp_path):
    mock = _SmcMock()
    runner, port = await _serve(mock)
    try:
        plugin = SupermicroPlugin()
        plugin._POLL_INTERVAL_S = 0.02
        plugin._UPLOAD_SETTLE_S = 0.02
        plugin._BIOS_TASK_PREWAIT_S = 0.02
        server = ServerInfo(ip="127.0.0.1", redfish_root=f"http://127.0.0.1:{port}/redfish/v1/")
        result = await plugin.update_bmc(server, _fw(tmp_path, "BMC"),
                                         lambda s, p, m="": None, Credentials("ADMIN", "ADMIN"))
        assert result.ok, result.message
        assert mock.upload_seen
        # The critical fix: FirmwareInventory target, not /Managers/1
        assert mock.upload_params["Targets"] == ["/redfish/v1/UpdateService/FirmwareInventory/BMC"]
        assert mock.upload_params["@Redfish.OperationApplyTime"] == "Immediate"
        assert mock.upload_oem == {"ImageType": "BMC"}
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_supermicro_bios_stage_only_when_not_apply_now(tmp_path):
    mock = _SmcMock()
    runner, port = await _serve(mock)
    try:
        plugin = SupermicroPlugin()
        plugin._POLL_INTERVAL_S = 0.02
        plugin._UPLOAD_SETTLE_S = 0.02
        plugin._BIOS_TASK_PREWAIT_S = 0.02
        server = ServerInfo(ip="127.0.0.1", redfish_root=f"http://127.0.0.1:{port}/redfish/v1/")
        result = await plugin.update_bios(server, _fw(tmp_path, "BIOS"),
                                          lambda s, p, m="": None, Credentials("ADMIN", "ADMIN"),
                                          apply_now=False)
        assert result.ok, result.message
        assert mock.upload_params["Targets"] == ["/redfish/v1/UpdateService/FirmwareInventory/BIOS"]
        assert mock.upload_params["@Redfish.OperationApplyTime"] == "OnReset"
        assert mock.install_called           # OEM install trigger fired
        assert mock.resets == []             # no power-cycle when staging only
        assert "staged" in result.message.lower()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_supermicro_bios_apply_now_power_cycles(tmp_path):
    mock = _SmcMock()
    runner, port = await _serve(mock)
    try:
        plugin = SupermicroPlugin()
        plugin._POLL_INTERVAL_S = 0.02
        plugin._UPLOAD_SETTLE_S = 0.02
        plugin._BIOS_TASK_PREWAIT_S = 0.02
        # Speed up the BIOS-task wait (test mock returns Completed immediately)
        import plugins.supermicro.plugin as smod
        server = ServerInfo(ip="127.0.0.1", redfish_root=f"http://127.0.0.1:{port}/redfish/v1/")
        # Patch the 60s pre-wait + interval for the test
        orig_sleep = __import__("asyncio").sleep
        result = await plugin.update_bios(server, _fw(tmp_path, "BIOS"),
                                          lambda s, p, m="": None, Credentials("ADMIN", "ADMIN"),
                                          apply_now=True)
        assert result.ok, result.message
        # ForceOff before upload, On after install — the proven sequence
        assert "ForceOff" in mock.resets
        assert "On" in mock.resets
        assert mock.install_called
    finally:
        await runner.cleanup()
