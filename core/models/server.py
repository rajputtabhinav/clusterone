from __future__ import annotations

from dataclasses import dataclass, field

from core.models.drive import Drive


@dataclass
class Credentials:
    username: str
    password: str


@dataclass
class ServerInfo:
    ip: str
    hostname: str | None = None
    manufacturer: str | None = None
    oem: str | None = None          # normalized vendor key (supermicro/dell/…)
    model: str | None = None
    serial: str | None = None
    bmc_version: str | None = None
    bios_version: str | None = None
    power_state: str = "Unknown"
    status: str = "unknown"          # online/offline/updating/failed/needs_update
    protocol: str | None = None      # redfish/ipmi/ssh
    redfish_root: str | None = None
    tls_fingerprint: str | None = None
    # Cached disk inventory for the OS provisioning workflow. Populated by
    # plugin.get_storage_inventory after discovery succeeds; persisted on
    # servers.disks_json so the resolver doesn't re-poll for every preview.
    drives: list[Drive] = field(default_factory=list)
