"""Gigabyte server plugin.

Gigabyte server BMCs run AMI MegaRAC firmware (same stack as Supermicro and
ASRock Rack). They follow the AMI upload contract:

* POST ``/redfish/v1/UpdateService/upload`` with the three-part multipart
  body (UpdateParameters + OemParameters + UpdateFile)
* OemParameters requires ``{"ImageType": "BIOS"|"BMC"}``
* Targets are ``/UpdateService/FirmwareInventory/BIOS|BMC``
* The upload alone starts the task; no extra install action required for
  modern Gigabyte server BMCs (R-series, H-series, S-series).
"""
from __future__ import annotations

from core.plugins_api.base import RedfishProbe
from plugins.ami_megarac_base import AmiMegaracPlugin


class GigabytePlugin(AmiMegaracPlugin):
    key = "gigabyte"
    display_name = "Gigabyte (MegaRAC)"
    supported_models = ("R*-Z*", "R*-G*", "H*-Z*", "S*-Z*", "G*-Z*")

    def match(self, probe: RedfishProbe) -> float:
        manuf = (probe.manufacturer or "").lower()
        # "Giga Computing" is Gigabyte's current server-division brand; modern
        # G-series boards report it instead of "Gigabyte".
        if (manuf == "gigabyte" or "gigabyte technology" in manuf
                or "giga computing" in manuf):
            return 0.92
        for block in probe.oem_blocks:
            b = (block or "").lower()
            if "gigabyte" in b or "giga computing" in b:
                return 0.82
        return 0.0


PLUGIN = GigabytePlugin()
