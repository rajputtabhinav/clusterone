"""ASRock Rack plugin.

ASRock Rack BMCs run AMI MegaRAC firmware (same stack as Supermicro). They
share the AMI upload contract:

* POST ``/redfish/v1/UpdateService/upload`` (advertised via
  ``MultipartHttpPushUri``) with a multipart body containing
  ``UpdateParameters``, ``UpdateFile``, and ``OemParameters``
  (``{"ImageType":"BIOS"|"BMC"}``)
* Targets are ``FirmwareInventory/BIOS`` / ``BMC`` URIs
* No vendor-specific install action — the upload itself starts the task,
  unlike Supermicro which needs ``SmcUpdateService.Install`` for BIOS

Validated against ASRock Rack's Redfish FAQ #66 and Tyrone Systems documentation.
"""
from __future__ import annotations

from core.plugins_api.base import RedfishProbe
from plugins.ami_megarac_base import AmiMegaracPlugin


class AsrockPlugin(AmiMegaracPlugin):
    key = "asrock"
    display_name = "ASRock Rack"
    supported_models = ("GENOA*", "EPYC*", "ROMED*", "X570D*", "B650D*", "ROME*", "TURIN*")

    def match(self, probe: RedfishProbe) -> float:
        manuf = (probe.manufacturer or "").lower().replace(" ", "")
        if "asrockrack" in manuf or "asrock" in manuf:
            return 0.92
        for block in probe.oem_blocks:
            b = (block or "").lower().replace(" ", "")
            if "asrock" in b:
                return 0.82
        return 0.0


PLUGIN = AsrockPlugin()
