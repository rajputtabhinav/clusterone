"""AMI MegaRAC shared base.

AMI MegaRAC is the BMC firmware stack underneath Supermicro, ASRock Rack,
Gigabyte, several ASUS models, Tyrone Systems, MiTAC, and many ODMs. They all
share a non-DMTF firmware-update API:

* Upload URI: ``/redfish/v1/UpdateService/upload`` (AMI extension, not the
  DMTF-standard ``MultipartHttpPushUri``)
* Multipart MUST include a third ``OemParameters`` part —
  ``{"ImageType": "BIOS"|"BMC"}`` — otherwise the BMC rejects with HTTP 400
* Targets are ``/redfish/v1/UpdateService/FirmwareInventory/BIOS`` or
  ``/BMC`` (DMTF-compliant)
* BIOS applies on host reset (some require the OEM install action; see
  Supermicro override)

This mixin lets per-vendor plugins (Supermicro, ASRock, Gigabyte, ASUS)
share the upload recipe while still overriding the install-trigger and
match-confidence specifics.
"""
from __future__ import annotations

from plugins.generic_redfish.plugin import GenericRedfishPlugin


class AmiMegaracPlugin(GenericRedfishPlugin):
    """Base class for AMI MegaRAC-based vendors.

    Overrides ``_update_targets`` to keep the FirmwareInventory URI (default)
    and ``_oem_update_params`` to inject the required ``ImageType`` field.
    The generic ``_do_update`` flow handles everything else; vendor-specific
    install actions slot into per-vendor subclasses.
    """

    # Subclasses set this if the BMC advertises an OEM upload URI under a
    # specific name in UpdateService.json (rare on modern MegaRAC). Most use
    # the DMTF ``MultipartHttpPushUri`` advertisement directly.
    _AMI_UPLOAD_URI_HINT = "/redfish/v1/UpdateService/upload"

    def _oem_update_params(self, target: str) -> dict | None:
        # The single field AMI MegaRAC requires. Some firmware also accepts
        # PreserveConfiguration / Config / Action keys for fine-grained BIOS
        # behavior — Supermicro overrides this with the richer dict.
        return {"ImageType": target}
