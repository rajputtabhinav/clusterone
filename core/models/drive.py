"""Server-side disk inventory model.

Populated by ``OEMPlugin.get_storage_inventory`` and cached on
``servers.disks_json``. The disk resolver matches selection strategies
(``first_nvme``, ``smallest_ssd``, ``by_size:480GB±10%`` …) against this
shape; the autoinstall renderer translates the chosen ``Drive`` into
the per-OS-family installer syntax.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Drive:
    name: str                 # Linux convention, normalized: 'nvme0n1', 'sda', 'sdb' …
    protocol: str             # 'NVMe' | 'SATA' | 'SAS' | 'PCIe' | 'SCSI'
    media_type: str           # 'SSD' | 'HDD' | 'SMR' | 'Unknown'
    capacity_bytes: int
    model: str
    serial: str = ""
    slot: str | None = None   # physical slot / drive bay label, when reported
    redfish_uri: str = ""     # /redfish/v1/Systems/.../Storage/.../Drives/N

    @property
    def capacity_gb(self) -> float:
        return self.capacity_bytes / (1000 ** 3)

    @property
    def is_nvme(self) -> bool:
        return self.protocol.upper() == "NVME"

    @property
    def is_ssd(self) -> bool:
        return self.media_type.upper() == "SSD"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Drive":
        # Keep only fields we know about so unknown keys don't choke us.
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})
