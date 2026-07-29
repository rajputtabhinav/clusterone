"""ISO library record (mirrors ``Firmware`` for install media)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Iso:
    id: int
    name: str
    os_family: str          # 'rhel'|'rocky'|'alma'|'ubuntu'|'debian'|'esxi'|'proxmox'|'windows'
    os_version: str
    arch: str
    file_path: str
    sha256: str
    size_bytes: int = 0
    source_url: str | None = None
    uploaded_at: str | None = None
    uploaded_by: str | None = None
    notes: str | None = None
