from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Firmware:
    name: str
    type: str           # BMC | BIOS | HGX
    oem: str
    version: str
    file_path: str
    sha256: str
    id: int | None = None
    model: str | None = None
    size_bytes: int = 0
