"""Dell iDRAC plugin.

iDRAC9 and iDRAC10 advertise ``MultipartHttpPushUri`` =
``/redfish/v1/UpdateService/MultipartUpload``. The recipe is mostly DMTF
but diverges in two specific ways:

1. **Targets MUST be empty (``[]``).** Dell auto-routes by reading the DUP /
   ``.d7`` / ``.d9`` header. Passing a FirmwareInventory URI here causes
   error ``RED097`` (target mismatch). This is the inverse of AMI MegaRAC,
   which requires a FirmwareInventory target — hence the OEM hook override.

2. **No ``OemParameters`` part.** The base ``UpdateParameters`` JSON with
   ``Targets`` + ``@Redfish.OperationApplyTime`` is sufficient. (A third
   part is accepted but not required.)

ApplyTime supported: ``Immediate`` (apply + iDRAC/host reset now) and
``OnReset`` (stage for next host power-cycle). The response is
``202 + Location: /redfish/v1/TaskService/Tasks/JID_xxxxxxxxxxxx`` — the
generic poll handles that fine.

Validated against Dell's own ``DeviceFirmwareMultipartUploadREDFISH.py``
reference script (github.com/dell/iDRAC-Redfish-Scripting).
"""
from __future__ import annotations

from core.plugins_api.base import RedfishProbe
from plugins.generic_redfish.plugin import GenericRedfishPlugin


class DellPlugin(GenericRedfishPlugin):
    key = "dell"
    display_name = "Dell (iDRAC)"
    supported_models = (
        "PowerEdge R*", "PowerEdge C*", "PowerEdge M*", "PowerEdge T*",
        "PowerEdge XR*", "PowerEdge XE*", "PowerEdge FC*",
    )

    # iDRAC9/10 publishes the OS-install Virtual Media slot as `RemovableDisk`
    # (vs the DMTF-standard `CD` that GenericRedfishPlugin assumes). Setting
    # this class field is enough — every InsertMedia / EjectMedia call uses it.
    _VM_SLOT = "RemovableDisk"

    def match(self, probe: RedfishProbe) -> float:
        manuf = (probe.manufacturer or "").lower()
        if manuf.startswith("dell"):
            return 0.92
        for block in probe.oem_blocks:
            if "dell" in (block or "").lower():
                return 0.82
        return 0.0

    # ---- Dell-specific multipart shape -------------------------------
    def _update_targets(self, target_uri: str) -> list[str]:
        """iDRAC auto-routes by DUP/`.d7` header — passing a target URI causes
        RED097. Always empty for Dell."""
        return []

    # Dell doesn't need or want OemParameters — generic returns None which
    # is correct. No override required.


PLUGIN = DellPlugin()
