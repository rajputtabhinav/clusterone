"""ASUS server plugin.

ASUS server BMCs (ASMB on the RS / ESC families) ship AMI MegaRAC firmware,
so they share the AMI upload contract used by Supermicro and ASRock Rack:

* POST ``/redfish/v1/UpdateService/upload``
* Three-part multipart: UpdateParameters + OemParameters + UpdateFile
* OemParameters requires ``{"ImageType": "BIOS"|"BMC"}``
* Targets are ``FirmwareInventory/BIOS|BMC``

ASMB-specific extras (e.g. recovery-bank handling) override per subclass.
"""
from __future__ import annotations

from core.plugins_api.base import RedfishProbe
from plugins.ami_megarac_base import AmiMegaracPlugin


class AsusPlugin(AmiMegaracPlugin):
    key = "asus"
    display_name = "ASUS"
    supported_models = ("RS*", "ESC*", "RS720*", "RS500*", "ESC4000*", "ESC8000*")

    def match(self, probe: RedfishProbe) -> float:
        manuf = (probe.manufacturer or "").lower()
        if manuf == "asus" or "asustek" in manuf or "asus technology" in manuf:
            return 0.92
        for block in probe.oem_blocks:
            if "asus" in (block or "").lower():
                return 0.82
        return 0.0


PLUGIN = AsusPlugin()
