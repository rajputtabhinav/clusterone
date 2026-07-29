"""Lenovo XCC (XClarity Controller) plugin.

XCC implements standard DMTF Redfish UpdateService. The upload contract:

* ``MultipartHttpPushUri`` = ``/mfwupdate``
* Multipart: ``UpdateParameters`` + ``UpdateFile`` (no ``OemParameters``)
* Targets are ``FirmwareInventory/<id>`` URIs. Common IDs:
  ``BMC-Primary``, ``BMC-Backup``, ``UEFI`` (BIOS), ``LXPM``,
  ``LXPMLinuxDriver``, ``LXPMWindowsDriver``, plus PLDM/agentless
  adapter entries.
* ``@Redfish.OperationApplyTime`` supports ``Immediate``, ``OnReset``,
  and ``OnStartUpdateRequest`` (stage + commit via ``StartUpdate``).
* Response is ``202 + Location: /redfish/v1/TaskService/Tasks/<id>`` —
  same as DMTF baseline.

XCC's only divergence from the generic flow is the BIOS apply path: when
``apply_now`` is False we want the image staged for the next host reboot.
``OnReset`` does this cleanly; the generic baseline already uses it.

Gotchas:
* BMC-Backup is the recovery image — flashing it first then BMC-Primary
  is the safe sequence for the ``BMC`` target.
* The plain ``HttpPushUri`` is ``/fwupdate`` (legacy / fallback). Always
  prefer the discovered ``MultipartHttpPushUri``.

Validated against Lenovo's ThinkSystem XCC REST API docs
(pubs.lenovo.com/xcc-restapi) and HT511484.
"""
from __future__ import annotations

from core.plugins_api.base import RedfishProbe
from plugins.generic_redfish.plugin import GenericRedfishPlugin


class LenovoPlugin(GenericRedfishPlugin):
    key = "lenovo"
    display_name = "Lenovo (XCC)"
    supported_models = (
        "ThinkSystem SR*", "ThinkSystem SD*", "ThinkSystem ST*",
        "ThinkSystem SE*", "ThinkServer*",
    )

    def match(self, probe: RedfishProbe) -> float:
        manuf = (probe.manufacturer or "").lower()
        if manuf == "lenovo" or manuf.startswith("lenovo "):
            return 0.92
        for block in probe.oem_blocks:
            if "lenovo" in (block or "").lower():
                return 0.82
        return 0.0


PLUGIN = LenovoPlugin()
