"""End-to-end discovery against the in-process Redfish simulator."""
from __future__ import annotations

import pytest
from aiohttp import web

from core.engine.scanner import parse_range, scan
from core.models.server import Credentials
from core.plugins_api.registry import PluginRegistry
from plugins.generic_redfish.plugin import GenericRedfishPlugin
from tests.sim.redfish_sim import DEFAULT_FLEET, make_app


@pytest.mark.asyncio
async def test_discover_single_simulated_host():
    fleet = DEFAULT_FLEET[:2]
    runners: list[web.AppRunner] = []
    for idx, host in enumerate(fleet):
        app = make_app(host)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 18100 + idx).start()
        runners.append(runner)

    try:
        registry = PluginRegistry()
        registry._plugins.append(GenericRedfishPlugin())

        targets = parse_range(f"http://127.0.0.1:18100-{18100 + len(fleet) - 1}")
        creds = Credentials(username="", password="")
        found: list = []

        stats = await scan(
            targets, creds, registry,
            on_found=lambda info: found.append(info),
            on_progress=lambda s, f, t: None,
        )

        assert stats["found"] == len(fleet)
        assert len(found) == len(fleet)
        ips = {s.ip for s in found}
        assert ips == {"127.0.0.1"}
        # Identity propagated from sim's Systems/Managers
        assert all(s.bios_version for s in found)
        assert all(s.bmc_version for s in found)
    finally:
        for r in runners:
            await r.cleanup()
