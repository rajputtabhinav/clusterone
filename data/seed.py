"""Optional demo data (no longer applied at startup).

This module previously ran on first launch via the container so empty pages
wouldn't look broken. The app now starts truly empty — proper empty-state
CTAs ("No servers — Run Discovery") guide the user from a clean install.

Keep this around as a dev utility: import and call ``seed_if_empty(...)``
manually for screenshots, demos, or QA fixtures.
"""
from __future__ import annotations

from datetime import datetime, timezone

from data.repositories.activity_repo import ActivityRepo
from data.repositories.firmware_repo import FirmwareRepo
from data.repositories.servers_repo import ServersRepo

_SERVERS = [
    {"ip": "10.10.10.11", "hostname": "node-a01", "oem": "supermicro", "manufacturer": "Supermicro", "model": "H13DSG-O", "serial": "S1A0011", "bmc_version": "1.04.06", "bios_version": "2.1", "power_state": "On", "status": "online", "protocol": "redfish"},
    {"ip": "10.10.10.12", "hostname": "node-a02", "oem": "dell", "manufacturer": "Dell", "model": "R760", "serial": "DLR7602", "bmc_version": "7.10.30", "bios_version": "1.4", "power_state": "On", "status": "needs_update", "protocol": "redfish"},
    {"ip": "10.10.10.13", "hostname": "node-a03", "oem": "hpe", "manufacturer": "HPE", "model": "DL380 G11", "serial": "HPL3803", "bmc_version": "2.44", "bios_version": "1.6", "power_state": "On", "status": "failed", "protocol": "redfish"},
    {"ip": "10.10.10.14", "hostname": "node-a04", "oem": "lenovo", "manufacturer": "Lenovo", "model": "SR650 V3", "serial": "LNV6504", "bmc_version": "3.10", "bios_version": "2.0", "power_state": "On", "status": "online", "protocol": "redfish"},
    {"ip": "10.10.10.15", "hostname": "node-a05", "oem": "gigabyte", "manufacturer": "Gigabyte", "model": "R283-Z90", "serial": "GBZ9005", "bmc_version": "12.05", "bios_version": "C20", "power_state": "On", "status": "online", "protocol": "redfish"},
    {"ip": "10.10.10.16", "hostname": "node-a06", "oem": "asrock", "manufacturer": "ASRock Rack", "model": "GENOA-D8", "serial": "ASR8006", "bmc_version": "1.18", "bios_version": "3.2", "power_state": "Off", "status": "offline", "protocol": "ipmi"},
    {"ip": "10.10.10.17", "hostname": "node-a07", "oem": "asus", "manufacturer": "ASUS", "model": "RS720A", "serial": "ASU7207", "bmc_version": "1.34", "bios_version": "0805", "power_state": "On", "status": "needs_update", "protocol": "redfish"},
    {"ip": "10.10.10.18", "hostname": "node-a08", "oem": "msi", "manufacturer": "MSI", "model": "S2206", "serial": "MSI2208", "bmc_version": "1.02", "bios_version": "1.1", "power_state": "On", "status": "online", "protocol": "redfish"},
]

_FIRMWARE = [
    {"name": "SMC_H13_BIOS_1.6.bin", "type": "BIOS", "oem": "supermicro", "model": "H13DSG-O", "version": "1.6", "sha256": "seed-smc-bios-16", "size_bytes": 33554432},
    {"name": "SMC_BMC_1.04.06.bin", "type": "BMC", "oem": "supermicro", "model": "H13DSG-O", "version": "1.04.06", "sha256": "seed-smc-bmc-10406", "size_bytes": 33554432},
    {"name": "iDRAC_7.10.30.exe", "type": "BMC", "oem": "dell", "model": "R760", "version": "7.10.30", "sha256": "seed-dell-idrac-71030", "size_bytes": 167772160},
    {"name": "HGX_H100_1.3.fwpkg", "type": "HGX", "oem": "nvidia", "model": "HGX-H100", "version": "1.3", "sha256": "seed-hgx-h100-13", "size_bytes": 268435456},
    {"name": "iLO6_2.44.bin", "type": "BMC", "oem": "hpe", "model": "DL380 G11", "version": "2.44", "sha256": "seed-hpe-ilo6-244", "size_bytes": 67108864},
]

_ACTIVITY = [
    {"level": "success", "category": "update", "message": "BIOS update completed — 7/8 succeeded"},
    {"level": "error", "category": "update", "message": "node-a03 failed verification (version mismatch)"},
    {"level": "info", "category": "update", "message": "BIOS update started on 8 servers"},
    {"level": "success", "category": "discovery", "message": "8 servers found"},
    {"level": "info", "category": "discovery", "message": "Discovery started (10.10.10.1-254)"},
    {"level": "info", "category": "firmware", "message": "Firmware SMC_H13_BIOS_1.6.bin uploaded"},
]


def seed_if_empty(servers: ServersRepo, firmware: FirmwareRepo, activity: ActivityRepo) -> None:
    if servers.count() == 0:
        for s in _SERVERS:
            servers.upsert(s)
    if firmware.count() == 0:
        # Full ISO-8601 (matches FirmwareRepo._now()) so seed + live rows sort
        # consistently under ORDER BY uploaded_at.
        today = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        for f in _FIRMWARE:
            firmware.add({**f, "file_path": f"seed://{f['name']}", "uploaded_at": today, "uploaded_by": "demo"})
    if activity.count() == 0:
        # Insert oldest-first so DESC ordering shows newest at top.
        now = datetime.now(timezone.utc)
        for i, a in enumerate(reversed(_ACTIVITY)):
            ts = now.replace(microsecond=0).isoformat()
            activity.log(level=a["level"], category=a["category"], message=a["message"], ts=ts)
