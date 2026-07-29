"""Multi-host Redfish simulator for ClusterOne dev + CI.

Spins up one aiohttp app per simulated BMC, each on a separate localhost port,
so ClusterOne can exercise the full discovery + update pipeline against
deterministic fake hosts without touching real hardware.

Run::

    python -m tests.sim.redfish_sim                # 8 hosts on 8000..8007
    python -m tests.sim.redfish_sim --port 9000    # base port override
    python -m tests.sim.redfish_sim --auth user:pw # require Basic auth

Then in ClusterOne's discovery dialog point at::

    http://127.0.0.1:8000-8007

The simulator implements ``UpdateService.SimpleUpdate`` + the matching
``TaskService/Tasks`` resource. After a (short, configurable) flash duration
the host's reported ``BiosVersion`` or ``FirmwareVersion`` flips to the version
supplied in the request body (``Oem.ClusterOne.TargetVersion``).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import re
import time

from aiohttp import web

log = logging.getLogger("redfish_sim")

# (hostname, manufacturer, model, serial, bios_ver, bmc_ver, power_state, oem_key)
DEFAULT_FLEET = [
    ("sim-s01", "Supermicro",  "H13DSG-O",               "S1A0011", "2.1",  "1.04.06", "On",  "Supermicro"),
    ("sim-d01", "Dell Inc.",   "PowerEdge R760",         "DLR7601", "1.4",  "7.10.30", "On",  "Dell"),
    ("sim-h01", "HPE",         "ProLiant DL380 Gen11",   "HPL3801", "1.6",  "2.44",    "On",  "Hpe"),
    ("sim-l01", "Lenovo",      "ThinkSystem SR650 V3",   "LNV6501", "2.0",  "3.10",    "On",  "Lenovo"),
    ("sim-g01", "Gigabyte",    "R283-Z90",               "GBZ9001", "C20",  "12.05",   "On",  "Gigabyte"),
    ("sim-a01", "ASRockRack",  "GENOA-D8",               "ASR8001", "3.2",  "1.18",    "Off", "ASRockRack"),
    ("sim-u01", "ASUS",        "RS720A",                 "ASU7201", "0805", "1.34",    "On",  "ASUS"),
    ("sim-m01", "MSI",         "S2206",                  "MSI2201", "1.1",  "1.02",    "On",  "MSI"),
]

FLASH_SECONDS = 4.0   # how long a simulated flash takes


def _auth_middleware(expected: tuple[str, str] | None):
    @web.middleware
    async def middleware(request: web.Request, handler):
        if expected is None:
            return await handler(request)
        header = request.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return web.Response(status=401, headers={"WWW-Authenticate": "Basic"})
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, password = decoded.partition(":")
        except Exception:
            return web.Response(status=401)
        if (user, password) != expected:
            return web.Response(status=401)
        return await handler(request)
    return middleware


def make_app(host_data: tuple, auth: tuple[str, str] | None = None) -> web.Application:
    name, manuf, model, serial, bios, bmc, power, oem_key = host_data

    # Per-host mutable state (so flashes actually change the reported version).
    state = {
        "bios_version": bios,
        "bmc_version": bmc,
        "power_state": power,
        "tasks": {},          # id -> {started, target_version, target_type, applied}
        "next_task_id": 1,
    }

    app = web.Application(middlewares=[_auth_middleware(auth)])

    async def service_root(_req):
        return web.json_response({
            "@odata.id": "/redfish/v1/",
            "@odata.type": "#ServiceRoot.v1_5_0.ServiceRoot",
            "Id": "RootService",
            "Name": "Root Service",
            "Vendor": manuf,
            "Systems": {"@odata.id": "/redfish/v1/Systems"},
            "Managers": {"@odata.id": "/redfish/v1/Managers"},
            "UpdateService": {"@odata.id": "/redfish/v1/UpdateService"},
            "TaskService": {"@odata.id": "/redfish/v1/TaskService"},
            "Oem": {oem_key: {"@odata.type": f"#{oem_key}.Oem"}},
        })

    async def systems_collection(_req):
        return web.json_response({
            "@odata.id": "/redfish/v1/Systems",
            "@odata.type": "#ComputerSystemCollection.ComputerSystemCollection",
            "Members": [{"@odata.id": "/redfish/v1/Systems/1"}],
            "Members@odata.count": 1,
        })

    async def system_one(_req):
        return web.json_response({
            "@odata.id": "/redfish/v1/Systems/1",
            "@odata.type": "#ComputerSystem.v1_15_0.ComputerSystem",
            "Id": "1",
            "Name": name,
            "HostName": name,
            "Manufacturer": manuf,
            "Model": model,
            "SerialNumber": serial,
            "BiosVersion": state["bios_version"],
            "PowerState": state["power_state"],
        })

    async def managers_collection(_req):
        return web.json_response({
            "@odata.id": "/redfish/v1/Managers",
            "Members": [{"@odata.id": "/redfish/v1/Managers/1"}],
            "Members@odata.count": 1,
        })

    async def manager_one(_req):
        return web.json_response({
            "@odata.id": "/redfish/v1/Managers/1",
            "@odata.type": "#Manager.v1_10_0.Manager",
            "Id": "1",
            "ManagerType": "BMC",
            "FirmwareVersion": state["bmc_version"],
            "Manufacturer": manuf,
            "Model": "BMC",
        })

    async def update_service(_req):
        return web.json_response({
            "@odata.id": "/redfish/v1/UpdateService",
            "@odata.type": "#UpdateService.v1_8_0.UpdateService",
            "Id": "UpdateService",
            "ServiceEnabled": True,
            "Actions": {
                "#UpdateService.SimpleUpdate": {
                    "target": "/redfish/v1/UpdateService/Actions/SimpleUpdate",
                },
            },
        })

    async def simple_update(req):
        try:
            body = await req.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON")

        target_version = (
            (body.get("Oem") or {}).get("ClusterOne", {}).get("TargetVersion")
        )
        target_type = (
            (body.get("Oem") or {}).get("ClusterOne", {}).get("TargetType")
        )
        # Fallbacks if caller doesn't use our Oem extension
        if not target_version:
            m = re.search(r"_([\d.]+)\b", body.get("ImageURI", ""))
            if m:
                target_version = m.group(1)
        if not target_type:
            targets = body.get("Targets") or []
            target_type = "BMC" if any("Manager" in str(t) for t in targets) else "BIOS"

        tid = str(state["next_task_id"])
        state["next_task_id"] += 1
        state["tasks"][tid] = {
            "started": time.time(),
            "target_version": target_version or "unknown",
            "target_type": target_type,
            "applied": False,
        }
        task_uri = f"/redfish/v1/TaskService/Tasks/{tid}"
        return web.Response(status=202, headers={"Location": task_uri})

    async def task_resource(req):
        tid = req.match_info["id"]
        task = state["tasks"].get(tid)
        if not task:
            return web.Response(status=404)
        elapsed = time.time() - task["started"]
        pct = min(int(elapsed / FLASH_SECONDS * 100), 100)
        if pct >= 100 and not task["applied"]:
            if task["target_type"] == "BMC":
                state["bmc_version"] = task["target_version"]
            else:
                state["bios_version"] = task["target_version"]
            task["applied"] = True
        task_state = "Completed" if pct >= 100 else "Running"
        return web.json_response({
            "@odata.id": f"/redfish/v1/TaskService/Tasks/{tid}",
            "@odata.type": "#Task.v1_5_0.Task",
            "Id": tid,
            "Name": f"Task {tid}",
            "TaskState": task_state,
            "TaskStatus": "OK" if task_state == "Completed" else "OK",
            "PercentComplete": pct,
        })

    # Routes
    app.router.add_get("/redfish/v1/", service_root)
    app.router.add_get("/redfish/v1", service_root)
    app.router.add_get("/redfish/v1/Systems", systems_collection)
    app.router.add_get("/redfish/v1/Systems/1", system_one)
    app.router.add_get("/redfish/v1/Managers", managers_collection)
    app.router.add_get("/redfish/v1/Managers/1", manager_one)
    app.router.add_get("/redfish/v1/UpdateService", update_service)
    app.router.add_post("/redfish/v1/UpdateService/Actions/SimpleUpdate", simple_update)
    app.router.add_get("/redfish/v1/TaskService/Tasks/{id}", task_resource)
    return app


async def run(port_base: int, host: str, auth: tuple[str, str] | None, fleet=None) -> None:
    fleet = fleet or DEFAULT_FLEET
    runners: list[web.AppRunner] = []
    print(f"[redfish-sim] starting {len(fleet)} hosts on {host}:{port_base}..{port_base + len(fleet) - 1}")
    for idx, host_data in enumerate(fleet):
        app = make_app(host_data, auth=auth)
        runner = web.AppRunner(app)
        await runner.setup()
        port = port_base + idx
        site = web.TCPSite(runner, host, port)
        await site.start()
        print(f"  {host_data[0]:<10} {host_data[1]:<13} {host_data[2]:<25} http://{host}:{port}/redfish/v1/")
        runners.append(runner)
    print("[redfish-sim] press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for r in runners:
            await r.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="ClusterOne Redfish simulator")
    parser.add_argument("--port", type=int, default=8000, help="base TCP port (default 8000)")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    parser.add_argument("--auth", help="require Basic auth, e.g. user:password")
    args = parser.parse_args()

    auth = None
    if args.auth:
        u, _, p = args.auth.partition(":")
        auth = (u, p)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s : %(message)s")
    try:
        asyncio.run(run(args.port, args.host, auth))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
