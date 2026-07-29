"""FirmwareInventory resolution across the AMI MegaRAC variants discovered
in the operator's 172.16.0.0/16 fleet (audit_update_apis.json — 26 BMCs in
6 distinct schemas).

These tests lock in the resolver's behavior so regressions can't break
flashes on any of the real boards in the fleet.
"""
from __future__ import annotations

import pytest

from plugins.generic_redfish.plugin import GenericRedfishPlugin


class _StubClient:
    """Minimal RedfishClient stub — only get_json is exercised here."""
    def __init__(self, members: list[str]):
        self._members = [{"@odata.id": f"/redfish/v1/UpdateService/FirmwareInventory/{m}"}
                         for m in members]

    async def get_json(self, path: str) -> dict:
        if path.endswith("/FirmwareInventory"):
            return {"Members": self._members}
        leaf = path.rsplit("/", 1)[-1]
        return {"Id": leaf, "Version": "1.0", "Updateable": True}


@pytest.mark.parametrize("members, want_bios, want_bmc", [
    # Group 1 (8 BMCs): Tyrone Camarero ES418INW / ES426ANWGN — single image
    (["BIOS", "BMC", "CPLD"],
     "BIOS", "BMC"),

    # Group 2 (6 BMCs): Tyrone / RDA200A2G / RH21XM — single + extra CPLD
    (["BIOS", "BMC", "CPLD", "CPLD_FW_1"],
     "BIOS", "BMC"),

    # Group 3 (5 BMCs): ADI200C3A-54 / MDI300A2N / Tyrone Camarero VEGA —
    # SINGLE BIOS, DUAL-IMAGE BMC. Must pick BMCImage1, NOT BMCImage2.
    (["BIOS", "BMCImage1", "BMCImage2", "CPLD"],
     "BIOS", "BMCImage1"),

    # Group 4 (4 BMCs): Tyrone Camarero ES381AMS — DUAL BIOS + DUAL BMC.
    # Must pick BIOSImage1 + BMCImage1, NOT *Image2 (those are recovery).
    (["BIOSImage1", "BIOSImage2", "BMCImage1", "BMCImage2", "CPLD"],
     "BIOSImage1", "BMCImage1"),

    # Group 5 (2 BMCs): ADI200C3A-54 variant — SINGLE BIOS, DUAL BMC
    (["BIOS", "BMCImage1", "BMCImage2", "CPLD"],
     "BIOS", "BMCImage1"),

    # Group 6 (1 BMC, 172.16.12.180 DSA700TN-48RT): rich inventory
    (["BIOS", "BMC", "BP", "CPLD", "FPGA", "PSU", "RETIMER"],
     "BIOS", "BMC"),

    # Supermicro-Tyrone hybrid (172.16.12.138 in the operator's DB):
    # primary banks + Backup/Golden/Staging — must SKIP recovery banks.
    (["BMC", "Backup_BMC", "Golden_BMC", "Staging_BMC",
      "BIOS", "Backup_BIOS", "Golden_BIOS", "Staging_BIOS"],
     "BIOS", "BMC"),
])
@pytest.mark.asyncio
async def test_resolver_picks_active_bank_across_fleet(members, want_bios, want_bmc):
    plugin = GenericRedfishPlugin()
    client = _StubClient(members)
    bios = await plugin._resolve_firmware_target(client, "BIOS")
    bmc = await plugin._resolve_firmware_target(client, "BMC")
    assert bios.endswith(f"/{want_bios}"), f"BIOS picked {bios!r}, want .../{want_bios}"
    assert bmc.endswith(f"/{want_bmc}"), f"BMC picked {bmc!r}, want .../{want_bmc}"


@pytest.mark.asyncio
async def test_resolver_never_picks_image2_or_backup():
    """Critical safety check — Image2 / Backup banks are the operator's
    rollback path. Flashing them destroys recovery. The resolver must
    never return one even if it's the only inventory entry."""
    plugin = GenericRedfishPlugin()
    # Only secondary banks present — should raise, not return one
    client = _StubClient(["BMCImage2", "BIOSImage2", "Backup_BMC", "Golden_BIOS"])
    import pytest as _pt
    from core.plugins_api.redfish_client import RedfishError
    with _pt.raises(RedfishError):
        await plugin._resolve_firmware_target(client, "BIOS")
    with _pt.raises(RedfishError):
        await plugin._resolve_firmware_target(client, "BMC")
