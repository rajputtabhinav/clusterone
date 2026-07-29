"""MSI server plugin.

MSI server BMCs on the S-series platforms run AMI MegaRAC like the other ODM
boards, so they share the AMI upload contract: ``/UpdateService/upload`` with
the three-part multipart body and ``OemParameters={"ImageType":...}``.
"""
from __future__ import annotations

from core.plugins_api.base import RedfishProbe
from plugins.ami_megarac_base import AmiMegaracPlugin


class MsiPlugin(AmiMegaracPlugin):
    key = "msi"
    display_name = "MSI"
    supported_models = ("S*", "S2206*", "S2207*", "S2208*", "S2209*")

    def match(self, probe: RedfishProbe) -> float:
        manuf = (probe.manufacturer or "").lower()
        if manuf == "msi" or "micro-star international" in manuf or "micro star" in manuf:
            return 0.92
        # Anchored "msi" — not a bare substring. Avoid matching unrelated
        # PCI / register OEM block names like ``nvm_msi`` or ``pci_msi``
        # that appear on Cisco / Pure Storage / generic hardware.
        for block in probe.oem_blocks:
            b = (block or "").lower().strip()
            if b == "msi" or b.startswith("msi.") or b.startswith("msi_"):
                return 0.82
        return 0.0


PLUGIN = MsiPlugin()
